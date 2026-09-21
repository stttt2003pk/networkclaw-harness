"""Frozen Host Protocol v1 command, event, and compatibility catalog."""

from __future__ import annotations

PROCESS_COMMANDS = frozenset(
    {"protocol.negotiate", "health.query", "capabilities.query", "metrics.query", "shutdown"}
)
SESSION_COMMANDS = frozenset(
    {
        "session.open",
        "session.resume",
        "session.close",
        "session.lease.update",
        "user.input",
        "turn.steer",
        "turn.cancel",
        "delegation.resolve",
        "clarification.answer",
        "approval.resolve",
    }
)
CONTROL_COMMANDS = frozenset(
    {"session.lease.update", "turn.steer", "turn.cancel", "delegation.resolve", "clarification.answer", "approval.resolve"}
)
COMMANDS = PROCESS_COMMANDS | SESSION_COMMANDS

EVENTS = frozenset(
    {
        "request.accepted",
        "protocol.negotiated",
        "health.status",
        "metrics.snapshot",
        "capabilities.report",
        "session.opened",
        "session.closed",
        "session.lease.updated",
        "turn.queued",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
        "turn.controlled",
        "assistant.delta",
        "plan.updated",
        "tool.started",
        "tool.progress",
        "tool.completed",
        "subagent.started",
        "subagent.completed",
        "delegation.requested",
        "artifact.created",
        "clarification.requested",
        "clarification.resolved",
        "approval.requested",
        "approval.resolved",
        "warning",
        "heartbeat",
        "error",
        "end",
        "shutdown.completed",
    }
)

# New optional fields and event types are compatible additions. A peer must reject
# unknown commands that can alter execution or authorization.
PRESENTATION_PREFIX = "presentation."
MAX_FRAME_BYTES = 64 * 1024
MAX_PENDING_FRAMES = 128
WRITE_TIMEOUT_MS = 5000
INCOMPATIBLE_PROTOCOL_EXIT = 64

ERROR_CODES = frozenset(
    {
        "invalid_json",
        "invalid_frame",
        "invalid_payload",
        "invalid_request",
        "invalid_identity",
        "unsupported_protocol_version",
        "unsupported_command",
        "unknown_control",
        "request_id_conflict",
        "request_in_progress",
        "session_not_open",
        "session_identity_mismatch",
        "invalid_state_transition",
        "stale_run_version",
        "stale_interaction_version",
        "interaction_not_found",
        "interaction_expired",
        "interaction_id_conflict",
        "resolution_conflict",
        "approval_not_active",
        "approval_arguments_changed",
        "approval_scope_mismatch",
        "resource_exhausted",
        "backpressure",
        "slow_consumer",
        "runtime_unavailable",
        "turn_already_active",
        "delegation_allocation_not_found",
        "delegation_resolution_invalid",
        "lease_lost",
        "stream_closed",
    }
)
