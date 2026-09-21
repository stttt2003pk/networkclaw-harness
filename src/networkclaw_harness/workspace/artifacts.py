"""Fenced artifact content, durable metadata, indexes, summaries, and quotas."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .atomic import atomic_write, fsync_directory
from .epoch import EpochGuard, FenceToken
from .session import SessionWorkspace, WorkspaceError

ARTIFACT_CATEGORIES = frozenset({"raw", "normalized", "evidence", "generated"})
ARTIFACT_LIFECYCLES = frozenset({"retained", "session", "expired"})


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    tenant_bytes: int
    session_bytes: int
    session_files: int
    single_file_bytes: int
    tmp_bytes: int

    def __post_init__(self) -> None:
        if min(self.tenant_bytes, self.session_bytes, self.session_files, self.single_file_bytes, self.tmp_bytes) <= 0:
            raise ValueError("all workspace quotas must be positive")


class QuotaExceeded(WorkspaceError):
    def __init__(self, *, dimension: str, usage: int, requested: int, limit: int) -> None:
        super().__init__(f"{dimension} quota exceeded", code="quota_exceeded")
        self.dimension = dimension
        self.usage = usage
        self.requested = requested
        self.limit = limit

    def to_mapping(self) -> dict[str, object]:
        return {
            "code": self.code, "dimension": self.dimension, "usage": self.usage,
            "requested": self.requested, "limit": self.limit,
            "remediation": "remove expired or rebuildable content, or request a larger profile quota",
        }


class QuotaLedger:
    """Process-local quota accounting port; production adapters persist it per tenant."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], tuple[int, int]] = {}
        self._tmp: dict[tuple[str, str], int] = {}
        self._lock = threading.RLock()

    def reserve_artifact(self, tenant_id: str, session_id: str, *, size: int, policy: QuotaPolicy) -> None:
        with self._lock:
            key = (tenant_id, session_id)
            session_bytes, session_files = self._sessions.get(key, (0, 0))
            tenant_bytes = sum(value[0] for identity, value in self._sessions.items() if identity[0] == tenant_id)
            checks = (
                ("single_file_bytes", 0, size, policy.single_file_bytes),
                ("session_bytes", session_bytes, size, policy.session_bytes),
                ("session_files", session_files, 1, policy.session_files),
                ("tenant_bytes", tenant_bytes, size, policy.tenant_bytes),
            )
            for dimension, usage, requested, limit in checks:
                if usage + requested > limit:
                    raise QuotaExceeded(dimension=dimension, usage=usage, requested=requested, limit=limit)
            self._sessions[key] = (session_bytes + size, session_files + 1)

    def register_session(self, tenant_id: str, session_id: str, root: Path) -> None:
        artifact_files = [path for path in (root / "artifacts").glob("*/*/*")
                          if path.is_file() and len(path.name) == 64]
        tmp_files = [path for path in (root / "tmp").rglob("*") if path.is_file() and not path.is_symlink()]
        with self._lock:
            self._sessions[(tenant_id, session_id)] = (
                sum(path.stat().st_size for path in artifact_files), len(artifact_files)
            )
            self._tmp[(tenant_id, session_id)] = sum(path.stat().st_size for path in tmp_files)

    def release_artifact(self, tenant_id: str, session_id: str, *, size: int) -> None:
        with self._lock:
            key = (tenant_id, session_id)
            current_bytes, current_files = self._sessions.get(key, (0, 0))
            self._sessions[key] = (max(0, current_bytes - size), max(0, current_files - 1))

    def set_tmp(self, tenant_id: str, session_id: str, *, previous: int, size: int, policy: QuotaPolicy) -> None:
        with self._lock:
            key = (tenant_id, session_id)
            usage = self._tmp.get(key, 0)
            updated = usage - previous + size
            if updated > policy.tmp_bytes:
                raise QuotaExceeded(dimension="tmp_bytes", usage=usage - previous, requested=size, limit=policy.tmp_bytes)
            self._tmp[key] = max(0, updated)

    def usage(self, tenant_id: str, session_id: str) -> dict[str, int]:
        with self._lock:
            content_bytes, files = self._sessions.get((tenant_id, session_id), (0, 0))
            return {"artifact_bytes": content_bytes, "artifact_files": files,
                    "tmp_bytes": self._tmp.get((tenant_id, session_id), 0)}


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    artifact_id: str
    tenant_id: str
    session_id: str
    category: str
    sha256: str
    size: int
    mime_type: str
    source: Mapping[str, str]
    references: tuple[str, ...]
    lifecycle: str
    created_at: str
    content_available: bool = True


