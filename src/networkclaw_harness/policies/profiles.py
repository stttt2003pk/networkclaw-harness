"""Profiles keep mutable runtime capability explicit and auditable."""

from dataclasses import dataclass

from networkclaw_harness.capacity import CapacityLimits
from networkclaw_harness.workspace import QuotaPolicy
from networkclaw_harness.runtime.progress import BudgetLimits


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    name: str
    browser_enabled: bool
    network_enabled: bool
    shell_enabled: bool
    runtime_skill_install_enabled: bool = False
    runtime_tool_install_enabled: bool = False
    self_evolution_enabled: bool = False
    capacity: CapacityLimits = CapacityLimits()
    workspace_quotas: QuotaPolicy = QuotaPolicy(
        tenant_bytes=10 * 1024**3, session_bytes=1024**3, session_files=10_000,
        single_file_bytes=256 * 1024**2, tmp_bytes=256 * 1024**2,
    )
    budget_limits: BudgetLimits = BudgetLimits()

    def with_budget_override(self, **overrides: object) -> "RuntimeProfile":
        """Apply an explicit, bounded per-run override without mutating the profile."""
        allowed = set(BudgetLimits.__dataclass_fields__)
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown budget override: {sorted(unknown)}")
        values = {name: getattr(self.budget_limits, name) for name in allowed}
        values.update(overrides)
        limits = BudgetLimits(**values)
        return RuntimeProfile(
            self.name, self.browser_enabled, self.network_enabled, self.shell_enabled,
            self.runtime_skill_install_enabled, self.runtime_tool_install_enabled,
            self.self_evolution_enabled, self.capacity, self.workspace_quotas, limits,
        )


def development_profile() -> RuntimeProfile:
    return RuntimeProfile(
        name="development",
        browser_enabled=True,
        network_enabled=True,
        shell_enabled=True,
        capacity=CapacityLimits(
            concurrent_sessions=128, concurrent_runs=16, concurrent_tools=32,
            concurrent_subagents=16, token_budget=2_000_000, queue_depth=128,
            cpu_cores=4, memory_bytes=4 * 1024**3, open_files=1024,
            subprocesses=32, browser_contexts=4, mcp_servers=16,
            idle_reclaim_seconds=1800,
        ),
        workspace_quotas=QuotaPolicy(
            tenant_bytes=20 * 1024**3, session_bytes=2 * 1024**3, session_files=20_000,
            single_file_bytes=512 * 1024**2, tmp_bytes=512 * 1024**2,
        ),
        budget_limits=BudgetLimits(max_steps=48, max_retries=8, max_actions=128, timeout_seconds=300),
    )


def staging_profile() -> RuntimeProfile:
    return RuntimeProfile(
        name="staging", browser_enabled=False, network_enabled=True, shell_enabled=True,
        capacity=CapacityLimits(concurrent_sessions=32, concurrent_runs=4, concurrent_tools=8,
                                concurrent_subagents=4, token_budget=500_000, queue_depth=32,
                                cpu_cores=2, memory_bytes=2 * 1024**3, open_files=512,
                                subprocesses=16, browser_contexts=1, mcp_servers=4,
                                idle_reclaim_seconds=900),
        budget_limits=BudgetLimits(max_steps=36, max_retries=5, max_actions=64, timeout_seconds=180),
    )


def customer_profile() -> RuntimeProfile:
    return RuntimeProfile(
        name="customer",
        browser_enabled=False,
        network_enabled=False,
        shell_enabled=True,
        capacity=CapacityLimits(
            concurrent_sessions=64, concurrent_runs=8, concurrent_tools=16,
            concurrent_subagents=8, token_budget=1_000_000, queue_depth=64,
            cpu_cores=2, memory_bytes=2 * 1024**3, open_files=512,
            subprocesses=16, browser_contexts=1, mcp_servers=4,
            idle_reclaim_seconds=900,
        ),
        budget_limits=BudgetLimits(max_steps=24, max_retries=3, max_actions=32, timeout_seconds=120),
    )
