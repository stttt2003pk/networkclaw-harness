from __future__ import annotations

import threading

import pytest

from networkclaw_harness.runtime import (
    BoundedStreamAssembler,
    ProviderChunk,
    RecoveryAction,
    RecoveryPolicy,
    StreamAssemblyError,
    StreamOutcome,
    StreamRecoveryRuntime,
    adapt_anthropic_event,
    adapt_openai_event,
)


def test_assembler_owns_sequence_split_tool_calls_usage_and_receipts():
    receipts = []
    assembler = BoundedStreamAssembler(receipt_sink=receipts.append)
    assembler.accept(ProviderChunk("hel", {"input_tokens": 3}))
    assembler.accept(ProviderChunk("lo", {"output_tokens": 1}))
    assembler.accept(ProviderChunk(tool_call={"index": 0, "id": "call-1", "function": {"name": "read", "arguments": '{"pa'}}))
    assembler.accept(ProviderChunk(tool_call={"index": 0, "function": {"arguments": 'th":"x"}'}}, finish_reason="tool_calls"))

    result = assembler.finish()

    assert result.text == "hello"
    assert result.last_sequence == 4
    assert [item.sequence for item in result.chunks] == [1, 2, 3, 4]
    assert [item.delivered_text for item in receipts] == ["hel", "lo"]
    assert dict(result.usage) == {"input_tokens": 3, "output_tokens": 1}
    assert result.tool_calls[0]["function"] == {"name": "read", "arguments": '{"path":"x"}'}
    assert result.invalid_reason is None


def test_assembler_rejects_sequence_gaps_and_multiple_writers():
    assembler = BoundedStreamAssembler()
    with pytest.raises(StreamAssemblyError, match="stream_sequence_invalid"):
        assembler.accept(ProviderChunk("x", sequence=2))

    owned = BoundedStreamAssembler()
    owned.accept(ProviderChunk("x"))
    errors = []
    thread = threading.Thread(target=lambda: _capture(errors, lambda: owned.accept(ProviderChunk("y"))))
    thread.start()
    thread.join()
    assert errors and errors[0].code == "stream_multiple_writers"


def test_interrupted_sse_returns_delivered_partial_without_retry():
    calls = []

    def request(directive):
        calls.append(directive)
        yield ProviderChunk("already delivered")
        raise OSError("private transport detail")

    result = StreamRecoveryRuntime().run(request)

    assert result.outcome is StreamOutcome.PARTIAL
    assert result.text == "already delivered"
    assert result.partial_delivered is True
    assert result.reason == "stream_interrupted"
    assert len(calls) == 1


def test_empty_response_ladder_uses_post_tool_nudge_then_housekeeping_or_fallback():
    directives = []

    def request(directive):
        directives.append(directive.action)
        return ()

    reused = StreamRecoveryRuntime().run(request, housekeeping_text="prior housekeeping")
    assert reused.text == "prior housekeeping"
    assert reused.reason == "housekeeping_reuse"

    attempts = iter(((), (ProviderChunk("recovered", finish_reason="stop"),)))
    recovered = StreamRecoveryRuntime().run(lambda directive: (directives.append(directive.action), next(attempts))[1], after_tool=True)
    assert recovered.outcome is StreamOutcome.COMPLETED
    assert directives[-1] is RecoveryAction.POST_TOOL_NUDGE

    empty = StreamRecoveryRuntime(RecoveryPolicy(max_empty_retries=0)).run(
        request, fallback=lambda _directive: (ProviderChunk("fallback", finish_reason="stop"),)
    )
    assert empty.text == "fallback"


def test_length_continuation_combines_text_and_context_gate_rolls_forward_partial():
    attempts = iter(((ProviderChunk("first ", finish_reason="length"),),
                     (ProviderChunk("second", finish_reason="stop"),)))
    directives = []
    receipts = []
    result = StreamRecoveryRuntime().run(lambda directive: (directives.append(directive), next(attempts))[1],
                                         receipt_sink=receipts.append)

    assert result.text == "first second"
    assert directives[1].action is RecoveryAction.CONTINUE_TEXT
    assert directives[1].prior_text == "first "
    assert [receipt.sequence for receipt in receipts] == [1, 2]
    assert result.delivery_sequence == 2

    gated = StreamRecoveryRuntime().run(
        lambda _directive: (ProviderChunk("partial", finish_reason="length"),),
        context_window=1000, prompt_tokens=700,
    )
    assert gated.outcome is StreamOutcome.TRUNCATED
    assert gated.text == "partial"
    assert gated.reason == "context_headroom_exhausted"


