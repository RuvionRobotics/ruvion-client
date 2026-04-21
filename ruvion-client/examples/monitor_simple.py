"""Minimal telemetry monitor — plain prints, 1 Hz, no alt-screen / ANSI.

Useful when examples/monitor.py shows only a blank screen (e.g. terminal
doesn't handle the alt buffer, or you want to see raw output / tracebacks).
"""
import asyncio
import time

from ruvion_client import connect_controller, discover_once

from config import CERT_DIR


async def main() -> None:
    print("Scanning for controllers (5s)...")
    controllers = await discover_once(timeout=5.0)
    if not controllers:
        print("No controllers found.")
        return

    c = controllers[0]
    print(f"Found {c.serial}  {c.model}/{c.mode}")

    conn = await connect_controller(c, cert_dir=CERT_DIR)
    print(f"Connected to {conn.remote_address}:{conn.controller.port}\n")

    last = 0.0
    async with conn:
        async for tel in conn.telemetry():
            now = time.monotonic()
            if now - last < 1.0:
                continue
            last = now

            s = tel.safety
            print(
                f"seq={tel.seq:>6}  "
                f"fault={s.any_fault}  quickstop={s.quickstop_active}  "
                f"controller={s.has_controller}  op_enabled={s.all_op_enabled}"
            )
            for label, arm in (
                ("arm", tel.arm),
                ("right", tel.right_arm),
                ("left", tel.left_arm),
            ):
                if arm is None:
                    continue
                print(f"  [{label}] pos    {arm.actual_position}")
                print(f"  [{label}] vel    {arm.actual_velocity}")
                print(f"  [{label}] torque {arm.actual_torque}")
            print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
