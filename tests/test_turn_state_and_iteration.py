from networkclaw_harness.runtime import (
    BudgetLimits, IterationReceiptState, RunBudget, TurnExitReason, TurnPhase, TurnState,
)


def test_iteration_receipt_only_refunds_before_request_or_token():
    budget = RunBudget(BudgetLimits(max_steps=2))
    receipt = budget.begin_provider_turn()
    assert receipt.state is IterationReceiptState.RESERVED
    assert receipt.refund() is True
    assert budget.steps == 0
    receipt = budget.begin_provider_turn()
    receipt.request_emitted()
    assert receipt.refund() is False
    receipt.token_emitted()
    receipt.commit()
    assert budget.steps == 1


def test_turn_state_public_dict_is_bounded_and_typed():
    state = TurnState("run", "turn", 1, phase=TurnPhase.CHECKPOINT,
                      exit_reason=TurnExitReason.BUDGET, counters={"tool_executions": 2})
    payload = state.public_dict()
    assert payload["phase"] == "checkpoint"
    assert payload["exit_reason"] == "budget"
    assert "prompt" not in payload and "reasoning" not in payload and "raw_payload" not in payload
