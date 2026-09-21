"""Structured stderr diagnostics, low-cardinality metrics, and trace context."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .projection import project_mapping


HARNESS_METRICS = frozenset({
    "health_queries_total", "readiness", "protocol_errors_total",
    "turns_started_total", "turns_terminal_total", "tools_started_total",
    "tools_terminal_total", "recoveries_total", "recovery_unknown_total",
    "queue_depth", "active_sessions", "active_runs", "rss_bytes", "cpu_ticks",
    "protocol_latency_ms", "turn_latency_ms", "tool_latency_ms", "recovery_latency_ms",
    "provider_requests_total", "provider_latency_ms", "provider_input_tokens_total",
    "provider_output_tokens_total",
})


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    span_id: str
    session_id: str | None = None
    run_id: str | None = None
    invocation_id: str | None = None

    @classmethod
    def create(cls, *, session_id: str | None = None, run_id: str | None = None,
               invocation_id: str | None = None) -> "TraceContext":
        return cls(uuid.uuid4().hex, uuid.uuid4().hex[:16], session_id, run_id, invocation_id)

    def child(self, *, invocation_id: str | None = None) -> "TraceContext":
        return TraceContext(self.trace_id, uuid.uuid4().hex[:16], self.session_id, self.run_id,
                            invocation_id if invocation_id is not None else self.invocation_id)


class JsonLogFormatter(logging.Formatter):
    def __init__(self, *, secret_values: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret_values)

    def format(self, record: logging.LogRecord) -> str:
        extra = getattr(record, "fields", {})
        payload = {**dict(extra),
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(), "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(project_mapping(payload, secret_values=self._secrets),
                          sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def configure_stderr_logging(*, level: int = logging.INFO,
                             secret_values: Sequence[str] = ()) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter(secret_values=secret_values))
    root = logging.getLogger("networkclaw_harness")
    root.setLevel(level)
    root.handlers[:] = [handler]
    root.propagate = False
    return handler


class StructuredLogger:
    def __init__(self, logger: logging.Logger, context: TraceContext | None = None) -> None:
        self._logger = logger
        self._context = context

    def bind(self, context: TraceContext) -> "StructuredLogger":
        return StructuredLogger(self._logger, context)

    def event(self, name: str, *, level: int = logging.INFO, **fields: Any) -> None:
        context = self._context
        identity = {} if context is None else {
            "trace_id": context.trace_id, "span_id": context.span_id,
            "session_id": context.session_id, "run_id": context.run_id,
            "invocation_id": context.invocation_id,
        }
        self._logger.log(level, name, extra={"fields": {**fields, "event": name, **identity}})


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    counters: Mapping[str, int]
    gauges: Mapping[str, float]
    histograms: Mapping[str, Mapping[str, float]]


class MetricsRegistry:
    """In-process metrics with bounded names and labels; no session ids become labels."""

    def __init__(self, *, allowed_names: Sequence[str] = (), max_names: int = 128) -> None:
        self._allowed = frozenset(allowed_names)
        self._max_names = max_names
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = {}
        self._lock = threading.RLock()

    def _validate(self, name: str) -> None:
        if not name.isascii() or not name or (self._allowed and name not in self._allowed):
            raise ValueError("metric name is not allowlisted")
        if name not in self._counters and name not in self._gauges and name not in self._histograms:
            if len(self._counters) + len(self._gauges) + len(self._histograms) >= self._max_names:
                raise ValueError("metric name capacity is exhausted")

    def inc(self, name: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("counter increments must be non-negative")
        with self._lock:
            self._validate(name)
            self._counters[name] = self._counters.get(name, 0) + amount

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._validate(name)
            self._gauges[name] = float(value)

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._validate(name)
            self._histograms.setdefault(name, []).append(float(value))

    def snapshot(self) -> MetricSnapshot:
        with self._lock:
            histograms = {}
            for name, values in self._histograms.items():
                ordered = sorted(values)
                histograms[name] = {
                    "count": float(len(values)), "sum": sum(values),
                    "p50": ordered[(len(ordered) - 1) // 2],
                    "p95": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
                }
            return MetricSnapshot(dict(self._counters), dict(self._gauges), histograms)

    def prometheus(self, *, prefix: str = "networkclaw_harness") -> str:
        """Render a bounded text exposition for the host-owned metrics relay."""
        snapshot = self.snapshot()
        lines: list[str] = []
        for name, value in sorted(snapshot.counters.items()):
            lines.append(f"{prefix}_{name} {value}")
        for name, value in sorted(snapshot.gauges.items()):
            lines.append(f"{prefix}_{name} {value}")
        for name, values in sorted(snapshot.histograms.items()):
            base = f"{prefix}_{name}"
            lines.extend((
                f"{base}_count {int(values['count'])}",
                f"{base}_sum {values['sum']}",
                f"{base}_p50 {values['p50']}",
                f"{base}_p95 {values['p95']}",
            ))
        return "\\n".join(lines) + ("\\n" if lines else "")


@dataclass(slots=True)
class OperationTimer:
    metrics: MetricsRegistry
    name: str
    started: float = field(default_factory=time.perf_counter)

    def finish(self) -> float:
        elapsed_ms = (time.perf_counter() - self.started) * 1000
        self.metrics.observe(self.name, elapsed_ms)
        return elapsed_ms


@dataclass(frozen=True, slots=True)
class AlertRule:
    name: str
    metric: str
    threshold: float
    comparison: str = "gte"

    def __post_init__(self) -> None:
        if self.comparison not in {"gte", "lte"}:
            raise ValueError("alert comparison must be gte or lte")


@dataclass(frozen=True, slots=True)
class Alert:
    name: str
    metric: str
    value: float
    threshold: float


class DiagnosticService:
    def __init__(self, metrics: MetricsRegistry, rules: Sequence[AlertRule] = ()) -> None:
        self._metrics = metrics
        self._rules = tuple(rules)

    def snapshot(self, *, health: str, readiness: bool,
                 resources: Mapping[str, float | int | None] | None = None) -> Mapping[str, Any]:
        metrics = self._metrics.snapshot()
        values = {**metrics.counters, **metrics.gauges}
        alerts = []
        for rule in self._rules:
            value = float(values.get(rule.metric, 0))
            fired = value >= rule.threshold if rule.comparison == "gte" else value <= rule.threshold
            if fired:
                alerts.append(Alert(rule.name, rule.metric, value, rule.threshold))
        return {
            "health": health, "ready": readiness,
            "metrics": {
                "counters": dict(metrics.counters), "gauges": dict(metrics.gauges),
                "histograms": {name: dict(value) for name, value in metrics.histograms.items()},
            },
            "resources": dict(resources or {}),
            "alerts": [{
                "name": alert.name, "metric": alert.metric,
                "value": alert.value, "threshold": alert.threshold,
            } for alert in alerts],
        }
