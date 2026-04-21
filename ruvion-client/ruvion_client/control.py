"""FlatBuffers serialization + typed errors for the control stream.

Per control_stream.fbs: each StreamCommand is sent on its own QUIC stream,
server replies with one StreamCommandResponse. Framing is "end of stream =
end of message", no length prefix.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Optional, Sequence

import flatbuffers

from .proto.ruvion.motion import ApplyUpdate as _ApplyUpdate
from .proto.ruvion.motion import Calibration as _Calibration
from .proto.ruvion.motion import CheckUpdate as _CheckUpdate
from .proto.ruvion.motion import ClaimControl as _ClaimControl
from .proto.ruvion.motion import ClearFault as _ClearFault
from .proto.ruvion.motion import HumanoidSetSpeedOverride as _HumanoidSSO
from .proto.ruvion.motion import HumanoidZeroing as _HumanoidZero
from .proto.ruvion.motion import Quickstop as _Quickstop
from .proto.ruvion.motion import Reboot as _Reboot
from .proto.ruvion.motion import ReleaseControl as _ReleaseControl
from .proto.ruvion.motion import SetDriveMode as _SetDriveMode
from .proto.ruvion.motion import SetStreamingParams as _SetStreamingParams
from .proto.ruvion.motion import SingleSetSpeedOverride as _SingleSSO
from .proto.ruvion.motion import SingleZeroing as _SingleZero
from .proto.ruvion.motion import Stop as _Stop
from .proto.ruvion.motion import StreamCommand as _StreamCommand
from .proto.ruvion.motion import StreamCommandResponse as _StreamCommandResponse

PROTOCOL_VERSION = 1


# ---------- User-facing enums ----------


class DriveMode(IntEnum):
    Csp = 0
    Impedance = 1


class QuickstopState(IntEnum):
    Engage = 0
    Disengage = 1


class CalibrationType(IntEnum):
    Gravity = 0
    Friction = 1


class _CommandTag(IntEnum):
    NONE = 0
    Quickstop = 1
    Calibration = 2
    SingleZeroing = 3
    HumanoidZeroing = 4
    ClearFault = 5
    ClaimControl = 6
    ReleaseControl = 7
    Stop = 8
    SingleSetSpeedOverride = 9
    HumanoidSetSpeedOverride = 10
    SetDriveMode = 11
    CheckUpdate = 12
    ApplyUpdate = 13
    Reboot = 14
    SetStreamingParams = 15


# ---------- Exceptions ----------


class CommandError(RuntimeError):
    """Base class for command failures. Maps to CommandResult >= 10."""

    result_code: int = -1

    def __init__(self, message: str = "") -> None:
        self.message = message
        detail = f"{type(self).__name__}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class InternalError(CommandError):
    result_code = 10


class NotImplementedCommand(CommandError):
    result_code = 11


class DecodeFailed(CommandError):
    result_code = 12


class AlreadyControlled(CommandError):
    result_code = 13


class NotController(CommandError):
    result_code = 14


class FaultActive(CommandError):
    result_code = 15


class QuickstopNotActive(CommandError):
    result_code = 16


class ModeMismatch(CommandError):
    result_code = 17


class ZeroingFailed(CommandError):
    result_code = 18


class ActivityNotIdle(CommandError):
    result_code = 19


class NotInCspMode(CommandError):
    result_code = 20


class NotAtZero(CommandError):
    result_code = 21


class GravityCalibrationRequired(CommandError):
    result_code = 22


class NotIdleOrAtRest(CommandError):
    result_code = 23


_ERROR_BY_CODE: dict[int, type[CommandError]] = {
    cls.result_code: cls
    for cls in (
        InternalError,
        NotImplementedCommand,
        DecodeFailed,
        AlreadyControlled,
        NotController,
        FaultActive,
        QuickstopNotActive,
        ModeMismatch,
        ZeroingFailed,
        ActivityNotIdle,
        NotInCspMode,
        NotAtZero,
        GravityCalibrationRequired,
        NotIdleOrAtRest,
    )
}


# ---------- StreamCommand envelope ----------


def _wrap_command(
    builder: flatbuffers.Builder, inner_offset: int, tag: _CommandTag
) -> bytes:
    _StreamCommand.Start(builder)
    _StreamCommand.AddProtocolVersion(builder, PROTOCOL_VERSION)
    _StreamCommand.AddCommandType(builder, int(tag))
    _StreamCommand.AddCommand(builder, inner_offset)
    root = _StreamCommand.End(builder)
    builder.Finish(root)
    return bytes(builder.Output())


def _empty_table_offset(
    builder: flatbuffers.Builder, start_fn, end_fn
) -> int:
    start_fn(builder)
    return end_fn(builder)


def _uint8_vector(builder: flatbuffers.Builder, values: Sequence[int]) -> int:
    values = list(values)
    builder.StartVector(1, len(values), 1)
    for v in reversed(values):
        builder.PrependUint8(int(v))
    return builder.EndVector()


def _float32_vector(builder: flatbuffers.Builder, values: Sequence[float]) -> int:
    values = list(values)
    builder.StartVector(4, len(values), 4)
    for v in reversed(values):
        builder.PrependFloat32(float(v))
    return builder.EndVector()


# ---------- Builders (one per CommandType) ----------


def build_claim_control(force: bool = False) -> bytes:
    b = flatbuffers.Builder(64)
    _ClaimControl.Start(b)
    _ClaimControl.AddForce(b, force)
    inner = _ClaimControl.End(b)
    return _wrap_command(b, inner, _CommandTag.ClaimControl)


def build_release_control() -> bytes:
    b = flatbuffers.Builder(32)
    inner = _empty_table_offset(b, _ReleaseControl.Start, _ReleaseControl.End)
    return _wrap_command(b, inner, _CommandTag.ReleaseControl)


def build_clear_fault() -> bytes:
    b = flatbuffers.Builder(32)
    inner = _empty_table_offset(b, _ClearFault.Start, _ClearFault.End)
    return _wrap_command(b, inner, _CommandTag.ClearFault)


def build_stop() -> bytes:
    b = flatbuffers.Builder(32)
    inner = _empty_table_offset(b, _Stop.Start, _Stop.End)
    return _wrap_command(b, inner, _CommandTag.Stop)


def build_quickstop(state: QuickstopState) -> bytes:
    b = flatbuffers.Builder(32)
    _Quickstop.Start(b)
    _Quickstop.AddState(b, int(state))
    inner = _Quickstop.End(b)
    return _wrap_command(b, inner, _CommandTag.Quickstop)


def build_calibration(calibration_type: CalibrationType) -> bytes:
    b = flatbuffers.Builder(32)
    _Calibration.Start(b)
    _Calibration.AddCalibrationType(b, int(calibration_type))
    inner = _Calibration.End(b)
    return _wrap_command(b, inner, _CommandTag.Calibration)


def build_set_drive_mode(mode: DriveMode) -> bytes:
    b = flatbuffers.Builder(32)
    _SetDriveMode.Start(b)
    _SetDriveMode.AddMode(b, int(mode))
    inner = _SetDriveMode.End(b)
    return _wrap_command(b, inner, _CommandTag.SetDriveMode)


def build_single_zeroing(joints: Sequence[int]) -> bytes:
    b = flatbuffers.Builder(64)
    vec = _uint8_vector(b, joints)
    _SingleZero.Start(b)
    _SingleZero.AddJoints(b, vec)
    inner = _SingleZero.End(b)
    return _wrap_command(b, inner, _CommandTag.SingleZeroing)


def build_humanoid_zeroing(
    right_joints: Optional[Sequence[int]] = None,
    left_joints: Optional[Sequence[int]] = None,
) -> bytes:
    b = flatbuffers.Builder(64)
    right_vec = _uint8_vector(b, right_joints) if right_joints is not None else None
    left_vec = _uint8_vector(b, left_joints) if left_joints is not None else None
    _HumanoidZero.Start(b)
    if right_vec is not None:
        _HumanoidZero.AddRightJoints(b, right_vec)
    if left_vec is not None:
        _HumanoidZero.AddLeftJoints(b, left_vec)
    inner = _HumanoidZero.End(b)
    return _wrap_command(b, inner, _CommandTag.HumanoidZeroing)


def build_single_set_speed_override(
    velocity_factor: float = 1.0, acceleration_factor: float = 1.0
) -> bytes:
    b = flatbuffers.Builder(32)
    _SingleSSO.Start(b)
    _SingleSSO.AddVelocityFactor(b, velocity_factor)
    _SingleSSO.AddAccelerationFactor(b, acceleration_factor)
    inner = _SingleSSO.End(b)
    return _wrap_command(b, inner, _CommandTag.SingleSetSpeedOverride)


def build_humanoid_set_speed_override(
    right_velocity_factor: float = 1.0,
    right_acceleration_factor: float = 1.0,
    left_velocity_factor: float = 1.0,
    left_acceleration_factor: float = 1.0,
) -> bytes:
    b = flatbuffers.Builder(32)
    _HumanoidSSO.Start(b)
    _HumanoidSSO.AddRightVelocityFactor(b, right_velocity_factor)
    _HumanoidSSO.AddRightAccelerationFactor(b, right_acceleration_factor)
    _HumanoidSSO.AddLeftVelocityFactor(b, left_velocity_factor)
    _HumanoidSSO.AddLeftAccelerationFactor(b, left_acceleration_factor)
    inner = _HumanoidSSO.End(b)
    return _wrap_command(b, inner, _CommandTag.HumanoidSetSpeedOverride)


def build_check_update() -> bytes:
    b = flatbuffers.Builder(32)
    inner = _empty_table_offset(b, _CheckUpdate.Start, _CheckUpdate.End)
    return _wrap_command(b, inner, _CommandTag.CheckUpdate)


def build_apply_update() -> bytes:
    b = flatbuffers.Builder(32)
    inner = _empty_table_offset(b, _ApplyUpdate.Start, _ApplyUpdate.End)
    return _wrap_command(b, inner, _CommandTag.ApplyUpdate)


def build_reboot() -> bytes:
    b = flatbuffers.Builder(32)
    inner = _empty_table_offset(b, _Reboot.Start, _Reboot.End)
    return _wrap_command(b, inner, _CommandTag.Reboot)


def build_set_streaming_params(
    kp: Optional[Sequence[float]] = None,
    kd: Optional[Sequence[float]] = None,
    t_la: float = 0.0,
) -> bytes:
    b = flatbuffers.Builder(64)
    kp_vec = _float32_vector(b, kp) if kp is not None else None
    kd_vec = _float32_vector(b, kd) if kd is not None else None
    _SetStreamingParams.Start(b)
    if kp_vec is not None:
        _SetStreamingParams.AddKp(b, kp_vec)
    if kd_vec is not None:
        _SetStreamingParams.AddKd(b, kd_vec)
    _SetStreamingParams.AddTLa(b, t_la)
    inner = _SetStreamingParams.End(b)
    return _wrap_command(b, inner, _CommandTag.SetStreamingParams)


# ---------- Response parsing ----------


def parse_response(data: bytes) -> tuple[int, str]:
    resp = _StreamCommandResponse.StreamCommandResponse.GetRootAs(data, 0)
    result = int(resp.Result())
    msg_bytes = resp.Message()
    if msg_bytes is None:
        message = ""
    elif isinstance(msg_bytes, bytes):
        message = msg_bytes.decode("utf-8", errors="replace")
    else:
        message = str(msg_bytes)
    return result, message


def raise_on_error(data: bytes) -> tuple[int, str]:
    """Returns (result_code, message). Raises CommandError subclass if result >= 10."""
    result, message = parse_response(data)
    if result == 0:
        return result, message
    cls = _ERROR_BY_CODE.get(result, CommandError)
    raise cls(message)
