"""Phase L.19 - cross-asset lead/lag signals (pure functions).

Builds an additive cross-asset snapshot from:

1. Daily OHLCV readings for a fixed lead/lag basket:
   - SPY (equity benchmark)
   - TLT (long-duration treasury)
   - HYG, LQD (credit: high-yield, investment-grade)
   - UUP (USD proxy)
   - BTC-USD, ETH-USD (crypto benchmarks)

2. Context from existing panels (read-only):
   - L.17 ``macro_label`` / ``rates_regime`` / ``credit_regime`` / ``usd_regime``
   - L.18 ``advance_ratio`` / ``breadth_label``
   - ``get_market_regime()`` ``vix`` / ``volatility_percentile``

The pure model has **no side effects**: no DB, no network, no logging,
no config reads. All callers wrap it with a service-layer writer that
handles ETF/crypto fetching, joining L.17 / L.18, mode gating, and
persistence to ``trading_cross_asset_snapshots``.

Signal basket (shadow-only, daily cadence)
------------------------------------------

- **Bond-equity lead**: ``TLT.ret_5d - SPY.ret_5d`` (and 20d variant).
  Positive = bonds leading risk (risk-off); negative = risk-on.
- **Credit-equity lead**: ``Δ(HYG.ret_Nd - LQD.ret_Nd)`` vs SPY.ret_Nd.
  Negative spread with weak SPY = credit-widening risk-off.
- **USD-crypto lead**: ``UUP.ret_5d - BTC.ret_5d`` (and 20d).
  Positive USD with weak crypto = risk-off for crypto.
- **VIX-breadth divergence**: VIX percentile vs advance_ratio.
  E.g. high VIX percentile + breadth risk-on = latent risk flag.
- **Crypto-equity beta** (window default 60d): rolling OLS beta of
  BTC daily returns on SPY daily returns + Pearson correlation.

Composite label in
``{risk_on_crosscheck, risk_off_crosscheck, divergence, neutral}``.

Determinism
-----------

``compute_snapshot_id(as_of_date)`` returns ``sha1(..)`` truncated to
16 hex chars. Two sweeps for the same trading day produce the same
``snapshot_id``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYMBOL_SPY = "SPY"
SYMBOL_TLT = "TLT"
SYMBOL_HYG = "HYG"
SYMBOL_LQD = "LQD"
SYMBOL_UUP = "UUP"
SYMBOL_BTC = "BTC-USD"
SYMBOL_ETH = "ETH-USD"

ALL_SYMBOLS: tuple[str, ...] = (
    SYMBOL_SPY, SYMBOL_TLT, SYMBOL_HYG, SYMBOL_LQD,
    SYMBOL_UUP, SYMBOL_BTC, SYMBOL_ETH,
)

# Composite labels.
CROSS_ASSET_RISK_ON = "risk_on_crosscheck"
CROSS_ASSET_RISK_OFF = "risk_off_crosscheck"
CROSS_ASSET_DIVERGENCE = "divergence"
CROSS_ASSET_NEUTRAL = "neutral"

_VALID_COMPOSITE = frozenset(
    (
        CROSS_ASSET_RISK_ON,
        CROSS_ASSET_RISK_OFF,
        CROSS_ASSET_DIVERGENCE,
        CROSS_ASSET_NEUTRAL,
    )
)

# Sub-labels for each lead (risk_on / risk_off / neutral / missing).
LEAD_RISK_ON = "risk_on"
LEAD_RISK_OFF = "risk_off"
LEAD_NEUTRAL = "neutral"
LEAD_MISSING = "missing"

# Numeric encoding for composite.
_NUMERIC_MAP = {
    CROSS_ASSET_RISK_ON: 1,
    CROSS_ASSET_NEUTRAL: 0,
    CROSS_ASSET_DIVERGENCE: 0,
    CROSS_ASSET_RISK_OFF: -1,
}


# ---------------------------------------------------------------------------
# Config + data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CrossAssetConfig:
    """Pure-model configuration.

    Thresholds are fraction-of-return units: 0.01 = 1 %.
    """

    fast_lead_threshold: float = 0.01
    slow_lead_threshold: float = 0.03
    vix_percentile_shock: float = 0.80
    min_coverage_score: float = 0.5
    beta_window_days: int = 60
    # Composite gating: min number of agreeing lead signals to label
    # risk_on_crosscheck / risk_off_crosscheck.
    composite_min_agreement: int = 2


@dataclass(frozen=True)
class AssetLeg:
    """Single-asset daily reading.

    ``returns_daily`` is the most recent daily-return series (most
    recent last) used for beta computation. Can be empty when the
    full series isn't needed for a given leg.
    """

    symbol: str
    last_close: float | None
    ret_1d: float | None
    ret_5d: float | None
    ret_20d: float | None
    missing: bool
    returns_daily: tuple[float, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not self.symbol or not isinstance(self.symbol, str):
            raise ValueError("AssetLeg.symbol must be a non-empty string")


@dataclass(frozen=True)
class CrossAssetInput:
    """Canonical input bundle for ``compute_cross_asset``.

    Missing legs should still be passed as ``AssetLeg(..., missing=True)``
    with ``None`` scalars so coverage counting is consistent.
    """

    as_of_date: date
    equity: AssetLeg          # SPY
    rates: AssetLeg           # TLT
    credit_hy: AssetLeg       # HYG
    credit_ig: AssetLeg       # LQD
    usd: AssetLeg             # UUP
    crypto_btc: AssetLeg      # BTC-USD
    crypto_eth: AssetLeg      # ETH-USD (sampled only, not in every label)
    vix_level: float | None
    vix_percentile: float | None
    breadth_advance_ratio: float | None
    breadth_label: str | None
    macro_label: str | None
    config: CrossAssetConfig = field(default_factory=CrossAssetConfig)


@dataclass(frozen=True)
class CrossAssetOutput:
    """Frozen output of ``compute_cross_asset``."""

    snapshot_id: str
    as_of_date: date

    bond_equity_lead_5d: float | None
    bond_equity_lead_20d: float | None
    bond_equity_label: str

    credit_equity_lead_5d: float | None
    credit_equity_lead_20d: float | None
    credit_equity_label: str

    usd_crypto_lead_5d: float | None
    usd_crypto_lead_20d: float | None
    usd_crypto_label: str

    vix_level: float | None
    vix_percentile: float | None
    breadth_advance_ratio: float | None
    vix_breadth_divergence_score: float | None
    vix_breadth_label: str

    crypto_equity_beta: float | None
    crypto_equity_beta_window_days: int | None
    crypto_equity_correlation: float | None

    cross_asset_numeric: int
    cross_asset_label: str

    symbols_sampled: int
    symbols_missing: int
    coverage_score: float

    payload: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Determinism helper
# ---------------------------------------------------------------------------

def compute_snapshot_id(as_of_date: date) -> str:
    """Deterministic 16-char id from the as-of date."""

    if not isinstance(as_of_date, date):
        raise ValueError("as_of_date must be a datetime.date")
    h = hashlib.sha1(as_of_date.isoformat().encode("utf-8")).hexdigest()
    return h[:16]


# ---------------------------------------------------------------------------
# Per-signal classifiers
# ---------------------------------------------------------------------------

def _classify_lead(
    diff: float | None,
    *,
    fast_threshold: float,
    invert: bool = False,
) -> str:
    """Classify a lead/lag return difference.

    When ``invert`` is ``False`` the convention is: positive diff ->
    risk-off (e.g. TLT outperforming SPY). When ``invert`` is ``True``
    positive diff is risk-on (e.g. crypto outperforming USD).
    """

    if diff is None:
        return LEAD_MISSING
    if diff >= fast_threshold:
        return LEAD_RISK_OFF if not invert else LEAD_RISK_ON
    if diff <= -fast_threshold:
        return LEAD_RISK_ON if not invert else LEAD_RISK_OFF
    return LEAD_NEUTRAL


def _bond_equity_lead(
    equity: AssetLeg, rates: AssetLeg, cfg: CrossAssetConfig
) -> tuple[float | None, float | None, str]:
    """Positive diff = bonds leading equities = risk-off."""

    if equity.missing or rates.missing:
        return None, None, LEAD_MISSING
    d5: float | None = None
    d20: float | None = None
    if equity.ret_5d is not None and rates.ret_5d is not None:
        d5 = float(rates.ret_5d) - float(equity.ret_5d)
    if equity.ret_20d is not None and rates.ret_20d is not None:
        d20 = float(rates.ret_20d) - float(equity.ret_20d)
    label = _classify_lead(d5, fast_threshold=cfg.fast_lead_threshold)
    return d5, d20, label


def _credit_equity_lead(
    equity: AssetLeg,
    hy: AssetLeg,
    ig: AssetLeg,
    cfg: CrossAssetConfig,
) -> tuple[float | None, float | None, str]:
    """Credit stress lead: Δ(HYG - LQD) vs SPY.

    When HYG underperforms LQD (spread tightening proxy going wrong
    direction) while SPY is weak, credit is widening = risk-off.
    Signal returned is ``(HYG.ret - LQD.ret) - SPY.ret``: when
    strongly negative, credit is worse than equities = risk-off.
    """

    if equity.missing or hy.missing or ig.missing:
        return None, None, LEAD_MISSING
    d5: float | None = None
    d20: float | None = None
    if (
        equity.ret_5d is not None
        and hy.ret_5d is not None
        and ig.ret_5d is not None
    ):
        d5 = (float(hy.ret_5d) - float(ig.ret_5d)) - float(equity.ret_5d)
    if (
        equity.ret_20d is not None
        and hy.ret_20d is not None
        and ig.ret_20d is not None
    ):
        d20 = (float(hy.ret_20d) - float(ig.ret_20d)) - float(equity.ret_20d)
    # negative d5 -> risk-off (credit underperforming vs equity)
    label: str
    if d5 is None:
        label = LEAD_MISSING
    elif d5 <= -cfg.fast_lead_threshold:
        label = LEAD_RISK_OFF
    elif d5 >= cfg.fast_lead_threshold:
        label = LEAD_RISK_ON
    else:
        label = LEAD_NEUTRAL
    return d5, d20, label


def _usd_crypto_lead(
    usd: AssetLeg, btc: AssetLeg, cfg: CrossAssetConfig
) -> tuple[float | None, float | None, str]:
    """Positive diff (USD up vs BTC) = risk-off for crypto."""

    if usd.missing or btc.missing:
        return None, None, LEAD_MISSING
    d5: float | None = None
    d20: float | None = None
    if usd.ret_5d is not None and btc.ret_5d is not None:
        d5 = float(usd.ret_5d) - float(btc.ret_5d)
    if usd.ret_20d is not None and btc.ret_20d is not None:
        d20 = float(usd.ret_20d) - float(btc.ret_20d)
    label = _classify_lead(d5, fast_threshold=cfg.fast_lead_threshold)
    return d5, d20, label


def _vix_breadth_divergence(
    vix_percentile: float | None,
    breadth_advance_ratio: float | None,
    cfg: CrossAssetConfig,
) -> tuple[float | None, str]:
    """High VIX percentile with risk-on breadth = latent stress flag.

    Score = vix_percentile - (1 - advance_ratio). Positive score
    means VIX is "hotter" than breadth implies (potential divergence).
    """

    if vix_percentile is None or breadth_advance_ratio is None:
        return None, LEAD_MISSING
    try:
        p = float(vix_percentile)
        a = float(breadth_advance_ratio)
    except (TypeError, ValueError):
        return None, LEAD_MISSING
    if not (0.0 <= p <= 1.0 and 0.0 <= a <= 1.0):
        return None, LEAD_MISSING
    score = p - (1.0 - a)
    label: str
    if p >= cfg.vix_percentile_shock and a >= 0.5:
        # VIX shock but breadth still risk-on -> divergence risk-off
        label = LEAD_RISK_OFF
    elif score >= 0.30:
        label = LEAD_RISK_OFF
    elif score <= -0.30:
        label = LEAD_RISK_ON
    else:
        label = LEAD_NEUTRAL
    return score, label


def _crypto_equity_beta(
    btc_returns: Sequence[float],
    spy_returns: Sequence[float],
    window_days: int,
) -> tuple[float | None, float | None]:
    """Pure OLS beta of BTC returns on SPY returns over the last
    ``window_days``, plus Pearson correlation. Returns ``(None, None)``
    when inputs are insufficient or have zero variance.
    """

    if window_days <= 1:
        return None, None
    try:
        n = min(len(btc_returns), len(spy_returns), int(window_days))
    except TypeError:
        return None, None
    if n < 5:
        return None, None
    b = [float(x) for x in list(btc_returns)[-n:]]
    s = [float(x) for x in list(spy_returns)[-n:]]
    mean_b = sum(b) / n
    mean_s = sum(s) / n
    num = 0.0
    den_s = 0.0
    den_b = 0.0
    for i in range(n):
        db = b[i] - mean_b
        ds = s[i] - mean_s
        num += db * ds
        den_s += ds * ds
        den_b += db * db
    if den_s <= 0.0 or den_b <= 0.0:
        return None, None
    beta = num / den_s
    corr = num / ((den_b ** 0.5) * (den_s ** 0.5))
    return float(beta), float(corr)


# ---------------------------------------------------------------------------
# Composite label
# ---------------------------------------------------------------------------

def _composite_cross_asset_label(
    *,
    bond_label: str,
    credit_label: str,
    usd_crypto_label: str,
    vix_breadth_label: str,
    cfg: CrossAssetConfig,
) -> tuple[str, int]:
    """Aggregate per-signal labels into the composite label.

    Rules:
      * Count risk_on / risk_off signals across the 4 gates
        (bond, credit, usd-crypto, vix-breadth). Missing signals do
        not count.
      * If risk_on >= composite_min_agreement and risk_off == 0 ->
        risk_on_crosscheck.
      * If risk_off >= composite_min_agreement and risk_on == 0 ->
        risk_off_crosscheck.
      * If risk_on >= 1 AND risk_off >= 1 -> divergence.
      * Otherwise neutral.
    """

    on = 0
    off = 0
    for label in (bond_label, credit_label, usd_crypto_label, vix_breadth_label):
        if label == LEAD_RISK_ON:
            on += 1
        elif label == LEAD_RISK_OFF:
            off += 1
    if on >= cfg.composite_min_agreement and off == 0:
        composite = CROSS_ASSET_RISK_ON
    elif off >= cfg.composite_min_agreement and on == 0:
        composite = CROSS_ASSET_RISK_OFF
    elif on >= 1 and off >= 1:
        composite = CROSS_ASSET_DIVERGENCE
    else:
        composite = CROSS_ASSET_NEUTRAL
    return composite, _NUMERIC_MAP[composite]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _count_coverage(input_: CrossAssetInput) -> tuple[int, int, float]:
    """Return (sampled, missing, coverage_score).

    "Sampled" counts all 7 core legs that aren't missing. "Missing"
    counts those that were provided as missing=True. ``coverage_score
    = sampled / total`` where total = 7.
    """

    legs = (
        input_.equity, input_.rates, input_.credit_hy, input_.credit_ig,
        input_.usd, input_.crypto_btc, input_.crypto_eth,
    )
    total = len(legs)
    missing = sum(1 for leg in legs if leg.missing)
    sampled = total - missing
    score = sampled / float(total) if total else 0.0
    return sampled, missing, score


def compute_cross_asset(input_: CrossAssetInput) -> CrossAssetOutput:
    """Deterministic pure classifier.

    Raises ``ValueError`` only on structural violations (e.g. bad
    as-of-date). Missing legs are tolerated and surface through the
    per-signal ``missing`` label + the coverage block.
    """

    cfg = input_.config
    snap_id = compute_snapshot_id(input_.as_of_date)

    be5, be20, be_label = _bond_equity_lead(
        input_.equity, input_.rates, cfg
    )
    ce5, ce20, ce_label = _credit_equity_lead(
        input_.equity, input_.credit_hy, input_.credit_ig, cfg
    )
    uc5, uc20, uc_label = _usd_crypto_lead(
        input_.usd, input_.crypto_btc, cfg
    )
    vbd_score, vbd_label = _vix_breadth_divergence(
        input_.vix_percentile, input_.breadth_advance_ratio, cfg
    )

    beta, corr = _crypto_equity_beta(
        input_.crypto_btc.returns_daily,
        input_.equity.returns_daily,
        cfg.beta_window_days,
    )
    beta_window = cfg.beta_window_days if beta is not None else None

    composite, composite_num = _composite_cross_asset_label(
        bond_label=be_label,
        credit_label=ce_label,
        usd_crypto_label=uc_label,
        vix_breadth_label=vbd_label,
        cfg=cfg,
    )

    sampled, missing, score = _count_coverage(input_)

    payload: dict[str, Any] = {
        "symbols": {
            leg.symbol: {
                "last_close": leg.last_close,
                "ret_1d": leg.ret_1d,
                "ret_5d": leg.ret_5d,
                "ret_20d": leg.ret_20d,
                "missing": leg.missing,
            }
            for leg in (
                input_.equity, input_.rates, input_.credit_hy,
                input_.credit_ig, input_.usd, input_.crypto_btc,
                input_.crypto_eth,
            )
        },
        "context": {
            "vix_level": input_.vix_level,
            "vix_percentile": input_.vix_percentile,
            "breadth_advance_ratio": input_.breadth_advance_ratio,
            "breadth_label": input_.breadth_label,
            "macro_label": input_.macro_label,
        },
        "config": {
            "fast_lead_threshold": cfg.fast_lead_threshold,
            "slow_lead_threshold": cfg.slow_lead_threshold,
            "vix_percentile_shock": cfg.vix_percentile_shock,
            "min_coverage_score": cfg.min_coverage_score,
            "beta_window_days": cfg.beta_window_days,
            "composite_min_agreement": cfg.composite_min_agreement,
        },
    }

    return CrossAssetOutput(
        snapshot_id=snap_id,
        as_of_date=input_.as_of_date,
        bond_equity_lead_5d=be5,
        bond_equity_lead_20d=be20,
        bond_equity_label=be_label,
        credit_equity_lead_5d=ce5,
        credit_equity_lead_20d=ce20,
        credit_equity_label=ce_label,
        usd_crypto_lead_5d=uc5,
        usd_crypto_lead_20d=uc20,
        usd_crypto_label=uc_label,
        vix_level=input_.vix_level,
        vix_percentile=input_.vix_percentile,
        breadth_advance_ratio=input_.breadth_advance_ratio,
        vix_breadth_divergence_score=vbd_score,
        vix_breadth_label=vbd_label,
        crypto_equity_beta=beta,
        crypto_equity_beta_window_days=beta_window,
        crypto_equity_correlation=corr,
        cross_asset_numeric=composite_num,
        cross_asset_label=composite,
        symbols_sampled=sampled,
        symbols_missing=missing,
        coverage_score=score,
        payload=payload,
    )
