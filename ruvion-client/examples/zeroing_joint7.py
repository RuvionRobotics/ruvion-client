"""Zero joint 7.

Setup
-----
1. Edit examples/config.py so CERT_DIR points at ca.crt, client.crt, client.key.
2. Run:  python examples/zeroing_joint7.py
"""
import asyncio
import logging

from ruvion_client import (
    ConnectError,
    QuickstopState,
    connect_controller,
    discover_once,
)
from ruvion_client.telemetry import ActivityType, DriveStatus

from config import CERT_DIR

JOINT_INDEX = 6  # 0-based index for joint 7


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

        async with conn.claim():
            print("  ✓ claim_control")

            await conn.quickstop(QuickstopState.Engage)
            print("  ✓ quickstop(Engage)")

            try:
                await conn.clear_fault()
                print("  ✓ clear_fault")
            except Exception as e:
                print(f"  ! clear_fault: {e} (continuing)")

            print("  Starting zeroing for joint 7...")
            if is_humanoid:
                await conn.humanoid_zeroing(
                    right_joints=[JOINT_INDEX],
                    left_joints=[JOINT_INDEX],
                )
            else:
                await conn.single_zeroing([JOINT_INDEX])
            print("  ✓ Zeroing complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("quic").setLevel(logging.WARNING)
    asyncio.run(main())
