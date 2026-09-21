#!/usr/bin/env python3
"""Repeatable local H5 capacity and latency baseline."""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from networkclaw_harness.capacity import CapacityError, CapacityLedger, CapacityLimits
from networkclaw_harness.runtime import (
    FairSessionScheduler,
    ProviderChunk,
    ProviderConfigRef,
    ProviderRequest,
    ProviderRuntime,
    ReferenceProviderResolver,
    ScheduledInput,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if args.iterations < 3:
        raise SystemExit("iterations must be at least 3")

    cold = [_cold_import_ms() for _ in range(args.iterations)]
    first_token = [_first_token_ms() for _ in range(args.iterations)]
    fairness = _scheduler_benchmark()
    backpressure = _capacity_benchmark()
    rss = _rss_bytes()
    thresholds = {
        "cold_start_p95_ms": 3000.0,
        "first_token_p95_ms": 100.0,
        "rss_bytes": 512 * 1024**2,
        "scheduler_ops_per_second": 100.0,
    }
    measurements = {
        "cold_start_p50_ms": statistics.median(cold),
        "cold_start_p95_ms": _percentile(cold, .95),
        "first_token_p50_ms": statistics.median(first_token),
        "first_token_p95_ms": _percentile(first_token, .95),
        "rss_bytes": rss,
        **fairness,
        **backpressure,
    }
    checks = {
        "cold_start": measurements["cold_start_p95_ms"] <= thresholds["cold_start_p95_ms"],
        "first_token": measurements["first_token_p95_ms"] <= thresholds["first_token_p95_ms"],
        "memory": measurements["rss_bytes"] <= thresholds["rss_bytes"],
        "scheduler": measurements["scheduler_ops_per_second"] >= thresholds["scheduler_ops_per_second"],
        "fairness": measurements["fairness_max_lead"] <= 1,
        "backpressure": measurements["backpressure_enforced"],
    }
    report = {
        "schema_version": 1,
        "environment": {"python": sys.version.split()[0], "platform": sys.platform},
        "scenario": {
            "sessions": 64, "queued_per_session": 16,
            "tool_peak": 16, "subagent_peak": 8,
        },
        "thresholds": thresholds, "measurements": measurements,
        "checks": checks, "status": "passed" if all(checks.values()) else "failed",
        "ownership": "one Harness remains exclusively owned by one chatsvc",
        "optimization": {
            "lazy_start": True, "idle_reclaim_seconds": 900,
            "lazy_provider": True, "lazy_browser": True, "lazy_mcp": True,
            "prewarm": "allowed only before exclusive lease to one chatsvc",
        },
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["status"] == "passed" else 1


def _cold_import_ms() -> float:
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    started = time.perf_counter()
    subprocess.run(
        [sys.executable, "-c", "import networkclaw_harness.runtime; import networkclaw_harness.host"],
        cwd=REPO_ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True, timeout=10,
    )
    return (time.perf_counter() - started) * 1000


def _first_token_ms() -> float:
    class Client:
        @staticmethod
        def stream(request, *, model_id, timeout_ms):
            yield ProviderChunk(delta="token")

    runtime = ProviderRuntime(ReferenceProviderResolver(lambda config: Client()))
    request = ProviderRequest("benchmark", b"system", (), "cache", 16)
    config = ProviderConfigRef("benchmark", "stub", "offline-ref", 1_000, 1)
    observed: list[float] = []
    started = time.perf_counter()
    runtime.invoke(
        request,
        primary=config,
        on_chunk=lambda chunk: observed.append((time.perf_counter() - started) * 1000),
    )
    if not observed:
        raise RuntimeError("provider did not emit a first token")
    return observed[0]


def _scheduler_benchmark() -> dict[str, float | int]:
    scheduler = FairSessionScheduler(max_active_runs=1, max_queued_per_session=16)
    sessions = [f"s{index}" for index in range(64)]
    for turn in range(16):
        for session in sessions:
            scheduler.enqueue(ScheduledInput(session, f"{session}-{turn}", None))
    dispatched: list[str] = []
    started = time.perf_counter()
    while item := scheduler.dispatch():
        dispatched.append(item.session_id)
        scheduler.complete(item.session_id, item.item_id)
    elapsed = max(time.perf_counter() - started, 1e-9)
    first_round = dispatched[:len(sessions)]
    counts = {session: first_round.count(session) for session in sessions}
    return {
        "scheduler_operations": len(dispatched),
        "scheduler_ops_per_second": len(dispatched) / elapsed,
        "fairness_max_lead": max(counts.values()) - min(counts.values()),
        "concurrent_sessions": len(sessions),
    }


def _capacity_benchmark() -> dict[str, bool | int]:
    ledger = CapacityLedger(CapacityLimits(concurrent_tools=16, concurrent_subagents=8))
    for _ in range(16):
        ledger.reserve_tool()
    for _ in range(8):
        ledger.reserve_subagent()
    enforced = False
    try:
        ledger.reserve_tool()
    except CapacityError:
        enforced = True
    return {"tool_peak": 16, "subagent_peak": 8, "backpressure_enforced": enforced}


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * quantile))]


if __name__ == "__main__":
    raise SystemExit(main())
