# NetworkClaw Harness Development Guide

This repository is the customer-deliverable, headless Harness described by
`src/networkclaw_harness/docs/hermes-headless-harness.md`.

## Invariants

- Target CPython 3.12 only.
- Keep `src/networkclaw_harness` independent from the full Hermes checkout.
- Never hand-edit `vendor/hermes`; update it through `scripts/sync-hermes-runtime.py`.
- A vendor update must retain its source commit, allowlist, patch series and file hashes.
- Standard output from the headless launcher is JSONL protocol traffic only. Logs go to stderr.
- The host supplies every session workspace explicitly. Never infer it from cwd or user input.
- Do not expose prompts, chain-of-thought, secrets, raw debug logs or unrestricted tool output.
- Production profiles disable runtime creation, modification or installation of skills and tools.
- Unknown side-effect results are fail-closed and are not automatically replayed.
- Do not add online installation or runtime downloads to customer paths.

## Workflow

- Put NetworkClaw code under `src/networkclaw_harness`.
- Keep architecture documentation under `src/networkclaw_harness/docs` so source and binary
  releases remain self-contained.
- Put upstream selection metadata and patches under `upstream`.
- Run tests with `scripts/run_tests.sh`, never bare `pytest`.
- Keep tests behavioral; do not read source text to assert implementation shape.
- Add dependencies with lower and upper bounds, regenerate `poetry.lock` and
  `requirements.lock`, and update offline/SBOM metadata in the same change.
