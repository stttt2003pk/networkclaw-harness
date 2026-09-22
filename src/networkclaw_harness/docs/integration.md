# External Platform Integration

## Go Host Adapter boundary

An integrating service such as `chatsvc` implements a thin Go Host Adapter. For every assigned
session it owns the authoritative routing chain:

```text
session_id
  -> session owner
  -> execution epoch
  -> absolute workspace
  -> assigned Harness process and child PID
  -> transport connection
```

That binding is platform state, not Python Agent state. The Go adapter must:

1. Allocate or resolve the session workspace and issue tenant/user/session identity, owner, epoch
   and lease before sending `session.open` or `session.resume`.
2. Start and supervise the assigned Harness process, negotiate Host Protocol v1, and forward JSONL
   frames without interpreting model prose.
3. Route `user.input`, `turn.cancel`, `turn.steer`, lease updates and public event frames through the
   connection selected by the current session owner and execution epoch.
4. Forward all turn parameters and the complete bounded host grant to the Python Host Adapter.
5. Enforce platform CAS/fencing so one session cannot be executed by two owners; stale processes,
   connections and epochs must not route inputs or publish accepted events.
6. Own disconnect cleanup, drain, process replacement and child-PID lifecycle. Replacement may
   resume only with a newer valid epoch and lease against the same explicitly assigned workspace.
7. Resolve `delegation.requested` by allocating a distinct child session, workspace, identity,
   lease and budget, then send `delegation.resolve`.
8. Persist or relay bounded public events, map them to the client protocol, and keep provider
   credentials in the platform secret/config boundary.

The Go adapter must not create or cache Python `AIAgent` objects, implement the Hermes conversation
loop, retry model or tool execution, own plan/todo/iteration state, schedule Hermes delegation, or
act as the source of truth for the session transcript. Those responsibilities remain in
`HermesHostAdapter`, the session-owned stores and Hermes `AIAgent`. Child creation is a Hermes
operation only after host allocation.

## Minimal integration test

The first acceptance test should prove:

```text
negotiate
  -> open(session, workspace, owner, epoch, lease)
  -> input(provider route)
  -> assistant.delta
  -> optional tool/plan/delegation events
  -> turn.completed
  -> close
```

Then test stale epoch rejection, cancel during a provider stream, steer during an active turn,
parent cancellation of a delegated child, host disconnect cleanup and resume from the same workspace.
These are platform integration tests; they do not justify adding another runtime to this repository.
