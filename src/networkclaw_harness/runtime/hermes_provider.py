"""Hermes provider resolution boundary; Harness remains the loop owner."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from .provider_catalog import canonical_model


def resolve_route(provider_id: str, model_id: str, *, vendor_root: Path | None = None) -> Mapping[str, Any]:
    """Load Hermes' resolver from the pinned snapshot without allowing arbitrary plugins."""
    canonical = canonical_model(provider_id, model_id)
    root = vendor_root or Path(__file__).resolve().parents[3] / "vendor" / "hermes"
    root_text = str(root)
    added = root_text not in sys.path
    if added:
        sys.path.insert(0, root_text)
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider  # type: ignore
        # Runtime resolver is intentionally probed with a minimal, host-owned route.  It may
        # return a backend object when deployed; no object crosses the Harness protocol boundary.
        return {"provider_id": canonical.provider_id, "model_id": canonical.model_id,
                "resolver": getattr(resolve_runtime_provider, "__name__", "resolve_runtime_provider"),
                "transport": "openai-compatible"}
    except Exception as error:
        raise RuntimeError("pinned Hermes provider runtime is unavailable") from error
    finally:
        if added:
            sys.path.remove(root_text)

