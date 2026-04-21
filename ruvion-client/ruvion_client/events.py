"""Parser + dataclasses for event.fbs.

Events are discrete state-change notifications delivered on reliable
unidirectional QUIC streams (one event per stream). Consume via
`Connection.events()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Union

from .proto.ruvion.motion import ActivityEvent as _ActivityEvent
from .proto.ruvion.motion import ControllerEvent as _ControllerEvent
from .proto.ruvion.motion import DriveModeEvent as _DriveModeEvent
from .proto.ruvion.motion import DriveStatusEvent as _DriveStatusEvent
from .proto.ruvion.motion import Event as _Event
from .proto.ruvion.motion import EventType as _EventType
from .proto.ruvion.motion import MotionStatusEvent as _MotionStatusEvent


class EventDriveStatus(IntEnum):
    Off = 0
    SettingUp = 1
    BusFault = 2
    DriveFault = 3
    QuickStop = 4
    Operational = 5


class EventMotionStatus(IntEnum):
    Idle = 0
    Settling = 1
    Moving = 2
    Braking = 3


class EventActivityType(IntEnum):
    Idle = 0
    GravityCalibration = 1
    FrictionCalibration = 2


class EventDriveMode(IntEnum):
    Csp = 0
    Impedance = 1


@dataclass
class DriveStatusEvent:
    status: EventDriveStatus


@dataclass
class MotionStatusEvent:
    status: EventMotionStatus


@dataclass
class ActivityEvent:
    activity: EventActivityType
    step: int
    total: int


@dataclass
class DriveModeEvent:
    mode: EventDriveMode


@dataclass
class ControllerEvent:
    has_controller: bool


AnyEvent = Union[
    DriveStatusEvent,
    MotionStatusEvent,
    ActivityEvent,
    DriveModeEvent,
    ControllerEvent,
]


class UnknownEvent(RuntimeError):
    """Raised when the event tag is outside the known range (forward-compat)."""


def parse_event(data: bytes) -> AnyEvent:
    """Parse one Event frame (a full FlatBuffer message) into the corresponding
    dataclass. Raises UnknownEvent for unrecognised tags.
    """
    ev = _Event.Event.GetRootAs(data, 0)
    tag = int(ev.EventType())
    inner = ev.Event()

    if tag == _EventType.EventType.DriveStatusEvent:
        t = _DriveStatusEvent.DriveStatusEvent()
        t.Init(inner.Bytes, inner.Pos)
        return DriveStatusEvent(status=EventDriveStatus(t.Status()))

    if tag == _EventType.EventType.MotionStatusEvent:
        t = _MotionStatusEvent.MotionStatusEvent()
        t.Init(inner.Bytes, inner.Pos)
        return MotionStatusEvent(status=EventMotionStatus(t.Status()))

    if tag == _EventType.EventType.ActivityEvent:
        t = _ActivityEvent.ActivityEvent()
        t.Init(inner.Bytes, inner.Pos)
        return ActivityEvent(
            activity=EventActivityType(t.Activity()),
            step=int(t.Step()),
            total=int(t.Total()),
        )

    if tag == _EventType.EventType.DriveModeEvent:
        t = _DriveModeEvent.DriveModeEvent()
        t.Init(inner.Bytes, inner.Pos)
        return DriveModeEvent(mode=EventDriveMode(t.Mode()))

    if tag == _EventType.EventType.ControllerEvent:
        t = _ControllerEvent.ControllerEvent()
        t.Init(inner.Bytes, inner.Pos)
        return ControllerEvent(has_controller=bool(t.HasController()))

    raise UnknownEvent(f"unknown event tag: {tag}")
