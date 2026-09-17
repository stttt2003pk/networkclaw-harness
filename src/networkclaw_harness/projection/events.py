"""Small, policy-neutral redaction used before events leave the Harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "chain_of_thought",
        "cookie",
        "internal_prompt",
        "password",
        "secret",
        "system_prompt",
        "token",
    }
)


def project_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively redact fields that must never reach a client event."""

    projected: dict[str, Any] = {}
    for key, item in value.items():
        if key.casefold() in SENSITIVE_KEYS:
            projected[key] = "[redacted]"
        elif isinstance(item, Mapping):
            projected[key] = project_mapping(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            projected[key] = [
                project_mapping(element) if isinstance(element, Mapping) else element
                for element in item
            ]
        else:
            projected[key] = item
    return projected

