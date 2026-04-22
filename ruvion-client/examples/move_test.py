"""Move each arm to a target pose, wait for idle, then return to 0.

Target per joint (degrees):
  J1: +30, J2: -30, J3..J7: +30

Setup
-----
1. Edit examples/config.py so CERT_DIR points at ca.crt, client.crt, client.key.
2. Run:  python examples/move_test.py
"""
import asyncio
import logging

from ruvion_client import (
    ConnectError,
    DriveMode,
    JointPose,
    QuickstopState,
    connect_controller,
    discover_once,
)
from ruvion_client.telemetry import DriveStatus, MotionStatus

from config import CERT_DIR

TARGET_DEG = [30.0, -30.0, 30.0, 30.0, 30.0, 30.0, 30.0]
TARGET_REV = [d / 360.0 for d in TARGET_DEG]
ZERO_REV   = [0.0] * 7


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
        deadline = asyncio.get_running_loop().time() + 15.0
        async for tel in conn.telemetry():
            if tel.system and tel.system.drive_status == DriveStatus.Operational:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError("drives did not reach Operational")

    async def wait_motion(status: MotionStatus, timeout: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        async for tel in conn.telemetry():
            if tel.system and tel.system.motion == status:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError(f"motion did not reach {status.name}")

    async def goto(label: str, position: list[float]) -> None:
        print(f"  → {label}  (deg: {[round(p*360, 1) for p in position]})")
        pose = JointPose(position=position)
        if is_humanoid:
            conn.send_humanoid(right=pose, left=pose)
        else:
            conn.send_single(pose)
        await wait_motion(MotionStatus.Moving)
        print("    ✓ Moving")
        await wait_motion(MotionStatus.Idle)
        print("    ✓ Idle")

    async with conn:
        print(f"  ✓ Connected via {conn.remote_address}:{conn.controller.port}")

        try:
            await conn.clear_fault()
            print("  ✓ clear_fault")
        except Exception as e:
            print(f"  ! clear_fault: {e} (continuing)")

        async with conn.claim():
            print("  ✓ claim_control")
            await conn.set_drive_mode(DriveMode.Csp)
            print("  ✓ set_drive_mode(Csp)")
            try:
                await conn.clear_fault()
            except Exception:
                pass
            await conn.quickstop(QuickstopState.Disengage)
            print("  ✓ quickstop(Disengage) — waiting for Operational...")
            await wait_operational()
            print("  ✓ drives Operational")

            await conn.set_drive_mode(DriveMode.Impedance)
            print("  ✓ set_drive_mode(Impedance)\n")

            await goto("target", TARGET_REV)
            await goto("zero", ZERO_REV)

            print("\nDone.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("quic").setLevel(logging.WARNING)
    asyncio.run(main())
