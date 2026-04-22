from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import AsyncExitStack
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import Optional, Sequence, Union

from aioquic.asyncio import QuicConnectionProtocol, connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import DatagramFrameReceived, StreamDataReceived

from . import (
    control,
    events as _events_mod,
    motion as _motion_mod,
    telemetry as _telemetry_mod,
)
from .discovery import EXPECTED_ALPN, Controller

log = logging.getLogger(__name__)

_DEFAULT_CONNECT_TIMEOUT = 5.0
_DEFAULT_IDLE_TIMEOUT = 15.0
_DEFAULT_REQUEST_TIMEOUT = 5.0


class _RuvionProtocol(QuicConnectionProtocol):
    """QuicConnectionProtocol that routes:
      - client-initiated bidi streams (stream_id % 4 == 0) -> per-stream queues
        for request/response (control_stream.fbs)
      - server-initiated uni streams (stream_id % 4 == 3)  -> fan-out to event
        subscribers on FIN (event.fbs — one event per stream)
      - QUIC datagrams                                     -> fan-out to
        telemetry subscribers (telemetry_datagram.fbs)
    """

    # Drop oldest on overflow to preserve datagram "latest wins" semantics.
    _DATAGRAM_QUEUE_SIZE = 16
    # Events are reliable and usually sparse — small queue is fine.
    _EVENT_QUEUE_SIZE = 64

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stream_queues: dict[int, asyncio.Queue] = {}
        self._datagram_subscribers: list[asyncio.Queue] = []
        self._event_subscribers: list[asyncio.Queue] = []
        self._event_buffers: dict[int, bytearray] = {}

    def register_stream(self, stream_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._stream_queues[stream_id] = q
        return q

    def unregister_stream(self, stream_id: int) -> None:
        self._stream_queues.pop(stream_id, None)

    def subscribe_datagrams(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._DATAGRAM_QUEUE_SIZE)
        self._datagram_subscribers.append(q)
        return q

    def unsubscribe_datagrams(self, q: asyncio.Queue) -> None:
        try:
            self._datagram_subscribers.remove(q)
        except ValueError:
            pass

    def subscribe_events(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._EVENT_QUEUE_SIZE)
        self._event_subscribers.append(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue) -> None:
        try:
            self._event_subscribers.remove(q)
        except ValueError:
            pass

    def _dispatch_event_frame(self, data: bytes) -> None:
        for q in self._event_subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                log.warning("event queue full, dropping event frame")

    def quic_event_received(self, event) -> None:
        if isinstance(event, StreamDataReceived):
            q = self._stream_queues.get(event.stream_id)
            if q is not None:
                q.put_nowait((event.data, event.end_stream))
                return
            # Server-initiated uni streams carry one Event each, FIN-terminated.
            if event.stream_id % 4 == 3:
                buf = self._event_buffers.setdefault(event.stream_id, bytearray())
                buf.extend(event.data)
                if event.end_stream:
                    self._dispatch_event_frame(bytes(buf))
                    self._event_buffers.pop(event.stream_id, None)
        elif isinstance(event, DatagramFrameReceived):
            for q in self._datagram_subscribers:
                try:
                    q.put_nowait(event.data)
                except asyncio.QueueFull:
                    # Drop oldest, add newest — latest-wins for telemetry
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(event.data)
                    except asyncio.QueueFull:
                        pass


class CertificateError(RuntimeError):
    pass


class ALPNMismatchError(RuntimeError):
    pass


class ConnectError(RuntimeError):
    pass


@dataclass
class CertBundle:
    """Paths to the three PEM files needed for mTLS against a Ruvion controller."""

    ca: Path
    client_cert: Path
    client_key: Path

    @classmethod
    def from_dir(cls, path: Union[str, Path]) -> "CertBundle":
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise CertificateError(f"Cert directory does not exist: {root}")
        ca = root / "ca.crt"
        cert = root / "client.crt"
        key = root / "client.key"
        missing = [p.name for p in (ca, cert, key) if not p.is_file()]
        if missing:
            raise CertificateError(
                f"Missing in {root}: {', '.join(missing)} "
                f"(expected ca.crt, client.crt, client.key)"
            )
        return cls(ca=ca, client_cert=cert, client_key=key)


class Connection:
    """Open QUIC+mTLS connection to a Ruvion controller.

    Exposes the control-stream API: typed request/response for each of the 15
    StreamCommand variants, plus a `claim()` context manager. Datagram and
    event-stream channels land on this object in Schritte 4-6.
    """

    def __init__(
        self,
        protocol: _RuvionProtocol,
        controller: Controller,
        remote_address: str,
        _stack: AsyncExitStack,
    ) -> None:
        self._protocol = protocol
        self.controller = controller
        self.remote_address = remote_address
        self._stack = _stack
        self._closed = False
        self._motion_seq = 1

    @property
    def protocol(self) -> _RuvionProtocol:
        return self._protocol

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stack.aclose()

    async def __aenter__(self) -> "Connection":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ---------- Low-level request/response ----------

    async def request(
        self, payload: bytes, *, timeout: float = _DEFAULT_REQUEST_TIMEOUT
    ) -> bytes:
        """Send `payload` on a new bidirectional QUIC stream with FIN, collect
        the server's reply (also FIN-terminated), return it as bytes.
        """
        if self._closed:
            raise ConnectError("connection is closed")
        quic = self._protocol._quic
        stream_id = quic.get_next_available_stream_id()  # bidi
        q = self._protocol.register_stream(stream_id)
        try:
            quic.send_stream_data(stream_id, payload, end_stream=True)
            self._protocol.transmit()

            buf = bytearray()

            async def _read() -> bytes:
                while True:
                    data, end = await q.get()
                    buf.extend(data)
                    if end:
                        return bytes(buf)

            return await asyncio.wait_for(_read(), timeout=timeout)
        finally:
            self._protocol.unregister_stream(stream_id)

    async def _execute(self, payload: bytes) -> str:
        """Send command payload, parse response, raise on error. Returns
        server's debug message (empty string on clean Ok)."""
        data = await self.request(payload)
        _, message = control.raise_on_error(data)
        return message

    # ---------- Control ownership ----------

    async def claim_control(self, *, force: bool = False) -> None:
        await self._execute(control.build_claim_control(force=force))

    async def release_control(self) -> None:
        await self._execute(control.build_release_control())

    def claim(self, *, force: bool = False) -> "_ClaimContext":
        """Context manager: claim on enter, release on exit (always)."""
        return _ClaimContext(self, force=force)

    # ---------- Safety / faults ----------

    async def clear_fault(self) -> None:
        await self._execute(control.build_clear_fault())

    async def quickstop(self, state: control.QuickstopState) -> None:
        await self._execute(control.build_quickstop(state))

    # ---------- Motion control ----------

    async def stop(self) -> None:
        await self._execute(control.build_stop())

    async def set_drive_mode(self, mode: control.DriveMode) -> None:
        await self._execute(control.build_set_drive_mode(mode))

    async def single_set_speed_override(
        self,
        *,
        velocity_factor: float = 1.0,
        acceleration_factor: float = 1.0,
    ) -> None:
        await self._execute(
            control.build_single_set_speed_override(
                velocity_factor=velocity_factor,
                acceleration_factor=acceleration_factor,
            )
        )

    async def humanoid_set_speed_override(
        self,
        *,
        right_velocity_factor: float = 1.0,
        right_acceleration_factor: float = 1.0,
        left_velocity_factor: float = 1.0,
        left_acceleration_factor: float = 1.0,
    ) -> None:
        await self._execute(
            control.build_humanoid_set_speed_override(
                right_velocity_factor=right_velocity_factor,
                right_acceleration_factor=right_acceleration_factor,
                left_velocity_factor=left_velocity_factor,
                left_acceleration_factor=left_acceleration_factor,
            )
        )

    # ---------- Calibration / zeroing ----------

    async def calibrate(self, calibration_type: control.CalibrationType) -> None:
        await self._execute(control.build_calibration(calibration_type))

    async def single_zeroing(self, joints: "Sequence[int]") -> None:  # noqa: F821
        await self._execute(control.build_single_zeroing(joints))

    async def humanoid_zeroing(
        self,
        *,
        right_joints: Optional["Sequence[int]"] = None,  # noqa: F821
        left_joints: Optional["Sequence[int]"] = None,  # noqa: F821
    ) -> None:
        await self._execute(
            control.build_humanoid_zeroing(
                right_joints=right_joints, left_joints=left_joints
            )
        )

    async def set_streaming_params(
        self,
        *,
        kp: Optional["Sequence[float]"] = None,  # noqa: F821
        kd: Optional["Sequence[float]"] = None,  # noqa: F821
        t_la: float = 0.0,
    ) -> None:
        await self._execute(
            control.build_set_streaming_params(kp=kp, kd=kd, t_la=t_la)
        )

    # ---------- Motion commands (datagrams) ----------

    def _next_seq(self, seq: Optional[int]) -> int:
        if seq is not None:
            self._motion_seq = seq + 1
            return seq
        s = self._motion_seq
        self._motion_seq += 1
        return s

    def _check_mode(self, required: str) -> None:
        mode = (self.controller.mode or "").lower()
        if mode and mode != required:
            raise ValueError(
                f"Controller is '{mode}', not '{required}' — "
                f"use the {required}-specific method"
            )

    def send_motion_datagram(self, payload: bytes) -> None:
        """Low-level: send raw MotionCommand bytes as a QUIC datagram.
        Caller is responsible for framing/seq/ttl; prefer send_single_*
        or send_humanoid_* for typed builders.
        """
        if self._closed:
            raise ConnectError("connection is closed")
        self._protocol._quic.send_datagram_frame(payload)
        self._protocol.transmit()

    def send_single(
        self,
        motion: "_motion_mod.ArmMotion",
        *,
        seq: Optional[int] = None,
        ttl_ms: int = 0,
    ) -> int:
        """Send a single-arm MotionCommand. Returns the seq used.

        motion: JointPose(...) or JointTwist(...).
        ttl_ms: brake-deadline. 0 (default) = no deadline — use for single-shot
                JointPose where the server plans the trajectory to target. Set
                to a short value (e.g. 100) only when continuously streaming;
                the server brakes if no successor arrives in that window.
        seq:    optional override; defaults to an auto-incrementing counter.
        """
        self._check_mode("single")
        s = self._next_seq(seq)
        payload = _motion_mod.build_single(motion, seq=s, ttl_ms=ttl_ms)
        self.send_motion_datagram(payload)
        return s

    def send_humanoid(
        self,
        right: "_motion_mod.ArmMotion",
        left: "_motion_mod.ArmMotion",
        *,
        seq: Optional[int] = None,
        ttl_ms: int = 0,
    ) -> int:
        """Send a humanoid (dual-arm) MotionCommand. Returns the seq used.

        See send_single() for the meaning of ttl_ms.
        """
        self._check_mode("humanoid")
        s = self._next_seq(seq)
        payload = _motion_mod.build_humanoid(right, left, seq=s, ttl_ms=ttl_ms)
        self.send_motion_datagram(payload)
        return s

    # ---------- Events (server-initiated uni streams) ----------

    async def events(self):
        """Yield parsed events (DriveStatusEvent, MotionStatusEvent,
        ActivityEvent, DriveModeEvent, ControllerEvent) as they arrive.
        Reliable — events are neither lost nor reordered.
        """
        q = self._protocol.subscribe_events()
        try:
            while True:
                data = await q.get()
                try:
                    yield _events_mod.parse_event(data)
                except Exception as e:  # noqa: BLE001
                    log.debug("event parse failed: %s", e)
                    continue
        finally:
            self._protocol.unsubscribe_events(q)

    # ---------- Telemetry (datagrams) ----------

    async def telemetry(self):
        """Yield parsed Telemetry objects as they arrive on the QUIC DATAGRAM
        channel. Lossy by design ('latest wins'). No control token required.
        """
        q = self._protocol.subscribe_datagrams()
        try:
            while True:
                data = await q.get()
                try:
                    yield _telemetry_mod.parse_telemetry(data)
                except Exception as e:  # noqa: BLE001
                    log.debug("telemetry parse failed: %s", e)
                    continue
        finally:
            self._protocol.unsubscribe_datagrams(q)

    # ---------- System ----------

    async def check_update(self) -> None:
        await self._execute(control.build_check_update())

    async def apply_update(self) -> None:
        await self._execute(control.build_apply_update())

    async def reboot(self) -> None:
        await self._execute(control.build_reboot())


class _ClaimContext:
    """async context manager returned by Connection.claim()."""

    def __init__(self, conn: Connection, *, force: bool) -> None:
        self._conn = conn
        self._force = force

    async def __aenter__(self) -> Connection:
        await self._conn.claim_control(force=self._force)
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._conn.release_control()
        except Exception as e:  # noqa: BLE001
            log.warning("release_control failed during context exit: %s", e)


def _format_address(addr) -> str:
    """Return an address string suitable for aioquic.

    IPv6 with scope-id (fe80::1%en0) is kept as-is — Python's socket layer
    handles scoped addresses. IPv4 and global IPv6 are just their str().
    """
    return str(addr)


def _is_link_local(addr) -> bool:
    return isinstance(addr, IPv6Address) and addr.is_link_local


def _sort_addresses(addresses: list) -> list:
    """Prefer IPv4 > global IPv6 > ULA IPv6 > link-local IPv6.

    IPv4 ranks highest because the mDNS announce on the reference Rust
    controller sometimes omits A records even when the host has an IPv4
    address — when we *do* have it (e.g. resolved via hostname fallback),
    the LAN route is usually the most reliable.
    """

    def rank(a) -> int:
        if isinstance(a, IPv4Address):
            return 0
        if isinstance(a, IPv6Address):
            if a.is_link_local:
                return 3
            if a.is_private:  # ULA (fd00::/8)
                return 2
            return 1
        return 99

    return sorted(addresses, key=rank)


async def _resolve_hostname(hostname: str) -> list:
    """Look up A/AAAA records via the system resolver (mDNS/DNS).

    Used as a fallback when the mDNS announce didn't include IPv4 addresses.
    Returns a list of IPv4Address/IPv6Address.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            hostname, None, type=socket.SOCK_DGRAM
        )
    except socket.gaierror as e:
        log.debug("hostname %s lookup failed: %s", hostname, e)
        return []
    out: list = []
    seen: set = set()
    for family, _, _, _, sockaddr in infos:
        addr_str = sockaddr[0]
        # Strip IPv6 zone-id if present (e.g. "fe80::1%en0")
        bare = addr_str.split("%", 1)[0]
        if bare in seen:
            continue
        seen.add(bare)
        try:
            out.append(ip_address(addr_str))
        except ValueError:
            pass
    return out


async def _attempt_connect(
    address: str,
    port: int,
    config: QuicConfiguration,
    timeout: float,
) -> tuple[str, _RuvionProtocol, AsyncExitStack]:
    """Open a single QUIC connection. Returns protocol + the exit stack that
    must be closed to tear it down."""
    stack = AsyncExitStack()
    try:
        ctx = connect(
            address,
            port,
            configuration=config,
            create_protocol=_RuvionProtocol,
            wait_connected=True,
        )
        protocol = await asyncio.wait_for(stack.enter_async_context(ctx), timeout)
        # Swallow any stray exceptions on internal aioquic futures so they
        # don't trigger "Future exception was never retrieved" warnings when
        # this connection loses the parallel race and gets cancelled.
        _silence_internal_futures(protocol)
        return address, protocol, stack
    except BaseException:
        await stack.aclose()
        raise


def _silence_internal_futures(protocol: QuicConnectionProtocol) -> None:
    for attr in ("_connected_waiter", "_closed"):
        fut = getattr(protocol, attr, None)
        if isinstance(fut, asyncio.Future) and not fut.done():
            fut.add_done_callback(lambda f: f.exception())


async def connect_controller(
    controller: Controller,
    cert_dir: Union[str, Path],
    *,
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
    idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    server_name: Optional[str] = None,
    skip_alpn_check: bool = False,
) -> Connection:
    """Open a QUIC+mTLS connection to `controller`.

    Tries all advertised addresses in parallel; the first successful handshake
    wins, the others are torn down. Raises ConnectError if none succeed within
    `connect_timeout`.

    `server_name` overrides the TLS SNI (default: controller hostname without
    trailing dot, e.g. "debian.local"). If the server cert doesn't match that
    name, pass the correct one explicitly.
    """
    # Pre-flight ALPN check (skippable for older firmware)
    if not skip_alpn_check:
        if not controller.alpn:
            raise ALPNMismatchError(
                f"Controller {controller.serial} did not advertise ALPN; "
                f"expected {EXPECTED_ALPN!r}. Pass skip_alpn_check=True to bypass."
            )
        if controller.alpn != EXPECTED_ALPN:
            raise ALPNMismatchError(
                f"Controller {controller.serial} advertises ALPN "
                f"{controller.alpn!r}, client expects {EXPECTED_ALPN!r}"
            )

    if not controller.addresses:
        raise ConnectError(f"Controller {controller.serial} has no addresses")

    bundle = CertBundle.from_dir(cert_dir)

    # Server cert SAN = lowercased serial (confirmed empirically). Caller
    # can override with server_name=... for non-standard cert setups.
    sni = server_name if server_name is not None else controller.serial.lower()

    config = QuicConfiguration(
        is_client=True,
        alpn_protocols=[EXPECTED_ALPN],
        server_name=sni,
        idle_timeout=idle_timeout,
        max_datagram_frame_size=1200,  # Conservative MTU for IPv6 over Ethernet
    )
    config.load_verify_locations(cafile=str(bundle.ca))
    config.load_cert_chain(certfile=str(bundle.client_cert), keyfile=str(bundle.client_key))

    addresses = list(controller.addresses)
    if controller.hostname:
        extra = await _resolve_hostname(controller.hostname.rstrip("."))
        for a in extra:
            if a not in addresses:
                addresses.append(a)
    addresses = _sort_addresses(addresses)
    address_strs = [_format_address(a) for a in addresses]
    log.debug("connect order: %s", address_strs)

    # Parallel attempts: race all addresses with the full connect_timeout each.
    # First successful handshake wins; losers are cancelled and their aioquic
    # stacks torn down. This beats serial attempts when the first address is
    # unreachable (e.g. IPv4 from hostname fallback that doesn't actually route).
    tasks: dict[asyncio.Task, str] = {
        asyncio.create_task(
            _attempt_connect(addr, controller.port, config, connect_timeout)
        ): addr
        for addr in address_strs
    }
    errors: list[tuple[str, BaseException]] = []
    winner: Optional[tuple[str, _RuvionProtocol, AsyncExitStack]] = None
    try:
        while tasks and winner is None:
            done, _ = await asyncio.wait(
                tasks.keys(), return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                addr = tasks.pop(t)
                try:
                    winner = t.result()
                    break
                except BaseException as e:  # noqa: BLE001
                    errors.append((addr, e))
                    log.debug("connect %s failed: %s: %s", addr, type(e).__name__, e)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                late = await t
            except BaseException:  # noqa: BLE001
                pass
            else:
                # Late winner we don't need — close it so the controller sees
                # a clean shutdown instead of an idle-timeout dangle.
                _, _, late_stack = late
                try:
                    await late_stack.aclose()
                except BaseException:  # noqa: BLE001
                    pass

    if winner is None:
        err_summary = "; ".join(
            f"{a}: {e.__class__.__name__}: {e}" for a, e in errors
        )
        raise ConnectError(
            f"All {len(address_strs)} connection attempts failed for "
            f"{controller.serial}. Tried: {err_summary or 'no results'}"
        )

    addr_, protocol, stack = winner
    log.info(
        "Connected to %s via %s:%d (ALPN=%s)",
        controller.serial, addr_, controller.port, EXPECTED_ALPN,
    )
    return Connection(protocol, controller, addr_, stack)
