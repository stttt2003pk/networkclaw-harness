import pytest

from networkclaw_harness.lifecycle import guard


def test_container_pid_one_is_a_valid_matching_owner(monkeypatch):
    started = []
    monkeypatch.setattr(guard.os, "getppid", lambda: 1)
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    monkeypatch.setattr(guard.threading.Thread, "start", lambda thread: started.append(thread.name))
    guard.install_parent_death_guard(1)
    assert started == ["parent-death-guard"]
    with pytest.raises(RuntimeError, match="expected=2, actual=1"):
        guard.install_parent_death_guard(2)
    with pytest.raises(RuntimeError, match="expected=0"):
        guard.install_parent_death_guard(0)
