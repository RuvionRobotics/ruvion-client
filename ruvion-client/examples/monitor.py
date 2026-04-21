"""Live telemetry monitor — read-only, no claiming.

Setup
-----
1. Edit examples/config.py so CERT_DIR points at ca.crt, client.crt, client.key.
2. Run:  python examples/monitor.py
"""
import asyncio
import logging
import sys

import numpy as np

from ruvion_client import ConnectError, connect_controller, discover_once
from ruvion_client.telemetry import ArmTelemetry, DriveStatus, MotionStatus, Telemetry

from config import CERT_DIR

HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ENTER_ALT   = "\033[?1049h"
EXIT_ALT    = "\033[?1049l"
HOME        = "\033[H"
CLEAR_EOS   = "\033[J"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
CYAN = "\033[36m"
RESET = "\033[0m"

_DRIVE_STATUS_COLOR = {
    DriveStatus.Operational: GREEN,
    DriveStatus.QuickStop: YELLOW,
    DriveStatus.BusFault: RED,
    DriveStatus.DriveFault: RED,
    DriveStatus.SettingUp: CYAN,
    DriveStatus.Off: DIM,
}

_MOTION_COLOR = {
    MotionStatus.Moving: CYAN,
    MotionStatus.Settling: YELLOW,
    MotionStatus.Braking: YELLOW,
    MotionStatus.Idle: RESET,
}


def _colored(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def _fmt_vec(v: np.ndarray, fmt: str = "{:7.3f}") -> str:
    if v is None or len(v) == 0:
        return "—"
    return "  ".join(fmt.format(x) for x in v)


def _fmt_temp(v: np.ndarray) -> str:
    if v is None or len(v) == 0:
        return "—"
    parts = []
    for x in v:
        s = "  —  " if np.isnan(x) else f"{x:5.1f}"
        if not np.isnan(x) and x >= 70:
            s = _colored(s, RED)
        elif not np.isnan(x) and x >= 55:
            s = _colored(s, YELLOW)
        parts.append(s)
    return "  ".join(parts)


def _fmt_faults(faulted: np.ndarray, codes: np.ndarray) -> str:
    if faulted is None or not any(faulted):
        return _colored("none", GREEN)
    parts = []
    for i, (f, c) in enumerate(zip(faulted, codes)):
        if f:
            parts.append(_colored(f"J{i+1}=0x{c:04X}", RED))
    return "  ".join(parts)


def _arm_block(label: str, arm: ArmTelemetry) -> list[str]:
    lines = []
    lines.append(f"  {BOLD}{label}{RESET}")
    lines.append(f"    {'pos actual':14s}  {_fmt_vec(arm.actual_position)}")
    lines.append(f"    {'pos target':14s}  {_fmt_vec(arm.target_position)}")
    lines.append(f"    {'vel actual':14s}  {_fmt_vec(arm.actual_velocity)}")
    lines.append(f"    {'torque actual':14s}  {_fmt_vec(arm.actual_torque)}")
    lines.append(f"    {'torque target':14s}  {_fmt_vec(arm.target_torque)}")
    lines.append(f"    {'motor temp °C':14s}  {_fmt_temp(arm.motor_temperature)}")
    lines.append(f"    {'driver temp °C':14s}  {_fmt_temp(arm.driver_temperature)}")
    lines.append(f"    {'faults':14s}  {_fmt_faults(arm.faulted, arm.fault_codes)}")

    if arm.tcp_actual_position is not None:
        pos = arm.tcp_actual_position
        ori = arm.tcp_actual_orientation
        lv  = arm.tcp_actual_linear_vel
        av  = arm.tcp_actual_angular_vel
        lines.append(f"    {DIM}── TCP ──{RESET}")
        lines.append(f"    {'tcp pos xyz':14s}  {_fmt_vec(pos, '{:8.4f}')}")
        if ori is not None:
            lines.append(f"    {'tcp ori wxyz':14s}  {_fmt_vec(ori, '{:7.4f}')}")
        if lv is not None:
            lines.append(f"    {'tcp lin vel':14s}  {_fmt_vec(lv, '{:8.4f}')}")
        if av is not None:
            lines.append(f"    {'tcp ang vel':14s}  {_fmt_vec(av, '{:8.4f}')}")

    return lines


def _render(tel: Telemetry, serial: str, addr: str) -> str:
    lines = []
    lines.append(f"{BOLD}Ruvion Monitor{RESET}  {DIM}{serial}  {addr}  seq={tel.seq}{RESET}")
    lines.append("")

    # Safety
    s = tel.safety
    fault_str = _colored("FAULT", RED) if s.any_fault else _colored("ok", GREEN)
    qs_str    = _colored("ACTIVE", YELLOW) if s.quickstop_active else _colored("off", DIM)
    ctrl_str  = _colored("yes", GREEN) if s.has_controller else _colored("no", DIM)
    lines.append(
        f"  {BOLD}Safety{RESET}   fault={fault_str}  quickstop={qs_str}  "
        f"controller={ctrl_str}  op_enabled={_colored(str(s.all_op_enabled), GREEN if s.all_op_enabled else DIM)}"
    )

    # System
    if tel.system:
        sys_ = tel.system
        ds_color = _DRIVE_STATUS_COLOR.get(sys_.drive_status, RESET)
        mo_color = _MOTION_COLOR.get(sys_.motion, RESET)
        mode_str = _colored("impedance", CYAN) if sys_.is_impedance else _colored("csp", DIM)
        drive_str = _colored(sys_.drive_status.name, ds_color)
        motion_str = _colored(sys_.motion.name, mo_color)

        act_str = sys_.activity.name
        if sys_.activity_total > 0:
            act_str += f" {sys_.activity_step}/{sys_.activity_total}"

        lines.append(
            f"  {BOLD}System{RESET}   drive={drive_str}  motion={motion_str}  "
            f"mode={mode_str}  activity={act_str}"
        )
    lines.append("")

    # Arm data
    header = f"  {'':14s}  " + "  ".join(f"  J{i+1}   " for i in range(7))
    lines.append(f"{DIM}{header}{RESET}")

    if tel.arm:
        lines.extend(_arm_block("Arm", tel.arm))
    if tel.right_arm:
        lines.extend(_arm_block("Right arm", tel.right_arm))
    if tel.left_arm:
        lines.extend(_arm_block("Left arm", tel.left_arm))

    lines.append("")
    lines.append(f"  {DIM}Ctrl+C to quit{RESET}")
    return lines


def _draw(lines: list[str]) -> None:
    sys.stdout.write(HOME + "\n".join(lines) + CLEAR_EOS)
    sys.stdout.flush()


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
    print(f"Found {c.serial}  {c.model}/{c.mode}\n")

    try:
        conn = await connect_controller(c, cert_dir=CERT_DIR)
    except ConnectError as e:
        print(f"Connect failed: {e}")
        return

    addr = f"{conn.remote_address}:{conn.controller.port}"
    sys.stdout.write(ENTER_ALT + HIDE_CURSOR + HOME + CLEAR_EOS)
    sys.stdout.flush()
    try:
        async with conn:
            async for tel in conn.telemetry():
                _draw(_render(tel, c.serial, addr))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR + EXIT_ALT)
        sys.stdout.flush()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("quic").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
