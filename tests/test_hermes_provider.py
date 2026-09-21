from pathlib import Path

from networkclaw_harness.runtime.hermes_provider import resolve_route


def test_pinned_hermes_provider_route_loads_without_network():
    route = resolve_route("openai", "gpt-5.5", vendor_root=Path(__file__).parents[1] / "vendor" / "hermes")
    assert route["transport"] == "openai-compatible"
    assert route["model_id"] == "gpt-5.5"

