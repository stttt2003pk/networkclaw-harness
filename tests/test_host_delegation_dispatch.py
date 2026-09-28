from types import SimpleNamespace

import pytest

from networkclaw_harness.runtime.hermes_native_probe import load_agent_class


@pytest.mark.parametrize("host_controlled,depth,background", [
    (False, 0, True), (False, 1, False), (True, 0, False), (True, 1, False),
])
def test_native_dispatch_honors_host_ownership(monkeypatch, host_controlled, depth, background):
    agent_type = load_agent_class()
    import tools.delegate_tool as delegate

    calls = []
    monkeypatch.setattr(delegate, "delegate_task", lambda **kwargs: calls.append(kwargs) or "done")
    agent = SimpleNamespace(_delegate_depth=depth)
    if host_controlled:
        agent._networkclaw_take_delegation_grant = lambda **kwargs: None
    assert agent_type._dispatch_delegate_task(agent, {"goal": "inspect", "background": True}) == "done"
    assert calls[0]["background"] is background
    assert calls[0]["parent_agent"] is agent
