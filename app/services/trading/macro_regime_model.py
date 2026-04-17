"""Phase L.17 - macro regime expansion (pure functions).

Builds an extended macro regime snapshot from:

1. The existing equity regime (SPY + VIX composite) produced today by
   :func:`app.services.trading.market_data.get_market_regime`.
2. A set of :class:`AssetReading` entries covering rates
   (``IEF`` / ``SHY`` / ``TLT``), credit (``HYG`` / ``LQD``), and USD
   (``UUP``) ETFs.

The pure model has **no side effects**: no DB, no network, no logging,
no config reads. All callers wrap it with a service-layer writer that
handles ETF fetching, mode gating, and persistence to
``trading_macro_regime_snapshots``.

Determinism
-----------

``compute_regime_id(as_of_date)`` returns ``sha1(as_of_date.isoformat())``
truncated to 16 hex chars. Two sweeps for the same trading day produce
the same ``regime_id`` so append-only writes are de-duplicable by the
caller when desired.

Backward compatibility
----------------------

L.17 does **not** change the shape of ``get_market_regime()``. The pure
model explicitly echoes the equity-block keys ``composite`` and
``regime_numeric`` so the new snapshot never reduces information vs the
existing in-memory cache.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TREND_UP = "up"
TREND_DOWN = "down"
TREND_FLAT = "flat"
TREND_MISSING = "missing"
_VALID_TRENDS = (TREND_UP, TREND_DOWN, TREND_FLAT, TREND_MISSING)

RATES_RISK_ON = "rates_risk_on"
RATES_NEUTRAL = "rates_neutral"
RATES_RISK_OFF = "rates_risk_off"

CREDIT_TIGHTENING = "credit_tightening"
CREDIT_NEUTRAL = "credit_neutral"
CREDIT_WIDENING = "credit_widening"

USD_STRONG = "usd_strong"
USD_NEUTRAL = "usd_neutral"
USD_WEAK = "usd_weak"

MACRO_RISK_ON = "risk_on"
MACRO_CAUTIOUS = "cautious"
MACRO_RISK_OFF = "risk_off"
_VALID_MACRO_LABELS = (MACRO_RISK_ON, MACRO_CAUTIOUS, MACRO_RISK_OFF)

# Canonical ETF tickers used as inputs. The service layer is responsible
# for turning each one into an ``AssetReading`` (or a missing one when the
# provider is flaky). The pure model never fetches market data.
SYMBOL_IEF = "IEF"   # 7-10y Treasuries
SYMBOL_SHY = "SHY"   # 1-3y Treasuries
SYMBOL_TLT = "TLT"   # 20y+ Treasuries
SYMBOL_HYG = "HYG"   # high-yield corporate
SYMBOL_LQD = "LQD"   # investment-grade corporate
SYMBOL_UUP = "UUP"   # USD index proxy
RATES_SYMBOLS = (SYMBOL_IEF, SYMBOL_SHY, SYMBOL_TLT)
CREDIT_SYMBOLS = (SYMBOL_HYG, SYMBOL_LQD)
USD_SYMBOLS = (SYMBOL_UUP,)
ALL_SYMBOLS = RATES_SYMBOLS + CREDIT_SYMBOLS + USD_SYMBOLS


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroRegimeConfig:
    """Tuning knobs for the macro regime classifier.

    Defaults chosen to match the existing SPY/VIX composite thresholds
    in spirit: a trend is "up" if the symbol's 20d momentum is above
    ``trend_up_threshold`` and "down" if below ``-trend_up_threshold``.
    The same threshold is used for credit/USD relative strength.
    """

    trend_up_threshold: float = 0.01     # 1.0% 20d momentum -> up
    strong_trend_threshold: float = 0.03  # 3.0% -> strong up/down
    min_coverage_score: float = 0.5      # persist only if at least 50% of ETFs resolved
    # Sub-regime weights into the overall macro composite. Rates dominant
    # because the yield-curve proxy is the most robust macro regime
    # signal historically; credit second; USD tie-breaker.
    weight_rates: float = 0.45
    weight_credit: float = 0.35
    weight_usd: float = 0.20
    # Hysteresis: to promote from neutral to risk_on/off we need a weighted
    # score above this magnitude.
    promote_threshold: float = 0.35


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetReading:
    """One ETF reading produced by the service layer.

    Fields are optional because real providers drop data; the classifier
    handles missing entries defensively. When ``missing`` is ``True`` the
    numeric fields are ignored and the symbol contributes ``0`` to
    coverage but never skews the classifier.
    """

    symbol: str
    missing: bool = False
    last_close: float | None = None
    momentum_20d: float | None = None
    momentum_5d: float | None = None
    trend: str = TREND_MISSING

    def __post_init__(self) -> None:
        if self.trend not in _VALID_TRENDS:
            raise ValueError(
                f"AssetReading.trend={self.trend!r} not in {_VALID_TRENDS}"
            )


@dataclass(frozen=True)
class EquityRegimeInput:
    """Pass-through of the existing SPY/VIX composite.

    All fields default to ``None`` so the pure model stays usable even
    when the upstream equity regime cannot be computed. When
    ``composite`` is ``None`` the macro snapshot falls back to the
    rates/credit/USD composite only.
    """

    spy_direction: str | None = None
    spy_momentum_5d: float | None = None
    vix: float | None = None
    vix_regime: str | None = None
    volatility_percentile: float | None = None
    composite: str | None = None
    regime_numeric: int | None = None


@dataclass(frozen=True)
class MacroRegimeInput:
    """Inputs to :func:`compute_macro_regime`."""

    as_of_date: date
    equity: EquityRegimeInput
    readings: Sequence[AssetReading] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroRegimeOutput:
    """Pure output of the macro regime classifier.

    Mirrors the ORM columns on ``MacroRegimeSnapshot`` so the service
    layer writer is a 1-to-1 shallow copy.
    """

    regime_id: str
    as_of_date: date

    # equity echo
    spy_direction: str | None
    spy_momentum_5d: float | None
    vix: float | None
    vix_regime: str | None
    volatility_percentile: float | None
    composite: str | None
    regime_numeric: int | None

    # rates
    ief_trend: str | None
    shy_trend: str | None
    tlt_trend: str | None
    yield_curve_slope_proxy: float | None
    rates_regime: str | None

    # credit
    hyg_trend: str | None
    lqd_trend: str | None
    credit_spread_proxy: float | None
    credit_regime: str | None

    # usd
    uup_trend: str | None
    uup_momentum_20d: float | None
    usd_regime: str | None

    # composite macro
    macro_numeric: int
    macro_label: str

    # coverage
    symbols_sampled: int
    symbols_missing: int
    coverage_score: float

    payload: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_regime_id(as_of_date: date) -> str:
    """Deterministic 16-char sha1 of the ISO date string."""
    if not isinstance(as_of_date, date):
        raise TypeError(
            f"compute_regime_id expected datetime.date, got {type(as_of_date).__name__}"
        )
    return hashlib.sha1(as_of_date.isoformat().encode("utf-8")).hexdigest()[:16]


def classify_trend(
    momentum_20d: float | None,
    *,
    cfg: MacroRegimeConfig,
) -> str:
    """Single-symbol trend classification from 20d momentum.

    ``None`` is treated as ``missing`` (not ``flat``) so a caller can
    distinguish "provider down" from "flat market".
    """
    if momentum_20d is None:
        return TREND_MISSING
    if momentum_20d > cfg.trend_up_threshold:
        return TREND_UP
    if momentum_20d < -cfg.trend_up_threshold:
        return TREND_DOWN
    return TREND_FLAT


def _trend_to_score(trend: str) -> float:
    if trend == TREND_UP:
        return 1.0
    if trend == TREND_DOWN:
        return -1.0
    return 0.0


def _safe_reading(
    readings: Mapping[str, AssetReading],
    symbol: str,
) -> AssetReading:
    """Return reading if present-and-not-missing, else a missing stub."""
    r = readings.get(symbol)
    if r is None or r.missing:
        return AssetReading(symbol=symbol, missing=True, trend=TREND_MISSING)
    return r


def _classify_rates(
    ief: AssetReading,
    shy: AssetReading,
    tlt: AssetReading,
) -> tuple[str | None, float | None]:
    """Classify rates regime + yield-curve slope proxy.

    Rationale: in a risk-on regime, long-duration Treasuries (TLT/IEF)
    tend to sell off (yields rising). In a risk-off regime long duration
    rallies. The yield-curve slope proxy here is defined as
    ``momentum_20d(TLT) - momentum_20d(SHY)``: positive when the long
    end outperforms the short end (curve steepening on the long end,
    typically risk-off); negative when long lags (risk-on).

    Returns ``(regime, slope_proxy)``. When we have zero usable symbols
    returns ``(None, None)`` so the caller can fall back to the composite.
    """
    usable = [r for r in (ief, shy, tlt) if not r.missing]
    if not usable:
        return None, None

    slope: float | None = None
    if (
        tlt.momentum_20d is not None
        and shy.momentum_20d is not None
        and not tlt.missing
        and not shy.missing
    ):
        slope = float(tlt.momentum_20d) - float(shy.momentum_20d)

    # Score: positive long-duration trend => rates-risk-off.
    long_score = _trend_to_score(tlt.trend) + _trend_to_score(ief.trend)
    short_score = _trend_to_score(shy.trend)
    net = long_score - short_score  # range roughly [-3, 3]

    if net >= 2:
        regime = RATES_RISK_OFF
    elif net <= -2:
        regime = RATES_RISK_ON
    else:
        regime = RATES_NEUTRAL

    return regime, slope


def _classify_credit(
    hyg: AssetReading,
    lqd: AssetReading,
) -> tuple[str | None, float | None]:
    """Classify credit regime + HYG-vs-LQD spread proxy.

    Positive proxy (HYG outperforming LQD on 20d) -> credit tightening
    (risk-on). Negative -> widening (risk-off).
    """
    usable = [r for r in (hyg, lqd) if not r.missing]
    if not usable:
        return None, None

    spread_proxy: float | None = None
    if (
        hyg.momentum_20d is not None
        and lqd.momentum_20d is not None
        and not hyg.missing
        and not lqd.missing
    ):
        spread_proxy = float(hyg.momentum_20d) - float(lqd.momentum_20d)

    # Fall back to single-symbol trend when we only have one.
    if hyg.missing and not lqd.missing:
        regime = CREDIT_NEUTRAL
    elif lqd.missing and not hyg.missing:
        if hyg.trend == TREND_UP:
            regime = CREDIT_TIGHTENING
        elif hyg.trend == TREND_DOWN:
            regime = CREDIT_WIDENING
        else:
            regime = CREDIT_NEUTRAL
    else:
        assert spread_proxy is not None  # both present
        if spread_proxy > 0.005:   # HYG +0.5pp ahead of LQD on 20d
            regime = CREDIT_TIGHTENING
        elif spread_proxy < -0.005:
            regime = CREDIT_WIDENING
        else:
            regime = CREDIT_NEUTRAL

    return regime, spread_proxy


def _classify_usd(uup: AssetReading) -> tuple[str | None, float | None]:
    """Classify USD regime.

    Strong USD (UUP up-trend) -> risk-off for equities historically.
    """
    if uup.missing:
        return None, None

    mom = uup.momentum_20d
    if uup.trend == TREND_UP:
        regime = USD_STRONG
    elif uup.trend == TREND_DOWN:
        regime = USD_WEAK
    else:
        regime = USD_NEUTRAL
    return regime, mom


def _rates_score(regime: str | None) -> float:
    if regime == RATES_RISK_ON:
        return 1.0
    if regime == RATES_RISK_OFF:
        return -1.0
    return 0.0


def _credit_score(regime: str | None) -> float:
    if regime == CREDIT_TIGHTENING:
        return 1.0
    if regime == CREDIT_WIDENING:
        return -1.0
    return 0.0


def _usd_score(regime: str | None) -> float:
    # Strong USD historically bearish for risk; we flip sign so that
    # "usd risk-on" (weak USD) contributes positively.
    if regime == USD_WEAK:
        return 1.0
    if regime == USD_STRONG:
        return -1.0
    return 0.0


def _composite_label(
    rates_regime: str | None,
    credit_regime: str | None,
    usd_regime: str | None,
    *,
    cfg: MacroRegimeConfig,
) -> tuple[str, int, float]:
    """Combine the three sub-regimes into the macro composite."""
    score = (
        cfg.weight_rates * _rates_score(rates_regime)
        + cfg.weight_credit * _credit_score(credit_regime)
        + cfg.weight_usd * _usd_score(usd_regime)
    )
    # Map score in roughly [-1.0, 1.0] to {risk_on, cautious, risk_off}.
    if score >= cfg.promote_threshold:
        return MACRO_RISK_ON, 1, round(float(score), 6)
    if score <= -cfg.promote_threshold:
        return MACRO_RISK_OFF, -1, round(float(score), 6)
    return MACRO_CAUTIOUS, 0, round(float(score), 6)


def compute_macro_regime(
    inputs: MacroRegimeInput,
    *,
    config: MacroRegimeConfig | None = None,
) -> MacroRegimeOutput:
    """Pure classifier for the macro regime snapshot.

    Rules:

    - Missing readings never raise. They reduce ``coverage_score`` and
      leave the corresponding trend / regime fields as ``None``.
    - ``macro_label`` is always one of ``{risk_on, cautious, risk_off}``.
      When we have zero sub-regimes (every ETF missing) we fall back to
      ``cautious`` with ``macro_numeric=0`` to keep the contract stable.
    - ``coverage_score = symbols_sampled / len(ALL_SYMBOLS)``.
    """
    cfg = config or MacroRegimeConfig()

    # Index readings by symbol; silently drop extras.
    indexed: dict[str, AssetReading] = {}
    for r in inputs.readings:
        if r.symbol in ALL_SYMBOLS:
            indexed[r.symbol] = r

    ief = _safe_reading(indexed, SYMBOL_IEF)
    shy = _safe_reading(indexed, SYMBOL_SHY)
    tlt = _safe_reading(indexed, SYMBOL_TLT)
    hyg = _safe_reading(indexed, SYMBOL_HYG)
    lqd = _safe_reading(indexed, SYMBOL_LQD)
    uup = _safe_reading(indexed, SYMBOL_UUP)

    rates_regime, slope = _classify_rates(ief, shy, tlt)
    credit_regime, spread = _classify_credit(hyg, lqd)
    usd_regime, uup_mom = _classify_usd(uup)

    macro_label, macro_numeric, composite_score = _composite_label(
        rates_regime, credit_regime, usd_regime, cfg=cfg
    )

    symbols_sampled = sum(
        1 for r in (ief, shy, tlt, hyg, lqd, uup) if not r.missing
    )
    symbols_missing = len(ALL_SYMBOLS) - symbols_sampled
    coverage_score = round(symbols_sampled / float(len(ALL_SYMBOLS)), 6)

    payload: dict[str, Any] = {
        "composite_score": composite_score,
        "readings": {
            r.symbol: {
                "missing": bool(r.missing),
                "last_close": r.last_close,
                "momentum_20d": r.momentum_20d,
                "momentum_5d": r.momentum_5d,
                "trend": r.trend,
            }
            for r in (ief, shy, tlt, hyg, lqd, uup)
        },
        "config": {
            "trend_up_threshold": cfg.trend_up_threshold,
            "strong_trend_threshold": cfg.strong_trend_threshold,
            "min_coverage_score": cfg.min_coverage_score,
            "weights": {
                "rates": cfg.weight_rates,
                "credit": cfg.weight_credit,
                "usd": cfg.weight_usd,
            },
            "promote_threshold": cfg.promote_threshold,
        },
    }

    return MacroRegimeOutput(
        regime_id=compute_regime_id(inputs.as_of_date),
        as_of_date=inputs.as_of_date,
        spy_direction=inputs.equity.spy_direction,
        spy_momentum_5d=inputs.equity.spy_momentum_5d,
        vix=inputs.equity.vix,
        vix_regime=inputs.equity.vix_regime,
        volatility_percentile=inputs.equity.volatility_percentile,
        composite=inputs.equity.composite,
        regime_numeric=inputs.equity.regime_numeric,
        ief_trend=(None if ief.missing else ief.trend),
        shy_trend=(None if shy.missing else shy.trend),
        tlt_trend=(None if tlt.missing else tlt.trend),
        yield_curve_slope_proxy=slope,
        rates_regime=rates_regime,
        hyg_trend=(None if hyg.missing else hyg.trend),
        lqd_trend=(None if lqd.missing else lqd.trend),
        credit_spread_proxy=spread,
        credit_regime=credit_regime,
        uup_trend=(None if uup.missing else uup.trend),
        uup_momentum_20d=uup_mom,
        usd_regime=usd_regime,
        macro_numeric=int(macro_numeric),
        macro_label=macro_label,
        symbols_sampled=int(symbols_sampled),
        symbols_missing=int(symbols_missing),
        coverage_score=float(coverage_score),
        payload=payload,
    )
