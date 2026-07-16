from __future__ import annotations

from dataclasses import asdict, fields
import hashlib
import inspect
import json

from pydantic import ValidationError
import pytest

from app.config import (
    CAPTURED_PAPER_CONFIG_ISOLATION_ENV,
    Settings,
    load_process_settings,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    ADAPTIVE_RISK_POLICY_SETTING_BINDINGS,
    ADAPTIVE_RISK_POLICY_SETTINGS_SCHEMA_VERSION,
    AdaptiveRiskContractError,
    AdaptiveRiskPolicy,
    adaptive_risk_policy_settings_projection,
    build_adaptive_risk_policy_from_settings,
)


EXPECTED_BINDINGS = (
    ("policy_version", "chili_momentum_adaptive_risk_policy_version"),
    ("policy_source", "chili_momentum_adaptive_risk_policy_source"),
    ("risk_fraction_of_equity", "chili_momentum_risk_loss_fraction_of_equity"),
    (
        "daily_risk_fraction_of_equity",
        "chili_momentum_risk_daily_loss_fraction_of_equity",
    ),
    (
        "portfolio_risk_fraction_of_equity",
        "chili_momentum_risk_concurrent_open_risk_fraction",
    ),
    (
        "cluster_risk_fraction_of_equity",
        "chili_momentum_adaptive_risk_cluster_fraction_of_equity",
    ),
    (
        "symbol_risk_fraction_of_equity",
        "chili_momentum_adaptive_risk_symbol_fraction_of_equity",
    ),
    (
        "daily_gap_reserve_fraction_of_equity",
        "chili_momentum_adaptive_risk_daily_gap_reserve_fraction_of_equity",
    ),
    (
        "max_notional_fraction_of_equity",
        "chili_momentum_risk_notional_fraction_of_equity",
    ),
    (
        "max_buying_power_fraction_for_notional",
        "chili_momentum_adaptive_risk_max_buying_power_fraction_for_notional",
    ),
    (
        "max_portfolio_gross_fraction_of_equity",
        "chili_momentum_adaptive_risk_max_portfolio_gross_fraction_of_equity",
    ),
    (
        "quality_multiplier_floor",
        "chili_momentum_adaptive_risk_quality_multiplier_floor",
    ),
    (
        "quality_multiplier_ceiling",
        "chili_momentum_adaptive_risk_quality_multiplier_ceiling",
    ),
    (
        "volatility_reference_fraction",
        "chili_momentum_adaptive_risk_volatility_reference_fraction",
    ),
    (
        "volatility_multiplier_floor",
        "chili_momentum_adaptive_risk_volatility_multiplier_floor",
    ),
    (
        "spread_reserve_multiple",
        "chili_momentum_adaptive_risk_spread_reserve_multiple",
    ),
    (
        "per_share_gap_reserve_volatility_multiple",
        "chili_momentum_adaptive_risk_gap_reserve_volatility_multiple",
    ),
    (
        "max_adv_participation",
        "chili_momentum_risk_liquidity_participation_fraction",
    ),
    (
        "max_recent_volume_participation",
        "chili_momentum_adaptive_risk_recent_volume_participation",
    ),
    (
        "max_executable_depth_participation",
        "chili_momentum_adaptive_risk_executable_depth_participation",
    ),
    (
        "market_data_max_age_seconds",
        "chili_momentum_adaptive_risk_market_data_max_age_seconds",
    ),
    (
        "account_data_max_age_seconds",
        "chili_momentum_adaptive_risk_account_data_max_age_seconds",
    ),
    (
        "reservation_data_max_age_seconds",
        "chili_momentum_adaptive_risk_reservation_data_max_age_seconds",
    ),
    (
        "context_data_max_age_seconds",
        "chili_momentum_adaptive_risk_context_data_max_age_seconds",
    ),
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "chili_momentum_adaptive_risk_policy_version": "test-shared-policy-v7",
        "chili_momentum_adaptive_risk_policy_source": "test:sealed-settings",
        "chili_momentum_risk_loss_fraction_of_equity": 0.012,
        "chili_momentum_risk_daily_loss_fraction_of_equity": 0.06,
        "chili_momentum_risk_concurrent_open_risk_fraction": 0.08,
        "chili_momentum_adaptive_risk_cluster_fraction_of_equity": 0.035,
        "chili_momentum_adaptive_risk_symbol_fraction_of_equity": 0.022,
        "chili_momentum_adaptive_risk_daily_gap_reserve_fraction_of_equity": 0.002,
        "chili_momentum_risk_notional_fraction_of_equity": 0.17,
        "chili_momentum_adaptive_risk_max_buying_power_fraction_for_notional": 0.45,
        "chili_momentum_adaptive_risk_max_portfolio_gross_fraction_of_equity": 1.75,
        "chili_momentum_adaptive_risk_quality_multiplier_floor": 0.60,
        "chili_momentum_adaptive_risk_quality_multiplier_ceiling": 1.40,
        "chili_momentum_adaptive_risk_volatility_reference_fraction": 0.04,
        "chili_momentum_adaptive_risk_volatility_multiplier_floor": 0.35,
        "chili_momentum_adaptive_risk_spread_reserve_multiple": 1.20,
        "chili_momentum_adaptive_risk_gap_reserve_volatility_multiple": 0.12,
        "chili_momentum_risk_liquidity_participation_fraction": 0.015,
        "chili_momentum_adaptive_risk_recent_volume_participation": 0.11,
        "chili_momentum_adaptive_risk_executable_depth_participation": 0.55,
        "chili_momentum_adaptive_risk_market_data_max_age_seconds": 1.75,
        "chili_momentum_adaptive_risk_account_data_max_age_seconds": 9.0,
        "chili_momentum_adaptive_risk_reservation_data_max_age_seconds": 0.20,
        "chili_momentum_adaptive_risk_context_data_max_age_seconds": 45.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_builder_maps_every_policy_field_and_hashes_named_settings() -> None:
    assert ADAPTIVE_RISK_POLICY_SETTING_BINDINGS == EXPECTED_BINDINGS
    assert {field.name for field in fields(AdaptiveRiskPolicy)} == {
        field_name for field_name, _setting_name in EXPECTED_BINDINGS
    }

    settings = _settings()
    receipt = build_adaptive_risk_policy_from_settings(settings)
    projection = receipt.to_settings_projection()

    assert projection["schema_version"] == ADAPTIVE_RISK_POLICY_SETTINGS_SCHEMA_VERSION
    assert projection["policy_field_bindings"] == dict(EXPECTED_BINDINGS)
    assert projection["policy_snapshot"] == asdict(receipt.policy)
    assert projection["policy_sha256"] == receipt.policy.policy_sha256
    assert projection["settings"] == {
        setting_name: getattr(receipt.policy, policy_field)
        for policy_field, setting_name in EXPECTED_BINDINGS
    }
    unsigned = dict(projection)
    digest = unsigned.pop("settings_projection_sha256")
    assert digest == _canonical_sha256(unsigned)
    assert digest == receipt.settings_projection_sha256

    # Existing equity-relative strategy settings remain the authoritative
    # values rather than a captured-paper-specific replacement.
    assert receipt.policy.risk_fraction_of_equity == 0.012
    assert receipt.policy.daily_risk_fraction_of_equity == 0.06
    assert receipt.policy.portfolio_risk_fraction_of_equity == 0.08
    assert receipt.policy.max_notional_fraction_of_equity == 0.17
    assert receipt.policy.max_adv_participation == 0.015


def test_documented_defaults_preserve_current_equity_fraction_knobs() -> None:
    defaults = Settings.model_fields
    assert defaults["chili_momentum_risk_loss_fraction_of_equity"].default == 0.01
    assert (
        defaults["chili_momentum_risk_daily_loss_fraction_of_equity"].default
        == 0.05
    )
    assert (
        defaults["chili_momentum_risk_concurrent_open_risk_fraction"].default
        == 0.10
    )
    assert (
        defaults["chili_momentum_risk_notional_fraction_of_equity"].default
        == 0.15
    )
    assert (
        defaults["chili_momentum_risk_liquidity_participation_fraction"].default
        == 0.01
    )


def test_invalid_setting_and_cross_field_policy_fail_closed() -> None:
    with pytest.raises(ValidationError):
        _settings(
            chili_momentum_adaptive_risk_market_data_max_age_seconds=0.0,
        )

    # The existing setting accepts zero for legacy disable/fallback behavior,
    # but the shared adaptive policy does not: it fails at the builder boundary.
    with pytest.raises(AdaptiveRiskContractError, match="risk_fraction_of_equity"):
        build_adaptive_risk_policy_from_settings(
            _settings(chili_momentum_risk_loss_fraction_of_equity=0.0)
        )

    with pytest.raises(
        AdaptiveRiskContractError,
        match="ceiling cannot be below",
    ):
        build_adaptive_risk_policy_from_settings(
            _settings(
                chili_momentum_adaptive_risk_quality_multiplier_floor=1.6,
                chili_momentum_adaptive_risk_quality_multiplier_ceiling=1.4,
            )
        )


def test_replay_and_captured_paper_use_identical_policy_projection() -> None:
    # Execution surface is deliberately absent from the builder API.  Two
    # independently parsed but equal settings objects must produce identical
    # replay/PAPER policy and provenance bytes.
    replay_settings = _settings()
    paper_settings = _settings()

    replay_receipt = build_adaptive_risk_policy_from_settings(replay_settings)
    paper_receipt = build_adaptive_risk_policy_from_settings(paper_settings)

    assert replay_receipt.policy == paper_receipt.policy
    assert (
        replay_receipt.to_settings_projection()
        == paper_receipt.to_settings_projection()
        == adaptive_risk_policy_settings_projection(paper_settings)
    )
    assert list(inspect.signature(build_adaptive_risk_policy_from_settings).parameters) == [
        "settings_obj"
    ]


def test_builder_cannot_bind_magic_dollar_or_one_symbol_activation_caps() -> None:
    bound_setting_names = {
        setting_name for _policy_field, setting_name in EXPECTED_BINDINGS
    }
    forbidden_settings = {
        "chili_momentum_risk_max_loss_per_trade_usd",
        "chili_momentum_risk_max_daily_loss_usd",
        "chili_momentum_risk_max_notional_per_trade_usd",
        "chili_momentum_max_open_positions_ceiling",
        "chili_momentum_max_open_positions_per_correlation_bucket",
    }
    assert bound_setting_names.isdisjoint(forbidden_settings)
    assert all(not name.endswith("_usd") for name in bound_setting_names)
    assert all("open_positions" not in name for name in bound_setting_names)

    builder_source = inspect.getsource(build_adaptive_risk_policy_from_settings)
    for forbidden_literal in ("$50", "$250", "50.0", "250.0", "one_symbol"):
        assert forbidden_literal not in builder_source


def test_captured_paper_marker_prevents_desktop_env_file_reload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "CHILI_MOMENTUM_RISK_LOSS_FRACTION_OF_EQUITY=0.77\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(
        "CHILI_MOMENTUM_RISK_LOSS_FRACTION_OF_EQUITY",
        raising=False,
    )
    monkeypatch.delenv(CAPTURED_PAPER_CONFIG_ISOLATION_ENV, raising=False)

    assert load_process_settings().chili_momentum_risk_loss_fraction_of_equity == 0.77

    monkeypatch.setenv(CAPTURED_PAPER_CONFIG_ISOLATION_ENV, "true")
    assert load_process_settings().chili_momentum_risk_loss_fraction_of_equity == 0.01

    monkeypatch.setenv(CAPTURED_PAPER_CONFIG_ISOLATION_ENV, "TRUE")
    with pytest.raises(RuntimeError, match="must be exactly 'true'"):
        load_process_settings()
