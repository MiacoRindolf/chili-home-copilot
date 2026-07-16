from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural.adaptive_risk_policy import (
    ADAPTIVE_RISK_POLICY_SETTING_BINDINGS,
    AdaptiveRiskPolicy,
)

from scripts.captured_paper_runtime_env import (
    CapturedPaperRuntimeEnvError,
    install_captured_paper_runtime_environment,
    validate_installed_captured_paper_settings,
)


ACCOUNT_ID = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
IQFEED_BUILD = "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"


def _env(tmp_path: Path, body: str) -> tuple[Path, str]:
    path = tmp_path / "runtime.env"
    path.write_text(body, encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _complete_env(tmp_path: Path, extra: str = "") -> tuple[Path, str]:
    return _env(
        tmp_path,
        "DATABASE_URL=postgresql://local/test\n"
        "CHILI_ALPACA_API_KEY=paper-key\n"
        "CHILI_ALPACA_API_SECRET=paper-secret\n"
        f"CHILI_ALPACA_EXPECTED_ACCOUNT_ID={ACCOUNT_ID}\n"
        "CHILI_AUTOTRADER_USER_ID=7\n"
        f"CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD={IQFEED_BUILD}\n"
        "CHILI_ALPACA_LIVE_API_KEY=must-not-load\n"
        "CHILI_ALPACA_LIVE_API_SECRET=must-not-load\n"
        "CHILI_MOMENTUM_PAPER_RUNNER_ENABLED=true\n"
        "CHILI_MOMENTUM_AUTO_ARM_CRYPTO_ONLY=true\n"
        "CHILI_MOMENTUM_RISK_LOSS_FRACTION_OF_EQUITY=0.01\n"
        f"{extra}",
    )


def test_installs_equity_only_candidate_and_excludes_every_live_credential(
    tmp_path: Path,
) -> None:
    path, digest = _complete_env(tmp_path)
    target = {
        "CHILI_ALPACA_LIVE_API_KEY": "parent-live",
        "APCA_API_KEY_ID": "parent-sdk-live",
        "COINBASE_API_KEY": "parent-cash",
        "UNRELATED": "preserved",
    }
    receipt = install_captured_paper_runtime_environment(
        path,
        expected_env_sha256=digest,
        expected_account_id=ACCOUNT_ID,
        environ=target,
    )

    assert target["CHILI_ALPACA_PAPER"] == "true"
    assert target["CHILI_ALPACA_EXPECTED_ACCOUNT_ID"] == ACCOUNT_ID
    assert target["CHILI_CAPTURED_PAPER_CONFIG_ISOLATED"] == "true"
    assert target["CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER"] == "true"
    assert target["CHILI_MOMENTUM_AUTO_ARM_EQUITY_ONLY"] == "true"
    assert target["CHILI_MOMENTUM_AUTO_ARM_CRYPTO_ONLY"] == "false"
    assert target["CHILI_MOMENTUM_PAPER_RUNNER_ENABLED"] == "false"
    assert target["CHILI_MOMENTUM_LIVE_RUNNER_ENABLED"] == "true"
    assert target["CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED"] == "true"
    assert target["CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED"] == "true"
    assert target["IQFEED_NOTIFY_ENABLED"] == "1"
    assert target["IQFEED_NOTIFY_CHANNEL"] == "momentum_iqfeed_l1"
    assert (
        target["CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_CHANNEL"]
        == target["IQFEED_NOTIFY_CHANNEL"]
    )
    assert target["CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD"] == IQFEED_BUILD
    assert target["CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED"] == "false"
    assert target["CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED"] == "false"
    assert target["CHILI_MOMENTUM_FIRST_DIP_RECLAIM_POLICY_MODE"] == "candidate"
    assert target["CHILI_MOMENTUM_SHORT_ENABLED"] == "false"
    assert target["CHILI_MOMENTUM_SHORT_LANE_ENABLED"] == "false"
    assert target["CHILI_ALPACA_QUOTES_VIA_IQFEED"] == "false"
    assert target["CHILI_EQUITY_EXECUTION_RAIL"] == "alpaca"
    assert target["CHILI_AUTOTRADER_USER_ID"] == "7"
    assert target["UNRELATED"] == "preserved"
    assert "CHILI_ALPACA_LIVE_API_KEY" not in target
    assert "CHILI_ALPACA_LIVE_API_SECRET" not in target
    assert "APCA_API_KEY_ID" not in target
    assert "COINBASE_API_KEY" not in target
    rendered = str(receipt.to_dict())
    assert "paper-secret" not in rendered
    assert "paper-key" not in rendered
    assert "postgresql://local/test" not in rendered
    assert len(receipt.configuration_sha256) == 64

    clean_parent: dict[str, str] = {}
    clean_receipt = install_captured_paper_runtime_environment(
        path,
        expected_env_sha256=digest,
        expected_account_id=ACCOUNT_ID,
        environ=clean_parent,
    )
    assert clean_receipt.configuration_sha256 == receipt.configuration_sha256


def test_hash_mismatch_and_duplicate_assignment_fail_before_install(
    tmp_path: Path,
) -> None:
    path, digest = _complete_env(tmp_path)
    target = {
        "UNCHANGED": "yes",
        "CHILI_ALPACA_LIVE_API_KEY": "must-remain-on-validation-failure",
    }
    with pytest.raises(CapturedPaperRuntimeEnvError, match="hash mismatch"):
        install_captured_paper_runtime_environment(
            path,
            expected_env_sha256="0" * 64,
            expected_account_id=ACCOUNT_ID,
            environ=target,
        )
    assert target == {
        "UNCHANGED": "yes",
        "CHILI_ALPACA_LIVE_API_KEY": "must-remain-on-validation-failure",
    }

    duplicate, duplicate_digest = _complete_env(
        tmp_path,
        extra="CHILI_ALPACA_API_KEY=other\n",
    )
    with pytest.raises(CapturedPaperRuntimeEnvError, match="duplicate key"):
        install_captured_paper_runtime_environment(
            duplicate,
            expected_env_sha256=duplicate_digest,
            expected_account_id=ACCOUNT_ID,
            environ={},
        )

    quoted_duplicate, quoted_digest = _complete_env(
        tmp_path,
        extra="'CHILI_ALPACA_API_KEY'=other\n",
    )
    with pytest.raises(CapturedPaperRuntimeEnvError, match="duplicate key"):
        install_captured_paper_runtime_environment(
            quoted_duplicate,
            expected_env_sha256=quoted_digest,
            expected_account_id=ACCOUNT_ID,
            environ={},
        )


def test_hash_bound_account_uuid_must_match_install_authority(
    tmp_path: Path,
) -> None:
    path, digest = _complete_env(tmp_path)
    target = {"UNCHANGED": "yes"}

    with pytest.raises(CapturedPaperRuntimeEnvError, match="does not match"):
        install_captured_paper_runtime_environment(
            path,
            expected_env_sha256=digest,
            expected_account_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            environ=target,
        )

    assert target == {"UNCHANGED": "yes"}


def test_runtime_environment_rejects_lexical_symlink_before_hash_read(
    tmp_path: Path,
) -> None:
    path, digest = _complete_env(tmp_path)
    linked = tmp_path / "linked-runtime.env"
    linked.symlink_to(path)

    with pytest.raises(CapturedPaperRuntimeEnvError, match="reparse link"):
        install_captured_paper_runtime_environment(
            linked,
            expected_env_sha256=digest,
            expected_account_id=ACCOUNT_ID,
            environ={},
        )


def test_missing_paper_credentials_and_non_candidate_policy_fail_closed(
    tmp_path: Path,
) -> None:
    path, digest = _env(tmp_path, "DATABASE_URL=postgresql://local/test\n")
    with pytest.raises(CapturedPaperRuntimeEnvError, match="inputs are missing"):
        install_captured_paper_runtime_environment(
            path,
            expected_env_sha256=digest,
            expected_account_id=ACCOUNT_ID,
            environ={},
        )

    complete, complete_digest = _complete_env(tmp_path)
    with pytest.raises(CapturedPaperRuntimeEnvError, match="candidate policy"):
        install_captured_paper_runtime_environment(
            complete,
            expected_env_sha256=complete_digest,
            expected_account_id=ACCOUNT_ID,
            first_dip_policy_mode="baseline",
            environ={},
        )


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ("", "bridge build is invalid"),
        (
            "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD=unreviewed\n",
            "bridge build is invalid",
        ),
        (
            "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD="
            "iqfeed-l1-quote-provenance-v2+sha256:0123456789abcdef\n",
            "bridge build is invalid",
        ),
        (
            f"CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD={IQFEED_BUILD}\n"
            "IQFEED_NOTIFY_CHANNEL=bridge_channel\n"
            "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_CHANNEL="
            "listener_channel\n",
            "channels do not match",
        ),
        (
            f"CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD={IQFEED_BUILD}\n"
            "IQFEED_NOTIFY_CHANNEL=MixedCase\n"
            "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_CHANNEL=MixedCase\n",
            "channels do not match",
        ),
    ],
)
def test_iqfeed_notify_contract_fails_before_environment_install(
    tmp_path: Path,
    extra: str,
    error: str,
) -> None:
    path, digest = _env(
        tmp_path,
        "DATABASE_URL=postgresql://local/test\n"
        "CHILI_ALPACA_API_KEY=paper-key\n"
        "CHILI_ALPACA_API_SECRET=paper-secret\n"
        f"CHILI_ALPACA_EXPECTED_ACCOUNT_ID={ACCOUNT_ID}\n"
        "CHILI_AUTOTRADER_USER_ID=7\n"
        f"{extra}",
    )
    target = {"UNCHANGED": "yes"}

    with pytest.raises(CapturedPaperRuntimeEnvError, match=error):
        install_captured_paper_runtime_environment(
            path,
            expected_env_sha256=digest,
            expected_account_id=ACCOUNT_ID,
            environ=target,
        )

    assert target == {"UNCHANGED": "yes"}


