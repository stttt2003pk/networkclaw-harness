#!/usr/bin/env python3
"""Rebuild, test, install, and run a release with network access disabled."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import venv
from datetime import datetime
from pathlib import Path

from release_common import safe_extract, sha256_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--skip-regression", action="store_true")
    parser.add_argument("--skip-image", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    release = args.release_dir.resolve()
    subprocess.run([sys.executable, str(Path(__file__).with_name("verify-offline-release.py")), str(release)], check=True)
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="networkclaw-offline-acceptance-") as temporary:
        root = Path(temporary)
        source_archive = next((release / "artifacts").glob("*-source.tar.gz"))
        source = safe_extract(source_archive, root / "source")
        offline_archive = next((release / "artifacts").glob("*-offline-build.tar.gz"))
        offline = safe_extract(offline_archive, root / "offline")
        wheelhouse = offline / "wheelhouse"
        environment = source / ".venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        sealed = {
            **os.environ,
            "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "http_proxy": "", "https_proxy": "", "HTTP_PROXY": "", "HTTPS_PROXY": "",
            "NO_PROXY": "*", "SOURCE_DATE_EPOCH": str(_epoch(manifest["generated_at"])),
        }
        for lock in ("requirements-build.lock", "requirements-dev.lock", "requirements.lock"):
            subprocess.run([
                str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                "--require-hashes", "--find-links", str(wheelhouse), "-r", str(offline / lock),
            ], env=sealed, check=True)

        rebuilt = root / "rebuilt"
        rebuilt.mkdir()
        code = (
            "from poetry.core.masonry.api import build_sdist, build_wheel; "
            f"build_wheel({str(rebuilt)!r}); build_sdist({str(rebuilt)!r})"
        )
        subprocess.run([str(python), "-c", code], cwd=source, env=sealed, check=True)
        for released in (release / "python").iterdir():
            counterpart = rebuilt / released.name
            if not counterpart.is_file() or sha256_file(counterpart) != sha256_file(released):
                raise SystemExit(f"rebuilt Python artifact differs: {released.name}")

        wheel = next((release / "python").glob("*.whl"))
        subprocess.run([
            str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel),
        ], env=sealed, check=True)
        if not args.skip_regression:
            subprocess.run([str(source / "scripts/run_tests.sh")], cwd=source, env=sealed, check=True)
        _headless_demo(python, sealed)
        if not args.skip_image:
            _image_rebuild(source, offline, manifest)

    report = {
        "schema_version": 1, "status": "passed",
        "release_version": manifest["release_version"],
        "source_commit": manifest["source_commit"],
        "network": "disabled", "cache": "empty",
        "python_artifacts_reproducible": True,
        "core_regression": not args.skip_regression,
        "headless_demo": True,
        "image_rebuilt": not args.skip_image,
        "image_digest": manifest["image"].get("digest"),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


def _headless_demo(python: Path, environment: dict[str, str]) -> None:
    frames = (
        {"protocol_version": "1.0", "type": "protocol.negotiate", "request_id": "offline-negotiate",
         "payload": {"supported_protocol_versions": ["1.0"]}},
        {"protocol_version": "1.0", "type": "health.query", "request_id": "offline-health"},
        {"protocol_version": "1.0", "type": "capabilities.query", "request_id": "offline-capabilities"},
        {"protocol_version": "1.0", "type": "shutdown", "request_id": "offline-shutdown"},
    )
    payload = "".join(json.dumps(frame) + "\n" for frame in frames)
    completed = subprocess.run(
        [str(python), "-m", "networkclaw_harness.host"], input=payload,
        text=True, capture_output=True, env=environment, check=True,
    )
    events = [json.loads(line) for line in completed.stdout.splitlines()]
    event_types = {event["type"] for event in events}
    required = {"protocol.negotiated", "health.status", "capabilities.report", "shutdown.completed"}
    if not required.issubset(event_types):
        raise SystemExit("offline headless demonstration did not complete")
    for line in io.StringIO(completed.stderr):
        json.loads(line)


def _image_rebuild(source: Path, offline: Path, manifest: dict) -> None:
    image = manifest["image"]
    if image.get("status") != "available" or not image.get("config_digest"):
        raise SystemExit("offline image rebuild requires an available release image")
    base = offline / "base-image.oci.tar"
    if not base.is_file():
        raise SystemExit("offline package is missing its base image archive")
    subprocess.run(["docker", "load", "--input", str(base)], check=True)
    tag = f"networkclaw-harness:{manifest['release_version']}-offline-rebuild"
    subprocess.run([
        "docker", "build", "--platform=linux/amd64", "--network=none",
        "--build-arg", f"SOURCE_COMMIT={manifest['source_commit']}",
        "--build-arg", f"SOURCE_DATE_EPOCH={_epoch(manifest['generated_at'])}",
        "--build-arg", f"VENDOR_COMMIT={manifest['vendor_source_commit']}",
        "--build-arg", f"HARNESS_VERSION={manifest['release_version']}",
        "--build-arg", f"PROTOCOL_VERSION={manifest['protocol_version']}",
        "--label", f"org.opencontainers.image.created={manifest['generated_at'].replace('Z', '+00:00')}",
        "-t", tag, str(source),
    ], env={**os.environ, "DOCKER_BUILDKIT": "1"}, check=True)
    metadata = json.loads(subprocess.check_output(["docker", "image", "inspect", tag], text=True))[0]
    if metadata["Id"] != image["config_digest"]:
        raise SystemExit("offline rebuilt image config differs from the release image")
    labels = metadata["Config"]["Labels"]
    expected = {
        "org.opencontainers.image.revision": manifest["source_commit"],
        "org.opencontainers.image.version": manifest["release_version"],
        "io.networkclaw.hermes.vendor-commit": manifest["vendor_source_commit"],
        "io.networkclaw.harness.protocol-version": manifest["protocol_version"],
    }
    if any(labels.get(name) != value for name, value in expected.items()):
        raise SystemExit("offline rebuilt image labels do not match the release manifest")
    frames = (
        {"protocol_version": "1.0", "type": "protocol.negotiate", "request_id": "container-negotiate",
         "payload": {"supported_protocol_versions": ["1.0"]}},
        {"protocol_version": "1.0", "type": "shutdown", "request_id": "container-shutdown"},
    )
    owner = (
        "import os,sys,subprocess; "
        "p=subprocess.Popen([sys.executable,'-m','networkclaw_harness.host','--parent-pid',str(os.getpid())]); "
        "raise SystemExit(p.wait())"
    )
    wrapper = (
        "import sys,subprocess; "
        f"p=subprocess.Popen([sys.executable,'-c',{owner!r}]); "
        "raise SystemExit(p.wait())"
    )
    completed = subprocess.run([
        "docker", "run", "--rm", "--platform=linux/amd64", "--network=none",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "-i",
        "--entrypoint", "/usr/local/bin/python", tag,
        "-c", wrapper,
    ], input="".join(json.dumps(frame) + "\n" for frame in frames), text=True,
       capture_output=True, check=True)
    if "protocol.negotiated" not in completed.stdout or "shutdown.completed" not in completed.stdout:
        raise SystemExit("offline rebuilt image failed the hardened headless demonstration")


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


if __name__ == "__main__":
    raise SystemExit(main())
