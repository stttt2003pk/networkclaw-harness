import json
import subprocess
import sys
from pathlib import Path

from networkclaw_harness.policies.profiles import customer_profile, development_profile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYNC = REPOSITORY_ROOT / "scripts" / "sync-hermes-runtime.py"
VERIFY = REPOSITORY_ROOT / "scripts" / "verify-hermes-vendor.py"
H0 = REPOSITORY_ROOT / "scripts" / "run-hermes-h0.py"


def test_sync_uses_the_pinned_ancestor_and_verifier_rejects_tampering(tmp_path: Path):
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("hermes_constants.py\n", encoding="utf-8")
    vendor = tmp_path / "vendor"
    manifest = tmp_path / "manifest.json"
    patch_directory = tmp_path / "patches"
    patch_directory.mkdir()

    subprocess.run(
        [
            sys.executable,
            str(SYNC),
            "--allowlist",
            str(allowlist),
            "--destination",
            str(vendor),
                "--manifest",
                str(manifest),
                "--patch-directory",
                str(patch_directory),
            ],
        check=True,
        capture_output=True,
        text=True,
    )
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    assert recorded["source_commit"] == "6005aa1fd9aac8b1024ace50fec8cd1c85a04bae"
    assert (vendor / "hermes_constants.py").is_file()

    verify = [
        sys.executable,
        str(VERIFY),
        "--vendor",
        str(vendor),
        "--manifest",
        str(manifest),
        "--allowlist",
        str(allowlist),
        "--patch-directory",
        str(patch_directory),
    ]
    assert subprocess.run(verify, capture_output=True, text=True).returncode == 0

    (vendor / "unexpected.py").write_text("changed = True\n", encoding="utf-8")
    rejected = subprocess.run(verify, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "unexpected" in rejected.stderr


def test_h0_import_probe_loads_only_from_the_vendor_snapshot(tmp_path: Path):
    report = tmp_path / "h0-p01.json"
    subprocess.run(
        [sys.executable, str(H0), "--report", str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(report.read_text(encoding="utf-8"))

    assert evidence["probe_id"] == "H0-P01"
    assert evidence["result"] == "passed"
    assert evidence["imports"] == {
        "agent": "agent/__init__.py",
        "hermes_constants": "hermes_constants.py",
        "tools": "tools/__init__.py",
    }


def test_production_profiles_reject_runtime_capability_mutation():
    for profile in (customer_profile(), development_profile()):
        assert not profile.runtime_skill_install_enabled
        assert not profile.runtime_tool_install_enabled
        assert not profile.self_evolution_enabled
