"""Versioned host protocol primitives."""

from .frames import InputFrame, ProtocolError, event_frame
from .version import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

__all__ = [
    "InputFrame",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "event_frame",
]

