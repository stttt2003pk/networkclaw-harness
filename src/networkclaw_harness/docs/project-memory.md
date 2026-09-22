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
- The Host Adapter authoritatively maps each session to its owner, execution epoch, explicit
  workspace, assigned Harness process and transport connection. It owns open/resume and control
  routing, process lifecycle, disconnect/drain/replacement, stale-owner fencing, and forwarding the
  complete host grant.
- The Host Adapter must not create or cache Python Agents, implement the Hermes conversation loop,
  retry model/tool execution, own plan/todo/iteration or transcript state, or schedule Hermes
  delegation.
- `HermesHostAdapter` owns session binding, NetworkClaw policy/tool boundaries, public event
  projection and native Hermes lifecycle.
- Hermes `AIAgent` owns the Agent loop, model calls, context, planning, todo, tools and subagents.

## Session, Agent and Turn lifecycle

The lifecycle model follows Hermes' durable-session design and is an explicit project invariant:

```ini
Session = durable identity + transcript + workspace + session metadata

AIAgent = cached, evictable execution object bound to one SessionBinding

Turn = one call to the cached agent's native `run_conversation()`
```

- One session can carry many sequential turns, but at most one active turn exists at a time.
- The first turn creates a native Hermes `AIAgent`; later sequential turns reuse it when the
  provider route, tool signature and profile are unchanged.
- Agent eviction releases only the cache entry. SessionDB, transcript, workspace and todo state
  remain authoritative and the next turn rebuilds the agent from them.
- `session_id` identifies durable state, not an in-memory object. Transcript, system-prompt/tool
  surface state, todo state, compression lineage and other cross-turn state must be hydrated from
  and persisted to the session-owned stores.
- `HermesHostAdapter` owns the durable `SessionBinding` boundary: workspace, lease/epoch fencing,
  SessionDB/history, policy, event projection and the factory/lifecycle for cached execution agents.
- Agent instances may be recreated after eviction, process restart, recovery or routing/profile/tool
  change. Provider clients and other resources may be cached only with explicit ownership/cleanup;
  no cache is the source of truth for session state.
- Turn concurrency, cancellation, steering, compression and recovery are fenced by
  `session_id + execution_epoch + turn_id + generation`; the admission gate is held only while
  reserving the turn, never across the Hermes execution itself.
- A delegated child is represented by a distinct host-allocated child session with its own
  workspace, SessionDB, `SessionBinding` and cached `AIAgent`. Explicit lineage links it to the
  parent; neither side directly owns or mutates the other's transcript.

This deliberately prevents process-level agent lifetime from becoming session debt and preserves
Hermes' ability to vary model/provider/tool behavior per execution while retaining one durable
logical conversation.

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
