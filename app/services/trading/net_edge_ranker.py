"""Phase E: NetEdgeRanker - single calibrated expected-net-PnL score.

Canonical scoring surface for every signal. Composes a regime-conditioned
calibrated probability with a cost-aware payoff in fraction-of-notional space:

    expected_net_pnl = p * payoff_win - (1 - p) * loss_per_unit
                       - spread_cost - slippage_cost - fees_cost
                       - miss_prob_cost - partial_fill_cost

All costs are in fraction-of-notional. ``p`` is the calibrated win probability
for this (asset_class, regime) bucket, produced by isotonic regression over
realized outcomes sourced from ``trading_trades`` and ``trading_paper_trades``.

Rollout discipline (identical ladder to the prediction mirror):
    off -> shadow -> compare -> authoritative

In any mode other than ``authoritative`` this module MUST NOT gate trading
decisions. Callers in shadow/compare are expected to call :func:`score` purely
for logging and diagnostics. See
``docs/TRADING_BRAIN_NET_EDGE_RANKER_ROLLOUT.md``.

Fail-open semantics: if any step fails (missing data, calibrator import error,
DB issue) ``score`` returns ``None`` and logs the error. Callers must tolerate
``None`` by falling back to the legacy heuristic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import (
    NetEdgeCalibrationSnapshot,
    NetEdgeScoreLog,
    PaperTrade,
    ScanPattern,
    Trade,
)
from ...trading_brain.infrastructure.net_edge_ops_log import (
    MODE_AUTHORITATIVE,
    MODE_COMPARE,
    MODE_OFF,
    MODE_SHADOW,
    READ_AUTHORITATIVE,
    READ_COLD_START,
    READ_COMPARE_DISAGREE,
    READ_COMPARE_OK,
    READ_ERROR,
    READ_NA,
    READ_SHADOW,
    format_net_edge_ops_line,
)

logger = logging.getLogger(__name__)

_VALID_MODES = {MODE_OFF, MODE_SHADOW, MODE_COMPARE, MODE_AUTHORITATIVE}

# Sensible cost defaults (fraction-of-notional) when venue-truth is unavailable.
# These are pre-Phase-F inputs; Phase F replaces them with data-driven values.
_DEFAULT_FEES_BPS_EQUITY = 0.0  # commission-free retail US equities
_DEFAULT_FEES_BPS_CRYPTO = 30.0  # ~0.3% taker on Coinbase spot
_DEFAULT_MISS_PROB_EQUITY = 0.02  # 2% miss rate on market entries (conservative placeholder)
_DEFAULT_MISS_PROB_CRYPTO = 0.05
_DEFAULT_PARTIAL_FILL_BPS_EQUITY = 1.0
_DEFAULT_PARTIAL_FILL_BPS_CRYPTO = 3.0


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetEdgeSignalContext:
    """Inputs to a single :func:`score` call.

    ``raw_prob`` is the uncalibrated win-probability estimate the caller has
    today (e.g. ``ScanPattern.oos_win_rate`` or a heuristic confidence). The
    ranker maps it to a calibrated probability for the active regime.

    ``entry_price`` / ``stop_price`` must both be positive. ``target_price`` is
    optional; if omitted we assume a 2:1 R:R target for the payoff calculation.
    """

    ticker: str
    asset_class: str  # "stock" | "crypto"
    scan_pattern_id: int | None
    raw_prob: float
    entry_price: float
    stop_price: float
    target_price: float | None = None
    regime: str | None = None
    timeframe: str | None = None
    quote_mid: float | None = None
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    heuristic_score: float | None = None  # legacy expectancy for comparison
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NetEdgeCostBreakdown:
    """Costs composed into the net-edge score, all in fraction-of-notional."""

    spread_cost: float
    slippage_cost: float
    fees_cost: float
    miss_prob_cost: float
    partial_fill_cost: float

    @property
    def total(self) -> float:
        return (
            self.spread_cost
            + self.slippage_cost
            + self.fees_cost
            + self.miss_prob_cost
            + self.partial_fill_cost
        )


@dataclass(frozen=True)
class NetEdgeProvenance:
    """What produced this score (for auditability)."""

    mode: str
    read_source: str
    calibrator_version_id: str | None
    calibrator_method: str
    calibrator_sample_count: int
    regime_bucket: str
    cold_start: bool
    produced_at: datetime


@dataclass(frozen=True)
class NetEdgeScore:
    """Final score returned from :func:`score`."""

    decision_id: str
    ticker: str
    asset_class: str
    scan_pattern_id: int | None
    calibrated_prob: float
    expected_payoff: float
    loss_per_unit: float
    costs: NetEdgeCostBreakdown
    expected_net_pnl: float
    heuristic_score: float | None
    disagree_vs_heuristic: bool
    provenance: NetEdgeProvenance

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "ticker": self.ticker,
            "asset_class": self.asset_class,
            "scan_pattern_id": self.scan_pattern_id,
            "calibrated_prob": self.calibrated_prob,
            "expected_payoff": self.expected_payoff,
            "loss_per_unit": self.loss_per_unit,
            "spread_cost": self.costs.spread_cost,
            "slippage_cost": self.costs.slippage_cost,
            "fees_cost": self.costs.fees_cost,
            "miss_prob_cost": self.costs.miss_prob_cost,
            "partial_fill_cost": self.costs.partial_fill_cost,
            "expected_net_pnl": self.expected_net_pnl,
            "heuristic_score": self.heuristic_score,
            "disagree_flag": self.disagree_vs_heuristic,
            "mode": self.provenance.mode,
            "provenance_json": {
                "read_source": self.provenance.read_source,
                "calibrator_version_id": self.provenance.calibrator_version_id,
                "calibrator_method": self.provenance.calibrator_method,
                "calibrator_sample_count": self.provenance.calibrator_sample_count,
                "regime_bucket": self.provenance.regime_bucket,
                "cold_start": self.provenance.cold_start,
                "produced_at": self.provenance.produced_at.isoformat(),
            },
        }


# ---------------------------------------------------------------------------
# Mode / gating helpers
# ---------------------------------------------------------------------------


def current_mode() -> str:
    m = (getattr(settings, "brain_net_edge_ranker_mode", MODE_OFF) or MODE_OFF).strip().lower()
    return m if m in _VALID_MODES else MODE_OFF


def mode_is_active() -> bool:
    """True when the ranker should compute a score (any mode except ``off``)."""
    return current_mode() != MODE_OFF


def mode_is_authoritative() -> bool:
    """True only when the ranker is allowed to drive trading decisions."""
    return current_mode() == MODE_AUTHORITATIVE


# ---------------------------------------------------------------------------
# Cost model (pre-Phase-F: composition of existing adaptive-spread + defaults)
# ---------------------------------------------------------------------------


def _is_crypto_ctx(asset_class: str) -> bool:
    return (asset_class or "").strip().lower() == "crypto"


def _spread_cost_fraction(db: Session, ctx: NetEdgeSignalContext) -> float:
    """Return spread cost in fraction-of-notional (one-way)."""
    try:
        from .execution_quality import suggest_adaptive_spread

        sug = suggest_adaptive_spread(db)
        raw = sug.get("suggested_spread")
        if raw is None:
            raw = float(settings.backtest_spread)
        return max(0.0, float(raw))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[net_edge] spread cost fell back to settings: %s", exc)
        return max(0.0, float(getattr(settings, "backtest_spread", 0.0005)))


def _slippage_cost_fraction(db: Session, ctx: NetEdgeSignalContext) -> float:
    """Half-spread slippage proxy until Phase F ships venue-truth."""
    # Pre-Phase-F proxy: half the adaptive spread for market-like fills.
    return 0.5 * _spread_cost_fraction(db, ctx)


def _fees_cost_fraction(ctx: NetEdgeSignalContext) -> float:
    if _is_crypto_ctx(ctx.asset_class):
        return _DEFAULT_FEES_BPS_CRYPTO / 10_000.0
    return _DEFAULT_FEES_BPS_EQUITY / 10_000.0


def _miss_prob_cost_fraction(ctx: NetEdgeSignalContext) -> float:
    """Expected loss from missed fills (fraction-of-notional).

    A missed market entry wastes opportunity but does not directly cost
    capital. We model it as a small drag equal to the miss probability times
    one spread - i.e. if you miss, you retry on the next bar and eat another
    spread. Phase F replaces this with data-driven partial/queue probabilities.
    """
    miss_prob = (
        _DEFAULT_MISS_PROB_CRYPTO if _is_crypto_ctx(ctx.asset_class) else _DEFAULT_MISS_PROB_EQUITY
    )
    # Use a static spread estimate here to keep this function cheap and DB-free.
    spread_fraction = float(getattr(settings, "backtest_spread", 0.0005))
    return max(0.0, miss_prob * spread_fraction)


def _partial_fill_cost_fraction(ctx: NetEdgeSignalContext) -> float:
    bps = (
        _DEFAULT_PARTIAL_FILL_BPS_CRYPTO
        if _is_crypto_ctx(ctx.asset_class)
        else _DEFAULT_PARTIAL_FILL_BPS_EQUITY
    )
    return max(0.0, bps / 10_000.0)


def _compose_costs(db: Session, ctx: NetEdgeSignalContext) -> NetEdgeCostBreakdown:
    return NetEdgeCostBreakdown(
        spread_cost=_spread_cost_fraction(db, ctx),
        slippage_cost=_slippage_cost_fraction(db, ctx),
        fees_cost=_fees_cost_fraction(ctx),
        miss_prob_cost=_miss_prob_cost_fraction(ctx),
        partial_fill_cost=_partial_fill_cost_fraction(ctx),
    )


# ---------------------------------------------------------------------------
# Calibrator
# ---------------------------------------------------------------------------


def _regime_bucket(regime: str | None) -> str:
    r = (regime or "unknown").strip().lower()
    # Collapse obvious synonyms to stabilize buckets.
    if r in ("", "none", "na"):
        return "unknown"
    if r in ("bull", "risk_on", "trending_up"):
        return "risk_on"
    if r in ("bear", "risk_off", "trending_down"):
        return "risk_off"
    if r in ("chop", "range", "ranging"):
        return "range"
    if r in ("high_vol", "vol_expansion"):
        return "high_vol"
    return r


def _load_training_pairs(
    db: Session, *, asset_class: str | None, regime_bucket: str, lookback_days: int = 180
) -> list[tuple[float, int]]:
    """Return ``(raw_prob, realized_win)`` pairs for calibrator fitting.

    We source realized outcomes from closed :class:`Trade` and exited
    :class:`PaperTrade` rows whose linked :class:`ScanPattern` carries a
    sensible ``oos_win_rate`` or ``win_rate`` (the current raw prob proxy).

    This is deliberately simple in v1. Phase D (triple-barrier labels) will
    supply a more honest ``realized_win`` signal; the calibrator interface
    does not need to change.
    """
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    pairs: list[tuple[float, int]] = []

    trades: Iterable[Trade] = (
        db.query(Trade)
        .filter(Trade.exit_date.isnot(None), Trade.entry_date >= cutoff)
        .order_by(desc(Trade.exit_date))
        .limit(2000)
        .all()
    )
    for t in trades:
        if not t.scan_pattern_id or t.pnl is None:
            continue
        pat = db.query(ScanPattern).filter(ScanPattern.id == int(t.scan_pattern_id)).one_or_none()
        if pat is None:
            continue
        raw = _pattern_raw_prob(pat)
        if raw is None:
            continue
        if asset_class and (pat.asset_class or "").lower() not in (asset_class.lower(), "all"):
            continue
        pairs.append((raw, 1 if float(t.pnl) > 0 else 0))

    papers: Iterable[PaperTrade] = (
        db.query(PaperTrade)
        .filter(PaperTrade.exit_date.isnot(None), PaperTrade.entry_date >= cutoff)
        .order_by(desc(PaperTrade.exit_date))
        .limit(4000)
        .all()
    )
    for pt in papers:
        if not pt.scan_pattern_id or pt.exit_price is None or pt.entry_price is None:
            continue
        pat = db.query(ScanPattern).filter(ScanPattern.id == int(pt.scan_pattern_id)).one_or_none()
        if pat is None:
            continue
        raw = _pattern_raw_prob(pat)
        if raw is None:
            continue
        if asset_class and (pat.asset_class or "").lower() not in (asset_class.lower(), "all"):
            continue
        win = 1 if float(pt.exit_price) > float(pt.entry_price) else 0
        pairs.append((raw, win))

    return pairs


def _pattern_raw_prob(pat: ScanPattern) -> float | None:
    for attr in ("oos_win_rate", "win_rate"):
        v = getattr(pat, attr, None)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        # Accept both 0-1 and 0-100 scales.
        if f > 1.0:
            f = f / 100.0
        if 0.0 <= f <= 1.0:
            return f
    return None


def _try_isotonic() -> Any | None:
    try:
        from sklearn.isotonic import IsotonicRegression  # type: ignore

        return IsotonicRegression(out_of_bounds="clip")
    except Exception:  # pragma: no cover - sklearn optional fail-open
        return None


class _Calibrator:
    """Per-bucket isotonic calibrator with a safe identity fallback."""

    def __init__(
        self,
        *,
        method: str,
        sample_count: int,
        version_id: str,
        regime_bucket: str,
        asset_class: str | None,
        fit: Any | None = None,
    ) -> None:
        self.method = method
        self.sample_count = sample_count
        self.version_id = version_id
        self.regime_bucket = regime_bucket
        self.asset_class = asset_class
        self._fit = fit

    def apply(self, raw_prob: float) -> float:
        p = max(0.0, min(1.0, float(raw_prob)))
        if self._fit is None:
            return p
        try:
            import numpy as np

            out = float(self._fit.predict(np.asarray([p]))[0])
            return max(0.0, min(1.0, out))
        except Exception:  # pragma: no cover - defensive
            return p

    @property
    def cold_start(self) -> bool:
        return self._fit is None


_CACHE: dict[tuple[str, str], tuple[_Calibrator, float]] = {}


def _cached_calibrator(asset_class: str, regime_bucket: str) -> _Calibrator | None:
    ttl = max(1, int(getattr(settings, "brain_net_edge_cache_ttl_s", 300)))
    key = (asset_class, regime_bucket)
    hit = _CACHE.get(key)
    if hit is not None:
        cal, fitted_at = hit
        if time.time() - fitted_at < ttl:
            return cal
    return None


def _store_calibrator(asset_class: str, regime_bucket: str, cal: _Calibrator) -> None:
    _CACHE[(asset_class, regime_bucket)] = (cal, time.time())


def _fit_calibrator(
    db: Session, *, asset_class: str, regime_bucket: str
) -> _Calibrator:
    """Fit (or identity-fallback) a calibrator for this bucket.

    Never raises. When sample count is below ``brain_net_edge_min_samples``
    we return a cold-start calibrator (identity). When scikit-learn is not
    available we also fall back to identity.
    """
    min_samples = int(getattr(settings, "brain_net_edge_min_samples", 50))
    version_id = f"netedge_{asset_class}_{regime_bucket}_{int(time.time())}"

    try:
        pairs = _load_training_pairs(
            db, asset_class=asset_class, regime_bucket=regime_bucket
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[net_edge] training pair load failed: %s", exc)
        pairs = []

    if len(pairs) < min_samples:
        return _Calibrator(
            method="cold_start",
            sample_count=len(pairs),
            version_id=version_id,
            regime_bucket=regime_bucket,
            asset_class=asset_class,
            fit=None,
        )

    model = _try_isotonic()
    if model is None:
        return _Calibrator(
            method="cold_start",
            sample_count=len(pairs),
            version_id=version_id,
            regime_bucket=regime_bucket,
            asset_class=asset_class,
            fit=None,
        )

    try:
        import numpy as np

        xs = np.asarray([p[0] for p in pairs], dtype=float)
        ys = np.asarray([p[1] for p in pairs], dtype=float)
        model.fit(xs, ys)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[net_edge] isotonic fit failed, using identity: %s", exc)
        return _Calibrator(
            method="cold_start",
            sample_count=len(pairs),
            version_id=version_id,
            regime_bucket=regime_bucket,
            asset_class=asset_class,
            fit=None,
        )

    return _Calibrator(
        method="isotonic",
        sample_count=len(pairs),
        version_id=version_id,
        regime_bucket=regime_bucket,
        asset_class=asset_class,
        fit=model,
    )


def _calibrator_for(db: Session, ctx: NetEdgeSignalContext) -> _Calibrator:
    asset = "crypto" if _is_crypto_ctx(ctx.asset_class) else "stock"
    bucket = _regime_bucket(ctx.regime)
    cached = _cached_calibrator(asset, bucket)
    if cached is not None:
        return cached
    cal = _fit_calibrator(db, asset_class=asset, regime_bucket=bucket)
    _store_calibrator(asset, bucket, cal)
    return cal


# ---------------------------------------------------------------------------
# Core score
# ---------------------------------------------------------------------------


def _fraction_to_stop(ctx: NetEdgeSignalContext) -> float | None:
    if ctx.entry_price is None or ctx.stop_price is None:
        return None
    e = float(ctx.entry_price)
    s = float(ctx.stop_price)
    if e <= 0 or s <= 0:
        return None
    return abs(e - s) / e


def _payoff_fraction(ctx: NetEdgeSignalContext, loss_per_unit: float) -> float:
    if ctx.target_price and ctx.entry_price:
        e = float(ctx.entry_price)
        t = float(ctx.target_price)
        if e > 0 and t > 0:
            return max(0.0, abs(t - e) / e)
    # Default to 2R if no explicit target.
    return 2.0 * loss_per_unit


def _ctx_hash(ctx: NetEdgeSignalContext) -> str:
    blob = json.dumps(
        {
            "t": ctx.ticker,
            "ac": ctx.asset_class,
            "pid": ctx.scan_pattern_id,
            "rp": round(float(ctx.raw_prob or 0.0), 4),
            "e": round(float(ctx.entry_price or 0.0), 6),
            "s": round(float(ctx.stop_price or 0.0), 6),
            "tg": round(float(ctx.target_price or 0.0), 6),
            "rg": _regime_bucket(ctx.regime),
            "tf": ctx.timeframe or "na",
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _is_disagreement(net_edge: float, heuristic: float | None) -> bool:
    """Cheap disagreement test: do we flip sign vs heuristic?"""
    if heuristic is None:
        return False
    try:
        return (float(net_edge) > 0.0) != (float(heuristic) > 0.0)
    except (TypeError, ValueError):
        return False


def score(db: Session, ctx: NetEdgeSignalContext) -> NetEdgeScore | None:
    """Compute a NetEdgeScore for *ctx*, honoring ``brain_net_edge_ranker_mode``.

    Returns ``None`` when the ranker is ``off`` or when scoring fails. Callers
    MUST tolerate ``None`` and fall back to their existing heuristic. This
    function does not gate any trading action - wiring into entries/sizing is
    the caller's decision and must be guarded by :func:`mode_is_authoritative`.
    """
    mode = current_mode()
    if mode == MODE_OFF:
        return None

    try:
        loss_per_unit = _fraction_to_stop(ctx)
        if loss_per_unit is None or loss_per_unit <= 0:
            _emit_ops_log(
                mode=mode,
                read=READ_ERROR,
                decision_id=ctx.decision_id,
                pattern_id=ctx.scan_pattern_id,
                asset_class=ctx.asset_class,
                regime=_regime_bucket(ctx.regime),
                net_edge=None,
                heuristic_score=ctx.heuristic_score,
                disagree=False,
            )
            return None

        cal = _calibrator_for(db, ctx)
        calibrated_prob = cal.apply(ctx.raw_prob)
        payoff = _payoff_fraction(ctx, loss_per_unit)
        costs = _compose_costs(db, ctx)

        expected_net_pnl = (
            calibrated_prob * payoff
            - (1.0 - calibrated_prob) * loss_per_unit
            - costs.total
        )

        disagree = _is_disagreement(expected_net_pnl, ctx.heuristic_score)
        read_source = _classify_read(mode=mode, cold_start=cal.cold_start, disagree=disagree)

        provenance = NetEdgeProvenance(
            mode=mode,
            read_source=read_source,
            calibrator_version_id=cal.version_id,
            calibrator_method=cal.method,
            calibrator_sample_count=cal.sample_count,
            regime_bucket=cal.regime_bucket,
            cold_start=cal.cold_start,
            produced_at=datetime.utcnow(),
        )

        result = NetEdgeScore(
            decision_id=ctx.decision_id,
            ticker=ctx.ticker,
            asset_class="crypto" if _is_crypto_ctx(ctx.asset_class) else "stock",
            scan_pattern_id=ctx.scan_pattern_id,
            calibrated_prob=calibrated_prob,
            expected_payoff=payoff,
            loss_per_unit=loss_per_unit,
            costs=costs,
            expected_net_pnl=expected_net_pnl,
            heuristic_score=ctx.heuristic_score,
            disagree_vs_heuristic=disagree,
            provenance=provenance,
        )

        _write_score_log(db, ctx, result)
        _emit_ops_log(
            mode=mode,
            read=read_source,
            decision_id=ctx.decision_id,
            pattern_id=ctx.scan_pattern_id,
            asset_class=result.asset_class,
            regime=cal.regime_bucket,
            net_edge=expected_net_pnl,
            heuristic_score=ctx.heuristic_score,
            disagree=disagree,
        )
        return result
    except Exception as exc:
        logger.warning("[net_edge] score failed: %s", exc, exc_info=True)
        _emit_ops_log(
            mode=mode,
            read=READ_ERROR,
            decision_id=ctx.decision_id,
            pattern_id=ctx.scan_pattern_id,
            asset_class=ctx.asset_class,
            regime=_regime_bucket(ctx.regime),
            net_edge=None,
            heuristic_score=ctx.heuristic_score,
            disagree=False,
        )
        return None


def _classify_read(*, mode: str, cold_start: bool, disagree: bool) -> str:
    if cold_start:
        return READ_COLD_START
    if mode == MODE_SHADOW:
        return READ_SHADOW
    if mode == MODE_COMPARE:
        return READ_COMPARE_DISAGREE if disagree else READ_COMPARE_OK
    if mode == MODE_AUTHORITATIVE:
        return READ_AUTHORITATIVE
    return READ_NA


def _write_score_log(db: Session, ctx: NetEdgeSignalContext, result: NetEdgeScore) -> None:
    try:
        payload = result.to_log_dict()
        row = NetEdgeScoreLog(
            decision_id=payload["decision_id"],
            scan_pattern_id=payload["scan_pattern_id"],
            ticker=payload["ticker"],
            asset_class=payload["asset_class"],
            regime=result.provenance.regime_bucket,
            ctx_hash=_ctx_hash(ctx),
            calibrated_prob=payload["calibrated_prob"],
            expected_payoff=payload["expected_payoff"],
            spread_cost=payload["spread_cost"],
            slippage_cost=payload["slippage_cost"],
            fees_cost=payload["fees_cost"],
            miss_prob_cost=payload["miss_prob_cost"],
            partial_fill_cost=payload["partial_fill_cost"],
            expected_net_pnl=payload["expected_net_pnl"],
            heuristic_score=payload["heuristic_score"],
            disagree_flag=bool(payload["disagree_flag"]),
            mode=payload["mode"],
            provenance_json=payload["provenance_json"],
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        logger.warning("[net_edge] score log write failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - defensive
            pass


def _emit_ops_log(
    *,
    mode: str,
    read: str,
    decision_id: str,
    pattern_id: int | None,
    asset_class: str | None,
    regime: str | None,
    net_edge: float | None,
    heuristic_score: float | None,
    disagree: bool,
) -> None:
    if not bool(getattr(settings, "brain_net_edge_ops_log_enabled", True)):
        return
    sample_pct = float(getattr(settings, "brain_net_edge_shadow_sample_pct", 1.0))
    line = format_net_edge_ops_line(
        mode=mode,
        read=read,
        decision_id=decision_id,
        pattern_id=pattern_id,
        asset_class=asset_class,
        regime=regime,
        net_edge=net_edge,
        heuristic_score=heuristic_score,
        disagree=disagree,
        sample_pct=sample_pct,
    )
    logger.info(line)


# ---------------------------------------------------------------------------
# Diagnostics (read-only)
# ---------------------------------------------------------------------------


def diagnostics(db: Session, *, lookback_hours: int = 24) -> dict[str, Any]:
    """Return reliability / Brier / disagreement metrics for the ops endpoint.

    Shape is frozen so the `/api/trading/brain/net-edge/diagnostics` endpoint
    is stable. Always returns a dict, even on empty DB (cold-start shape).
    """
    cutoff = datetime.utcnow() - timedelta(hours=max(1, int(lookback_hours)))
    payload: dict[str, Any] = {
        "ok": True,
        "mode": current_mode(),
        "lookback_hours": int(lookback_hours),
        "sample_count": 0,
        "disagreement_rate": None,
        "brier_score": None,
        "per_regime": [],
        "last_calibration": None,
    }

    try:
        rows = (
            db.query(NetEdgeScoreLog)
            .filter(NetEdgeScoreLog.created_at >= cutoff)
            .order_by(desc(NetEdgeScoreLog.created_at))
            .limit(5000)
            .all()
        )
        payload["sample_count"] = len(rows)
        if rows:
            disagreements = sum(1 for r in rows if bool(r.disagree_flag))
            payload["disagreement_rate"] = round(disagreements / len(rows), 4)

            by_regime: dict[str, list[NetEdgeScoreLog]] = {}
            for r in rows:
                by_regime.setdefault(r.regime or "unknown", []).append(r)
            payload["per_regime"] = [
                {
                    "regime": rg,
                    "sample_count": len(items),
                    "disagreement_rate": round(
                        sum(1 for r in items if bool(r.disagree_flag)) / len(items), 4
                    ),
                    "avg_net_edge": round(
                        sum(float(r.expected_net_pnl or 0.0) for r in items) / len(items), 6
                    ),
                }
                for rg, items in sorted(by_regime.items())
            ]

        cal = (
            db.query(NetEdgeCalibrationSnapshot)
            .order_by(desc(NetEdgeCalibrationSnapshot.fitted_at))
            .first()
        )
        if cal is not None:
            payload["last_calibration"] = {
                "version_id": cal.version_id,
                "method": cal.method,
                "sample_count": int(cal.sample_count or 0),
                "regime": cal.regime,
                "asset_class": cal.asset_class,
                "brier_score": cal.brier_score,
                "fitted_at": cal.fitted_at.isoformat() if cal.fitted_at else None,
                "is_active": bool(cal.is_active),
            }
    except Exception as exc:
        logger.warning("[net_edge] diagnostics failed: %s", exc)
        payload["ok"] = False
        payload["error"] = str(exc)

    return payload


# Exported for tests and callers.
__all__ = [
    "NetEdgeCostBreakdown",
    "NetEdgeProvenance",
    "NetEdgeScore",
    "NetEdgeSignalContext",
    "current_mode",
    "diagnostics",
    "mode_is_active",
    "mode_is_authoritative",
    "score",
]
