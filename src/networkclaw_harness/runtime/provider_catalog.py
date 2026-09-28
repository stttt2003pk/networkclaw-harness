"""Small, explicit model catalog for provider admission and parameter mapping."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelCapability:
    provider_id: str
    model_id: str
    context_tokens: int
    reasoning: bool = True
    tools: bool = True


_GPT5 = {
    name: ModelCapability("openai", name, 400_000, reasoning=True, tools=True)
    for name in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra")
}

_ZHIPU = {
    "glm-5.3": ModelCapability("zai", "glm-5.3", 1_310_720, reasoning=True, tools=True),
}


def canonical_model(provider_id: str, model_id: str) -> ModelCapability:
    provider = provider_id.strip().lower()
    model = model_id.strip()
    if provider in {"openai-compatible", "openai_api", "custom"}:
        provider = "openai"
    if provider in {"zhipu", "glm", "z-ai"}:
        provider = "zai"
    capability = (_GPT5.get(model) if provider == "openai" else
                  _ZHIPU.get(model) if provider == "zai" else None)
    if capability is None:
        raise ValueError("model is not in the authorized provider catalog")
    return capability


def reasoning_parameters(model_id: str, effort: str | None = None) -> dict[str, object]:
    if not effort:
        return {}
    if effort not in {"minimal", "low", "medium", "high"}:
        raise ValueError("reasoning effort is not supported")
    return {"reasoning_effort": effort} if model_id.startswith("gpt-5") else {}
