"""Telemetry reachability probe with full diagnostics.

Question: after the TLS handshake succeeds, do QUIC DATAGRAM frames
actually reach the client, or only reliable STREAM frames?

Method: tap the aioquic protocol to count raw events by type for a
fixed window, and run the normal telemetry()/events() generators in
parallel. Instrument with aioquic debug logging, socket stats, and
timing data to isolate the failure point.

Run:
    python examples/diagnose.py

Read-only — no claim/motion.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import socket
import sys
import time
from collections import Counter
from typing import Optional

from ruvion_client import connect_controller, discover_once

from config import CERT_DIR

OBSERVE_SECONDS = 10.0
DISCOVER_TIMEOUT = 5.0


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _get_socket_stats(sock: Optional[socket.socket]) -> dict:
    """Attempt to read UDP socket buffer stats. Platform-dependent."""
    stats = {}
    if sock is None:
        return stats
    try:
        if hasattr(socket, "SO_RCVBUF"):
            stats["rcv_buf"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    except Exception:
        pass
    try:
        if hasattr(socket, "SO_SNDBUF"):
            stats["snd_buf"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
    except Exception:
        pass
    return stats


def _get_quic_mtu_info(protocol) -> dict:
    """Extract QUIC's path MTU and other connection info."""
    info = {}
    try:
        quic = protocol._quic
        info["local_address"] = getattr(quic, "_local_address", None)
        info["remote_address"] = getattr(quic, "_remote_address", None)
        info["path_mtu"] = getattr(quic, "_path", None)
        # Try to get congestion window and loss state
        cong = getattr(quic, "_congestion_control", None)
        if cong:
            info["congestion_window"] = getattr(cong, "congestion_window", None)
    except Exception:
        pass
    return info


