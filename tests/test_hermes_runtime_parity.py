from networkclaw_harness.runtime import (
    ContextPreflight, ContextItem, repair_transcript_tail, TranscriptMessage,
    CompactionCoordinator, CompactionPolicy, CompactionService, ReferenceDurableSessionStore,
    ReferenceHermesCompaction, CompactionRequest, bound_tool_output, bounded_context_items,
)
from networkclaw_harness.runtime.progress import BudgetLimits, BudgetExceeded, RunBudget


def test_iteration_budget_refund_matches_hermes_semantics():
    budget = RunBudget(BudgetLimits(max_steps=1))
    budget.consume("steps")
    assert budget.steps == 1 and budget.remaining_steps == 0
    budget.refund_iteration()
    assert budget.steps == 0 and budget.remaining_steps == 1
    budget.consume("steps")
    try:
        budget.consume("steps")
    except BudgetExceeded as error:
        assert error.dimension == "steps"
    else:
        raise AssertionError("iteration ceiling was not enforced")


def test_context_preflight_uses_provider_token_counts_and_cooldown():
    gate = ContextPreflight(context_window=100, output_reserve=20, threshold=.8, cooldown_turns=2)
    pressure = gate.estimate((ContextItem("h", "history", "x", 65, {}),), system_tokens=10)
    assert gate.should_compact(pressure, turn=1)
    gate.mark_compacted(1)
    assert not gate.should_compact(pressure, turn=2)
    assert gate.should_compact(pressure, turn=3)


def test_transcript_repair_drops_unmatched_tool_tail():
    messages = (
        TranscriptMessage("user", "inspect", "user_interaction"),
        TranscriptMessage("assistant", None, "model_step", tool_calls=("call",)),
    )
    assert repair_transcript_tail(messages) == (messages[0],)


def test_context_pressure_uses_provider_usage_and_bounds_latest_context():
    gate = ContextPreflight(context_window=100, output_reserve=20)
    pressure = gate.estimate(
        (ContextItem("old", "history", "old", 80, {}),),
        system_tokens=5, tool_tokens=3, plan_tokens=2,
        provider_usage={"input_tokens": 40, "output_tokens": 7},
    )
    assert pressure.input_tokens == 50
    assert pressure.provider_input_tokens == 40
    assert pressure.headroom == 30
    bounded, truncated = bound_tool_output("x" * 100, max_chars=32)
    assert truncated and len(bounded) <= 32
    items = tuple(ContextItem(str(i), "history", str(i), 4, {}, critical=(i == 0)) for i in range(6))
    selected = bounded_context_items(items, max_tokens=12, protect_latest=2)
    assert selected[0].item_id == "0" and {item.item_id for item in selected[-2:]} == {"4", "5"}


def test_compaction_coordinator_defers_and_fences_late_results():
    service = CompactionService(ReferenceDurableSessionStore(), ReferenceHermesCompaction(), lambda *_: None)
    coordinator = CompactionCoordinator(service, policy=CompactionPolicy(cooldown_turns=2, max_attempts=1))
    request = CompactionRequest(
        "session", 0, 1,
        (TranscriptMessage("user", "hello", "user_interaction"),), (), (), (),
    )
    first = coordinator.compact(request, event_id="compact", turn=1, workspace_epoch=4)
    assert first.status == "completed" and first.binding is not None
    deferred = coordinator.compact(request, event_id="compact-2", turn=2, workspace_epoch=4)
    assert deferred.status == "deferred" and deferred.reason == "cooldown"
    assert coordinator.accepts_late_commit(first.binding, current_input_hash="bad", workspace_epoch=4) is False
    assert coordinator.accepts_late_commit(first.binding, current_input_hash=first.binding.input_hash, workspace_epoch=4)
