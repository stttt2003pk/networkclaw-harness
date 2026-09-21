"""Versioned host protocol primitives."""

from .frames import InputFrame, ProtocolError, event_frame
from .version import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from .catalog import COMMANDS, EVENTS, ERROR_CODES

__all__ = [
    "InputFrame",
    "COMMANDS",
    "EVENTS",
    "ERROR_CODES",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "event_frame",
]