def test_reasoning_only_tool_retry_ceiling_and_continuation_rollback_are_bounded():
    reasoning = StreamRecoveryRuntime().run(
        lambda _directive: (ProviderChunk(reasoning_delta="hidden", finish_reason="length"),)
    )
    assert reasoning.outcome is StreamOutcome.TRUNCATED
    assert reasoning.reason == "reasoning_only"

    tool_attempts = []
    tool = {"index": 0, "id": "c", "function": {"name": "run", "arguments": "{}"}}
    tool_result = StreamRecoveryRuntime(RecoveryPolicy(max_tool_call_retries=1)).run(
        lambda directive: (tool_attempts.append(directive.action), (ProviderChunk(tool_call=tool, finish_reason="length"),))[1]
    )
    assert tool_attempts == [RecoveryAction.INITIAL, RecoveryAction.RETRY_TOOL_CALL]
    assert tool_result.reason == "tool_call_retry_ceiling"

    rolled_back = StreamRecoveryRuntime(RecoveryPolicy(max_continuations=1)).run(
        lambda _directive: (ProviderChunk("fragment", finish_reason="length"),)
    )
    assert rolled_back.outcome is StreamOutcome.TRUNCATED
    assert rolled_back.reason == "continuation_ceiling_rollback"
    assert "fragment" not in rolled_back.text

    guarded = StreamRecoveryRuntime(RecoveryPolicy(max_total_usage_tokens=1)).run(
        lambda _directive: (ProviderChunk("x", {"output_tokens": 2}, finish_reason="stop"),)
    )
    assert guarded.outcome is StreamOutcome.SAFE_SENTINEL
    assert guarded.reason == "usage_cost_guard"


def test_repetition_refusal_content_filter_and_invalid_shape_are_typed():
    loop = ("repeat this exact fragment for the guard to detect a degenerate response " * 10)
    repetition = StreamRecoveryRuntime().run(lambda _directive: (ProviderChunk(loop, finish_reason="stop"),))
    assert repetition.outcome is StreamOutcome.REPETITION
    assert repetition.text == ""

    refusal = StreamRecoveryRuntime().run(
        lambda _directive: (ProviderChunk("visible", refusal="refused", finish_reason="refusal"),)
    )
    assert refusal.outcome is StreamOutcome.REFUSAL
    assert refusal.text == "visible"

    filtered = StreamRecoveryRuntime().run(
        lambda _directive: (ProviderChunk("visible", finish_reason="content_filter"),)
    )
    assert filtered.outcome is StreamOutcome.CONTENT_FILTER
    assert filtered.text == "visible"

    invalid = StreamRecoveryRuntime().run(
        lambda _directive: (ProviderChunk("visible", finish_reason="made_up"),)
    )
    assert invalid.outcome is StreamOutcome.INVALID_SHAPE
    assert invalid.text == "visible"


def test_openai_and_anthropic_adapters_share_canonical_event_shape():
    openai = adapt_openai_event({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
                                 "usage": {"output_tokens": 1}})
    assert openai == (ProviderChunk("hi", {"output_tokens": 1}, "stop"),)

    responses = (
        *adapt_openai_event({"type": "response.output_text.delta", "delta": "hello"}),
        *adapt_openai_event({"type": "response.completed", "response": {"usage": {"output_tokens": 1}}}),
    )
    responses_assembler = BoundedStreamAssembler()
    for chunk in responses:
        responses_assembler.accept(chunk)
    assert responses_assembler.finish().text == "hello"
    assert responses_assembler.finish().finish_reason == "stop"

    start = adapt_anthropic_event({"type": "content_block_start", "index": 0,
                                   "content_block": {"type": "tool_use", "id": "t", "name": "read"}})
    delta = adapt_anthropic_event({"type": "content_block_delta", "index": 0,
                                   "delta": {"type": "input_json_delta", "partial_json": "{}"}})
    assembler = BoundedStreamAssembler()
    for chunk in (*start, *delta, *adapt_anthropic_event({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})):
        assembler.accept(chunk)
    assert assembler.finish().tool_calls[0]["function"] == {"name": "read", "arguments": "{}"}


def _capture(target, callback):
    try:
        callback()
    except Exception as error:
        target.append(error)
