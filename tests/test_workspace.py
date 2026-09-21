import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from networkclaw_harness.workspace import (
    REQUIRED_DIRECTORIES,
    ArtifactMetadataStore,
    ArtifactService,
    CheckpointStore,
    EpochError,
    EpochGuard,
    LeasePolicy,
    QuotaExceeded,
    QuotaLedger,
    QuotaPolicy,
    ReferenceLeaseAuthority,
    SessionWorkspace,
    WorkspaceError,
    WorkspaceArtifactMetadataStore,
)


DEFAULT_QUOTA = QuotaPolicy(
    tenant_bytes=1024 * 1024, session_bytes=512 * 1024,
    session_files=100, single_file_bytes=256 * 1024, tmp_bytes=64 * 1024,
)


def lease_fixture(session_id="session-1", owner_id="owner-a", *, clock=None):
    authority = ReferenceLeaseAuthority(clock=clock)
    record = authority.acquire(
        session_id=session_id, owner_id=owner_id,
        policy=LeasePolicy(ttl_ms=60000, renew_interval_ms=30000, grace_ms=10000),
        expected_lease_version=0, expected_execution_epoch=0,
    )
    return authority, EpochGuard(authority), record, authority.token(record)


def artifact_fixture(tmp_path: Path, *, session_id="session-1", tenant_id="tenant-1",
                     owner_id="owner-a", ledger=None, quota=DEFAULT_QUOTA):
    authority, guard, record, token = lease_fixture(session_id, owner_id)
    workspace = SessionWorkspace.open(tmp_path, tenant_id=tenant_id, session_id=session_id)
    metadata = ArtifactMetadataStore(guard)
    service = ArtifactService(workspace, guard, metadata, ledger or QuotaLedger(), quota)
    return authority, guard, record, token, workspace, metadata, service


def test_workspace_binds_identity_layout_and_rejects_escape(tmp_path: Path):
    root = tmp_path / "tenant" / "session"
    workspace = SessionWorkspace.open(root, tenant_id="tenant-1", session_id="session-1")
    assert all((workspace.root / relative).is_dir() for relative in REQUIRED_DIRECTORIES)
    identity = json.loads(workspace.identity_path.read_text())
    assert identity | {} == identity
    assert identity["format_version"] == 1
    assert identity["tenant_id"] == "tenant-1"
    assert identity["session_id"] == "session-1"
    with pytest.raises(WorkspaceError, match="cannot contain"):
        workspace.path_for("../other-session/state.json")
    with pytest.raises(WorkspaceError, match="does not match"):
        SessionWorkspace.open(root, tenant_id="tenant-1", session_id="session-2")


def test_workspace_rejects_root_and_child_symlinks(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="root cannot"):
        SessionWorkspace.open(root_link, tenant_id="tenant", session_id="session")

    assigned = tmp_path / "assigned"
    workspace = SessionWorkspace.open(assigned, tenant_id="tenant", session_id="session")
    (assigned / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="symbolic link"):
        workspace.path_for("escape/result.json")


def test_concurrent_workspace_binding_cannot_change_owner_identity(tmp_path: Path):
    root = tmp_path / "shared"
    barrier = threading.Barrier(2)

    def bind(session_id):
        barrier.wait()
        try:
            return SessionWorkspace.open(root, tenant_id="tenant", session_id=session_id).session_id
        except WorkspaceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, ("session-a", "session-b")))
    assert len([result for result in results if result.startswith("session-")]) == 1
    assert "workspace_identity_mismatch" in results


def test_epoch_cas_renewal_and_a_to_b_to_a_fencing():
    now = [datetime(2026, 9, 17, tzinfo=timezone.utc)]
    authority, guard, a1, token_a1 = lease_fixture(clock=lambda: now[0])
    a2 = authority.renew(token_a1)
    token_a2 = authority.token(a2)
    with pytest.raises(EpochError, match="stale"):
        guard.check(token_a1)

    b = authority.acquire(
        session_id="session-1", owner_id="owner-b", policy=a2.policy,
        expected_lease_version=a2.lease_version,
        expected_execution_epoch=a2.execution_epoch,
    )
    assert b.execution_epoch == 2
    with pytest.raises(EpochError, match="stale"):
        guard.check(token_a2)

    a3 = authority.acquire(
        session_id="session-1", owner_id="owner-a", policy=b.policy,
        expected_lease_version=b.lease_version,
        expected_execution_epoch=b.execution_epoch,
    )
    assert a3.execution_epoch == 3
    invoked = []
    with pytest.raises(EpochError):
        guard.run(authority.token(b), lambda: invoked.append(True))
    assert invoked == []

    with pytest.raises(EpochError, match="changed"):
        authority.acquire(
            session_id="session-1", owner_id="owner-b", policy=b.policy,
            expected_lease_version=b.lease_version,
            expected_execution_epoch=b.execution_epoch,
        )


