"""Telemetry reachability probe with full diagnostics.

Question: does anything even arrive? And if so, on which layer does it
fail — UDP, QUIC, DATAGRAM, parser?

Method: probe each layer bottom-up.

  1. environment (python / aioquic / OS)
  2. local network interfaces + default route
  3. mDNS discovery (with all advertised addresses)
  4. per-address reachability (ICMP ping + UDP socket open)
  5. QUIC handshake — parallel first, then sequential per-address on failure
  6. post-handshake: raw UDP tap (sendto/recvfrom at transport layer)
                    + QUIC event tap (StreamDataReceived / DatagramFrameReceived)
  7. telemetry + events drained in parallel; per-second heartbeat

Run:
    python examples/diagnose.py

Read-only — no claim/motion.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Optional

from aioquic.quic.configuration import QuicConfiguration
from aioquic.asyncio import connect as aioquic_connect

from ruvion_client import connect_controller, discover_once
from ruvion_client.discovery import EXPECTED_ALPN, Controller
from ruvion_client.transport import CertBundle, _RuvionProtocol

from config import CERT_DIR

OBSERVE_SECONDS = 10.0
DISCOVER_TIMEOUT = 5.0
PING_TIMEOUT = 2.0
UDP_PROBE_TIMEOUT = 1.0


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Environment / network info (before we touch the controller)
# ---------------------------------------------------------------------------

def _env_info() -> None:
    try:
        import aioquic  # type: ignore
        aioquic_ver = getattr(aioquic, "__version__", "?")
    except Exception:  # noqa: BLE001
        aioquic_ver = "?"
    log(f"python  {sys.version.split()[0]}   aioquic {aioquic_ver}   "
        f"{platform.platform()}")
    log(f"hostname {socket.gethostname()}   user-run at {time.strftime('%H:%M:%S')}")


def _interface_info() -> None:
    """Best-effort dump of local interfaces + default route. Helps identify
    wrong interface selected (e.g. VPN stealing traffic) or no route at all."""
    log("")
    log("local network interfaces:")
    ip_bin = shutil.which("ip")
    ifconfig_bin = shutil.which("ifconfig")
    try:
        if ip_bin:
            out = subprocess.check_output(
                [ip_bin, "-brief", "addr"], text=True, timeout=2.0
            )
            for line in out.strip().splitlines():
                log(f"  {line}")
        elif ifconfig_bin:
            out = subprocess.check_output(
                [ifconfig_bin], text=True, timeout=2.0
            )
            # Trim to first address lines only (too verbose otherwise).
            for line in out.splitlines():
                if any(k in line for k in ("flags=", "inet ", "inet6 ")):
                    log(f"  {line.strip()}")
        else:
            log("  (no `ip`/`ifconfig` on PATH — skipping interface dump)")
    except Exception as e:  # noqa: BLE001
        log(f"  interface dump failed: {e}")

    log("")
    log("default route(s):")
    try:
        if ip_bin:
            for fam in ("-4", "-6"):
                try:
                    out = subprocess.check_output(
                        [ip_bin, fam, "route", "show", "default"],
                        text=True,
                        timeout=2.0,
                    )
                    out = out.strip() or "(none)"
                    for line in out.splitlines():
                        log(f"  [{fam.lstrip('-')}] {line}")
                except subprocess.CalledProcessError:
                    log(f"  [{fam.lstrip('-')}] (none)")
        else:
            log("  (no `ip` on PATH — skipping route dump)")
    except Exception as e:  # noqa: BLE001
        log(f"  route dump failed: {e}")


# ---------------------------------------------------------------------------
# Per-address reachability probes
# ---------------------------------------------------------------------------

def _ping_one(addr: str, timeout: float) -> str:
    """Return a short status string for a single ICMP ping. We don't fail
    hard on non-zero — some controllers block ICMP but still speak QUIC."""
    ping = shutil.which("ping")
    if ping is None:
        return "no ping binary"
    ip_obj = None
    try:
        ip_obj = ip_address(addr.split("%", 1)[0])
    except ValueError:
        pass
    args: list[str]
    if isinstance(ip_obj, IPv6Address):
        args = [ping, "-6", "-c", "1", "-W", str(int(max(1, timeout))), addr]
    else:
        args = [ping, "-c", "1", "-W", str(int(max(1, timeout))), addr]
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout + 1.0
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"
    if r.returncode == 0:
        # Extract the round-trip time from the output if present.
        for line in r.stdout.splitlines():
            if "time=" in line:
                piece = line.split("time=", 1)[1].split(" ", 1)[0]
                return f"ok ({piece} ms)"
        return "ok"
    # Non-zero — surface stderr hint.
    err = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["fail"]
    return f"fail ({err[0][:60]})"


def _udp_probe(addr: str, port: int, timeout: float) -> str:
    """Try to open a UDP socket toward the address and send a single byte.
    We don't expect a reply (the controller only speaks QUIC), but this
    surfaces:
      - `Network is unreachable` — no route
      - `Permission denied` — blocked outbound
      - immediate errno — something local is broken
    """
    bare = addr.split("%", 1)[0]
    try:
        ip_obj = ip_address(bare)
    except ValueError:
        return f"bad addr: {addr}"
    fam = socket.AF_INET6 if isinstance(ip_obj, IPv6Address) else socket.AF_INET
    sock = socket.socket(fam, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sockaddr: tuple
        if fam == socket.AF_INET6:
            # Preserve scope id if caller passed "fe80::...%iface"
            if "%" in addr:
                sockaddr = (addr, port, 0, 0)
            else:
                sockaddr = (addr, port, 0, 0)
        else:
            sockaddr = (addr, port)
        try:
            sock.connect(sockaddr)
        except BlockingIOError:
            pass  # UDP connect shouldn't block, but tolerate
        except OSError as e:
            return f"connect fail: {e}"
        try:
            sock.send(b"\x00")
        except OSError as e:
            return f"send fail: {e}"
        local = sock.getsockname()
        peer = sock.getpeername()
        return f"ok (local={local[0]}:{local[1]} peer={peer[0]}:{peer[1]})"
    finally:
        sock.close()


async def _probe_addresses(c: Controller) -> None:
    log("")
    log(f"reachability probes for {c.serial}:{c.port}")
    loop = asyncio.get_running_loop()
    for a in c.addresses:
        addr_str = str(a)
        # Run blocking probes in a thread so we don't stall the loop.
        ping_res = await loop.run_in_executor(
            None, _ping_one, addr_str, PING_TIMEOUT
        )
        udp_res = await loop.run_in_executor(
            None, _udp_probe, addr_str, c.port, UDP_PROBE_TIMEOUT
        )
        log(f"  {addr_str:<42} ping: {ping_res}")
        log(f"  {'':<42} udp:  {udp_res}")


# ---------------------------------------------------------------------------
# Raw UDP transport tap — counts packets BELOW the QUIC layer
# ---------------------------------------------------------------------------

class _UdpTap:
    """Hooks the asyncio DatagramProtocol callbacks on the QUIC protocol and
    wraps transport.sendto so we see every UDP packet moving in either
    direction."""

    DUMP_FIRST_N = 5  # hex-dump the first N packets in each direction

    def __init__(self) -> None:
        self.rx_pkts = 0
        self.rx_bytes = 0
        self.tx_pkts = 0
        self.tx_bytes = 0
        self.first_rx: Optional[float] = None
        self.last_rx: Optional[float] = None
        self.first_tx: Optional[float] = None
        self.last_tx: Optional[float] = None
        self.rx_peers: Counter[str] = Counter()
        self.tx_peers: Counter[str] = Counter()
        self.err_received: int = 0
        self.rx_size_hist: Counter[int] = Counter()  # bucketed by 100 bytes
        self.rx_dumps: list[tuple[float, int, bytes]] = []
        self.tx_dumps: list[tuple[float, int, bytes]] = []
        self._t0 = time.monotonic()

    def attach(self, protocol) -> None:
        # Wrap datagram_received
        orig_recv = protocol.datagram_received

        def tapped_recv(data, addr):  # noqa: ANN001
            self.rx_pkts += 1
            self.rx_bytes += len(data)
            now = time.monotonic()
            if self.first_rx is None:
                self.first_rx = now - self._t0
            self.last_rx = now - self._t0
            self.rx_size_hist[(len(data) // 100) * 100] += 1
            if len(self.rx_dumps) < self.DUMP_FIRST_N:
                self.rx_dumps.append((now - self._t0, len(data), data[:32]))
            try:
                self.rx_peers[f"{addr[0]}:{addr[1]}"] += 1
            except Exception:  # noqa: BLE001
                pass
            return orig_recv(data, addr)

        protocol.datagram_received = tapped_recv  # type: ignore[assignment]

        # Wrap error_received (ICMP unreachable, etc.) — aioquic defines it
        # but may ignore most errors.
        orig_err = getattr(protocol, "error_received", None)

        def tapped_err(exc):  # noqa: ANN001
            self.err_received += 1
            log(f"  [udp-tap] error_received: {exc}")
            if orig_err is not None:
                return orig_err(exc)

        protocol.error_received = tapped_err  # type: ignore[assignment]

        # Wrap transport.sendto
        transport = getattr(protocol, "_transport", None)
        if transport is None:
            return
        orig_sendto = transport.sendto

        def tapped_sendto(data, addr=None):  # noqa: ANN001
            self.tx_pkts += 1
            self.tx_bytes += len(data)
            now = time.monotonic()
            if self.first_tx is None:
                self.first_tx = now - self._t0
            self.last_tx = now - self._t0
            if len(self.tx_dumps) < self.DUMP_FIRST_N:
                self.tx_dumps.append((now - self._t0, len(data), data[:32]))
            try:
                if addr is not None:
                    self.tx_peers[f"{addr[0]}:{addr[1]}"] += 1
            except Exception:  # noqa: BLE001
                pass
            if addr is None:
                return orig_sendto(data)
            return orig_sendto(data, addr)

        transport.sendto = tapped_sendto  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Sequential handshake fallback — if the parallel connect_controller fails,
# try each address on its own and surface the exact error.
# ---------------------------------------------------------------------------

async def _sequential_handshake(c: Controller, bundle: CertBundle) -> None:
    log("")
    log("sequential per-address handshake (each 5s):")
    for a in c.addresses:
        addr_str = str(a)
        config = QuicConfiguration(
            is_client=True,
            alpn_protocols=[EXPECTED_ALPN],
            server_name=c.serial.lower(),
            idle_timeout=5.0,
            max_datagram_frame_size=1200,
        )
        config.load_verify_locations(cafile=str(bundle.ca))
        config.load_cert_chain(
            certfile=str(bundle.client_cert), keyfile=str(bundle.client_key)
        )
        t0 = time.monotonic()
        try:
            async with aioquic_connect(
                addr_str,
                c.port,
                configuration=config,
                create_protocol=_RuvionProtocol,
                wait_connected=True,
            ) as proto:
                dt = (time.monotonic() - t0) * 1000
                log(f"  ✓ {addr_str}  handshake ok in {dt:.0f} ms")
                # Log local/remote socket addresses.
                try:
                    tp = getattr(proto, "_transport", None)
                    if tp:
                        local = tp.get_extra_info("sockname")
                        peer = tp.get_extra_info("peername")
                        log(f"      local={local} peer={peer}")
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.TimeoutError:
            dt = (time.monotonic() - t0) * 1000
            log(f"  ✗ {addr_str}  TIMEOUT after {dt:.0f} ms "
                f"(no handshake reply — UDP blocked or wrong route)")
        except Exception as e:  # noqa: BLE001
            dt = (time.monotonic() - t0) * 1000
            log(f"  ✗ {addr_str}  {type(e).__name__}: {e}  ({dt:.0f} ms)")


# ---------------------------------------------------------------------------
# QUIC / socket inspection helpers (kept from previous version)
# ---------------------------------------------------------------------------

def _get_socket_stats(sock: Optional[socket.socket]) -> dict:
    stats: dict = {}
    if sock is None:
        return stats
    for name in ("SO_RCVBUF", "SO_SNDBUF"):
        try:
            opt = getattr(socket, name)
            stats[name.lower()] = sock.getsockopt(socket.SOL_SOCKET, opt)
        except Exception:  # noqa: BLE001
            pass
    return stats


def _get_quic_info(protocol) -> dict:
    info: dict = {}
    try:
        quic = protocol._quic
        info["remote_max_datagram_frame_size"] = getattr(
            quic, "_remote_max_datagram_frame_size", None
        )
        cong = getattr(quic, "_congestion_control", None)
        if cong is not None:
            info["congestion_window"] = getattr(cong, "congestion_window", None)
    except Exception:  # noqa: BLE001
        pass
    return info


def _get_local_peer_from_transport(protocol) -> tuple[Optional[tuple], Optional[tuple], Optional[socket.socket]]:
    transport = getattr(protocol, "_transport", None)
    if transport is None:
        return None, None, None
    try:
        local = transport.get_extra_info("sockname")
    except Exception:  # noqa: BLE001
        local = None
    try:
        peer = transport.get_extra_info("peername")
    except Exception:  # noqa: BLE001
        peer = None
    try:
        sock = transport.get_extra_info("socket")
    except Exception:  # noqa: BLE001
        sock = None
    return local, peer, sock


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(name)s [%(levelname)s] %(message)s",
    )
    logging.getLogger("ruvion_client").setLevel(logging.DEBUG)
    logging.getLogger("quic").setLevel(logging.DEBUG)
    logging.getLogger("aioquic").setLevel(logging.DEBUG)

    _env_info()
    _interface_info()

    log("")
    log(f"discovering (<= {DISCOVER_TIMEOUT:.0f}s) ...")
    controllers = await discover_once(timeout=DISCOVER_TIMEOUT)
    if not controllers:
        sys.exit("no controller discovered — mDNS problem, not a telemetry one. "
                 "Check: firewall allows UDP/5353, same L2 segment, "
                 "zeroconf binary up-to-date.")
    c = controllers[0]
    log(f"  {c.serial}  model={c.model}  mode={c.mode}  alpn={c.alpn!r}")
    log(f"  hostname={c.hostname!r}  port={c.port}")
    log(f"  addresses:")
    for a in c.addresses:
        kind = (
            "IPv4" if isinstance(a, IPv4Address)
            else "IPv6 link-local" if (isinstance(a, IPv6Address) and a.is_link_local)
            else "IPv6 ULA" if (isinstance(a, IPv6Address) and a.is_private)
            else "IPv6 global" if isinstance(a, IPv6Address)
            else "?"
        )
        log(f"    - {a}  ({kind})")

    await _probe_addresses(c)

    # Cert bundle check before attempting handshake — fail loudly if missing.
    try:
        bundle = CertBundle.from_dir(CERT_DIR)
        log("")
        log(f"cert bundle: ca={bundle.ca.name} cert={bundle.client_cert.name} "
            f"key={bundle.client_key.name}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"cert bundle invalid in {CERT_DIR}: {e}")

    log("")
    log("attempting handshake (parallel across all addresses, 5s timeout) ...")
    t0 = time.monotonic()
    try:
        conn = await connect_controller(c, cert_dir=CERT_DIR)
    except Exception as e:  # noqa: BLE001
        log(f"✗ parallel handshake failed: {type(e).__name__}: {e}")
        log("")
        await _sequential_handshake(c, bundle)
        sys.exit(
            "\nhandshake failed on every path. Typical causes:\n"
            "  - UDP/4433 blocked by host or network firewall (check iptables/nftables/ufw)\n"
            "  - no route to controller (check `ip route get <addr>`)\n"
            "  - server cert SNI mismatch (we use serial lowercased — override if needed)\n"
            "  - mTLS: client cert not trusted by controller (check controller log)\n"
            "  - controller crashed / reboot required\n"
        )
    log(f"✓ parallel handshake ok in {(time.monotonic()-t0)*1000:.0f} ms "
        f"→ {conn.remote_address}:{c.port}")

    # Attach raw UDP tap FIRST so we see the effect of any further activity.
    udp_tap = _UdpTap()
    udp_tap.attach(conn.protocol)

    local, peer, sock = _get_local_peer_from_transport(conn.protocol)
    log(f"local socket: {local}    peer: {peer}")
    socket_stats = _get_socket_stats(sock)
    if socket_stats:
        log(f"socket buffers: {socket_stats}")

    qinfo = _get_quic_info(conn.protocol)
    log(f"QUIC: {qinfo}")
    remote_max_dg = qinfo.get("remote_max_datagram_frame_size")
    log(f"server advertises max_datagram_frame_size = {remote_max_dg}")

    # QUIC event tap
    counts: Counter[str] = Counter()
    event_times: dict[str, list[float]] = {}
    dg_size_hist: Counter[int] = Counter()
    dg_dumps: list[tuple[float, int, bytes]] = []
    stream_size_hist: Counter[int] = Counter()
    orig_handler = conn.protocol.quic_event_received
    tap_start = time.monotonic()

    # aioquic events we want to inspect by payload — imported locally so an
    # older aioquic doesn't break the script.
    try:
        from aioquic.quic.events import (
            DatagramFrameReceived as _DgEvt,
            StreamDataReceived as _SdEvt,
        )
    except Exception:  # noqa: BLE001
        _DgEvt = _SdEvt = None  # type: ignore[assignment]

    def quic_tap(event) -> None:
        counts[type(event).__name__] += 1
        evt_name = type(event).__name__
        event_times.setdefault(evt_name, []).append(
            time.monotonic() - tap_start
        )
        if _DgEvt is not None and isinstance(event, _DgEvt):
            n = len(event.data)
            dg_size_hist[(n // 100) * 100] += 1
            if len(dg_dumps) < 5:
                dg_dumps.append(
                    (time.monotonic() - tap_start, n, bytes(event.data[:32]))
                )
        elif _SdEvt is not None and isinstance(event, _SdEvt):
            stream_size_hist[(len(event.data) // 100) * 100] += 1
        return orig_handler(event)

    conn.protocol.quic_event_received = quic_tap  # type: ignore[assignment]

    telemetry_yields = 0
    event_yields = 0
    telemetry_parse_errors = 0
    event_parse_errors = 0
    first_telemetry_parse_error: Optional[str] = None
    first_event_parse_error: Optional[str] = None

    # Re-implement the drain loops so we can see parse failures that are
    # otherwise swallowed at DEBUG level inside Connection.telemetry()/events().
    async def drain_telemetry() -> None:
        nonlocal telemetry_yields, telemetry_parse_errors
        nonlocal first_telemetry_parse_error
        from ruvion_client import telemetry as _tel_mod
        q = conn.protocol.subscribe_datagrams()
        try:
            while True:
                data = await q.get()
                try:
                    _tel_mod.parse_telemetry(data)
                    telemetry_yields += 1
                except Exception as e:  # noqa: BLE001
                    telemetry_parse_errors += 1
                    if first_telemetry_parse_error is None:
                        first_telemetry_parse_error = (
                            f"{type(e).__name__}: {e} "
                            f"(payload len={len(data)}, head={data[:16].hex()})"
                        )
        finally:
            conn.protocol.unsubscribe_datagrams(q)

    async def drain_events() -> None:
        nonlocal event_yields, event_parse_errors, first_event_parse_error
        from ruvion_client import events as _evt_mod
        q = conn.protocol.subscribe_events()
        try:
            while True:
                data = await q.get()
                try:
                    _evt_mod.parse_event(data)
                    event_yields += 1
                except Exception as e:  # noqa: BLE001
                    event_parse_errors += 1
                    if first_event_parse_error is None:
                        first_event_parse_error = (
                            f"{type(e).__name__}: {e} "
                            f"(payload len={len(data)}, head={data[:16].hex()})"
                        )
        finally:
            conn.protocol.unsubscribe_events(q)

    log("")
    log(f"observing {OBSERVE_SECONDS:.0f}s (1 Hz heartbeat — proves Python is alive "
        f"even if everything stays 0):")
    log(f"  {'t':>5s}  {'udp_rx':>7s}  {'udp_tx':>7s}  "
        f"{'streams':>7s}  {'dgrams':>6s}  {'tel_y':>5s}  {'evt_y':>5s}")

    async with conn:
        tel_task = asyncio.create_task(drain_telemetry())
        evt_task = asyncio.create_task(drain_events())
        try:
            deadline = time.monotonic() + OBSERVE_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                log(
                    f"  {time.monotonic()-t0:5.1f}  "
                    f"{udp_tap.rx_pkts:>7d}  "
                    f"{udp_tap.tx_pkts:>7d}  "
                    f"{counts.get('StreamDataReceived', 0):>7d}  "
                    f"{counts.get('DatagramFrameReceived', 0):>6d}  "
                    f"{telemetry_yields:>5d}  "
                    f"{event_yields:>5d}"
                )
        finally:
            counts_observed = Counter(counts)
            for t in (tel_task, evt_task):
                t.cancel()
            for t in (tel_task, evt_task):
                try:
                    await t
                except BaseException:  # noqa: BLE001
                    pass

    log("")
    log("raw UDP (transport-layer tap):")
    log(f"  rx_packets={udp_tap.rx_pkts}  rx_bytes={udp_tap.rx_bytes}  "
        f"first@{(udp_tap.first_rx or 0):.2f}s  last@{(udp_tap.last_rx or 0):.2f}s")
    log(f"  tx_packets={udp_tap.tx_pkts}  tx_bytes={udp_tap.tx_bytes}  "
        f"first@{(udp_tap.first_tx or 0):.2f}s  last@{(udp_tap.last_tx or 0):.2f}s")
    if udp_tap.err_received:
        log(f"  error_received calls: {udp_tap.err_received} "
            f"(ICMP unreachable or similar — see log lines above)")
    if udp_tap.rx_peers:
        log(f"  rx peers: {dict(udp_tap.rx_peers)}")
    if udp_tap.tx_peers:
        log(f"  tx peers: {dict(udp_tap.tx_peers)}")
    if udp_tap.rx_size_hist:
        log(f"  rx size histogram (bucketed by 100B): "
            f"{dict(sorted(udp_tap.rx_size_hist.items()))}")
    if udp_tap.rx_dumps:
        log("  first rx packets (hex of first 32 bytes):")
        for t, n, head in udp_tap.rx_dumps:
            log(f"    t={t:6.2f}s  len={n:>5d}  {head.hex()}")
    if udp_tap.tx_dumps:
        log("  first tx packets (hex of first 32 bytes):")
        for t, n, head in udp_tap.tx_dumps:
            log(f"    t={t:6.2f}s  len={n:>5d}  {head.hex()}")

    log("")
    log("QUIC-layer payload stats:")
    if dg_size_hist:
        log(f"  DATAGRAM payload sizes (bucketed by 100B): "
            f"{dict(sorted(dg_size_hist.items()))}")
    else:
        log("  (no DATAGRAM frames observed)")
    if dg_dumps:
        log("  first DATAGRAM payloads (hex of first 32 bytes):")
        for t, n, head in dg_dumps:
            log(f"    t={t:6.2f}s  len={n:>5d}  {head.hex()}")
    if stream_size_hist:
        log(f"  StreamData sizes (bucketed by 100B): "
            f"{dict(sorted(stream_size_hist.items()))}")

    log("")
    log("raw QUIC events received:")
    if counts_observed:
        for name, n in sorted(counts_observed.items(), key=lambda kv: -kv[1]):
            times = event_times.get(name, [])
            if times:
                log(f"  {name:<28} {n:>4d}  "
                    f"(first at {times[0]:6.2f}s, last at {times[-1]:6.2f}s)")
            else:
                log(f"  {name:<28} {n:>4d}")
    else:
        log("  (none)")

    sd = counts_observed.get("StreamDataReceived", 0)
    dg = counts_observed.get("DatagramFrameReceived", 0)
    term = counts_observed.get("ConnectionTerminated", 0)

    log("")
    log("summary:")
    log(f"  udp_rx_packets:          {udp_tap.rx_pkts}")
    log(f"  udp_tx_packets:          {udp_tap.tx_pkts}")
    log(f"  StreamDataReceived:      {sd}")
    log(f"  DatagramFrameReceived:   {dg}")
    log(f"  telemetry_yields:        {telemetry_yields} "
        f"(expected ~{int(OBSERVE_SECONDS * 500)})")
    log(f"  telemetry_parse_errors:  {telemetry_parse_errors}")
    if first_telemetry_parse_error:
        log(f"      first: {first_telemetry_parse_error}")
    log(f"  event_yields:            {event_yields}")
    log(f"  event_parse_errors:      {event_parse_errors}")
    if first_event_parse_error:
        log(f"      first: {first_event_parse_error}")

    log("")
    log("diagnosis:")
    if term:
        log("  ⚠ connection was torn down during observation "
            "(ConnectionTerminated event). Check idle_timeout, server-side "
            "error logs, and DEBUG trace above for reason_phrase.")

    if udp_tap.tx_pkts == 0:
        log("  ✗ the client sent ZERO UDP packets after handshake — "
            "transport tap is either not wired (bug) or aioquic stopped "
            "transmitting. Highly unusual.")
    if udp_tap.rx_pkts == 0:
        log("  ✗ ZERO raw UDP packets arrived from the controller.")
        log("    The failure is BELOW QUIC — network layer drop on the reverse path:")
        log("      - host firewall (ufw/iptables/nftables/firewalld) blocking inbound UDP")
        log("      - rp_filter (strict reverse-path) on Linux — try:")
        log("          sudo sysctl -w net.ipv4.conf.all.rp_filter=2")
        log("      - NAT / router dropping unsolicited return traffic")
        log("      - wrong interface picked (VPN? default route via tun0?) — "
            "compare 'local socket' above against 'local network interfaces'")
        log("      - IPv6 scope-id missing on link-local (fe80::) address")
        log("    TEST: run tcpdump on this machine during the run:")
        log(f"          sudo tcpdump -ni any 'udp and port {c.port}'")
        return

    if not remote_max_dg:
        log("  ✗ server did NOT negotiate QUIC DATAGRAM — "
            "telemetry cannot arrive.  Check controller firmware / QUIC config.")
    elif dg == 0 and sd == 0 and udp_tap.rx_pkts > 0:
        log("  ⚠ UDP packets arrive (handshake-level ACKs / keepalives) but "
            "QUIC yields neither streams nor datagrams in the app layer.")
        log("    Hypotheses, most → least likely:")
        log("      1. Server waits for a client trigger before sending telemetry")
        log("         (unlikely for this protocol — monitor_simple.py just subscribes).")
        log("         Try briefly running the controller (press a key on its UI) "
            "or sending a harmless control request to see if data starts.")
        log("      2. Only handshake/ACK traffic survives the path; anything larger "
            "gets dropped (path MTU black hole).")
        log("         TEST: compare 'rx size histogram' above — if only <100B "
            "buckets appear, it's MTU-limited.")
        log("      3. Server got the handshake but its telemetry publisher is "
            "disabled / crashed. Check controller-side logs.")
        log("    Current server state can be probed by checking `conn.events()`; "
            "if events also stay at 0, the server really isn't pushing anything.")
    elif dg == 0 and sd > 0:
        log("  ✗ streams arrive but DATAGRAM frames are absent — "
            "drop is specific to QUIC DATAGRAM payloads:")
        log("      - path MTU < datagram size; server drops oversized DATAGRAMs silently")
        log("      - UDP GRO/GSO kernel quirk (coalesced frames mis-parsed)")
        log("      - server implementation bug")
        log("    TEST: try IPv4 or global IPv6 instead of link-local; "
            "check controller logs for MTU errors")
    elif dg > 0 and telemetry_yields == 0:
        log("  ⚠ datagrams arrive at QUIC layer but parser yields no Telemetry.")
        if telemetry_parse_errors:
            log(f"    {telemetry_parse_errors} parse errors captured — see summary above.")
            log("    Cause: flatbuffers schema mismatch between client and controller firmware.")
        else:
            log("    No parse errors either — datagrams arrive but something is "
                "swallowing them before the subscriber queue. Check for multiple "
                "subscribers or queue overflow warnings above.")
    else:
        rate = telemetry_yields / OBSERVE_SECONDS
        if rate < 10:
            log(f"  ⚠ telemetry flows but slowly: {rate:.1f} Hz (expected ~500 Hz)")
            log("    Hypothesis: server rate-limits or the path has heavy loss. "
                "Check aioquic DEBUG output for retransmits.")
        else:
            log(f"  ✓ telemetry flowing at {rate:.1f} Hz — transport is healthy.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
