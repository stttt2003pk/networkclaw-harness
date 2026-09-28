# NetworkClaw Headless Harness

NetworkClaw Harness is the customer-deliverable, headless session kernel that will host a
traceable Hermes runtime snapshot behind a versioned JSONL protocol. It is intentionally
separate from the full Hermes fork.

This repository is the extracted Hermes Agent kernel behind a host-owned boundary. In the target
architecture, an external distributed platform supplies identity, workspace, lease and lifecycle;
`chatsvc` is one possible transport host; this Harness is the session's single decision-making
runtime. The host and Harness must not maintain a second planning or agent loop.

The architecture and implementation source is
[`src/networkclaw_harness/docs/architecture.md`](src/networkclaw_harness/docs/architecture.md),
with the only active plan in
[`src/networkclaw_harness/docs/plan/implementation-plan.md`](src/networkclaw_harness/docs/plan/implementation-plan.md).

The repository is currently in a **Hermes core integration baseline**:

- the Python 3.12 package and headless JSONL process exist;
- protocol envelopes, workspace binding and safe event projection have baseline tests;
- Hermes source provenance, allowlist sync and vendor hash verification are automated;
- the Hermes runtime snapshot and the NetworkClaw protocol/workspace layers exist;
- the production CLI has one runtime entry: the native Hermes adapter;
- legacy reference runtimes and the former self-built coordinator are not shipped or selectable.

The native probe and adapter acceptance establish that Hermes' `AIAgent.run_conversation()` runs
inside a host-assigned workspace with Harness callbacks and tool policy. Distributed placement,
lease authority and production failover remain responsibilities of the integrating platform.

## Layout

```text
src/networkclaw_harness/   NetworkClaw-owned host, protocol, workspace and policy code
  docs/                    architecture documents shipped with source and wheel releases
vendor/hermes/             generated Hermes runtime snapshot; never hand-edited
upstream/                  pinned source, allowlist and patch series
scripts/                   vendor, verification, test and offline-release tooling
offline/                   lock/wheel/SBOM metadata
deploy/                    deployment notes and manifests
tests/                     protocol and Harness regression tests
```

## Development

Use CPython 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
scripts/run_tests.sh
```

Run the headless process over stdin/stdout JSONL:

```bash
PYTHONPATH=src python -m networkclaw_harness.host
```

Each stdout line is one protocol event. Diagnostics are written to stderr.

Run the Gateway over UDS + JSONL for a local process owner:

```bash
PYTHONPATH=src python -m networkclaw_harness.host --socket-path /tmp/networkclaw-gateway.sock
```

The owner supplies an absolute socket path and connects with newline-delimited Host Protocol
frames. The Gateway keeps stdout empty in UDS mode, writes diagnostics to stderr, and removes the
socket on shutdown or SIGTERM. Keep each session's commands on the connection that opened it.

## Vendor bootstrap

1. Use the Hermes source commit pinned in `upstream/hermes-source.json`; it is retained in
   this repository's Git ancestry.
2. Create a temporary clean worktree/archive at that commit, then build
   `upstream/hermes-runtime-files.txt` through real import/resource tracing and
   capability tests.
3. Generate the snapshot:

```bash
python scripts/sync-hermes-runtime.py /path/to/pinned-hermes-worktree
python scripts/verify-hermes-vendor.py
```

The sync script copies only allowlisted files, applies `upstream/patches/*.patch`, and writes
`upstream/hermes-vendor-manifest.json` with the source commit and post-patch SHA-256 hashes.
The source path is explicit so the snapshot can be reviewed and reproduced offline. Vendor updates
are release inputs, not runtime downloads.