def test_expired_or_invalidated_lease_fails_closed():
    now = [datetime(2026, 9, 17, tzinfo=timezone.utc)]
    authority, guard, record, token = lease_fixture(clock=lambda: now[0])
    now[0] += timedelta(seconds=61)
    with pytest.raises(EpochError, match="expired"):
        guard.check(token)

    authority, guard, record, token = lease_fixture()
    authority.invalidate(token)
    with pytest.raises(EpochError, match="invalid"):
        guard.check(token)


def test_checkpoint_is_atomic_and_requires_durable_identity(tmp_path: Path, monkeypatch):
    authority, guard, record, token = lease_fixture()
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant-1", session_id="session-1")
    store = CheckpointStore(workspace, guard)
    checkpoint = store.write(
        token, durable_cursor="41", hermes_snapshot_hash="a" * 64,
        artifact_manifest_hash="b" * 64, state={"step": 1},
    )
    assert store.load(durable_cursor="41", owner_id="owner-a", execution_epoch=1) == checkpoint
    with pytest.raises(WorkspaceError) as stale:
        store.load(durable_cursor="42", owner_id="owner-a", execution_epoch=1)
    assert stale.value.code == "checkpoint_stale"

    import networkclaw_harness.workspace.atomic as atomic_module
    monkeypatch.setattr(atomic_module.os, "replace", lambda source, target: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError, match="crash"):
        store.write(token, durable_cursor="42", hermes_snapshot_hash="c" * 64,
                    artifact_manifest_hash="d" * 64, state={"step": 2})
    assert json.loads(store.path.read_text())["durable_cursor"] == "41"


def test_large_artifact_metadata_index_summary_and_partial_read(tmp_path: Path):
    _, _, _, token, workspace, metadata, service = artifact_fixture(tmp_path / "workspace")
    content = (b"0123456789abcdef" * 2048)
    view = service.store_tool_output(
        token, artifact_id="artifact/large", content=content, mime_type="text/plain",
        source={"tool": "fixture", "invocation_id": "inv-1"}, summary="large result",
    )
    record = metadata.get("artifact/large")
    assert record is not None and record.size == len(content)
    assert view.inline_text is None
    assert len(view.excerpt) <= service.excerpt_limit
    assert service.read_range(token, "artifact/large", offset=10, length=25) == content[10:35]
    assert service.index.query(field="source", value="inv-1")[0]["artifact_id"] == "artifact/large"
    summary = service.summaries.get("artifact/large", source_sha256=record.sha256, generator_version="host-v1")
    assert summary is not None and summary.summary == "large result"
    assert service.summaries.get("artifact/large", source_sha256="wrong", generator_version="host-v1") is None
    assert len(service.manifest_hash()) == 64
    content_files = [path for path in (workspace.root / "artifacts/raw").glob("*/*") if path.is_file()]
    assert len(content_files) == 1


def test_workspace_artifact_metadata_survives_reopen(tmp_path: Path):
    _, guard, _, token = lease_fixture()
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant-1", session_id="session-1")
    metadata = WorkspaceArtifactMetadataStore(workspace, guard)
    service = ArtifactService(workspace, guard, metadata, QuotaLedger(), DEFAULT_QUOTA)
    stored = service.write(
        token, artifact_id="artifact-1", category="raw", chunks=(b"persistent output",),
        mime_type="text/plain", source={"tool": "host_bash"},
    )

    reopened = WorkspaceArtifactMetadataStore(workspace, guard)
    assert reopened.get("artifact-1") == stored
    assert reopened.list_session("tenant-1", "session-1") == [stored]