def test_parsed_settings_projection_rechecks_every_execution_boundary(
    tmp_path: Path,
) -> None:
    path, digest = _complete_env(tmp_path)
    target: dict[str, str] = {}
    receipt = install_captured_paper_runtime_environment(
        path,
        expected_env_sha256=digest,
        expected_account_id=ACCOUNT_ID,
        environ=target,
    )
    values = {
        "chili_alpaca_enabled": True,
        "chili_alpaca_paper": True,
        "chili_alpaca_expected_account_id": ACCOUNT_ID,
        "chili_alpaca_quotes_via_iqfeed": False,
        "chili_equity_execution_rail": "alpaca",
        "chili_momentum_equity_execution_via_alpaca_paper": True,
        "chili_momentum_crypto_execution_via_alpaca_paper": False,
        "chili_momentum_auto_arm_crypto_only": False,
        "chili_momentum_auto_arm_equity_only": True,
        "chili_momentum_paper_runner_enabled": False,
        "chili_momentum_paper_runner_scheduler_enabled": False,
        "chili_momentum_paper_runner_dev_tick_enabled": False,
        "chili_momentum_live_runner_enabled": True,
        "chili_momentum_live_runner_scheduler_enabled": False,
        "chili_momentum_live_runner_loop_enabled": True,
        "chili_momentum_live_runner_loop_iqfeed_notify_enabled": True,
        "chili_momentum_live_runner_loop_iqfeed_notify_channel": (
            "momentum_iqfeed_l1"
        ),
        "chili_iqfeed_l1_authoritative_bridge_build": IQFEED_BUILD,
        "chili_momentum_live_runner_dev_tick_enabled": False,
        "chili_momentum_auto_arm_live_enabled": True,
        "chili_momentum_auto_arm_live_scheduler_enabled": False,
        "chili_autopilot_price_bus_enabled": True,
        "chili_momentum_first_dip_reclaim_policy_mode": "candidate",
        "chili_momentum_short_enabled": False,
        "chili_momentum_short_lane_enabled": False,
        "chili_autotrader_user_id": 7,
        "chili_alpaca_api_key": "paper-key",
        "chili_alpaca_api_secret": "paper-secret",
        "chili_alpaca_live_api_key": "",
        "chili_alpaca_live_api_secret": "",
        "chili_momentum_captured_paper_action_claim_lease_seconds": 30,
        "chili_momentum_captured_paper_outbox_max_attempts": 3,
        "chili_momentum_captured_paper_outbox_max_reconciliation_attempts": 3,
        "chili_momentum_captured_paper_reconciliation_retry_delay_seconds": 5,
        "chili_momentum_captured_paper_reconciliation_health_escalation_seconds": 30,
        "chili_momentum_captured_paper_time_in_force": "day",
        "chili_momentum_captured_paper_extended_hours": True,
        "chili_momentum_captured_paper_worker_idle_poll_seconds": 0.25,
        "chili_momentum_captured_paper_trigger_max_attempts": 3,
        "chili_momentum_captured_paper_trigger_retry_delay_seconds": 0.01,
        "chili_momentum_captured_paper_trigger_future_tolerance_seconds": 1.0,
        "chili_momentum_captured_paper_trigger_exact_print_window_seconds": 0.001,
    }
    policy = AdaptiveRiskPolicy(
        policy_version="shared-v1",
        policy_source="test:settings",
        risk_fraction_of_equity=0.01,
        daily_risk_fraction_of_equity=0.05,
        portfolio_risk_fraction_of_equity=0.10,
        cluster_risk_fraction_of_equity=0.04,
        symbol_risk_fraction_of_equity=0.03,
        daily_gap_reserve_fraction_of_equity=0.001,
        max_notional_fraction_of_equity=0.15,
        max_buying_power_fraction_for_notional=0.50,
        max_portfolio_gross_fraction_of_equity=2.0,
        quality_multiplier_floor=0.50,
        quality_multiplier_ceiling=1.50,
        volatility_reference_fraction=0.05,
        volatility_multiplier_floor=0.40,
        spread_reserve_multiple=1.0,
        per_share_gap_reserve_volatility_multiple=0.10,
        max_adv_participation=0.01,
        max_recent_volume_participation=0.10,
        max_executable_depth_participation=0.50,
        market_data_max_age_seconds=2.0,
        account_data_max_age_seconds=10.0,
        reservation_data_max_age_seconds=0.25,
        context_data_max_age_seconds=60.0,
    )
    policy_values = asdict(policy)
    values.update(
        {
            setting_name: policy_values[policy_field]
            for policy_field, setting_name in ADAPTIVE_RISK_POLICY_SETTING_BINDINGS
        }
    )
    projection = validate_installed_captured_paper_settings(
        SimpleNamespace(**values), receipt, environ=target
    )
    assert len(projection["settings_projection_sha256"]) == 64
    assert projection["adaptive_risk_policy"]["policy_sha256"] == (
        policy.policy_sha256
    )
    assert projection["captured_paper_config_isolated"] is True
    assert projection["settings"]["chili_autopilot_price_bus_enabled"] is True
    assert projection["settings"]["chili_autotrader_user_id"] == 7
    assert (
        projection["settings"]["chili_iqfeed_l1_authoritative_bridge_build"]
        == IQFEED_BUILD
    )
    assert projection["captured_paper_operational_policy"] == {
        "chili_momentum_captured_paper_action_claim_lease_seconds": 30,
        "chili_momentum_captured_paper_extended_hours": True,
        "chili_momentum_captured_paper_outbox_max_attempts": 3,
        "chili_momentum_captured_paper_outbox_max_reconciliation_attempts": 3,
        "chili_momentum_captured_paper_reconciliation_health_escalation_seconds": 30,
        "chili_momentum_captured_paper_reconciliation_retry_delay_seconds": 5,
        "chili_momentum_captured_paper_time_in_force": "day",
        "chili_momentum_captured_paper_worker_idle_poll_seconds": 0.25,
        "chili_momentum_captured_paper_trigger_exact_print_window_seconds": 0.001,
        "chili_momentum_captured_paper_trigger_future_tolerance_seconds": 1.0,
        "chili_momentum_captured_paper_trigger_max_attempts": 3,
        "chili_momentum_captured_paper_trigger_retry_delay_seconds": 0.01,
    }
    assert "paper-key" not in str(dict(projection))

    values["chili_momentum_auto_arm_equity_only"] = False
    with pytest.raises(CapturedPaperRuntimeEnvError, match="activation posture"):
        validate_installed_captured_paper_settings(
            SimpleNamespace(**values), receipt, environ=target
        )

    values["chili_momentum_auto_arm_equity_only"] = True
    values["chili_equity_execution_rail"] = "alpaca"
    target["IQFEED_NOTIFY_ENABLED"] = "0"
    with pytest.raises(CapturedPaperRuntimeEnvError, match="notify admission is disabled"):
        validate_installed_captured_paper_settings(
            SimpleNamespace(**values), receipt, environ=target
        )

    target["IQFEED_NOTIFY_ENABLED"] = "1"
    target["IQFEED_NOTIFY_CHANNEL"] = "foreign_channel"
    with pytest.raises(CapturedPaperRuntimeEnvError, match="channels do not match"):
        validate_installed_captured_paper_settings(
            SimpleNamespace(**values), receipt, environ=target
        )

    target["IQFEED_NOTIFY_CHANNEL"] = "momentum_iqfeed_l1"
    values["chili_equity_execution_rail"] = "robinhood_spot"
    with pytest.raises(CapturedPaperRuntimeEnvError, match="activation posture"):
        validate_installed_captured_paper_settings(
            SimpleNamespace(**values), receipt, environ=target
        )


