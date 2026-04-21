"""Telemetry reachability probe.

Question: after the TLS handshake succeeds, do QUIC DATAGRAM frames
actually reach the client, or only reliable STREAM frames?

Method: tap the aioquic protocol to count raw events by type for a
fixed window, and run the normal telemetry()/events() generators in
parallel. That splits the transport into two independent channels
(streams = reliable, datagrams = telemetry) so the failing one is
unambiguous. No host-side guessing.

Run:
    python examples/diagnose.py

Read-only — no claim/motion.
"""
from __future__ import annotations

import asyncio
import platform
import sys
import time
from collections import Counter

from ruvion_client import connect_controller, discover_once

from config import CERT_DIR

OBSERVE_SECONDS = 10.0
DISCOVER_TIMEOUT = 5.0


def log(msg: str = "") -> None:
    print(msg, flush=True)


async def main() -> None:
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

    counts: Counter[str] = Counter()
    orig_handler = conn.protocol.quic_event_received

    def tap(event) -> None:
        counts[type(event).__name__] += 1
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
            log(f"  {name:<28} {n}")
    else:
        log("  (none)")

    sd = counts_observed.get("StreamDataReceived", 0)
    dg = counts_observed.get("DatagramFrameReceived", 0)
    term = counts_observed.get("ConnectionTerminated", 0)

    log("")
    log("conclusion:")
    if term:
        log("  connection was torn down during observation — check idle timeout")
        log("  and any ConnectionTerminated details in aioquic logs.")
    if not remote_max_dg:
        log("  server did NOT negotiate QUIC DATAGRAM — telemetry cannot arrive by")
        log("  protocol. Check controller firmware / QUIC config on that side.")
    elif dg == 0 and sd == 0:
        log("  after handshake, nothing returns. packets on the reverse path are")
        log("  being discarded before reaching the process (rp_filter with")
        log("  asymmetric routing, host packet filter, or NAT conntrack eviction).")
    elif dg == 0 and sd > 0:
        log("  reliable streams flow, but QUIC DATAGRAM frames are absent.")
        log("  this isolates the drop to the datagram path specifically:")
        log("    - a packet filter that allows streams but strips datagram frames")
        log("    - path MTU < datagram size with DF=1 (silent drop)")
        log("    - UDP GRO/GSO mismatch between kernel and aioquic (coalesced")
        log("      datagrams not demultiplexed; more common on newer Ubuntu)")
    elif dg > 0 and telemetry_yields == 0:
        log("  datagrams arrive but telemetry() yields nothing — parser error.")
        log("  re-run with logging.getLogger('ruvion_client').setLevel('DEBUG').")
    else:
        rate = telemetry_yields / OBSERVE_SECONDS
        log(f"  telemetry flowing at {rate:.1f} Hz — transport is healthy.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