async def main() -> None:
    # Configure detailed logging
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(name)s [%(levelname)s] %(message)s",
    )
    logging.getLogger("ruvion_client").setLevel(logging.DEBUG)
    logging.getLogger("quic").setLevel(logging.DEBUG)

    try:
        import aioquic  # type: ignore
        aioquic_ver = getattr(aioquic, "__version__", "?")
    except Exception:  # noqa: BLE001
        aioquic_ver = "?"
    log(f"python  {sys.version.split()[0]}   aioquic {aioquic_ver}   {platform.platform()}")

    log(f"discovering (<= {DISCOVER_TIMEOUT:.0f}s) ...")
    controllers = await discover_once(timeout=DISCOVER_TIMEOUT)
    if not controllers:
        sys.exit("no controller discovered — mDNS problem, not a telemetry one")
    c = controllers[0]
    log(f"  {c.serial}  model={c.model}  mode={c.mode}  alpn={c.alpn!r}")
    log(f"  addrs={[str(a) for a in c.addresses]}  port={c.port}")

    t0 = time.monotonic()
    try:
        conn = await connect_controller(c, cert_dir=CERT_DIR)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"handshake failed: {type(e).__name__}: {e}")
    log(f"handshake ok in {(time.monotonic()-t0)*1000:.0f} ms → {conn.remote_address}:{c.port}")

    remote_max_dg = getattr(conn.protocol._quic, "_remote_max_datagram_frame_size", None)
    log(f"server advertises max_datagram_frame_size = {remote_max_dg}")

    # Try to get initial socket/QUIC state
    quic_info = _get_quic_mtu_info(conn.protocol)
    log(f"QUIC connection state: {quic_info}")

    # Try to access the underlying UDP socket
    try:
        sock = conn.protocol._quic._transport[1] if hasattr(conn.protocol._quic, "_transport") else None
        socket_stats = _get_socket_stats(sock)
        if socket_stats:
            log(f"socket buffers: {socket_stats}")
    except Exception as e:  # noqa: BLE001
        log(f"could not read socket stats: {e}")

    counts: Counter[str] = Counter()
    event_times: dict[str, list[float]] = {}
    orig_handler = conn.protocol.quic_event_received
    tap_start = time.monotonic()

    def tap(event) -> None:
        counts[type(event).__name__] += 1
        evt_name = type(event).__name__
        if evt_name not in event_times:
            event_times[evt_name] = []
        event_times[evt_name].append(time.monotonic() - tap_start)
        return orig_handler(event)

    conn.protocol.quic_event_received = tap  # type: ignore[assignment]

    telemetry_yields = 0
    event_yields = 0

    async def drain_telemetry() -> None:
        nonlocal telemetry_yields
        async for _ in conn.telemetry():
            telemetry_yields += 1

    async def drain_events() -> None:
        nonlocal event_yields
        async for _ in conn.events():
            event_yields += 1

    log("")
    log("observing (1 Hz heartbeat — means Python is alive even if counts stay 0):")
    log(f"  {'t':>5s}  {'streams':>7s}  {'datagrams':>9s}  {'tel_yield':>9s}  {'evt_yield':>9s}")

    async with conn:
        tel_task = asyncio.create_task(drain_telemetry())
        evt_task = asyncio.create_task(drain_events())
        try:
            deadline = time.monotonic() + OBSERVE_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                log(
                    f"  {time.monotonic()-t0:5.1f}  "
                    f"{counts.get('StreamDataReceived', 0):>7d}  "
                    f"{counts.get('DatagramFrameReceived', 0):>9d}  "
                    f"{telemetry_yields:>9d}  "
                    f"{event_yields:>9d}"
                )
        finally:
            # Snapshot counts before teardown, otherwise the graceful close
            # shows up as a spurious ConnectionTerminated.
            counts_observed = Counter(counts)
            for t in (tel_task, evt_task):
                t.cancel()
            for t in (tel_task, evt_task):
                try:
                    await t
                except BaseException:  # noqa: BLE001
                    pass

    log("")
    log("raw QUIC events received:")
    if counts_observed:
        for name, n in sorted(counts_observed.items(), key=lambda kv: -kv[1]):
            times = event_times.get(name, [])
            if times:
                first_t = times[0]
                last_t = times[-1]
                log(f"  {name:<28} {n:>3d}  (first at {first_t:6.2f}s, last at {last_t:6.2f}s)")
            else:
                log(f"  {name:<28} {n:>3d}")
    else:
        log("  (none)")

    sd = counts_observed.get("StreamDataReceived", 0)
    dg = counts_observed.get("DatagramFrameReceived", 0)
    term = counts_observed.get("ConnectionTerminated", 0)

    log("")
    log("summary:")
    log(f"  telemetry_yields: {telemetry_yields} (expected ~{int(OBSERVE_SECONDS * 500)})")
    log(f"  event_yields:     {event_yields}")
    log(f"  StreamDataReceived:      {sd}")
    log(f"  DatagramFrameReceived:   {dg}")

    log("")
    log("diagnosis:")
    if term:
        log("  ⚠ connection was torn down during observation — check idle timeout")
        log("    and any ConnectionTerminated details in aioquic logs above.")
    if not remote_max_dg:
        log("  ✗ server did NOT negotiate QUIC DATAGRAM — telemetry cannot arrive.")
        log("    Check controller firmware / QUIC config on server.")
    elif dg == 0 and sd == 0:
        log("  ✗ nothing arrives. packets on reverse path discarded at network layer:")
        log("      - rp_filter with asymmetric routing on server")
        log("      - host firewall / packet filter blocking replies")
        log("      - NAT with no reverse route")
        log("    TEST: run tcpdump on this machine during connection")
    elif dg == 0 and sd > 0:
        log("  ✗ streams arrive but DATAGRAM frames are absent — drop is specific to UDP datagrams:")
        log("      - packet filter allowing STREAM (TCP-like) but dropping DATAGRAM (UDP) frames")
        log("      - path MTU < datagram size; server drops oversized DATAGRAM frames silently")
        log("      - UDP GRO/GSO kernel mismatch (coalesced packets not split; Linux issue)")
        log("    TEST: check server logs for 'datagram too large' / MTU errors")
        log("    TEST: try ipv4 or global ipv6 instead of link-local")
    elif dg > 0 and telemetry_yields == 0:
        log("  ⚠ datagrams arrive at QUIC layer but parse as telemetry fails")
        log("    Likely: corrupt datagram payload, parser bug, or flatbuffers mismatch")
        log("    DEBUG output above should show parse errors from ruvion_client")
    else:
        rate = telemetry_yields / OBSERVE_SECONDS
        if rate < 10:
            log(f"  ⚠ telemetry flows but very slowly: {rate:.1f} Hz (expected ~500 Hz)")
            log("    Hypothesis: server rate-limits (waiting for ACKs that don't arrive,")
            log("    or misconfigurations). Enable aioquic DEBUG output (already enabled).")
        else:
            log(f"  ✓ telemetry flowing at {rate:.1f} Hz — transport is healthy.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