class ArtifactMetadataPort(Protocol):
    def commit(self, token: FenceToken, metadata: ArtifactMetadata) -> ArtifactMetadata: ...
    def get(self, artifact_id: str) -> ArtifactMetadata | None: ...
    def list_session(self, tenant_id: str, session_id: str) -> list[ArtifactMetadata]: ...
    def expire(self, token: FenceToken, artifact_id: str) -> ArtifactMetadata: ...
    def mark_content_deleted(self, token: FenceToken, artifact_id: str) -> ArtifactMetadata: ...


class ArtifactMetadataStore:
    """Reference port for Lobby-owned artifact metadata and references."""

    def __init__(self, guard: EpochGuard) -> None:
        self._guard = guard
        self._records: dict[str, ArtifactMetadata] = {}
        self._lock = threading.RLock()

    def commit(self, token: FenceToken, metadata: ArtifactMetadata) -> ArtifactMetadata:
        self._guard.check(token)
        if metadata.session_id != token.session_id:
            raise WorkspaceError("artifact session identity does not match fence", code="artifact_identity_mismatch")
        with self._lock:
            current = self._records.get(metadata.artifact_id)
            if current is not None:
                comparable = ("tenant_id", "session_id", "category", "sha256", "size", "mime_type", "source", "references", "lifecycle")
                if any(getattr(current, field) != getattr(metadata, field) for field in comparable):
                    raise WorkspaceError("artifact_id already has different metadata", code="artifact_id_conflict")
                self._guard.check(token)
                return current
            self._guard.check(token)
            self._records[metadata.artifact_id] = metadata
            return metadata

    def get(self, artifact_id: str) -> ArtifactMetadata | None:
        with self._lock:
            return self._records.get(artifact_id)

    def list_session(self, tenant_id: str, session_id: str) -> list[ArtifactMetadata]:
        with self._lock:
            return [record for record in self._records.values()
                    if record.tenant_id == tenant_id and record.session_id == session_id]

    def expire(self, token: FenceToken, artifact_id: str) -> ArtifactMetadata:
        self._guard.check(token)
        with self._lock:
            current = self._records.get(artifact_id)
            if current is None:
                raise WorkspaceError("artifact metadata does not exist", code="artifact_not_found")
            if current.session_id != token.session_id:
                raise WorkspaceError("artifact belongs to another session", code="artifact_identity_mismatch")
            updated = replace(current, lifecycle="expired")
            self._guard.check(token)
            self._records[artifact_id] = updated
            return updated

    def mark_content_deleted(self, token: FenceToken, artifact_id: str) -> ArtifactMetadata:
        self._guard.check(token)
        with self._lock:
            current = self._records[artifact_id]
            if current.session_id != token.session_id:
                raise WorkspaceError("artifact belongs to another session", code="artifact_identity_mismatch")
            updated = replace(current, content_available=False)
            self._guard.check(token)
            self._records[artifact_id] = updated
            return updated


