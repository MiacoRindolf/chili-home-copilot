from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
import pytest

from scripts import build_captured_paper_runtime_env as builder
from scripts.captured_paper_runtime_env import (
    install_captured_paper_runtime_environment,
    validate_installed_captured_paper_settings,
)


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
ORTEX_STRATEGY_POLICY = {
    "CHILI_MOMENTUM_SQUEEZE_FUEL_TILT_ENABLED": "true",
    "CHILI_MOMENTUM_SQUEEZE_FUEL_TOP_N": "17",
    "CHILI_MOMENTUM_FAKE_CATALYST_GUARD_ENABLED": "true",
    "CHILI_MOMENTUM_SQUEEZE_ENTRY_SIZEUP_ENABLED": "true",
    "CHILI_MOMENTUM_SQUEEZE_ENTRY_TOP_PCTL": "0.81",
    "CHILI_MOMENTUM_SQUEEZE_ENTRY_MAX_MULT": "1.41",
    "CHILI_MOMENTUM_SQUEEZE_EXIT_HOLD_ENABLED": "true",
    "CHILI_MOMENTUM_SQUEEZE_EXIT_TAIL_PCTL": "0.91",
    "CHILI_MOMENTUM_SQUEEZE_EXIT_MAX_WIDEN": "1.31",
    "CHILI_MOMENTUM_KELLY_CONVICTION_ENABLED": "true",
    "CHILI_MOMENTUM_KELLY_CONVICTION_MAX_MULTIPLIER": "1.42",
    "CHILI_MOMENTUM_KELLY_CONVICTION_GAIN": "0.92",
    "CHILI_MOMENTUM_KELLY_CONVICTION_W_SQUEEZE": "0.45",
    "CHILI_MOMENTUM_KELLY_CONVICTION_W_OFI": "0.35",
    "CHILI_MOMENTUM_KELLY_CONVICTION_W_NEWS": "0.20",
    "CHILI_MOMENTUM_SUB_VWAP_TRAP_ENTRY_ENABLED": "true",
    "CHILI_MOMENTUM_BAIL_ON_NO_CONFIRMATION_ENABLED": "true",
    "CHILI_MOMENTUM_CATALYST_ARB_FLAT_GATE_ENABLED": "true",
    "CHILI_MOMENTUM_TICK_BREAK_TAPE_CONFIRM_ENABLED": "true",
    "CHILI_MOMENTUM_FLUSH_DIP_VOLUME_GATE_ENABLED": "true",
    "CHILI_MOMENTUM_ROSS_STOP_ALIGNMENT_ENABLED": "true",
    "CHILI_MOMENTUM_ORB_IHS_STRUCTURAL_STOP_ENABLED": "true",
    "CHILI_MOMENTUM_FRESH_IGNITION_REENTRY_BYPASS_ENABLED": "true",
    "CHILI_MOMENTUM_UNIVERSE_FLOAT_GATE_ENABLED": "true",
}
WINDOWS_DACL_API_AVAILABLE = os.name == "nt" and all(
    importlib.util.find_spec(module_name) is not None
    for module_name in ("ntsecuritycon", "win32api", "win32con", "win32security")
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


@pytest.mark.skipif(
    not WINDOWS_DACL_API_AVAILABLE,
    reason="captured PAPER env publication requires the Windows DACL API",
)
def test_exact_projection_carries_hash_bound_public_ortex_policy(
    tmp_path: Path,
) -> None:
    policy = {
        "CHILI_ORTEX_MONTHLY_REQUEST_LIMIT": "1000",
        "CHILI_ORTEX_REQUEST_INTERVAL_SECONDS": "1.25",
        "CHILI_ORTEX_RESERVATION_LEASE_SECONDS": "45",
        "CHILI_ORTEX_RESPONSE_MAX_BYTES": "524288",
        "CHILI_ORTEX_SUCCESS_CACHE_TTL_SECONDS": "43200",
        "CHILI_ORTEX_TRANSIENT_BACKOFF_BASE_SECONDS": "3",
        "CHILI_ORTEX_TRANSIENT_BACKOFF_MAX_SECONDS": "180",
    }
    source, source_sha256 = _write_source(
        tmp_path,
        _source_text(
            extra="".join(f"{key}={value}\n" for key, value in policy.items())
        ),
    )

    receipt, output = _build(
        tmp_path,
        source=source,
        source_sha256=source_sha256,
    )
    parsed = {
        str(key): str(value)
        for key, value in dotenv_values(output, interpolate=False).items()
    }

    assert {key: parsed[key] for key in policy} == policy
    assert receipt.output_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    rendered_receipt = json.dumps(receipt.to_dict())
    assert all(value not in rendered_receipt for value in SECRET_VALUES)


@pytest.mark.skipif(
    not WINDOWS_DACL_API_AVAILABLE,
    reason="captured PAPER env publication requires the Windows DACL API",
)
def test_default_on_ortex_requires_credential_but_explicit_off_may_omit_it(
    tmp_path: Path,
) -> None:
    without_key = _source_text().replace(
        f"CHILI_ORTEX_API_KEY='{SECRET_VALUES[5]}'\n",
        "",
    )
    source, source_sha256 = _write_source(tmp_path, without_key)
    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError,
        match="requires its captured PAPER credential",
    ):
        _build(tmp_path, source=source, source_sha256=source_sha256)

    explicitly_off = (
        without_key + "CHILI_MOMENTUM_SQUEEZE_FUEL_TILT_ENABLED=false\n"
    )
    source, source_sha256 = _write_source(tmp_path, explicitly_off)
    receipt, output = _build(
        tmp_path,
        source=source,
        source_sha256=source_sha256,
    )
    parsed = {
        str(key): str(value)
        for key, value in dotenv_values(output, interpolate=False).items()
    }
    assert parsed["CHILI_MOMENTUM_SQUEEZE_FUEL_TILT_ENABLED"] == "false"
    assert "CHILI_ORTEX_API_KEY" not in parsed
    assert receipt.output_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()


