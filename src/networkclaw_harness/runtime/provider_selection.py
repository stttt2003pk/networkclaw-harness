"""Host-owned provider route selection for the native Hermes adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from .provider_catalog import canonical_model


class ProviderSelectionError(RuntimeError):
    """The host supplied a provider route that is not authorized."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    provider_id: str
    model_id: str
    config_ref: str


def load_selection(payload: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> ProviderSelection:
    """Resolve one explicit, allowlisted route without exposing credentials."""
    env = environ or os.environ
    raw = payload.get("provider_selection")
    if isinstance(raw, Mapping):
        provider = str(raw.get("provider_id") or "").strip().lower()
        model = str(raw.get("model_id") or "").strip()
        config = str(raw.get("config_ref") or "env:openai").strip()
    else:
        provider = str(payload.get("provider") or env.get("NETWORKCLAW_HARNESS_PROVIDER") or "openai").strip().lower()
        model = str(payload.get("model") or env.get("OPENAI_MODEL") or "").strip()
        config = "env:openai"
    if not provider or not model or not config or any("\n" in value or "\r" in value for value in (provider, model, config)):
        raise ProviderSelectionError("provider_selection_invalid", "provider selection is incomplete")
    if config != "env:openai":
        raise ProviderSelectionError("config_ref_not_authorized", "provider config reference is not authorized")
    try:
        canonical_model(provider, model)
    except ValueError as error:
        raise ProviderSelectionError("model_not_authorized", "requested provider/model route is not authorized") from error
    allowed = {item.strip() for item in env.get("NETWORKCLAW_HARNESS_ALLOWED_MODELS", "").split(",") if item.strip()}
    if allowed and model not in allowed:
        raise ProviderSelectionError("model_not_authorized", "requested model is not authorized")
    return ProviderSelection(provider, model, config)
