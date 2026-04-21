"""Friction compensation calibration.

Setup
-----
1. Edit examples/config.py so CERT_DIR points at ca.crt, client.crt, client.key.
2. Run:  python examples/friction_comp.py
"""
import asyncio
import logging

from ruvion_client import (
    CalibrationType,
    ConnectError,
    DriveMode,
    JointPose,
    QuickstopState,
    connect_controller,
    discover_once,
)
from ruvion_client.telemetry import ActivityType, DriveStatus

from config import CERT_DIR


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

    async def wait_idle() -> None:
        deadline = asyncio.get_running_loop().time() + 15.0
        async for tel in conn.telemetry():
            if tel.system and tel.system.activity == ActivityType.Idle:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError("drives did not reach Idle")

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

            print("  Sending all joints to position 0...")
            pose = JointPose(position=[0.0] * 7)
            if is_humanoid:
                conn.send_humanoid(right=pose, left=pose)
            else:
                conn.send_single(pose)

            await asyncio.sleep(0.5)
            print("  Waiting for Idle...")
            await wait_idle()
            print("  ✓ Idle")

            await asyncio.sleep(0.5)
            await conn.set_drive_mode(DriveMode.Csp)
            print("  ✓ set_drive_mode(Csp)")
            await asyncio.sleep(1.0)

            print("  Starting friction compensation calibration...")
            await conn.calibrate(CalibrationType.Friction)
            print("  ✓ Friction compensation calibration complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("quic").setLevel(logging.WARNING)
    asyncio.run(main())
