# External Platform Integration

An integrating platform should implement the following thin Host Adapter responsibilities:

1. Allocate a session workspace and issue tenant/user/session identity, owner, epoch and lease.
2. Start and supervise the Harness process, negotiate protocol v1 and route JSONL frames.
3. Deliver user input and versioned controls without interpreting model prose.
4. Resolve `delegation.requested` by allocating child workspace, identity and budget, then send
   `delegation.resolve`.
5. Persist or relay public events and map them to the platform's client protocol.
6. Renew leases, fence stale owners, stop dead processes and perform takeover with a newer epoch.
7. Keep provider credentials in the platform secret/config boundary.

The platform must not create a planner, parse assistant output to decide tools, duplicate todo state,
or create child Agents directly. Child creation is a Hermes operation after Host allocation.

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
