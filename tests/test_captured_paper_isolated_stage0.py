from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import pytest

from scripts import captured_paper_isolated_stage0 as stage0


GENERATION = "12aa9f2d-bda8-43d1-b0c4-397b7dbaac82"
SOURCE_STAGE0 = Path(stage0.__file__).resolve(strict=True)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, raw: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _fixture(
    tmp_path: Path,
    *,
    swapping_target: bool = False,
    dependency_guard_target: bool = False,
    dependency_resource_target: bool = False,
) -> dict[str, Any]:
    candidate = tmp_path / "candidate"
    artifact_root = tmp_path / "activation"
    dependency_source_root = tmp_path / "dependency-source"
    dependency_source = _write(
        dependency_source_root / "bounddep" / "module.py", b"VALUE = 1\n"
    )
    _write(dependency_source_root / "bounddep" / "__init__.py", b"\n")
    _write(dependency_source_root / "bounddep" / "data.txt", b"sealed-resource\n")
    scripts = candidate / "scripts"
    stage0_source = _write(
        scripts / "captured_paper_isolated_stage0.py", SOURCE_STAGE0.read_bytes()
    )
    marker = tmp_path / "target-ran.txt"
    safe_marker = tmp_path / "held-safe.txt"
    malicious_marker = tmp_path / "mutable-malicious.txt"
    helper = _write(
        scripts / "helper.py",
        (
            "from pathlib import Path\n"
            f"Path({str(safe_marker)!r}).write_text('held-safe', encoding='utf-8')\n"
        ).encode(),
    )
    if dependency_resource_target:
        target_source = (
            "from importlib import resources\n"
            "from pathlib import Path\n"
            "value = resources.files('bounddep').joinpath('data.txt').read_text()\n"
            f"Path({str(marker)!r}).write_text(value, encoding='utf-8')\n"
        ).encode()
    elif dependency_guard_target:
        target_source = (
            "from pathlib import Path\n"
            "import sys\n"
            "dependency = Path(sys.path[-1]) / 'bounddep' / 'module.py'\n"
            "try:\n"
            "    dependency.write_text('VALUE = 9\\n', encoding='utf-8')\n"
            "    outcome = 'mutable'\n"
            "except OSError:\n"
            "    outcome = 'blocked'\n"
            "from bounddep.module import VALUE\n"
            f"Path({str(marker)!r}).write_text(f'{{outcome}}:{{VALUE}}', encoding='utf-8')\n"
        ).encode()
    elif swapping_target:
        target_source = (
            "from pathlib import Path\n"
            f"helper = Path({str(helper)!r})\n"
            "helper.write_text(\"from pathlib import Path\\n"
            f"Path({str(malicious_marker)!r}).write_text('bad', encoding='utf-8')\\n\", "
            "encoding='utf-8')\n"
            "import scripts.helper\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        ).encode()
    else:
        target_source = (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        ).encode()
    service_source = _write(scripts / "service.py", target_source)
    host_source = _write(scripts / "host.py", b"raise SystemExit(0)\n")
    rows = []
    for role, path in (
        ("activation_service", service_source),
        ("activation_stage0", stage0_source),
        ("captured_paper_host_cutover", host_source),
        ("local_dependency:scripts.helper", helper),
    ):
        rows.append({"role": role, "path": str(path), "sha256": _sha(path)})
    rows.sort(key=lambda row: row["role"])
    code_body = {
        "schema_version": "chili.captured-paper-code-build.v1",
        "artifacts": rows,
    }
    code_build = {
        **code_body,
        "code_build_sha256": hashlib.sha256(_canonical(code_body)).hexdigest(),
    }
    stage0_sha = _sha(stage0_source)
    service_sha = _sha(service_source)
    staged_stage0 = _write(
        artifact_root / GENERATION / stage0_sha / f"{stage0_sha}.py",
        stage0_source.read_bytes(),
    )
    staged_service = _write(
        artifact_root / GENERATION / service_sha / f"{service_sha}.py",
        service_source.read_bytes(),
    )
    python = Path(sys.executable).resolve(strict=True)
    python_sha = _sha(python)
    dependency_tree_sha = str(
        stage0._dependency_tree_inventory(dependency_source_root)["tree_sha256"]
    )
    dependency_root = (
        artifact_root
        / GENERATION
        / "dependencies"
        / dependency_tree_sha
        / "site-packages"
    )
    shutil.copytree(dependency_source_root, dependency_root)
    dependency = dependency_root / dependency_source.relative_to(dependency_source_root)
    dependency_identity_sha = stage0.dependency_root_identity_sha256(
        dependency_root=dependency_root,
        python_executable=python,
        python_executable_sha256=python_sha,
    )
    manifest_body = {
        "schema_version": "chili.captured-paper-activation.v3",
        "activation_generation": GENERATION,
        "code_build": code_build,
        "cutover": {
            "activation_artifact_root": str(artifact_root),
            "candidate_root": str(candidate),
            "python_executable_path": str(python),
            "python_executable_sha256": python_sha,
            "python_dependency_root": str(dependency_root),
            "python_dependency_root_identity_sha256": dependency_identity_sha,
            "service_path": str(staged_service),
            "service_sha256": service_sha,
            "stage0_source_path": str(stage0_source),
            "stage0_source_sha256": stage0_sha,
            "stage0_path": str(staged_stage0),
            "stage0_sha256": stage0_sha,
        },
    }
    manifest_body["activation_manifest_sha256"] = hashlib.sha256(
        _canonical(manifest_body)
    ).hexdigest()
    manifest_raw = _canonical(manifest_body)
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    manifest = _write(
        tmp_path / "manifests" / manifest_sha[:2] / f"{manifest_sha}.json",
        manifest_raw,
    )
    return {
        "candidate": candidate,
        "dependency": dependency,
        "dependency_root": dependency_root,
        "manifest": manifest,
        "manifest_sha": manifest_sha,
        "marker": marker,
        "safe_marker": safe_marker,
        "malicious_marker": malicious_marker,
        "service": staged_service,
        "service_sha": service_sha,
        "stage0": staged_stage0,
    }


def _run(bundle: dict[str, Any], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(bundle["stage0"]),
            "--manifest",
            str(bundle["manifest"]),
            "--manifest-sha256",
            bundle["manifest_sha"],
            "--candidate-root",
            str(bundle["candidate"]),
            "--target-role",
            "activation_service",
            "--target",
            str(bundle["service"]),
            "--target-sha256",
            bundle["service_sha"],
            "--",
            "--test-target",
        ],
        cwd=bundle["candidate"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_stage0_uses_held_local_bytes_after_verified_path_is_swapped(tmp_path: Path) -> None:
    bundle = _fixture(tmp_path, swapping_target=True)

    result = _run(bundle)

    assert result.returncode == 0, result.stderr
    assert bundle["marker"].read_text(encoding="utf-8") == "ran"
    assert bundle["safe_marker"].read_text(encoding="utf-8") == "held-safe"
    assert not bundle["malicious_marker"].exists()


def test_stage0_rejects_dependency_byte_mutation_before_target(tmp_path: Path) -> None:
    bundle = _fixture(tmp_path)
    root_times = bundle["dependency_root"].stat()
    bundle["dependency"].write_bytes(b"VALUE = 2\n")
    os.utime(
        bundle["dependency_root"],
        ns=(root_times.st_atime_ns, root_times.st_mtime_ns),
    )

    result = _run(bundle)

    assert result.returncode == 2
    assert "DEPENDENCY_ROOT_NOT_CONTENT_ADDRESSED" in result.stderr
    assert not bundle["marker"].exists()


def test_isolated_flags_prevent_all_preverification_sentinels(tmp_path: Path) -> None:
    bundle = _fixture(tmp_path)
    sentinel = tmp_path / "startup-sentinel.txt"
    payload = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
    )
    for relative in (
        "sitecustomize.py",
        "usercustomize.py",
        "dotenv.py",
        "scripts/__init__.py",
    ):
        _write(bundle["candidate"] / relative, payload.encode())
    injected = tmp_path / "injected"
    _write(injected / "sitecustomize.py", payload.encode())
    _write(injected / "usercustomize.py", payload.encode())
    _write(injected / "startup.pth", (str(bundle["candidate"]) + "\n").encode())
    env = dict(os.environ)
    env["PYTHONPATH"] = str(injected)
    env["PYTHONUSERBASE"] = str(injected)

    result = _run(bundle, env=env)

    assert result.returncode == 2
    assert "UNSEALED_BOOTSTRAP_MODULE" in result.stderr
    assert not sentinel.exists()
    assert not bundle["marker"].exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows deny-write handle contract")
def test_stage0_holds_dependency_bytes_immutable_through_import(tmp_path: Path) -> None:
    bundle = _fixture(tmp_path, dependency_guard_target=True)

    result = _run(bundle)

    assert result.returncode == 0, result.stderr
    assert bundle["marker"].read_text(encoding="utf-8") == "blocked:1"
    assert bundle["dependency"].read_bytes() == b"VALUE = 1\n"


def test_stage0_serves_only_hash_bound_dependency_resources(tmp_path: Path) -> None:
    bundle = _fixture(tmp_path, dependency_resource_target=True)

    result = _run(bundle)

    assert result.returncode == 0, result.stderr
    assert bundle["marker"].read_text(encoding="utf-8") == "sealed-resource\n"


def test_unsealed_nonstdlib_import_cannot_fall_through_interpreter_paths(
    tmp_path: Path,
) -> None:
    dependency_root = tmp_path / "site-packages"
    dependency_root.mkdir()
    guard = stage0._DenyUnsealedImportFinder(dependency_root=dependency_root)

    with pytest.raises(ImportError, match="unsealed import rejected"):
        guard.find_spec("chili_unsealed_optional_integration")
