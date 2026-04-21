"""Builders for control_datagram.fbs (MotionCommand / QUIC datagrams).

The schema has file_identifier "MOCM". Sent as unreliable datagrams; the
server brakes if a successor doesn't arrive within `ttl_ms`. Rate control
is the caller's responsibility — the builder/sender just serialises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Union

import flatbuffers

from .proto.ruvion.motion import ArmCommand as _ArmCommand
from .proto.ruvion.motion import HumanoidCommand as _HumanoidCommand
from .proto.ruvion.motion import JointImpedance as _JointImpedance
from .proto.ruvion.motion import JointPoseCommand as _JointPoseCommand
from .proto.ruvion.motion import JointTwistCommand as _JointTwistCommand
from .proto.ruvion.motion import MotionCommand as _MotionCommand
from .proto.ruvion.motion import SingleCommand as _SingleCommand
from .proto.ruvion.motion import TcpImpedance as _TcpImpedance

PROTOCOL_VERSION = 1
FILE_IDENTIFIER = b"MOCM"

# Union tags for ArmCommand.CommandType (joint commands only — distinct from
# the StreamCommand union even though flatc puts them in a shared CommandType
# class when schemas share a namespace).
_ARM_COMMAND_JOINT_TWIST = 1
_ARM_COMMAND_JOINT_POSE = 2

# Union tags for MotionCommand.RobotCommandType
_ROBOT_COMMAND_SINGLE = 1
_ROBOT_COMMAND_HUMANOID = 2

# Union tags for JointPoseCommand/JointTwistCommand.ImpedanceType
_IMPEDANCE_NONE = 0
_IMPEDANCE_TCP = 1
_IMPEDANCE_JOINT = 2


# ---------- User-facing dataclasses ----------


@dataclass
class TcpImpedance:
    """TCP Cartesian impedance (6-DoF, robot base frame).

    stiffness: [x, y, z, rx, ry, rz] — N/m for xyz, Nm/rad for rxryrz.
    damping:   same axes — N·s/m and Nm·s/rad.
    """
    stiffness: Sequence[float]
    damping: Sequence[float]


@dataclass
class JointImpedance:
    """Joint-space impedance (7 joints).

    stiffness: Nm/rad per joint.
    damping:   Nm·s/rad per joint.
    """
    stiffness: Sequence[float]
    damping: Sequence[float]


Impedance = Union[TcpImpedance, JointImpedance]


@dataclass
class JointPose:
    """Joint-space position command.

    position: 7 values (rev).
    velocity: optional — omit (None) for "stop at target".
    impedance: optional override, only effective in Impedance DriveMode.
    """
    position: Sequence[float]
    velocity: Optional[Sequence[float]] = None
    impedance: Optional[Impedance] = None


@dataclass
class JointTwist:
    """Joint-space velocity command.

    twist: 7 values (rev/s).
    impedance: optional override, only effective in Impedance DriveMode.
    """
    twist: Sequence[float]
    impedance: Optional[Impedance] = None


ArmMotion = Union[JointPose, JointTwist]


# ---------- Low-level builders ----------


def _f32_vec(b: flatbuffers.Builder, values: Sequence[float]) -> int:
    vs = list(values)
    b.StartVector(4, len(vs), 4)
    for v in reversed(vs):
        b.PrependFloat32(float(v))
    return b.EndVector()


def _build_impedance(
    b: flatbuffers.Builder, imp: Impedance
) -> tuple[int, int]:
    """Returns (tag, offset) for the impedance union slot."""
    if isinstance(imp, JointImpedance):
        stiff = _f32_vec(b, imp.stiffness)
        damp = _f32_vec(b, imp.damping)
        _JointImpedance.Start(b)
        _JointImpedance.AddStiffness(b, stiff)
        _JointImpedance.AddDamping(b, damp)
        return _IMPEDANCE_JOINT, _JointImpedance.End(b)
    if isinstance(imp, TcpImpedance):
        stiff = _f32_vec(b, imp.stiffness)
        damp = _f32_vec(b, imp.damping)
        _TcpImpedance.Start(b)
        _TcpImpedance.AddStiffness(b, stiff)
        _TcpImpedance.AddDamping(b, damp)
        return _IMPEDANCE_TCP, _TcpImpedance.End(b)
    raise TypeError(f"unknown impedance type: {type(imp).__name__}")


def _build_arm_command(b: flatbuffers.Builder, motion: ArmMotion) -> int:
    """Build one ArmCommand (with inner JointPose or JointTwist) and return
    its offset."""
    if isinstance(motion, JointPose):
        pos_vec = _f32_vec(b, motion.position)
        vel_vec = (
            _f32_vec(b, motion.velocity) if motion.velocity is not None else None
        )
        imp_tag, imp_off = (
            _build_impedance(b, motion.impedance)
            if motion.impedance is not None
            else (_IMPEDANCE_NONE, 0)
        )

        _JointPoseCommand.Start(b)
        _JointPoseCommand.AddPosition(b, pos_vec)
        if vel_vec is not None:
            _JointPoseCommand.AddVelocity(b, vel_vec)
        if imp_tag != _IMPEDANCE_NONE:
            _JointPoseCommand.AddImpedanceType(b, imp_tag)
            _JointPoseCommand.AddImpedance(b, imp_off)
        inner = _JointPoseCommand.End(b)
        cmd_tag = _ARM_COMMAND_JOINT_POSE

    elif isinstance(motion, JointTwist):
        twist_vec = _f32_vec(b, motion.twist)
        imp_tag, imp_off = (
            _build_impedance(b, motion.impedance)
            if motion.impedance is not None
            else (_IMPEDANCE_NONE, 0)
        )

        _JointTwistCommand.Start(b)
        _JointTwistCommand.AddTwist(b, twist_vec)
        if imp_tag != _IMPEDANCE_NONE:
            _JointTwistCommand.AddImpedanceType(b, imp_tag)
            _JointTwistCommand.AddImpedance(b, imp_off)
        inner = _JointTwistCommand.End(b)
        cmd_tag = _ARM_COMMAND_JOINT_TWIST

    else:
        raise TypeError(f"unknown arm motion type: {type(motion).__name__}")

    _ArmCommand.Start(b)
    _ArmCommand.AddCommandType(b, cmd_tag)
    _ArmCommand.AddCommand(b, inner)
    return _ArmCommand.End(b)


def _finish_motion(
    b: flatbuffers.Builder,
    robot_tag: int,
    robot_offset: int,
    seq: int,
    ttl_ms: int,
) -> bytes:
    _MotionCommand.Start(b)
    _MotionCommand.AddProtocolVersion(b, PROTOCOL_VERSION)
    _MotionCommand.AddSeq(b, seq)
    _MotionCommand.AddTtlMs(b, ttl_ms)
    _MotionCommand.AddRobotCommandType(b, robot_tag)
    _MotionCommand.AddRobotCommand(b, robot_offset)
    root = _MotionCommand.End(b)
    b.Finish(root, file_identifier=FILE_IDENTIFIER)
    return bytes(b.Output())


def build_single(motion: ArmMotion, *, seq: int, ttl_ms: int) -> bytes:
    """Build a MotionCommand for a single-arm robot."""
    b = flatbuffers.Builder(256)
    arm = _build_arm_command(b, motion)
    _SingleCommand.Start(b)
    _SingleCommand.AddArm(b, arm)
    single = _SingleCommand.End(b)
    return _finish_motion(b, _ROBOT_COMMAND_SINGLE, single, seq, ttl_ms)


def build_humanoid(
    right: ArmMotion, left: ArmMotion, *, seq: int, ttl_ms: int
) -> bytes:
    """Build a MotionCommand for a humanoid (dual-arm) robot."""
    b = flatbuffers.Builder(512)
    right_arm = _build_arm_command(b, right)
    left_arm = _build_arm_command(b, left)
    _HumanoidCommand.Start(b)
    _HumanoidCommand.AddRight(b, right_arm)
    _HumanoidCommand.AddLeft(b, left_arm)
    hum = _HumanoidCommand.End(b)
    return _finish_motion(b, _ROBOT_COMMAND_HUMANOID, hum, seq, ttl_ms)
