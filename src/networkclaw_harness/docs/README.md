# NetworkClaw Harness Documentation

These documents describe the corrected architecture and are shipped with the Harness source.

## Architecture

- [Architecture](./architecture.md): ownership, runtime boundary and session lifecycle.
- [Host Contract](./host-contract.md): JSONL commands, identity, controls and public events.
- [Capabilities](./capabilities.md): what is reused from Hermes, what NetworkClaw adapts and what
  remains outside the core.
- [Operations and Security](./operations.md): deployment, provider secrets, side effects, recovery
  and offline verification.
- [External Platform Integration](./integration.md): responsibilities and acceptance flow for a
  distributed host such as chatsvc.
- [Project Memory](./project-memory.md): durable project mission and design constraints.

## One active plan

- [Implementation Plan](./plan/implementation-plan.md): the only active plan. It records the reset,
  removes the old self-built CoordinatorLoop work, and lists the remaining external-platform tasks.

The architecture is always:

```text
External distributed platform -> Host Adapter -> HermesHostAdapter -> Hermes AIAgent
```

No document in this directory defines a second coordinator, planner or production runtime.
