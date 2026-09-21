import json
import runpy

import pytest

from networkclaw_harness.runtime.retirement import (
    RetirementEvidence, RuntimeMode, RuntimeSelectionError,
    evaluate_retirement, select_runtime,
)


def test_production_runtime_selection_is_fail_closed_without_hermes():
    with pytest.raises(RuntimeSelectionError):
        select_runtime(requested="coordinator_fixture", production=True)
    with pytest.raises(RuntimeSelectionError):
        select_runtime(requested="hermes_adapter", production=True, hermes_ready=False)


def test_only_native_hermes_runtime_is_admitted():
    selected = select_runtime(requested="hermes_adapter", production=False, hermes_ready=True)
    assert selected.mode is RuntimeMode.HERMES_ADAPTER
    with pytest.raises(RuntimeSelectionError):
        select_runtime(requested="deterministic_reference", production=False)


def test_retirement_requires_all_evidence_and_preserves_explicit_rollback():
    blocked = evaluate_retirement(RetirementEvidence(True, False, True, True, True, "commit"))
    assert not blocked.can_retire
    assert "missing_long_context_evidence" in blocked.reasons
    complete = evaluate_retirement(RetirementEvidence(True, True, True, True, True, "commit"))
    assert complete.can_retire
    assert "never dual-run" in complete.rollback_admission


def test_local_h9_acceptance_produces_resume_and_compaction_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "src")
    module = runpy.run_path("scripts/run-live-long-context-acceptance.py")
    report = module["run"](tmp_path / "h9.json")
    assert report["status"] == "local_contract_verified"
    assert report["long_context"] is True
    assert report["reconnect_resume"] is True
    assert report["unknown_side_effect_replay"] is False
    assert json.loads((tmp_path / "h9.json").read_text())["local_probes"]["checkpoint_resume"] is True
