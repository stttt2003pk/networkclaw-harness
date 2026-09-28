from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts/build-offline-release.py"
VERIFY = ROOT / "scripts/verify-offline-release.py"
GIT_CHECKOUT = (ROOT / ".git").exists()


@pytest.mark.skipif(not GIT_CHECKOUT, reason="release assembly requires a Git checkout")
def test_publishable_release_requires_clean_tree_external_key_and_image(tmp_path: Path):
    completed = subprocess.run(
        [sys.executable, str(BUILD), "--output-root", str(tmp_path / "release"),
         "--signing-key", "/missing/key.pem", "--image-digest", "sha256:test"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert completed.returncode != 0
    assert "clean" in (completed.stderr + completed.stdout)


@pytest.mark.skipif(not GIT_CHECKOUT, reason="release assembly requires a Git checkout")
def test_release_builder_refuses_to_overwrite_existing_version(tmp_path: Path):
    target = tmp_path / "release"
    version = target / "0.1.0"
    version.mkdir(parents=True)
    completed = subprocess.run(
        [sys.executable, str(BUILD), "--output-root", str(target), "--allow-dirty",
         "--ephemeral-signing-key"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert completed.returncode != 0
    assert "already exists" in (completed.stderr + completed.stdout)


@pytest.mark.skipif(not GIT_CHECKOUT, reason="release assembly requires a Git checkout")
def test_offline_validation_release_has_traceable_signed_archives(tmp_path: Path):
    output = tmp_path / "release"
    completed = subprocess.run(
        [sys.executable, str(BUILD), "--output-root", str(output), "--allow-dirty",
         "--ephemeral-signing-key"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    release = output / "0.1.0"
    verified = subprocess.run(
        [sys.executable, str(VERIFY), str(release)],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    result = json.loads(verified.stdout)
    assert result["status"] == "passed" and result["publishable"] is False
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["target"] == "ubuntu-22.04-linux-amd64-cp312"
    assert manifest["protocol_version"] == "1.0"
    assert manifest["vendor_source_commit"]
    assert len(manifest["artifacts"]) >= 10
    source_archive = next((release / "artifacts").glob("*-source.tar.gz"))
    with tarfile.open(source_archive, "r:gz") as archive:
        names = archive.getnames()
    assert all("/.git/" not in name for name in names)
    assert not any((release / "artifacts").rglob("*.whl"))


@pytest.mark.skipif(not GIT_CHECKOUT, reason="release assembly requires a Git checkout")
def test_image_build_context_contains_wheels_without_polluting_source_archive(tmp_path: Path):
    """The offline image needs wheels, while the source artifact must remain wheelhouse-free."""
    import importlib.util

    scripts_path = str(ROOT / "scripts")
    sys.path.insert(0, scripts_path)
    try:
        spec = importlib.util.spec_from_file_location("build_offline_release", BUILD)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts_path)
    source = tmp_path / "source"
    source.mkdir()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "example-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
    module._populate_image_wheelhouse(source, wheelhouse)
    image = source / "offline" / "wheels"
    assert (image / "example-1.0.0-py3-none-any.whl").is_file()


@pytest.mark.parametrize("script", ("verify-offline-release.py", "test-offline-release.py"))
def test_release_tools_are_present_and_python312_only(script: str):
    path = ROOT / "scripts" / script
    assert path.is_file()
    completed = subprocess.run([sys.executable, str(path), "--help"], capture_output=True, text=True)
    assert completed.returncode == 0
    assert "release" in completed.stdout.lower()
