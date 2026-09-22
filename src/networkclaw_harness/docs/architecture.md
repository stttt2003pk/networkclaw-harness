# Harness Architecture

## Scope

NetworkClaw Harness is a headless, customer-deliverable execution engine. It extracts the
headless runtime from a pinned Hermes source commit and exposes it through a small host contract.
It is not a distributed scheduler, a chat service, a UI, or a replacement for the platform's
durable authority.

The only supported execution chain is:

```text
External distributed platform
        |
        v
Host Adapter
        |
        v
HermesHostAdapter
        |
        v
Hermes AIAgent
```

The external platform owns placement, process supervision, tenant and user identity, session
ownership, workspace allocation, execution epoch, lease renewal, failover and client transport.
The Host Adapter translates that platform contract to JSONL protocol frames and translates bounded
Harness events back to the platform. `HermesHostAdapter` owns the durable session binding and an
evictable cache of agents. For each `session_id`, the external Host Adapter authoritatively maps the
current owner and execution epoch to one explicit workspace, assigned Harness process and transport
connection. It owns open/resume routing, controls, disconnect, drain, replacement and CAS fencing,
and forwards the complete turn parameters and host grant. It never creates Python Agents or owns
conversation, retry, plan/todo/iteration, delegation scheduling or transcript semantics. The
vendored Hermes `AIAgent` owns the model/tool/plan/todo/subagent loop.

CapacityLedger-style quota, fair scheduling and placement are platform responsibilities. The
Harness accepts a bounded host grant (`session_id`, `execution_epoch`, workspace, turn budget,
resource profile, allowed tools and lease), validates that it matches the opened binding, and
enforces only local hard safety limits. It does not choose placement or implement tenant fairness.

There is no NetworkClaw `CoordinatorLoop`, reference planner, deterministic production runtime or
second planning engine. Tests may inject provider and agent doubles, but those doubles are not
runtime modes and are never selected by the CLI.

## Ownership boundary

| Concern | External platform / host | Harness / Hermes |
| --- | --- | --- |
| Placement and capacity | authoritative | reports bounded resource use |
| Session owner, epoch and lease | authoritative | validates every session-bound operation |
| Workspace root | allocates and supplies explicitly | confines tools and SessionDB to it |
| Provider credentials | secret manager and config reference | consumes an authorized route without exposing secrets |
| Agent loop and planning | never reimplements | Hermes `AIAgent` |
| Tool policy and audit | supplies profile and lease | NetworkClaw policy/tool adapters |
| Cancel and steer | sends versioned controls | calls Hermes interrupt/redirect/steer |
| Child-agent allocation | grants workspace, identity and budget | requests through the host delegation broker |
| Client presentation | maps public events to clients | emits bounded protocol events |
| Recovery authority | durable owner and CAS/fencing | persists local state and refuses stale writes |

## Runtime contents

`vendor/hermes/` is generated from the pinned source commit by the sync script. Its source commit,
allowlist, patch series, file hashes, license notices and import closure are release inputs. It is
not edited by hand and is not downloaded at runtime.

NetworkClaw code under `src/networkclaw_harness` supplies only the boundary around Hermes:

- protocol and JSONL framing;
- explicit session workspace and lease/epoch validation;
- host-owned provider route admission;
- policy-bound NetworkClaw tools and bounded audit projection;
- public event projection for deltas, tools, todo plans and subagents;
- lifecycle, cancellation, recovery records and offline release checks.

Capabilities that require a UI, mutable global home, online installation, or unclear ownership are
disabled, externalized through a service/MCP, or postponed. They are not reimplemented as another
agent loop.

## Session lifecycle

The host sends `session.open` with tenant, user, session, absolute workspace root and lease. The
adapter creates one durable `SessionBinding` for that session. Each `user.input` acquires the
session turn-admission gate, gets or creates an `AIAgent` cache entry, and calls Hermes'
`run_conversation()` exactly once. Sequential turns may reuse that entry; eviction, restart, or a
provider/route/tool/profile change only releases the agent and rebuilds it from the same
SessionDB/workspace. `turn.steer`, `turn.cancel`, `delegation.resolve` and lease updates are
control-plane inputs; they never create a second loop. Closing or losing the host fences the
session, interrupts the native turn, and releases the binding and child resources.

Only one turn may be active for a session. Admission uses session/turn/generation and the current
execution epoch; it does not hold a lock while Hermes performs model or tool work. Session identity,
transcript, workspace and todo facts remain durable even when the cached agent is gone.

A delegated child never shares the parent's session identity, workspace, SessionDB or cached
`AIAgent`. The host allocates a distinct child session and records explicit parent/child lineage;
the Harness binds that grant to an independent `SessionBinding` before Hermes creates the child
agent. Parent and child state may interact only through the bounded delegation request/result
contract.

The Harness may host multiple isolated sessions in one process when the external platform assigns
them to that process. A session is never inferred from cwd, process-global state or user text.

## Event visibility

Only bounded public events cross the host boundary: `assistant.delta`, `turn.*`, `tool.*`,
`plan.updated`, `subagent.*`, `delegation.requested`, artifact references, warnings and health or
capability responses. Prompts, chain-of-thought, credentials, raw provider payloads and unrestricted
tool output never cross the boundary.
