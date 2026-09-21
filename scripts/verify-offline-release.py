#!/usr/bin/env python3
"""Verify release hashes, signatures, identities, archives, and SBOM linkage offline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from release_common import safe_extract, sha256_file, source_files, tree_hash


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--require-publishable", action="store_true")
    args = parser.parse_args()
    release = args.release_dir.resolve()
    manifest_path = release / "release-manifest.json"
    signature_path = release / "release-manifest.json.sig"
    public_key = release / "supply-chain/release-signing-public.pem"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise SystemExit("unsupported release manifest")
    if args.require_publishable and not manifest.get("publishable"):
        raise SystemExit("release is a validation build and cannot be published")
    _verify_signature(public_key, manifest_path, signature_path)

    for relative, expected in manifest["artifacts"].items():
        path = release / relative
        if not path.is_file() or sha256_file(path) != expected:
            raise SystemExit(f"artifact hash mismatch: {relative}")
        signature = release / manifest["signatures"][relative]
        _verify_signature(public_key, path, signature)

    source_archive = _one(release.glob("artifacts/*-source.tar.gz"), "source archive")
    offline_archive = _one(release.glob("artifacts/*-offline-build.tar.gz"), "offline archive")
    wheel = _one(release.glob("python/*.whl"), "application wheel")
    _verify_wheel(wheel, manifest["release_version"])
    with tempfile.TemporaryDirectory(prefix="networkclaw-verify-") as temporary:
        source = safe_extract(source_archive, Path(temporary) / "source")
        if (source / "SOURCE_COMMIT").read_text(encoding="ascii").strip() != manifest["source_commit"]:
            raise SystemExit("SOURCE_COMMIT does not match release manifest")
        if (source / "SOURCE_TREE_SHA256").read_text(encoding="ascii").strip() != manifest["source_tree_sha256"]:
            raise SystemExit("source tree identity does not match release manifest")
        actual_tree = tree_hash(source, source_files(source))
        if actual_tree != manifest["source_tree_sha256"]:
            raise SystemExit("source archive content does not match SOURCE_TREE_SHA256")
        vendor = json.loads((source / "upstream/hermes-source.json").read_text(encoding="utf-8"))
        if vendor["commit"] != manifest["vendor_source_commit"]:
            raise SystemExit("vendor source commit does not match release manifest")
        if sha256_file(source / "upstream/hermes-vendor-manifest.json") != manifest["vendor_manifest_sha256"]:
            raise SystemExit("vendor manifest does not match release manifest")
        forbidden = tuple((source / "vendor/hermes").rglob("*.whl")) + tuple(
            (source / "vendor/hermes").rglob("*.tar")
        )
        if forbidden:
            raise SystemExit("vendor/hermes contains release outputs")
        safe_extract(offline_archive, Path(temporary) / "offline")

    for name in ("source-sbom.cdx.json", "wheelhouse-sbom.cdx.json", "image-sbom.cdx.json"):
        document = json.loads((release / "supply-chain" / name).read_text(encoding="utf-8"))
        if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.6":
            raise SystemExit(f"invalid SBOM: {name}")
    if not (release / "supply-chain/LICENSES/HERMES-MIT.txt").is_file():
        raise SystemExit("Hermes license is missing")
    if not (release / "supply-chain/THIRD_PARTY_NOTICES.md").is_file():
        raise SystemExit("third-party notices are missing")
    image = manifest["image"]
    if image["status"] == "available":
        if not isinstance(image.get("digest"), str) or not image["digest"].startswith("sha256:"):
            raise SystemExit("image digest is invalid")
        if image.get("archive"):
            archive = release / image["archive"]
            if sha256_file(archive) != image["archive_sha256"]:
                raise SystemExit("OCI archive hash mismatch")
    print(json.dumps({
        "status": "passed", "release_version": manifest["release_version"],
        "publishable": manifest["publishable"], "artifact_count": len(manifest["artifacts"]),
        "image_digest": image.get("digest"),
    }, sort_keys=True))
    return 0


def _verify_signature(public_key: Path, payload: Path, signature: Path) -> None:
    completed = subprocess.run([
        "openssl", "dgst", "-sha256", "-verify", str(public_key),
        "-signature", str(signature), str(payload),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if completed.returncode:
        raise SystemExit(f"signature verification failed: {payload.name}")


def _one(paths, label: str) -> Path:
    values = tuple(paths)
    if len(values) != 1:
        raise SystemExit(f"release must contain exactly one {label}")
    return values[0]


def _verify_wheel(path: Path, version: str) -> None:
    if not path.name.endswith("-py3-none-any.whl"):
        raise SystemExit("application wheel is not portable across CPython 3.12 linux/amd64")
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    if f"Version: {version}\n" not in metadata or "Requires-Python: >=3.12,<3.13" not in metadata:
        raise SystemExit("application wheel metadata does not match the release")


if __name__ == "__main__":
    raise SystemExit(main())
