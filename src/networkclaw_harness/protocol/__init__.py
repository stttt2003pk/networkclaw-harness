"""Versioned host protocol primitives."""

from .frames import InputFrame, ProtocolError, event_frame
from .version import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from .catalog import ALL_EVENTS, COMMANDS, EVENTS, ERROR_CODES, PROCESS_EXTENSIONS

__all__ = [
    "InputFrame",
    "ALL_EVENTS",
    "COMMANDS",
    "EVENTS",
    "ERROR_CODES",
    "PROCESS_EXTENSIONS",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "event_frame",
]
