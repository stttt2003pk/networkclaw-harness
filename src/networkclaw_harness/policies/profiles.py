"""Profiles keep mutable runtime capability explicit and auditable."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    name: str
    browser_enabled: bool
    network_enabled: bool
    shell_enabled: bool
    runtime_skill_install_enabled: bool = False
    runtime_tool_install_enabled: bool = False
    self_evolution_enabled: bool = False


def development_profile() -> RuntimeProfile:
    return RuntimeProfile(
        name="development",
        browser_enabled=True,
        network_enabled=True,
        shell_enabled=True,
    )


def customer_profile() -> RuntimeProfile:
    return RuntimeProfile(
        name="customer",
        browser_enabled=False,
        network_enabled=False,
        shell_enabled=True,
    )

