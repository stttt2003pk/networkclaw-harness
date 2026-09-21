#!/usr/bin/env python3
"""Assemble one signed, traceable, offline NetworkClaw Harness release."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from release_common import canonical_json, deterministic_tar, sha256_file, source_files, tree_hash

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_VERSION = "1.0"
TARGET = "ubuntu-22.04-linux-amd64-cp312"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / "dist/release")
    parser.add_argument("--wheelhouse", type=Path, default=REPO_ROOT / "offline/wheels")
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--ephemeral-signing-key", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--build-image", action="store_true")
    parser.add_argument("--builder")
    parser.add_argument("--base-image-archive", type=Path)
    parser.add_argument("--image-digest")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("offline releases must be assembled with CPython 3.12")

    version = _project_version()
    commit = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    if dirty and not args.allow_dirty:
        raise SystemExit("offline releases require a clean, committed source tree")
    if args.ephemeral_signing_key and not args.allow_dirty:
        raise SystemExit("ephemeral signing keys are restricted to non-publishable validation builds")
    if args.signing_key and args.ephemeral_signing_key:
        raise SystemExit("choose an external signing key or an ephemeral validation key")
    if not args.signing_key and not args.ephemeral_signing_key:
        raise SystemExit("a PEM signing key is required for a release")
    if not args.build_image and not args.image_digest and not args.allow_dirty:
        raise SystemExit("a publishable release requires a built or registry-provided image digest")

    release_dir = args.output_root.resolve() / version
    if release_dir.exists():
        raise SystemExit(f"release assembly directory already exists: {release_dir}")
    release_dir.mkdir(parents=True)
    artifacts_dir = release_dir / "artifacts"
    python_dir = release_dir / "python"
    supply_dir = release_dir / "supply-chain"
    signatures_dir = supply_dir / "signatures"
    image_dir = release_dir / "image"
    for directory in (artifacts_dir, python_dir, signatures_dir, image_dir):
        directory.mkdir(parents=True)

    epoch = int(_git("show", "-s", "--format=%ct", "HEAD") or "0")
    vendor_source = json.loads((REPO_ROOT / "upstream/hermes-source.json").read_text(encoding="utf-8"))
    wheelhouse = args.wheelhouse.resolve()
    _verify_wheelhouse(wheelhouse)

    with tempfile.TemporaryDirectory(prefix="networkclaw-release-") as temporary:
        temp = Path(temporary)
        source_stage = temp / f"networkclaw-harness-{version}"
        _copy_source(source_stage)
        files = source_files(source_stage)
        source_digest = tree_hash(source_stage, files)
        (source_stage / "SOURCE_COMMIT").write_text(commit + "\n", encoding="ascii")
        (source_stage / "SOURCE_TREE_SHA256").write_text(source_digest + "\n", encoding="ascii")

        _build_python_artifacts(source_stage, python_dir, wheelhouse, epoch)
        source_archive = artifacts_dir / f"networkclaw-harness-{version}-source.tar.gz"
        deterministic_tar(source_stage, source_archive, prefix=source_stage.name, epoch=epoch)

        image = _image_identity(
            args, source_stage, wheelhouse, image_dir, version, commit,
            str(vendor_source["commit"]), epoch,
        )
        _write_sboms(supply_dir, version, commit, source_digest, wheelhouse, image, vendor_source)
        shutil.copy2(REPO_ROOT / "THIRD_PARTY_NOTICES.md", supply_dir / "THIRD_PARTY_NOTICES.md")
        shutil.copytree(REPO_ROOT / "LICENSES", supply_dir / "LICENSES")
        shutil.copy2(REPO_ROOT / "offline/security-advisories.json", supply_dir / "security-advisories.json")
        shutil.copy2(
            REPO_ROOT / "offline/system-packages-ubuntu22.04-amd64.json",
            supply_dir / "system-packages-ubuntu22.04-amd64.json",
        )

        offline_stage = temp / f"networkclaw-harness-{version}-offline"
        offline_stage.mkdir()
        shutil.copy2(source_archive, offline_stage / source_archive.name)
        shutil.copytree(wheelhouse, offline_stage / "wheelhouse", ignore=shutil.ignore_patterns(".gitkeep"))
        shutil.copytree(python_dir, offline_stage / "python")
        for lock in ("requirements.lock", "requirements-build.lock", "requirements-dev.lock"):
            shutil.copy2(REPO_ROOT / lock, offline_stage / lock)
        shutil.copytree(supply_dir, offline_stage / "supply-chain")
        if args.base_image_archive:
            shutil.copy2(args.base_image_archive, offline_stage / "base-image.oci.tar")
        offline_archive = artifacts_dir / f"networkclaw-harness-{version}-offline-build.tar.gz"
        deterministic_tar(offline_stage, offline_archive, prefix=offline_stage.name, epoch=epoch)

        signing_key = _signing_key(args, temp)
        public_key = supply_dir / "release-signing-public.pem"
        _run(["openssl", "pkey", "-in", str(signing_key), "-pubout", "-out", str(public_key)])

        signed_files = [path for path in release_dir.rglob("*") if path.is_file()]
        signatures: dict[str, str] = {}
        for path in sorted(signed_files):
            relative = path.relative_to(release_dir).as_posix()
            signature = signatures_dir / (relative.replace("/", "__") + ".sig")
            _run(["openssl", "dgst", "-sha256", "-sign", str(signing_key),
                  "-out", str(signature), str(path)])
            signatures[relative] = signature.relative_to(release_dir).as_posix()

        artifact_hashes = {
            path.relative_to(release_dir).as_posix(): sha256_file(path)
            for path in sorted(release_dir.rglob("*"))
            if path.is_file() and not path.is_relative_to(signatures_dir)
        }
        manifest = {
            "schema_version": 1,
            "release_version": version,
            "target": TARGET,
            "source_commit": commit,
            "source_tree_sha256": source_digest,
            "source_dirty": dirty,
            "publishable": not dirty and not args.ephemeral_signing_key and image["status"] == "available",
            "generated_at": datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z"),
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "protocol_version": PROTOCOL_VERSION,
            "vendor_source_commit": vendor_source["commit"],
            "vendor_manifest_sha256": sha256_file(REPO_ROOT / "upstream/hermes-vendor-manifest.json"),
            "locks": {
                name: sha256_file(REPO_ROOT / name)
                for name in ("poetry.lock", "requirements.lock", "requirements-build.lock", "requirements-dev.lock")
            },
            "image": image,
            "artifacts": artifact_hashes,
            "signatures": signatures,
            "signature_algorithm": "rsa-3072-sha256",
            "allowed_nondeterminism": ["OCI archive transport metadata"],
        }
        manifest_path = release_dir / "release-manifest.json"
        manifest_path.write_bytes(canonical_json(manifest))
        _run(["openssl", "dgst", "-sha256", "-sign", str(signing_key),
              "-out", str(release_dir / "release-manifest.json.sig"), str(manifest_path)])

    print(release_dir)
    return 0


def _project_version() -> str:
    for line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"', 2)[1]
    raise SystemExit("project version is missing")


def _git(*arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(REPO_ROOT), *arguments], text=True).strip()


def _copy_source(destination: Path) -> None:
    destination.mkdir()
    for source in source_files(REPO_ROOT):
        relative = source.relative_to(REPO_ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _verify_wheelhouse(wheelhouse: Path) -> None:
    if not wheelhouse.is_dir():
        raise SystemExit("offline wheelhouse is missing")
    wheels = tuple(wheelhouse.glob("*.whl"))
    if not wheels:
        raise SystemExit("offline wheelhouse is empty")
    invalid = [path.name for path in wheels if not (
        path.name.endswith("-py3-none-any.whl")
        or ("cp312" in path.name and any(tag in path.name for tag in ("x86_64", "amd64")))
    )]
    if invalid:
        raise SystemExit(f"wheelhouse contains non-cp312/linux-amd64 wheels: {invalid}")


def _build_python_artifacts(source: Path, output: Path, wheelhouse: Path, epoch: int) -> None:
    with tempfile.TemporaryDirectory(prefix="networkclaw-build-env-") as temporary:
        environment = Path(temporary) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
              "--require-hashes", "--find-links", str(wheelhouse),
              "-r", str(source / "requirements-build.lock")])
        code = (
            "from poetry.core.masonry.api import build_sdist, build_wheel; "
            f"build_wheel({str(output)!r}); build_sdist({str(output)!r})"
        )
        _run([str(python), "-c", code], cwd=source, env={"SOURCE_DATE_EPOCH": str(epoch)})


def _image_identity(args: argparse.Namespace, source: Path, wheelhouse: Path, image_dir: Path,
                    version: str, commit: str, vendor_commit: str, epoch: int) -> dict[str, Any]:
    if args.image_digest:
        return {"status": "available", "digest": args.image_digest, "platform": "linux/amd64",
                "archive": None, "archive_sha256": None}
    if not args.build_image:
        return {"status": "not-built-validation-only", "digest": None, "platform": "linux/amd64",
                "archive": None, "archive_sha256": None}
    archive = image_dir / f"networkclaw-harness-{version}-linux-amd64.oci.tar"
    tag = f"networkclaw-harness:{version}-h5"
    command = [
        "docker", "build", "--platform=linux/amd64", "--network=none",
        "--build-arg", f"SOURCE_COMMIT={commit}",
        "--build-arg", f"SOURCE_DATE_EPOCH={epoch}",
        "--build-arg", f"VENDOR_COMMIT={vendor_commit}",
        "--build-arg", f"HARNESS_VERSION={version}",
        "--build-arg", f"PROTOCOL_VERSION={PROTOCOL_VERSION}",
        "--label", f"org.opencontainers.image.created={datetime.fromtimestamp(epoch, timezone.utc).isoformat()}",
        "-t", tag, str(source),
    ]
    _run(command)
    metadata = json.loads(subprocess.check_output(["docker", "image", "inspect", tag], text=True))[0]
    if metadata.get("Architecture") != "amd64" or metadata.get("Os") != "linux":
        raise SystemExit("built image does not target linux/amd64")
    with archive.open("wb") as output:
        subprocess.run(["docker", "save", tag], stdout=output, check=True)
    with tarfile.open(archive, "r") as oci:
        index = json.loads(oci.extractfile("index.json").read())
    descriptor = index["manifests"][0]
    return {
        "status": "available", "digest": descriptor["digest"], "platform": "linux/amd64",
        "config_digest": metadata["Id"],
        "archive": archive.relative_to(image_dir.parent).as_posix(),
        "archive_sha256": sha256_file(archive),
    }


def _write_sboms(destination: Path, version: str, commit: str, source_digest: str,
                 wheelhouse: Path, image: dict[str, Any], vendor_source: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    source_sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {"component": {"type": "application", "name": "networkclaw-harness", "version": version}},
        "components": [
            {"type": "application", "name": "networkclaw-harness-source", "version": commit,
             "hashes": [{"alg": "SHA-256", "content": source_digest}]},
            {"type": "library", "name": "hermes-agent-runtime-snapshot", "version": vendor_source["commit"]},
        ],
    }
    wheel_components = [{
        "type": "library", "name": path.name.rsplit("-", 3)[0].replace("_", "-"),
        "version": path.name.split("-")[1],
        "hashes": [{"alg": "SHA-256", "content": sha256_file(path)}],
        "properties": [{"name": "networkclaw:wheel-file", "value": path.name}],
    } for path in sorted(wheelhouse.glob("*.whl"))]
    wheel_sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {"component": {"type": "file", "name": "networkclaw-cp312-wheelhouse", "version": version}},
        "components": wheel_components,
    }
    image_sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {"component": {"type": "container", "name": "networkclaw-harness", "version": version}},
        "components": [{
            "type": "container", "name": "networkclaw-harness", "version": version,
            "purl": f"pkg:oci/networkclaw-harness@{image['digest']}" if image["digest"] else None,
            "properties": [
                {"name": "networkclaw:platform", "value": "linux/amd64"},
                {"name": "networkclaw:source-commit", "value": commit},
                {"name": "networkclaw:protocol-version", "value": PROTOCOL_VERSION},
                {"name": "networkclaw:vendor-commit", "value": vendor_source["commit"]},
            ],
        }],
    }
    (destination / "source-sbom.cdx.json").write_bytes(canonical_json(source_sbom))
    (destination / "wheelhouse-sbom.cdx.json").write_bytes(canonical_json(wheel_sbom))
    (destination / "image-sbom.cdx.json").write_bytes(canonical_json(image_sbom))


def _signing_key(args: argparse.Namespace, temp: Path) -> Path:
    if args.signing_key:
        if not args.signing_key.is_file():
            raise SystemExit("signing key does not exist")
        return args.signing_key.resolve()
    key = temp / "ephemeral-release-signing-key.pem"
    _run([
        "openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
        "-out", str(key),
    ])
    return key


def _run(command: list[str], *, cwd: Path | None = None,
         env: dict[str, str] | None = None) -> None:
    environment = os.environ.copy()
    if env:
        environment.update(env)
    subprocess.run(command, cwd=cwd, env=environment, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
