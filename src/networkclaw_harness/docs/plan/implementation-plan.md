# Harness Implementation Plan

This is the only active implementation plan. Earlier numbered plans were written while the project
was still designing a self-owned CoordinatorLoop. That work diverged from the corrected architecture
and has been removed from both code and documentation. The history is summarized below so the reset
is auditable; those tasks are not carried forward.

## Target architecture

```text
External distributed platform
  -> Host Adapter
  -> HermesHostAdapter
  -> vendored Hermes AIAgent
```

The external platform owns placement, identity, workspace, owner/epoch/lease, process lifecycle,
durable authority, client transport and failover. Harness owns the adapter boundary, policy-bound
tools, public event projection and the native Hermes session. Hermes owns planning, tool looping,
todo, context, provider calls and subagent execution.

## Completed reset

- Hermes source commit, runtime allowlist, patch series, vendor hashes, license metadata and offline
  runtime closure are captured and verified.
- `HermesHostAdapter` is the sole production runtime entry and binds one native `AIAgent` to each
  host-assigned session workspace and SessionDB.
- Host Protocol v1 covers negotiation, session open/resume/close, user input, lease updates, cancel,
  steer, delegation resolution, public events and shutdown.
- Hermes deltas, tool lifecycle, todo updates (`plan.updated`) and subagent callbacks are projected
  as bounded events. Prompts, chain-of-thought, credentials and raw provider payloads remain private.
- Host-controlled delegation requests allocation before child workspace, identity, lease and budget
  creation. Parent/child cleanup and bounded result projection are covered by behavioral E2E tests.
- Native cancel/steer, workspace fencing, provider admission, tool policy, recovery records and
  process-level Host/provider E2E are implemented.
- The former `CoordinatorLoop`, reference planning/model types, old host adapter, old subagent
  manager, coordinator tool adapter and pseudo parity runner were deleted. Their tests and plans
  were deleted with them rather than preserved as an alternate architecture.

## Remaining work

### E-06 runtime event acceptance increment (2026-09-27)

- Managed patch `0002-host-controlled-foreground-dispatch.patch` keeps native delegation
  foreground when a host allocation callback is present; ordinary Hermes dispatch is unchanged.
  The runtime allowlist includes `agent/periodic_scheduler.py`, needed by real children.
  Managed sync now verifies 773 vendor files, including `agent/turn_liveness.py`
  for subsequent durable turns and `agent/auxiliary_wire.py` for actual compression calls.
- Adapter preserves child session identity and registers allocation/parent lineage before
  child callbacks. Integration captures actual native dual-child grant and deny events through
  UDS, Gateway, production gRPC/Lobby code and the web2 parser/reducer/render gate.
- Allocation timeout now emits `delegation.resolved` with deny/timeout reason and rejects
  late grants under the resolution lock, so the frontend does not retain a pending allocation.
  The real timeout ledger reaches the same transport/frontend gate without event loss.
- Verification: `scripts/run_tests.sh -q tests/test_delegation.py
  tests/test_host_delegation_dispatch.py tests/test_hermes_host_adapter.py` passed 40 tests;
  final resolution guard rechecked with 5 delegation tests and then the full
  `scripts/run_tests.sh -q` passed 264 tests plus vendor/runtime closure verification.
  Complete E-06 combination acceptance is still pending.
- Adapter now binds native compression `status_callback` to bounded
  `context.started`/`context.continued`, fences callbacks by turn generation and avoids
  duplicate result fallback. Every native turn loads its own SessionDB history, including
  immediate lease admission and Agent cache rebuilds; no second transcript store is added.
- Verification of this increment: `scripts/run_tests.sh -q tests/test_hermes_host_adapter.py`
  passed 33 tests plus vendor/runtime closure; the real Integration context scenario passed
  through UDS/Gateway/gRPC/Lobby with `go test -race` and matching compression versions.
- Real child failure/tool progress, clarification and usage already have combined ledgers.
  Frontend context/render confirmation and the Mac matrix passed with 11 ledgers.
  The external Go platform now saves child lineage in PostgreSQL run admissions;
  separate OS workers prove replay recovery, cross-tenant rejection and epoch fencing.
  Remaining: refreshed bundle/image after the durable-child increment and Ubuntu acceptance.

### 1. External platform contract

Define the production mapping from the platform's durable owner/epoch/lease and workspace records
to Host Protocol v1. The platform must implement CAS/fencing and never infer a workspace from cwd.

### 2. Host Adapter integration

Implement the thin adapter in the chosen distributed service (for example chatsvc): process spawn,
JSONL forwarding, bounded event mapping, health, drain, disconnect cleanup and child-PID ownership.
No planning, tool selection or Agent creation belongs in that service.

### 3. Production recovery

Exercise crash, SIGKILL, lease expiry, stale epoch, reconnect, provider stream interruption and
unknown side-effect reconciliation in the target deployment. A replacement Harness may resume only
with a newer valid platform lease/epoch and must not auto-replay unknown effects.

### 4. Production profiles and capacity

Finalize customer resource limits, provider egress, tool/profile allowlists, artifact quotas,
metrics and alerting. Keep runtime skill/tool mutation disabled and verify secrets never enter
protocol or evidence.

### 5. Offline release acceptance

Build and verify a clean CPython 3.12 source/wheelhouse/image release with SBOM, licenses,
signatures and vendor provenance. Run the release in the target air-gapped environment.

### 6. Platform rollout

Run a staged external-platform rollout using the minimal vertical link, then test multiple sessions,
cancel/steer, delegation, reconnect and failover. The rollout is complete when the platform can
operate the Harness without any legacy coordinator path.

## Exit criteria

The project is ready for platform adoption when all six remaining work items have evidence in the
platform's repository or release system, while this repository continues to pass its own tests,
vendor verification, runtime closure and documentation-link checks. A green local test suite alone
does not prove distributed failover or production deployment.
