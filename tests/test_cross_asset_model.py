"""Phase L.19 pure-model unit tests (no DB, no network).

Covers deterministic id, each lead/lag scalar + sign, divergence
detection, partial coverage, all-missing, beta window + zero-variance,
config thresholds, and frozen dataclass immutability.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from app.services.trading.cross_asset_model import (
    ALL_SYMBOLS,
    CROSS_ASSET_DIVERGENCE,
    CROSS_ASSET_NEUTRAL,
    CROSS_ASSET_RISK_OFF,
    CROSS_ASSET_RISK_ON,
    LEAD_MISSING,
    LEAD_NEUTRAL,
    LEAD_RISK_OFF,
    LEAD_RISK_ON,
    SYMBOL_BTC,
    SYMBOL_ETH,
    SYMBOL_HYG,
    SYMBOL_LQD,
    SYMBOL_SPY,
    SYMBOL_TLT,
    SYMBOL_UUP,
    AssetLeg,
    CrossAssetConfig,
    CrossAssetInput,
    compute_cross_asset,
    compute_snapshot_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _leg(
    symbol: str,
    *,
    ret_5d: float | None = 0.0,
    ret_20d: float | None = 0.0,
    ret_1d: float | None = 0.0,
    missing: bool = False,
    close: float | None = 100.0,
    daily_returns: tuple[float, ...] = (),
) -> AssetLeg:
    return AssetLeg(
        symbol=symbol,
        last_close=(None if missing else close),
        ret_1d=(None if missing else ret_1d),
        ret_5d=(None if missing else ret_5d),
        ret_20d=(None if missing else ret_20d),
        missing=missing,
        returns_daily=daily_returns,
    )


def _build_input(
    *,
    as_of: date = date(2026, 4, 16),
    # Defaults picked to sit clearly outside threshold boundaries so
    # float-precision doesn't flip labels.
    equity_ret5: float = 0.03,       # SPY clearly up
    tlt_ret5: float = -0.02,         # TLT clearly down (risk-on)
    hy_ret5: float = 0.05,           # HY clearly above IG + SPY
    ig_ret5: float = 0.01,           # IG mild positive
    uup_ret5: float = -0.02,         # USD clearly down
    btc_ret5: float = 0.06,          # BTC clearly up
    eth_ret5: float = 0.07,
    vix_pct: float | None = 0.30,
    vix_level: float | None = 14.0,
    breadth_ratio: float | None = 0.62,
    breadth_label: str | None = "broad_risk_on",
    macro_label: str | None = "risk_on",
    missing: dict[str, bool] | None = None,
    config: CrossAssetConfig | None = None,
    btc_returns: tuple[float, ...] = (),
    spy_returns: tuple[float, ...] = (),
) -> CrossAssetInput:
    missing = missing or {}
    return CrossAssetInput(
        as_of_date=as_of,
        equity=_leg(
            SYMBOL_SPY, ret_5d=equity_ret5, ret_20d=equity_ret5 * 2,
            missing=missing.get(SYMBOL_SPY, False),
            daily_returns=spy_returns,
        ),
        rates=_leg(
            SYMBOL_TLT, ret_5d=tlt_ret5, ret_20d=tlt_ret5 * 2,
            missing=missing.get(SYMBOL_TLT, False),
        ),
        credit_hy=_leg(
            SYMBOL_HYG, ret_5d=hy_ret5, ret_20d=hy_ret5 * 2,
            missing=missing.get(SYMBOL_HYG, False),
        ),
        credit_ig=_leg(
            SYMBOL_LQD, ret_5d=ig_ret5, ret_20d=ig_ret5 * 2,
            missing=missing.get(SYMBOL_LQD, False),
        ),
        usd=_leg(
            SYMBOL_UUP, ret_5d=uup_ret5, ret_20d=uup_ret5 * 2,
            missing=missing.get(SYMBOL_UUP, False),
        ),
        crypto_btc=_leg(
            SYMBOL_BTC, ret_5d=btc_ret5, ret_20d=btc_ret5 * 2,
            missing=missing.get(SYMBOL_BTC, False),
            daily_returns=btc_returns,
        ),
        crypto_eth=_leg(
            SYMBOL_ETH, ret_5d=eth_ret5, ret_20d=eth_ret5 * 2,
            missing=missing.get(SYMBOL_ETH, False),
        ),
        vix_level=vix_level,
        vix_percentile=vix_pct,
        breadth_advance_ratio=breadth_ratio,
        breadth_label=breadth_label,
        macro_label=macro_label,
        config=config or CrossAssetConfig(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_compute_snapshot_id_deterministic():
    a = compute_snapshot_id(date(2026, 4, 16))
    b = compute_snapshot_id(date(2026, 4, 16))
    assert a == b
    assert len(a) == 16


def test_compute_snapshot_id_different_dates_differ():
    a = compute_snapshot_id(date(2026, 4, 16))
    b = compute_snapshot_id(date(2026, 4, 17))
    assert a != b


def test_compute_snapshot_id_rejects_non_date():
    with pytest.raises(ValueError):
        compute_snapshot_id("2026-04-16")  # type: ignore[arg-type]


def test_constants_expose_all_symbols():
    assert set(ALL_SYMBOLS) == {
        SYMBOL_SPY, SYMBOL_TLT, SYMBOL_HYG, SYMBOL_LQD,
        SYMBOL_UUP, SYMBOL_BTC, SYMBOL_ETH,
    }


def test_frozen_dataclasses_reject_mutation():
    cfg = CrossAssetConfig()
    leg = _leg(SYMBOL_SPY)
    with pytest.raises(FrozenInstanceError):
        cfg.fast_lead_threshold = 0.99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        leg.symbol = "X"  # type: ignore[misc]


def test_asset_leg_requires_symbol():
    with pytest.raises(ValueError):
        AssetLeg(
            symbol="",
            last_close=None,
            ret_1d=None,
            ret_5d=None,
            ret_20d=None,
            missing=True,
        )


def test_all_agree_risk_on():
    # SPY up 3%, TLT down 2% -> bond diff = -5% (risk-on).
    # Credit: (0.05-0.01)-0.03 = 0.01 -> boundary; use 0.06 to be safe.
    # USD-crypto: -0.02 - 0.06 = -0.08 (risk-on crypto).
    inp = _build_input(hy_ret5=0.06)
    out = compute_cross_asset(inp)
    assert out.bond_equity_label == LEAD_RISK_ON
    # (0.06 - 0.01) - 0.03 = 0.02 >= 0.01 -> RISK_ON
    assert out.credit_equity_label == LEAD_RISK_ON
    assert out.usd_crypto_label == LEAD_RISK_ON
    # 2+ risk_on, 0 risk_off -> composite risk_on_crosscheck
    assert out.cross_asset_label == CROSS_ASSET_RISK_ON
    assert out.cross_asset_numeric == 1


def test_all_agree_risk_off():
    inp = _build_input(
        equity_ret5=-0.03,
        tlt_ret5=0.03,
        # credit: (hy-ig)-spy = (-0.04 - 0.0) - (-0.03) = -0.01 boundary.
        # use -0.05 hy to clear threshold safely.
        hy_ret5=-0.05,
        ig_ret5=0.0,
        uup_ret5=0.03,
        btc_ret5=-0.06,
        # vix 0.95 + breadth 0.4 -> score = 0.35 >= 0.30 -> RISK_OFF
        vix_pct=0.95,
        breadth_ratio=0.40,
        breadth_label="broad_risk_off",
    )
    out = compute_cross_asset(inp)
    assert out.bond_equity_label == LEAD_RISK_OFF
    # (-0.05 - 0.0) - (-0.03) = -0.02 <= -0.01 -> RISK_OFF
    assert out.credit_equity_label == LEAD_RISK_OFF
    assert out.usd_crypto_label == LEAD_RISK_OFF
    assert out.vix_breadth_label == LEAD_RISK_OFF
    assert out.cross_asset_label == CROSS_ASSET_RISK_OFF
    assert out.cross_asset_numeric == -1


def test_divergence_when_labels_conflict():
    # bond risk_off (TLT up, SPY down); crypto risk_on (USD down, BTC up).
    inp = _build_input(
        equity_ret5=-0.02,
        tlt_ret5=0.02,
        hy_ret5=0.0,
        ig_ret5=0.0,
        uup_ret5=-0.02,
        btc_ret5=0.03,
    )
    out = compute_cross_asset(inp)
    assert out.bond_equity_label == LEAD_RISK_OFF
    assert out.usd_crypto_label == LEAD_RISK_ON
    assert out.cross_asset_label == CROSS_ASSET_DIVERGENCE


def test_neutral_when_all_small():
    inp = _build_input(
        equity_ret5=0.002, tlt_ret5=0.001, hy_ret5=0.001, ig_ret5=0.001,
        uup_ret5=0.001, btc_ret5=0.001, eth_ret5=0.001,
        vix_pct=0.40, breadth_ratio=0.50,
    )
    out = compute_cross_asset(inp)
    assert out.cross_asset_label == CROSS_ASSET_NEUTRAL


def test_vix_shock_with_risk_on_breadth_flags_divergence_risk_off():
    inp = _build_input(
        vix_pct=0.90,
        breadth_ratio=0.70,
        equity_ret5=0.0, tlt_ret5=0.0, hy_ret5=0.0, ig_ret5=0.0,
        uup_ret5=0.0, btc_ret5=0.0,
    )
    out = compute_cross_asset(inp)
    # VIX shock + risk-on breadth -> latent risk_off
    assert out.vix_breadth_label == LEAD_RISK_OFF
    # Only one risk_off signal, no risk_on signals, so composite neutral.
    assert out.cross_asset_label == CROSS_ASSET_NEUTRAL


def test_partial_coverage_missing_crypto():
    inp = _build_input(missing={SYMBOL_BTC: True, SYMBOL_ETH: True})
    out = compute_cross_asset(inp)
    assert out.usd_crypto_label == LEAD_MISSING
    assert out.symbols_missing == 2
    assert out.symbols_sampled == 5
    assert abs(out.coverage_score - 5 / 7) < 1e-6


def test_all_missing_yields_zero_coverage_and_neutral():
    inp = _build_input(
        missing={s: True for s in ALL_SYMBOLS},
        vix_pct=None,
        breadth_ratio=None,
    )
    out = compute_cross_asset(inp)
    assert out.symbols_sampled == 0
    assert out.symbols_missing == 7
    assert out.coverage_score == 0.0
    assert out.cross_asset_label == CROSS_ASSET_NEUTRAL
    assert out.bond_equity_label == LEAD_MISSING
    assert out.credit_equity_label == LEAD_MISSING
    assert out.usd_crypto_label == LEAD_MISSING
    assert out.vix_breadth_label == LEAD_MISSING


def test_beta_zero_variance_returns_none():
    # exactly-zero returns -> zero variance -> None (avoids float
    # precision errors that creep in with constant nonzero floats).
    inp = _build_input(
        btc_returns=(0.0,) * 60,
        spy_returns=(0.0,) * 60,
    )
    out = compute_cross_asset(inp)
    assert out.crypto_equity_beta is None
    assert out.crypto_equity_correlation is None


def test_beta_computed_on_sufficient_series():
    btc = tuple(0.02 if i % 2 == 0 else -0.01 for i in range(90))
    spy = tuple(0.01 if i % 2 == 0 else -0.005 for i in range(90))
    inp = _build_input(btc_returns=btc, spy_returns=spy)
    out = compute_cross_asset(inp)
    assert out.crypto_equity_beta is not None
    assert out.crypto_equity_beta_window_days == 60
    # BTC moves 2x SPY, so beta should be ~2.0 and correlation ~1.0
    assert 1.8 < out.crypto_equity_beta < 2.2
    assert out.crypto_equity_correlation is not None
    assert out.crypto_equity_correlation > 0.99


def test_beta_none_when_series_too_short():
    inp = _build_input(
        btc_returns=(0.01, -0.01),
        spy_returns=(0.01, -0.01),
    )
    out = compute_cross_asset(inp)
    assert out.crypto_equity_beta is None


def test_beta_window_clamping_respects_config():
    cfg = CrossAssetConfig(beta_window_days=30)
    btc = tuple(0.02 if i % 2 == 0 else -0.01 for i in range(90))
    spy = tuple(0.01 if i % 2 == 0 else -0.005 for i in range(90))
    inp = _build_input(config=cfg, btc_returns=btc, spy_returns=spy)
    out = compute_cross_asset(inp)
    assert out.crypto_equity_beta_window_days == 30


def test_config_threshold_tighten_moves_labels_to_neutral():
    cfg = CrossAssetConfig(fast_lead_threshold=0.10)
    # Default risk_on case has 3% bond lead - under 10% threshold now.
    inp = _build_input(config=cfg)
    out = compute_cross_asset(inp)
    assert out.bond_equity_label == LEAD_NEUTRAL


def test_composite_min_agreement_tightens_labeling():
    # default is 2; raise to 3 so 2-agree cases stay neutral. Pick
    # hy/ig so credit lands squarely in NEUTRAL.
    cfg = CrossAssetConfig(composite_min_agreement=3)
    inp = _build_input(config=cfg, hy_ret5=0.035, ig_ret5=0.01)
    out = compute_cross_asset(inp)
    # bond: RISK_ON, credit: NEUTRAL, usd-crypto: RISK_ON,
    # vix-breadth: NEUTRAL -> 2 risk_on, not 3 -> neutral composite.
    assert out.credit_equity_label == LEAD_NEUTRAL
    assert out.cross_asset_label == CROSS_ASSET_NEUTRAL


def test_payload_structure_is_frozen_shape():
    out = compute_cross_asset(_build_input())
    assert set(out.payload.keys()) == {"symbols", "context", "config"}
    assert set(out.payload["context"].keys()) == {
        "vix_level", "vix_percentile", "breadth_advance_ratio",
        "breadth_label", "macro_label",
    }
    for sym in ALL_SYMBOLS:
        assert sym in out.payload["symbols"]
        assert set(out.payload["symbols"][sym].keys()) == {
            "last_close", "ret_1d", "ret_5d", "ret_20d", "missing",
        }


def test_snapshot_id_matches_helper():
    out = compute_cross_asset(_build_input(as_of=date(2026, 4, 16)))
    assert out.snapshot_id == compute_snapshot_id(date(2026, 4, 16))


def test_vix_percentile_invalid_maps_to_missing_label():
    inp = _build_input(vix_pct=1.5, breadth_ratio=0.5)
    out = compute_cross_asset(inp)
    assert out.vix_breadth_label == LEAD_MISSING
    assert out.vix_breadth_divergence_score is None
