"""Unit tests for the Phase L.17 macro regime pure model."""
from __future__ import annotations

from datetime import date

import pytest

from app.services.trading.macro_regime_model import (
    ALL_SYMBOLS,
    AssetReading,
    CREDIT_NEUTRAL,
    CREDIT_TIGHTENING,
    CREDIT_WIDENING,
    EquityRegimeInput,
    MACRO_CAUTIOUS,
    MACRO_RISK_OFF,
    MACRO_RISK_ON,
    MacroRegimeConfig,
    MacroRegimeInput,
    RATES_NEUTRAL,
    RATES_RISK_OFF,
    RATES_RISK_ON,
    SYMBOL_HYG,
    SYMBOL_IEF,
    SYMBOL_LQD,
    SYMBOL_SHY,
    SYMBOL_TLT,
    SYMBOL_UUP,
    TREND_DOWN,
    TREND_FLAT,
    TREND_MISSING,
    TREND_UP,
    USD_NEUTRAL,
    USD_STRONG,
    USD_WEAK,
    classify_trend,
    compute_macro_regime,
    compute_regime_id,
)

_DATE = date(2026, 4, 15)


def _reading(
    symbol: str,
    *,
    momentum_20d: float | None,
    trend: str,
    last_close: float = 100.0,
    momentum_5d: float | None = 0.0,
    missing: bool = False,
) -> AssetReading:
    return AssetReading(
        symbol=symbol,
        missing=missing,
        last_close=(None if missing else last_close),
        momentum_20d=(None if missing else momentum_20d),
        momentum_5d=(None if missing else momentum_5d),
        trend=trend,
    )


# ---------------------------------------------------------------------------
# compute_regime_id
# ---------------------------------------------------------------------------


def test_regime_id_deterministic() -> None:
    a = compute_regime_id(_DATE)
    b = compute_regime_id(_DATE)
    assert a == b
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


def test_regime_id_changes_with_date() -> None:
    a = compute_regime_id(_DATE)
    b = compute_regime_id(date(2026, 4, 16))
    assert a != b


def test_regime_id_type_check() -> None:
    with pytest.raises(TypeError):
        compute_regime_id("2026-04-15")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AssetReading invariants
# ---------------------------------------------------------------------------


def test_asset_reading_rejects_invalid_trend() -> None:
    with pytest.raises(ValueError):
        AssetReading(symbol=SYMBOL_IEF, trend="bananas")


# ---------------------------------------------------------------------------
# classify_trend
# ---------------------------------------------------------------------------


def test_classify_trend_thresholds() -> None:
    cfg = MacroRegimeConfig()
    assert classify_trend(0.05, cfg=cfg) == TREND_UP
    assert classify_trend(-0.05, cfg=cfg) == TREND_DOWN
    assert classify_trend(0.0, cfg=cfg) == TREND_FLAT
    assert classify_trend(0.005, cfg=cfg) == TREND_FLAT  # below 1% threshold
    assert classify_trend(None, cfg=cfg) == TREND_MISSING


# ---------------------------------------------------------------------------
# compute_macro_regime - baseline shapes
# ---------------------------------------------------------------------------


def _empty_equity() -> EquityRegimeInput:
    return EquityRegimeInput(
        spy_direction=None,
        spy_momentum_5d=None,
        vix=None,
        vix_regime=None,
        volatility_percentile=None,
        composite=None,
        regime_numeric=None,
    )


def test_no_readings_returns_cautious_zero_coverage() -> None:
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=()
        )
    )
    assert out.macro_label == MACRO_CAUTIOUS
    assert out.macro_numeric == 0
    assert out.symbols_sampled == 0
    assert out.symbols_missing == len(ALL_SYMBOLS)
    assert out.coverage_score == 0.0
    assert out.rates_regime is None
    assert out.credit_regime is None
    assert out.usd_regime is None
    assert out.yield_curve_slope_proxy is None
    assert out.credit_spread_proxy is None
    assert out.uup_momentum_20d is None
    assert out.ief_trend is None


