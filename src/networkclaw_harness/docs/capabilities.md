# Capability Boundary

| Capability | Current decision |
| --- | --- |
| Hermes agent loop, provider calls, tool rounds, context, session persistence | Reuse pinned Hermes runtime through `HermesHostAdapter` |
| Workspace, lease/epoch fencing, tool policy, audit and public events | NetworkClaw boundary code |
| Todo and planning | Hermes-native `todo_list`, projected as `plan.updated` |
| Subagents | Hermes-native delegation, Host-controlled allocation and bounded projection |
| Cancel and steer | Native Hermes control surface via Host Adapter |
| UI, TUI, desktop rendering | Outside the Harness |
| Distributed placement, durable authority and failover | External platform |
| Runtime tool/skill installation or mutation | Disabled |
| Unbounded browser/MCP/side-effect integrations | Separate reviewed companion/service or postponed |

The goal is not to copy a percentage of Hermes source. The goal is to preserve the headless Agent
core while keeping distributed ownership, security and recovery explicit. Any capability that cannot
meet those contracts must remain outside the core until its owner and failure semantics are defined.
