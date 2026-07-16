from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
import pytest

from scripts import build_captured_paper_runtime_env as builder


ACCOUNT_ID = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
IQFEED_BUILD = "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"
CHANNEL = "momentum_iqfeed_l1"
SECRET_VALUES = (
    "postgresql://paper-user:db-secret@localhost/chili",
    "paper-api-key-sensitive",
    "paper-api-secret-sensitive",
    "massive-secret-sensitive",
    "polygon-secret-sensitive",
    "ortex-secret-sensitive",
    "live-cash-secret-must-not-copy",
    "generic-broker-secret-must-not-copy",
)


def _source_text(*, data_feed: str = "iex", extra: str = "") -> str:
    return (
        f"DATABASE_URL='{SECRET_VALUES[0]}'\n"
        f"CHILI_ALPACA_API_KEY='{SECRET_VALUES[1]}'\n"
        f"CHILI_ALPACA_API_SECRET='{SECRET_VALUES[2]}'\n"
        f"CHILI_ALPACA_DATA_FEED={data_feed}\n"
        "CHILI_AUTOTRADER_USER_ID=7\n"
        f"MASSIVE_API_KEY='{SECRET_VALUES[3]}'\n"
        f"POLYGON_API_KEY='{SECRET_VALUES[4]}'\n"
        f"CHILI_ORTEX_API_KEY='{SECRET_VALUES[5]}'\n"
        f"CHILI_ALPACA_LIVE_API_SECRET='{SECRET_VALUES[6]}'\n"
        f"APCA_API_SECRET_KEY='{SECRET_VALUES[7]}'\n"
        "CHILI_ALPACA_EXPECTED_ACCOUNT_ID=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n"
        "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD=untrusted-source-build\n"
        "IQFEED_NOTIFY_CHANNEL=UntrustedSourceChannel\n"
        "TEST_DATABASE_URL=postgresql://must/not/copy\n"
        "CHILI_MOMENTUM_PAPER_RUNNER_ENABLED=true\n"
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=true\n"
        "CHILI_MOMENTUM_MAX_POSITION_USD=50\n"
        "CHILI_MOMENTUM_DAILY_LOSS_CAP_USD=250\n"
        "CHILI_MOMENTUM_MAX_CONCURRENT_SYMBOLS=1\n"
        "UNKNOWN_AUTHORITY=must-not-copy\n"
        f"{extra}"
    )


def _write_source(tmp_path: Path, body: str | None = None) -> tuple[Path, str]:
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    path = inputs / "desktop-source.env"
    path.write_text(body if body is not None else _source_text(), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _build(
    tmp_path: Path,
    *,
    source: Path | None = None,
    source_sha256: str | None = None,
    output: Path | None = None,
    account_id: str = ACCOUNT_ID,
    bridge_build: str = IQFEED_BUILD,
    channel: str = CHANNEL,
) -> tuple[builder.CapturedPaperRuntimeEnvBuildReceipt, Path]:
    if source is None:
        source, observed_sha = _write_source(tmp_path)
    else:
        observed_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    output_root = tmp_path / "output"
    output_root.mkdir(exist_ok=True)
    target = output or output_root / "captured-paper.env"
    receipt = builder.build_captured_paper_runtime_env(
        source,
        expected_source_sha256=source_sha256 or observed_sha,
        output_env=target,
        expected_account_id=account_id,
        iqfeed_bridge_build=bridge_build,
        iqfeed_notify_channel=channel,
        allow_read_roots=[source.parent],
        allow_write_roots=[target.parent],
    )
    return receipt, target


def test_exact_projection_excludes_live_flags_magic_caps_test_db_and_unknowns(
    tmp_path: Path,
) -> None:
    receipt, output = _build(tmp_path)
    parsed = {
        str(key): str(value)
        for key, value in dotenv_values(output, interpolate=False).items()
    }

    assert parsed == {
        "DATABASE_URL": SECRET_VALUES[0],
        "CHILI_ALPACA_API_KEY": SECRET_VALUES[1],
        "CHILI_ALPACA_API_SECRET": SECRET_VALUES[2],
        "CHILI_ALPACA_DATA_FEED": "iex",
        "CHILI_AUTOTRADER_USER_ID": "7",
        "MASSIVE_API_KEY": SECRET_VALUES[3],
        "POLYGON_API_KEY": SECRET_VALUES[4],
        "CHILI_ORTEX_API_KEY": SECRET_VALUES[5],
        "CHILI_ALPACA_EXPECTED_ACCOUNT_ID": ACCOUNT_ID,
        "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD": IQFEED_BUILD,
        "IQFEED_NOTIFY_CHANNEL": CHANNEL,
    }
    assert set(receipt.to_dict()) == {
        "schema_version",
        "source_sha256",
        "output_sha256",
        "secret_fingerprints",
    }
    assert receipt.output_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert set(receipt.secret_fingerprints) == {
        "DATABASE_URL",
        "CHILI_ALPACA_API_KEY",
        "CHILI_ALPACA_API_SECRET",
        "MASSIVE_API_KEY",
        "POLYGON_API_KEY",
        "CHILI_ORTEX_API_KEY",
    }
    rendered_receipt = json.dumps(receipt.to_dict())
    assert all(secret not in rendered_receipt for secret in SECRET_VALUES)
    assert list(output.parent.glob("*.pending")) == []
    assert list(output.parent.glob(".*.pending")) == []


def test_runtime_validation_uses_an_isolated_mapping_and_precedes_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_install = builder.runtime_env.install_captured_paper_runtime_environment
    observations: dict[str, Any] = {}

    def observed_install(*args: Any, **kwargs: Any) -> Any:
        target = kwargs["environ"]
        observations["mapping"] = target
        observations["initial"] = dict(target)
        result = real_install(*args, **kwargs)
        observations["effective"] = dict(target)
        return result

    monkeypatch.setattr(
        builder.runtime_env,
        "install_captured_paper_runtime_environment",
        observed_install,
    )
    _receipt, output = _build(tmp_path)

    assert observations["mapping"] is not os.environ
    assert observations["initial"] == {}
    assert observations["effective"]["CHILI_ALPACA_PAPER"] == "true"
    assert observations["effective"]["CHILI_ALPACA_EXPECTED_ACCOUNT_ID"] == ACCOUNT_ID
    assert output.exists()

    output.unlink()

    def reject_install(*_args: Any, **_kwargs: Any) -> None:
        raise builder.runtime_env.CapturedPaperRuntimeEnvError("synthetic rejection")

    monkeypatch.setattr(
        builder.runtime_env,
        "install_captured_paper_runtime_environment",
        reject_install,
    )
    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError,
        match="isolated runtime validation",
    ):
        _build(tmp_path, output=output)
    assert not output.exists()
    assert list(output.parent.glob(".*.pending")) == []

    def mismatched_receipt_install(*args: Any, **kwargs: Any) -> Any:
        receipt = real_install(*args, **kwargs)
        return replace(
            receipt,
            expected_account_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )

    monkeypatch.setattr(
        builder.runtime_env,
        "install_captured_paper_runtime_environment",
        mismatched_receipt_install,
    )
    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError,
        match="receipt identity",
    ):
        _build(tmp_path, output=output)
    assert not output.exists()
    assert list(output.parent.glob(".*.pending")) == []


