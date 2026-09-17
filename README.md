# NetworkClaw Headless Harness

NetworkClaw Harness is the customer-deliverable, headless session kernel that will host a
traceable Hermes runtime snapshot behind a versioned JSONL protocol. It is intentionally
separate from the full Hermes fork.

This repository is the evolution of the earlier coordinator semantic-runtime design: the
coordinator and agent loop move out of `chatsvc` into this Harness. In the target architecture,
`chatsvc` is the host adapter and lifecycle/transport boundary; the Harness is the session's
single decision-making kernel. The two components must not retain parallel coordinator loops.

The repository-owned architecture source is
[`src/networkclaw_harness/docs/hermes-headless-harness.md`](src/networkclaw_harness/docs/hermes-headless-harness.md).

This repository is currently at the **H0 bootstrap** stage:

- the Python 3.12 package and headless JSONL process exist;
- protocol envelopes, workspace binding and safe event projection have baseline tests;
- Hermes source provenance, allowlist sync and vendor hash verification are automated;
- the actual Hermes runtime allowlist and adapter are not yet accepted, so `user.input`
  fails explicitly with `runtime_unavailable` instead of pretending H1 is complete.

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

Run the headless process:

```bash
PYTHONPATH=src python -m networkclaw_harness.host
```

Each stdout line is one protocol event. Diagnostics are written to stderr.

## Vendor bootstrap

1. Point `upstream/hermes-source.json` at the approved full fork and commit.
2. Build `upstream/hermes-runtime-files.txt` through real import/resource tracing and
   capability tests.
3. Generate the snapshot:

```bash
python scripts/sync-hermes-runtime.py /path/to/networkclaw-hermes-fork
python scripts/verify-hermes-vendor.py
```

The sync script copies only allowlisted files, applies `upstream/patches/*.patch`, and writes
`upstream/hermes-vendor-manifest.json` with the source commit and post-patch SHA-256 hashes.
