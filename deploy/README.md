# Deployment

The production process is the JSONL headless entry point in the root `Dockerfile`. The host
must mount and pass a session-specific absolute workspace, provide an execution epoch, and
ensure only one Harness process owns a session at a time. Kubernetes manifests wait until the
workspace ownership and chatrtmgr lease model is designed.

