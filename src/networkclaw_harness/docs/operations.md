# Operations And Security

## Deployment

Run the CPython 3.12 headless launcher under a supervisor. The platform should assign one process
to one Host Adapter owner; a process can carry multiple explicitly opened sessions. Mount only the
assigned workspaces and a bounded temporary directory. Use a read-only root filesystem, dropped
Linux capabilities, `no-new-privileges`, resource limits and an explicit provider egress allowlist.

The process must be stopped when its host owner disappears. A replacement process can resume a
session only after the platform grants a newer valid epoch and lease. Shared storage is not a
distributed lock.

## Provider boundary

The host selects an authorized provider/model/config reference. `HermesHostAdapter` validates the
route and reads credentials from the process environment or secret manager. It rejects unauthorized
config references, missing credentials, invalid endpoints and customer egress without an allowlist.
Credentials, prompts, raw requests and raw responses are never written to protocol frames, durable
facts or release evidence.

## Tools and side effects

Every NetworkClaw tool is registered with a schema, profile, side-effect class, workspace scope,
timeout, output bound and audit identity. Workspace tools operate only on the host-supplied root.
Unknown side-effect results are fail-closed and are not automatically replayed. Recovery must query
the external system or obtain a new host/user decision.

Production profiles do not install, modify or publish tools and skills at runtime. New capabilities
enter through source review, tests, locked dependencies, vendor/release metadata and a new artifact.

## Recovery

The platform is authoritative for durable ownership and takeover. The Harness persists Hermes
SessionDB state and bounded semantic/audit metadata in the session workspace. On restart, the host
reopens the workspace with the valid epoch and decides whether the prior turn completed, was
cancelled, is waiting, is retryable or has an unknown side effect. The Harness never silently
switches to another runtime or replays a side effect because a response was lost.

## Verification

Use the repository test entry point, not bare pytest:

```bash
scripts/run_tests.sh
python3.12 scripts/verify-hermes-vendor.py
python3.12 scripts/check-hermes-runtime-closure.py
python3.12 scripts/check-doc-links.py
git diff --check
```

Offline releases require CPython 3.12, locked dependencies, a generated vendor snapshot, SBOM,
license notices, a signed source/release manifest and a verified image identity. Online installation
and runtime downloads are not customer paths.
