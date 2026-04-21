"""Streaming example: send a 0.2 Hz sine wave on joint 0 at 100 Hz.

Only joint 0 moves; all other joints stay at 0.
Uses JointPose with ttl_ms=20 so the server brakes if updates stop.
After DURATION_S the loop finishes at the next zero crossing and sends
a single-shot command to hold position 0.

Setup
-----
1. Edit examples/config.py so CERT_DIR points at ca.crt, client.crt, client.key.
2. Run:  python examples/streaming_test.py
"""
import asyncio
import logging
import math

from ruvion_client import (
    ConnectError,
    DriveMode,
    JointPose,
    QuickstopState,
    connect_controller,
    discover_once,
)
from ruvion_client.telemetry import DriveStatus

from config import CERT_DIR

# ── Configuration ────────────────────────────────────────────────────────────
RATE_HZ    = 100
DURATION_S = 20.0
AMPLITUDE  = 0.1    # rev — peak displacement on joint 0
FREQ_HZ    = 0.2    # sine oscillation frequency
TTL_MS     = 20     # server brakes if no update arrives within 20 ms
# ─────────────────────────────────────────────────────────────────────────────


def _suppress_stale_quic_warnings(loop, context):
    exc = context.get("exception")
    msg = context.get("message", "")
    if isinstance(exc, ConnectionError) and "Future exception" in msg:
        return
    loop.default_exception_handler(context)


async def main() -> None:
    asyncio.get_running_loop().set_exception_handler(_suppress_stale_quic_warnings)

    print("Scanning for controllers (5s)...")
    controllers = await discover_once(timeout=5.0)
    if not controllers:
        print("No controllers found.")
        return

    c = controllers[0]
    is_humanoid = (c.mode or "").lower() == "humanoid"
    print(f"Found {c.serial}  {c.model}/{c.mode}\n")

    try:
        conn = await connect_controller(c, cert_dir=CERT_DIR)
    except ConnectError as e:
        print(f"Connect failed: {e}")
        return

    async def wait_operational() -> None:
        last = [None]
        deadline = asyncio.get_running_loop().time() + 15.0
        async for tel in conn.telemetry():
            if tel.system and tel.system.drive_status != last[0]:
                last[0] = tel.system.drive_status
                print(f"    DriveStatus → {tel.system.drive_status.name}")
            if tel.system and tel.system.drive_status == DriveStatus.Operational:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError("drives did not reach Operational")

    async with conn:
        print(f"  ✓ Connected via {conn.remote_address}:{conn.controller.port}")

        try:
            await conn.clear_fault()
            print("  ✓ clear_fault")
        except Exception as e:  # noqa: BLE001
            print(f"  ! clear_fault: {e} (continuing)")

        async with conn.claim():
            print("  ✓ claim_control")
            await conn.set_drive_mode(DriveMode.Csp)
            print("  ✓ set_drive_mode(Csp)")
            try:
                await conn.clear_fault()
            except Exception:  # noqa: BLE001
                pass
            await conn.quickstop(QuickstopState.Disengage)
            print("  ✓ quickstop(Disengage) — waiting for Operational...")
            await wait_operational()
            print("  ✓ drives Operational")
            await conn.set_drive_mode(DriveMode.Impedance)
            print(f"  ✓ set_drive_mode(Impedance)\n")

            print(f"Streaming sine on joint 0 — amp={AMPLITUDE} rev, "
                  f"f={FREQ_HZ} Hz, {RATE_HZ} Hz rate, {DURATION_S}s\n")

            dt = 1.0 / RATE_HZ
            t_start = asyncio.get_running_loop().time()
            count = 0
            past_duration = False

            while True:
                now = asyncio.get_running_loop().time()
                t = now - t_start
                if t >= DURATION_S:
                    past_duration = True

                phase = 2 * math.pi * FREQ_HZ * t
                pos = [0.0] * 7
                vel = [0.0] * 7
                pos[0] = AMPLITUDE * math.sin(phase)
                vel[0] = AMPLITUDE * 2 * math.pi * FREQ_HZ * math.cos(phase)

                # At the next zero crossing after DURATION_S: send single-shot
                # pose to 0 with no velocity (server plans the return trajectory).
                if past_duration and abs(pos[0]) < AMPLITUDE * 0.01 and vel[0] <= 0.0:
                    final = JointPose(position=[0.0] * 7)
                    if is_humanoid:
                        conn.send_humanoid(right=final, left=final)
                    else:
                        conn.send_single(final)
                    break

                pose = JointPose(position=pos, velocity=vel)
                if is_humanoid:
                    conn.send_humanoid(right=pose, left=pose, ttl_ms=TTL_MS)
                else:
                    conn.send_single(pose, ttl_ms=TTL_MS)

                count += 1
                await asyncio.sleep(dt)

            print(f"Done — sent {count} datagrams, stopped at zero crossing")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("quic").setLevel(logging.WARNING)
    asyncio.run(main())