def test_concurrent_content_writes_are_atomic_and_deduplicated(tmp_path: Path):
    _, _, _, token, workspace, metadata, service = artifact_fixture(tmp_path / "workspace")
    content = b"concurrent content" * 1000
    barrier = threading.Barrier(2)

    def write(artifact_id):
        barrier.wait()
        return service.write(token, artifact_id=artifact_id, category="evidence",
                             chunks=(content,), mime_type="application/octet-stream",
                             source={"fixture": "concurrent"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(write, ("artifact-a", "artifact-b")))
    assert records[0].sha256 == records[1].sha256
    files = [path for path in (workspace.root / "artifacts/evidence").glob("*/*") if path.is_file()]
    assert len(files) == 1 and files[0].read_bytes() == content
    assert service.ledger.usage("tenant-1", "session-1")["artifact_files"] == 1
    assert len(metadata.list_session("tenant-1", "session-1")) == 2


def test_stale_callback_cannot_commit_artifact_or_checkpoint(tmp_path: Path):
    authority, guard, record, token, workspace, metadata, service = artifact_fixture(tmp_path / "workspace")
    checkpoint = CheckpointStore(workspace, guard)
    successor = authority.acquire(
        session_id="session-1", owner_id="owner-b", policy=record.policy,
        expected_lease_version=record.lease_version,
        expected_execution_epoch=record.execution_epoch,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.write, token, artifact_id="late", category="raw", chunks=(b"late",),
            mime_type="text/plain", source={"callback": "late"},
        )
        with pytest.raises(EpochError):
            future.result()
    with pytest.raises(EpochError):
        checkpoint.write(token, durable_cursor="1", hermes_snapshot_hash="a" * 64,
                         artifact_manifest_hash="b" * 64, state={})
    assert metadata.get("late") is None
    assert guard.check(authority.token(successor)).owner_id == "owner-b"


def test_quota_errors_are_structured_across_session_and_tenant(tmp_path: Path):
    quota = QuotaPolicy(tenant_bytes=10, session_bytes=8, session_files=2, single_file_bytes=8, tmp_bytes=4)
    ledger = QuotaLedger()
    *_, token, workspace, metadata, service = artifact_fixture(tmp_path / "s1", ledger=ledger, quota=quota)
    service.write(token, artifact_id="first", category="raw", chunks=(b"123456",),
                  mime_type="text/plain", source={"fixture": "quota"})
    with pytest.raises(QuotaExceeded) as exceeded:
        service.write(token, artifact_id="second", category="raw", chunks=(b"abc",),
                      mime_type="text/plain", source={"fixture": "quota"})
    diagnostic = exceeded.value.to_mapping()
    assert diagnostic["code"] == "quota_exceeded"
    assert diagnostic["dimension"] == "session_bytes"
    with pytest.raises(QuotaExceeded) as tmp_exceeded:
        service.write_tmp(token, "scratch.bin", b"12345")
    assert tmp_exceeded.value.dimension == "tmp_bytes"

    authority2, guard2, record2, token2 = lease_fixture("session-2", "owner-b")
    workspace2 = SessionWorkspace.open(tmp_path / "s2", tenant_id="tenant-1", session_id="session-2")
    service2 = ArtifactService(workspace2, guard2, ArtifactMetadataStore(guard2), ledger, quota)
    with pytest.raises(QuotaExceeded) as tenant_exceeded:
        service2.write(token2, artifact_id="tenant-over", category="raw", chunks=(b"12345",),
                       mime_type="text/plain", source={"fixture": "quota"})
    assert tenant_exceeded.value.dimension == "tenant_bytes"


def test_cleanup_removes_only_tmp_or_explicitly_expired_unreferenced_content(tmp_path: Path):
    _, _, _, token, workspace, metadata, service = artifact_fixture(tmp_path / "workspace")
    tmp = service.write_tmp(token, "nested/scratch", b"temporary")
    assert tmp.exists() and service.cleanup_tmp(token) == 1 and not tmp.exists()
    retained = service.write(token, artifact_id="retained", category="generated", chunks=(b"keep",),
                             mime_type="text/plain", source={"fixture": "cleanup"})
    referenced = service.write(token, artifact_id="referenced", category="generated", chunks=(b"referenced",),
                               mime_type="text/plain", source={"fixture": "cleanup"}, references=("run-1",))
    metadata.expire(token, "retained")
    metadata.expire(token, "referenced")
    assert service.cleanup_expired(token) == ["retained"]
    assert not metadata.get("retained").content_available
    assert metadata.get("referenced").content_available
    assert service.read_range(token, "referenced", offset=0, length=10) == b"referenced"
    with pytest.raises(WorkspaceError) as unavailable:
        service.read_range(token, "retained", offset=0, length=4)
    assert unavailable.value.code == "artifact_content_unavailable"
