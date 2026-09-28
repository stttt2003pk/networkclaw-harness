# Host Contract

The Host Adapter is the integration surface for any distributed platform. `chatsvc` is only one
possible host; the contract does not grant it agent-loop ownership.

## Process transport

The launcher accepts stdin/stdout JSONL by default. With `--socket-path /absolute/path.sock`, it
listens on a Unix domain socket and exchanges the same newline-delimited Host Protocol frames.
Each connection has its own `JsonlHost` session bindings; the owner must keep a session's commands
on its connection. A second connection can query health while the first is active. On disconnect,
that connection's sessions are fenced and their runtime bindings are released. `shutdown` closes
the listener and active connections. The socket has mode `0600`, and the launcher removes it on
normal shutdown or SIGTERM. An existing socket path is rejected rather than overwritten.

In stdin/stdout mode, stdout contains protocol frames only. In UDS mode, stdout remains empty.
Diagnostics go to stderr in both modes.

Every request has `protocol_version`, `type`, `request_id` and a JSON `payload`. Session commands
also carry explicit tenant, user and session identity. Turn controls carry the turn/run identity
and the current interaction/run version. Request IDs are idempotency keys: a conflicting reuse is
rejected. The Harness stores a SHA-256 hash of the canonical request envelope; an identical reuse
replays the recorded response frames without executing Hermes again, while a different hash returns
`request_id_conflict`.

## Required command flow

```text
protocol.negotiate
health.query / capabilities.query
session.open or session.resume
user.input
  -> turn.started
  -> assistant.delta / tool.* / plan.updated / subagent.*
  -> turn.completed | turn.failed | turn.cancelled
session.close
shutdown
```

The host supplies `workspace_root`, `owner_id`, `execution_epoch` and lease data in `session.open`
or `session.resume`. The Harness rejects missing identity, stale lease, invalid epoch and workspace
escape. Provider credentials are never protocol fields; the host supplies an authorized config
reference and the process environment/secret manager resolves it.

When supplied, `payload.host_grant` is validated as platform-owned admission metadata: it must bind
the session, epoch and workspace and contain bounded turn budget, resource profile and allowed-tool
fields. Validation does not perform placement, quota arbitration or fair scheduling.

`session.open` and `session.resume` also accept bounded `agent_id` and `profile_id` configuration
identifiers. They select a session-local Agent cache entry; they never select a process or share
mutable Agents across sessions. Reopening the same epoch preserves workspace, runtime, tool session,
turn admission and event bridge. Changed Agent/profile identifiers, host resource profile, tool grant
or turn budget evict only that session's Agent. Provider route changes rebuild it on the next turn.
Configuration changes during an active turn are rejected. Omitted configuration fields on resume
retain the current configuration. Lease changes must use `session.lease.update`; same-epoch reopen
cannot reactivate a drained or fenced session. If a turn omits its tool selection, the Host grant's
allowed tools still apply.

These identifiers are host-selected configuration identities, not a Harness-side Agent catalog.
The host still supplies the resolved provider route and turn parameters.

The host routes a session only through the Harness process and connection selected by its current
owner and execution epoch. It forwards the complete turn parameters and host grant, and owns
process supervision, disconnect, drain, replacement and stale-owner fencing. It must not construct
or cache Python `AIAgent` objects, retry model/tool work, maintain plan/todo state, schedule Hermes
delegation, or treat its event relay as the authoritative session transcript.

## Controls

- `turn.cancel` requests interruption of the active native Hermes turn. Requested, acknowledged and
  terminal states remain distinct. A lease/epoch loss fences the binding, interrupts Hermes through
  its native interrupt surface, and permits only one terminal interrupted/cancelled outcome.
- `turn.steer` passes a bounded redirect to the active Hermes agent. A stale turn or run version is
  rejected and cannot affect a newer turn.
- `delegation.resolve` grants or denies a pending HostDelegationBroker request. A child Agent is
  not created before the host grants a distinct child session identity, workspace, lease and
  budget. The child session has its own SessionDB and `SessionBinding`, carries explicit parent
  lineage, and exchanges only bounded delegation inputs and results with the parent session.
- `session.lease.update` changes the fence used by tools and child resources. A stale owner must
  stop producing model steps, tool side effects and durable writes. Events from a fenced generation
  are dropped; a replacement epoch may resume the same durable session.

Unknown commands that could change execution or authorization fail closed. Presentation-only fields
may be ignored by a compatible host, but control semantics require an understood protocol version.

## Public event rules

Events are request-scoped and sequence-numbered. The adapter emits bounded content and metadata:

| Event | Meaning |
| --- | --- |
| `turn.started` | native Hermes turn admitted |
| `assistant.delta` | bounded assistant stream fragment |
| `tool.started` / `tool.progress` / `tool.completed` | NetworkClaw tool lifecycle |
| `plan.updated` | projection of Hermes `todo_list`, never a second planner |
| `subagent.started` / `subagent.completed` | native delegation lifecycle projection |
| `delegation.requested` | host allocation request before child creation |
| `turn.completed` / `turn.failed` / `turn.cancelled` | exactly one terminal outcome |

The three turn terminal events are themselves the request-ending frame (`end=true`); a second generic
`end` frame is not emitted. Their payload always contains `outcome`, `reason_code`,
`execution_epoch`, `generation`, and `end=true`. `outcome` is respectively `completed`, `failed`, or
`cancelled`. The host accepts only the first terminal for projection. Later terminal, delta, tool, or
plan events are stale audit input and cannot be projected to the client. Unknown provider or tool
side effects remain failed/unknown and are never automatically replayed.

The host may persist or relay events, but must not infer a new plan by parsing assistant text.
