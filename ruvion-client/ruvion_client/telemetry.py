"""Parser + dataclasses for telemetry_datagram.fbs.

Datagrams are lossy by design ("latest wins"); use the async iterator
`Connection.telemetry()` to consume them.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

import numpy as np

from .proto.ruvion.motion import ArmTelemetry as _ArmTelemetry
from .proto.ruvion.motion import HumanoidTelemetry as _HumanoidTelemetry
from .proto.ruvion.motion import RobotTelemetryType as _RobotTelemetryType
from .proto.ruvion.motion import SafetyState as _SafetyState
from .proto.ruvion.motion import SingleTelemetry as _SingleTelemetry
from .proto.ruvion.motion import SystemState as _SystemState
from .proto.ruvion.motion import TelemetryData as _TelemetryData


class DriveStatus(IntEnum):
    Off = 0
    SettingUp = 1
    BusFault = 2
    DriveFault = 3
    QuickStop = 4
    Operational = 5


class ActivityType(IntEnum):
    Idle = 0
    GravityCalibration = 1
    FrictionCalibration = 2


class MotionStatus(IntEnum):
    Idle = 0
    Settling = 1
    Moving = 2
    Braking = 3


@dataclass
class SafetyState:
    any_fault: bool
    all_op_enabled: bool
    quickstop_requested: bool
    quickstop_active: bool
    last_received_seq: int
    has_controller: bool


@dataclass
class SystemState:
    drive_status: DriveStatus
    activity: ActivityType
    activity_step: int
    activity_total: int
    motion: MotionStatus
    is_impedance: bool


@dataclass
class ArmTelemetry:
    target_position: np.ndarray       # (7,)
    target_velocity: np.ndarray       # (7,)
    target_torque: np.ndarray         # (7,)
    actual_position: np.ndarray       # (7,)
    actual_velocity: np.ndarray       # (7,)
    actual_torque: np.ndarray         # (7,)
    motor_temperature: np.ndarray     # (7,) — NaN = unavailable
    driver_temperature: np.ndarray    # (7,)
    fault_codes: np.ndarray           # (7,) uint16 — 0 = none
    faulted: np.ndarray               # (7,) bool
    quick_stop_active: np.ndarray     # (7,) bool

    # TCP pose/velocity — None when kinematics unavailable
    tcp_actual_position: Optional[np.ndarray] = None       # (3,)
    tcp_actual_orientation: Optional[np.ndarray] = None    # (4,) w,x,y,z
    tcp_actual_linear_vel: Optional[np.ndarray] = None     # (3,)
    tcp_actual_angular_vel: Optional[np.ndarray] = None    # (3,)
    tcp_commanded_position: Optional[np.ndarray] = None
    tcp_commanded_orientation: Optional[np.ndarray] = None
    tcp_commanded_linear_vel: Optional[np.ndarray] = None
    tcp_commanded_angular_vel: Optional[np.ndarray] = None


@dataclass
class Telemetry:
    seq: int
    safety: SafetyState
    system: Optional[SystemState]
    # Exactly one of these is set depending on robot config
    arm: Optional[ArmTelemetry] = None         # single-arm
    right_arm: Optional[ArmTelemetry] = None   # humanoid
    left_arm: Optional[ArmTelemetry] = None    # humanoid


# ---------- Parser helpers ----------


def _vec(get_numpy_fn, is_none_fn, dtype: np.dtype) -> np.ndarray:
    """Required vector: empty array if absent, else a copy with the right dtype."""
    if is_none_fn():
        return np.zeros(0, dtype=dtype)
    return np.asarray(get_numpy_fn()).astype(dtype, copy=True)


def _vec_opt(get_numpy_fn, is_none_fn) -> Optional[np.ndarray]:
    """Optional vector: None if absent."""
    if is_none_fn():
        return None
    return np.asarray(get_numpy_fn()).copy()


def _arm_from_fb(t: _ArmTelemetry.ArmTelemetry) -> ArmTelemetry:
    f32 = np.float32
    return ArmTelemetry(
        target_position=_vec(t.TargetPositionAsNumpy, t.TargetPositionIsNone, f32),
        target_velocity=_vec(t.TargetVelocityAsNumpy, t.TargetVelocityIsNone, f32),
        target_torque=_vec(t.TargetTorqueAsNumpy, t.TargetTorqueIsNone, f32),
        actual_position=_vec(t.ActualPositionAsNumpy, t.ActualPositionIsNone, f32),
        actual_velocity=_vec(t.ActualVelocityAsNumpy, t.ActualVelocityIsNone, f32),
        actual_torque=_vec(t.ActualTorqueAsNumpy, t.ActualTorqueIsNone, f32),
        motor_temperature=_vec(
            t.MotorTemperatureAsNumpy, t.MotorTemperatureIsNone, f32
        ),
        driver_temperature=_vec(
            t.DriverTemperatureAsNumpy, t.DriverTemperatureIsNone, f32
        ),
        fault_codes=_vec(t.FaultCodesAsNumpy, t.FaultCodesIsNone, np.uint16),
        faulted=_vec(t.FaultedAsNumpy, t.FaultedIsNone, bool),
        quick_stop_active=_vec(
            t.QuickStopActiveAsNumpy, t.QuickStopActiveIsNone, bool
        ),
        tcp_actual_position=_vec_opt(
            t.TcpActualPositionAsNumpy, t.TcpActualPositionIsNone
        ),
        tcp_actual_orientation=_vec_opt(
            t.TcpActualOrientationAsNumpy, t.TcpActualOrientationIsNone
        ),
        tcp_actual_linear_vel=_vec_opt(
            t.TcpActualLinearVelAsNumpy, t.TcpActualLinearVelIsNone
        ),
        tcp_actual_angular_vel=_vec_opt(
            t.TcpActualAngularVelAsNumpy, t.TcpActualAngularVelIsNone
        ),
        tcp_commanded_position=_vec_opt(
            t.TcpCommandedPositionAsNumpy, t.TcpCommandedPositionIsNone
        ),
        tcp_commanded_orientation=_vec_opt(
            t.TcpCommandedOrientationAsNumpy, t.TcpCommandedOrientationIsNone
        ),
        tcp_commanded_linear_vel=_vec_opt(
            t.TcpCommandedLinearVelAsNumpy, t.TcpCommandedLinearVelIsNone
        ),
        tcp_commanded_angular_vel=_vec_opt(
            t.TcpCommandedAngularVelAsNumpy, t.TcpCommandedAngularVelIsNone
        ),
    )


def _safety_from_fb(s: _SafetyState.SafetyState) -> SafetyState:
    return SafetyState(
        any_fault=bool(s.AnyFault()),
        all_op_enabled=bool(s.AllOpEnabled()),
        quickstop_requested=bool(s.QuickstopRequested()),
        quickstop_active=bool(s.QuickstopActive()),
        last_received_seq=int(s.LastReceivedSeq()),
        has_controller=bool(s.HasController()),
    )


def _system_from_fb(s: _SystemState.SystemState) -> SystemState:
    return SystemState(
        drive_status=DriveStatus(s.DriveStatus()),
        activity=ActivityType(s.Activity()),
        activity_step=int(s.ActivityStep()),
        activity_total=int(s.ActivityTotal()),
        motion=MotionStatus(s.Motion()),
        is_impedance=bool(s.IsImpedance()),
    )


def parse_telemetry(data: bytes) -> Telemetry:
    """Parse a telemetry datagram into a Telemetry dataclass."""
    td = _TelemetryData.TelemetryData.GetRootAs(data, 0)

    safety_fb = td.Safety()
    safety = _safety_from_fb(safety_fb) if safety_fb else SafetyState(
        False, False, False, False, 0, False
    )

    system_fb = td.System()
    system = _system_from_fb(system_fb) if system_fb else None

    robot_type = td.RobotType()
    arm = right_arm = left_arm = None

    if robot_type == _RobotTelemetryType.RobotTelemetryType.SingleTelemetry:
        single = _SingleTelemetry.SingleTelemetry()
        robot_tab = td.Robot()
        if robot_tab is not None:
            single.Init(robot_tab.Bytes, robot_tab.Pos)
            arm_fb = single.Arm()
            if arm_fb is not None:
                arm = _arm_from_fb(arm_fb)
    elif robot_type == _RobotTelemetryType.RobotTelemetryType.HumanoidTelemetry:
        hum = _HumanoidTelemetry.HumanoidTelemetry()
        robot_tab = td.Robot()
        if robot_tab is not None:
            hum.Init(robot_tab.Bytes, robot_tab.Pos)
            right_fb = hum.Right()
            left_fb = hum.Left()
            if right_fb is not None:
                right_arm = _arm_from_fb(right_fb)
            if left_fb is not None:
                left_arm = _arm_from_fb(left_fb)

    return Telemetry(
        seq=int(td.Seq()),
        safety=safety,
        system=system,
        arm=arm,
        right_arm=right_arm,
        left_arm=left_arm,
    )
