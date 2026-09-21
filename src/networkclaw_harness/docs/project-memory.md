# NetworkClaw Harness Project Memory

## Mission

The project delivers a headless Harness engine, similar in role to a dedicated server. It extracts
the execution core of a pinned Hermes source version and lets an external distributed platform
start, supervise, fence, replace and recover it through a stable host contract.

The target chain is fixed:

```text
External distributed platform -> Host Adapter -> HermesHostAdapter -> Hermes AIAgent
```

## Ownership

- The external platform owns placement, capacity, identity, workspace allocation, durable authority,
  owner, execution epoch, lease, process supervision, failover and client transport.
- The Host Adapter translates that platform contract and must not implement planning, tool selection,
  todo state or Agent scheduling.
- `HermesHostAdapter` owns session binding, NetworkClaw policy/tool boundaries, public event
  projection and native Hermes lifecycle.
- Hermes `AIAgent` owns the Agent loop, model calls, context, planning, todo, tools and subagents.

## Non-distributed capabilities

UI, mutable global home state, online installation, unbounded browser/MCP access and capabilities
without clear ownership do not belong in this core. They are disabled, exposed through a separately
owned service/MCP, kept as injected test fixtures, or postponed until ownership and recovery are
defined.

The repository must never regain a self-built CoordinatorLoop or a deterministic production
fallback. Tests may use doubles, but only the native Hermes adapter is selectable by the launcher.

## Hermes and release discipline

`vendor/hermes` is generated, never hand-edited. Every update keeps the source commit, allowlist,
patch series, file hashes, license notices and offline closure. Customer profiles cannot install or
modify tools and skills at runtime. Secrets, prompts, chain-of-thought, raw provider payloads and
unrestricted tool output stay outside protocol frames and release evidence.
