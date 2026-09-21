"""Hermes-compatible tool manifests and a profile-filtered execution surface."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from networkclaw_harness.policies import RuntimeProfile


class ToolRegistryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolLayer(StrEnum):
    CORE = "core"
    PROFILE = "profile"
    SERVICE = "service"
    SESSION = "session"


class ToolAdapter(Protocol):
    def __call__(self, arguments: Mapping[str, Any], cancellation: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ToolManifest:
    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    policy_id: str
    capability: str
    layer: ToolLayer
    source: str
    adapter: ToolAdapter

    def __post_init__(self) -> None:
        if any(not value or not value.isascii() for value in (self.name, self.version, self.policy_id, self.capability)):
            raise ValueError("tool manifest identities must be non-empty ASCII strings")
        if self.input_schema.get("type") != "object" or self.output_schema.get("type") != "object":
            raise ValueError("tool input and output schemas must be JSON object schemas")
        if self.layer is ToolLayer.CORE and self.capability != "core":
            raise ValueError("core tools must use the always-available core capability")
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
        object.__setattr__(self, "output_schema", MappingProxyType(dict(self.output_schema)))

    def model_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name, "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


class HermesToolRegistryPort(Protocol):
    def register(self, manifest: ToolManifest) -> None: ...


class ToolRegistry:
    def __init__(self, *, hermes: HermesToolRegistryPort | None = None) -> None:
        self._manifests: dict[str, ToolManifest] = {}
        self._hermes = hermes

    def register(self, manifest: ToolManifest) -> None:
        if manifest.name in self._manifests:
            raise ToolRegistryError("tool_conflict", "tool name is already registered")
        self._manifests[manifest.name] = manifest
        if self._hermes is not None:
            self._hermes.register(manifest)

    def surface(self, *, profile: RuntimeProfile, reachable_services: Sequence[str],
                session_tools: Sequence[str]) -> "ToolSurface":
        reachable = set(reachable_services)
        requested = set(session_tools)
        selected: dict[str, ToolManifest] = {}
        for name, manifest in self._manifests.items():
            if manifest.layer is ToolLayer.CORE:
                selected[name] = manifest
            elif manifest.layer is ToolLayer.PROFILE and _capability_enabled(profile, manifest.capability):
                selected[name] = manifest
            elif (manifest.layer is ToolLayer.SERVICE and name in reachable
                  and _capability_enabled(profile, manifest.capability)):
                selected[name] = manifest
            elif (manifest.layer is ToolLayer.SESSION and name in requested
                  and _capability_enabled(profile, manifest.capability)):
                selected[name] = manifest
        # Session selection is a restriction for optional tools, never a way to hide core guards.
        if requested:
            selected = {name: value for name, value in selected.items()
                        if value.layer is ToolLayer.CORE or name in requested}
        return ToolSurface(selected)


class ToolSurface:
    def __init__(self, manifests: Mapping[str, ToolManifest]) -> None:
        self._manifests = MappingProxyType(dict(manifests))

    def require(self, name: str) -> ToolManifest:
        try:
            return self._manifests[name]
        except KeyError as error:
            raise ToolRegistryError("tool_not_available", "tool is outside the active session surface") from error

    @property
    def definitions(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._manifests[name].model_definition() for name in sorted(self._manifests))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._manifests))


def validate_schema(value: Mapping[str, Any], schema: Mapping[str, Any], *, label: str) -> None:
    if not isinstance(value, Mapping):
        raise ToolRegistryError("schema_invalid", f"{label} must be an object")
    properties = schema.get("properties", {})
    required = schema.get("required", ())
    additional = schema.get("additionalProperties", True)
    if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
        raise ToolRegistryError("schema_invalid", f"{label} schema is malformed")
    missing = [name for name in required if name not in value]
    if missing:
        raise ToolRegistryError("schema_invalid", f"{label} is missing required fields")
    if additional is False and set(value) - set(properties):
        raise ToolRegistryError("schema_invalid", f"{label} has unknown fields")
    for name, item in value.items():
        expected = properties.get(name, {}).get("type") if isinstance(properties.get(name), Mapping) else None
        if expected and not _matches_type(item, expected):
            raise ToolRegistryError("schema_invalid", f"{label}.{name} has the wrong type")


def _matches_type(value: Any, expected: str) -> bool:
    types: dict[str, type | tuple[type, ...]] = {
        "string": str, "integer": int, "number": (int, float), "boolean": bool,
        "object": Mapping, "array": (list, tuple), "null": type(None),
    }
    python_type = types.get(expected)
    if python_type is None:
        return False
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, python_type)


def _capability_enabled(profile: RuntimeProfile, capability: str) -> bool:
    return {
        "core": True, "shell": profile.shell_enabled, "network": profile.network_enabled,
        "browser": profile.browser_enabled and profile.network_enabled,
        "mcp": profile.network_enabled, "business_api": profile.network_enabled,
    }.get(capability, False)
