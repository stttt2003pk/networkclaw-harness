# Deployment

The production process is the JSONL headless entry point in the root `Dockerfile`. The host
must mount and pass a session-specific absolute workspace, provide an execution epoch, and
ensure only one Harness process owns a session at a time. Kubernetes manifests wait until the
workspace ownership and chatrtmgr lease model is designed.

Production container launchers must apply all of the following controls:

- run as the image user `65532:65532` and set `no-new-privileges`;
- drop all Linux capabilities and use the runtime default seccomp profile;
- mount the root filesystem read-only;
- mount only the host-assigned session workspace and a bounded temporary directory writable;
- enforce the selected profile's CPU, memory, PID/subprocess, and `nofile` limits;
- keep network egress denied unless an explicit destination policy and deployment rule allow it.

The customer baseline is 2 CPU cores, 2 GiB memory, 512 file descriptors, and 16
subprocesses. Browser and MCP limits are additionally enforced inside the Harness. Prewarmed
containers remain unassigned until they are exclusively leased to one chatsvc; a running
Harness is never shared across chatsvc processes.

The release commands are:

```text
python scripts/prepare-offline-wheelhouse.py
python scripts/build-offline-release.py --signing-key <release-key.pem> --image-digest <sha256:...>
python scripts/verify-offline-release.py dist/release/<version> --require-publishable
python scripts/test-offline-release.py dist/release/<version>
```

The final two commands run without registry access once the offline package includes the
reviewed base-image OCI archive. A dirty-tree or ephemeral-key run is validation-only and is
rejected by `--require-publishable`.

For a live provider deployment, inject `OPENAI_API_KEY` through the deployment secret manager and
set only the host-owned references `OPENAI_BASE_URL`, `OPENAI_MODEL`,
`NETWORKCLAW_HARNESS_ALLOWED_MODELS`, `NETWORKCLAW_HARNESS_PROVIDER_MODE=live`, and bounded
timeout/retry settings. `chatsvc` sends `config_ref=env:openai`; it never sends the key or endpoint
through JSONL/protobuf. Set `NETWORKCLAW_HARNESS_PROVIDER_STREAM=true` only when the selected
endpoint's SSE contract has passed staging verification. Customer/offline profiles keep network
disabled unless a reviewed provider egress policy explicitly enables it.

The egress policy is an explicit hostname allowlist in
`NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS`; a customer profile refuses live provider traffic when
that list is absent. A config rotation changes `NETWORKCLAW_HARNESS_PROVIDER_CONFIG_VERSION` and
the secret-manager environment for new runs. Existing model requests use their copied environment
and finish or fail under the old configuration; they are never silently replayed against the new
endpoint. Harness metrics are queried over the existing chatsvc UDS HealthCheck path and relayed by
chatrtmgr, so no inbound Harness or chatsvc metrics port is required.
