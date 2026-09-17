import pytest

from networkclaw_harness.protocol import InputFrame, ProtocolError


def test_input_frame_requires_supported_version_and_correlation_id():
    frame = InputFrame.from_mapping(
        {"protocol_version": "1.0", "type": "health.query", "request_id": "req-1"}
    )
    assert frame.request_id == "req-1"

    with pytest.raises(ProtocolError, match="supported versions"):
        InputFrame.from_mapping(
            {"protocol_version": "2.0", "type": "health.query", "request_id": "req-2"}
        )