def test_all_flat_returns_cautious_full_coverage() -> None:
    readings = [
        _reading(s, momentum_20d=0.0, trend=TREND_FLAT)
        for s in ALL_SYMBOLS
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    assert out.macro_label == MACRO_CAUTIOUS
    assert out.symbols_sampled == len(ALL_SYMBOLS)
    assert out.symbols_missing == 0
    assert out.coverage_score == 1.0
    assert out.rates_regime == RATES_NEUTRAL
    assert out.credit_regime == CREDIT_NEUTRAL
    assert out.usd_regime == USD_NEUTRAL
    assert out.yield_curve_slope_proxy == 0.0
    assert out.credit_spread_proxy == 0.0
    assert out.uup_momentum_20d == 0.0


# ---------------------------------------------------------------------------
# Risk-on scenario (rates risk-on + credit tightening + weak USD)
# ---------------------------------------------------------------------------


def test_risk_on_composite() -> None:
    readings = [
        _reading(SYMBOL_SHY, momentum_20d=0.02, trend=TREND_UP),   # short up
        _reading(SYMBOL_IEF, momentum_20d=-0.03, trend=TREND_DOWN),
        _reading(SYMBOL_TLT, momentum_20d=-0.05, trend=TREND_DOWN),
        _reading(SYMBOL_HYG, momentum_20d=0.04, trend=TREND_UP),
        _reading(SYMBOL_LQD, momentum_20d=0.005, trend=TREND_FLAT),
        _reading(SYMBOL_UUP, momentum_20d=-0.02, trend=TREND_DOWN),
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    assert out.rates_regime == RATES_RISK_ON
    assert out.credit_regime == CREDIT_TIGHTENING
    assert out.usd_regime == USD_WEAK
    assert out.macro_label == MACRO_RISK_ON
    assert out.macro_numeric == 1
    # Yield-curve slope proxy is tlt.mom20 - shy.mom20 = -0.05 - 0.02 = -0.07.
    assert out.yield_curve_slope_proxy == pytest.approx(-0.07)
    assert out.credit_spread_proxy == pytest.approx(0.035)


# ---------------------------------------------------------------------------
# Risk-off scenario (long-duration bid + credit widening + strong USD)
# ---------------------------------------------------------------------------


def test_risk_off_composite() -> None:
    readings = [
        _reading(SYMBOL_SHY, momentum_20d=-0.01, trend=TREND_FLAT),
        _reading(SYMBOL_IEF, momentum_20d=0.03, trend=TREND_UP),
        _reading(SYMBOL_TLT, momentum_20d=0.06, trend=TREND_UP),
        _reading(SYMBOL_HYG, momentum_20d=-0.02, trend=TREND_DOWN),
        _reading(SYMBOL_LQD, momentum_20d=0.01, trend=TREND_FLAT),
        _reading(SYMBOL_UUP, momentum_20d=0.03, trend=TREND_UP),
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    assert out.rates_regime == RATES_RISK_OFF
    assert out.credit_regime == CREDIT_WIDENING
    assert out.usd_regime == USD_STRONG
    assert out.macro_label == MACRO_RISK_OFF
    assert out.macro_numeric == -1


# ---------------------------------------------------------------------------
# Cautious tie-breaker (mixed signals)
# ---------------------------------------------------------------------------


def test_cautious_when_signals_cancel() -> None:
    readings = [
        _reading(SYMBOL_SHY, momentum_20d=0.0, trend=TREND_FLAT),
        _reading(SYMBOL_IEF, momentum_20d=0.0, trend=TREND_FLAT),
        _reading(SYMBOL_TLT, momentum_20d=0.0, trend=TREND_FLAT),
        _reading(SYMBOL_HYG, momentum_20d=0.02, trend=TREND_UP),
        _reading(SYMBOL_LQD, momentum_20d=0.02, trend=TREND_UP),  # spread flat
        _reading(SYMBOL_UUP, momentum_20d=0.02, trend=TREND_UP),
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    assert out.rates_regime == RATES_NEUTRAL
    # HYG vs LQD spread is 0.0; within +/-0.005 neutral band.
    assert out.credit_regime == CREDIT_NEUTRAL
    assert out.usd_regime == USD_STRONG
    # Only USD contributes score; magnitude 0.2 * -1 = -0.2 < promote_threshold (0.35).
    assert out.macro_label == MACRO_CAUTIOUS
    assert out.macro_numeric == 0


# ---------------------------------------------------------------------------
# Coverage-score / missing-reading handling
# ---------------------------------------------------------------------------


def test_partial_coverage_does_not_raise_and_reflects_missing() -> None:
    readings = [
        _reading(SYMBOL_HYG, momentum_20d=0.04, trend=TREND_UP),
        _reading(SYMBOL_LQD, momentum_20d=0.01, trend=TREND_FLAT),
        _reading(SYMBOL_UUP, momentum_20d=-0.02, trend=TREND_DOWN),
        # rates symbols entirely absent
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    assert out.rates_regime is None
    assert out.yield_curve_slope_proxy is None
    assert out.credit_regime == CREDIT_TIGHTENING
    assert out.usd_regime == USD_WEAK
    assert out.ief_trend is None
    assert out.shy_trend is None
    assert out.tlt_trend is None
    assert out.symbols_sampled == 3
    assert out.symbols_missing == 3
    assert out.coverage_score == pytest.approx(0.5)


def test_missing_entries_ignored_not_treated_as_flat() -> None:
    # If missing were treated as flat, this would be RATES_NEUTRAL.
    # Correct behavior: two missing out of three still classifies from the
    # single available long-duration print.
    readings = [
        _reading(SYMBOL_SHY, momentum_20d=None, trend=TREND_MISSING, missing=True),
        _reading(SYMBOL_IEF, momentum_20d=None, trend=TREND_MISSING, missing=True),
        _reading(SYMBOL_TLT, momentum_20d=0.08, trend=TREND_UP),
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    # One long-duration UP with short missing gives net = 1 - 0 = 1
    # (between 2 and -2 thresholds), so rates_regime is RATES_NEUTRAL.
    assert out.rates_regime == RATES_NEUTRAL
    # Slope is None because SHY is missing.
    assert out.yield_curve_slope_proxy is None


def test_only_hyg_available_falls_back_to_single_symbol_trend() -> None:
    readings = [
        _reading(SYMBOL_HYG, momentum_20d=0.05, trend=TREND_UP),
        _reading(SYMBOL_LQD, momentum_20d=None, trend=TREND_MISSING, missing=True),
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    assert out.credit_regime == CREDIT_TIGHTENING
    assert out.credit_spread_proxy is None  # can't compute spread with one symbol


# ---------------------------------------------------------------------------
# Equity echo + regime_id
# ---------------------------------------------------------------------------


def test_equity_fields_are_echoed() -> None:
    equity = EquityRegimeInput(
        spy_direction="up",
        spy_momentum_5d=0.012,
        vix=14.5,
        vix_regime="calm",
        volatility_percentile=0.3,
        composite="risk_on",
        regime_numeric=1,
    )
    out = compute_macro_regime(
        MacroRegimeInput(as_of_date=_DATE, equity=equity, readings=())
    )
    assert out.spy_direction == "up"
    assert out.spy_momentum_5d == 0.012
    assert out.vix == 14.5
    assert out.vix_regime == "calm"
    assert out.volatility_percentile == 0.3
    assert out.composite == "risk_on"
    assert out.regime_numeric == 1
    assert out.regime_id == compute_regime_id(_DATE)


# ---------------------------------------------------------------------------
# Payload structure (stable wire shape for diagnostics / soak)
# ---------------------------------------------------------------------------


def test_payload_has_stable_top_level_keys() -> None:
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=()
        )
    )
    assert set(out.payload.keys()) == {"composite_score", "readings", "config"}
    assert set(out.payload["readings"].keys()) == set(ALL_SYMBOLS)
    assert set(out.payload["config"].keys()) == {
        "trend_up_threshold",
        "strong_trend_threshold",
        "min_coverage_score",
        "weights",
        "promote_threshold",
    }
    assert set(out.payload["config"]["weights"].keys()) == {
        "rates", "credit", "usd"
    }


def test_duplicate_symbols_last_one_wins() -> None:
    # Two readings for UUP: the second should win.
    readings = [
        _reading(SYMBOL_UUP, momentum_20d=0.05, trend=TREND_UP),
        _reading(SYMBOL_UUP, momentum_20d=-0.05, trend=TREND_DOWN),
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    assert out.usd_regime == USD_WEAK


def test_extra_symbols_are_ignored() -> None:
    readings = [
        AssetReading(
            symbol="XLE",
            missing=False,
            last_close=80.0,
            momentum_20d=0.1,
            momentum_5d=0.02,
            trend=TREND_UP,
        ),
    ]
    out = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=_DATE, equity=_empty_equity(), readings=readings
        )
    )
    # XLE isn't in ALL_SYMBOLS; should be ignored silently.
    assert out.symbols_sampled == 0
    assert out.symbols_missing == len(ALL_SYMBOLS)
