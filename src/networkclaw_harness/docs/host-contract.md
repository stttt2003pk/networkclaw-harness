# Host Contract

The Host Adapter is the integration surface for any distributed platform. `chatsvc` is only one
possible host; the contract does not grant it agent-loop ownership.

## Process transport

The reference launcher uses stdin/stdout JSONL. stdout contains protocol frames only; diagnostics
go to stderr. A host may later map the same semantics to UDS, gRPC or another transport without
changing the Harness runtime boundary.

Every request has `protocol_version`, `type`, `request_id` and a JSON `payload`. Session commands
also carry explicit tenant, user and session identity. Turn controls carry the turn/run identity
and the current interaction/run version. Request IDs are idempotency keys: a conflicting reuse is
rejected.

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

## Controls

- `turn.cancel` requests interruption of the active native Hermes turn. Requested, acknowledged and
  terminal states remain distinct.
- `turn.steer` passes a bounded redirect to the active Hermes agent. A stale turn or run version is
  rejected and cannot affect a newer turn.
- `delegation.resolve` grants or denies a pending HostDelegationBroker request. A child Agent is
  not created before the host grants workspace, identity, lease and budget.
- `session.lease.update` changes the fence used by tools and child resources. A stale owner must
  stop producing model steps, tool side effects and durable writes.

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

The host may persist or relay events, but must not infer a new plan by parsing assistant text.