@pytest.mark.skipif(
    not WINDOWS_DACL_API_AVAILABLE,
    reason="captured PAPER env publication requires the Windows DACL API",
)
def test_strategy_kill_switch_survives_builder_parse_and_projection_hash(
    tmp_path: Path,
) -> None:
    from app.config import Settings

    def build_projection(
        root: Path, *, squeeze_fuel_enabled: str
    ) -> tuple[dict[str, str], Any, dict[str, object]]:
        root.mkdir(parents=True, exist_ok=True)
        output = root / "output" / "captured-paper.env"
        output.unlink(missing_ok=True)
        strategy = {
            **ORTEX_STRATEGY_POLICY,
            "CHILI_MOMENTUM_SQUEEZE_FUEL_TILT_ENABLED": squeeze_fuel_enabled,
        }
        source, source_sha256 = _write_source(
            root,
            _source_text(
                extra="".join(f"{key}={value}\n" for key, value in strategy.items())
            ),
        )
        receipt, output = _build(
            root,
            source=source,
            source_sha256=source_sha256,
            output=output,
        )
        parsed = {
            str(key): str(value)
            for key, value in dotenv_values(output, interpolate=False).items()
        }
        installed: dict[str, str] = {}
        runtime_receipt = install_captured_paper_runtime_environment(
            output,
            expected_env_sha256=receipt.output_sha256,
            expected_account_id=ACCOUNT_ID,
            environ=installed,
        )
        settings = Settings(_env_file=None, **installed)
        projection = dict(
            validate_installed_captured_paper_settings(
                settings,
                runtime_receipt,
                environ=installed,
            )
        )
        return parsed, settings, projection

    enabled_parsed, enabled_settings, enabled_projection = build_projection(
        tmp_path,
        squeeze_fuel_enabled="true",
    )
    disabled_parsed, disabled_settings, disabled_projection = build_projection(
        tmp_path,
        squeeze_fuel_enabled="false",
    )

    assert {
        key: disabled_parsed[key]
        for key in ORTEX_STRATEGY_POLICY
    } == {
        **ORTEX_STRATEGY_POLICY,
        "CHILI_MOMENTUM_SQUEEZE_FUEL_TILT_ENABLED": "false",
    }
    assert enabled_settings.chili_momentum_squeeze_fuel_tilt_enabled is True
    assert disabled_settings.chili_momentum_squeeze_fuel_tilt_enabled is False
    enabled_policy = enabled_projection["captured_paper_operational_policy"]
    disabled_policy = disabled_projection["captured_paper_operational_policy"]
    assert isinstance(enabled_policy, dict)
    assert isinstance(disabled_policy, dict)
    assert {
        key
        for key in enabled_policy
        if enabled_policy[key] != disabled_policy[key]
    } == {"chili_momentum_squeeze_fuel_tilt_enabled"}
    assert enabled_projection["settings_projection_sha256"] != (
        disabled_projection["settings_projection_sha256"]
    )
    assert enabled_parsed["CHILI_MOMENTUM_SQUEEZE_FUEL_TILT_ENABLED"] == "true"