@pytest.mark.parametrize("raw", ["", "0", "-1", "+7", "07", "abc", "2147483648"])
def test_missing_or_noncanonical_autotrader_user_fails_before_install(
    tmp_path: Path,
    raw: str,
) -> None:
    path, digest = _env(
        tmp_path,
        "DATABASE_URL=postgresql://local/test\n"
        "CHILI_ALPACA_API_KEY=paper-key\n"
        "CHILI_ALPACA_API_SECRET=paper-secret\n"
        f"CHILI_ALPACA_EXPECTED_ACCOUNT_ID={ACCOUNT_ID}\n"
        f"CHILI_AUTOTRADER_USER_ID={raw}\n",
    )
    target = {
        "UNCHANGED": "yes",
        "CHILI_ALPACA_LIVE_API_KEY": "must-remain-on-validation-failure",
    }
    with pytest.raises(CapturedPaperRuntimeEnvError, match="AUTOTRADER_USER_ID"):
        install_captured_paper_runtime_environment(
            path,
            expected_env_sha256=digest,
            expected_account_id=ACCOUNT_ID,
            environ=target,
        )
    assert target == {
        "UNCHANGED": "yes",
        "CHILI_ALPACA_LIVE_API_KEY": "must-remain-on-validation-failure",
    }


def test_parsed_autotrader_user_drift_fails_closed(
    tmp_path: Path,
) -> None:
    path, digest = _complete_env(tmp_path)
    target: dict[str, str] = {}
    receipt = install_captured_paper_runtime_environment(
        path,
        expected_env_sha256=digest,
        expected_account_id=ACCOUNT_ID,
        environ=target,
    )
    values = {
        "chili_autotrader_user_id": 8,
        "chili_alpaca_api_key": "paper-key",
        "chili_alpaca_api_secret": "paper-secret",
        "chili_alpaca_live_api_key": "",
        "chili_alpaca_live_api_secret": "",
    }
    with pytest.raises(CapturedPaperRuntimeEnvError, match="activation posture"):
        validate_installed_captured_paper_settings(
            SimpleNamespace(**values), receipt, environ=target
        )
