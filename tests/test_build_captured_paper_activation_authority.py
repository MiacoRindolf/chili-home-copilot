from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from typing import Any, Mapping

import pytest

from scripts import build_captured_paper_activation_authority as builder
from scripts import captured_paper_activation_runner as runner
from scripts import run_captured_paper_operator_chain as chain


REPO = Path(__file__).resolve().parents[1]
ACCOUNT_ID = "11111111-2222-4333-8444-555555555555"
RUNTIME_SECRET = "NEVER_SERIALIZE_THIS_PAPER_SECRET_76f061"
BRIDGE_CONFIGURATION: Mapping[str, Any] = {
    "iqfeed_l1": {
        "schema_version": "chili.iqfeed-l1-bridge-capture-config.v3",
        "protocol_version": "6.2",
        "host": "127.0.0.1",
        "port": 5009,
    },
    "iqfeed_l2": {
        "schema_version": "chili.iqfeed-depth-bridge.capture-config.v1",
        "protocol": "6.2",
        "provider_timezone": "America/New_York",
    },
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _git(candidate: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git.exe" if sys.platform == "win32" else "git")
    assert git
    return subprocess.run(
        [git, *arguments],
        cwd=candidate,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        shell=False,
        check=True,
    )


@dataclass(slots=True)
class AuthorityFixture:
    root: Path
    candidate: Path
    artifact: Path
    legacy: Path
    dependency: Path
    runtime_env: Path
    benchmark: Path

    def kwargs(self, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "candidate_root": self.candidate,
            "artifact_root": self.artifact,
            "legacy_root": self.legacy,
            "python_dependency_root": self.dependency,
            "runtime_env_path": self.runtime_env,
            "resource_benchmark_path": self.benchmark,
            "expected_account_id": ACCOUNT_ID,
            "test_database_name": "captured_paper_test",
            "bridge_configuration": BRIDGE_CONFIGURATION,
        }
        values.update(overrides)
        return values


def _make_fixture(tmp_path: Path) -> AuthorityFixture:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for relative in sorted(builder._CRITICAL_TRACKED):
        source = REPO / relative
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _git(candidate, "init")
    _git(candidate, "config", "core.autocrlf", "false")
    _git(candidate, "config", "user.name", "CHILI Test")
    _git(candidate, "config", "user.email", "chili-test@example.invalid")
    _git(candidate, "add", "--all")
    _git(candidate, "commit", "-m", "sealed candidate")

    artifact = tmp_path / "artifacts"
    artifact.mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    dependency = tmp_path / "dependencies"
    dependency.mkdir()
    inputs = tmp_path / "authority-inputs"
    inputs.mkdir()
    runtime_env = inputs / "captured-paper.env"
    runtime_env.write_text(
        "DATABASE_URL=postgresql://paper_user:"
        f"{RUNTIME_SECRET}@127.0.0.1:5433/captured_paper_test\n"
        f"CHILI_ALPACA_API_KEY={RUNTIME_SECRET}\n"
        f"CHILI_ALPACA_API_SECRET={RUNTIME_SECRET}\n"
        f"CHILI_ALPACA_EXPECTED_ACCOUNT_ID={ACCOUNT_ID}\n",
        encoding="utf-8",
    )
    benchmark = inputs / "resource-benchmark.json"
    benchmark.write_bytes(
        json.dumps(
            {
                "benchmark_schema_version": "test.resource-benchmark.v1",
                "measured": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return AuthorityFixture(
        root=tmp_path,
        candidate=candidate.resolve(strict=True),
        artifact=artifact.resolve(strict=True),
        legacy=legacy.resolve(strict=True),
        dependency=dependency.resolve(strict=True),
        runtime_env=runtime_env.resolve(strict=True),
        benchmark=benchmark.resolve(strict=True),
    )


@pytest.fixture
def authority_fixture(tmp_path: Path) -> AuthorityFixture:
    return _make_fixture(tmp_path)


def test_import_is_inert_stdlib_only_and_performs_no_authority_probe(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-be-created"
    code = f"""
import importlib.util, pathlib, sys
path = pathlib.Path({str((REPO / 'scripts/build_captured_paper_activation_authority.py').resolve())!r})
spec = importlib.util.spec_from_file_location('authority_import_probe', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert callable(module.build_captured_paper_activation_authority)
assert not pathlib.Path({str(marker)!r}).exists()
assert not any(name.startswith('scripts.captured_paper_') for name in sys.modules)
print('IMPORT_INERT')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        shell=False,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IMPORT_INERT"
    assert not marker.exists()


def test_real_temp_git_builds_exact_canonical_loader_roundtrip_and_no_secret_receipt(
    authority_fixture: AuthorityFixture,
) -> None:
    built = builder.build_captured_paper_activation_authority(
        **authority_fixture.kwargs()
    )

    chain_raw = built.chain_request_path.read_bytes()
    chain_document = json.loads(chain_raw)
    assert chain_raw == builder._canonical_json_bytes(chain_document)
    assert set(chain_document) == set(chain._CHAIN_KEYS) == set(builder._CHAIN_KEYS)
    assert chain_document["account_scope"] == "alpaca:paper"
    assert chain_document["live_cash_authorized"] is False
    assert built.chain_request_path == (
        authority_fixture.artifact
        / "authority"
        / "chain-request"
        / built.chain_request_sha256[:2]
        / f"{built.chain_request_sha256}.json"
    )

    request_raw = built.activation_request_path.read_bytes()
    request_document = json.loads(request_raw)
    assert request_raw == builder._canonical_json_bytes(request_document)
    assert set(request_document) == set(runner._REQUEST_KEYS) == set(
        builder._REQUEST_KEYS
    )
    assert request_document["chain_request_path"] == str(built.chain_request_path)
    assert request_document["chain_request_sha256"] == built.chain_request_sha256
    assert request_document["live_cash_authorized"] is False
    allowed_roots = tuple(Path(value) for value in request_document["allowed_read_roots"])
    assert authority_fixture.artifact in allowed_roots
    assert not builder._inside(authority_fixture.artifact, authority_fixture.candidate)
    assert not builder._inside(authority_fixture.candidate, authority_fixture.artifact)
    assert "request_path" not in request_document
    assert "request_sha256" not in request_document
    loaded = runner.load_activation_runner_request(
        request_path=built.activation_request_path,
        request_sha256=built.activation_request_sha256,
    )
    loaded_chain = chain._load_chain_request(
        request_path=built.chain_request_path,
        request_sha256=built.chain_request_sha256,
        activation_request=loaded,
    )
    assert loaded_chain == chain_document

    receipt_raw = built.receipt_path.read_bytes()
    receipt = json.loads(receipt_raw)
    assert _sha(receipt_raw) == built.receipt_sha256
    assert RUNTIME_SECRET.encode() not in receipt_raw
    assert RUNTIME_SECRET.encode() not in chain_raw
    assert RUNTIME_SECRET.encode() not in request_raw
    assert receipt["live_cash_authorized"] is False
    assert receipt["invoked"] is False
    assert receipt["broker_contacted"] is False
    assert receipt["host_state_mutated"] is False
    assert receipt["argv_is_shell_string"] is False
    assert built.validate_only_argv[-2:] == ("--mode", "ValidateOnly")
    assert built.validate_only_argv[:4] == (
        str(Path(sys.executable).resolve(strict=True)),
        "-I",
        "-S",
        "-B",
    )
    assert built.activate_paper_argv[-4:] == (
        "--mode",
        "ActivatePaper",
        "--confirm-fake-money-paper",
        builder.ACTIVATE_CONFIRMATION,
    )
    assert _git(authority_fixture.candidate, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize(
    ("left", "right"),
    (
        ("artifact_root", "candidate_root"),
        ("candidate_root", "artifact_root"),
        ("python_dependency_root", "candidate_root"),
        ("legacy_root", "artifact_root"),
    ),
)
def test_security_domains_reject_overlap_in_both_directions(
    authority_fixture: AuthorityFixture,
    left: str,
    right: str,
) -> None:
    nested = authority_fixture.root / f"overlap-{left}-{right}"
    nested.mkdir()
    child = nested / "child"
    child.mkdir()
    overrides: dict[str, Path] = {left: child, right: nested}
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as caught:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs(**overrides)
        )
    assert caught.value.code == "SECURITY_ROOT_OVERLAP"


@pytest.mark.parametrize(
    "candidate_value",
    (
        r"\\server\share\candidate",
        r"\\?\C:\candidate",
        r"\\.\C:\candidate",
    ),
)
def test_nonlocal_device_paths_reject_before_any_executable_probe(
    authority_fixture: AuthorityFixture,
    monkeypatch: pytest.MonkeyPatch,
    candidate_value: str,
) -> None:
    monkeypatch.setattr(
        builder,
        "_authoritative_executables",
        lambda: (_ for _ in ()).throw(AssertionError("probe ran")),
    )
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as caught:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs(candidate_root=candidate_value)
        )
    assert caught.value.code == "NONLOCAL_PATH"


def test_ads_and_reparse_paths_reject_before_any_executable_probe(
    authority_fixture: AuthorityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        builder,
        "_authoritative_executables",
        lambda: (_ for _ in ()).throw(AssertionError("probe ran")),
    )
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as ads:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs(
                candidate_root=f"{authority_fixture.candidate}:alternate"
            )
        )
    assert ads.value.code == "ADS_PATH_FORBIDDEN"

    link = authority_fixture.root / "candidate-link"
    try:
        link.symlink_to(authority_fixture.candidate, target_is_directory=True)
    except OSError:
        pytest.skip("host policy does not permit a test symlink")
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as reparse:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs(candidate_root=link)
        )
    assert reparse.value.code == "REPARSE_PATH_FORBIDDEN"


def test_dirty_untracked_and_ignored_importable_payloads_fail_closed(
    authority_fixture: AuthorityFixture,
) -> None:
    untracked = authority_fixture.candidate / "untracked.txt"
    untracked.write_text("drift", encoding="utf-8")
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as dirty:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs()
        )
    assert dirty.value.code == "WORKTREE_DIRTY"
    untracked.unlink()

    ignore = authority_fixture.candidate / ".gitignore"
    ignore.write_text("ignored-danger.py\n", encoding="utf-8")
    _git(authority_fixture.candidate, "add", ".gitignore")
    _git(authority_fixture.candidate, "commit", "-m", "ignore test payload")
    (authority_fixture.candidate / "ignored-danger.py").write_text(
        "raise RuntimeError('must never import')\n", encoding="utf-8"
    )
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as ignored:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs()
        )
    assert ignored.value.code == "IGNORED_EXECUTABLE_PAYLOAD"


def test_input_drift_after_real_loader_roundtrip_fails_closed(
    authority_fixture: AuthorityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = builder._revalidate_with_real_loaders

    def drift_after_loaders(**kwargs: Any) -> None:
        real(**kwargs)
        authority_fixture.runtime_env.write_text(
            f"CHILI_ALPACA_API_SECRET={RUNTIME_SECRET}-drift\n", encoding="utf-8"
        )

    monkeypatch.setattr(builder, "_revalidate_with_real_loaders", drift_after_loaders)
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as caught:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs()
        )
    assert caught.value.code == "FILE_DRIFT"


def test_valid_looking_ignored_python_cache_cannot_execute_during_build(
    authority_fixture: AuthorityFixture,
) -> None:
    ignore = authority_fixture.candidate / ".gitignore"
    ignore.write_text("__pycache__/\n", encoding="utf-8")
    _git(authority_fixture.candidate, "add", ".gitignore")
    _git(authority_fixture.candidate, "commit", "-m", "ignore ordinary bytecode")

    source = authority_fixture.candidate / "scripts/run_captured_paper_operator_chain.py"
    marker = authority_fixture.root / "MALICIOUS_PYC_EXECUTED"
    cached = Path(importlib.util.cache_from_source(str(source)))
    cached.parent.mkdir(parents=True, exist_ok=True)
    malicious = compile(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        str(source),
        "exec",
    )
    metadata = source.stat()
    header = (
        importlib.util.MAGIC_NUMBER
        + struct.pack("<I", 0)
        + struct.pack("<II", int(metadata.st_mtime), int(metadata.st_size))
    )
    cached.write_bytes(header + marshal.dumps(malicious))
    assert not marker.exists()

    built = builder.build_captured_paper_activation_authority(
        **authority_fixture.kwargs()
    )

    assert built.receipt_path.is_file()
    assert not marker.exists()


def test_private_publication_failure_leaves_no_final_or_pending_and_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    raw = b'{"paper":true}'
    digest = _sha(raw)
    real_fsync_directory = builder._fsync_directory

    def fail_after_link(path: Path) -> None:
        target = path / f"{digest}.json"
        if target.exists():
            raise builder.CapturedPaperActivationAuthorityError(
                "INJECTED_DURABILITY_FAILURE", "test"
            )
        real_fsync_directory(path)

    monkeypatch.setattr(builder, "_fsync_directory", fail_after_link)
    with pytest.raises(builder.CapturedPaperActivationAuthorityError):
        builder._publish_new_json(artifact, kind="receipt", raw=raw)
    parent = artifact / "authority" / "receipt" / digest[:2]
    assert not (parent / f"{digest}.json").exists()
    assert not tuple(parent.glob(".pending-*"))

    monkeypatch.setattr(builder, "_fsync_directory", real_fsync_directory)
    target, observed = builder._publish_new_json(
        artifact, kind="activation-request", raw=raw
    )
    assert observed == digest
    assert target.read_bytes() == raw
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as conflict:
        builder._publish_new_json(artifact, kind="activation-request", raw=raw)
    assert conflict.value.code == "APPEND_ONLY_CONFLICT"
    assert target.read_bytes() == raw
    assert not tuple(target.parent.glob(".pending-*"))


@pytest.mark.parametrize("field", ("runtime_env_path", "resource_benchmark_path"))
def test_hash_bound_inputs_inside_artifact_root_are_rejected(
    authority_fixture: AuthorityFixture,
    field: str,
) -> None:
    source = (
        authority_fixture.runtime_env
        if field == "runtime_env_path"
        else authority_fixture.benchmark
    )
    unsafe = authority_fixture.artifact / source.name
    shutil.copy2(source, unsafe)
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as caught:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs(**{field: unsafe.resolve(strict=True)})
        )
    assert caught.value.code == "INPUT_OUTPUT_OVERLAP"


@pytest.mark.parametrize(
    ("overrides", "code"),
    (
        ({"expected_account_id": "not-a-uuid"}, "ACCOUNT_ID_INVALID"),
        ({"test_database_name": "production"}, "TEST_DATABASE_INVALID"),
        (
            {
                "bridge_configuration": {
                    "iqfeed_l1": {"api_key": "forbidden"},
                    "iqfeed_l2": {},
                }
            },
            "SECRET_INPUT_FORBIDDEN",
        ),
        (
            {
                "bridge_configuration": {
                    "iqfeed_l1": {"endpoint": "Bearer abc123"},
                    "iqfeed_l2": {},
                }
            },
            "SECRET_INPUT_FORBIDDEN",
        ),
        (
            {
                "bridge_configuration": {
                    "iqfeed_l1": {"endpoint": "https://user:pass@localhost/feed"},
                    "iqfeed_l2": {},
                }
            },
            "SECRET_INPUT_FORBIDDEN",
        ),
        (
            {
                "bridge_configuration": {
                    "iqfeed_l1": {"endpoint": "https://localhost/feed?api_key=x"},
                    "iqfeed_l2": {},
                }
            },
            "SECRET_INPUT_FORBIDDEN",
        ),
        (
            {
                "bridge_configuration": {
                    "iqfeed_l1": {"payload": "-----BEGIN PRIVATE KEY-----abc"},
                    "iqfeed_l2": {},
                }
            },
            "SECRET_INPUT_FORBIDDEN",
        ),
        (
            {
                "bridge_configuration": {
                    "iqfeed_l1": {"value": "bad\x00value"},
                    "iqfeed_l2": {},
                }
            },
            "BRIDGE_CONFIGURATION_INVALID",
        ),
        (
            {
                "bridge_configuration": {
                    "iqfeed_l1": {"value": "x" * (64 * 1024 + 1)},
                    "iqfeed_l2": {},
                }
            },
            "BRIDGE_CONFIGURATION_INVALID",
        ),
        (
            {
                "bridge_configuration": {
                    "iqfeed_l1": {
                        "nested": {
                            "nested": {
                                "nested": {
                                    "nested": {
                                        "nested": {
                                            "nested": {
                                                "nested": {
                                                    "nested": {
                                                        "nested": {
                                                            "nested": {
                                                                "nested": {
                                                                    "nested": {
                                                                        "nested": {
                                                                            "nested": {
                                                                                "nested": {
                                                                                    "nested": {
                                                                                        "nested": {}
                                                                                    }
                                                                                }
                                                                            }
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "iqfeed_l2": {},
                }
            },
            "BRIDGE_CONFIGURATION_INVALID",
        ),
    ),
)
def test_malformed_identity_database_and_secret_configuration_reject(
    authority_fixture: AuthorityFixture,
    overrides: Mapping[str, Any],
    code: str,
) -> None:
    with pytest.raises(builder.CapturedPaperActivationAuthorityError) as caught:
        builder.build_captured_paper_activation_authority(
            **authority_fixture.kwargs(**dict(overrides))
        )
    assert caught.value.code == code
