import threading
from pathlib import Path

import pytest

from networkclaw_harness.runtime.delegation import DelegationError, HostDelegationBroker


def _lease(session_id: str):
    return {"session_id": session_id, "owner_id": "owner", "execution_epoch": 1,
            "lease_id": "lease", "lease_version": 1}


def test_host_must_grant_identity_workspace_and_budget_before_child_creation(tmp_path: Path):
    events = []
    broker = HostDelegationBroker(session_id="parent", emit=lambda kind, payload: events.append((kind, payload)))
    result = []
    worker = threading.Thread(target=lambda: result.append(broker.request(
        task_index=0, task_count=1, depth=1, requested_iterations=8,
    )))
    worker.start()
    while not events:
        worker.join(0.01)
    allocation_id = events[0][1]["allocation_id"]
    assert events[0][0] == "delegation.requested"
    assert "goal" not in events[0][1]
    broker.resolve(allocation_id, {
        "decision": "grant", "child_session_id": "child-1",
        "workspace_root": str(tmp_path / "child"), "lease": _lease("child-1"),
        "budget": {"max_iterations": 4, "timeout_seconds": 30, "max_output_bytes": 4096},
    })
    worker.join(1)
    assert result[0].child_session_id == "child-1"
    assert result[0].max_iterations == 4
    assert result[0].workspace_root == (tmp_path / "child").resolve()


def test_child_grant_carries_bounded_parent_lineage(tmp_path: Path):
    events = []
    broker = HostDelegationBroker(session_id="parent", emit=lambda kind, payload: events.append((kind, payload)))
    result = []
    worker = threading.Thread(target=lambda: result.append(broker.request(
        task_index=0, task_count=1, depth=1, requested_iterations=8,
        parent_turn_id="turn-parent", parent_generation=3,
    )))
    worker.start()
    while not events:
        worker.join(0.01)
    allocation_id = events[0][1]["allocation_id"]
    assert events[0][1]["parent_turn_id"] == "turn-parent"
    broker.resolve(allocation_id, {
        "decision": "grant", "child_session_id": "child-1",
        "workspace_root": str(tmp_path / "child"), "lease": _lease("child-1"),
        "budget": {"max_iterations": 4, "timeout_seconds": 30, "max_output_bytes": 4096},
    })
    worker.join(1)
    assert result[0].parent_session_id == "parent"
    assert result[0].parent_turn_id == "turn-parent"
    assert result[0].parent_generation == 3
    assert result[0].hermes_mapping()["lineage"]["child_session_id"] == "child-1"


def test_broker_rejects_malformed_child_grant_before_release(tmp_path: Path):
    events = []
    broker = HostDelegationBroker(session_id="parent", emit=lambda kind, payload: events.append((kind, payload)))
    failure = []
    def request():
        try:
            broker.request(task_index=0, task_count=1, depth=1, requested_iterations=8)
        except DelegationError as error:
            failure.append(error.code)
    worker = threading.Thread(target=request)
    worker.start()
    while not events:
        worker.join(0.01)
    allocation_id = events[0][1]["allocation_id"]
    with pytest.raises(DelegationError, match="workspace_root"):
        broker.resolve(allocation_id, {"decision": "grant", "child_session_id": "child"})
    broker.close()
    worker.join(1)
    assert failure == ["delegation_broker_closed"]


def test_denied_delegation_fails_closed():
    events = []
    broker = HostDelegationBroker(session_id="parent", emit=lambda kind, payload: events.append((kind, payload)))
    failure = []

    def request():
        try:
            broker.request(task_index=0, task_count=1, depth=1, requested_iterations=8)
        except DelegationError as error:
            failure.append(error.code)

    worker = threading.Thread(target=request)
    worker.start()
    while not events:
        worker.join(0.01)
    broker.resolve(events[0][1]["allocation_id"], {"decision": "deny", "reason": "capacity"})
    worker.join(1)
    assert failure == ["delegation_denied"]