def test_hash_drift_and_duplicate_assignments_fail_before_output(
    tmp_path: Path,
) -> None:
    source, original_sha = _write_source(tmp_path)
    source.write_text(_source_text(extra="IGNORED_AFTER_PIN=changed\n"), encoding="utf-8")
    output_root = tmp_path / "output"
    output_root.mkdir()
    output = output_root / "captured-paper.env"

    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError, match="hash mismatch"
    ):
        _build(
            tmp_path,
            source=source,
            source_sha256=original_sha,
            output=output,
        )
    assert not output.exists()

    duplicate_secret = "duplicate-secret-never-render"
    source, digest = _write_source(
        tmp_path,
        _source_text(extra=f"CHILI_ALPACA_API_SECRET={duplicate_secret}\n"),
    )
    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError, match="duplicate assignment"
    ) as raised:
        _build(
            tmp_path,
            source=source,
            source_sha256=digest,
            output=output,
        )
    assert duplicate_secret not in str(raised.value)
    assert not output.exists()


def test_quoted_duplicate_curated_key_is_rejected_without_secret_leak(
    tmp_path: Path,
) -> None:
    duplicate_secret = "quoted-duplicate-secret-never-render"
    source, digest = _write_source(
        tmp_path,
        _source_text(extra=f"'CHILI_ALPACA_API_SECRET'={duplicate_secret}\n"),
    )

    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError, match="duplicate assignment"
    ) as raised:
        _build(tmp_path, source=source, source_sha256=digest)

    assert duplicate_secret not in str(raised.value)


def test_duplicate_unrelated_assignments_are_ignored_not_projected(
    tmp_path: Path,
) -> None:
    raw = _source_text(
        extra=(
            "CHILI_UNRELATED_DESKTOP_FLAG=false\n"
            "CHILI_UNRELATED_DESKTOP_FLAG=true\n"
        )
    )
    source, source_sha = _write_source(tmp_path, raw)
    receipt, output = _build(
        tmp_path, source=source, source_sha256=source_sha
    )

    assert receipt.output_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert b"CHILI_UNRELATED_DESKTOP_FLAG" not in output.read_bytes()


def test_same_bytes_are_idempotent_and_different_bytes_never_overwrite(
    tmp_path: Path,
) -> None:
    source, digest = _write_source(tmp_path)
    first, output = _build(tmp_path, source=source, source_sha256=digest)
    original = output.read_bytes()
    original_stat = output.stat()

    second, same_output = _build(
        tmp_path,
        source=source,
        source_sha256=digest,
        output=output,
    )
    assert same_output == output
    assert second == first
    assert output.read_bytes() == original
    assert output.stat().st_mtime_ns == original_stat.st_mtime_ns

    source.write_text(_source_text(data_feed="sip"), encoding="utf-8")
    changed_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError, match="overwrite is forbidden"
    ):
        _build(
            tmp_path,
            source=source,
            source_sha256=changed_sha,
            output=output,
        )
    assert output.read_bytes() == original