@pytest.mark.skipif(
    not WINDOWS_DACL_API_AVAILABLE,
    reason="captured PAPER env publication requires the Windows DACL API",
)
def test_all_nine_operator_kill_switches_round_trip_false(
    tmp_path: Path,
) -> None:
    from app.config import Settings

    operator_switches = {
        "CHILI_MOMENTUM_SUB_VWAP_TRAP_ENTRY_ENABLED": (
            "chili_momentum_sub_vwap_trap_entry_enabled"
        ),
        "CHILI_MOMENTUM_BAIL_ON_NO_CONFIRMATION_ENABLED": (
            "chili_momentum_bail_on_no_confirmation_enabled"
        ),
        "CHILI_MOMENTUM_CATALYST_ARB_FLAT_GATE_ENABLED": (
            "chili_momentum_catalyst_arb_flat_gate_enabled"
        ),
        "CHILI_MOMENTUM_TICK_BREAK_TAPE_CONFIRM_ENABLED": (
            "chili_momentum_tick_break_tape_confirm_enabled"
        ),
        "CHILI_MOMENTUM_FLUSH_DIP_VOLUME_GATE_ENABLED": (
            "chili_momentum_flush_dip_volume_gate_enabled"
        ),
        "CHILI_MOMENTUM_ROSS_STOP_ALIGNMENT_ENABLED": (
            "chili_momentum_ross_stop_alignment_enabled"
        ),
        "CHILI_MOMENTUM_ORB_IHS_STRUCTURAL_STOP_ENABLED": (
            "chili_momentum_orb_ihs_structural_stop_enabled"
        ),
        "CHILI_MOMENTUM_FRESH_IGNITION_REENTRY_BYPASS_ENABLED": (
            "chili_momentum_fresh_ignition_reentry_bypass_enabled"
        ),
        "CHILI_MOMENTUM_UNIVERSE_FLOAT_GATE_ENABLED": (
            "chili_momentum_universe_float_gate_enabled"
        ),
    }
    strategy = {
        **ORTEX_STRATEGY_POLICY,
        **{name: "false" for name in operator_switches},
    }
    source, source_sha256 = _write_source(
        tmp_path,
        _source_text(
            extra="".join(f"{key}={value}\n" for key, value in strategy.items())
        ),
    )
    receipt, output = _build(
        tmp_path,
        source=source,
        source_sha256=source_sha256,
    )
    installed: dict[str, str] = {}
    runtime_receipt = install_captured_paper_runtime_environment(
        output,
        expected_env_sha256=receipt.output_sha256,
        expected_account_id=ACCOUNT_ID,
        environ=installed,
    )
    settings = Settings(_env_file=None, **installed)
    projection = validate_installed_captured_paper_settings(
        settings,
        runtime_receipt,
        environ=installed,
    )
    policy = projection["captured_paper_operational_policy"]
    assert isinstance(policy, dict)
    for env_name, setting_name in operator_switches.items():
        assert installed[env_name] == "false"
        assert getattr(settings, setting_name) is False
        assert policy[setting_name] is False


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


def _read_output_acl(path: Path) -> tuple[set[str], set[str]]:
    """Return (allow-ACE SID strings, non-allow ACE type names) for *path*."""

    import win32security

    dacl = win32security.GetFileSecurity(
        str(path), win32security.DACL_SECURITY_INFORMATION
    ).GetSecurityDescriptorDacl()
    assert dacl is not None
    allow_sids: set[str] = set()
    other_types: set[str] = set()
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE:
            allow_sids.add(win32security.ConvertSidToStringSid(ace[2]))
        else:
            other_types.add(str(ace[0][0]))
    return allow_sids, other_types


def _private_sids() -> set[str]:
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    operator = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    return {
        win32security.ConvertSidToStringSid(operator),
        "S-1-5-18",  # SYSTEM
        "S-1-5-32-544",  # Administrators
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL enforcement")
def test_published_output_acl_is_private_and_protected(tmp_path: Path) -> None:
    _receipt, output = _build(tmp_path)

    allow_sids, other_types = _read_output_acl(output)
    assert other_types == set()
    assert allow_sids == _private_sids()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL enforcement")
def test_existing_output_with_loosened_acl_is_resealed(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32security

    source, digest = _write_source(tmp_path)
    _first, output = _build(tmp_path, source=source, source_sha256=digest)

    # Loosen out of band: grant Authenticated Users read, unprotected DACL.
    authenticated = win32security.CreateWellKnownSid(
        win32security.WinAuthenticatedUserSid, None
    )
    loose = win32security.ACL()
    loose.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, authenticated
    )
    for sid_text in _private_sids():
        loose.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_ALL_ACCESS,
            win32security.ConvertStringSidToSid(sid_text),
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, loose, 0)
    win32security.SetFileSecurity(
        str(output), win32security.DACL_SECURITY_INFORMATION, descriptor
    )
    loosened, _ = _read_output_acl(output)
    assert loosened != _private_sids()

    _second, same_output = _build(
        tmp_path, source=source, source_sha256=digest, output=output
    )
    assert same_output == output
    allow_sids, other_types = _read_output_acl(output)
    assert other_types == set()
    assert allow_sids == _private_sids()


def test_acl_enforcement_failure_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def rejecting_enforcement(path: Path) -> None:
        calls.append(path)
        raise builder.CapturedPaperRuntimeEnvBuildError(
            "synthetic ACL rejection", code="ACL_ENFORCEMENT_FAILED"
        )

    monkeypatch.setattr(
        builder, "_enforce_private_output_acl", rejecting_enforcement
    )
    output = tmp_path / "output" / "captured-paper.env"

    with pytest.raises(
        builder.CapturedPaperRuntimeEnvBuildError, match="synthetic ACL rejection"
    ) as raised:
        _build(tmp_path, output=output)

    assert raised.value.code == "ACL_ENFORCEMENT_FAILED"
    assert calls
    assert not output.exists()
    assert list(output.parent.glob(".*.pending")) == []


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
