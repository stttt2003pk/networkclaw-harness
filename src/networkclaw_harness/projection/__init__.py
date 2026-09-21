"""Safe client-visible event and append-only audit projection."""

from .artifacts import ArtifactReference, ArtifactReferenceProjector, ArtifactSummaryPort
from .audit import (
    AuditContext,
    AuditError,
    AuditPolicy,
    AuditRecord,
    AuditService,
    AuditStorePort,
    ReferenceAuditStore,
)
from .events import (
    DeltaBatch,
    DeltaCoalescer,
    EventProjector,
    EventVisibilityPolicy,
    ProjectedEvent,
    ProjectionContext,
    ProjectionError,
    VisibilityMode,
    VisibilityRule,
    project_mapping,
)
from .semantic import (
    IdentityGraph, SemanticFactEnvelope, TimelineEntry, TimelineProjector,
    SEMANTIC_SCHEMA_VERSION, TIMELINE_SCHEMA_VERSION, identity_graph,
    versioned_fact,
)

__all__ = [
    "ArtifactReference", "ArtifactReferenceProjector", "ArtifactSummaryPort",
    "AuditContext", "AuditError", "AuditPolicy", "AuditRecord", "AuditService",
    "AuditStorePort", "DeltaBatch", "DeltaCoalescer", "EventProjector",
    "EventVisibilityPolicy", "ProjectedEvent", "ProjectionContext", "ProjectionError",
    "ReferenceAuditStore", "VisibilityMode", "VisibilityRule", "project_mapping",
    "IdentityGraph", "SemanticFactEnvelope", "TimelineEntry", "TimelineProjector",
    "SEMANTIC_SCHEMA_VERSION", "TIMELINE_SCHEMA_VERSION", "identity_graph", "versioned_fact",
]