@pytest.mark.parametrize(
    ("account_id", "bridge_build", "channel", "error"),
    [
        (ACCOUNT_ID.upper(), IQFEED_BUILD, CHANNEL, "lower-case UUID"),
        (
            ACCOUNT_ID,
            "iqfeed-l1-quote-provenance-v2+sha256:0123456789abcdef",
            CHANNEL,
            "exact v3",
        ),
        (ACCOUNT_ID, IQFEED_BUILD, "MixedCase", "lower-case PostgreSQL"),
    ],
)
def test_supplied_identity_and_listener_inputs_are_exact_and_lower_case(
    tmp_path: Path,
    account_id: str,
    bridge_build: str,
    channel: str,
    error: str,
) -> None:
    with pytest.raises(builder.CapturedPaperRuntimeEnvBuildError, match=error):
        _build(
            tmp_path,
            account_id=account_id,
            bridge_build=bridge_build,
            channel=channel,
        )


def test_cli_stdout_and_errors_never_expose_secrets_or_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, digest = _write_source(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    output = output_root / "captured-paper.env"
    arguments = [
        "--source-env",
        str(source),
        "--source-sha256",
        digest,
        "--output-env",
        str(output),
        "--expected-account-id",
        ACCOUNT_ID,
        "--iqfeed-bridge-build",
        IQFEED_BUILD,
        "--iqfeed-notify-channel",
        CHANNEL,
        "--allow-read-root",
        str(source.parent),
        "--allow-write-root",
        str(output.parent),
    ]

    assert builder.main(arguments) == 0
    success = capsys.readouterr()
    report = json.loads(success.out)
    assert set(report) == {
        "schema_version",
        "source_sha256",
        "output_sha256",
        "secret_fingerprints",
    }
    combined = success.out + success.err
    assert str(source) not in combined
    assert str(output) not in combined
    assert all(secret not in combined for secret in SECRET_VALUES)

    output.unlink()
    source.write_text(
        _source_text(extra="CHILI_ALPACA_API_KEY=cli-duplicate-secret\n"),
        encoding="utf-8",
    )
    arguments[3] = hashlib.sha256(source.read_bytes()).hexdigest()
    assert builder.main(arguments) == 2
    failure = capsys.readouterr()
    error_report = json.loads(failure.out)
    assert error_report == {
        "environment_published": False,
        "error_code": "DUPLICATE_SOURCE_KEY",
    }
    combined = failure.out + failure.err
    assert "cli-duplicate-secret" not in combined
    assert all(secret not in combined for secret in SECRET_VALUES)
    assert not output.exists()


def test_cli_rejects_duplicate_scalar_security_argument(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, digest = _write_source(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    output = output_root / "captured-paper.env"
    arguments = [
        "--source-env",
        str(source),
        "--source-sha256",
        digest,
        "--output-env",
        str(output),
        "--expected-account-id",
        ACCOUNT_ID,
        "--expected-account-id",
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "--iqfeed-bridge-build",
        IQFEED_BUILD,
        "--iqfeed-notify-channel",
        CHANNEL,
        "--allow-read-root",
        str(source.parent),
        "--allow-write-root",
        str(output.parent),
    ]

    assert builder.main(arguments) == 2
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "environment_published": False,
        "error_code": "DUPLICATE_SECURITY_ARGUMENT",
    }
    assert not output.exists()


def test_cli_rejects_unknown_argument_without_echoing_its_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_shaped_path = r"C:\synthetic\secret-shaped-path.env"

    assert builder.main(["--unknown-runtime-path", secret_shaped_path]) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report == {
        "environment_published": False,
        "error_code": "INVALID_ARGUMENTS",
    }
    assert secret_shaped_path not in captured.out + captured.err


def test_read_write_allowlists_fail_closed(tmp_path: Path) -> None:
    source, digest = _write_source(tmp_path)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError, match="allow-read"
    ):
        builder.build_captured_paper_runtime_env(
            source,
            expected_source_sha256=digest,
            output_env=allowed / "runtime.env",
            expected_account_id=ACCOUNT_ID,
            iqfeed_bridge_build=IQFEED_BUILD,
            iqfeed_notify_channel=CHANNEL,
            allow_read_roots=[outside],
            allow_write_roots=[allowed],
        )

    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError, match="allow-write"
    ):
        builder.build_captured_paper_runtime_env(
            source,
            expected_source_sha256=digest,
            output_env=outside / "runtime.env",
            expected_account_id=ACCOUNT_ID,
            iqfeed_bridge_build=IQFEED_BUILD,
            iqfeed_notify_channel=CHANNEL,
            allow_read_roots=[source.parent],
            allow_write_roots=[allowed],
        )


def test_builder_import_surface_has_no_external_io_clients() -> None:
    tree = ast.parse(Path(builder.__file__).read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert {
        "psycopg",
        "requests",
        "socket",
        "sqlalchemy",
        "subprocess",
    }.isdisjoint(imports)
