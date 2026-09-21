"""Safe client references for durable artifact metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from networkclaw_harness.workspace import ArtifactMetadata, ArtifactMetadataPort

from .events import project_mapping


class ArtifactSummaryPort(Protocol):
    def __call__(self, metadata: ArtifactMetadata) -> str: ...


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    name: str
    mime_type: str | None
    size: int | None
    summary: str
    source: dict[str, str]
    status: str
    view_ref: str | None


class ArtifactReferenceProjector:
    """Projects metadata only; content access remains an authorized artifact operation."""

    def __init__(self, metadata: ArtifactMetadataPort,
                 summary: ArtifactSummaryPort | None = None,
                 view_ref_factory: Callable[[ArtifactMetadata], str] | None = None) -> None:
        self._metadata = metadata
        self._summary = summary or (lambda item: f"{item.mime_type} artifact ({item.size} bytes)")
        self._view_ref_factory = view_ref_factory or (lambda item: f"artifact:{item.artifact_id}")

    def project(self, *, tenant_id: str, session_id: str, artifact_id: str,
                authorized: bool = False,
                secret_values: Sequence[str] = ()) -> ArtifactReference:
        metadata = self._metadata.get(artifact_id)
        if metadata is None or metadata.tenant_id != tenant_id or metadata.session_id != session_id:
            return ArtifactReference(artifact_id, artifact_id, None, None, "Artifact not found.", {}, "not_found", None)
        source = {
            str(key): str(value)
            for key, value in project_mapping(
                metadata.source, secret_values=secret_values,
            ).items()
        }
        name = source.get("name") or artifact_id
        if metadata.lifecycle == "expired":
            status = "expired"
        elif not metadata.content_available:
            status = "unavailable"
        elif metadata.category == "raw" and not authorized:
            status = "restricted"
        else:
            status = "available"
        reference = self._view_ref_factory(metadata) if status == "available" and authorized else None
        summary = project_mapping(
            {"summary": self._summary(metadata)}, secret_values=secret_values,
        )["summary"]
        return ArtifactReference(
            artifact_id, name, metadata.mime_type, metadata.size,
            str(summary), source, status, reference,
        )
