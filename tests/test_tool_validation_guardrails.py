from networkclaw_harness.tools.validation import ToolValidationState, validate_tool_calls


def call(call_id, name, arguments):
    return {"id": call_id, "function": {"name": name, "arguments": arguments}}


def test_mixed_batch_repairs_name_and_keeps_valid_call():
    result = validate_tool_calls(
        (call("same", "read_flie", "{}"), call("same", "write_file", "{")),
        ("read_file",), ToolValidationState(),
    )
    assert [item.tool_name for item in result.valid] == ["read_file"]
    assert result.errors[0].tool_name == "write_file"
    assert result.valid[0].call_id != result.errors[0].call_id


def test_invalid_json_retries_then_injects_error_without_execution():
    state = ToolValidationState(max_json_retries=2)
    malformed = (call("c1", "inspect", '{bad}'),)
    assert validate_tool_calls(malformed, ("inspect",), state).retry
    assert validate_tool_calls(malformed, ("inspect",), state).retry
    final = validate_tool_calls(malformed, ("inspect",), state)
    assert not final.retry and final.errors and not final.valid


def test_truncated_json_is_partial_and_unknown_names_three_strikes():
    state = ToolValidationState()
    assert validate_tool_calls((call("c", "inspect", '{"x":'),), ("inspect",), state).partial
    state = ToolValidationState()
    for _ in range(2):
        assert not validate_tool_calls((call("c", "missing", "{}"),), ("inspect",), state).partial
    assert validate_tool_calls((call("c", "missing", "{}"),), ("inspect",), state).partial