class WorkspaceArtifactMetadataStore(ArtifactMetadataStore):
    """Workspace-local durable implementation of the artifact metadata port."""

    def __init__(self, workspace: SessionWorkspace, guard: EpochGuard) -> None:
        super().__init__(guard)
        self._path = workspace.path_for("indexes/artifact-metadata.json")
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._records = {str(key): _metadata_from_mapping(value) for key, value in raw.items()}
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                raise WorkspaceError("artifact metadata is corrupt", code="artifact_metadata_corrupt") from error

    def commit(self, token: FenceToken, metadata: ArtifactMetadata) -> ArtifactMetadata:
        result = super().commit(token, metadata)
        self._persist()
        return result

    def expire(self, token: FenceToken, artifact_id: str) -> ArtifactMetadata:
        result = super().expire(token, artifact_id)
        self._persist()
        return result

    def mark_content_deleted(self, token: FenceToken, artifact_id: str) -> ArtifactMetadata:
        result = super().mark_content_deleted(token, artifact_id)
        self._persist()
        return result

    def _persist(self) -> None:
        atomic_write(self._path, json.dumps(
            {key: _metadata_mapping(value) for key, value in self._records.items()},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))


class ArtifactIndex:
    """Rebuildable workspace index; durable metadata remains authoritative."""

    def __init__(self, workspace: SessionWorkspace, guard: EpochGuard) -> None:
        self._workspace = workspace
        self._guard = guard
        self._path = workspace.path_for("indexes/artifacts.json")
        self._lock = threading.RLock()

    def rebuild(self, token: FenceToken, records: Iterable[ArtifactMetadata]) -> None:
        self._guard.check(token)
        payload = {record.artifact_id: _metadata_mapping(record) for record in records}
        atomic_write(self._path, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def upsert(self, token: FenceToken, metadata: ArtifactMetadata) -> None:
        self._guard.check(token)
        with self._lock:
            values = self._load()
            values[metadata.artifact_id] = _metadata_mapping(metadata)
            self._guard.check(token)
            atomic_write(self._path, json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def query(self, *, field: str, value: object) -> list[dict[str, Any]]:
        allowed = {"artifact_id", "category", "mime_type", "lifecycle", "sha256", "source", "references"}
        if field not in allowed:
            raise WorkspaceError("artifact index field is not queryable", code="invalid_index_query")
        results = []
        for record in self._load().values():
            candidate = record.get(field)
            if candidate == value or isinstance(candidate, (list, tuple)) and value in candidate or isinstance(candidate, dict) and value in candidate.values():
                results.append(record)
        return results

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError("artifact index is corrupt and must be rebuilt", code="index_corrupt") from error
        if not isinstance(value, dict):
            raise WorkspaceError("artifact index is corrupt and must be rebuilt", code="index_corrupt")
        return value


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    artifact_id: str
    source_sha256: str
    generator_version: str
    summary: str
    created_at: str


class SummaryStore:
    def __init__(self, workspace: SessionWorkspace, guard: EpochGuard) -> None:
        self._workspace = workspace
        self._guard = guard

    def put(self, token: FenceToken, record: SummaryRecord) -> None:
        self._guard.check(token)
        path = self._path(record.artifact_id)
        atomic_write(path, json.dumps(asdict(record), sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def get(self, artifact_id: str, *, source_sha256: str, generator_version: str) -> SummaryRecord | None:
        path = self._path(artifact_id)
        if not path.exists():
            return None
        try:
            record = SummaryRecord(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if (record.source_sha256, record.generator_version) != (source_sha256, generator_version):
            return None
        return record

    def _path(self, artifact_id: str) -> Path:
        name = hashlib.sha256(artifact_id.encode("ascii")).hexdigest()
        return self._workspace.path_for(f"summaries/{name}.json")


@dataclass(frozen=True, slots=True)
class ModelArtifactView:
    artifact_id: str
    summary: str
    excerpt: str
    inline_text: str | None


class ArtifactService:
    def __init__(self, workspace: SessionWorkspace, guard: EpochGuard,
                 metadata: ArtifactMetadataPort, ledger: QuotaLedger,
                 policy: QuotaPolicy, *, inline_limit: int = 16 * 1024,
                 excerpt_limit: int = 2048, partial_read_limit: int = 64 * 1024) -> None:
        self.workspace = workspace
        self.guard = guard
        self.metadata = metadata
        self.ledger = ledger
        self.policy = policy
        self.inline_limit = inline_limit
        self.excerpt_limit = excerpt_limit
        self.partial_read_limit = partial_read_limit
        self.index = ArtifactIndex(workspace, guard)
        self.summaries = SummaryStore(workspace, guard)
        self.ledger.register_session(workspace.tenant_id, workspace.session_id, workspace.root)

    def write(self, token: FenceToken, *, artifact_id: str, category: str,
              chunks: Iterable[bytes], mime_type: str, source: Mapping[str, str],
              references: Iterable[str] = (), lifecycle: str = "retained") -> ArtifactMetadata:
        self._validate_identity(token)
        if category not in ARTIFACT_CATEGORIES or not artifact_id or not artifact_id.isascii():
            raise WorkspaceError("artifact category or id is invalid", code="invalid_artifact")
        if lifecycle not in ARTIFACT_LIFECYCLES or not mime_type or not source:
            raise WorkspaceError("artifact lifecycle, MIME type, and source are required", code="invalid_artifact")
        category_root = self.workspace.path_for(f"artifacts/{category}", create_directory=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=category_root)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise WorkspaceError("artifact chunks must be bytes", code="invalid_artifact")
                    size += len(chunk)
                    if size > self.policy.single_file_bytes:
                        raise QuotaExceeded(dimension="single_file_bytes", usage=0, requested=size, limit=self.policy.single_file_bytes)
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            sha256 = digest.hexdigest()
            final_parent = self.workspace.path_for(f"artifacts/{category}/{sha256[:2]}", create_directory=True)
            final = self.workspace.path_for(f"artifacts/{category}/{sha256[:2]}/{sha256}")
            if final.exists():
                if final.stat().st_size != size or _hash_file(final) != sha256:
                    raise WorkspaceError("content-addressed artifact hash collision", code="artifact_corrupt")
            else:
                self.ledger.reserve_artifact(self.workspace.tenant_id, self.workspace.session_id, size=size, policy=self.policy)
                self.guard.check(token)
                try:
                    os.link(temporary, final)
                    fsync_directory(final_parent)
                except FileExistsError:
                    self.ledger.release_artifact(self.workspace.tenant_id, self.workspace.session_id, size=size)
            record = ArtifactMetadata(
                artifact_id=artifact_id, tenant_id=self.workspace.tenant_id,
                session_id=self.workspace.session_id, category=category, sha256=sha256,
                size=size, mime_type=mime_type, source=dict(source),
                references=tuple(references), lifecycle=lifecycle,
                created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            committed = self.metadata.commit(token, record)
            self.index.upsert(token, committed)
            return committed
        finally:
            temporary.unlink(missing_ok=True)

    def store_tool_output(self, token: FenceToken, *, artifact_id: str, content: bytes,
                          mime_type: str, source: Mapping[str, str], summary: str) -> ModelArtifactView:
        record = self.write(token, artifact_id=artifact_id, category="raw", chunks=(content,),
                            mime_type=mime_type, source=source)
        text = content.decode("utf-8", errors="replace")
        excerpt = text[:self.excerpt_limit]
        self.summaries.put(token, SummaryRecord(
            artifact_id=artifact_id, source_sha256=record.sha256,
            generator_version="host-v1", summary=summary,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        ))
        return ModelArtifactView(artifact_id, summary, excerpt, text if len(content) <= self.inline_limit else None)

    def read_range(self, token: FenceToken, artifact_id: str, *, offset: int, length: int) -> bytes:
        self._validate_identity(token)
        if offset < 0 or length < 0 or length > self.partial_read_limit:
            raise WorkspaceError("artifact range exceeds partial-read policy", code="invalid_artifact_range")
        record = self.metadata.get(artifact_id)
        if record is None or record.tenant_id != self.workspace.tenant_id or record.session_id != self.workspace.session_id:
            raise WorkspaceError("artifact does not belong to this session", code="artifact_not_found")
        if not record.content_available:
            raise WorkspaceError("artifact content is unavailable", code="artifact_content_unavailable")
        path = self._content_path(record)
        if not path.exists() or _hash_file(path) != record.sha256:
            raise WorkspaceError("artifact content is missing or corrupt", code="artifact_corrupt")
        self.guard.check(token)
        with path.open("rb") as stream:
            stream.seek(offset)
            return stream.read(length)

    def write_tmp(self, token: FenceToken, relative: str, content: bytes) -> Path:
        self._validate_identity(token)
        path = self.workspace.path_for(Path("tmp") / relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.stat().st_size if path.exists() else 0
        self.ledger.set_tmp(self.workspace.tenant_id, self.workspace.session_id,
                            previous=previous, size=len(content), policy=self.policy)
        self.guard.check(token)
        atomic_write(path, content)
        return path

    def cleanup_tmp(self, token: FenceToken) -> int:
        self._validate_identity(token)
        root = self.workspace.path_for("tmp")
        removed = 0
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                size = path.stat().st_size
                path.unlink()
                self.ledger.set_tmp(self.workspace.tenant_id, self.workspace.session_id,
                                    previous=size, size=0, policy=self.policy)
                removed += 1
        return removed

    def cleanup_expired(self, token: FenceToken) -> list[str]:
        self._validate_identity(token)
        removed = []
        records = self.metadata.list_session(self.workspace.tenant_id, self.workspace.session_id)
        for record in records:
            if record.lifecycle != "expired" or record.references or not record.content_available:
                continue
            shared = [other for other in records if other.artifact_id != record.artifact_id and
                      other.category == record.category and other.sha256 == record.sha256 and
                      other.content_available and (other.lifecycle != "expired" or bool(other.references))]
            if shared:
                continue
            self.guard.check(token)
            path = self._content_path(record)
            if path.exists():
                path.unlink()
                fsync_directory(path.parent)
                self.ledger.release_artifact(self.workspace.tenant_id, self.workspace.session_id, size=record.size)
            self.metadata.mark_content_deleted(token, record.artifact_id)
            removed.append(record.artifact_id)
        self.index.rebuild(token, self.metadata.list_session(self.workspace.tenant_id, self.workspace.session_id))
        return removed

    def manifest_hash(self) -> str:
        records = self.metadata.list_session(self.workspace.tenant_id, self.workspace.session_id)
        payload = [(record.artifact_id, record.sha256, record.content_available) for record in sorted(records, key=lambda item: item.artifact_id)]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()

    def verify_integrity(self, token: FenceToken, artifact_id: str) -> str:
        """Return a recovery-safe status without exposing artifact content."""
        self._validate_identity(token)
        record = self.metadata.get(artifact_id)
        if (record is None or record.tenant_id != self.workspace.tenant_id
                or record.session_id != self.workspace.session_id
                or not record.content_available):
            return "missing"
        path = self._content_path(record)
        if not path.exists():
            return "missing"
        if path.stat().st_size != record.size or _hash_file(path) != record.sha256:
            return "hash_mismatch"
        return "available"

    def _validate_identity(self, token: FenceToken) -> None:
        if token.session_id != self.workspace.session_id:
            raise WorkspaceError("fence token belongs to another workspace", code="artifact_identity_mismatch")
        self.guard.check(token)

    def _content_path(self, record: ArtifactMetadata) -> Path:
        return self.workspace.path_for(f"artifacts/{record.category}/{record.sha256[:2]}/{record.sha256}")


def _metadata_mapping(metadata: ArtifactMetadata) -> dict[str, Any]:
    value = asdict(metadata)
    value["references"] = list(metadata.references)
    return value


def _metadata_from_mapping(value: Mapping[str, Any]) -> ArtifactMetadata:
    raw = dict(value)
    raw["references"] = tuple(raw.get("references") or ())
    return ArtifactMetadata(**raw)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
