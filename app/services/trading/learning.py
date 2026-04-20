"""Learning: pattern mining, deep study, learning cycles, brain stats."""
from __future__ import annotations

import json
import logging
import math
import os
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import func

from ...models.trading import (
    LearningEvent,
    MarketSnapshot,
    ScanPattern,
    ScanResult,
    TradingInsight,
    Trade,
)
from .market_data import (
    fetch_quote, fetch_quotes_batch, fetch_ohlcv_df, get_indicator_snapshot,
    get_vix, get_volatility_regime, DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
    _use_massive, _use_polygon,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights, save_insight, preload_active_insights, get_trade_stats_by_pattern
from .brain_resource_budget import BrainResourceBudget
from .brain_io_concurrency import (
    io_workers_high,
    io_workers_low,
    io_workers_med,
    io_workers_for_snapshot_batch,
    snapshot_batch_stats,
)
from .snapshot_bar_ops import (
    dedupe_sample_rows,
    normalize_bar_start_utc,
    try_insert_insight_evidence,
    upsert_market_snapshot,
)
from .learning_cycle_architecture import (
    apply_learning_cycle_step_status,
    apply_learning_cycle_step_status_progress,
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
    SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID,
)
from .learning_cycle_steps import load_prescreen_scan_and_universe, run_secondary_miners_phase

from .learning_predictions import (
    _build_prediction_tickers,
    _get_current_predictions_impl as _lp_get_current_predictions_impl,
    _indicator_data_to_flat_snapshot,
    compute_prediction,
    predict_confidence,
    predict_direction,
)


def _get_current_predictions_impl(*args, **kwargs):
    return _lp_get_current_predictions_impl(*args, **kwargs)


logger = logging.getLogger(__name__)

_CPU_COUNT = os.cpu_count() or 4

_shutting_down = threading.Event()

# Stale-while-revalidate cache for get_current_predictions
_pred_cache: dict[str, Any] = {"results": [], "ts": 0.0}
_PRED_CACHE_TTL = 180       # 3 min fresh
_PRED_CACHE_STALE_TTL = 600  # 10 min stale-while-revalidate
_pred_refreshing = False
_pred_refresh_lock = threading.Lock()


def get_prediction_swr_cache_meta() -> dict[str, Any]:
    """Read-only metadata for live-prediction SWR cache (no API keys, no ticker lists).

    Used by opportunity board freshness: ``data_as_of`` must not pretend predictions
    are newer than this wall time when serving from cache.
    """
    with _pred_refresh_lock:
        ts = float(_pred_cache.get("ts") or 0.0)
        n = len(_pred_cache.get("results") or [])
        refreshing = bool(_pred_refreshing)
    now = time.time()
    return {
        "cache_last_updated_unix": ts,
        "cache_age_seconds": round(now - ts, 3) if ts > 0 else None,
        "cached_result_count": n,
        "background_refresh_in_progress": refreshing,
    }


def _persist_prediction_runtime_surface(results: list[dict] | None) -> None:
    try:
        from .runtime_surface_state import persist_runtime_surface_now

        persist_runtime_surface_now(
            surface="predictions",
            state="ok",
            source="get_current_predictions",
            as_of=datetime.utcnow(),
            details={"cached_result_count": len(results or [])},
            updated_by="learning",
        )
    except Exception:
        logger.debug("[learning] prediction runtime surface persist failed", exc_info=True)


def get_current_predictions(db: Session, tickers: list[str] | None = None) -> list[dict]:
    """Generate live predictions for a set of tickers.

    Blends rule-based scores with ML probabilities and adjusts for
    volatility regime. Includes risk-management fields (stop, target, R:R).
    Uses ThreadPoolExecutor to process tickers in parallel for speed.

    When *tickers* is None (the common case from Top Picks), results are
    cached with stale-while-revalidate: 3 min fresh, 10 min stale.
    Explicit ticker lists bypass the cache.

    Prediction-mirror authority contract (this is the frozen contract
    referenced by ``CLAUDE.md`` Hard Rule 5):

    * ``explicit_api_tickers=True`` — caller passed a non-empty,
      caller-chosen ticker list. Phase 7+ candidate-authoritative mirror
      reads are allowed. The operator has an explicit intent, so the
      read is safe to authorize against the mirror DB.
    * ``explicit_api_tickers=False`` — caller passed ``None``, an empty
      list, or the cache/SWR background refresh triggered the call.
      These read against the legacy path; the mirror is not consulted.

    The release-blocker check in ``trading_brain`` enforces that no
    ``[chili_prediction_ops]`` line ever has
    ``read=auth_mirror AND explicit_api_tickers=False`` together. If you
    are about to change the signature or the branches below, read
    ``docs/TRADING_BRAIN_LIVE_BRACKETS_ROLLOUT.md`` and the rollout docs
    under ``docs/`` for the prediction mirror migration BEFORE editing;
    this is not a safe-to-improvise path.
    """
    global _pred_cache, _pred_refreshing

    if tickers is not None:
        explicit_for_mirror = bool(tickers)
        return _get_current_predictions_impl(
            db, tickers, explicit_api_tickers=explicit_for_mirror
        )

    now = time.time()
    age = now - _pred_cache["ts"]

    if _pred_cache["results"] and age < _PRED_CACHE_TTL:
        return _pred_cache["results"]

    if _pred_cache["results"] and age < _PRED_CACHE_STALE_TTL:
        with _pred_refresh_lock:
            if not _pred_refreshing:
                _pred_refreshing = True

                def _bg_refresh():
                    global _pred_cache, _pred_refreshing
                    try:
                        from ...db import SessionLocal
                        s = SessionLocal()
                        try:
                            fresh = _get_current_predictions_impl(s, None, explicit_api_tickers=False)
                            _pred_cache = {"results": fresh, "ts": time.time()}
                            _persist_prediction_runtime_surface(fresh)
                        finally:
                            s.close()
                    except Exception:
                        logger.debug("Background prediction refresh failed", exc_info=True)
                    finally:
                        _pred_refreshing = False

                threading.Thread(target=_bg_refresh, daemon=True).start()
        return _pred_cache["results"]

    results = _get_current_predictions_impl(db, None, explicit_api_tickers=False)
    _pred_cache = {"results": results, "ts": time.time()}
    _persist_prediction_runtime_surface(results)
    return results


def refresh_promoted_prediction_cache(db: Session) -> dict[str, Any]:
    """Recompute ``get_current_predictions`` SWR cache using promoted ScanPatterns only.

    No mining or backtests. Called at end of ``run_learning_cycle`` and optionally from
    APScheduler when learning is idle.
    """
    from ...config import settings
    from ...models.trading import ScanPattern

    if not getattr(settings, "brain_fast_eval_enabled", True):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    promoted = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.promotion_status == "promoted",
        )
        .all()
    )
    if not promoted:
        return {"ok": True, "skipped": True, "reason": "no_promoted_patterns"}

    tickers = _build_prediction_tickers(db, None)
    max_n = max(1, int(getattr(settings, "brain_fast_eval_max_tickers", 400)))
    ticker_batch = tickers[:max_n]

    results = _get_current_predictions_impl(
        db,
        ticker_batch,
        explicit_api_tickers=False,
        active_patterns_override=promoted,
        max_ticker_batch=max_n,
    )

    global _pred_cache, _pred_refreshing
    with _pred_refresh_lock:
        _pred_cache = {"results": results, "ts": time.time()}
        _pred_refreshing = False
    _persist_prediction_runtime_surface(results)

    logger.info(
        "[learning] Promoted prediction cache: tickers=%s patterns=%s predictions=%s",
        len(ticker_batch),
        len(promoted),
        len(results),
    )
    return {
        "ok": True,
        "tickers": len(ticker_batch),
        "promoted_patterns": len(promoted),
        "predictions": len(results),
    }



def signal_shutdown():
    _shutting_down.set()


def shutdown_requested() -> bool:
    """True after cooperative stop (file, DB stop_requested, or in-process signal)."""
    return _shutting_down.is_set()


# Origins that must pass OOS / spread-aware backtests before staying active (see config brain_oos_*).
_BRAIN_OOS_GATED_ORIGINS = frozenset({"web_discovered", "brain_discovered"})

# Miner / evolution taxonomy — keep compression (pre-expansion) vs high-vol regime separate.
BRAIN_HYPOTHESIS_FAMILY_COMPRESSION = "compression_expansion"
BRAIN_HYPOTHESIS_FAMILY_HIGH_VOL = "high_vol_regime"


def _trim_bench_walk_forward_for_db(bench: dict[str, Any]) -> dict[str, Any]:
    """Drop per-window arrays from bench JSON to keep scan_patterns row small."""
    out: dict[str, Any] = {k: v for k, v in bench.items() if k != "tickers"}
    tickers: dict[str, Any] = {}
    for sym, rec in (bench.get("tickers") or {}).items():
        if not isinstance(rec, dict):
            continue
        tickers[sym] = {k: v for k, v in rec.items() if k != "windows"}
    out["tickers"] = tickers
    return out


def brain_apply_bench_promotion_gate(
    *,
    origin: str,
    bench_summary: dict[str, Any] | None,
    current_promotion_status: str,
) -> tuple[str | None, bool]:
    """Optional stricter gate using benchmark walk-forward *passes_gate*.

    Returns ``(promotion_status_override, allow_active)``.  ``None`` status means
    keep *current_promotion_status*.
    """
    from ...config import settings

    if not settings.brain_bench_walk_forward_gate_enabled:
        return None, True
    o = (origin or "").strip().lower()
    if o not in _BRAIN_OOS_GATED_ORIGINS:
        return None, True
    cur = (current_promotion_status or "").strip().lower()
    if not bench_summary or not bench_summary.get("ok"):
        if cur == "promoted":
            return None, True
        return "pending_bench", True
    if not bench_summary.get("passes_gate"):
        return "rejected_bench", False
    return None, True


def brain_pattern_backtest_friction_kwargs(db: Session | None = None) -> dict[str, Any]:
    """Spread, commission, and OOS holdout for pattern hypothesis backtests.

    When *db* is provided, attempts to use realised execution slippage
    (P90) via ``suggest_adaptive_spread`` so backtests reflect actual
    fill quality.  Falls back to the static ``settings.backtest_spread``
    when there is insufficient data or on error.
    """
    from ...config import settings

    spread = float(settings.backtest_spread)

    if db is not None:
        try:
            from .execution_quality import suggest_adaptive_spread

            suggestion = suggest_adaptive_spread(db)
            if suggestion.get("should_update") and suggestion.get("suggested_spread") is not None:
                spread = float(suggestion["suggested_spread"])
                logger.info(
                    "[friction] adaptive spread override: %.4f → %.4f  "
                    "(p90=%.2f%%, trades=%d)",
                    float(settings.backtest_spread),
                    spread,
                    suggestion.get("p90_slippage_pct", 0),
                    suggestion.get("trades_measured", 0),
                )
        except Exception:
            logger.debug("[friction] adaptive spread lookup failed; using static", exc_info=True)

    return {
        "spread": spread,
        "commission": float(settings.backtest_commission),
        "oos_holdout_fraction": float(settings.brain_oos_holdout_fraction),
    }


def brain_oos_gate_kwargs_for_pattern(pattern: Any | None, oos_trade_sum: int) -> dict[str, Any]:
    """Resolved thresholds for ``brain_apply_oos_promotion_gate`` (stricter short-TF / crypto / family)."""
    from ...config import settings

    min_wr = float(settings.brain_oos_min_win_rate_pct)
    max_gap = float(settings.brain_oos_max_is_oos_gap_pct)
    min_agg: int | None = None
    if pattern is not None:
        tf = (getattr(pattern, "timeframe", None) or "1d").strip().lower()
        ac = (getattr(pattern, "asset_class", None) or "all").strip().lower()
        fam = (getattr(pattern, "hypothesis_family", None) or "").strip().lower()
        if tf in ("1m", "5m", "15m", "1h"):
            v = settings.brain_oos_min_win_rate_pct_short_tf
            if v is not None:
                min_wr = max(min_wr, float(v))
            t = settings.brain_oos_min_oos_trades_short_tf
            if t is not None:
                min_agg = max(min_agg or 0, int(t))
        if ac == "crypto":
            v = settings.brain_oos_min_win_rate_pct_crypto
            if v is not None:
                min_wr = max(min_wr, float(v))
            t = settings.brain_oos_min_oos_trades_crypto
            if t is not None:
                min_agg = max(min_agg or 0, int(t))
        if fam == BRAIN_HYPOTHESIS_FAMILY_HIGH_VOL:
            v = settings.brain_oos_min_win_rate_pct_high_vol_family
            if v is not None:
                min_wr = max(min_wr, float(v))
            t = settings.brain_oos_min_oos_trades_high_vol_family
            if t is not None:
                min_agg = max(min_agg or 0, int(t))
    return {
        "min_win_rate_pct": min_wr,
        "max_is_oos_gap_pct": max_gap,
        "min_oos_aggregate_trades": min_agg,
        "oos_aggregate_trade_count": oos_trade_sum,
    }


def _find_dead_tickers(db, tickers: list[str], *, stale_days: int = 14) -> set[str]:
    """Find tickers with no recent OHLCV data (delisted or data unavailable)."""
    from ...models.trading import MarketSnapshot
    from sqlalchemy import func as sa_func

    if not tickers:
        return set()
    cutoff = datetime.utcnow() - timedelta(days=stale_days)
    # Tickers with at least one snapshot but none recent → likely dead
    latest = (
        db.query(
            MarketSnapshot.ticker,
            sa_func.max(MarketSnapshot.snapshot_date).label("last_snap"),
        )
        .filter(MarketSnapshot.ticker.in_(tickers))
        .group_by(MarketSnapshot.ticker)
        .all()
    )
    dead: set[str] = set()
    seen = set()
    for ticker, last_snap in latest:
        seen.add(ticker)
        if last_snap and last_snap < cutoff:
            dead.add(ticker)
    # Tickers with zero snapshots at all are also dead
    for t in tickers:
        if t not in seen:
            dead.add(t)
    return dead


def brain_apply_oos_promotion_gate(
    *,
    origin: str,
    mean_is_win_rate: float,
    mean_oos_win_rate: float | None,
    oos_tickers_with_result: int,
    min_win_rate_pct: float | None = None,
    max_is_oos_gap_pct: float | None = None,
    min_oos_aggregate_trades: int | None = None,
    oos_aggregate_trade_count: int | None = None,
    mean_oos_expectancy_pct: float | None = None,
    mean_oos_profit_factor: float | None = None,
    oos_wr_robust_min: float | None = None,
    oos_bootstrap_wr_ci_low: float | None = None,
    walk_forward_passes_gate: bool | None = None,
) -> tuple[str, bool]:
    """Return ``(promotion_status, allow_active)``.

    ``allow_active`` False means the caller should set ``ScanPattern.active = False``.

    Discovery-phase miners never call this — only backtest evidence paths. Optional aggregate
    OOS trade floor (``min_oos_aggregate_trades``) reduces promotion on thin short-horizon samples.

    P1.3 — ``walk_forward_passes_gate`` is a tri-state:

        * ``None`` — walk-forward result not supplied. When the global
          ``chili_walk_forward_enabled`` flag is OFF this is a pass-through
          (legacy behavior). When the flag is ON, a missing walk-forward
          result downgrades to ``pending_oos`` — we can't promote a
          pattern we haven't walked forward.
        * ``True``  — walk-forward passed (see
          ``run_walk_forward(...).passes_gate``). Continues through the
          other gates as before.
        * ``False`` — walk-forward failed. Hard-reject the pattern so it
          can't be promoted on OOS-only evidence.
    """
    from ...config import settings

    if not settings.brain_oos_gate_enabled:
        return "legacy", True
    o = (origin or "").strip().lower()
    if o not in _BRAIN_OOS_GATED_ORIGINS:
        return "legacy", True
    min_wr = (
        float(min_win_rate_pct)
        if min_win_rate_pct is not None
        else float(settings.brain_oos_min_win_rate_pct)
    )
    max_gap = (
        float(max_is_oos_gap_pct)
        if max_is_oos_gap_pct is not None
        else float(settings.brain_oos_max_is_oos_gap_pct)
    )
    if oos_tickers_with_result < int(settings.brain_oos_min_evaluated_tickers):
        return "pending_oos", True
    if mean_oos_win_rate is None:
        return "pending_oos", True
    agg_floor = (
        int(min_oos_aggregate_trades)
        if min_oos_aggregate_trades is not None
        else int(getattr(settings, "brain_oos_min_aggregate_trades", 0))
    )
    if agg_floor > 0 and int(oos_aggregate_trade_count or 0) < agg_floor:
        return "pending_oos", True
    gap = float(mean_is_win_rate) - float(mean_oos_win_rate)
    if float(mean_oos_win_rate) < min_wr:
        return "rejected_oos", False
    if gap > max_gap:
        return "rejected_oos", False

    min_exp = getattr(settings, "brain_oos_min_expectancy_pct", None)
    if min_exp is not None and mean_oos_expectancy_pct is not None:
        if float(mean_oos_expectancy_pct) < float(min_exp):
            return "rejected_oos", False

    min_pf = getattr(settings, "brain_oos_min_profit_factor", None)
    if min_pf is not None and mean_oos_profit_factor is not None:
        if float(mean_oos_profit_factor) < float(min_pf):
            return "rejected_oos", False

    if getattr(settings, "brain_oos_require_robustness_wr_above_gate", False):
        if oos_wr_robust_min is not None and float(oos_wr_robust_min) < min_wr:
            return "rejected_oos", False

    ci_floor = getattr(settings, "brain_oos_bootstrap_ci_min_wr", None)
    if ci_floor is not None and oos_bootstrap_wr_ci_low is not None:
        if float(oos_bootstrap_wr_ci_low) < float(ci_floor):
            return "rejected_oos", False

    # P1.3 — walk-forward gate. Only enforced when the feature flag is
    # on so existing promotion flows keep working verbatim until an
    # operator flips ``chili_walk_forward_enabled``.
    if bool(getattr(settings, "chili_walk_forward_enabled", False)):
        if walk_forward_passes_gate is False:
            return "rejected_walk_forward", False
        if walk_forward_passes_gate is None:
            # Flag on but no walk-forward result supplied — don't promote
            # on OOS-only evidence. Keep pattern active, flagged pending.
            return "pending_walk_forward", True

    if int(oos_aggregate_trade_count or 0) < getattr(settings, "brain_min_trades_for_promotion", 30):
        return "pending_oos", True

    return "promoted", True


def get_research_funnel_snapshot(db: Session) -> dict[str, Any]:
    """Aggregate ScanPattern promotion + backtest queue for Brain research funnel UI."""
    from sqlalchemy import func

    from ...models.trading import ScanPattern
    from .backtest_queue import get_queue_status

    rows = (
        db.query(ScanPattern.promotion_status, func.count(ScanPattern.id))
        .filter(ScanPattern.active.is_(True))
        .group_by(ScanPattern.promotion_status)
        .all()
    )
    promo_active = {str(r[0] or ""): int(r[1]) for r in rows}

    rows_i = (
        db.query(ScanPattern.promotion_status, func.count(ScanPattern.id))
        .filter(ScanPattern.active.is_(False))
        .group_by(ScanPattern.promotion_status)
        .all()
    )
    promo_inactive = {str(r[0] or ""): int(r[1]) for r in rows_i}

    qt_rows = (
        db.query(ScanPattern.queue_tier, func.count(ScanPattern.id))
        .filter(ScanPattern.active.is_(True))
        .group_by(ScanPattern.queue_tier)
        .all()
    )
    queue_tier_counts = {str(r[0] or "full"): int(r[1]) for r in qt_rows}

    total_sp = int(db.query(func.count(ScanPattern.id)).scalar() or 0)

    return {
        "promotion_status_active": promo_active,
        "promotion_status_inactive": promo_inactive,
        "queue_tier_active": queue_tier_counts,
        "scan_patterns_total": total_sp,
        "queue": get_queue_status(db, use_cache=False),
    }


def get_pattern_pipeline_near(db: Session, *, limit: int = 14) -> list[dict[str, Any]]:
    """Active ScanPatterns in pending_oos or candidate, for Brain pipeline visibility."""
    from ...models.trading import ScanPattern

    lim = max(1, min(int(limit), 40))
    rows = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.promotion_status.in_(["pending_oos", "candidate"]),
        )
        .order_by(
            ScanPattern.oos_win_rate.desc().nullslast(),
            ScanPattern.oos_trade_count.desc().nullslast(),
            ScanPattern.id.desc(),
        )
        .limit(lim)
        .all()
    )
    out: list[dict[str, Any]] = []
    for p in rows:
        out.append({
            "id": p.id,
            "name": (p.name or "")[:120],
            "promotion_status": p.promotion_status,
            "oos_win_rate": float(p.oos_win_rate) if p.oos_win_rate is not None else None,
            "oos_trade_count": int(p.oos_trade_count) if p.oos_trade_count is not None else None,
            "timeframe": p.timeframe,
            "asset_class": p.asset_class,
        })
    return out


def get_attribution_coverage_stats(db: Session, user_id: int | None) -> dict[str, Any]:
    from sqlalchemy import func

    from ...models.trading import Trade

    if user_id is None:
        return {
            "closed_trades": 0,
            "closed_with_scan_pattern_id": 0,
            "coverage_pct": None,
        }
    closed = (
        db.query(func.count(Trade.id))
        .filter(Trade.user_id == user_id, Trade.status == "closed")
        .scalar()
        or 0
    )
    with_sp = (
        db.query(func.count(Trade.id))
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.scan_pattern_id.isnot(None),
        )
        .scalar()
        or 0
    )
    pct = round(100.0 * with_sp / closed, 2) if closed else None
    return {
        "closed_trades": int(closed),
        "closed_with_scan_pattern_id": int(with_sp),
        "coverage_pct": pct,
    }


def _repaired_oos_gate_failures(pattern: Any) -> list[str]:
    from ...config import settings

    reasons: list[str] = []
    min_trades = int(getattr(settings, "brain_min_trades_for_promotion", 30) or 30)
    oos_trade_count = int(getattr(pattern, "oos_trade_count", 0) or 0)
    if oos_trade_count < min_trades:
        reasons.append(f"oos_trade_count<{min_trades}")

    oos_avg_return_pct = getattr(pattern, "oos_avg_return_pct", None)
    if oos_avg_return_pct is None or float(oos_avg_return_pct) <= 0:
        reasons.append("oos_avg_return_pct<=0")

    freshness_ref = getattr(pattern, "oos_evaluated_at", None) or getattr(pattern, "last_backtest_at", None)
    stale_cutoff = datetime.utcnow() - timedelta(days=7)
    if freshness_ref is None or freshness_ref < stale_cutoff:
        reasons.append("backtest_stale>7d")

    ov = dict(getattr(pattern, "oos_validation_json", {}) or {})
    provenance_status = str(ov.get("provenance_status") or "").strip().lower()
    if provenance_status in {"incomplete", "quarantined"}:
        reasons.append(f"provenance_{provenance_status}")

    return reasons


def run_live_pattern_depromotion(db: Session) -> dict[str, Any]:
    """Maintenance / reconcile: demote patterns when live win rate lags research OOS.

    Also updates per-pattern decay_monitor metadata and may auto-retire stale promoted rows.
    Not primary operator-facing “brain progress”; runs from execution-feedback / reconcile paths.
    """
    from ...config import settings

    if not getattr(settings, "brain_live_depromotion_enabled", False):
        return {"ok": True, "skipped": True, "decay_monitor_updated": 0}
    uid = getattr(settings, "brain_default_user_id", None)
    if uid is None:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_brain_default_user_id",
            "decay_monitor_updated": 0,
        }

    from ...models.trading import ScanPattern
    from .attribution_service import live_vs_research_by_pattern

    rep = live_vs_research_by_pattern(db, int(uid), days=120, limit=200)
    patterns = rep.get("patterns") or []
    min_n = int(getattr(settings, "brain_live_depromotion_min_closed_trades", 8))
    max_gap = float(getattr(settings, "brain_live_depromotion_max_gap_pct", 25.0))
    demoted = 0
    demoted_pattern_ids: set[int] = set()
    promoted_patterns = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.promotion_status == "promoted",
            ScanPattern.active.is_(True),
        )
        .all()
    )
    for p in promoted_patterns:
        failures = _repaired_oos_gate_failures(p)
        if not failures:
            continue
        _op = (p.promotion_status or "").strip()
        _ol = (p.lifecycle_stage or "").strip()
        from .lifecycle import transition_on_decay

        reason = "repaired_oos_gate_failed: " + ", ".join(failures[:4])
        try:
            transition_on_decay(db, p, reason=reason)
        except Exception:
            p.active = False
            p.promotion_status = "degraded_live"
            p.lifecycle_stage = "decayed"
            p.lifecycle_changed_at = datetime.utcnow()
            try:
                from .brain_work.promotion_surface import emit_promotion_surface_change

                emit_promotion_surface_change(
                    db,
                    scan_pattern_id=int(p.id),
                    old_promotion_status=_op,
                    old_lifecycle_stage=_ol,
                    new_promotion_status=(p.promotion_status or "").strip(),
                    new_lifecycle_stage=(p.lifecycle_stage or "").strip(),
                    source="run_live_pattern_depromotion_repaired_oos_fallback",
                )
            except Exception:
                logger.debug(
                    "[learning] repaired_oos depromotion emit failed",
                    exc_info=True,
                )
        demoted_pattern_ids.add(int(p.id))
        demoted += 1
    for row in patterns:
        n_live = int(row.get("live_closed_trades") or 0)
        if n_live < min_n:
            continue
        oos_wr = row.get("research_oos_win_rate_pct")
        live_wr = row.get("live_win_rate_pct")
        pid = row.get("scan_pattern_id")
        if oos_wr is None or live_wr is None or pid is None:
            continue
        if int(pid) in demoted_pattern_ids:
            continue
        if float(live_wr) < float(oos_wr) - max_gap:
            p = db.query(ScanPattern).filter(ScanPattern.id == int(pid)).first()
            if p and p.active and (p.promotion_status or "") == "promoted":
                _op = (p.promotion_status or "").strip()
                _ol = (p.lifecycle_stage or "").strip()
                from .lifecycle import transition_on_decay
                try:
                    transition_on_decay(
                        db, p,
                        reason=f"live WR {live_wr:.1f}% vs OOS {oos_wr:.1f}% (gap>{max_gap}pp)",
                    )
                except Exception:
                    p.active = False
                    p.promotion_status = "degraded_live"
                    p.lifecycle_stage = "decayed"
                    p.lifecycle_changed_at = datetime.utcnow()
                    try:
                        from .brain_work.promotion_surface import emit_promotion_surface_change

                        emit_promotion_surface_change(
                            db,
                            scan_pattern_id=int(p.id),
                            old_promotion_status=_op,
                            old_lifecycle_stage=_ol,
                            new_promotion_status=(p.promotion_status or "").strip(),
                            new_lifecycle_stage=(p.lifecycle_stage or "").strip(),
                            source="run_live_pattern_depromotion_fallback",
                        )
                    except Exception:
                        logger.debug(
                            "[learning] depromotion_fallback promotion_surface emit failed",
                            exc_info=True,
                        )
                demoted += 1
    if demoted:
        db.commit()

    touched = 0
    for row in patterns:
        pid = row.get("scan_pattern_id")
        oos_wr = row.get("research_oos_win_rate_pct")
        live_wr = row.get("live_win_rate_pct")
        n_live = int(row.get("live_closed_trades") or 0)
        if pid is None or oos_wr is None or live_wr is None:
            continue
        p = db.query(ScanPattern).filter(ScanPattern.id == int(pid)).first()
        if not p:
            continue
        ov = dict(p.oos_validation_json or {}) if isinstance(p.oos_validation_json, dict) else {}
        dm = dict(ov.get("decay_monitor") or {})
        dm["live_vs_oos_gap_pct"] = round(float(oos_wr) - float(live_wr), 2)
        dm["live_wr_pct"] = float(live_wr)
        dm["oos_wr_pct_ref"] = float(oos_wr)
        dm["live_n_closed"] = n_live
        dm["updated_at"] = datetime.utcnow().isoformat() + "Z"
        ov["decay_monitor"] = dm
        p.oos_validation_json = ov
        touched += 1
    if touched:
        db.commit()

    # D2: Auto-retire stale patterns — zero trades in 90 days + low confidence
    retired = 0
    try:
        stale_cutoff = datetime.utcnow() - timedelta(days=90)
        stale_patterns = (
            db.query(ScanPattern)
            .filter(
                ScanPattern.promotion_status == "promoted",
                ScanPattern.active.is_(True),
            )
            .all()
        )
        for sp in stale_patterns:
            ov = dict(sp.oos_validation_json or {}) if isinstance(sp.oos_validation_json, dict) else {}
            dm = ov.get("decay_monitor") or {}
            n_live = int(dm.get("live_n_closed") or 0)
            conf = float(getattr(sp, "confidence", 0.5) or 0.5)
            last_trade_str = dm.get("updated_at")
            last_active = None
            if last_trade_str:
                try:
                    last_active = datetime.fromisoformat(str(last_trade_str).replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    pass
            if n_live == 0 and conf < 0.15 and (last_active is None or last_active < stale_cutoff):
                sp.active = False
                sp.promotion_status = "retired"
                sp.lifecycle_stage = "retired"
                sp.lifecycle_changed_at = datetime.utcnow()
                ov["retirement_reason"] = "stale_no_activity_90d"
                sp.oos_validation_json = ov
                retired += 1
        if retired:
            db.commit()
            logger.info("[learning] Auto-retired %d stale promoted patterns (no trades 90d + low confidence)", retired)
    except Exception as e:
        logger.debug("[learning] Stale pattern retirement skipped: %s", e)

    return {"ok": True, "demoted": demoted, "decay_monitor_updated": touched, "retired_stale": retired}


# ── Learning Event Logger (extracted to learning_events.py) ───────────
from .learning_events import log_learning_event, get_learning_events  # noqa: F401 — re-export for backward compat


# ── AI Self-Learning ──────────────────────────────────────────────────

CLOSED_TRADE_LLM_ENRICH_MIN_MOVE_PCT = 5.0


def _trade_exit_snapshot_flat(trade: Trade) -> dict[str, Any]:
    """Flatten stored exit indicator snapshot for rule evaluation."""
    raw = trade.indicator_snapshot
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    from .pattern_position_monitor import _flatten_indicators

    return _flatten_indicators(raw)


def _closed_trade_price_move_pct(trade: Trade) -> float:
    try:
        ep = float(trade.exit_price or 0)
        xp = float(trade.entry_price or 0)
    except (TypeError, ValueError):
        return 0.0
    if xp <= 0:
        return 0.0
    if (trade.direction or "long").lower() == "short":
        return abs((xp - ep) / xp * 100.0)
    return abs((ep - xp) / xp * 100.0)


def _reinforce_patterns_at_trade_close(
    db: Session,
    trade: Trade,
    flat: dict[str, Any],
    *,
    trade_won: bool,
) -> list[str]:
    """Boost linked insights when exit-time indicators still match pattern rules (no LLM)."""
    from .pattern_engine import _eval_condition

    if trade.user_id is None or not flat:
        return []

    touched: list[str] = []
    pattern_ids: list[int] = []
    if trade.scan_pattern_id:
        pattern_ids.append(int(trade.scan_pattern_id))
    for (pid,) in (
        db.query(ScanPattern.id)
        .filter(ScanPattern.active.is_(True))
        .order_by(ScanPattern.confidence.desc())
        .limit(24)
        .all()
    ):
        if pid not in pattern_ids:
            pattern_ids.append(pid)

    for pid in pattern_ids[:32]:
        sp = db.get(ScanPattern, pid)
        if not sp or not sp.rules_json:
            continue
        rj = sp.rules_json
        if not isinstance(rj, dict):
            continue
        conds = rj.get("conditions") or []
        if len(conds) < 2:
            continue
        ok = 0
        total = 0
        for c in conds:
            if not isinstance(c, dict):
                continue
            total += 1
            try:
                if _eval_condition(c, flat):
                    ok += 1
            except Exception:
                continue
        if total == 0:
            continue
        ratio = ok / total
        if ratio < 0.65:
            continue

        ins = (
            db.query(TradingInsight)
            .filter(TradingInsight.scan_pattern_id == sp.id)
            .filter(TradingInsight.user_id == trade.user_id)
            .filter(TradingInsight.active.is_(True))
            .first()
        )
        if not ins:
            continue
        old_conf = ins.confidence
        ins.evidence_count = (ins.evidence_count or 0) + 1
        ins.win_count = (ins.win_count or 0) + (1 if trade_won else 0)
        ins.loss_count = (ins.loss_count or 0) + (0 if trade_won else 1)
        delta = 0.04 if trade_won else -0.02
        ins.confidence = max(0.05, min(0.95, float(old_conf) + delta))
        ins.last_seen = datetime.utcnow()
        touched.append(sp.name or f"pattern_{sp.id}")
        log_learning_event(
            db,
            trade.user_id,
            "update",
            f"Post-close rule match ({ratio:.0%} conditions) reinforced: {sp.name}",
            confidence_before=old_conf,
            confidence_after=ins.confidence,
            related_insight_id=ins.id,
        )

    if touched:
        try:
            db.commit()
        except Exception:
            logger.debug("[learning] reinforce_patterns_at_close commit failed", exc_info=True)

    return touched


def _closed_trade_journal_template(
    trade: Trade,
    flat: dict[str, Any],
    reinforced: list[str],
) -> str:
    pnl_label = "PROFIT" if (trade.pnl or 0) > 0 else "LOSS"
    lines = [
        f"[Close] Trade #{trade.id} {trade.ticker} {trade.direction} — {pnl_label} "
        f"${trade.pnl} (entry {trade.entry_price} → exit {trade.exit_price}).",
    ]
    if trade.exit_reason:
        lines.append(f"Exit reason: {trade.exit_reason}")
    ind_bits = []
    for k in ("rsi_14", "macd_hist", "adx", "ema_20", "ema_50", "volume_ratio", "bb_pct_b"):
        if k in flat and flat[k] is not None:
            try:
                ind_bits.append(f"{k}={float(flat[k]):.4g}")
            except (TypeError, ValueError):
                ind_bits.append(f"{k}={flat[k]}")
    if ind_bits:
        lines.append("Indicators at exit: " + ", ".join(ind_bits[:8]))
    if reinforced:
        lines.append("Reinforced pattern insights (rule match): " + ", ".join(reinforced[:6]))
    return "\n".join(lines)


def analyze_closed_trade(db: Session, trade: Trade) -> str | None:
    """Post-close: structured rule reinforcement + template journal; optional LLM on large moves."""
    from ...prompts import load_prompt
    from ... import openai_client
    from ...logger import log_info, new_trace_id
    from .journal import add_journal_entry

    trace_id = new_trace_id()
    trade_won = trade.pnl is not None and trade.pnl > 0
    pnl_label = "PROFIT" if trade_won else "LOSS"

    flat = _trade_exit_snapshot_flat(trade)
    reinforced: list[str] = []
    try:
        reinforced = _reinforce_patterns_at_trade_close(db, trade, flat, trade_won=trade_won)
    except Exception as e:
        logger.warning("[learning] Structured post-close reinforcement failed: %s", e)

    journal_base = _closed_trade_journal_template(trade, flat, reinforced)
    llm_reply = ""

    move_pct = _closed_trade_price_move_pct(trade)
    existing_insights = get_insights(db, trade.user_id, limit=10)

    if move_pct >= CLOSED_TRADE_LLM_ENRICH_MIN_MOVE_PCT:
        snap_data = ""
        if trade.indicator_snapshot:
            try:
                raw = trade.indicator_snapshot
                if isinstance(raw, str):
                    raw = json.loads(raw)
                snap_data = json.dumps(raw, indent=2, default=str)
            except Exception:
                snap_data = str(trade.indicator_snapshot)

        trade_summary = (
            f"Ticker: {trade.ticker}\n"
            f"Direction: {trade.direction}\n"
            f"Entry: ${trade.entry_price} on {trade.entry_date}\n"
            f"Exit: ${trade.exit_price} on {trade.exit_date}\n"
            f"P&L: ${trade.pnl} ({pnl_label}, ~{move_pct:.1f}% price move)\n"
            f"Indicator snapshot at exit:\n{snap_data}"
        )
        insight_text = ""
        if existing_insights:
            insight_text = "\n".join(
                f"- [{ins.confidence:.0%}] {ins.pattern_description}"
                for ins in existing_insights
            )
        user_msg = (
            f"A trade was just closed (large move). Analyze and extract patterns.\n\n"
            f"## Trade Details\n{trade_summary}\n\n"
            f"## Existing Learned Patterns\n{insight_text or 'None yet.'}\n\n"
            f"Instructions:\n"
            f"1. Briefly explain the outcome vs indicator state.\n"
            f"2. Extract 1-3 reusable patterns as JSON array:\n"
            f'   [{{"pattern": "description", "confidence": 0.0-1.0}}]\n'
            f"3. Put the JSON array on a line starting with PATTERNS:"
        )
        try:
            system_prompt = load_prompt("trading_analyst")
            result = openai_client.chat(
                messages=[{"role": "user", "content": user_msg}],
                system_prompt=system_prompt,
                trace_id=trace_id,
                user_message=user_msg,
                max_tokens=2048,
            )
            llm_reply = (result.get("reply") or "").strip()
        except Exception as e:
            log_info(trace_id, f"[trading] post-trade LLM enrichment error: {e}")

        if llm_reply:
            _extract_and_store_patterns(
                db,
                trade.user_id,
                llm_reply,
                existing_insights,
                trade_won=trade_won,
            )

    full_journal = journal_base
    if llm_reply:
        full_journal += "\n\n[AI enrichment]\n" + llm_reply[:1200]

    if trade.user_id is not None:
        add_journal_entry(
            db,
            trade.user_id,
            content=full_journal[:8000],
            trade_id=trade.id,
        )

    return llm_reply or journal_base


def _extract_and_store_patterns(
    db: Session, user_id: int | None,
    ai_reply: str, existing_insights: list[TradingInsight],
    trade_won: bool = False,
) -> None:
    """Parse PATTERNS: JSON from the AI reply and upsert insights."""
    import re

    match = re.search(r"PATTERNS:\s*(\[.*?\])", ai_reply, re.DOTALL)
    if not match:
        return

    try:
        patterns = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return

    if not isinstance(patterns, list):
        return

    existing_map = {
        ins.pattern_description.lower().strip(): ins
        for ins in existing_insights
    }

    for p in patterns:
        if not isinstance(p, dict):
            continue
        desc = str(p.get("pattern", "")).strip()
        conf = float(p.get("confidence", 0.5))
        if not desc or len(desc) < 10:
            continue

        matched_existing = None
        desc_lower = desc.lower()
        for key, ins in existing_map.items():
            if key in desc_lower or desc_lower in key:
                matched_existing = ins
                break

        if matched_existing:
            old_conf = matched_existing.confidence
            matched_existing.evidence_count += 1
            matched_existing.win_count = (matched_existing.win_count or 0) + (1 if trade_won else 0)
            matched_existing.loss_count = (matched_existing.loss_count or 0) + (0 if trade_won else 1)
            matched_existing.confidence = min(0.95, old_conf + 0.05)
            matched_existing.last_seen = datetime.utcnow()
            db.commit()
            log_learning_event(
                db, user_id, "update",
                f"Pattern reinforced: {matched_existing.pattern_description[:100]} "
                f"({old_conf:.0%} -> {matched_existing.confidence:.0%}, {matched_existing.evidence_count} evidence)",
                confidence_before=old_conf,
                confidence_after=matched_existing.confidence,
                related_insight_id=matched_existing.id,
            )
        else:
            save_insight(db, user_id, desc, confidence=max(0.1, min(0.9, conf)),
                         wins=1 if trade_won else 0, losses=0 if trade_won else 1)


# ── Market Snapshots ──────────────────────────────────────────────────

def _fetch_news_sentiment(ticker: str) -> tuple[float | None, int | None]:
    """Fetch news for a ticker and return (avg_sentiment, news_count)."""
    try:
        from .sentiment import aggregate_sentiment
        from ..yf_session import get_ticker_news
        news = get_ticker_news(ticker, limit=10)
        titles = [n.get("title", "") for n in news if n.get("title")]
        if not titles:
            return None, 0
        agg = aggregate_sentiment(titles)
        return agg["avg_score"], agg["count"]
    except Exception:
        return None, None


def _fetch_fundamentals(ticker: str) -> tuple[float | None, float | None]:
    """Return (pe_ratio, market_cap_billions) for a ticker."""
    try:
        quote = fetch_quote(ticker)
        if not quote:
            return None, None
        pe = quote.get("pe") or quote.get("trailingPE")
        mcap = quote.get("marketCap") or quote.get("market_cap")
        pe_f = float(pe) if pe else None
        mcap_b = float(mcap) / 1e9 if mcap else None
        return pe_f, mcap_b
    except Exception:
        return None, None


_event_calendar_cache: tuple[float, set[str]] = (0.0, set())


def _fetch_event_context(ticker: str) -> dict[str, Any]:
    """Event-calendar and lightweight alt-data flags for snapshot provenance."""
    global _event_calendar_cache
    now = time.time()
    cached_at, symbols = _event_calendar_cache
    if now - cached_at > 1800:
        try:
            from .prescreener import _massive_earnings

            symbols = {str(s).strip().upper() for s in _massive_earnings()}
            _event_calendar_cache = (now, symbols)
        except Exception:
            symbols = set()
    sym = (ticker or "").strip().upper()
    return {
        "earnings_upcoming": sym in symbols,
        "event_source": "massive_earnings",
    }


def take_market_snapshot(db: Session, ticker: str, bar_interval: str = "1d") -> None:
    """Upsert one snapshot row for the latest *closed* bar in ``bar_interval`` (UTC bar open key)."""
    try:
        snap = get_indicator_snapshot(ticker, bar_interval)
        period = "3mo" if bar_interval == "1d" else "60d"
        df = fetch_ohlcv_df(ticker, period=period, interval=bar_interval)
        if df is None or df.empty:
            return
        price = float(df.iloc[-1]["Close"])
        bar_start = normalize_bar_start_utc(df.index[-1])
        snap["data_provenance"] = {
            "provider": df.attrs.get("provider"),
            "fetched_at_utc": df.attrs.get("fetched_at_utc"),
            "integrity_ok": df.attrs.get("integrity_ok"),
            "bar_interval": bar_interval,
        }
        snap["event_context"] = _fetch_event_context(ticker)
        ind_data = {k: v for k, v in snap.items() if k not in ("ticker", "interval")}
        pred_score = compute_prediction(ind_data) if ind_data else None
        vix = get_vix()
        sent_score, sent_count = _fetch_news_sentiment(ticker)
        pe_ratio, mcap_b = _fetch_fundamentals(ticker)
        upsert_market_snapshot(
            db,
            ticker=ticker,
            bar_interval=bar_interval,
            bar_start_at=bar_start,
            close_price=price,
            indicator_data=json.dumps(snap),
            predicted_score=pred_score,
            vix_at_snapshot=vix,
            news_sentiment=sent_score,
            news_count=sent_count,
            pe_ratio=pe_ratio,
            market_cap_b=mcap_b,
        )
        db.commit()
    except Exception:
        logger.debug("[learning] take_market_snapshot: non-critical operation failed", exc_info=True)


def _snapshot_data(ticker: str, bar_interval: str = "1d") -> tuple[
    str,
    dict | None,
    dict | None,
    float | None,
    int | None,
    float | None,
    float | None,
    str,
    datetime | None,
]:
    """Fetch snapshot data in a thread (no DB access).

    Returns bar_interval and bar_start_at (UTC-naive) for the OHLC row used as close price.
    """
    try:
        period = "3mo" if bar_interval == "1d" else "60d"
        df = fetch_ohlcv_df(ticker, period=period, interval=bar_interval)
        try:
            snapshot_batch_stats().add_ohlcv(1)
        except Exception:
            pass
        if df is None or df.empty:
            return ticker, None, None, None, None, None, None, bar_interval, None
        snap = get_indicator_snapshot(ticker, bar_interval, ohlcv_df=df)
        price = float(df.iloc[-1]["Close"])
        bar_start = normalize_bar_start_utc(df.index[-1])
        snap["data_provenance"] = {
            "provider": df.attrs.get("provider"),
            "fetched_at_utc": df.attrs.get("fetched_at_utc"),
            "integrity_ok": df.attrs.get("integrity_ok"),
            "bar_interval": bar_interval,
        }
        snap["event_context"] = _fetch_event_context(ticker)
        quote = {"price": price}
        sent_score, sent_count = _fetch_news_sentiment(ticker)
        pe, mcap = _fetch_fundamentals(ticker)
        return ticker, snap, quote, sent_score, sent_count, pe, mcap, bar_interval, bar_start
    except Exception:
        return ticker, None, None, None, None, None, None, bar_interval, None


def take_snapshots_parallel(
    db: Session,
    tickers: list[str],
    max_workers: int | None = None,
    *,
    bar_interval: str = "1d",
) -> int:
    """Take snapshots for many tickers using a thread pool.

    Data fetching runs in parallel; DB writes happen sequentially on the
    calling thread to avoid SQLAlchemy session issues.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ...config import settings as _settings

    resolved_workers = (
        max_workers
        if max_workers is not None
        else io_workers_for_snapshot_batch(_settings)
    )
    resolved_workers = max(1, int(resolved_workers))
    try:
        snapshot_batch_stats().reset()
        snapshot_batch_stats().set_snapshot_threads(resolved_workers)
    except Exception:
        pass

    # Pre-warm the OHLCV cache so indicator computation hits cache.
    # When Massive or Polygon is active the per-ticker cache inside the
    # respective client handles this automatically.
    if not (_use_massive() or _use_polygon()):
        from ..yf_session import batch_download
        BATCH = 100
        period = "3mo" if bar_interval == "1d" else "60d"
        for i in range(0, len(tickers), BATCH):
            try:
                batch_download(tickers[i:i + BATCH], period=period, interval=bar_interval)
            except Exception:
                logger.debug("[learning] take_snapshots_parallel: non-critical operation failed", exc_info=True)

    _t0 = time.time()
    fetched: list[tuple] = []
    total = len(tickers)
    with ThreadPoolExecutor(max_workers=resolved_workers) as executor:
        futures = {executor.submit(_snapshot_data, t, bar_interval): t for t in tickers}
        for future in as_completed(futures):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                fetched.append(future.result())
            except Exception:
                logger.debug("[learning] take_snapshots_parallel: non-critical operation failed", exc_info=True)
            # Progress logging every 100 tickers
            done = len(fetched)
            if done % 100 == 0 or done == total:
                elapsed = round(time.time() - _t0, 1)
                logger.info(f"[learning] Snapshot progress: {done}/{total} ({elapsed}s)")
                # graph-node: c_state/snapshots_daily (progress text; metadata in learning_cycle_architecture)
                apply_learning_cycle_step_status_progress(
                    _learning_status, "c_state", "snapshots_daily", done, total,
                )

    _fetch_elapsed = round(time.time() - _t0, 1)
    _t_db0 = time.time()
    logger.info(
        f"[learning] Snapshot data fetch: {len(fetched)}/{len(tickers)} tickers "
        f"in {_fetch_elapsed}s ({resolved_workers} workers) interval={bar_interval}"
    )
    vix = get_vix()
    count = 0
    for row in fetched:
        ticker, snap, quote = row[0], row[1], row[2]
        sent_score = row[3] if len(row) > 3 else None
        sent_count = row[4] if len(row) > 4 else None
        pe = row[5] if len(row) > 5 else None
        mcap = row[6] if len(row) > 6 else None
        biv = row[7] if len(row) > 7 else bar_interval
        bst = row[8] if len(row) > 8 else None
        if snap is None or bst is None:
            continue
        try:
            price = quote.get("price", 0) if quote else 0
            ind_data = {k: v for k, v in snap.items() if k not in ("ticker", "interval")}
            pred_score = compute_prediction(ind_data) if ind_data else None
            upsert_market_snapshot(
                db,
                ticker=ticker,
                bar_interval=biv,
                bar_start_at=bst,
                close_price=price,
                indicator_data=json.dumps(snap),
                predicted_score=pred_score,
                vix_at_snapshot=vix,
                news_sentiment=sent_score,
                news_count=sent_count,
                pe_ratio=pe,
                market_cap_b=mcap,
            )
            count += 1
        except Exception:
            logger.debug("[learning] take_snapshots_parallel: non-critical operation failed", exc_info=True)
    _db_elapsed = round(time.time() - _t_db0, 1)
    try:
        _st = snapshot_batch_stats().snapshot()
    except Exception:
        _st = {}
    logger.info(
        "[chili_brain_io] snapshot_batch tickers=%s written=%s fetch_s=%s db_write_s=%s "
        "workers=%s ohlcv_fetches_accounted=%s interval=%s",
        len(tickers),
        count,
        _fetch_elapsed,
        _db_elapsed,
        resolved_workers,
        _st.get("ohlcv_fetches", -1),
        bar_interval,
    )
    if count:
        db.commit()
    return count


def _take_intraday_crypto_snapshots(db: Session, top_tickers: list[str]) -> int:
    """Intraday snapshot passes for crypto in ``top_tickers``; returns rows written."""
    from ...config import settings as _s

    if not _s.brain_intraday_snapshots_enabled:
        return 0
    total = 0
    cap = max(1, int(_s.brain_intraday_max_tickers))
    crypto_sn = [t for t in top_tickers if t.endswith("-USD")][:cap]
    for raw_iv in _s.brain_intraday_intervals.split(","):
        iv = raw_iv.strip()
        if iv and iv != "1d" and crypto_sn:
            total += take_snapshots_parallel(db, crypto_sn, bar_interval=iv)
    return total


def take_all_snapshots(db: Session, user_id: int | None, ticker_list: list[str] | None = None) -> int:
    if ticker_list:
        tickers = list(set(ticker_list))
    else:
        tickers = list(set(DEFAULT_SCAN_TICKERS[:20] + DEFAULT_CRYPTO_TICKERS[:10]))

    watchlist = get_watchlist(db, user_id)
    for w in watchlist:
        if w.ticker not in tickers:
            tickers.append(w.ticker)

    return take_snapshots_parallel(db, tickers)


def backfill_future_returns(db: Session) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Oldest first: recent snapshots often lack enough *forward* daily bars yet;
    # unordered LIMIT was skewing attempts toward rows that always fail.
    unfilled = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.future_return_5d.is_(None))
        .order_by(MarketSnapshot.snapshot_date.asc())
        .limit(3000)
        .all()
    )

    if not unfilled:
        return 0

    tickers = list({s.ticker for s in unfilled})
    if not (_use_massive() or _use_polygon()):
        from ..yf_session import batch_download as _bd
        BATCH = 100
        for i in range(0, len(tickers), BATCH):
            try:
                _bd(tickers[i:i + BATCH], period="1mo", interval="1d")
            except Exception:
                logger.debug("[learning] backfill_future_returns: non-critical operation failed", exc_info=True)

    _bf_stats: dict[str, int] = {
        "skip_recent": 0,
        "bad_snap": 0,
        "empty": 0,
        "bad_px": 0,
        "no_anchor": 0,
        "no_forward": 0,
        "exc": 0,
        "ok": 0,
    }
    _bf_lock = threading.Lock()

    def _snap_utc_date(snap) -> date | None:
        raw = getattr(snap, "bar_start_at", None) or snap.snapshot_date
        if raw is None:
            return None
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC").date()

    def _fetch_returns(snap):
        try:
            snap_d = _snap_utc_date(snap)
            if snap_d is None:
                with _bf_lock:
                    _bf_stats["bad_snap"] += 1
                return None
            today_utc = datetime.now(timezone.utc).date()
            # Same UTC calendar day: daily provider data usually has no "next" close yet.
            if snap_d >= today_utc:
                with _bf_lock:
                    _bf_stats["skip_recent"] += 1
                return None

            start_cal = snap_d - timedelta(days=14)
            iv = (getattr(snap, "bar_interval", None) or "1d").strip() or "1d"
            per = "60d" if iv == "1d" else "30d"
            df = fetch_ohlcv_df(
                snap.ticker,
                interval=iv,
                period=per,
                start=start_cal.isoformat(),
            )
            if df.empty or len(df) < 2:
                with _bf_lock:
                    _bf_stats["empty"] += 1
                return None
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            base_price = float(snap.close_price)
            if base_price <= 0:
                with _bf_lock:
                    _bf_stats["bad_px"] += 1
                return None

            pos = None
            for i in range(len(df) - 1, -1, -1):
                bar_ts = pd.Timestamp(df.index[i])
                if bar_ts.tzinfo is None:
                    bar_ts = bar_ts.tz_localize("UTC")
                bar_d = bar_ts.tz_convert("UTC").date()
                if bar_d <= snap_d:
                    pos = i
                    break
            if pos is None:
                with _bf_lock:
                    _bf_stats["no_anchor"] += 1
                return None
            if pos + 1 >= len(df):
                with _bf_lock:
                    _bf_stats["no_forward"] += 1
                return None

            def _ret_forward(off: int):
                j = pos + off
                if j >= len(df):
                    return None
                return round(
                    (float(df["Close"].iloc[j]) - base_price) / base_price * 100, 2
                )

            r1 = _ret_forward(1)
            r3 = _ret_forward(3)
            r5 = _ret_forward(5)
            r10 = _ret_forward(10)
            with _bf_lock:
                _bf_stats["ok"] += 1
            return (snap.id, r1, r3, r5, r10)
        except Exception:
            with _bf_lock:
                _bf_stats["exc"] += 1
            return None

    from ...config import settings as _bf_settings

    _workers = (
        io_workers_high(_bf_settings)
        if (_use_massive() or _use_polygon())
        else io_workers_med(_bf_settings)
    )
    _t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=_workers) as executor:
        futures = {executor.submit(_fetch_returns, s): s for s in unfilled}
        for f in as_completed(futures):
            if _shutting_down.is_set():
                break
            r = f.result()
            if r:
                results.append(r)

    st = _bf_stats
    logger.info(
        f"[learning] Backfill returns fetch: {len(results)}/{len(unfilled)} snapshots "
        f"in {time.time() - _t0:.1f}s ({_workers} workers, {len(tickers)} tickers) "
        f"[skip_recent={st['skip_recent']} bad_snap={st['bad_snap']} empty={st['empty']} "
        f"bad_px={st['bad_px']} no_anchor={st['no_anchor']} no_forward={st['no_forward']} "
        f"exc={st['exc']} ok_rows={st['ok']}]"
    )
    updated = 0
    snap_map = {s.id: s for s in unfilled}
    for snap_id, r1, r3, r5, r10 in results:
        snap = snap_map.get(snap_id)
        if not snap:
            continue
        if r1 is not None:
            snap.future_return_1d = r1
        if r3 is not None:
            snap.future_return_3d = r3
        if r5 is not None:
            snap.future_return_5d = r5
        if r10 is not None:
            snap.future_return_10d = r10
        updated += 1

    if updated:
        db.commit()
    return updated


# ── Pattern Mining ────────────────────────────────────────────────────

_spy_regime_cache: dict[str, Any] = {"data": {}, "ts": 0.0}
_SPY_REGIME_CACHE_TTL = 600


def _get_historical_regime_map() -> dict[str, dict]:
    """Build a date->regime map from SPY daily data (cached).

    Returns {date_str: {"spy_chg": ..., "spy_mom_5d": ..., "regime": ...}}
    """
    import time as _t

    now = _t.time()
    if _spy_regime_cache["data"] and now - _spy_regime_cache["ts"] < _SPY_REGIME_CACHE_TTL:
        return _spy_regime_cache["data"]

    try:
        spy_df = fetch_ohlcv_df("SPY", period="6mo", interval="1d")
        if spy_df.empty or len(spy_df) < 10:
            return {}
    except Exception:
        return {}

    spy_close = spy_df["Close"]
    regime_map: dict[str, dict] = {}
    for i in range(5, len(spy_df)):
        dt_str = str(spy_df.index[i].date()) if hasattr(spy_df.index[i], "date") else str(spy_df.index[i])[:10]
        chg = (float(spy_close.iloc[i]) - float(spy_close.iloc[i - 1])) / float(spy_close.iloc[i - 1]) * 100
        mom_5d = (float(spy_close.iloc[i]) - float(spy_close.iloc[i - 5])) / float(spy_close.iloc[i - 5]) * 100

        if chg > 0.3 and mom_5d > 0:
            regime = "risk_on"
        elif chg < -0.3 or mom_5d < -2:
            regime = "risk_off"
        else:
            regime = "cautious"

        regime_map[dt_str] = {
            "spy_chg": round(chg, 2),
            "spy_mom_5d": round(mom_5d, 2),
            "regime": regime,
        }

    _spy_regime_cache["data"] = regime_map
    _spy_regime_cache["ts"] = now
    return regime_map


# OHLCV fetch period per bar interval (must be in market_data._VALID_PERIODS / clampable).
_MINE_FETCH_PERIOD: dict[str, str] = {
    "1m": "5d",
    "5m": "1mo",
    "15m": "1mo",
    "30m": "1mo",
    "1h": "6mo",
    "60m": "6mo",
    "90m": "6mo",
    "1d": "6mo",
}

_MINE_MIN_LEN: dict[str, int] = {
    "1m": 200,
    "5m": 120,
    "15m": 120,
    "30m": 120,
    "1h": 120,
    "60m": 120,
    "90m": 120,
    "1d": 60,
}


def _mine_forward_horizon(bar_interval: str) -> tuple[int, int]:
    """(primary_forward_bars, secondary_forward_bars) for ret_5d / ret_10d style keys."""
    b = (bar_interval or "1d").strip().lower()
    if b == "1m":
        return 60, 120
    if b == "5m":
        return 36, 72
    if b in ("15m", "30m"):
        return 20, 40
    if b in ("1h", "60m", "90m"):
        return 10, 20
    return 5, 10


def _mine_min_avg_ret_pct(bar_interval: str) -> float:
    """Minimum mean forward return (%) for mine_patterns _check gate."""
    b = (bar_interval or "1d").strip().lower()
    if b == "1m":
        return 0.10
    if b == "5m":
        return 0.15
    if b in ("15m", "30m"):
        return 0.20
    if b in ("1h", "60m", "90m"):
        return 0.25
    return 0.30


def _mine_from_history(ticker: str, bar_interval: str = "1d") -> list[dict]:
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange
    from ta.volume import OnBalanceVolumeIndicator
    from .scanner import _detect_resistance_retests, _detect_narrow_range, _detect_vcp

    from .data_quality import clean_ohlcv

    biv = (bar_interval or "1d").strip().lower()
    period = _MINE_FETCH_PERIOD.get(biv, "6mo")
    min_len = _MINE_MIN_LEN.get(biv, 120)
    fwd_primary, fwd_secondary = _mine_forward_horizon(biv)
    tail_need = max(fwd_primary, fwd_secondary)
    try:
        df = fetch_ohlcv_df(ticker, period=period, interval=bar_interval)
        if df.empty or len(df) < min_len:
            return []
        df = clean_ohlcv(df)
        if len(df) < min_len:
            return []
    except Exception:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd_obj = MACD(close=close)
    macd_line = macd_obj.macd()
    macd_signal = macd_obj.macd_signal()
    macd_hist = macd_obj.macd_diff()
    sma20 = SMAIndicator(close=close, window=20).sma_indicator()
    ema20 = EMAIndicator(close=close, window=20).ema_indicator()
    ema50 = EMAIndicator(close=close, window=50).ema_indicator()
    ema100 = EMAIndicator(close=close, window=100).ema_indicator()
    bb = BollingerBands(close=close, window=20, window_dev=2)
    bb_upper = bb.bollinger_hband()
    bb_lower = bb.bollinger_lband()
    bb_width = bb.bollinger_wband()
    adx = ADXIndicator(high=high, low=low, close=close).adx()
    atr = AverageTrueRange(high=high, low=low, close=close).average_true_range()
    stoch = StochasticOscillator(high=high, low=low, close=close)
    stoch_k = stoch.stoch()
    vol_sma = volume.rolling(20).mean()
    vol_roll_std = volume.rolling(20).std()
    obv_series = OnBalanceVolumeIndicator(close=close, volume=volume).on_balance_volume()
    log_ret = np.log(close.astype(float) / close.astype(float).shift(1))

    regime_map = _get_historical_regime_map()

    rows = []
    for i in range(50, len(df) - tail_need):
        price = float(close.iloc[i])
        if price <= 0:
            continue
        ret_5d = (float(close.iloc[i + fwd_primary]) - price) / price * 100
        ret_10d = (
            (float(close.iloc[i + fwd_secondary]) - price) / price * 100
            if i + fwd_secondary < len(df) else None
        )

        bb_range = float(bb_upper.iloc[i]) - float(bb_lower.iloc[i]) if pd.notna(bb_upper.iloc[i]) and pd.notna(bb_lower.iloc[i]) else 0
        bb_pct = (price - float(bb_lower.iloc[i])) / bb_range if bb_range > 0 else 0.5

        e20 = float(ema20.iloc[i]) if pd.notna(ema20.iloc[i]) else None
        e50 = float(ema50.iloc[i]) if pd.notna(ema50.iloc[i]) else None
        e100 = float(ema100.iloc[i]) if pd.notna(ema100.iloc[i]) else None

        vol_ratio = (float(volume.iloc[i]) / float(vol_sma.iloc[i])
                     if pd.notna(vol_sma.iloc[i]) and float(vol_sma.iloc[i]) > 0 else 1.0)
        vm_i, vs_i = vol_sma.iloc[i], vol_roll_std.iloc[i]
        vol_z_20 = None
        if pd.notna(vm_i) and pd.notna(vs_i) and float(vs_i) > 0:
            vol_z_20 = round((float(volume.iloc[i]) - float(vm_i)) / float(vs_i), 4)
        rv_slice = log_ret.iloc[max(0, i - 19):i + 1].dropna()
        realized_vol_20 = None
        if len(rv_slice) >= 10:
            realized_vol_20 = round(float(rv_slice.std() * np.sqrt(252)), 6)

        prev_close = float(close.iloc[i - 1]) if i > 0 else price
        gap_pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0

        # Stochastic divergence detection (last 5 bars)
        stoch_bull_div = False
        stoch_bear_div = False
        if i >= 5:
            prices_5 = [float(close.iloc[j]) for j in range(i - 4, i + 1)]
            stochs_5 = [float(stoch_k.iloc[j]) if pd.notna(stoch_k.iloc[j]) else 50 for j in range(i - 4, i + 1)]
            if prices_5[-1] < min(prices_5[:-1]) and stochs_5[-1] > min(stochs_5[:-1]):
                stoch_bull_div = True
            if prices_5[-1] > max(prices_5[:-1]) and stochs_5[-1] < max(stochs_5[:-1]):
                stoch_bear_div = True

        # Breakout-specific enrichment
        lookback = min(20, i)
        h_slice = high.iloc[i - lookback:i + 1]
        c_slice = close.iloc[i - lookback:i + 1]
        l_slice = low.iloc[i - lookback:i + 1]
        v_slice = volume.iloc[i - lookback:i + 1]
        resistance = float(h_slice.max()) if len(h_slice) > 0 else price

        try:
            retest_info = _detect_resistance_retests(h_slice, c_slice, resistance, tolerance_pct=1.5, lookback=lookback)
            retest_count = retest_info.get("retest_count", 0)
        except Exception:
            retest_count = 0

        try:
            nr = _detect_narrow_range(h_slice, l_slice)
        except Exception:
            nr = None

        try:
            vcp = _detect_vcp(h_slice, l_slice, v_slice, lookback=lookback)
        except Exception:
            vcp = 0

        bw = float(bb_width.iloc[i]) if pd.notna(bb_width.iloc[i]) else 0.1
        bb_squeeze = bw < 0.04

        dt_str = str(df.index[i].date()) if hasattr(df.index[i], "date") else str(df.index[i])[:10]
        regime_info = regime_map.get(dt_str, {})

        bar_start_utc = normalize_bar_start_utc(df.index[i])
        rows.append({
            "ticker": ticker,
            "bar_interval": biv,
            "bar_start_utc": bar_start_utc,
            "price": price,
            "ret_5d": round(ret_5d, 2),
            "ret_10d": round(ret_10d, 2) if ret_10d is not None else None,
            "rsi": float(rsi.iloc[i]) if pd.notna(rsi.iloc[i]) else 50,
            "macd": float(macd_line.iloc[i]) if pd.notna(macd_line.iloc[i]) else 0,
            "macd_sig": float(macd_signal.iloc[i]) if pd.notna(macd_signal.iloc[i]) else 0,
            "macd_hist": float(macd_hist.iloc[i]) if pd.notna(macd_hist.iloc[i]) else 0,
            "adx": float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0,
            "bb_pct": bb_pct,
            "bb_squeeze": bb_squeeze,
            "atr": float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0,
            "stoch_k": float(stoch_k.iloc[i]) if pd.notna(stoch_k.iloc[i]) else 50,
            "stoch_bull_div": stoch_bull_div,
            "stoch_bear_div": stoch_bear_div,
            "above_sma20": price > float(sma20.iloc[i]) if pd.notna(sma20.iloc[i]) else False,
            "sma_20": float(sma20.iloc[i]) if pd.notna(sma20.iloc[i]) else None,
            "obv": float(obv_series.iloc[i]) if pd.notna(obv_series.iloc[i]) else None,
            "vol_z_20": vol_z_20,
            "realized_vol_20": realized_vol_20,
            "ema_20": e20,
            "ema_50": e50,
            "ema_100": e100,
            "ema_stack": (e20 is not None and e50 is not None and e100 is not None
                          and price > e20 > e50 > e100),
            "resistance": round(resistance, 4),
            "resistance_retests": retest_count,
            "narrow_range": nr or "",
            "vcp_count": vcp,
            "is_crypto": ticker.endswith("-USD"),
            "vol_ratio": round(vol_ratio, 2),
            "gap_pct": round(gap_pct, 2),
            "regime": regime_info.get("regime", "unknown"),
            "spy_mom_5d": regime_info.get("spy_mom_5d", 0),
        })
    return rows


def mine_row_to_indicator_payload(row: dict) -> dict:
    """Indicator dict shape compatible with :func:`compute_prediction` (backfill / scripts)."""
    sma_v = row.get("sma_20")
    obv_v = row.get("obv")
    return {
        "rsi": {"value": row.get("rsi")},
        "macd": {
            "macd": row.get("macd"),
            "signal": row.get("macd_sig"),
            "histogram": row.get("macd_hist"),
        },
        "stoch": {"k": row.get("stoch_k")},
        "adx": {"adx": row.get("adx")},
        "atr": {"value": row.get("atr")},
        "ema_20": {"value": row.get("ema_20")},
        "ema_50": {"value": row.get("ema_50")},
        "ema_100": {"value": row.get("ema_100")},
        "sma_20": {"value": sma_v},
        "bbands": {},
        "obv": {"value": obv_v} if obv_v is not None else {},
        "volume_z_20": {"value": row.get("vol_z_20")},
        "realized_vol_20": {"value": row.get("realized_vol_20")},
    }


def ensure_mined_scan_pattern(
    db, name: str, conditions: list[dict], *,
    confidence: float, win_rate: float, avg_return_pct: float,
    evidence_count: int, asset_class: str = "all",
    timeframe: str = "1d",
) -> int | None:
    """Create or update a ScanPattern with structured rules_json from mining.

    Shared by all miners (daily, intraday, secondary) so every discovery
    produces a machine-actionable ScanPattern entry.
    """
    from ...models.trading import ScanPattern

    tf = (timeframe or "1d").strip() or "1d"
    rules = json.dumps({"conditions": conditions})
    existing = db.query(ScanPattern).filter(
        ScanPattern.name == name,
        ScanPattern.origin == "mined",
        ScanPattern.timeframe == tf,
        ScanPattern.active.is_(True),
    ).first()
    if existing:
        existing.confidence = confidence
        existing.win_rate = win_rate
        existing.avg_return_pct = avg_return_pct
        existing.evidence_count = evidence_count
        existing.rules_json = rules
        existing.timeframe = tf
        db.flush()
        return existing.id

    sp = ScanPattern(
        name=name,
        description=f"Mined: {name}",
        rules_json=rules,
        origin="mined",
        asset_class=asset_class,
        timeframe=tf,
        confidence=confidence,
        win_rate=win_rate,
        avg_return_pct=avg_return_pct,
        evidence_count=evidence_count,
        lifecycle_stage="candidate",
    )
    db.add(sp)
    db.flush()
    try:
        from .brain_work.emitters import emit_backtest_requested_for_pattern

        emit_backtest_requested_for_pattern(db, sp.id, source="mining_new_pattern")
    except Exception:
        logger.debug("[learning] brain_work emit backtest_requested failed", exc_info=True)
    return sp.id


def _matches_filter(row: dict, conditions: list[dict] | None) -> bool:
    """Check if a mining row matches the structured conditions for holdout validation."""
    if not conditions:
        return True
    from .pattern_engine import _eval_condition
    snap = dict(row)
    snap.setdefault("price", row.get("price", 0))
    snap.setdefault("rsi_14", row.get("rsi"))
    snap.setdefault("adx", row.get("adx"))
    snap.setdefault("macd_histogram", row.get("macd_hist"))
    snap.setdefault("macd_hist", row.get("macd_hist"))
    snap.setdefault("macd", row.get("macd"))
    snap.setdefault("macd_signal", row.get("macd_sig"))
    snap.setdefault("bb_pct", row.get("bb_pct"))
    snap.setdefault("bb_squeeze", row.get("bb_squeeze"))
    snap.setdefault("stochastic_k", row.get("stoch_k"))
    snap.setdefault("stoch_k", row.get("stoch_k"))
    snap.setdefault("volume_ratio", row.get("vol_ratio"))
    snap.setdefault("rel_vol", row.get("vol_ratio"))
    snap.setdefault("ema_stack", row.get("ema_stack"))
    snap.setdefault("stoch_bull_div", row.get("stoch_bull_div"))
    snap.setdefault("stoch_bear_div", row.get("stoch_bear_div"))
    snap.setdefault("news_sentiment", row.get("news_sentiment"))
    snap.setdefault("atr", row.get("atr"))
    snap.setdefault("gap_pct", row.get("gap_pct"))
    for cond in conditions:
        if not _eval_condition(cond, snap):
            return False
    return True


def mine_patterns(
    db: Session,
    user_id: int | None,
    *,
    ticker_universe: list[str] | None = None,
) -> list[str]:
    """Discover patterns from historical price data + existing snapshots."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ...config import settings
    from .market_data import ALL_SCAN_TICKERS as _ALL_TICKERS

    if ticker_universe is not None:
        mine_tickers = list(ticker_universe)
    else:
        mine_tickers = list(_ALL_TICKERS)

        watchlist = get_watchlist(db, user_id)
        for w in watchlist:
            if w.ticker not in mine_tickers:
                mine_tickers.append(w.ticker)

        try:
            from .prescreener import get_trending_crypto
            for t in get_trending_crypto():
                if t not in mine_tickers:
                    mine_tickers.append(t)
        except Exception:
            logger.debug("[learning] mine_patterns: non-critical operation failed", exc_info=True)

    max_mine = int(getattr(settings, "brain_mine_patterns_max_tickers", 1000))
    if max_mine > 0:
        mine_tickers = mine_tickers[:max_mine]

    interval_jobs: list[tuple[str, list[str]]] = [("1d", mine_tickers)]
    try:
        if settings.brain_intraday_snapshots_enabled:
            cap = max(1, int(settings.brain_intraday_max_tickers))
            crypto_only = [t for t in mine_tickers if t.endswith("-USD")][:cap]
            for raw_iv in settings.brain_intraday_intervals.split(","):
                iv = raw_iv.strip()
                if iv and iv != "1d":
                    interval_jobs.append((iv, crypto_only))
    except Exception:
        logger.debug("[learning] mine_patterns: non-critical operation failed", exc_info=True)

    _workers = (
        io_workers_high(settings)
        if (_use_massive() or _use_polygon())
        else io_workers_med(settings)
    )
    _t0 = time.time()
    all_rows: list[dict] = []
    for bar_iv, tick_chunk in interval_jobs:
        if _shutting_down.is_set():
            break
        with ThreadPoolExecutor(max_workers=_workers) as executor:
            futures = {
                executor.submit(_mine_from_history, t, bar_iv): t
                for t in tick_chunk
            }

            for future in as_completed(futures):
                if _shutting_down.is_set():
                    break
                try:
                    rows = future.result()
                    all_rows.extend(rows)
                except Exception:
                    continue

    logger.info(
        f"[learning] Pattern mining OHLCV fetch: {len(mine_tickers)} tickers / {len(interval_jobs)} intervals → "
        f"{len(all_rows)} data rows in {time.time() - _t0:.1f}s ({_workers} workers)"
    )

    snapshots = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(5000).all()

    for s in snapshots:
        try:
            data = json.loads(s.indicator_data) if s.indicator_data else {}
            rsi_data = data.get("rsi", {})
            macd_data = data.get("macd", {})
            bb_data = data.get("bbands", {})
            adx_data = data.get("adx", {})
            stoch_data = data.get("stoch", {})
            sma20_data = data.get("sma_20", {})
            ema20_data = data.get("ema_20", {})
            ema50_data = data.get("ema_50", {})
            ema100_data = data.get("ema_100", {})

            bb_range = ((bb_data.get("upper", 0) or 0) - (bb_data.get("lower", 0) or 0))
            bb_pct = ((s.close_price - (bb_data.get("lower", 0) or 0)) / bb_range
                      if bb_range > 0 and s.close_price else 0.5)
            bpb_direct = (data.get("bb_pct_b") or {}).get("value")
            if bpb_direct is not None:
                try:
                    bb_pct = float(bpb_direct)
                except (TypeError, ValueError):
                    pass

            dt_str = (
                str(s.snapshot_date.date())
                if getattr(s, "snapshot_date", None)
                else ""
            )
            eq = data.get("equity_regime")
            spy_regime = None
            if isinstance(eq, dict):
                spy_regime = eq.get("regime")
            if not spy_regime and dt_str:
                spy_regime = _get_historical_regime_map().get(dt_str, {}).get("regime", "unknown")
            else:
                spy_regime = spy_regime or "unknown"

            e20 = ema20_data.get("value") if ema20_data else None
            e50 = ema50_data.get("value") if ema50_data else None
            e100 = ema100_data.get("value") if ema100_data else None
            price = s.close_price or 0
            ema_stack = (e20 is not None and e50 is not None and e100 is not None
                         and price > e20 > e50 > e100)

            biv = (s.bar_interval or "").strip() or "legacy_ingest"
            if s.bar_start_at is not None:
                bst = normalize_bar_start_utc(s.bar_start_at)
            else:
                bst = datetime.combine(s.snapshot_date.date(), datetime.min.time())

            rsi7_data = data.get("rsi_7") or {}
            vz_data = data.get("volume_z_20") or {}
            rv_data = data.get("realized_vol_20") or {}

            all_rows.append({
                "ticker": s.ticker,
                "bar_interval": biv,
                "bar_start_utc": bst,
                "price": price,
                "ret_5d": s.future_return_5d or 0,
                "ret_10d": s.future_return_10d,
                "rsi": rsi_data.get("value", 50) or 50,
                "rsi_7": rsi7_data.get("value"),
                "macd": macd_data.get("macd", 0) or 0,
                "macd_sig": macd_data.get("signal", 0) or 0,
                "macd_hist": macd_data.get("histogram", 0) or 0,
                "adx": adx_data.get("adx", 0) or 0,
                "bb_pct": bb_pct,
                "atr": data.get("atr", {}).get("value", 0) or 0,
                "stoch_k": stoch_data.get("k", 50) or 50,
                "above_sma20": (price > (sma20_data.get("value", 0) or 0)
                                if sma20_data and price else False),
                "ema_stack": ema_stack,
                "regime": spy_regime,
                "vol_z_20": vz_data.get("value"),
                "realized_vol_20": rv_data.get("value"),
                "is_crypto": s.ticker.endswith("-USD"),
                "news_sentiment": getattr(s, "news_sentiment", None),
                "news_count": getattr(s, "news_count", None) or 0,
                "pe_ratio": getattr(s, "pe_ratio", None),
                "market_cap_b": getattr(s, "market_cap_b", None),
            })
        except Exception:
            continue

    all_rows = dedupe_sample_rows(all_rows)

    if len(all_rows) < 10:
        return []

    vol_regime = get_volatility_regime()
    regime_tag = f" [{vol_regime['label']}]" if vol_regime.get("regime") != "unknown" else ""

    discoveries: list[str] = []
    MIN_SAMPLES = max(20, int(getattr(settings, "brain_mining_min_samples", 20)))
    MIN_WIN_RATE = float(getattr(settings, "brain_mining_min_win_rate", 0.58))
    _cpcv_on = getattr(settings, "brain_mining_purged_cpcv_enabled", True)
    _emit_scan_patterns = bool(getattr(settings, "brain_mining_emit_scan_patterns", True))
    _use_v2_gates = bool(getattr(settings, "brain_mining_use_v2_promotion", True))

    _insight_cache = preload_active_insights(db, user_id)

    # Phase 1a: reset trial counter and split data into discovery/holdout
    from .mining_validation import (
        reset_trial_counter, increment_trial_counter, get_trial_count,
        check_promotion_ready, check_promotion_ready_v2,
        temporal_holdout_split,
    )
    from collections import defaultdict

    def _normalize_mine_bar_interval(iv: str | None) -> str:
        s = (str(iv) if iv is not None else "").strip().lower()
        if not s or s == "legacy_ingest":
            return "1d"
        return s

    _rows_by_tf: defaultdict[str, list] = defaultdict(list)
    for _row in all_rows:
        _rows_by_tf[_normalize_mine_bar_interval(_row.get("bar_interval"))].append(_row)

    _mine_tf_sort = {"1m": 0, "5m": 1, "15m": 2, "30m": 3, "1h": 4, "60m": 4, "90m": 4, "4h": 5, "1d": 6}

    for mine_tf in sorted(_rows_by_tf.keys(), key=lambda k: (_mine_tf_sort.get(k, 99), k)):
        tf_rows = _rows_by_tf[mine_tf]
        if len(tf_rows) < 10:
            continue
        reset_trial_counter()
        discovery_rows, holdout_rows = temporal_holdout_split(tf_rows, holdout_fraction=0.25)
        min_ret_pct = _mine_min_avg_ret_pct(mine_tf)
        _mine_src = discovery_rows if discovery_rows else tf_rows
        logger.info(
            "[mine_patterns] Mining TF=%s from %d data points (discovery=%d, holdout=%d)",
            mine_tf, len(tf_rows), len(discovery_rows), len(holdout_rows),
        )

        def _ensure_mined_scan_pattern(
            _db, name, conds, *, confidence, win_rate, avg_return_pct, evidence_count,
        ) -> int | None:
            return ensure_mined_scan_pattern(
                _db, name, conds,
                confidence=confidence, win_rate=win_rate,
                avg_return_pct=avg_return_pct, evidence_count=evidence_count,
                timeframe=mine_tf,
            )

        def _check(filtered, label, conditions=None, _minr=min_ret_pct, _mtf=mine_tf):
            increment_trial_counter()
            if len(filtered) < MIN_SAMPLES:
                return

            if _cpcv_on:
                from .mining_validation import mined_candidate_passes_purged_segments
                ok, _ = mined_candidate_passes_purged_segments(filtered)
                if not ok:
                    return

            # Build per-trade returns for DSR computation
            _returns_for_dsr = [float(r.get("ret_5d") or 0) for r in filtered]

            # Build holdout predicate matching this filter's conditions
            def _holdout_pred(row: dict) -> bool:
                return row in filtered  # placeholder — replaced below

            if _use_v2_gates and holdout_rows:
                # Re-apply the same filter logic on holdout rows
                _holdout_filtered = [r for r in holdout_rows if _matches_filter(r, conditions)]
                _ready, _detail = check_promotion_ready_v2(
                    filtered,
                    min_trades=MIN_SAMPLES,
                    returns_for_dsr=_returns_for_dsr,
                    holdout_rows=holdout_rows,
                    holdout_predicate=lambda r: _matches_filter(r, conditions),
                )
            else:
                _ready, _detail = check_promotion_ready(
                    filtered,
                    min_trades=MIN_SAMPLES,
                    n_hypotheses_tested=max(get_trial_count(), 1),
                )
            if not _ready:
                return
            avg_5d = sum(r["ret_5d"] for r in filtered) / len(filtered)
            avg_10d_vals = [r["ret_10d"] for r in filtered if r.get("ret_10d") is not None]
            avg_10d = (sum(avg_10d_vals) / len(avg_10d_vals)) if avg_10d_vals else None
            wins = sum(1 for r in filtered if r["ret_5d"] > 0)
            wr = wins / len(filtered)
            if math.isnan(avg_5d) or math.isnan(wr):
                return
            if avg_10d is not None and math.isnan(avg_10d):
                return
            if avg_5d > _minr and wr >= MIN_WIN_RATE:
                ret_str = f"{avg_5d:+.1f}%/5d"
                if avg_10d is not None:
                    ret_str += f", {avg_10d:+.1f}%/10d"
                _pat_name = label if _mtf == "1d" else f"{label} [{_mtf}]"
                pattern = f"{_pat_name} -> avg {ret_str} ({wr*100:.0f}% win, {len(filtered)} samples){regime_tag}"
                discoveries.append(pattern)

                sp_id = None
                if _emit_scan_patterns and conditions:
                    sp_id = _ensure_mined_scan_pattern(
                        db, _pat_name, conditions,
                        confidence=min(0.9, wr),
                        win_rate=wr,
                        avg_return_pct=avg_5d,
                        evidence_count=len(filtered),
                    )

                save_insight(db, user_id, pattern, confidence=min(0.9, wr),
                             wins=wins, losses=len(filtered) - wins,
                             scan_pattern_id=sp_id,
                             _existing_cache=_insight_cache)

        # ── RSI-based patterns (all upgraded to min 2 conditions) ──
        _check([r for r in _mine_src if r["rsi"] < 30 and r["macd_hist"] > 0],
               "RSI oversold (<30) + MACD histogram turning positive",
               conditions=[{"indicator": "rsi_14", "op": "<", "value": 30},
                           {"indicator": "macd_histogram", "op": ">", "value": 0}])
        _check([r for r in _mine_src if r["rsi"] > 70 and r["macd_hist"] < 0],
               "RSI overbought (>70) + MACD histogram negative — sell signal",
               conditions=[{"indicator": "rsi_14", "op": ">", "value": 70},
                           {"indicator": "macd_histogram", "op": "<", "value": 0}])
        _check([r for r in _mine_src if 30 <= r["rsi"] < 40 and r["macd_hist"] > 0],
               "RSI near-oversold (30-40) + MACD histogram positive",
               conditions=[{"indicator": "rsi_14", "op": ">=", "value": 30},
                           {"indicator": "rsi_14", "op": "<", "value": 40},
                           {"indicator": "macd_histogram", "op": ">", "value": 0}])
    
        # ── MACD patterns (upgraded) ──
        _check([r for r in _mine_src if r["macd"] > r["macd_sig"] and r["rsi"] < 60],
               "MACD bullish crossover + RSI not overbought",
               conditions=[{"indicator": "macd", "op": ">", "ref": "macd_signal"},
                           {"indicator": "rsi_14", "op": "<", "value": 60}])
        _check([r for r in _mine_src if r["macd_hist"] > 0 and r["macd"] < 0],
               "MACD histogram positive while MACD negative (early reversal)",
               conditions=[{"indicator": "macd_histogram", "op": ">", "value": 0},
                           {"indicator": "macd", "op": "<", "value": 0}])
    
        # ── Bollinger Band patterns (upgraded) ──
        _check([r for r in _mine_src if r["bb_pct"] < 0.1 and r["rsi"] < 35],
               "Price below lower Bollinger Band (<10%) + RSI oversold",
               conditions=[{"indicator": "bb_pct", "op": "<", "value": 0.1},
                           {"indicator": "rsi_14", "op": "<", "value": 35}])
        _check([r for r in _mine_src if r["bb_pct"] > 0.9 and r["rsi"] > 65],
               "Price above upper Bollinger Band (>90%) + RSI elevated — sell signal",
               conditions=[{"indicator": "bb_pct", "op": ">", "value": 0.9},
                           {"indicator": "rsi_14", "op": ">", "value": 65}])
    
        # ── ADX + RSI ──
        _check([r for r in _mine_src if r["adx"] > 30 and r["rsi"] < 40],
               "Strong trend (ADX>30) + RSI<40 (trending oversold)",
               conditions=[{"indicator": "adx", "op": ">", "value": 30},
                           {"indicator": "rsi_14", "op": "<", "value": 40}])
        _check([r for r in _mine_src if r["adx"] < 15 and 0.3 < r["bb_pct"] < 0.7],
               "No trend (ADX<15) + mid-BB range (mean reversion zone)",
               conditions=[{"indicator": "adx", "op": "<", "value": 15},
                           {"indicator": "bb_pct", "op": ">", "value": 0.3},
                           {"indicator": "bb_pct", "op": "<", "value": 0.7}])
    
        # ── EMA stack patterns (use ema_stack boolean for parity) ──
        _check([r for r in _mine_src if r["ema_stack"] and r["adx"] > 20],
               "EMA stacking bullish + trending (ADX>20)",
               conditions=[{"indicator": "ema_stack", "op": "==", "value": True},
                           {"indicator": "adx", "op": ">", "value": 20}])
    
        # ── Triple / multi-indicator confluence ──
        _check([r for r in _mine_src
                if r["rsi"] < 35 and r["macd"] > r["macd_sig"] and r["bb_pct"] < 0.2],
               "Triple confluence: RSI<35 + MACD bullish + near lower BB",
               conditions=[{"indicator": "rsi_14", "op": "<", "value": 35},
                           {"indicator": "macd", "op": ">", "ref": "macd_signal"},
                           {"indicator": "bb_pct", "op": "<", "value": 0.2}])
        _check([r for r in _mine_src
                if r["rsi"] > 55 and r["adx"] > 25 and r["macd"] > r["macd_sig"]],
               "Momentum confluence: RSI>55 + ADX>25 + MACD bullish (trend continuation)",
               conditions=[{"indicator": "rsi_14", "op": ">", "value": 55},
                           {"indicator": "adx", "op": ">", "value": 25},
                           {"indicator": "macd", "op": ">", "ref": "macd_signal"}])
    
        # ── ATR-relative patterns (use bb_squeeze as proxy for low vol) ──
        atr_vals = [r["atr"] for r in _mine_src if r["atr"] > 0]
        if atr_vals:
            atr_median = sorted(atr_vals)[len(atr_vals) // 2]
            _check([r for r in _mine_src if r["atr"] > atr_median * 1.5 and r["rsi"] < 35
                    and r["macd_hist"] > 0],
                   "High volatility + oversold RSI + MACD turning (capitulation bounce)",
                   conditions=[{"indicator": "rsi_14", "op": "<", "value": 35},
                               {"indicator": "macd_histogram", "op": ">", "value": 0}])
            _check([r for r in _mine_src if r.get("bb_squeeze") and r["adx"] < 20],
                   "Low volatility squeeze (BB squeeze) + no trend — breakout expected",
                   conditions=[{"indicator": "bb_squeeze", "op": "==", "value": True},
                               {"indicator": "adx", "op": "<", "value": 20}])
    
        # ── Crypto-specific (min 2 conditions) ──
        crypto = [r for r in _mine_src if r["is_crypto"]]
        if crypto:
            _check([r for r in crypto if r["rsi"] < 25 and r["stoch_k"] < 20],
                   "Crypto deep oversold (RSI<25 + Stoch<20)",
                   conditions=[{"indicator": "rsi_14", "op": "<", "value": 25},
                               {"indicator": "stochastic_k", "op": "<", "value": 20}])
            _check([r for r in crypto if r["rsi"] < 35 and r["macd_hist"] > 0],
                   "Crypto RSI<35 + MACD histogram positive — reversal",
                   conditions=[{"indicator": "rsi_14", "op": "<", "value": 35},
                               {"indicator": "macd_histogram", "op": ">", "value": 0}])
    
        # ── Trend continuation ──
        _check([r for r in _mine_src if r["above_sma20"] and r["rsi"] > 50 and r["adx"] > 20],
               "Above SMA20 + RSI>50 + ADX>20 (healthy uptrend)",
               conditions=[{"indicator": "price", "op": ">", "ref": "sma_20"},
                           {"indicator": "rsi_14", "op": ">", "value": 50},
                           {"indicator": "adx", "op": ">", "value": 20}])
    
        # ── Stochastic patterns (upgraded to 2+ conditions) ──
        _check([r for r in _mine_src if r["stoch_k"] < 20 and r["rsi"] < 40],
               "Stochastic oversold (K<20) + RSI<40 (double confirmation)",
               conditions=[{"indicator": "stochastic_k", "op": "<", "value": 20},
                           {"indicator": "rsi_14", "op": "<", "value": 40}])
    
        _check([r for r in _mine_src if r["bb_pct"] < 0.15 and r["macd_hist"] > 0],
               "Lower BB + MACD turning positive (bounce setup)",
               conditions=[{"indicator": "bb_pct", "op": "<", "value": 0.15},
                           {"indicator": "macd_histogram", "op": ">", "value": 0}])
    
        _check([r for r in _mine_src if r["above_sma20"] and r["ema_stack"] and r["adx"] > 20],
               "Full alignment: EMA stack + above SMA20 + ADX>20 (strong trend)",
               conditions=[{"indicator": "price", "op": ">", "ref": "sma_20"},
                           {"indicator": "ema_stack", "op": "==", "value": True},
                           {"indicator": "adx", "op": ">", "value": 20}])
    
        # ── Stochastic + MACD confluence ──
        _check([r for r in _mine_src if r["stoch_k"] < 20 and r["macd_hist"] > 0],
               "Stochastic oversold + MACD turning positive (double bottom signal)",
               conditions=[{"indicator": "stochastic_k", "op": "<", "value": 20},
                           {"indicator": "macd_histogram", "op": ">", "value": 0}])
        _check([r for r in _mine_src if r["stoch_k"] > 80 and r["macd_hist"] < 0],
               "Stochastic overbought + MACD turning negative — sell signal",
               conditions=[{"indicator": "stochastic_k", "op": ">", "value": 80},
                           {"indicator": "macd_histogram", "op": "<", "value": 0}])
    
        # ── EMA stack with RSI confirmation ──
        _check([r for r in _mine_src if r["ema_stack"] and 40 <= r["rsi"] <= 60],
               "EMA stack + RSI neutral zone (healthy trend, not overextended)",
               conditions=[{"indicator": "ema_stack", "op": "==", "value": True},
                           {"indicator": "rsi_14", "op": ">=", "value": 40},
                           {"indicator": "rsi_14", "op": "<=", "value": 60}])
    
        # ── Extreme RSI with trend ──
        _check([r for r in _mine_src if r["rsi"] < 25 and r["adx"] > 20],
               "Deep oversold RSI<25 in trending market (sharp reversal setup)",
               conditions=[{"indicator": "rsi_14", "op": "<", "value": 25},
                           {"indicator": "adx", "op": ">", "value": 20}])
    
        # ── Consolidation breakout ──
        _check([r for r in _mine_src if r["bb_pct"] > 0.5 and r["bb_pct"] < 0.7
                and r["adx"] < 20 and r["macd_hist"] > 0],
               "Mid-BB range + low ADX + MACD positive (consolidation breakout)",
               conditions=[{"indicator": "bb_pct", "op": ">", "value": 0.5},
                           {"indicator": "bb_pct", "op": "<", "value": 0.7},
                           {"indicator": "adx", "op": "<", "value": 20},
                           {"indicator": "macd_histogram", "op": ">", "value": 0}])
    
        # ── Bearish divergence ──
        _check([r for r in _mine_src if r["rsi"] > 60 and r["macd_hist"] < 0 and r["adx"] > 25],
               "RSI>60 but MACD negative + strong trend — bearish divergence sell signal",
               conditions=[{"indicator": "rsi_14", "op": ">", "value": 60},
                           {"indicator": "macd_histogram", "op": "<", "value": 0},
                           {"indicator": "adx", "op": ">", "value": 25}])
    
        # ── Volume spike patterns ──
        vol_rows = [r for r in _mine_src if r.get("vol_ratio") is not None]
        if vol_rows:
            _check([r for r in vol_rows if r["vol_ratio"] > 2.0 and r["rsi"] < 40],
                   "Volume spike 2x+ with RSI<40 (capitulation / accumulation)",
                   conditions=[{"indicator": "volume_ratio", "op": ">", "value": 2.0},
                               {"indicator": "rsi_14", "op": "<", "value": 40}])
            _check([r for r in vol_rows if r["vol_ratio"] > 2.0 and r["ema_stack"]],
                   "Volume spike 2x+ with EMA stack (breakout confirmation)",
                   conditions=[{"indicator": "volume_ratio", "op": ">", "value": 2.0},
                               {"indicator": "ema_stack", "op": "==", "value": True}])
            _check([r for r in vol_rows if r["vol_ratio"] > 1.5 and r["macd_hist"] > 0
                    and r["rsi"] > 50],
                   "Volume surge + MACD positive + RSI>50 (momentum ignition)",
                   conditions=[{"indicator": "volume_ratio", "op": ">", "value": 1.5},
                               {"indicator": "macd_histogram", "op": ">", "value": 0},
                               {"indicator": "rsi_14", "op": ">", "value": 50}])
    
        # ── Gap patterns (upgraded to 3 conditions) ──
        gap_rows = [r for r in _mine_src if r.get("gap_pct") is not None]
        if gap_rows:
            _check([r for r in gap_rows if r["gap_pct"] > 2.0 and r["rsi"] < 70
                    and r.get("vol_ratio") is not None and r["vol_ratio"] > 1.5],
                   "Gap up >2% + RSI not overbought + volume confirmation (momentum gap)",
                   conditions=[{"indicator": "gap_pct", "op": ">", "value": 2.0},
                               {"indicator": "rsi_14", "op": "<", "value": 70},
                               {"indicator": "volume_ratio", "op": ">", "value": 1.5}])
            _check([r for r in gap_rows if r["gap_pct"] < -2.0 and r["rsi"] < 30
                    and r["macd_hist"] > 0],
                   "Gap down >2% + RSI oversold + MACD turning (gap-fill reversal)",
                   conditions=[{"indicator": "gap_pct", "op": "<", "value": -2.0},
                               {"indicator": "rsi_14", "op": "<", "value": 30},
                               {"indicator": "macd_histogram", "op": ">", "value": 0}])
    
        # ── Momentum pullback patterns ──
        if vol_rows:
            _check([r for r in vol_rows if r["vol_ratio"] > 5.0
                    and r["macd"] > r["macd_sig"] and r["macd_hist"] > 0
                    and r["rsi"] < 65],
                   "MACD positive + volume surge 5x+ (momentum pullback setup)",
                   conditions=[{"indicator": "volume_ratio", "op": ">", "value": 5.0},
                               {"indicator": "macd", "op": ">", "ref": "macd_signal"},
                               {"indicator": "macd_histogram", "op": ">", "value": 0},
                               {"indicator": "rsi_14", "op": "<", "value": 65}])
    
        _check([r for r in _mine_src
                if r["rsi"] > 60 and r.get("vol_ratio") is not None
                and r["vol_ratio"] > 2.0 and r["macd_hist"] < 0],
               "High RSI + volume spike + MACD turning negative (topping/reversal warning)",
               conditions=[{"indicator": "rsi_14", "op": ">", "value": 60},
                           {"indicator": "volume_ratio", "op": ">", "value": 2.0},
                           {"indicator": "macd_histogram", "op": "<", "value": 0}])
    
        _check([r for r in _mine_src
                if r["macd"] < r["macd_sig"] and r["macd_hist"] < 0
                and r["rsi"] > 40 and r["adx"] > 20],
               "MACD flipped negative in active trend — setup invalidated (avoid entry)",
               conditions=[{"indicator": "macd", "op": "<", "ref": "macd_signal"},
                           {"indicator": "macd_histogram", "op": "<", "value": 0},
                           {"indicator": "rsi_14", "op": ">", "value": 40},
                           {"indicator": "adx", "op": ">", "value": 20}])
    
        if gap_rows and vol_rows:
            _check([r for r in gap_rows
                    if r["gap_pct"] > 10.0 and r["macd_hist"] > 0
                    and r.get("vol_ratio") is not None and r["vol_ratio"] > 3.0],
                   "10%+ gapper + MACD positive + high volume (high-conviction momentum)",
                   conditions=[{"indicator": "gap_pct", "op": ">", "value": 10.0},
                               {"indicator": "macd_histogram", "op": ">", "value": 0},
                               {"indicator": "volume_ratio", "op": ">", "value": 3.0}])
    
        # ── First pullback (full parity: MACD + hist + EMA stack + volume) ──
        _check([r for r in _mine_src
                if r["rsi"] > 45 and r["rsi"] < 65
                and r["macd"] > r["macd_sig"] and r["macd_hist"] > 0
                and r["ema_stack"] and r.get("vol_ratio") is not None
                and r["vol_ratio"] > 1.5],
               "First pullback: MACD+, EMA stack, rising volume (bread-and-butter entry)",
               conditions=[{"indicator": "rsi_14", "op": ">", "value": 45},
                           {"indicator": "rsi_14", "op": "<", "value": 65},
                           {"indicator": "macd", "op": ">", "ref": "macd_signal"},
                           {"indicator": "macd_histogram", "op": ">", "value": 0},
                           {"indicator": "ema_stack", "op": "==", "value": True},
                           {"indicator": "volume_ratio", "op": ">", "value": 1.5}])
    
        # ── Extended pullback (parity: broken EMA stack) ──
        _check([r for r in _mine_src
                if r["rsi"] < 35 and r["macd_hist"] < 0
                and r["adx"] > 15 and not r["ema_stack"]],
               "Extended pullback with MACD negative + broken EMA stack — setup dead",
               conditions=[{"indicator": "rsi_14", "op": "<", "value": 35},
                           {"indicator": "macd_histogram", "op": "<", "value": 0},
                           {"indicator": "adx", "op": ">", "value": 15},
                           {"indicator": "ema_stack", "op": "==", "value": False}])
    
        # ── Stochastic divergence patterns (true divergence boolean) ──
        _check([r for r in _mine_src if r.get("stoch_bull_div") and r["rsi"] < 40],
               "Stochastic bullish divergence + RSI<40",
               conditions=[{"indicator": "stoch_bull_div", "op": "==", "value": True},
                           {"indicator": "rsi_14", "op": "<", "value": 40}])
        _check([r for r in _mine_src if r.get("stoch_bear_div") and r["rsi"] > 60],
               "Stochastic bearish divergence + RSI>60 — sell signal",
               conditions=[{"indicator": "stoch_bear_div", "op": "==", "value": True},
                           {"indicator": "rsi_14", "op": ">", "value": 60}])
        _check([r for r in _mine_src if r.get("stoch_bull_div") and r["macd_hist"] > 0],
               "Stoch bullish divergence + MACD turning positive (reversal confirmation)",
               conditions=[{"indicator": "stoch_bull_div", "op": "==", "value": True},
                           {"indicator": "macd_histogram", "op": ">", "value": 0}])
        _check([r for r in _mine_src if r.get("stoch_bear_div") and r["macd_hist"] < 0],
               "Stoch bearish divergence + MACD turning negative (top confirmation)",
               conditions=[{"indicator": "stoch_bear_div", "op": "==", "value": True},
                           {"indicator": "macd_histogram", "op": "<", "value": 0}])
    
        # ── Multi-indicator confluence patterns ──
        _check([r for r in _mine_src
                if r["rsi"] < 35 and r["stoch_k"] < 25 and r["bb_pct"] < 0.15],
               "Triple oversold confluence: RSI<35 + Stoch<25 + BB<0.15",
               conditions=[{"indicator": "rsi_14", "op": "<", "value": 35},
                           {"indicator": "stochastic_k", "op": "<", "value": 25},
                           {"indicator": "bb_pct", "op": "<", "value": 0.15}])
        _check([r for r in _mine_src
                if r["adx"] > 30 and r["stoch_k"] < 20 and r["ema_stack"]],
               "Trend pullback to oversold: ADX>30 + Stoch<20 + EMA stack",
               conditions=[{"indicator": "adx", "op": ">", "value": 30},
                           {"indicator": "stochastic_k", "op": "<", "value": 20},
                           {"indicator": "ema_stack", "op": "==", "value": True}])
        _check([r for r in _mine_src
                if r.get("stoch_bull_div") and r["rsi"] < 40 and r["bb_pct"] < 0.25],
               "Multi-signal reversal: stoch bull divergence + RSI<40 + near lower BB",
               conditions=[{"indicator": "stoch_bull_div", "op": "==", "value": True},
                           {"indicator": "rsi_14", "op": "<", "value": 40},
                           {"indicator": "bb_pct", "op": "<", "value": 0.25}])
    
        # ── News sentiment + technical confluence patterns ──
        sent_rows = [r for r in _mine_src if r.get("news_sentiment") is not None]
        if len(sent_rows) >= 5:
            _check([r for r in sent_rows if r["news_sentiment"] > 0.15 and r["rsi"] < 35
                    and r["macd_hist"] > 0],
                   "Bullish news + RSI oversold + MACD turning — contrarian catalyst",
                   conditions=[{"indicator": "news_sentiment", "op": ">", "value": 0.15},
                               {"indicator": "rsi_14", "op": "<", "value": 35},
                               {"indicator": "macd_histogram", "op": ">", "value": 0}])
            _check([r for r in sent_rows if r["news_sentiment"] < -0.15 and r["rsi"] > 70
                    and r["macd_hist"] < 0],
                   "Bearish news + RSI overbought + MACD negative — sell signal confluence",
                   conditions=[{"indicator": "news_sentiment", "op": "<", "value": -0.15},
                               {"indicator": "rsi_14", "op": ">", "value": 70},
                               {"indicator": "macd_histogram", "op": "<", "value": 0}])
            _check([r for r in sent_rows if r["news_sentiment"] > 0.15 and r["macd_hist"] > 0
                    and r["ema_stack"]],
                   "Bullish news + MACD positive + EMA stack — momentum confirmation",
                   conditions=[{"indicator": "news_sentiment", "op": ">", "value": 0.15},
                               {"indicator": "macd_histogram", "op": ">", "value": 0},
                               {"indicator": "ema_stack", "op": "==", "value": True}])
            _check([r for r in sent_rows if r["news_sentiment"] < -0.15 and r["macd_hist"] < 0
                    and r["rsi"] > 50],
                   "Bearish news + MACD negative + RSI>50 — downtrend confirmation",
                   conditions=[{"indicator": "news_sentiment", "op": "<", "value": -0.15},
                               {"indicator": "macd_histogram", "op": "<", "value": 0},
                               {"indicator": "rsi_14", "op": ">", "value": 50}])
            _check([r for r in sent_rows if r.get("news_count", 0) >= 5
                    and r.get("vol_ratio") is not None and r["vol_ratio"] > 2
                    and r["adx"] > 20],
                   "High news volume + high trading volume + trending — event-driven breakout",
                   conditions=[{"indicator": "volume_ratio", "op": ">", "value": 2.0},
                               {"indicator": "adx", "op": ">", "value": 20}])
            _check([r for r in sent_rows if r["news_sentiment"] > 0.2 and r["stoch_k"] < 25
                    and r["rsi"] < 40],
                   "Strong bullish news + stochastic oversold + RSI<40 — bounce",
                   conditions=[{"indicator": "news_sentiment", "op": ">", "value": 0.2},
                               {"indicator": "stochastic_k", "op": "<", "value": 25},
                               {"indicator": "rsi_14", "op": "<", "value": 40}])
            _check([r for r in sent_rows if abs(r["news_sentiment"]) < 0.05
                    and r["adx"] > 30 and r["rsi"] < 40],
                   "Neutral news + strong trend (ADX>30) + RSI<40 — trend pullback, no catalyst fear",
                   conditions=[{"indicator": "adx", "op": ">", "value": 30},
                               {"indicator": "rsi_14", "op": "<", "value": 40}])
    
        # ── Volume profile patterns ──
        vp_rows = [r for r in _mine_src if r.get("vol_ratio") is not None and r.get("atr") and r["atr"] > 0]
        if len(vp_rows) >= 20:
            _check([r for r in vp_rows if r["vol_ratio"] > 3.0 and r["bb_pct"] > 0.8
                    and r["adx"] > 20],
                   "Volume profile breakout: 3x volume + upper BB + trending (institutional buying)",
                   conditions=[{"indicator": "volume_ratio", "op": ">", "value": 3.0},
                               {"indicator": "bb_pct", "op": ">", "value": 0.8},
                               {"indicator": "adx", "op": ">", "value": 20}])
            _check([r for r in vp_rows if r["vol_ratio"] < 0.5 and r["bb_pct"] > 0.4
                    and r["bb_pct"] < 0.6 and r["adx"] < 15],
                   "Volume dry-up + mid-BB + no trend: coiling before expansion",
                   conditions=[{"indicator": "volume_ratio", "op": "<", "value": 0.5},
                               {"indicator": "bb_pct", "op": ">", "value": 0.4},
                               {"indicator": "bb_pct", "op": "<", "value": 0.6},
                               {"indicator": "adx", "op": "<", "value": 15}])
            _check([r for r in vp_rows if r["vol_ratio"] > 2.0 and r["rsi"] > 50
                    and r["macd_hist"] > 0 and r["ema_stack"]],
                   "Volume accumulation: 2x vol + RSI>50 + MACD+ + EMA stack (institutional trend)",
                   conditions=[{"indicator": "volume_ratio", "op": ">", "value": 2.0},
                               {"indicator": "rsi_14", "op": ">", "value": 50},
                               {"indicator": "macd_histogram", "op": ">", "value": 0},
                               {"indicator": "ema_stack", "op": "==", "value": True}])
    
        # ── Cross-asset / correlation patterns ──
        crypto_rows = [r for r in _mine_src if r["is_crypto"]]
        stock_rows = [r for r in _mine_src if not r["is_crypto"]]
        if len(crypto_rows) >= 10 and len(stock_rows) >= 10:
            crypto_avg = sum(r["ret_5d"] for r in crypto_rows) / len(crypto_rows)
            stock_avg = sum(r["ret_5d"] for r in stock_rows) / len(stock_rows)
            if crypto_avg > 0.5 and stock_avg < -0.5:
                _check([r for r in crypto_rows if r["rsi"] > 50 and r["macd_hist"] > 0
                        and r["ema_stack"]],
                       "Crypto divergence: crypto bullish + EMA stack while stocks weak",
                       conditions=[{"indicator": "rsi_14", "op": ">", "value": 50},
                                   {"indicator": "macd_histogram", "op": ">", "value": 0},
                                   {"indicator": "ema_stack", "op": "==", "value": True}])
            if stock_avg > 0.5 and crypto_avg < -0.5:
                _check([r for r in stock_rows if r["ema_stack"] and r["adx"] > 20
                        and r["rsi"] > 50],
                       "Stock leadership: EMA stack + trending + RSI>50 (traditional risk-on)",
                       conditions=[{"indicator": "ema_stack", "op": "==", "value": True},
                                   {"indicator": "adx", "op": ">", "value": 20},
                                   {"indicator": "rsi_14", "op": ">", "value": 50}])
    
        # ── Microstructure / price action patterns ──
        atr_rows = [r for r in _mine_src if r.get("atr") and r["atr"] > 0]
        if len(atr_rows) >= 20:
            atr_med = sorted([r["atr"] for r in atr_rows])[len(atr_rows) // 2]
            _check([r for r in atr_rows if r.get("bb_squeeze") and r["adx"] < 15
                    and r["bb_pct"] > 0.3 and r["bb_pct"] < 0.7],
                   "Extreme compression: BB squeeze + ADX<15 + mid-BB (NR setup pre-breakout)",
                   conditions=[{"indicator": "bb_squeeze", "op": "==", "value": True},
                               {"indicator": "adx", "op": "<", "value": 15},
                               {"indicator": "bb_pct", "op": ">", "value": 0.3},
                               {"indicator": "bb_pct", "op": "<", "value": 0.7}])
            _check([r for r in atr_rows if r["atr"] > atr_med * 2.0 and r["rsi"] < 30
                    and r["macd_hist"] > 0 and r["bb_pct"] < 0.15],
                   "Volatility expansion + oversold + MACD turning + lower BB: capitulation reversal",
                   conditions=[{"indicator": "rsi_14", "op": "<", "value": 30},
                               {"indicator": "macd_histogram", "op": ">", "value": 0},
                               {"indicator": "bb_pct", "op": "<", "value": 0.15}])
    
        # ── Composite multi-signal miners (5+ conditions) ──
        _check([r for r in _mine_src
                if r["rsi"] > 45 and r["rsi"] < 65
                and r["macd"] > r["macd_sig"] and r["macd_hist"] > 0
                and r["ema_stack"] and r["adx"] > 20
                and r.get("vol_ratio") is not None and r["vol_ratio"] > 1.5
                and r["bb_pct"] > 0.4 and r["bb_pct"] < 0.8],
               "Full setup: RSI neutral + MACD+ + EMA stack + ADX>20 + vol surge + mid-BB (highest conviction)",
               conditions=[
                   {"indicator": "rsi_14", "op": ">=", "value": 45},
                   {"indicator": "rsi_14", "op": "<=", "value": 65},
                   {"indicator": "macd", "op": ">", "ref": "macd_signal"},
                   {"indicator": "macd_histogram", "op": ">", "value": 0},
                   {"indicator": "ema_stack", "op": "==", "value": True},
                   {"indicator": "adx", "op": ">", "value": 20},
                   {"indicator": "volume_ratio", "op": ">", "value": 1.5},
                   {"indicator": "bb_pct", "op": ">", "value": 0.4},
                   {"indicator": "bb_pct", "op": "<", "value": 0.8},
               ])
        _check([r for r in _mine_src
                if r["stoch_k"] < 25 and r["rsi"] < 35 and r["bb_pct"] < 0.15
                and r["macd_hist"] > 0 and r.get("vol_ratio") is not None and r["vol_ratio"] > 1.5],
               "Quad oversold bounce: Stoch<25 + RSI<35 + BB<15% + MACD turning + volume (max-conviction reversal)",
               conditions=[
                   {"indicator": "stochastic_k", "op": "<", "value": 25},
                   {"indicator": "rsi_14", "op": "<", "value": 35},
                   {"indicator": "bb_pct", "op": "<", "value": 0.15},
                   {"indicator": "macd_histogram", "op": ">", "value": 0},
                   {"indicator": "volume_ratio", "op": ">", "value": 1.5},
               ])
    
        try:
            if getattr(settings, "brain_regime_mining_enabled", True):
                from .regime_mining import run_regime_gated_mining_checks
    
                run_regime_gated_mining_checks(tf_rows, _check)
        except Exception:
            logger.debug("[learning] mine_patterns: non-critical operation failed", exc_info=True)

    # Maintenance: decay / demote stale trading insights (confidence), not a standalone “cycle step”.
    existing = get_insights(db, user_id, limit=50)
    now = datetime.utcnow()
    _PROTECTED_ORIGINS = {"user_seeded", "seed", "user", "exit_variant", "entry_variant", "combo_variant", "tf_variant", "scope_variant"}
    for ins in existing:
        sp = None
        if getattr(ins, "scan_pattern_id", None):
            from ...models.trading import ScanPattern as _SP
            sp = db.query(_SP).get(ins.scan_pattern_id)

        origin = getattr(sp, "origin", None) if sp else None
        if origin in _PROTECTED_ORIGINS:
            continue

        if ins.evidence_count >= 5 and ins.confidence < 0.35:
            if (ins.win_count or 0) > 0 and (ins.win_count or 0) / max(1, (ins.win_count or 0) + (ins.loss_count or 0)) >= 0.4:
                continue
            old_conf = ins.confidence
            ins.active = False
            db.commit()
            log_learning_event(
                db, user_id, "demotion",
                f"Pattern demoted (low confidence {old_conf:.0%}): {ins.pattern_description[:100]}",
                confidence_before=old_conf, confidence_after=0,
                related_insight_id=ins.id,
            )
            continue

        days_since_seen = (now - (ins.last_seen or ins.created_at)).days
        if days_since_seen > 30:
            months_inactive = days_since_seen / 30
            decay = 0.95 ** months_inactive
            old_conf = ins.confidence
            ins.confidence = round(max(0.05, ins.confidence * decay), 3)
            if ins.confidence < 0.15:
                if origin in _PROTECTED_ORIGINS:
                    ins.confidence = max(ins.confidence, 0.15)
                    db.commit()
                    continue
                ins.active = False
                db.commit()
                log_learning_event(
                    db, user_id, "demotion",
                    f"Pattern decayed and demoted (inactive {days_since_seen}d): {ins.pattern_description[:100]}",
                    confidence_before=old_conf, confidence_after=ins.confidence,
                    related_insight_id=ins.id,
                )
            elif abs(ins.confidence - old_conf) > 0.01:
                db.commit()

    logger.info(
        "[mine_patterns] Discovered %d patterns from %d data points (trials=%d)",
        len(discoveries), len(all_rows), get_trial_count(),
    )
    return discoveries


_PATTERN_CONDITION_MAP: dict[str, dict[str, Any]] = {
    "rsi oversold": {"field": "rsi", "op": "lt", "val": 30},
    "rsi overbought": {"field": "rsi", "op": "gt", "val": 70},
    "rsi near-oversold": {"field": "rsi", "op": "lt", "val": 40},
    "macd bullish": {"field": "macd", "op": "gt_field", "val": "macd_sig"},
    "macd positive": {"field": "macd_hist", "op": "gt", "val": 0},
    "macd negative": {"field": "macd_hist", "op": "lt", "val": 0},
    "ema stack": {"field": "ema_stack", "op": "eq", "val": True},
    "bollinger": {"field": "bb_pct", "op": "lt", "val": 0.15},
    "adx>25": {"field": "adx", "op": "gt", "val": 25},
    "adx>30": {"field": "adx", "op": "gt", "val": 30},
    "stoch oversold": {"field": "stoch_k", "op": "lt", "val": 20},
    "stoch overbought": {"field": "stoch_k", "op": "gt", "val": 80},
    "volume surge": {"field": "vol_ratio", "op": "gt", "val": 2.0},
    "5x": {"field": "vol_ratio", "op": "gt", "val": 5.0},
    "gap up": {"field": "gap_pct", "op": "gt", "val": 2.0},
    "pullback": {"field": "rsi", "op": "lt", "val": 55},
}


def _row_matches_condition(row: dict, cond: dict) -> bool:
    val = row.get(cond["field"])
    if val is None:
        return False
    op = cond["op"]
    target = cond["val"]
    if op == "lt":
        return val < target
    elif op == "gt":
        return val > target
    elif op == "eq":
        return val == target
    elif op == "gt_field":
        return val > (row.get(target) or 0)
    return False


def _filter_rows_by_condition(rows: list[dict], condition_str: str) -> list[dict]:
    """Parse condition expressions and filter rows.

    Supports:
      - Numeric comparisons: ``rsi > 65``, ``adx <= 20``, ``vol_ratio >= 3``
      - Boolean fields: ``ema_stack == true``, ``bb_squeeze == 1``, ``bb_squeeze == false``
      - String equality: ``narrow_range == NR7``, ``regime == risk_on``
      - Compound AND: ``rsi > 65 and ema_stack == true and resistance_retests >= 3``
      - Field-vs-field: ``macd > macd_sig``
    """
    import re
    parts = re.split(r'\s+and\s+', condition_str.strip())
    filtered = list(rows)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        m = re.match(r'(\w+)\s*([<>=!]+)\s*(.+)', part)
        if not m:
            continue
        field = m.group(1)
        op_str = m.group(2)
        raw_val = m.group(3).strip()

        FIELD_ALIASES = {
            "rsi_14": "rsi",
            "rsi14": "rsi",
            "rel_vol": "vol_ratio",
            "rvol": "vol_ratio",
            "relative_volume": "vol_ratio",
            "macd_histogram": "macd_hist",
            "stoch": "stoch_k",
            "stochastic": "stoch_k",
            "retest_count": "resistance_retests",
            "retests": "resistance_retests",
        }
        field = FIELD_ALIASES.get(field, field)

        bool_map = {"true": True, "false": False, "1": True, "0": False}
        if raw_val.lower() in bool_map:
            target_val = bool_map[raw_val.lower()]
            if op_str == "==" or op_str == "=":
                filtered = [r for r in filtered if r.get(field) == target_val]
            elif op_str == "!=" or op_str == "<>":
                filtered = [r for r in filtered if r.get(field) != target_val]
            continue

        try:
            threshold = float(raw_val)
            is_numeric = True
        except ValueError:
            is_numeric = False

        if is_numeric:
            if op_str == "<":
                filtered = [r for r in filtered if _num(r, field) < threshold]
            elif op_str == "<=":
                filtered = [r for r in filtered if _num(r, field) <= threshold]
            elif op_str == ">":
                filtered = [r for r in filtered if _num(r, field) > threshold]
            elif op_str == ">=":
                filtered = [r for r in filtered if _num(r, field) >= threshold]
            elif op_str == "==" or op_str == "=":
                filtered = [r for r in filtered if _num(r, field) == threshold]
            elif op_str == "!=" or op_str == "<>":
                filtered = [r for r in filtered if _num(r, field) != threshold]
        else:
            other_field = raw_val
            if any(r.get(other_field) is not None for r in filtered[:20]):
                if op_str == ">":
                    filtered = [r for r in filtered
                                if _num(r, field) > _num(r, other_field)]
                elif op_str == ">=":
                    filtered = [r for r in filtered
                                if _num(r, field) >= _num(r, other_field)]
                elif op_str == "<":
                    filtered = [r for r in filtered
                                if _num(r, field) < _num(r, other_field)]
                elif op_str == "<=":
                    filtered = [r for r in filtered
                                if _num(r, field) <= _num(r, other_field)]
            else:
                if op_str == "==" or op_str == "=":
                    filtered = [r for r in filtered if str(r.get(field, "")).lower() == raw_val.lower()]
                elif op_str == "!=" or op_str == "<>":
                    filtered = [r for r in filtered if str(r.get(field, "")).lower() != raw_val.lower()]

    return filtered


def _num(row: dict, field: str, default: float = 0.0) -> float:
    """Extract a numeric value from a row, coercing booleans and Nones."""
    v = row.get(field)
    if v is None:
        return default
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def seek_pattern_data(db: Session, user_id: int | None) -> dict[str, Any]:
    """Actively mine more data for under-sampled but promising patterns.

    Identifies insights with few evidence samples but decent confidence,
    then mines a broader ticker set specifically looking for bars that
    match those pattern conditions to boost evidence counts.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .prescreen_job import prescreen_candidates_for_universe

    insights = get_insights(db, user_id, limit=50)
    under_sampled = [
        ins for ins in insights
        if ins.evidence_count < 20 and ins.confidence > 0.4 and ins.active
    ]
    if not under_sampled:
        return {"sought": 0, "note": "no under-sampled patterns"}

    try:
        seek_tickers = prescreen_candidates_for_universe(
            db, max_total=600, include_crypto=True,
        )
    except Exception:
        from .market_data import ALL_SCAN_TICKERS
        seek_tickers = list(ALL_SCAN_TICKERS)

    seek_tickers = seek_tickers[:400]
    from ...config import settings as _seek_settings

    _workers = (
        io_workers_high(_seek_settings)
        if (_use_massive() or _use_polygon())
        else io_workers_med(_seek_settings)
    )
    extra_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in seek_tickers}
        for f in as_completed(futs):
            if _shutting_down.is_set():
                break
            try:
                extra_rows.extend(f.result())
            except Exception:
                logger.debug("[learning] seek_pattern_data: non-critical operation failed", exc_info=True)

    extra_rows = dedupe_sample_rows(extra_rows)

    if len(extra_rows) < 10:
        return {"sought": 0, "rows_mined": len(extra_rows)}

    boosted = 0
    for ins in under_sampled:
        desc_lower = ins.pattern_description.lower()
        conditions = [
            cond for keyword, cond in _PATTERN_CONDITION_MAP.items()
            if keyword in desc_lower
        ]
        if not conditions:
            continue

        matching = [
            r for r in extra_rows
            if all(_row_matches_condition(r, c) for c in conditions)
        ]
        if len(matching) < 3:
            continue

        new_rows: list[dict] = []
        for r in matching:
            biv = r.get("bar_interval") or "1d"
            bst = r.get("bar_start_utc")
            if bst is None:
                continue
            if try_insert_insight_evidence(
                db,
                insight_id=ins.id,
                ticker=r["ticker"],
                bar_interval=str(biv),
                bar_start_utc=bst,
                source="seek",
            ):
                new_rows.append(r)

        if not new_rows:
            continue

        avg_5d = sum(r["ret_5d"] for r in new_rows) / len(new_rows)
        wins = sum(1 for r in new_rows if r["ret_5d"] > 0)
        wr = wins / len(new_rows) * 100

        old_evidence = ins.evidence_count
        old_conf = ins.confidence
        ins.evidence_count = min(ins.evidence_count + len(new_rows), 200)
        ins.win_count = (ins.win_count or 0) + wins
        ins.loss_count = (ins.loss_count or 0) + (len(new_rows) - wins)

        if avg_5d > 0 and wr > 50:
            ins.confidence = round(min(0.95, old_conf * 0.7 + (wr / 100) * 0.3), 3)
        elif wr < 40:
            ins.confidence = round(max(0.1, old_conf * 0.8), 3)

        ins.last_seen = datetime.utcnow()
        db.commit()
        boosted += 1

        log_learning_event(
            db, user_id, "active_seeking",
            f"Boosted '{ins.pattern_description[:60]}' with {len(new_rows)} new bar-credits "
            f"(evidence {old_evidence}->{ins.evidence_count}, "
            f"conf {old_conf:.0%}->{ins.confidence:.0%}, "
            f"avg {avg_5d:+.2f}%/5d, {wr:.0f}%wr)",
            confidence_before=old_conf,
            confidence_after=ins.confidence,
            related_insight_id=ins.id,
        )

    logger.info(
        f"[learning] Active seeking: boosted {boosted}/{len(under_sampled)} "
        f"under-sampled patterns from {len(extra_rows)} extra rows"
    )
    return {
        "sought": boosted,
        "under_sampled_total": len(under_sampled),
        "extra_rows_mined": len(extra_rows),
    }


def _auto_backtest_patterns(db: Session, user_id: int | None) -> int:
    """Run smart, diversified backtests to validate discovered patterns.

    Uses the centralized ``smart_backtest_insight`` engine which picks
    contextually relevant tickers across sectors, maps multiple strategies,
    and runs them in parallel.
    """
    from .backtest_engine import smart_backtest_insight

    insights = get_insights(db, user_id, limit=20)
    if not insights:
        return 0

    backtests_run = 0
    for ins in insights[:20]:
        if _shutting_down.is_set():
            break
        old_conf = ins.confidence
        try:
            result = smart_backtest_insight(
                db, ins, target_tickers=25, update_confidence=True,
            )
        except Exception:
            continue

        total = result["total"]
        wins = result["wins"]
        backtests_run += result["backtests_run"]

        if total >= 3 and abs(ins.confidence - old_conf) > 0.01:
            log_learning_event(
                db, user_id, "backtest_validation",
                f"Pattern backtested ({wins}/{total} profitable): "
                f"{ins.pattern_description[:80]} | conf {old_conf:.0%}->{ins.confidence:.0%}",
                confidence_before=old_conf,
                confidence_after=ins.confidence,
                related_insight_id=ins.id,
            )

    logger.info(f"[learning] Auto-backtest: {backtests_run} backtests across {len(insights[:20])} patterns")
    return backtests_run


# Backward-compat alias (referenced by stale .pyc or external code)
auto_backtest_active_insights = _auto_backtest_patterns


def _backtest_one_pattern_from_queue(pattern_id: int, user_id: int | None) -> tuple[int, int]:
    """Run backtest for one pattern in a worker thread (own DB session). Returns (backtests_run, 1)."""
    from .backtest_queue_worker import execute_queue_backtest_for_pattern

    return execute_queue_backtest_for_pattern(pattern_id, user_id)


def _auto_backtest_from_queue(db: Session, user_id: int | None, batch_size: int | None = None) -> dict[str, Any]:
    """Process ScanPatterns from the priority queue (parallel when configured).
    
    Uses BRAIN_BACKTEST_PARALLEL workers and BRAIN_QUEUE_BATCH_SIZE (or batch_size)
    to run multiple pattern backtests concurrently, each with its own DB session.
    """
    from ...config import settings
    from .backtest_queue import (
        get_exploration_pattern_ids,
        get_pending_patterns,
        get_queue_status,
        get_retest_interval_days,
    )

    if batch_size is None:
        batch_size = settings.brain_queue_batch_size
    pattern_ids = list(get_pending_patterns(db, limit=batch_size, ids_only=True))
    exploration_added = 0

    if getattr(settings, "brain_queue_exploration_enabled", True):
        explore_cap = max(0, int(getattr(settings, "brain_queue_exploration_max", 40)))
        slots = min(batch_size - len(pattern_ids), explore_cap)
        if slots > 0:
            extra = get_exploration_pattern_ids(db, set(pattern_ids), slots)
            exploration_added = len(extra)
            pattern_ids.extend(extra)

    if not pattern_ids:
        status = get_queue_status(db, use_cache=False)
        rd = get_retest_interval_days()
        logger.info(
            "[learning] Queue backtest: no eligible patterns (batch_size=%s) | "
            "queue_pending=%s queue_empty=%s boosted=%s — "
            "eligible = active AND (boosted OR never tested OR last_backtest older than %s days)",
            batch_size,
            status.get("pending"),
            status.get("queue_empty"),
            status.get("boosted"),
            rd,
        )
        return {
            "backtests_run": 0,
            "patterns_processed": 0,
            "queue_empty": status["queue_empty"],
            "queue_exploration_added": 0,
            **status,
        }

    logger.info(
        "[learning] Queue backtest: starting batch | pattern_ids=%s (count=%s) batch_size=%s "
        "exploration_added=%s",
        pattern_ids[:50],
        len(pattern_ids),
        batch_size,
        exploration_added,
    )

    max_workers = settings.brain_backtest_parallel
    if settings.brain_max_cpu_pct is not None and _CPU_COUNT:
        cap = max(1, int(_CPU_COUNT * settings.brain_max_cpu_pct / 100))
        max_workers = min(max_workers, cap)
    max_workers = min(max_workers, len(pattern_ids))
    proc_cap = getattr(settings, "brain_queue_process_cap", None)
    if proc_cap is not None:
        max_workers = min(max_workers, max(1, int(proc_cap)))

    exec_mode = (getattr(settings, "brain_queue_backtest_executor", "threads") or "threads").strip().lower()
    use_process = exec_mode == "process" and max_workers > 1

    logger.info(
        "[learning] Queue backtest: executor=%s max_workers=%s patterns=%s child_pool=%s+%s",
        "process" if use_process else "threads",
        max_workers,
        len(pattern_ids),
        settings.brain_mp_child_database_pool_size,
        settings.brain_mp_child_database_max_overflow,
    )

    backtests_run = 0
    patterns_processed = 0

    if max_workers <= 1:
        for pid in pattern_ids:
            if _shutting_down.is_set():
                break
            bt, proc = _backtest_one_pattern_from_queue(pid, user_id)
            backtests_run += bt
            patterns_processed += proc
    elif use_process:
        from concurrent.futures import ProcessPoolExecutor
        from multiprocessing import get_context

        from .backtest_queue_worker import (
            configure_multiprocess_child_db_env,
            run_one_pattern_job,
        )

        ps = int(settings.brain_mp_child_database_pool_size)
        mo = int(settings.brain_mp_child_database_max_overflow)
        ctx = get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=ctx,
            initializer=configure_multiprocess_child_db_env,
            initargs=(ps, mo),
        ) as executor:
            futures = {
                executor.submit(run_one_pattern_job, pid, user_id): pid
                for pid in pattern_ids
            }
            for fut in as_completed(futures):
                if _shutting_down.is_set():
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except TypeError:
                        executor.shutdown(wait=False)
                    break
                try:
                    bt, proc = fut.result()
                    backtests_run += bt
                    patterns_processed += proc
                except Exception as e:
                    logger.warning("[backtest_queue] Process worker error: %s", e)
                    patterns_processed += 1
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_backtest_one_pattern_from_queue, pid, user_id): pid
                for pid in pattern_ids
            }
            for fut in as_completed(futures):
                if _shutting_down.is_set():
                    for f in futures:
                        f.cancel()
                    break
                try:
                    bt, proc = fut.result()
                    backtests_run += bt
                    patterns_processed += proc
                except Exception as e:
                    logger.warning("[backtest_queue] Worker error: %s", e)
                    patterns_processed += 1

    status = get_queue_status(db, use_cache=False)
    logger.info(
        "[learning] Queue backtest: done | backtests_run=%s patterns_processed=%s | "
        "queue_pending=%s queue_empty=%s boosted=%s",
        backtests_run,
        patterns_processed,
        status.get("pending"),
        status.get("queue_empty"),
        status.get("boosted"),
    )
    return {
        "backtests_run": backtests_run,
        "patterns_processed": patterns_processed,
        "queue_empty": status["queue_empty"],
        "queue_exploration_added": exploration_added,
        "queue_executor": "process" if use_process else "threads",
        **status,
    }


def validate_and_evolve(db: Session, user_id: int | None) -> dict[str, Any]:
    """Dynamically test hypotheses from the TradingHypothesis table against real data.

    On first run, seeds the table with the original hardcoded hypotheses.
    Every subsequent run loads all pending/testing hypotheses, evaluates them
    against mined historical rows, and updates their lifecycle state.
    The brain's perspective grows as the data grows — no hardcoded ceiling.
    """
    from .market_data import ALL_SCAN_TICKERS
    from ...models.trading import TradingHypothesis

    mine_tickers = list(ALL_SCAN_TICKERS)[:500]

    rows: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ...config import settings as _ve_settings
    import random

    _workers = (
        io_workers_high(_ve_settings)
        if (_use_massive() or _use_polygon())
        else io_workers_med(_ve_settings)
    )
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in mine_tickers}
        for f in as_completed(futs):
            try:
                rows.extend(f.result())
            except Exception:
                logger.debug("[learning] validate_and_evolve: non-critical operation failed", exc_info=True)

    if len(rows) < 30:
        return {"tested": 0, "note": "insufficient data for self-validation"}

    # ── Seed built-in hypotheses on first run ──
    existing_count = db.query(TradingHypothesis).count()
    if existing_count == 0:
        _seed_builtin_hypotheses(db)

    # ── Auto-derive hypotheses from active ScanPatterns ──
    try:
        _derive_hypotheses_from_patterns(db)
    except Exception as e:
        logger.warning(f"[learning] Pattern-derived hypothesis generation failed: {e}")

    # ── Migrate any old-format TradingInsight hypotheses ──
    try:
        _migrate_legacy_hypotheses(db, user_id)
    except Exception:
        logger.debug("[learning] validate_and_evolve: non-critical operation failed", exc_info=True)

    # ── Load all testable hypotheses ──
    # Prioritize builtin_seed origin (they have simpler, more testable conditions)
    from sqlalchemy import case
    origin_priority = case(
        (TradingHypothesis.origin == "builtin_seed", 0),
        (TradingHypothesis.origin == "llm_generated", 1),
        else_=2,
    )
    hypotheses = (
        db.query(TradingHypothesis)
        .filter(TradingHypothesis.status.in_(["pending", "testing", "confirmed", "rejected"]))
        .order_by(origin_priority, TradingHypothesis.times_tested.asc())
        .limit(50)
        .all()
    )

    results: list[dict[str, Any]] = []

    for hyp in hypotheses:
        group_a = _filter_rows_by_condition(rows, hyp.condition_a)
        group_b = _filter_rows_by_condition(rows, hyp.condition_b)

        min_samples = 3 if (hyp.times_tested or 0) == 0 else 5
        if len(group_a) < min_samples or len(group_b) < min_samples:
            continue

        avg_a = sum(r["ret_5d"] for r in group_a) / len(group_a)
        avg_b = sum(r["ret_5d"] for r in group_b) / len(group_b)
        wr_a = sum(1 for r in group_a if r["ret_5d"] > 0) / len(group_a) * 100
        wr_b = sum(1 for r in group_b if r["ret_5d"] > 0) / len(group_b) * 100

        # Bootstrap mean-difference significance to reduce heuristic confirmations.
        rng = random.Random(42 + int(hyp.id or 0) + int(hyp.times_tested or 0))
        n_boot = max(200, int(getattr(_ve_settings, "brain_hypothesis_bootstrap_iterations", 500)))
        a_vals = [float(r["ret_5d"]) for r in group_a]
        b_vals = [float(r["ret_5d"]) for r in group_b]
        deltas: list[float] = []
        for _ in range(n_boot):
            sa = rng.choices(a_vals, k=len(a_vals))
            sb = rng.choices(b_vals, k=len(b_vals))
            deltas.append((sum(sa) / len(sa)) - (sum(sb) / len(sb)))
        deltas.sort()
        lo_i = max(0, int(n_boot * 0.025))
        hi_i = min(n_boot - 1, int(n_boot * 0.975))
        ci_low = deltas[lo_i]
        ci_high = deltas[hi_i]
        mean_delta = sum(deltas) / len(deltas)
        p_nonpos = sum(1 for d in deltas if d <= 0) / len(deltas)
        p_nonneg = sum(1 for d in deltas if d >= 0) / len(deltas)
        p_value = min(1.0, 2 * min(p_nonpos, p_nonneg))
        if hyp.expected_winner == "a":
            confirmed = mean_delta > 0 and ci_low > 0 and p_value < 0.05
        else:
            confirmed = mean_delta < 0 and ci_high < 0 and p_value < 0.05

        finding = {
            "hypothesis": hyp.description,
            "hypothesis_id": hyp.id,
            "confirmed": confirmed,
            "group_a_avg": round(avg_a, 3),
            "group_b_avg": round(avg_b, 3),
            "group_a_wr": round(wr_a, 1),
            "group_b_wr": round(wr_b, 1),
            "group_a_n": len(group_a),
            "group_b_n": len(group_b),
            "bootstrap": {
                "mean_delta": round(mean_delta, 4),
                "ci_low": round(ci_low, 4),
                "ci_high": round(ci_high, 4),
                "p_value": round(p_value, 6),
                "iterations": n_boot,
            },
        }
        results.append(finding)

        hyp.times_tested = (hyp.times_tested or 0) + 1
        if confirmed:
            hyp.times_confirmed = (hyp.times_confirmed or 0) + 1
        else:
            hyp.times_rejected = (hyp.times_rejected or 0) + 1
        hyp.status = "testing"
        hyp.last_tested_at = datetime.utcnow()
        hyp.last_result_json = json.dumps(finding)

        confirm_rate = (hyp.times_confirmed or 0) / max(1, hyp.times_tested)
        if hyp.times_tested >= 5:
            if confirm_rate >= 0.7:
                hyp.status = "confirmed"
            elif confirm_rate < 0.3:
                hyp.status = "rejected"
        if hyp.times_tested >= 10 and confirm_rate < 0.4:
            hyp.status = "retired"

        if not confirmed:
            wins_a = sum(1 for r in group_a if r["ret_5d"] > 0)
            save_insight(
                db, user_id,
                f"CHILI challenge: {hyp.description} — data says otherwise "
                f"(A: {avg_a:+.2f}%/5d {wr_a:.0f}%wr vs B: {avg_b:+.2f}%/5d {wr_b:.0f}%wr, "
                f"n={len(group_a)}+{len(group_b)})",
                confidence=0.45,
                wins=wins_a, losses=len(group_a) - wins_a,
            )
            log_learning_event(
                db, user_id, "hypothesis_challenged",
                f"Data challenges: {hyp.description} | "
                f"Expected {'A' if hyp.expected_winner == 'a' else 'B'} wins, "
                f"but {'B' if hyp.expected_winner == 'a' else 'A'} actually performed better "
                f"(tested {hyp.times_tested}x, confirm rate {confirm_rate:.0%})",
            )
        else:
            if avg_a > 0 and wr_a > 55:
                wins_a = sum(1 for r in group_a if r["ret_5d"] > 0)
                save_insight(
                    db, user_id,
                    f"CHILI validated: {hyp.description} — confirmed by data "
                    f"({avg_a:+.2f}%/5d, {wr_a:.0f}%wr, {len(group_a)} samples, "
                    f"tested {hyp.times_tested}x, {confirm_rate:.0%} confirm rate)",
                    confidence=min(0.85, wr_a / 100),
                    wins=wins_a, losses=len(group_a) - wins_a,
                )
                spawned_id = _spawn_pattern_from_hypothesis(db, hyp, avg_a, wr_a, user_id)
                if spawned_id:
                    finding["spawned_pattern_id"] = spawned_id

    db.commit()

    # ── Feed real-trade per-pattern win rates back into insight confidence ──
    real_trade_adjustments = 0
    try:
        pattern_stats = get_trade_stats_by_pattern(db, user_id, min_trades=3)
        if pattern_stats:
            all_insights = get_insights(db, user_id, limit=100)
            for ps in pattern_stats:
                tag = ps["pattern"]
                real_wr = ps["win_rate"]
                for ins in all_insights:
                    if tag.replace("_", " ") in ins.pattern_description.lower():
                        old_conf = ins.confidence
                        ins.confidence = round(
                            min(0.95, old_conf * 0.5 + (real_wr / 100) * 0.5), 3
                        )
                        ins.win_count = ps.get("wins", 0) or 0
                        ins.loss_count = (ps.get("trades", 0) or 0) - (ps.get("wins", 0) or 0)
                        db.commit()
                        real_trade_adjustments += 1
                        log_learning_event(
                            db, user_id, "real_trade_validation",
                            f"Pattern '{tag}' real-trade WR {real_wr:.0f}% "
                            f"({ps['trades']} trades) adjusted confidence "
                            f"{old_conf:.0%} -> {ins.confidence:.0%}",
                            confidence_before=old_conf,
                            confidence_after=ins.confidence,
                            related_insight_id=ins.id,
                        )
                        break
    except Exception as e:
        logger.warning(f"[learning] Per-pattern trade feedback failed: {e}")

    # ── Evolve scoring weights ──
    from .scanner import evolve_strategy_weights
    weight_result = evolve_strategy_weights(db)

    confirmed_count = sum(1 for r in results if r.get("confirmed"))
    challenged_count = sum(1 for r in results if not r.get("confirmed", True))

    total_hyp = db.query(TradingHypothesis).count()
    active_hyp = db.query(TradingHypothesis).filter(
        TradingHypothesis.status.in_(["pending", "testing", "confirmed"])
    ).count()

    log_learning_event(
        db, user_id, "self_validation",
        f"Tested {len(results)} hypotheses (of {total_hyp} total, {active_hyp} active): "
        f"{confirmed_count} confirmed, {challenged_count} challenged by data. "
        f"Real-trade adjustments: {real_trade_adjustments}. "
        f"Evolved {weight_result.get('adjusted', 0)} scoring weights.",
    )

    logger.info(
        f"[learning] Dynamic self-validation: {len(results)} hypotheses tested "
        f"({total_hyp} total in pool, {active_hyp} active), "
        f"{confirmed_count} confirmed, {challenged_count} challenged, "
        f"{real_trade_adjustments} real-trade adjustments, "
        f"{weight_result.get('adjusted', 0)} weights evolved"
    )

    hypothesis_patterns_spawned = sum(
        1 for r in results if r.get("spawned_pattern_id")
    )
    return {
        "hypotheses_tested": len(results),
        "hypotheses_in_pool": total_hyp,
        "hypotheses_active": active_hyp,
        "confirmed": confirmed_count,
        "challenged": challenged_count,
        "real_trade_adjustments": real_trade_adjustments,
        "weights_evolved": weight_result.get("adjusted", 0),
        "hypothesis_patterns_spawned": hypothesis_patterns_spawned,
        "details": results,
    }


_BUILTIN_SEED_HYPOTHESES: list[dict[str, str]] = [
    {"description": "MACD positive entries outperform MACD negative entries",
     "condition_a": "macd > macd_sig and macd_hist > 0",
     "condition_b": "macd < macd_sig and macd_hist < 0",
     "expected_winner": "a", "related_weight": "macd_positive_bonus"},
    {"description": "High relative volume (>3x) entries outperform low volume (<1.5x)",
     "condition_a": "vol_ratio > 3.0",
     "condition_b": "vol_ratio < 1.5",
     "expected_winner": "a", "related_weight": "vol_surge_3x"},
    {"description": "Strong trends (ADX>25) outperform range-bound (ADX<15)",
     "condition_a": "adx > 25",
     "condition_b": "adx < 15",
     "expected_winner": "a", "related_weight": "bo_adx_trending"},
    {"description": "Bullish EMA stack outperforms broken EMA alignment",
     "condition_a": "ema_stack == true",
     "condition_b": "ema_stack == false",
     "expected_winner": "a", "related_weight": "bo_ema_support"},
    {"description": "Oversold RSI (<30) mean-reversion outperforms neutral RSI entries",
     "condition_a": "rsi < 30",
     "condition_b": "rsi >= 40 and rsi <= 60",
     "expected_winner": "a"},
    {"description": "MACD histogram positive outperforms negative in neutral RSI zone",
     "condition_a": "macd_hist > 0 and rsi > 40 and rsi < 65",
     "condition_b": "macd_hist < 0 and rsi > 40 and rsi < 65",
     "expected_winner": "a"},
    {"description": "BB squeeze + declining volume outperforms BB squeeze + rising volume",
     "condition_a": "bb_pct < 0.20 and vol_ratio < 0.8",
     "condition_b": "bb_pct < 0.20 and vol_ratio > 1.5",
     "expected_winner": "a"},
    {"description": "Consolidating (ADX<20) squeeze outperforms trending (ADX>25) squeeze",
     "condition_a": "bb_pct < 0.20 and adx < 20",
     "condition_b": "bb_pct < 0.20 and adx > 25",
     "expected_winner": "a", "related_weight": "bo_adx_consolidating"},
    {"description": "BB squeeze + bullish EMA stack outperforms squeeze + broken EMAs",
     "condition_a": "bb_pct < 0.20 and ema_stack == true",
     "condition_b": "bb_pct < 0.20 and ema_stack == false",
     "expected_winner": "a"},
    {"description": "MACD positive in risk_on outperforms MACD positive in risk_off",
     "condition_a": "macd > macd_sig and macd_hist > 0 and regime == risk_on",
     "condition_b": "macd > macd_sig and macd_hist > 0 and regime == risk_off",
     "expected_winner": "a"},
    {"description": "EMA stack in risk_on outperforms EMA stack in risk_off",
     "condition_a": "ema_stack == true and regime == risk_on",
     "condition_b": "ema_stack == true and regime == risk_off",
     "expected_winner": "a"},
    {"description": "BB squeeze in bullish SPY momentum outperforms squeeze in bearish SPY momentum",
     "condition_a": "bb_pct < 0.20 and spy_mom_5d > 1",
     "condition_b": "bb_pct < 0.20 and spy_mom_5d < -1",
     "expected_winner": "a"},
    {"description": "High volume in risk_off outperforms low volume in risk_off (flight-to-quality)",
     "condition_a": "vol_ratio > 2.0 and regime == risk_off",
     "condition_b": "vol_ratio < 1.0 and regime == risk_off",
     "expected_winner": "a"},
    {"description": "RSI momentum + EMA stack + resistance retests outperforms RSI momentum + EMA stack alone",
     "condition_a": "rsi > 65 and ema_stack == true and resistance_retests >= 3",
     "condition_b": "rsi > 65 and ema_stack == true and resistance_retests < 3",
     "expected_winner": "a", "related_weight": "bo_retest_pressure"},
    {"description": "VCP (2+ contractions) near resistance outperforms no VCP near resistance",
     "condition_a": "vcp_count >= 2 and bb_pct < 0.30",
     "condition_b": "vcp_count < 1 and bb_pct < 0.30",
     "expected_winner": "a"},
]


def _seed_builtin_hypotheses(db: Session) -> int:
    """Seed the TradingHypothesis table with initial hypotheses on first run."""
    from ...models.trading import TradingHypothesis
    added = 0
    for h in _BUILTIN_SEED_HYPOTHESES:
        hyp = TradingHypothesis(
            description=h["description"],
            condition_a=h["condition_a"],
            condition_b=h["condition_b"],
            expected_winner=h.get("expected_winner", "a"),
            origin="builtin_seed",
            status="pending",
            related_weight=h.get("related_weight"),
        )
        db.add(hyp)
        added += 1
    db.commit()
    logger.info(f"[learning] Seeded {added} builtin hypotheses")
    return added


def _migrate_legacy_hypotheses(db: Session, user_id: int | None) -> int:
    """Migrate old TradingInsight hypothesis:... entries into TradingHypothesis."""
    from ...models.trading import TradingHypothesis

    legacy = db.query(TradingInsight).filter(
        TradingInsight.active.is_(True),
        TradingInsight.pattern_description.like("hypothesis:%"),
    ).limit(20).all()

    migrated = 0
    for ins in legacy:
        desc = ins.pattern_description
        parts = desc.split("|")
        if len(parts) < 3:
            ins.active = False
            continue

        label = parts[0].replace("hypothesis:", "").strip()
        cond_a = parts[1].strip().replace("A:", "").strip()
        cond_b = parts[2].strip().replace("B:", "").strip()
        expected = "a"
        if len(parts) >= 4 and "b" in parts[3].strip().lower():
            expected = "b"

        existing = db.query(TradingHypothesis).filter_by(description=label).first()
        if not existing:
            hyp = TradingHypothesis(
                description=label,
                condition_a=cond_a,
                condition_b=cond_b,
                expected_winner=expected,
                origin="llm_generated",
                status="pending",
            )
            db.add(hyp)
            migrated += 1

        ins.active = False

    if migrated:
        db.commit()
        logger.info(f"[learning] Migrated {migrated} legacy hypothesis insights")
    return migrated


def _derive_hypotheses_from_patterns(db: Session) -> int:
    """Auto-generate A/B hypotheses from active ScanPatterns.

    For each ScanPattern with conditions, we create a hypothesis that tests
    whether the FULL pattern outperforms a version with one key condition removed.
    This makes every user/brain/web-discovered pattern automatically testable.
    """
    from ...models.trading import ScanPattern, TradingHypothesis

    patterns = db.query(ScanPattern).filter_by(active=True).all()
    existing_pattern_ids = {
        h.related_pattern_id
        for h in db.query(TradingHypothesis).filter(
            TradingHypothesis.related_pattern_id.isnot(None)
        ).all()
    }

    _OP_MAP = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "==", "!=": "!="}
    _OP_NEGATE = {">": "<=", ">=": "<", "<": ">=", "<=": ">", "==": "!="}

    added = 0
    for p in patterns:
        if p.id in existing_pattern_ids:
            continue

        try:
            rules = json.loads(p.rules_json or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        conditions = rules.get("conditions", [])
        if len(conditions) < 2:
            continue

        parts_a: list[str] = []
        for cond in conditions:
            ind = cond.get("indicator", "")
            op = _OP_MAP.get(cond.get("op", ""), "==")
            ref = cond.get("ref")
            val = cond.get("value")

            if ind == "price" and ref:
                parts_a.append(f"ema_stack == true")
                continue
            if isinstance(val, bool):
                parts_a.append(f"{ind} == {'true' if val else 'false'}")
            elif isinstance(val, list) and len(val) == 2:
                parts_a.append(f"{ind} >= {val[0]} and {ind} <= {val[1]}")
            elif isinstance(val, list):
                parts_a.append(f"{ind} == {val[0]}")
            else:
                parts_a.append(f"{ind} {op} {val}")

        if not parts_a:
            continue

        condition_a = " and ".join(parts_a)

        drop_idx = len(conditions) - 1
        last_cond = conditions[drop_idx]
        last_ind = last_cond.get("indicator", "")
        last_op = last_cond.get("op", ">")
        last_val = last_cond.get("value")
        last_ref = last_cond.get("ref")

        parts_b = list(parts_a[:-1])
        if last_ind == "price" and last_ref:
            parts_b.append("ema_stack == false")
        elif last_op in _OP_NEGATE:
            neg_op = _OP_NEGATE[last_op]
            if isinstance(last_val, bool):
                parts_b.append(f"{last_ind} == {'false' if last_val else 'true'}")
            elif isinstance(last_val, list):
                pass
            else:
                parts_b.append(f"{last_ind} {neg_op} {last_val}")
        else:
            parts_b.append(f"{last_ind} == 0")

        if not parts_b:
            continue

        condition_b = " and ".join(parts_b)
        description = f"{p.name}: full pattern outperforms partial (without {last_ind} condition)"

        hyp = TradingHypothesis(
            description=description,
            condition_a=condition_a,
            condition_b=condition_b,
            expected_winner="a",
            origin="pattern_derived",
            status="pending",
            related_pattern_id=p.id,
        )
        db.add(hyp)
        added += 1

    if added:
        db.commit()
        logger.info(f"[learning] Derived {added} hypotheses from ScanPatterns")
    return added


def _parse_condition_to_rules(condition_str: str) -> list[dict[str, Any]]:
    """Parse a hypothesis condition string back into rules_json format.

    E.g. "rsi > 65 and ema_stack == true" -> [{"indicator": "rsi", "op": ">", "value": 65}, ...]
    """
    import re
    rules: list[dict[str, Any]] = []

    parts = re.split(r"\s+and\s+", condition_str.strip(), flags=re.IGNORECASE)
    for part in parts:
        part = part.strip()
        if not part:
            continue

        match = re.match(
            r"(\w+)\s*(>=|<=|>|<|==|!=)\s*(.+)",
            part,
        )
        if not match:
            continue

        indicator = match.group(1)
        op = match.group(2)
        raw_val = match.group(3).strip()

        if raw_val.lower() == "true":
            value: Any = True
        elif raw_val.lower() == "false":
            value = False
        else:
            try:
                value = float(raw_val) if "." in raw_val else int(raw_val)
            except ValueError:
                value = raw_val

        rules.append({"indicator": indicator, "op": op, "value": value})

    return rules


def _spawn_pattern_from_hypothesis(
    db: Session,
    hyp,
    avg_ret: float,
    win_rate: float,
    user_id: int | None = None,
) -> int | None:
    """Create ScanPattern from confirmed hypothesis condition_a.

    Only spawns if:
    - avg_ret >= 2.0% and win_rate >= 55%
    - hypothesis is NOT pattern_derived (those validate existing patterns)
    - a pattern with this description doesn't already exist

    Returns the new pattern ID, or None if not spawned.
    """
    from ...models.trading import ScanPattern, TradingInsight

    if avg_ret < 2.0 or win_rate < 55:
        return None

    if hyp.origin == "pattern_derived":
        return None

    rules = _parse_condition_to_rules(hyp.condition_a)
    if not rules:
        logger.debug(f"[learning] Could not parse hypothesis condition_a: {hyp.condition_a}")
        return None

    pattern_name = f"Hyp: {hyp.description[:60]}"
    existing = db.query(ScanPattern).filter(ScanPattern.name == pattern_name).first()
    if existing:
        return None

    pattern = ScanPattern(
        name=pattern_name,
        rules_json=json.dumps({"conditions": rules}),
        origin="hypothesis_confirmed",
        confidence=min(0.85, win_rate / 100),
        active=True,
        timeframe="1d",
    )
    db.add(pattern)
    db.flush()

    insight = TradingInsight(
        user_id=user_id,
        pattern_description=f"Pattern spawned from confirmed hypothesis: {hyp.description}",
        confidence=min(0.85, win_rate / 100),
        scan_pattern_id=pattern.id,
        active=True,
    )
    db.add(insight)
    db.commit()

    logger.info(
        f"[learning] Spawned pattern '{pattern_name}' from hypothesis "
        f"(avg_ret={avg_ret:.1f}%, WR={win_rate:.0f}%)"
    )
    return pattern.id


def _intraday_bar_minutes(interval: str) -> int:
    iv = (interval or "15m").strip().lower()
    return {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "60m": 60, "90m": 90}.get(iv, 15)


def _intraday_forward_bar_counts(interval: str) -> tuple[int, int]:
    """4h and 8h forward offsets in *bars* for the given bar size."""
    mins = max(1, _intraday_bar_minutes(interval))
    bars_4h = max(1, (4 * 60) // mins)
    bars_8h = max(bars_4h + 1, (8 * 60) // mins)
    return bars_4h, bars_8h


# ── Intraday Breakout Pattern Mining (configurable bar interval) ──────

def _mine_intraday_breakout_patterns(ticker: str, interval: str = "15m") -> list[dict]:
    """Mine OHLCV (default 15m) for short-term breakout patterns (minutes to hours).

    Returns rows of indicator + pattern states with 4h and 8h forward returns.
    """
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    iv = (interval or "15m").strip().lower()
    bars_4h, bars_8h = _intraday_forward_bar_counts(iv)
    period = _MINE_FETCH_PERIOD.get(iv, "5d")
    min_df = max(80, 55 + bars_8h)
    try:
        df = fetch_ohlcv_df(ticker, period=period, interval=iv)
        if df.empty or len(df) < min_df:
            return []
        from ...config import settings as _bqs
        from .market_data import assess_ohlcv_bar_quality

        _bq = assess_ohlcv_bar_quality(df)
        if not _bq.get("ok") and getattr(_bqs, "brain_bar_quality_strict", False):
            return []
    except Exception:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd_obj = MACD(close=close)
    macd_hist = macd_obj.macd_diff()
    ema9 = EMAIndicator(close=close, window=9).ema_indicator()
    ema21 = EMAIndicator(close=close, window=21).ema_indicator()
    bb = BollingerBands(close=close, window=20, window_dev=2)
    bb_width = bb.bollinger_wband()
    adx = ADXIndicator(high=high, low=low, close=close).adx()
    atr = AverageTrueRange(high=high, low=low, close=close).average_true_range()
    stoch = StochasticOscillator(high=high, low=low, close=close)
    stoch_k = stoch.stoch()
    vol_sma = volume.rolling(20).mean()

    rows = []

    for i in range(50, len(df) - bars_8h):
        price = float(close.iloc[i])
        if price <= 0:
            continue

        ret_4h = (float(close.iloc[i + bars_4h]) - price) / price * 100
        ret_8h = (float(close.iloc[i + bars_8h]) - price) / price * 100

        bw = float(bb_width.iloc[i]) if pd.notna(bb_width.iloc[i]) else 0
        bw_pct = 0.5
        if i >= 50:
            bw_window = bb_width.iloc[i - 49:i + 1].dropna()
            if len(bw_window) > 10 and bw > 0:
                bw_pct = float((bw_window < bw).sum() / len(bw_window))

        bb_squeeze = bw_pct < 0.20

        vol_ratio = 1.0
        if pd.notna(vol_sma.iloc[i]) and float(vol_sma.iloc[i]) > 0:
            vol_ratio = float(volume.iloc[i]) / float(vol_sma.iloc[i])

        e9 = float(ema9.iloc[i]) if pd.notna(ema9.iloc[i]) else None
        e21 = float(ema21.iloc[i]) if pd.notna(ema21.iloc[i]) else None
        ema_bullish = e9 is not None and e21 is not None and price > e9 > e21

        current_range = float(high.iloc[i]) - float(low.iloc[i])
        nr7 = False
        if i >= 7 and current_range > 0:
            prev_ranges = [float(high.iloc[i - j]) - float(low.iloc[i - j]) for j in range(1, 7)]
            nr7 = current_range <= min(prev_ranges) if prev_ranges else False

        atr_val = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0
        atr_compressed = False
        if i >= 50 and atr_val > 0:
            atr_window = atr.iloc[i - 49:i + 1].dropna()
            if len(atr_window) > 10:
                atr_compressed = atr_val <= float(atr_window.quantile(0.25))

        rows.append({
            "ticker": ticker,
            "price": price,
            "ret_4h": round(ret_4h, 3),
            "ret_8h": round(ret_8h, 3),
            "rsi": float(rsi.iloc[i]) if pd.notna(rsi.iloc[i]) else 50,
            "macd_hist": float(macd_hist.iloc[i]) if pd.notna(macd_hist.iloc[i]) else 0,
            "adx": float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0,
            "stoch_k": float(stoch_k.iloc[i]) if pd.notna(stoch_k.iloc[i]) else 50,
            "bb_squeeze": bb_squeeze,
            "vol_ratio": round(vol_ratio, 2),
            "ema_bullish": ema_bullish,
            "nr7": nr7,
            "atr_compressed": atr_compressed,
            "is_crypto": ticker.endswith("-USD"),
        })
    return rows


def _mine_intraday_one_ticker(
    ticker: str, budget: BrainResourceBudget | None, interval: str = "15m",
) -> list[dict]:
    if budget is not None and not budget.try_ohlcv("intraday_compression", 1):
        return []
    try:
        return _mine_intraday_breakout_patterns(ticker, interval=interval)
    except Exception:
        if budget is not None:
            budget.record_miner_error("intraday_compression")
        return []


def _bridge_compression_scanpattern_from_miner(
    db: Session, user_id: int | None
) -> int:
    """When enabled, enqueue a prescreen-tier ScanPattern from miner-positive cycle."""
    from ...models.trading import ScanPattern, TradingInsight
    from .pattern_engine import _BUILTIN_PATTERNS

    name = "Brain miner: BB squeeze prescreen (15m)"
    if db.query(ScanPattern).filter(ScanPattern.name == name).first():
        return 0
    rules_json: str | None = None
    for bp in _BUILTIN_PATTERNS:
        if bp.get("name") == "BB Squeeze Breakout":
            rules_json = bp.get("rules_json")
            break
    if not rules_json:
        return 0
    p = ScanPattern(
        name=name,
        description="Auto-queued from intraday compression miner (prescreen tier).",
        rules_json=rules_json,
        origin="brain_discovered",
        asset_class="all",
        timeframe="15m",
        confidence=0.45,
        active=True,
        promotion_status="pending_oos",
        lifecycle_stage="candidate",
        hypothesis_family=BRAIN_HYPOTHESIS_FAMILY_COMPRESSION,
        queue_tier="prescreen",
    )
    db.add(p)
    db.flush()
    ins = TradingInsight(
        user_id=user_id,
        pattern_description=f"{name} — composable pattern backtest",
        confidence=0.45,
        scan_pattern_id=p.id,
        active=True,
        hypothesis_family=BRAIN_HYPOTHESIS_FAMILY_COMPRESSION,
    )
    db.add(ins)
    db.commit()
    from .backtest_queue import invalidate_queue_status_cache

    invalidate_queue_status_cache()
    logger.info("[learning] Miner→ScanPattern bridge id=%s (prescreen)", p.id)
    return 1


def mine_intraday_patterns(
    db: Session,
    user_id: int | None,
    budget: BrainResourceBudget | None = None,
) -> dict[str, Any]:
    """Phase-A discovery on configured intraday intervals (``brain_intraday_intervals``).

    Tags ``hypothesis_family=compression_expansion``. Does not promote ScanPatterns;
    OOS promotion remains on backtest paths only. Optional ``budget`` caps OHLCV and row volume.
    """
    from .market_data import DEFAULT_CRYPTO_TICKERS, DEFAULT_SCAN_TICKERS
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tickers = list(DEFAULT_CRYPTO_TICKERS)[:30] + list(DEFAULT_SCAN_TICKERS)[:30]

    from ...config import settings as _intraday_settings

    _workers = io_workers_low(_intraday_settings)
    discoveries = 0
    rows_total = 0
    intervals_used: list[str] = []
    _fam = BRAIN_HYPOTHESIS_FAMILY_COMPRESSION
    _insight_cache = preload_active_insights(db, user_id)

    for raw_iv in getattr(_intraday_settings, "brain_intraday_intervals", "15m").split(","):
        iv = raw_iv.strip()
        if not iv or iv == "1d":
            continue
        intervals_used.append(iv)
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=_workers) as pool:
            futs = {pool.submit(_mine_intraday_one_ticker, t, budget, iv): t for t in tickers}
            for f in as_completed(futs):
                try:
                    rows.extend(f.result())
                except Exception:
                    if budget is not None:
                        budget.record_miner_error("intraday_compression")

        if budget is not None:
            take = budget.add_miner_rows(len(rows))
            if take < len(rows):
                rows = rows[:take]

        rows_total += len(rows)
        if len(rows) < 30:
            continue

        # Hypothesis: BB squeeze -> 4h positive returns
        sq = [r for r in rows if r["bb_squeeze"]]
        no_sq = [r for r in rows if not r["bb_squeeze"]]
        if len(sq) >= 10 and len(no_sq) >= 10:
            avg_sq = sum(r["ret_4h"] for r in sq) / len(sq)
            avg_no = sum(r["ret_4h"] for r in no_sq) / len(no_sq)
            wr_sq = sum(1 for r in sq if r["ret_4h"] > 0) / len(sq)
            if avg_sq > avg_no and avg_sq > 0.1:
                w = sum(1 for r in sq if r["ret_4h"] > 0)
                sp_id = ensure_mined_scan_pattern(
                    db, f"Intraday BB Squeeze -> 4h Return [{iv}]",
                    [{"indicator": "bb_squeeze", "op": "==", "value": True}],
                    confidence=min(0.80, wr_sq), win_rate=wr_sq,
                    avg_return_pct=avg_sq, evidence_count=len(sq),
                    timeframe=iv,
                )
                save_insight(
                    db, user_id,
                    f"Intraday ({iv}): BB squeeze -> {avg_sq:+.2f}% avg 4h return, "
                    f"{wr_sq*100:.0f}%wr (n={len(sq)}) vs non-squeeze {avg_no:+.2f}%",
                    confidence=min(0.80, wr_sq),
                    wins=w, losses=len(sq) - w,
                    scan_pattern_id=sp_id,
                    hypothesis_family=_fam,
                )
                discoveries += 1
    
        # Hypothesis: BB squeeze + volume declining -> better breakouts
        sq_vol_low = [r for r in rows if r["bb_squeeze"] and r["vol_ratio"] < 0.8]
        sq_vol_high = [r for r in rows if r["bb_squeeze"] and r["vol_ratio"] > 1.5]
        if len(sq_vol_low) >= 5 and len(sq_vol_high) >= 5:
            avg_low = sum(r["ret_4h"] for r in sq_vol_low) / len(sq_vol_low)
            avg_high = sum(r["ret_4h"] for r in sq_vol_high) / len(sq_vol_high)
            w_low = sum(1 for r in sq_vol_low if r["ret_4h"] > 0)
            wr_low = w_low / len(sq_vol_low) if sq_vol_low else 0
            sp_id = ensure_mined_scan_pattern(
                db, f"Intraday Squeeze + Declining Volume [{iv}]",
                [{"indicator": "bb_squeeze", "op": "==", "value": True},
                 {"indicator": "volume_ratio", "op": "<", "value": 0.8}],
                confidence=0.5, win_rate=wr_low,
                avg_return_pct=avg_low, evidence_count=len(sq_vol_low),
                timeframe=iv,
            )
            save_insight(
                db, user_id,
                f"Intraday ({iv}): squeeze + low vol {avg_low:+.2f}%/4h "
                f"vs squeeze + high vol {avg_high:+.2f}%/4h "
                f"(n={len(sq_vol_low)}+{len(sq_vol_high)})",
                confidence=0.5,
                wins=w_low, losses=len(sq_vol_low) - w_low,
                scan_pattern_id=sp_id,
                hypothesis_family=_fam,
            )
            discoveries += 1
    
        # Hypothesis: NR7 -> expansion profitable within 8h
        nr7s = [r for r in rows if r["nr7"]]
        if len(nr7s) >= 10:
            avg_nr7 = sum(r["ret_8h"] for r in nr7s) / len(nr7s)
            wr_nr7 = sum(1 for r in nr7s if r["ret_8h"] > 0) / len(nr7s)
            w_nr7 = sum(1 for r in nr7s if r["ret_8h"] > 0)
            sp_id = ensure_mined_scan_pattern(
                db, f"Intraday NR7 -> 8h Expansion [{iv}]",
                [{"indicator": "nr7", "op": "==", "value": True}],
                confidence=min(0.75, wr_nr7), win_rate=wr_nr7,
                avg_return_pct=avg_nr7, evidence_count=len(nr7s),
                timeframe=iv,
            )
            save_insight(
                db, user_id,
                f"Intraday ({iv}): NR7 (narrow range 7) -> {avg_nr7:+.2f}% avg 8h return, "
                f"{wr_nr7*100:.0f}%wr (n={len(nr7s)})",
                confidence=min(0.75, wr_nr7),
                wins=w_nr7, losses=len(nr7s) - w_nr7,
                scan_pattern_id=sp_id,
                hypothesis_family=_fam,
            )
            discoveries += 1
    
        # Hypothesis: ATR compressed + EMA bullish -> breakout outperforms
        coiled = [r for r in rows if r["atr_compressed"] and r["ema_bullish"]]
        if len(coiled) >= 5:
            avg_coil = sum(r["ret_4h"] for r in coiled) / len(coiled)
            wr_coil = sum(1 for r in coiled if r["ret_4h"] > 0) / len(coiled) * 100
            w_coil = sum(1 for r in coiled if r["ret_4h"] > 0)
            save_insight(
                db, user_id,
                f"Intraday: ATR compressed + EMA bullish = coiled spring, "
                f"{avg_coil:+.2f}%/4h, {wr_coil:.0f}%wr (n={len(coiled)})",
                confidence=min(0.80, wr_coil / 100),
                wins=w_coil, losses=len(coiled) - w_coil,
                hypothesis_family=_fam,
            )
            discoveries += 1
    
        # Hypothesis: RSI 40-65 zone outperforms extremes in squeeze context
        sq_rsi_mid = [r for r in rows if r["bb_squeeze"] and 40 <= r["rsi"] <= 65]
        sq_rsi_ext = [r for r in rows if r["bb_squeeze"] and (r["rsi"] < 30 or r["rsi"] > 70)]
        if len(sq_rsi_mid) >= 5 and len(sq_rsi_ext) >= 5:
            avg_mid = sum(r["ret_4h"] for r in sq_rsi_mid) / len(sq_rsi_mid)
            avg_ext = sum(r["ret_4h"] for r in sq_rsi_ext) / len(sq_rsi_ext)
            w_mid = sum(1 for r in sq_rsi_mid if r["ret_4h"] > 0)
            save_insight(
                db, user_id,
                f"Intraday: squeeze + RSI 40-65 {avg_mid:+.2f}%/4h vs "
                f"squeeze + extreme RSI {avg_ext:+.2f}%/4h",
                confidence=0.55,
                wins=w_mid, losses=len(sq_rsi_mid) - w_mid,
                hypothesis_family=_fam,
            )
            discoveries += 1
    
        # Crypto vs Stock breakout comparison
        crypto_rows = [r for r in rows if r["is_crypto"] and r["bb_squeeze"]]
        stock_rows = [r for r in rows if not r["is_crypto"] and r["bb_squeeze"]]
        if len(crypto_rows) >= 10 and len(stock_rows) >= 10:
            avg_crypto = sum(r["ret_4h"] for r in crypto_rows) / len(crypto_rows)
            avg_stock = sum(r["ret_4h"] for r in stock_rows) / len(stock_rows)
            save_insight(
                db, user_id,
                f"Intraday squeeze: crypto {avg_crypto:+.2f}%/4h vs "
                f"stocks {avg_stock:+.2f}%/4h (n={len(crypto_rows)}+{len(stock_rows)})",
                confidence=0.5,
                hypothesis_family=_fam,
            )
            discoveries += 1

    if not intervals_used:
        return {
            "tested": 0,
            "note": "no intraday intervals configured (expected comma list in brain_intraday_intervals)",
            "hypothesis_family": BRAIN_HYPOTHESIS_FAMILY_COMPRESSION,
        }

    bridge_n = 0
    from ...config import settings as _s_bridge
    if (
        getattr(_s_bridge, "brain_miner_scanpattern_bridge_enabled", False)
        and discoveries > 0
        and (budget is None or budget.try_pattern_inject())
    ):
        try:
            bridge_n = _bridge_compression_scanpattern_from_miner(db, user_id)
        except Exception as e:
            logger.warning("[learning] miner ScanPattern bridge failed: %s", e)

    log_learning_event(
        db, user_id, "intraday_pattern_mining",
        f"Mined {rows_total} intraday bars ({','.join(intervals_used) or 'n/a'}) from {len(tickers)} tickers, "
        f"{discoveries} breakout pattern discoveries [{_fam}]"
        + (f", bridge={bridge_n}" if bridge_n else ""),
    )

    return {
        "rows_mined": rows_total,
        "tickers": len(tickers),
        "intervals": intervals_used,
        "discoveries": discoveries,
        "hypothesis_family": _fam,
        "scanpattern_bridge_created": bridge_n,
    }


def _mine_high_vol_one_ticker(
    ticker: str, budget: BrainResourceBudget | None, interval: str = "15m",
) -> list[dict]:
    """Intraday rows where volatility is already expanded (ATR or BB width in top quartile vs 50-bar window)."""
    from ta.momentum import RSIIndicator
    from ta.trend import MACD, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    if budget is not None and not budget.try_ohlcv("high_vol_regime", 1):
        return []
    iv = (interval or "15m").strip().lower()
    bars_4h, bars_8h = _intraday_forward_bar_counts(iv)
    period = _MINE_FETCH_PERIOD.get(iv, "5d")
    min_df = max(80, 55 + bars_8h)
    try:
        df = fetch_ohlcv_df(ticker, period=period, interval=iv)
        if df.empty or len(df) < min_df:
            return []
    except Exception:
        if budget is not None:
            budget.record_miner_error("high_vol_regime")
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd_obj = MACD(close=close)
    macd_hist = macd_obj.macd_diff()
    ema9 = EMAIndicator(close=close, window=9).ema_indicator()
    ema21 = EMAIndicator(close=close, window=21).ema_indicator()
    bb = BollingerBands(close=close, window=20, window_dev=2)
    bb_width = bb.bollinger_wband()
    adx = ADXIndicator(high=high, low=low, close=close).adx()
    atr = AverageTrueRange(high=high, low=low, close=close).average_true_range()
    vol_sma = volume.rolling(20).mean()

    rows = []

    for i in range(50, len(df) - bars_8h):
        price = float(close.iloc[i])
        if price <= 0:
            continue

        ret_4h = (float(close.iloc[i + bars_4h]) - price) / price * 100
        ret_8h = (float(close.iloc[i + bars_8h]) - price) / price * 100

        bw = float(bb_width.iloc[i]) if pd.notna(bb_width.iloc[i]) else 0
        atr_val = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0

        atr_high = False
        bb_high = False
        if i >= 50 and atr_val > 0:
            atr_window = atr.iloc[i - 49:i + 1].dropna()
            if len(atr_window) > 10:
                atr_high = atr_val >= float(atr_window.quantile(0.75))
        if i >= 50 and bw > 0:
            bw_window = bb_width.iloc[i - 49:i + 1].dropna()
            if len(bw_window) > 10:
                bb_high = bw >= float(bw_window.quantile(0.75))

        high_vol_regime = atr_high or bb_high

        vol_ratio = 1.0
        if pd.notna(vol_sma.iloc[i]) and float(vol_sma.iloc[i]) > 0:
            vol_ratio = float(volume.iloc[i]) / float(vol_sma.iloc[i])

        e9 = float(ema9.iloc[i]) if pd.notna(ema9.iloc[i]) else None
        e21 = float(ema21.iloc[i]) if pd.notna(ema21.iloc[i]) else None
        ema_bullish = e9 is not None and e21 is not None and price > e9 > e21

        rows.append({
            "ticker": ticker,
            "price": price,
            "ret_4h": round(ret_4h, 3),
            "ret_8h": round(ret_8h, 3),
            "rsi": float(rsi.iloc[i]) if pd.notna(rsi.iloc[i]) else 50,
            "macd_hist": float(macd_hist.iloc[i]) if pd.notna(macd_hist.iloc[i]) else 0,
            "adx": float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0,
            "high_vol_regime": high_vol_regime,
            "vol_ratio": round(vol_ratio, 2),
            "ema_bullish": ema_bullish,
            "is_crypto": True,
        })
    return rows


def mine_high_vol_regime_patterns(
    db: Session,
    user_id: int | None,
    budget: BrainResourceBudget | None = None,
) -> dict[str, Any]:
    """Phase-A discovery: crypto intraday bars in *expanded* vol (distinct from compression miner)."""
    from ...config import settings
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not getattr(settings, "brain_high_vol_miner_enabled", True):
        return {"discoveries": 0, "rows_mined": 0, "tickers": 0, "skipped": True}

    tickers = [t for t in DEFAULT_CRYPTO_TICKERS if str(t).endswith("-USD")][:30]
    _hv_workers = io_workers_low(settings)
    discoveries = 0
    _fam = BRAIN_HYPOTHESIS_FAMILY_HIGH_VOL
    rows_total = 0
    intervals_used: list[str] = []

    for raw_iv in getattr(settings, "brain_intraday_intervals", "15m").split(","):
        iv = raw_iv.strip()
        if not iv or iv == "1d":
            continue
        intervals_used.append(iv)
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=_hv_workers) as pool:
            futs = {pool.submit(_mine_high_vol_one_ticker, t, budget, iv): t for t in tickers}
            for f in as_completed(futs):
                try:
                    rows.extend(f.result())
                except Exception:
                    if budget is not None:
                        budget.record_miner_error("high_vol_regime")

        if budget is not None:
            take = budget.add_miner_rows(len(rows))
            if take < len(rows):
                rows = rows[:take]

        rows_total += len(rows)
        if len(rows) < 30:
            continue

        hv = [r for r in rows if r["high_vol_regime"]]
        lv = [r for r in rows if not r["high_vol_regime"]]
        if len(hv) >= 10 and len(lv) >= 10:
            avg_h = sum(r["ret_4h"] for r in hv) / len(hv)
            avg_l = sum(r["ret_4h"] for r in lv) / len(lv)
            wr_h = sum(1 for r in hv if r["ret_4h"] > 0) / len(hv) * 100
            w_h = sum(1 for r in hv if r["ret_4h"] > 0)
            if abs(avg_h - avg_l) > 0.05:
                save_insight(
                    db, user_id,
                    f"Crypto high-vol regime ({iv}): expanded ATR/BB vs calm — "
                    f"{avg_h:+.2f}%/4h ({wr_h:.0f}%wr, n={len(hv)}) vs {avg_l:+.2f}%/4h (n={len(lv)})",
                    confidence=min(0.72, max(0.35, wr_h / 100)),
                    wins=w_h, losses=len(hv) - w_h,
                    hypothesis_family=_fam,
                )
                discoveries += 1

        hv_bull = [r for r in hv if r["ema_bullish"]]
        hv_bear = [r for r in hv if not r["ema_bullish"]]
        if len(hv_bull) >= 8 and len(hv_bear) >= 8:
            ab = sum(r["ret_4h"] for r in hv_bull) / len(hv_bull)
            ae = sum(r["ret_4h"] for r in hv_bear) / len(hv_bear)
            w_b = sum(1 for r in hv_bull if r["ret_4h"] > 0)
            if abs(ab - ae) > 0.05:
                save_insight(
                    db, user_id,
                    f"Crypto high-vol {iv}: EMA bullish stack {ab:+.2f}%/4h vs not "
                    f"{ae:+.2f}%/4h (n={len(hv_bull)}+{len(hv_bear)})",
                    confidence=0.55,
                    wins=w_b, losses=len(hv_bull) - w_b,
                    hypothesis_family=_fam,
                )
                discoveries += 1

    if not intervals_used:
        return {
            "discoveries": 0,
            "rows_mined": 0,
            "tickers": len(tickers),
            "skipped": True,
            "note": "no intraday intervals configured",
            "hypothesis_family": BRAIN_HYPOTHESIS_FAMILY_HIGH_VOL,
        }

    log_learning_event(
        db, user_id, "high_vol_pattern_mining",
        f"Mined {rows_total} crypto bars ({','.join(intervals_used)}), "
        f"{discoveries} high-vol regime insights [{_fam}]",
    )

    return {
        "rows_mined": rows_total,
        "tickers": len(tickers),
        "intervals": intervals_used,
        "discoveries": discoveries,
        "hypothesis_family": _fam,
    }


# ── Breakout Outcome Learning ──────────────────────────────────────────

def learn_from_breakout_outcomes(db: Session, user_id: int | None) -> dict[str, Any]:
    """Compute per-pattern win rates from resolved BreakoutAlert outcomes
    and feed them back into both TradingInsight and ScanPattern records.
    
    Updates:
    - TradingInsight: win_count, loss_count, confidence
    - ScanPattern: win_rate, avg_return_pct (real trade feedback)
    """
    from ...models.trading import BreakoutAlert, ScanPattern

    try:
        cutoff = datetime.utcnow() - timedelta(days=180)
        alert_q = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.outcome_checked_at >= cutoff,
        )
        if user_id is not None:
            alert_q = alert_q.filter(BreakoutAlert.user_id == user_id)
        resolved = alert_q.order_by(BreakoutAlert.outcome_checked_at.desc()).limit(500).all()
    except Exception:
        return {"patterns_learned": 0}

    if len(resolved) < 3:
        return {"patterns_learned": 0, "note": "insufficient resolved alerts"}

    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    pattern_outcomes: dict[int, list] = defaultdict(list)  # scan_pattern_id -> outcomes
    
    for alert in resolved:
        key = f"{alert.asset_type}|{alert.alert_tier}"
        groups[key].append(alert)

        _spid = getattr(alert, "scan_pattern_id", None)
        if _spid:
            pattern_outcomes[_spid].append({
                "outcome": alert.outcome,
                "gain_pct": alert.max_gain_pct or 0,
                "drawdown_pct": alert.max_drawdown_pct or 0,
            })

    patterns_created = 0
    patterns_updated = 0
    
    # Update ScanPattern stats directly with real trade outcomes
    for pattern_id, outcomes in pattern_outcomes.items():
        if len(outcomes) < 3:
            continue
        
        pattern = db.query(ScanPattern).get(pattern_id)
        if not pattern:
            continue
        if (
            user_id is not None
            and pattern.user_id is not None
            and pattern.user_id != user_id
        ):
            continue

        winners = sum(1 for o in outcomes if o["outcome"] == "winner")
        total = len(outcomes)
        new_win_rate = winners / total
        
        avg_gain = sum(o["gain_pct"] for o in outcomes) / total
        
        # Exponential moving average to blend new data with existing
        old_wr = pattern.win_rate or 0.5
        old_ret = pattern.avg_return_pct or 0.0
        
        # Blend factor based on sample size (more data = more trust in new stats)
        blend = min(0.8, total / 50)  # up to 80% weight for new data
        
        pattern.win_rate = round(old_wr * (1 - blend) + new_win_rate * blend, 4)
        pattern.avg_return_pct = round(old_ret * (1 - blend) + avg_gain * blend, 2)
        actual_trade_count = db.query(func.count(Trade.id)).filter(
            Trade.scan_pattern_id == pattern.id
        ).scalar() or 0
        pattern.trade_count = actual_trade_count
        pattern.updated_at = datetime.utcnow()
        
        patterns_updated += 1
        
        logger.info(
            "[learning] Updated ScanPattern '%s' (id=%d) from real trades: "
            "win_rate=%.1f%% (was %.1f%%), avg_return=%.2f%% (was %.2f%%), n=%d",
            pattern.name, pattern.id,
            pattern.win_rate * 100, old_wr * 100,
            pattern.avg_return_pct, old_ret,
            actual_trade_count,
        )

    # Also create/update TradingInsight summaries by asset_type/tier (existing logic)
    for key, alerts in groups.items():
        if len(alerts) < 3:
            continue
        asset_type, tier = key.split("|", 1)
        winners = sum(1 for a in alerts if a.outcome == "winner")
        fakeouts = sum(1 for a in alerts if a.outcome == "fakeout")
        total = len(alerts)
        win_rate = winners / total * 100
        avg_gain = sum(
            (a.max_gain_pct or 0) for a in alerts
        ) / total
        avg_dd = sum(
            (a.max_drawdown_pct or 0) for a in alerts
        ) / total

        desc = (
            f"Breakout outcome: {asset_type} {tier} — "
            f"{win_rate:.0f}% win rate ({winners}/{total}), "
            f"avg peak gain {avg_gain:+.1f}%, avg max DD {avg_dd:+.1f}%, "
            f"fakeout rate {fakeouts/total*100:.0f}%"
        )
        confidence = min(0.90, win_rate / 100)

        existing = db.query(TradingInsight).filter(
            TradingInsight.user_id == user_id,
            TradingInsight.pattern_description.like(f"%{asset_type} {tier}%"),
            TradingInsight.pattern_description.like("Breakout outcome:%"),
            TradingInsight.active.is_(True),
        ).first()

        if existing:
            existing.confidence = round(
                existing.confidence * 0.4 + confidence * 0.6, 3
            )
            existing.evidence_count = total
            existing.win_count = winners
            existing.loss_count = total - winners
            existing.pattern_description = desc
            existing.last_seen = datetime.utcnow()
        else:
            save_insight(db, user_id, desc, confidence=confidence,
                         wins=winners, losses=total - winners)

        patterns_created += 1

        log_learning_event(
            db, user_id, "breakout_outcome_learning",
            f"{asset_type} {tier}: {win_rate:.0f}%wr ({total} alerts), "
            f"avg gain {avg_gain:+.1f}%, fakeout {fakeouts/total*100:.0f}%",
        )

    return {
        "patterns_learned": patterns_created,
        "scan_patterns_updated": patterns_updated,
        "total_resolved": len(resolved),
    }


# ── Closed-Trade → ScanPattern Feedback ────────────────────────────────

def update_pattern_stats_from_closed_trades(db: Session, user_id: int | None) -> dict[str, Any]:
    """Aggregate win/loss/return from closed trades and update their linked ScanPattern.

    Only considers trades closed in the last 180 days that have a ``scan_pattern_id``.
    Uses exponential blending so new data gradually outweighs stale stats.
    """
    from ...models.trading import ScanPattern, Trade

    cutoff = datetime.utcnow() - timedelta(days=180)
    try:
        closed_q = (
            db.query(
                Trade.scan_pattern_id,
                Trade.pnl,
                Trade.entry_price,
                Trade.exit_price,
            )
            .filter(
                Trade.status == "closed",
                Trade.scan_pattern_id.isnot(None),
                Trade.exit_date >= cutoff,
            )
        )
        if user_id is not None:
            closed_q = closed_q.filter(Trade.user_id == user_id)
        closed = closed_q.all()
    except Exception:
        return {"patterns_updated": 0}

    if not closed:
        return {"patterns_updated": 0, "note": "no closed trades with pattern linkage"}

    from collections import defaultdict

    buckets: dict[int, list[dict]] = defaultdict(list)
    for row in closed:
        if row.scan_pattern_id is None:
            continue
        ret_pct = 0.0
        if row.entry_price and row.exit_price and row.entry_price > 0:
            ret_pct = (row.exit_price - row.entry_price) / row.entry_price * 100
        buckets[row.scan_pattern_id].append({
            "win": (row.pnl or 0) > 0,
            "return_pct": ret_pct,
        })

    updated = 0
    for pattern_id, trades in buckets.items():
        if len(trades) < 2:
            continue
        pattern = db.get(ScanPattern, pattern_id)
        if not pattern:
            continue
        if (
            user_id is not None
            and pattern.user_id is not None
            and pattern.user_id != user_id
        ):
            continue

        wins = sum(1 for t in trades if t["win"])
        n = len(trades)
        new_wr = wins / n
        new_ret = sum(t["return_pct"] for t in trades) / n

        blend = min(0.8, n / 30)
        old_wr = pattern.win_rate or 0.5
        old_ret = pattern.avg_return_pct or 0.0

        pattern.win_rate = round(old_wr * (1 - blend) + new_wr * blend, 4)
        pattern.avg_return_pct = round(old_ret * (1 - blend) + new_ret * blend, 2)
        actual_trade_count = db.query(func.count(Trade.id)).filter(
            Trade.scan_pattern_id == pattern.id
        ).scalar() or 0
        pattern.trade_count = actual_trade_count
        pattern.updated_at = datetime.utcnow()
        updated += 1

        logger.info(
            "[learning] Trade feedback → ScanPattern '%s' (id=%d): "
            "wr=%.1f%% (was %.1f%%), avg_ret=%.2f%% (was %.2f%%), n=%d",
            pattern.name, pattern.id,
            pattern.win_rate * 100, old_wr * 100,
            pattern.avg_return_pct, old_ret,
            actual_trade_count,
        )

    return {"patterns_updated": updated, "trades_processed": len(closed)}


# ── Pattern Monitor Decision Learning ────────────────────────────────

def learn_from_monitor_decisions(db: Session, user_id: int | None) -> dict[str, Any]:
    """Aggregate pattern-monitor decision outcomes and evolve adaptive thresholds.

    Reads resolved ``PatternMonitorDecision`` rows (``was_beneficial`` is not null),
    computes benefit rates per action type, and nudges the adaptive weight keys
    ``monitor_health_healthy``, ``monitor_health_weakening``, and
    ``monitor_llm_confidence_min`` accordingly.
    """
    from ...models.trading import PatternMonitorDecision
    from .scanner import get_adaptive_weight, _adaptive_weights, _weights_lock

    cutoff = datetime.utcnow() - timedelta(days=90)
    rows = (
        db.query(PatternMonitorDecision)
        .filter(
            PatternMonitorDecision.was_beneficial.isnot(None),
            PatternMonitorDecision.created_at >= cutoff,
        )
        .all()
    )
    if not rows:
        return {"decisions_reviewed": 0}

    by_action: dict[str, list[bool]] = {}
    for r in rows:
        by_action.setdefault(r.action, []).append(bool(r.was_beneficial))

    total = len(rows)
    overall_benefit = sum(1 for r in rows if r.was_beneficial) / total if total else 0

    nudge = 0.02
    with _weights_lock:
        # If tighten_stop decisions are mostly NOT beneficial, raise the
        # weakening threshold so we trigger less often.
        tighten_rate = (
            sum(by_action.get("tighten_stop", [])) / len(by_action["tighten_stop"])
            if by_action.get("tighten_stop") else 0.5
        )
        if tighten_rate < 0.4:
            _adaptive_weights["monitor_health_weakening"] = max(
                0.3, _adaptive_weights.get("monitor_health_weakening", 0.5) - nudge
            )
        elif tighten_rate > 0.7:
            _adaptive_weights["monitor_health_weakening"] = min(
                0.7, _adaptive_weights.get("monitor_health_weakening", 0.5) + nudge
            )

        if overall_benefit < 0.4:
            _adaptive_weights["monitor_llm_confidence_min"] = min(
                0.8, _adaptive_weights.get("monitor_llm_confidence_min", 0.5) + nudge
            )
        elif overall_benefit > 0.7:
            _adaptive_weights["monitor_llm_confidence_min"] = max(
                0.3, _adaptive_weights.get("monitor_llm_confidence_min", 0.5) - nudge
            )

    # ── Trade plan signal predictiveness ──
    signal_stats = _analyze_trade_plan_signals(rows)
    vitals_hist_summary = _vitals_history_learning_summary(db)

    # ── Rules engine aggregation (self-learning) ──
    rules_stats: dict[str, Any] = {}
    try:
        from .monitor_rules_engine import (
            aggregate_decision_outcomes,
            update_mesh_node_state,
            update_plan_accuracy,
        )

        rules_stats = aggregate_decision_outcomes(db)
        logger.info("[learning] Rules engine: %s", rules_stats)

        # Update neural mesh node state
        update_mesh_node_state(db, "nm_monitor_rules_learner", {
            "rules_count": rules_stats.get("rules_updated", 0),
            "total_samples": rules_stats.get("rows_processed", 0),
            "last_aggregation_ts": datetime.utcnow().isoformat(),
        })

        # Plan accuracy tracking from dual-path decisions
        _update_plan_accuracy_from_decisions(db, rows)

    except Exception:
        logger.debug("[learning] Rules engine aggregation failed", exc_info=True)

    logger.info(
        "[learning] Monitor decisions: %d reviewed, overall benefit %.0f%%, "
        "tighten benefit %.0f%%, plan signals analyzed: %d, rules updated: %d",
        total, overall_benefit * 100, tighten_rate * 100,
        signal_stats.get("signals_analyzed", 0),
        rules_stats.get("rules_updated", 0),
    )
    return {
        "decisions_reviewed": total,
        "overall_benefit_rate": round(overall_benefit, 3),
        "by_action": {k: round(sum(v) / len(v), 3) for k, v in by_action.items()},
        "plan_signal_stats": signal_stats,
        "vitals_history_summary": vitals_hist_summary,
        "rules_engine": rules_stats,
    }


def _analyze_trade_plan_signals(rows: list) -> dict[str, Any]:
    """Analyze which trade plan signals (invalidations, caution changes) were
    predictive of beneficial/harmful outcomes.

    Returns aggregated stats that can be fed back into the plan extractor
    system prompt for calibration.
    """
    signal_outcomes: dict[str, list[bool]] = {}
    total_signals = 0

    for r in rows:
        snap = r.conditions_snapshot
        if not isinstance(snap, dict):
            continue
        tp = snap.get("trade_plan", {})
        if not tp:
            continue

        beneficial = bool(r.was_beneficial)

        for inv in tp.get("invalidations_triggered", []):
            key = f"inv:{inv.get('indicator', 'unknown')}"
            signal_outcomes.setdefault(key, []).append(beneficial)
            total_signals += 1

        for cc in tp.get("caution_signals_changed", []):
            direction = cc.get("direction", "changed")
            key = f"caution:{cc.get('indicator', 'unknown')}:{direction}"
            signal_outcomes.setdefault(key, []).append(beneficial)
            total_signals += 1

        vit = snap.get("vitals") or {}
        try:
            ch = vit.get("composite_health")
            if ch is not None:
                key = f"vitals_composite:{round(float(ch), 2)}"
                signal_outcomes.setdefault(key, []).append(beneficial)
                total_signals += 1
        except (TypeError, ValueError):
            pass

        vd = snap.get("vitals_degradation") or {}
        if vd.get("degraded_3plus"):
            signal_outcomes.setdefault("vitals:degraded_3plus", []).append(beneficial)
            total_signals += 1
        if vd.get("mom_urgent"):
            signal_outcomes.setdefault("vitals:mom_urgent", []).append(beneficial)
            total_signals += 1

    predictive_signals = {}
    for key, outcomes in signal_outcomes.items():
        if len(outcomes) < 3:
            continue
        rate = sum(outcomes) / len(outcomes)
        predictive_signals[key] = {
            "count": len(outcomes),
            "benefit_rate": round(rate, 3),
            "predictive": rate > 0.6 or rate < 0.3,
        }

    return {
        "signals_analyzed": total_signals,
        "predictive_signals": predictive_signals,
    }


def _vitals_history_learning_summary(db: Session, *, lookback_days: int = 90) -> dict[str, Any]:
    """Aggregate per-trade vitals history rows for degradation vs outcomes (lightweight)."""
    from sqlalchemy import func

    from ...models.trading import SetupVitalsHistory, Trade

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    try:
        n_hist = (
            db.query(func.count(SetupVitalsHistory.id))
            .filter(SetupVitalsHistory.created_at >= cutoff)
            .scalar()
            or 0
        )
    except Exception:
        return {"rows": 0, "note": "setup_vitals_history unavailable"}

    degraded_benefit: list[bool] = []
    try:
        rows = (
            db.query(SetupVitalsHistory, Trade)
            .join(Trade, SetupVitalsHistory.trade_id == Trade.id)
            .filter(SetupVitalsHistory.created_at >= cutoff)
            .filter(Trade.status == "closed")
            .limit(5000)
            .all()
        )
        for hist, tr in rows:
            flags = hist.degradation_flags or {}
            if not isinstance(flags, dict) or not flags.get("degraded_3plus"):
                continue
            # last decision benefit for trade — approximate with PnL
            won = (tr.pnl or 0) > 0
            degraded_benefit.append(won)
    except Exception:
        pass

    rate = sum(degraded_benefit) / len(degraded_benefit) if degraded_benefit else None
    return {
        "history_rows_window": int(n_hist),
        "degraded_3plus_closed_trades_sampled": len(degraded_benefit),
        "win_rate_when_had_degraded_3plus": round(rate, 3) if rate is not None else None,
    }


def _update_plan_accuracy_from_decisions(db: Session, rows: list) -> None:
    """Compare LLM vs mechanical decisions in resolved rows and update plan accuracy."""
    from .monitor_rules_engine import update_plan_accuracy, update_mesh_node_state
    from ...models.trading import ScanPattern

    stats = {"tracked": 0, "agreed": 0}
    for r in rows:
        if not r.mechanical_action or not r.action:
            continue
        if not r.scan_pattern_id:
            continue

        sp = db.query(ScanPattern).filter(ScanPattern.id == r.scan_pattern_id).first()
        if not sp:
            continue

        ptype = (sp.name or f"pattern_{sp.id}")[:120]
        rules = sp.rules_json
        if isinstance(rules, str):
            try:
                import json as _json
                rules = _json.loads(rules)
            except Exception:
                rules = {}
        n_conds = len((rules or {}).get("conditions", []))
        complexity_band = "simple" if n_conds < 5 else "complex"

        agreed = r.mechanical_action == r.action
        llm_correct = bool(r.was_beneficial) and r.decision_source == "llm"
        mech_correct = bool(r.was_beneficial) and r.decision_source == "mechanical"
        # If both sources agree and decision was beneficial, both are correct
        if agreed and r.was_beneficial:
            llm_correct = True
            mech_correct = True

        update_plan_accuracy(
            db, ptype, complexity_band,
            llm_correct=llm_correct,
            mechanical_correct=mech_correct,
            agreed=agreed,
        )
        stats["tracked"] += 1
        if agreed:
            stats["agreed"] += 1

    if stats["tracked"]:
        try:
            update_mesh_node_state(db, "nm_plan_accuracy_tracker", {
                "decisions_tracked": stats["tracked"],
                "agreement_count": stats["agreed"],
                "agreement_rate": round(stats["agreed"] / stats["tracked"], 3),
            })
        except Exception:
            pass


# ── Exit Optimization Learning ────────────────────────────────────────

def learn_exit_optimization(db: Session, user_id: int | None) -> dict[str, Any]:
    """Analyze time-to-peak, time-to-stop, and trailing stop data to
    recommend ATR multiplier adjustments for stops and targets."""
    from ...models.trading import BreakoutAlert

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.time_to_peak_hours.isnot(None),
        ).all()
    except Exception:
        return {"adjustments": 0}

    if len(resolved) < 5:
        return {"adjustments": 0, "note": "insufficient data"}

    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for a in resolved:
        groups[f"{a.asset_type}|{a.alert_tier}"].append(a)

    adjustments = 0
    for key, alerts in groups.items():
        if len(alerts) < 5:
            continue
        asset_type, tier = key.split("|", 1)

        peaks = [a.time_to_peak_hours for a in alerts if a.time_to_peak_hours is not None]
        stops = [a.time_to_stop_hours for a in alerts if a.time_to_stop_hours is not None]
        winners = [a for a in alerts if a.outcome == "winner"]
        losers = [a for a in alerts if a.outcome == "loser"]

        if peaks:
            median_peak = sorted(peaks)[len(peaks) // 2]

            if median_peak < 2 and asset_type == "crypto":
                desc = (
                    f"Exit optimization: {asset_type} {tier} — median time-to-peak "
                    f"is {median_peak:.1f}h. Consider tighter crypto_bo_target_atr_mult "
                    f"targets for faster profit-taking."
                )
                save_insight(db, user_id, desc, confidence=0.6,
                             wins=len(winners), losses=len(losers))
                adjustments += 1

        if stops and losers:
            fast_stops = sum(1 for s in stops if s < 1)
            if fast_stops / len(stops) > 0.5:
                prefix = "crypto_bo" if asset_type == "crypto" else "bo"
                desc = (
                    f"Exit optimization: {asset_type} {tier} — {fast_stops}/{len(stops)} "
                    f"alerts hit stop within 1h. Consider widening {prefix}_stop_atr_mult."
                )
                save_insight(db, user_id, desc, confidence=0.55,
                             wins=len(winners), losses=len(losers))
                adjustments += 1

        # Optimal exit vs actual outcome
        opt_exits = [a.optimal_exit_pct for a in alerts if a.optimal_exit_pct is not None]
        actual_gains = [a.max_gain_pct for a in winners if a.max_gain_pct is not None]
        if opt_exits and actual_gains:
            avg_opt = sum(opt_exits) / len(opt_exits)
            avg_actual = sum(actual_gains) / len(actual_gains)
            if avg_opt > avg_actual * 0.8 and avg_opt > 1.0:
                desc = (
                    f"Exit optimization: {asset_type} {tier} — trailing stop would "
                    f"capture avg {avg_opt:.1f}% vs actual avg peak {avg_actual:.1f}%. "
                    f"Trailing stop strategy is recommended."
                )
                save_insight(db, user_id, desc, confidence=0.65,
                             wins=len(winners), losses=len(alerts) - len(winners))
                adjustments += 1

    if adjustments:
        log_learning_event(
            db, user_id, "exit_optimization",
            f"Generated {adjustments} exit optimization insights from {len(resolved)} alerts",
        )

    return {"adjustments": adjustments, "alerts_analyzed": len(resolved)}


# ── Fakeout Pattern Mining ────────────────────────────────────────────

def mine_fakeout_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """Mine indicator states that commonly precede fakeout outcomes and
    create insights so the brain learns to penalize them."""
    import json as _json
    from ...models.trading import BreakoutAlert

    try:
        fakeouts = db.query(BreakoutAlert).filter(BreakoutAlert.outcome == "fakeout").all()
        winners = db.query(BreakoutAlert).filter(BreakoutAlert.outcome == "winner").all()
    except Exception:
        return {"patterns_found": 0}

    if len(fakeouts) < 3 or len(winners) < 3:
        return {"patterns_found": 0, "note": "insufficient fakeout/winner data"}

    def _parse_indicators(alerts):
        parsed = []
        for a in alerts:
            try:
                ind = _json.loads(a.indicator_snapshot) if a.indicator_snapshot else {}
                parsed.append(ind)
            except Exception:
                logger.debug("[learning] mine_fakeout_patterns: non-critical operation failed", exc_info=True)
        return parsed

    fakeout_inds = _parse_indicators(fakeouts)
    winner_inds = _parse_indicators(winners)

    patterns_found = 0

    def _check_condition(inds, condition_fn):
        return sum(1 for i in inds if condition_fn(i)) / max(len(inds), 1) * 100

    conditions = [
        ("RSI > 65 at alert", lambda i: (i.get("rsi") or 50) > 65, "overbought squeeze fakeout"),
        ("RVOL < 1.0", lambda i: (i.get("rvol") or 1.0) < 1.0, "low volume fakeout"),
        ("ADX > 30", lambda i: (i.get("adx") or 0) > 30, "trending squeeze fakeout"),
        ("BB width narrow (<0.02)", lambda i: (i.get("bb_width") or 1.0) < 0.02, "extremely narrow range fakeout"),
    ]

    for label, cond, keyword in conditions:
        fakeout_pct = _check_condition(fakeout_inds, cond)
        winner_pct = _check_condition(winner_inds, cond)

        if fakeout_pct > winner_pct + 15 and fakeout_pct > 30:
            desc = (
                f"Fakeout pattern: {label} occurs in {fakeout_pct:.0f}% of fakeouts "
                f"vs {winner_pct:.0f}% of winners — {keyword}"
            )
            save_insight(db, user_id, desc, confidence=0.55,
                         wins=len(winner_inds), losses=len(fakeout_inds))
            patterns_found += 1

    # Signal combination analysis
    from collections import Counter
    fakeout_sig_combos: Counter = Counter()
    winner_sig_combos: Counter = Counter()

    for a in fakeouts:
        try:
            sigs = _json.loads(a.signals_snapshot) if a.signals_snapshot else []
            for i in range(len(sigs)):
                for j in range(i + 1, min(i + 3, len(sigs))):
                    combo = tuple(sorted([sigs[i][:30], sigs[j][:30]]))
                    fakeout_sig_combos[combo] += 1
        except Exception:
            logger.debug("[learning] mine_fakeout_patterns: non-critical operation failed", exc_info=True)

    for a in winners:
        try:
            sigs = _json.loads(a.signals_snapshot) if a.signals_snapshot else []
            for i in range(len(sigs)):
                for j in range(i + 1, min(i + 3, len(sigs))):
                    combo = tuple(sorted([sigs[i][:30], sigs[j][:30]]))
                    winner_sig_combos[combo] += 1
        except Exception:
            logger.debug("[learning] mine_fakeout_patterns: non-critical operation failed", exc_info=True)

    for combo, count in fakeout_sig_combos.most_common(5):
        if count < 3:
            continue
        fakeout_rate = count / max(len(fakeouts), 1) * 100
        winner_rate = winner_sig_combos.get(combo, 0) / max(len(winners), 1) * 100
        if fakeout_rate > winner_rate * 1.5 and fakeout_rate > 20:
            desc = (
                f"Fakeout combo: '{combo[0]}' + '{combo[1]}' — "
                f"{fakeout_rate:.0f}% fakeout rate vs {winner_rate:.0f}% winner rate"
            )
            save_insight(db, user_id, desc, confidence=0.5,
                         losses=count)
            patterns_found += 1

    if patterns_found:
        log_learning_event(
            db, user_id, "fakeout_mining",
            f"Discovered {patterns_found} fakeout patterns from {len(fakeouts)} fakeouts",
        )

    return {"patterns_found": patterns_found, "fakeouts_analyzed": len(fakeouts)}


# ── Position Sizing Feedback Loop ─────────────────────────────────────

def tune_position_sizing(db: Session, user_id: int | None) -> dict[str, Any]:
    """Link breakout outcome stats to position sizing adaptive weights."""
    from ...models.trading import BreakoutAlert
    from collections import defaultdict

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
        ).all()
    except Exception:
        return {"adjustments": 0}

    if len(resolved) < 10:
        return {"adjustments": 0, "note": "insufficient data"}

    groups: dict[str, list] = defaultdict(list)
    for a in resolved:
        regime = a.regime_at_alert or "unknown"
        groups[f"{a.asset_type}|{regime}"].append(a)

    adjustments = 0
    for key, alerts in groups.items():
        if len(alerts) < 5:
            continue
        asset_type, regime = key.split("|", 1)
        winners = [a for a in alerts if a.outcome == "winner"]
        fakeouts = [a for a in alerts if a.outcome == "fakeout"]
        losers = [a for a in alerts if a.outcome == "loser"]
        win_rate = len(winners) / len(alerts) * 100
        fakeout_rate = len(fakeouts) / len(alerts) * 100

        if asset_type == "crypto" and regime == "risk_off" and fakeout_rate > 60:
            desc = (
                f"Position sizing: crypto in risk_off has {fakeout_rate:.0f}% fakeout rate "
                f"({len(alerts)} alerts) — reduce pos_speculative_mult"
            )
            save_insight(db, user_id, desc, confidence=0.6,
                         wins=len(winners), losses=len(losers) + len(fakeouts))
            adjustments += 1

        if regime == "risk_on" and win_rate > 70:
            desc = (
                f"Position sizing: {asset_type} in risk_on has {win_rate:.0f}% win rate "
                f"({len(alerts)} alerts) — can increase pos_regime_risk_off_mult towards 1.0"
            )
            save_insight(db, user_id, desc, confidence=0.6,
                         wins=len(winners), losses=len(losers) + len(fakeouts))
            adjustments += 1

        # Profit factor per tier
        avg_winner_gain = sum(a.max_gain_pct or 0 for a in winners) / max(len(winners), 1)
        avg_loser_loss = abs(sum(a.max_drawdown_pct or 0 for a in losers) / max(len(losers), 1))
        if avg_loser_loss > 0:
            profit_factor = avg_winner_gain / avg_loser_loss
            if profit_factor > 2.0 and len(alerts) >= 10:
                desc = (
                    f"Position sizing: {asset_type} {regime} has profit factor "
                    f"{profit_factor:.1f}x — consider larger pos_pct_hard_cap for this regime"
                )
                save_insight(db, user_id, desc, confidence=0.65,
                             wins=len(winners), losses=len(losers))
                adjustments += 1

    if adjustments:
        log_learning_event(
            db, user_id, "position_sizing_feedback",
            f"Generated {adjustments} sizing adjustments from {len(resolved)} alerts",
        )

    return {"adjustments": adjustments}


# ── Inter-Alert Learning ──────────────────────────────────────────────

def learn_inter_alert_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """Correlate co-fired alerts (same scan_cycle_id) to learn about
    alert volume and sector concentration effects."""
    from ...models.trading import BreakoutAlert
    from collections import defaultdict

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.scan_cycle_id.isnot(None),
        ).all()
    except Exception:
        return {"insights": 0}

    if len(resolved) < 10:
        return {"insights": 0, "note": "insufficient data"}

    cycles: dict[str, list] = defaultdict(list)
    for a in resolved:
        cycles[a.scan_cycle_id].append(a)

    insights_created = 0
    multi_alert_cycles = {k: v for k, v in cycles.items() if len(v) >= 3}

    if len(multi_alert_cycles) >= 3:
        high_vol_wins = []
        low_vol_wins = []
        for cid, alerts in cycles.items():
            winners = sum(1 for a in alerts if a.outcome == "winner")
            wr = winners / len(alerts) * 100 if alerts else 0
            if len(alerts) >= 4:
                high_vol_wins.append(wr)
            elif len(alerts) <= 2:
                low_vol_wins.append(wr)

        if high_vol_wins and low_vol_wins:
            avg_high = sum(high_vol_wins) / len(high_vol_wins)
            avg_low = sum(low_vol_wins) / len(low_vol_wins)
            if avg_high < avg_low - 10:
                desc = (
                    f"Inter-alert: high-volume cycles (4+ alerts) have {avg_high:.0f}% win rate "
                    f"vs low-volume (1-2) at {avg_low:.0f}% — reduce crypto_alert_max_per_cycle"
                )
                save_insight(db, user_id, desc, confidence=0.55)
                insights_created += 1

    # Sector concentration analysis
    for cid, alerts in multi_alert_cycles.items():
        sectors = set(a.sector or "unknown" for a in alerts)
        winners = sum(1 for a in alerts if a.outcome == "winner")
        wr = winners / len(alerts) * 100

        if len(sectors) == 1 and wr > 60 and len(alerts) >= 3:
            desc = (
                f"Inter-alert: single-sector cycle ({list(sectors)[0]}, {len(alerts)} alerts) "
                f"achieved {wr:.0f}% win rate — sector momentum confirmed"
            )
            save_insight(db, user_id, desc, confidence=0.55)
            insights_created += 1
            break  # one insight per cycle is enough

    if insights_created:
        log_learning_event(
            db, user_id, "inter_alert_learning",
            f"Generated {insights_created} inter-alert insights from {len(cycles)} cycles",
        )

    return {"insights": insights_created, "cycles_analyzed": len(cycles)}


# ── Adaptive Timeframe Learning ───────────────────────────────────────

def learn_timeframe_performance(db: Session, user_id: int | None) -> dict[str, Any]:
    """Learn which scanner timeframes produce the best outcomes."""
    from ...models.trading import BreakoutAlert
    from collections import defaultdict

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.timeframe.isnot(None),
        ).all()
    except Exception:
        return {"insights": 0}

    if len(resolved) < 10:
        return {"insights": 0, "note": "insufficient data"}

    groups: dict[str, list] = defaultdict(list)
    for a in resolved:
        groups[f"{a.asset_type}|{a.timeframe}"].append(a)

    insights_created = 0
    tf_stats: list[tuple[str, float, int]] = []

    for key, alerts in groups.items():
        if len(alerts) < 5:
            continue
        asset_type, tf = key.split("|", 1)
        winners = sum(1 for a in alerts if a.outcome == "winner")
        wr = winners / len(alerts) * 100
        avg_gain = sum(a.max_gain_pct or 0 for a in alerts) / len(alerts)
        tf_stats.append((key, wr, len(alerts)))

        if wr > 65 and len(alerts) >= 8:
            desc = (
                f"Timeframe performance: {asset_type} {tf} achieves {wr:.0f}% win rate "
                f"(avg gain {avg_gain:+.1f}%, n={len(alerts)}) — boost {tf} pattern weights"
            )
            save_insight(db, user_id, desc, confidence=min(0.8, wr / 100),
                         wins=winners, losses=len(alerts) - winners)
            insights_created += 1

    if tf_stats and len(tf_stats) >= 2:
        best = max(tf_stats, key=lambda x: x[1])
        worst = min(tf_stats, key=lambda x: x[1])
        if best[1] - worst[1] > 20:
            desc = (
                f"Timeframe comparison: best {best[0]} at {best[1]:.0f}%wr (n={best[2]}) "
                f"vs worst {worst[0]} at {worst[1]:.0f}%wr (n={worst[2]})"
            )
            save_insight(db, user_id, desc, confidence=0.55)
            insights_created += 1

    if insights_created:
        log_learning_event(
            db, user_id, "timeframe_learning",
            f"Generated {insights_created} timeframe insights from {len(resolved)} alerts",
        )

    return {"insights": insights_created}


# ── Confidence Decay ──────────────────────────────────────────────────

def decay_stale_insights(db: Session, user_id: int | None) -> dict[str, Any]:
    """Decay confidence of insights not refreshed recently. Prune dead ones."""
    now = datetime.utcnow()
    q = db.query(TradingInsight).filter(TradingInsight.active.is_(True))
    if user_id is not None:
        q = q.filter(TradingInsight.user_id == user_id)
    insights = q.all()

    decayed = 0
    pruned = 0
    for ins in insights:
        if ins.last_seen is None:
            continue
        age_days = (now - ins.last_seen).days

        if age_days > 90 and ins.confidence < 0.3:
            ins.active = False
            pruned += 1
            log_learning_event(
                db, user_id, "insight_pruned",
                f"Pruned stale insight (>{age_days}d, conf {ins.confidence:.0%}): "
                f"{ins.pattern_description[:60]}",
                confidence_before=ins.confidence,
                confidence_after=0,
                related_insight_id=ins.id,
            )
        elif age_days > 60:
            old_conf = ins.confidence
            ins.confidence = round(ins.confidence * 0.8, 3)
            decayed += 1
        elif age_days > 30:
            old_conf = ins.confidence
            ins.confidence = round(ins.confidence * 0.9, 3)
            decayed += 1

    if decayed or pruned:
        db.commit()
        log_learning_event(
            db, user_id, "confidence_decay",
            f"Decayed {decayed} stale insights, pruned {pruned} dead insights",
        )

    return {"decayed": decayed, "pruned": pruned}


# ── Signal Synergy Mining ─────────────────────────────────────────────

def mine_signal_synergies(db: Session, user_id: int | None) -> dict[str, Any]:
    """Find which signal combinations are most powerful (win more than
    individual signals predict) and create synergy insights."""
    import json as _json
    from ...models.trading import BreakoutAlert
    from collections import Counter, defaultdict

    try:
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.signals_snapshot.isnot(None),
        ).all()
    except Exception:
        return {"synergies_found": 0}

    if len(resolved) < 10:
        return {"synergies_found": 0, "note": "insufficient data"}

    combo_outcomes: dict[tuple, list[str]] = defaultdict(list)

    for a in resolved:
        try:
            sigs = _json.loads(a.signals_snapshot) if a.signals_snapshot else []
            short_sigs = [s[:35] for s in sigs[:8]]
            for i in range(len(short_sigs)):
                for j in range(i + 1, len(short_sigs)):
                    combo = tuple(sorted([short_sigs[i], short_sigs[j]]))
                    combo_outcomes[combo].append(a.outcome)
        except Exception:
            logger.debug("[learning] mine_signal_synergies: non-critical operation failed", exc_info=True)

    synergies_found = 0
    overall_wr = sum(1 for a in resolved if a.outcome == "winner") / len(resolved) * 100

    for combo, outcomes in combo_outcomes.items():
        if len(outcomes) < 5:
            continue
        wr = sum(1 for o in outcomes if o == "winner") / len(outcomes) * 100

        if wr > overall_wr + 15 and wr > 65:
            desc = (
                f"Signal synergy: '{combo[0]}' + '{combo[1]}' — "
                f"{wr:.0f}% win rate (n={len(outcomes)}) vs baseline {overall_wr:.0f}% "
                f"— pattern combo synergy bonus"
            )
            w = sum(1 for o in outcomes if o == "winner")
            save_insight(db, user_id, desc, confidence=min(0.85, wr / 100),
                         wins=w, losses=len(outcomes) - w)
            synergies_found += 1

        if synergies_found >= 5:
            break

    if synergies_found:
        log_learning_event(
            db, user_id, "synergy_mining",
            f"Discovered {synergies_found} signal synergies from {len(resolved)} alerts",
        )

    return {"synergies_found": synergies_found}


# ── Pattern Refinement Engine ──────────────────────────────────────────

REFINEMENT_RULES: dict[str, list[dict[str, Any]]] = {
    "rsi oversold": [
        {"field": "rsi", "op": "lt", "variations": [25, 28, 30, 32, 35]},
    ],
    "rsi overbought": [
        {"field": "rsi", "op": "gt", "variations": [65, 68, 70, 72, 75]},
    ],
    "rsi<35": [
        {"field": "rsi", "op": "lt", "variations": [30, 33, 35, 38, 40]},
    ],
    "rsi<40": [
        {"field": "rsi", "op": "lt", "variations": [35, 38, 40, 42, 45]},
    ],
    "adx>25": [
        {"field": "adx", "op": "gt", "variations": [20, 22, 25, 28, 30]},
    ],
    "adx>30": [
        {"field": "adx", "op": "gt", "variations": [25, 28, 30, 33, 35]},
    ],
    "stoch<20": [
        {"field": "stoch_k", "op": "lt", "variations": [15, 18, 20, 22, 25]},
    ],
    "stoch<25": [
        {"field": "stoch_k", "op": "lt", "variations": [18, 20, 25, 28, 30]},
    ],
    "bb<0.15": [
        {"field": "bb_pct", "op": "lt", "variations": [0.10, 0.12, 0.15, 0.18, 0.20]},
    ],
    "volume surge": [
        {"field": "vol_ratio", "op": "gt", "variations": [1.5, 2.0, 2.5, 3.0, 4.0]},
    ],
    "volume spike 2x": [
        {"field": "vol_ratio", "op": "gt", "variations": [1.5, 2.0, 3.0, 4.0, 5.0]},
    ],
    "macd positive": [
        {"field": "macd_hist", "op": "gt", "variations": [0, 0.01, 0.05]},
    ],
}


def refine_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """Test parameter variations of top patterns and save improved variants.

    For each high-evidence pattern, tries different threshold values
    (e.g. RSI < 25/28/30/32/35) and saves the variant that outperforms
    the original.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .market_data import ALL_SCAN_TICKERS

    insights = get_insights(db, user_id, limit=50)
    top_patterns = sorted(insights, key=lambda i: i.evidence_count, reverse=True)[:10]

    if not top_patterns:
        return {"refined": 0, "note": "no patterns to refine"}

    mine_tickers = list(ALL_SCAN_TICKERS)[:600]
    from ...config import settings as _ref_settings

    _workers = (
        io_workers_high(_ref_settings)
        if (_use_massive() or _use_polygon())
        else io_workers_med(_ref_settings)
    )
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in mine_tickers}
        for f in as_completed(futs):
            if _shutting_down.is_set():
                break
            try:
                all_rows.extend(f.result())
            except Exception:
                logger.debug("[learning] refine_patterns: non-critical operation failed", exc_info=True)

    if len(all_rows) < 50:
        return {"refined": 0, "note": "insufficient data for refinement"}

    refined_count = 0
    for ins in top_patterns:
        desc_lower = ins.pattern_description.lower()

        for rule_key, rule_defs in REFINEMENT_RULES.items():
            if rule_key not in desc_lower:
                continue

            for rule_def in rule_defs:
                field = rule_def["field"]
                op = rule_def["op"]
                variations = rule_def["variations"]

                best_variant = None
                best_score = -999.0
                best_wr = 0.0
                best_n = 0
                best_wins = 0

                for threshold in variations:
                    if op == "lt":
                        filtered = [r for r in all_rows if r.get(field, 999) < threshold]
                    elif op == "gt":
                        filtered = [r for r in all_rows if r.get(field, -999) > threshold]
                    else:
                        continue

                    if len(filtered) < 10:
                        continue

                    avg_5d = sum(r["ret_5d"] for r in filtered) / len(filtered)
                    wins = sum(1 for r in filtered if r["ret_5d"] > 0)
                    wr = wins / len(filtered)
                    composite = avg_5d * 0.6 + wr * 0.4

                    if composite > best_score:
                        best_score = composite
                        best_variant = threshold
                        best_wr = wr
                        best_n = len(filtered)
                        best_wins = wins

                if best_variant is not None and best_score > 0:
                    original_desc = ins.pattern_description[:80]
                    refined_label = (
                        f"CHILI refinement: {field} {op} {best_variant} "
                        f"(avg {best_score:.2f}, {best_wr*100:.0f}%wr, n={best_n}) "
                        f"— refined from '{original_desc}'"
                    )
                    save_insight(
                        db, user_id, refined_label,
                        confidence=min(0.85, best_wr),
                        wins=best_wins, losses=best_n - best_wins,
                    )
                    refined_count += 1
                    log_learning_event(
                        db, user_id, "pattern_refinement",
                        f"Refined '{original_desc}': best threshold "
                        f"{field}{op}{best_variant} "
                        f"({best_wr*100:.0f}%wr, n={best_n})",
                        related_insight_id=ins.id,
                    )
                    break

    logger.info(f"[learning] Pattern refinement: {refined_count} patterns improved")
    return {"refined": refined_count, "top_patterns_checked": len(top_patterns)}


def deep_study(db: Session, user_id: int | None) -> dict[str, Any]:
    """Intensive AI-powered learning: analyze pattern performance, evolution results,
    and generate testable hypotheses for the next validation cycle."""
    from ...models.trading import ScanPattern, BacktestResult
    
    discoveries = mine_patterns(db, user_id)

    # ── ScanPattern performance (the new pattern-based system) ──
    active_patterns = db.query(ScanPattern).filter(
        ScanPattern.active.is_(True)
    ).order_by(ScanPattern.win_rate.desc().nullslast()).limit(30).all()
    
    pattern_text_lines = []
    for p in active_patterns:
        wr = f"{p.win_rate*100:.0f}%" if p.win_rate else "?"
        avg_ret = f"{p.avg_return_pct:+.1f}%" if p.avg_return_pct else "?"
        trades = p.trade_count or 0
        origin = p.origin or "manual"
        variant = f" ({p.variant_label})" if p.variant_label else ""
        pattern_text_lines.append(
            f"- {p.name}{variant}: {wr}wr, {avg_ret} avg, {trades} trades, origin={origin}"
        )
    pattern_text = "\n".join(pattern_text_lines) if pattern_text_lines else "No ScanPatterns yet."
    
    # ── Pattern evolution stats ──
    root_patterns = db.query(ScanPattern).filter(
        ScanPattern.active.is_(True),
        ScanPattern.parent_id.is_(None),
    ).count()
    variant_patterns = db.query(ScanPattern).filter(
        ScanPattern.active.is_(True),
        ScanPattern.parent_id.isnot(None),
    ).count()
    evolution_text = f"- Root patterns: {root_patterns}\n- Active variants: {variant_patterns}"
    
    # ── Recent backtest performance ──
    recent_bts = db.query(BacktestResult).order_by(
        BacktestResult.ran_at.desc()
    ).limit(50).all()
    if recent_bts:
        avg_sharpe = sum(bt.sharpe or 0 for bt in recent_bts) / len(recent_bts)
        avg_return = sum(bt.return_pct or 0 for bt in recent_bts) / len(recent_bts)
        total_trades = sum(bt.trade_count or 0 for bt in recent_bts)
        winners = sum(1 for bt in recent_bts if (bt.return_pct or 0) > 0)
        backtest_text = (
            f"- Last {len(recent_bts)} backtests: avg sharpe={avg_sharpe:.2f}, "
            f"avg return={avg_return:+.1f}%, total trades={total_trades}, "
            f"profitable={winners}/{len(recent_bts)} ({winners/len(recent_bts)*100:.0f}%)"
        )
    else:
        backtest_text = "No recent backtests."

    # ── Legacy TradingInsight patterns ──
    insights = get_insights(db, user_id, limit=15)
    insight_lines = []
    for ins in insights:
        insight_lines.append(
            f"- [{ins.confidence:.0%} conf, {ins.evidence_count} evidence] {ins.pattern_description[:80]}"
        )
    insight_text = "\n".join(insight_lines) if insight_lines else "No TradingInsights yet."

    snap_count = db.query(MarketSnapshot).count()
    filled_count = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).count()

    recent_scans = db.query(ScanResult).order_by(
        ScanResult.scanned_at.desc()
    ).limit(20).all()
    buy_count = sum(1 for s in recent_scans if s.signal == "buy")
    sell_count = sum(1 for s in recent_scans if s.signal == "sell")
    hold_count = sum(1 for s in recent_scans if s.signal == "hold")

    stats = get_trade_stats(db, user_id)

    # Per-pattern trade stats
    pattern_stats = get_trade_stats_by_pattern(db, user_id, min_trades=1)
    pattern_stats_text = "No per-pattern trade data yet."
    if pattern_stats:
        lines = []
        for ps in pattern_stats[:10]:
            lines.append(
                f"  - {ps['pattern']}: {ps['trades']} trades, "
                f"{ps['win_rate']:.0f}%wr, avg P&L ${ps['avg_pnl']:.2f}"
            )
        pattern_stats_text = "\n".join(lines)

    # Market regime
    regime_text = "Unknown"
    try:
        from .market_data import get_market_regime
        regime = get_market_regime()
        regime_text = (
            f"SPY {regime['spy_direction']} (5d momentum {regime['spy_momentum_5d']:+.1f}%), "
            f"VIX {regime['vix_regime']} ({regime['vix']}), "
            f"overall: {regime['regime']}"
        )
    except Exception:
        logger.debug("[learning] deep_study: non-critical operation failed", exc_info=True)

    # Adaptive weights drift
    weights_text = "Not available"
    try:
        from .scanner import get_all_weights, _DEFAULT_WEIGHTS
        current_w = get_all_weights()
        drifts = []
        for k, v in current_w.items():
            default = _DEFAULT_WEIGHTS.get(k, v)
            if default != 0 and abs(v - default) / abs(default) > 0.1:
                drifts.append(f"  - {k}: {default} -> {v} ({(v-default)/abs(default):+.0%})")
        weights_text = "\n".join(drifts) if drifts else "All weights at defaults."
    except Exception:
        logger.debug("[learning] deep_study: non-critical operation failed", exc_info=True)

    # Recently challenged hypotheses
    challenged_text = "None yet."
    try:
        challenged_events = db.query(LearningEvent).filter(
            LearningEvent.event_type == "hypothesis_challenged",
        ).order_by(LearningEvent.created_at.desc()).limit(5).all()
        if challenged_events:
            challenged_text = "\n".join(
                f"  - {e.description[:120]}" for e in challenged_events
            )
    except Exception:
        logger.debug("[learning] deep_study: non-critical operation failed", exc_info=True)

    # Dynamic hypothesis pool status
    from ...models.trading import TradingHypothesis
    hypothesis_pool_text = "No hypotheses yet."
    try:
        recent_hyps = db.query(TradingHypothesis).order_by(
            TradingHypothesis.last_tested_at.desc().nullslast()
        ).limit(15).all()
        if recent_hyps:
            lines = []
            for h in recent_hyps:
                rate = (h.times_confirmed or 0) / max(1, h.times_tested or 1)
                result_json = json.loads(h.last_result_json) if h.last_result_json else {}
                a_avg = result_json.get("group_a_avg", "?")
                b_avg = result_json.get("group_b_avg", "?")
                lines.append(
                    f"  - [{h.status}] {h.description} "
                    f"(tested {h.times_tested}x, confirm rate {rate:.0%}, "
                    f"last A={a_avg}, B={b_avg}, origin={h.origin})"
                )
            hypothesis_pool_text = "\n".join(lines)
    except Exception:
        logger.debug("[learning] deep_study: non-critical operation failed", exc_info=True)

    _monitor_decisions_text = "No pattern monitor decisions yet."
    try:
        from ...models.trading import PatternMonitorDecision as _PMD
        _recent_md = db.query(_PMD).filter(
            _PMD.was_beneficial.isnot(None),
        ).order_by(_PMD.created_at.desc()).limit(20).all()
        if _recent_md:
            _md_lines = []
            _beneficial = sum(1 for d in _recent_md if d.was_beneficial)
            _md_lines.append(f"Last {len(_recent_md)} decisions: {_beneficial} beneficial ({_beneficial/len(_recent_md):.0%})")
            for d in _recent_md[:8]:
                _md_lines.append(
                    f"  - {d.action} | health={d.health_score:.0%} | "
                    f"confidence={d.llm_confidence or 0:.0%} | "
                    f"beneficial={'yes' if d.was_beneficial else 'no'} | "
                    f"{(d.llm_reasoning or '')[:60]}"
                )
            _monitor_decisions_text = "\n".join(_md_lines)
    except Exception:
        logger.debug("[learning] deep_study: monitor decisions query failed", exc_info=True)

    reflection_prompt = f"""You are an AI trading brain doing a self-reflection on what you've learned.

## SCAN PATTERNS (Composable Rule Engine)
These are the evolved, backtested trading patterns the brain has learned:
{pattern_text}

## PATTERN EVOLUTION STATUS
{evolution_text}

## BACKTEST PERFORMANCE
{backtest_text}

## TRADING INSIGHTS (Discovered Correlations)
{insight_text}

## DATA STATS
- Total market snapshots: {snap_count}
- Snapshots with verified outcomes: {filled_count}
- New patterns discovered this session: {len(discoveries)}
- Recent scan: {buy_count} buys, {sell_count} sells, {hold_count} holds

## USER'S REAL TRADING PERFORMANCE
- Total trades: {stats.get('total_trades', 0)}
- Win rate: {stats.get('win_rate', 0)}%
- Total P&L: ${stats.get('total_pnl', 0)}

## PER-PATTERN TRADE PERFORMANCE (Real Trades)
{pattern_stats_text}

## CURRENT MARKET REGIME
{regime_text}

## ADAPTIVE WEIGHT DRIFT (what the brain is learning)
{weights_text}

## PATTERN MONITOR DECISIONS (live position management)
{_monitor_decisions_text}

## RECENTLY CHALLENGED HYPOTHESES
{challenged_text}

## HYPOTHESIS POOL STATUS
{hypothesis_pool_text}

Write a LEARNING REPORT in this format:

### Top Performing Patterns
(Summarize the top 3-5 ScanPatterns by win rate and backtest performance. Explain what conditions each pattern looks for in plain English.)

### Evolution Insights
(What pattern variants are being promoted? Which types of exits/entries work best? What's the evolution process discovering?)

### What's Working
(Which patterns have the highest confidence and best returns? Are real trades matching backtest expectations?)

### What's Not Working
(Which patterns have poor backtests? What hypotheses have been rejected? Any fakeout patterns to avoid?)

### My Current Market Read
(Based on the patterns + regime, what's the overall market telling you?)

### Pattern Monitor Performance
(Review the PATTERN MONITOR DECISIONS section. Are stop tightenings saving money or being premature? Are target loosenings capturing more profit? What should I adjust about my monitoring thresholds or LLM confidence requirements?)

### Next Study Goals
(What should the brain focus on? More timeframe variants? Different exit strategies? New combo patterns?)

### Active Hypothesis Results
(Review the hypothesis pool. Which hypotheses are being confirmed? Which are failing?
 Note any surprises — especially hypotheses you expected to confirm but didn't.)

### Hypotheses to Test
IMPORTANT: After your report, output a JSON block with concrete testable hypotheses.
These will be automatically added to the hypothesis pool and tested against real data.
Use EXACTLY this format (valid JSON):

```json
{{{{
  "hypotheses": [
    {{{{
      "description": "human-readable description of what to test",
      "condition_a": "indicator condition for group A (e.g. 'rsi > 65 and ema_stack == true')",
      "condition_b": "indicator condition for group B (e.g. 'rsi > 65 and ema_stack == false')",
      "expected_winner": "a",
      "related_weight": "optional_weight_name_to_influence"
    }}}}
  ]
}}}}
```

Available indicator fields: rsi, macd, macd_sig, macd_hist, adx, stoch_k, bb_pct,
bb_squeeze (bool), vol_ratio, ema_stack (bool), ema_20, ema_50, ema_100, gap_pct,
resistance_retests (int), narrow_range (string like NR7), vcp_count (int),
above_sma20 (bool), regime (string), spy_mom_5d (float).

Generate 3-5 NEW hypotheses based on:
1. Gaps in your knowledge from the hypothesis pool above
2. Challenged hypotheses that need follow-up investigation
3. Interesting indicator combinations not yet tested
4. Breakout-specific tests (resistance retests, VCP, narrow range patterns)

Keep it conversational and honest. Use actual numbers from the patterns above."""

    try:
        from ...prompts import load_prompt
        from ... import openai_client
        from ...logger import new_trace_id

        system_prompt = load_prompt("trading_analyst")
        trace_id = new_trace_id()
        result = openai_client.chat(
            messages=[{"role": "user", "content": reflection_prompt}],
            system_prompt=system_prompt,
            trace_id=trace_id,
            user_message=reflection_prompt,
            max_tokens=3000,
        )
        reflection = result.get("reply", "Could not generate reflection.")
    except Exception as e:
        reflection = f"Reflection unavailable: {e}"

    # Extract structured hypotheses from the LLM response
    extracted_hypotheses = _extract_hypotheses_from_reflection(reflection)
    hypotheses_saved = 0
    for hyp in extracted_hypotheses:
        desc = hyp.get("description", "")
        cond_a = hyp.get("condition_a", "")
        cond_b = hyp.get("condition_b", "")
        expected = hyp.get("expected_winner", "a")
        related_w = hyp.get("related_weight")
        if desc and cond_a and cond_b:
            existing = db.query(TradingHypothesis).filter_by(description=desc).first()
            if not existing:
                new_hyp = TradingHypothesis(
                    description=desc,
                    condition_a=cond_a,
                    condition_b=cond_b,
                    expected_winner=expected,
                    origin="llm_generated",
                    status="pending",
                    related_weight=related_w if related_w else None,
                )
                db.add(new_hyp)
                hypotheses_saved += 1
    if hypotheses_saved:
        db.commit()

    log_learning_event(
        db, user_id, "review",
        f"Deep study: {len(discoveries)} new patterns, {len(insights)} total active. "
        f"AI reflection generated. {hypotheses_saved} new hypotheses extracted.",
    )

    return {
        "ok": True,
        "discoveries": discoveries,
        "total_patterns": len(insights),
        "reflection": reflection,
        "hypotheses_extracted": hypotheses_saved,
        "stats": {
            "snapshots": snap_count,
            "verified": filled_count,
            "new_discoveries": len(discoveries),
        },
    }


def _extract_hypotheses_from_reflection(text: str) -> list[dict]:
    """Parse structured hypotheses JSON from LLM reflection output."""
    import re
    hypotheses = []
    json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if not json_match:
        json_match = re.search(r'\{\s*"hypotheses"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1) if json_match.lastindex else json_match.group(0))
            raw = data.get("hypotheses", [])
            for h in raw:
                if isinstance(h, dict) and "description" in h:
                    hypotheses.append(h)
        except (json.JSONDecodeError, AttributeError):
            pass
    return hypotheses[:10]


# ── Multi-Signal Prediction Engine (compute_prediction in learning_predictions) ──


def run_promoted_pattern_fast_eval(db: Session) -> dict[str, Any]:
    """Scheduler entrypoint: refresh promoted-only cache when a full cycle is not running."""
    from ...config import settings

    if not getattr(settings, "brain_fast_eval_enabled", True):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    if get_learning_status().get("running"):
        return {"ok": True, "skipped": True, "reason": "learning_cycle_active"}

    return refresh_promoted_prediction_cache(db)


# ── Brain Dashboard Stats ────────────────────────────────────────────

def backfill_predicted_scores(db: Session, limit: int = 500) -> int:
    """Batch-fill predicted_score on snapshots that have indicator_data but no score."""
    unfilled = db.query(MarketSnapshot).filter(
        MarketSnapshot.predicted_score.is_(None),
        MarketSnapshot.indicator_data.isnot(None),
    ).limit(limit).all()

    filled = 0
    for snap in unfilled:
        try:
            ind_data = json.loads(snap.indicator_data) if snap.indicator_data else {}
            if not ind_data:
                continue
            clean = {k: v for k, v in ind_data.items() if k not in ("ticker", "interval")}
            snap.predicted_score = compute_prediction(clean)
            filled += 1
        except Exception:
            continue

    if filled > 0:
        try:
            db.commit()
        except Exception:
            db.rollback()
    return filled


def get_brain_stats(db: Session, user_id: int | None) -> dict[str, Any]:
    from ...models.trading import ScanPattern
    from ..ticker_universe import get_ticker_count
    from .scanner import get_scan_status

    total_patterns = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
    ).count()

    total_scan_patterns = db.query(ScanPattern).filter(
        ScanPattern.active.is_(True),
    ).count()
    promoted_patterns = db.query(ScanPattern).filter(
        ScanPattern.active.is_(True),
        ScanPattern.promotion_status == "promoted",
    ).count()

    avg_confidence_row = db.query(func.avg(TradingInsight.confidence)).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
    ).scalar()
    avg_confidence = round(float(avg_confidence_row or 0) * 100, 1)

    week_ago = datetime.utcnow() - timedelta(days=7)
    two_weeks_ago = datetime.utcnow() - timedelta(days=14)
    patterns_this_week = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.created_at >= week_ago,
    ).count()

    recent_conf = db.query(func.avg(TradingInsight.confidence)).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
        TradingInsight.created_at >= week_ago,
    ).scalar()
    prior_conf = db.query(func.avg(TradingInsight.confidence)).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
        TradingInsight.created_at >= two_weeks_ago,
        TradingInsight.created_at < week_ago,
    ).scalar()
    if recent_conf and prior_conf:
        conf_delta = round((float(recent_conf) - float(prior_conf)) * 100, 1)
    else:
        conf_delta = 0.0

    total_snapshots = db.query(MarketSnapshot).count()

    filled_snaps = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(2000).all()
    correct = 0
    medium_correct = medium_total = 0
    strong_correct = strong_total = 0
    total_predictions = 0
    stock_correct = stock_total = crypto_correct = crypto_total = 0
    for snap in filled_snaps:
        try:
            if snap.predicted_score is not None:
                pred_score = snap.predicted_score
            else:
                ind_data = json.loads(snap.indicator_data) if snap.indicator_data else {}
                if not ind_data:
                    continue
                pred_score = compute_prediction(ind_data)

            if abs(pred_score) < 0.1:
                continue

            predicted_up = pred_score > 0
            actual_up = (snap.future_return_5d or 0) > 0
            is_hit = predicted_up == actual_up
            if is_hit:
                correct += 1
            total_predictions += 1

            is_c = snap.ticker.endswith("-USD")
            if is_c:
                crypto_total += 1
                if is_hit:
                    crypto_correct += 1
            else:
                stock_total += 1
                if is_hit:
                    stock_correct += 1

            if abs(pred_score) >= 1.0:
                medium_total += 1
                if is_hit:
                    medium_correct += 1

            if abs(pred_score) >= 3.0:
                strong_total += 1
                if is_hit:
                    strong_correct += 1
        except Exception:
            continue
    if total_predictions > 0:
        try:
            db.commit()
        except Exception:
            db.rollback()
    accuracy = round(correct / total_predictions * 100, 1) if total_predictions > 0 else 0
    medium_accuracy = round(medium_correct / medium_total * 100, 1) if medium_total > 0 else 0
    strong_accuracy = round(strong_correct / strong_total * 100, 1) if strong_total > 0 else 0
    stock_accuracy = round(stock_correct / stock_total * 100, 1) if stock_total > 0 else 0
    crypto_accuracy = round(crypto_correct / crypto_total * 100, 1) if crypto_total > 0 else 0

    # Early accuracy using 3-day returns (available sooner than 5-day)
    early_accuracy = 0
    early_predictions = 0
    if total_predictions == 0:
        early_snaps = db.query(MarketSnapshot).filter(
            MarketSnapshot.future_return_3d.isnot(None),
            MarketSnapshot.predicted_score.isnot(None),
            MarketSnapshot.future_return_5d.is_(None),
        ).order_by(MarketSnapshot.snapshot_date.desc()).limit(2000).all()
        e_correct = 0
        for snap in early_snaps:
            try:
                pred_score = snap.predicted_score
                if abs(pred_score) < 0.1:
                    continue
                predicted_up = pred_score > 0
                actual_up = (snap.future_return_3d or 0) > 0
                if predicted_up == actual_up:
                    e_correct += 1
                early_predictions += 1
            except Exception:
                continue
        early_accuracy = round(e_correct / early_predictions * 100, 1) if early_predictions > 0 else 0

    # Pipeline status: pending predictions (have predicted_score, awaiting outcome)
    pending_predictions = db.query(MarketSnapshot).filter(
        MarketSnapshot.predicted_score.isnot(None),
        MarketSnapshot.future_return_5d.is_(None),
    ).count()

    evaluated_snapshots = db.query(MarketSnapshot).filter(
        MarketSnapshot.predicted_score.isnot(None),
        MarketSnapshot.future_return_5d.isnot(None),
    ).count()

    oldest_unevaluated = None
    days_until_first_result = None
    if pending_predictions > 0:
        oldest_pending = db.query(MarketSnapshot.snapshot_date).filter(
            MarketSnapshot.predicted_score.isnot(None),
            MarketSnapshot.future_return_5d.is_(None),
        ).order_by(MarketSnapshot.snapshot_date.asc()).first()
        if oldest_pending and oldest_pending[0]:
            oldest_unevaluated = oldest_pending[0].isoformat()
            days_elapsed = (datetime.utcnow() - oldest_pending[0]).days
            remaining = max(0, 7 - days_elapsed)
            days_until_first_result = remaining

    if total_snapshots == 0:
        pipeline_status = "no_data"
    elif pending_predictions > 0 and total_predictions == 0:
        pipeline_status = "pending_verification"
    elif total_predictions > 0:
        pipeline_status = "active"
    else:
        pipeline_status = "collecting"

    total_events = db.query(LearningEvent).filter(
        LearningEvent.user_id == user_id,
    ).count()

    universe_counts = get_ticker_count()
    scan_st = get_scan_status()
    learning_st = get_learning_status()
    vol_regime = get_volatility_regime()

    from .pattern_ml import get_meta_learner
    _meta = get_meta_learner()
    ml_stats = _meta.get_stats()

    research_kpi_benchmarks: dict[str, Any] = {"sample_count": 0}
    try:
        from ...models.trading import BacktestResult
        from .research_kpis import aggregate_kpis_from_params_rows

        param_rows = [
            r[0]
            for r in (
                db.query(BacktestResult.params)
                .filter(
                    BacktestResult.scan_pattern_id.isnot(None),
                    BacktestResult.trade_count > 0,
                )
                .order_by(BacktestResult.ran_at.desc())
                .limit(800)
                .all()
            )
        ]
        research_kpi_benchmarks = aggregate_kpis_from_params_rows(
            param_rows, max_samples=800,
        )
    except Exception:
        logger.debug("[learning] get_brain_stats: non-critical operation failed", exc_info=True)

    last_cycle_digest = None
    last_proposal_skips = None
    try:
        import json as _json

        from ..brain_worker_signals import get_worker_control_snapshot

        _bwc = get_worker_control_snapshot(db)
        if _bwc is not None:
            raw_d = getattr(_bwc, "last_cycle_digest_json", None)
            if raw_d:
                try:
                    last_cycle_digest = _json.loads(raw_d)
                except Exception:
                    last_cycle_digest = None
            raw_p = getattr(_bwc, "last_proposal_skips_json", None)
            if raw_p:
                try:
                    last_proposal_skips = _json.loads(raw_p)
                except Exception:
                    last_proposal_skips = None
    except Exception:
        logger.debug("[learning] get_brain_stats: non-critical operation failed", exc_info=True)

    return {
        "total_patterns": total_patterns,
        "total_scan_patterns": total_scan_patterns,
        "promoted_patterns": promoted_patterns,
        "avg_confidence": avg_confidence,
        "confidence_trend": conf_delta,
        "patterns_this_week": patterns_this_week,
        "total_snapshots": total_snapshots,
        "prediction_accuracy": accuracy,
        "medium_accuracy": medium_accuracy,
        "strong_accuracy": strong_accuracy,
        "total_predictions": total_predictions,
        "medium_predictions": medium_total,
        "strong_predictions": strong_total,
        "stock_accuracy": stock_accuracy,
        "stock_predictions": stock_total,
        "crypto_accuracy": crypto_accuracy,
        "crypto_predictions": crypto_total,
        "early_accuracy": early_accuracy,
        "early_predictions": early_predictions,
        "pending_predictions": pending_predictions,
        "evaluated_snapshots": evaluated_snapshots,
        "oldest_unevaluated": oldest_unevaluated,
        "days_until_first_result": days_until_first_result,
        "pipeline_status": pipeline_status,
        "total_events": total_events,
        "universe_stocks": universe_counts["stocks"],
        "universe_crypto": universe_counts["crypto"],
        "universe_total": universe_counts["total"],
        "last_scan": scan_st.get("last_run"),
        "learning_running": learning_st.get("running", False),
        "vix": vol_regime.get("vix"),
        "vix_regime": vol_regime.get("regime"),
        "vix_label": vol_regime.get("label"),
        "ml_ready": _meta.is_ready(),
        "ml_accuracy": ml_stats.get("cv_accuracy", 0),
        "ml_samples": ml_stats.get("samples", 0),
        "ml_trained_at": ml_stats.get("trained_at"),
        "ml_feature_importances": ml_stats.get("feature_importances"),
        "ml_active_patterns": ml_stats.get("active_patterns", 0),
        "research_funnel": get_research_funnel_snapshot(db),
        "attribution_coverage": get_attribution_coverage_stats(db, user_id),
        "last_cycle_funnel": get_learning_status().get("last_cycle_funnel"),
        "last_cycle_budget": get_learning_status().get("last_cycle_budget"),
        "research_kpi_benchmarks": research_kpi_benchmarks,
        "pattern_pipeline_near": get_pattern_pipeline_near(db, limit=14),
        "last_cycle_digest": last_cycle_digest,
        "last_proposal_skips": last_proposal_skips,
    }


def dedup_existing_patterns(db: Session, user_id: int | None) -> dict[str, Any]:
    """One-time cleanup: merge duplicate active patterns that share the same label prefix."""
    from .portfolio import _pattern_label, _pattern_keywords

    active = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
        TradingInsight.active.is_(True),
    ).order_by(TradingInsight.evidence_count.desc()).all()

    groups: dict[str, list[TradingInsight]] = {}
    for ins in active:
        label = _pattern_label(ins.pattern_description).lower().strip()
        kw = frozenset(_pattern_keywords(label))
        placed = False
        for key, members in groups.items():
            existing_kw = frozenset(_pattern_keywords(key))
            if existing_kw and kw:
                overlap = len(kw & existing_kw) / max(1, len(kw | existing_kw))
                if overlap >= 0.5:
                    members.append(ins)
                    placed = True
                    break
        if not placed:
            groups[label] = [ins]

    merged = 0
    deactivated = 0
    for _label, members in groups.items():
        if len(members) <= 1:
            continue
        members.sort(key=lambda i: i.evidence_count, reverse=True)
        keeper = members[0]
        for dup in members[1:]:
            keeper.evidence_count += dup.evidence_count
            keeper.confidence = round(
                min(0.95, max(keeper.confidence, dup.confidence)), 3
            )
            if dup.last_seen and (not keeper.last_seen or dup.last_seen > keeper.last_seen):
                keeper.last_seen = dup.last_seen
            dup.active = False
            deactivated += 1
        keeper.pattern_description = members[0].pattern_description
        merged += 1

    if deactivated:
        db.commit()
        log_learning_event(
            db, user_id, "review",
            f"Pattern cleanup: merged {merged} groups, deactivated {deactivated} duplicates",
        )

    return {
        "groups_merged": merged,
        "duplicates_removed": deactivated,
        "remaining_active": len(active) - deactivated,
    }


def get_accuracy_detail(
    db: Session,
    detail_type: str = "all",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent evaluated predictions with outcomes for drill-down."""
    filled_snaps = db.query(MarketSnapshot).filter(
        MarketSnapshot.future_return_5d.isnot(None),
        MarketSnapshot.predicted_score.isnot(None),
    ).order_by(MarketSnapshot.snapshot_date.desc()).limit(500).all()

    results: list[dict[str, Any]] = []
    for snap in filled_snaps:
        pred_score = snap.predicted_score
        if abs(pred_score) < 0.1:
            continue

        is_crypto = snap.ticker.endswith("-USD")
        if detail_type == "stock" and is_crypto:
            continue
        if detail_type == "crypto" and not is_crypto:
            continue
        if detail_type == "strong" and abs(pred_score) < 3.0:
            continue

        predicted_up = pred_score > 0
        actual_return = snap.future_return_5d or 0
        actual_up = actual_return > 0
        is_hit = predicted_up == actual_up

        results.append({
            "ticker": snap.ticker,
            "date": snap.snapshot_date.isoformat() if snap.snapshot_date else None,
            "predicted_score": round(pred_score, 2),
            "predicted_direction": "bullish" if predicted_up else "bearish",
            "actual_return_5d": round(actual_return, 2),
            "actual_direction": "up" if actual_up else "down",
            "hit": is_hit,
            "close_price": round(snap.close_price, 4) if snap.close_price else None,
        })
        if len(results) >= limit:
            break

    return results


def get_confidence_history(db: Session, user_id: int | None) -> list[dict[str, Any]]:
    insights = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id,
    ).order_by(TradingInsight.created_at.asc()).all()

    if not insights:
        return []

    points: list[dict[str, Any]] = []
    start = insights[0].created_at
    end = datetime.utcnow()
    current = start
    while current <= end:
        week_end = current + timedelta(days=7)
        week_insights = [i for i in insights if current <= i.created_at < week_end and i.active]
        if week_insights:
            avg_conf = sum(i.confidence for i in week_insights) / len(week_insights)
            points.append({
                "time": int(current.timestamp()),
                "value": round(avg_conf * 100, 1),
                "count": len(week_insights),
            })
        current = week_end

    return points


# ── Learning Cycle Orchestrator ───────────────────────────────────────

_learning_status: dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_duration_s": None,
    "phase": "idle",
    "current_step": "",
    "graph_node_id": "",
    "current_cluster_id": "",
    "current_step_sid": "",
    "current_cluster_index": -1,
    "current_step_index": -1,
    "nodes_completed": 0,
    "total_nodes": sum(
        len(c.steps)
        for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
        if c.id != SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID
    ),
    "clusters_completed": 0,
    "total_clusters": sum(
        1 for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
        if c.id != SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID
    ),
    "patterns_found": 0,
    "tickers_processed": 0,
    "step_timings": {},
    "data_provider": None,
    "last_cycle_funnel": None,
    "last_cycle_budget": None,
    "correlation_id": None,
    "secondary_miners_skipped": False,
}

# Phase 3: global cycle lease enforcement (dedicated sessions; cleared in run_learning_cycle finally).
_brain_lease_enforcement_ctx: dict[str, Any] = {}

_last_learning_live_persist_mono: float = 0.0
_LEARNING_LIVE_DB_THROTTLE_S = 2.0

# Keys mirrored in data/brain_worker_status.json["learning"] for cross-process UI (uvicorn reads worker).
_BRAIN_WORKER_STATUS_LEARNING_KEYS: tuple[str, ...] = (
    "running",
    "phase",
    "current_step",
    "graph_node_id",
    "current_cluster_id",
    "current_step_sid",
    "current_cluster_index",
    "current_step_index",
    "nodes_completed",
    "total_nodes",
    "clusters_completed",
    "total_clusters",
    "started_at",
    "elapsed_s",
)


def _learning_status_with_elapsed() -> dict[str, Any]:
    status = dict(_learning_status)
    if status.get("running") and status.get("started_at"):
        try:
            started = datetime.fromisoformat(status["started_at"])
            status["elapsed_s"] = round((datetime.utcnow() - started).total_seconds(), 1)
        except Exception:
            logger.debug("[learning] _learning_status_with_elapsed: non-critical operation failed", exc_info=True)
    # Neural mesh desk: map architecture ids → ``brain_graph_nodes`` ids (nm_lc_*).
    cid = (status.get("current_cluster_id") or "").strip()
    sid = (status.get("current_step_sid") or "").strip()
    if status.get("running") and sid:
        status["mesh_step_node_id"] = f"nm_lc_{sid}"
    else:
        status["mesh_step_node_id"] = ""
    if status.get("running") and cid:
        status["mesh_cluster_node_id"] = f"nm_lc_{cid}"
    else:
        status["mesh_cluster_node_id"] = ""
    return status


def snapshot_learning_for_brain_worker_status_file() -> dict[str, Any]:
    """In-process cycle fields for ``brain_worker_status.json`` (brain worker process)."""
    st = _learning_status_with_elapsed()
    return {k: st.get(k) for k in _BRAIN_WORKER_STATUS_LEARNING_KEYS}


def _overlay_learning_from_brain_worker_status_file(status: dict[str, Any]) -> None:
    """When the trading brain worker runs out-of-process, merge its live cycle snapshot for the web UI."""
    try:
        from ...db import DATA_DIR

        path = DATA_DIR / "brain_worker_status.json"
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("status") != "running":
            return
        file_pid = data.get("pid")
        try:
            if file_pid is not None and int(file_pid) == os.getpid():
                return
        except (TypeError, ValueError):
            pass
        snap = data.get("learning")
        if not isinstance(snap, dict):
            return
        for k in _BRAIN_WORKER_STATUS_LEARNING_KEYS:
            if k in snap:
                status[k] = snap[k]
    except Exception:
        logger.debug("[learning] _overlay_learning_from_brain_worker_status_file: non-critical operation failed", exc_info=True)


def _overlay_learning_from_brain_worker_db(status: dict[str, Any]) -> None:
    """Merge live cycle snapshot from PostgreSQL (authoritative across processes/containers)."""
    try:
        from ...db import SessionLocal
        from ...models.core import BrainWorkerControl

        _sdb = SessionLocal()
        try:
            row = _sdb.get(BrainWorkerControl, 1)
            if row is None or not row.learning_live_json:
                return
            data = json.loads(row.learning_live_json)
            if not isinstance(data, dict):
                return
            for k in _BRAIN_WORKER_STATUS_LEARNING_KEYS:
                if k in data:
                    status[k] = data[k]
        finally:
            _sdb.close()
    except Exception:
        logger.debug("[learning] _overlay_learning_from_brain_worker_db: non-critical operation failed", exc_info=True)


def _persist_learning_live_snapshot_to_db() -> None:
    try:
        from ...db import SessionLocal
        from ..brain_worker_signals import persist_learning_live_json

        snap = snapshot_learning_for_brain_worker_status_file()
        _sdb = SessionLocal()
        try:
            persist_learning_live_json(_sdb, snap)
        finally:
            _sdb.close()
    except Exception as e:
        logger.warning("[learning] persist_learning_live_snapshot_to_db: %s", e)


def maybe_persist_learning_live_after_architecture_step(status_dict: dict[str, Any]) -> None:
    """Throttled DB write while a cycle is running (skip tests using a throwaway status dict)."""
    if status_dict is not _learning_status:
        return
    if not _learning_status.get("running"):
        return
    global _last_learning_live_persist_mono
    now = time.monotonic()
    if now - _last_learning_live_persist_mono < _LEARNING_LIVE_DB_THROTTLE_S:
        return
    _last_learning_live_persist_mono = now
    _persist_learning_live_snapshot_to_db()


def persist_learning_live_snapshot_force() -> None:
    """Write current in-memory snapshot (e.g. idle after cycle end); bypasses throttle."""
    global _last_learning_live_persist_mono
    _last_learning_live_persist_mono = time.monotonic()
    _persist_learning_live_snapshot_to_db()


def get_learning_status() -> dict[str, Any]:
    status = _learning_status_with_elapsed()
    _overlay_learning_from_brain_worker_status_file(status)
    _overlay_learning_from_brain_worker_db(status)
    return status


def _run_pattern_engine_cycle(db: Session, user_id: int | None) -> dict[str, Any]:
    """Run pattern discovery, testing, and evolution in one cycle."""
    result: dict[str, Any] = {
        "hypotheses_generated": 0,
        "patterns_tested": 0,
        "patterns_evolved": 0,
        "web_patterns_created": 0,
    }

    try:
        from .pattern_engine import get_active_patterns, update_pattern
        from ..backtest_service import backtest_pattern

        # Step A: Statistical + insight-keyword hypothesis discovery (no LLM)
        hypotheses = discover_pattern_hypotheses(db, user_id)
        result["hypotheses_generated"] = len(hypotheses)

        # Step B: Web research — browse online for new patterns
        try:
            from .web_pattern_researcher import run_web_pattern_research
            web_report = run_web_pattern_research(db)
            result["web_patterns_created"] = web_report.get("patterns_created", 0)
            result["web_searches"] = web_report.get("searches", 0)
        except Exception as e:
            logger.warning(f"[learning] Web pattern research failed: {e}")

        # Step C: Test all active patterns
        patterns = get_active_patterns(db)
        tested = 0
        for p in patterns[:10]:
            try:
                bt_result = test_pattern_hypothesis(db, p, user_id)
                if bt_result:
                    tested += 1
            except Exception:
                continue
        result["patterns_tested"] = tested

        # Step D: Evolve — prune weak, promote strong
        evolved = evolve_patterns(db)
        result["patterns_evolved"] = evolved

    except Exception as e:
        logger.warning(f"[learning] Pattern engine cycle error: {e}")

    return result


def discover_pattern_hypotheses(
    db: Session,
    user_id: int | None,
    max_hypotheses: int = 3,
) -> list[dict[str, Any]]:
    """Propose new ScanPattern hypotheses from snapshot lift + insight keywords (no LLM).

    Primary: statistical templates ranked on ``future_return_5d`` vs baseline.
    Secondary: pair conditions from high-confidence insight text keyword matches.
    """
    import json as _json

    from .pattern_engine import create_pattern, list_patterns
    from .statistical_pattern_hypotheses import (
        mine_proposals_from_insights,
        mine_proposals_from_snapshots,
    )
    from ...models.trading import TradingInsight

    existing = list_patterns(db)
    existing_names = {p["name"].lower() for p in existing}

    def _fp_for_pattern(p: dict) -> str | None:
        try:
            rj = p.get("rules_json")
            if isinstance(rj, str):
                rj = _json.loads(rj)
            conds = (rj or {}).get("conditions") or []
            return _json.dumps(conds, sort_keys=True, default=str)
        except Exception:
            return None

    existing_fps: set[str] = set()
    for p in existing:
        fp = _fp_for_pattern(p)
        if fp:
            existing_fps.add(fp)

    insights = (
        db.query(TradingInsight)
        .filter(TradingInsight.active.is_(True))
        .filter(TradingInsight.confidence >= 0.6)
        .order_by(TradingInsight.confidence.desc())
        .limit(20)
        .all()
    )

    if len(insights) < 3:
        return []

    proposals: list[dict[str, Any]] = []
    try:
        proposals.extend(
            mine_proposals_from_snapshots(db, max_proposals=max_hypotheses + 2)
        )
    except Exception as e:
        logger.warning("[learning] Statistical snapshot hypothesis mining failed: %s", e)

    try:
        proposals.extend(
            mine_proposals_from_insights(
                insights[:15],
                existing_condition_fps=existing_fps.copy(),
                max_proposals=max(1, max_hypotheses),
            )
        )
    except Exception as e:
        logger.warning("[learning] Insight keyword hypothesis mining failed: %s", e)

    # Dedupe by condition fingerprint; prefer earlier (snapshot lift ranked first).
    merged: list[dict[str, Any]] = []
    seen_local: set[str] = set(existing_fps)
    for prop in proposals:
        conds = prop.get("conditions") or []
        if len(conds) < 2:
            continue
        fp = _json.dumps(conds, sort_keys=True, default=str)
        if fp in seen_local:
            continue
        seen_local.add(fp)
        merged.append(prop)
        if len(merged) >= max_hypotheses * 2:
            break

    try:
        created = []
        for prop in merged[:max_hypotheses]:
            name = prop.get("name", "").strip()
            if not name or name.lower() in existing_names:
                continue

            pattern_data = {
                "name": name,
                "description": prop.get("description", ""),
                "rules_json": _json.dumps({"conditions": prop.get("conditions", [])}),
                "origin": "brain_discovered",
                "score_boost": prop.get("score_boost", 1.0),
                "min_base_score": prop.get("min_base_score", 4.0),
                "confidence": 0.3,
                "active": True,
            }
            p = create_pattern(db, pattern_data)

            user_ids_q = [
                r[0] for r in db.query(TradingInsight.user_id)
                .filter(TradingInsight.active.is_(True))
                .distinct()
                .all()
            ]
            if not user_ids_q:
                user_ids_q = [user_id]
            for uid in user_ids_q:
                db.add(TradingInsight(
                    user_id=uid,
                    scan_pattern_id=p.id,
                    pattern_description=f"{p.name} — {p.description or ''}",
                    confidence=p.confidence or 0.3,
                    evidence_count=0,
                    win_count=0,
                    loss_count=0,
                    active=True,
                ))
            db.commit()

            created.append({"id": p.id, "name": p.name})
            existing_names.add(name.lower())
            logger.info(f"[learning] Discovered new pattern hypothesis: {name}")

        return created

    except Exception as e:
        logger.warning("[learning] Pattern hypothesis discovery failed: %s", e)
        return []


def _find_insight_for_pattern(db: Session, pattern) -> Any | None:
    """Find the TradingInsight linked to a ScanPattern (for persisting backtests)."""
    from ...models.trading import TradingInsight
    return (
        db.query(TradingInsight)
        .filter(TradingInsight.scan_pattern_id == pattern.id)
        .order_by(TradingInsight.id.desc())
        .first()
    )


def test_pattern_hypothesis(
    db: Session,
    pattern,
    user_id: int | None,
    tickers: list[str] | None = None,
) -> dict[str, Any] | None:
    """Backtest a ScanPattern on a sample of tickers and update its confidence."""
    from ..backtest_service import (
        backtest_metrics_for_promotion_gate,
        backtest_pattern,
        get_backtest_params,
        save_backtest,
    )
    from .pattern_engine import update_pattern
    from .market_data import (
        ALL_SCAN_TICKERS,
        DEFAULT_CRYPTO_TICKERS,
        DEFAULT_SCAN_TICKERS,
    )

    if tickers is None:
        ac = (getattr(pattern, "asset_class", None) or "all").strip().lower()
        if ac in ("stock", "equity", "equities"):
            ac = "stocks"
        if ac == "crypto":
            tickers = list(DEFAULT_CRYPTO_TICKERS[:10])
        elif ac == "stocks":
            tickers = list(DEFAULT_SCAN_TICKERS[:10])
        else:
            tickers = list(ALL_SCAN_TICKERS[:12])

    linked_insight = _find_insight_for_pattern(db, pattern)
    from .backtest_engine import hydrate_scan_pattern_rules_json

    hydrate_scan_pattern_rules_json(db, pattern, linked_insight)
    db.refresh(pattern)

    tf = getattr(pattern, "timeframe", "1d") or "1d"
    bt_params = get_backtest_params(tf)
    bt_kw = brain_pattern_backtest_friction_kwargs(db)

    wins = 0
    total = 0
    returns: list[float] = []
    is_wrs: list[float] = []
    oos_wrs: list[float] = []
    oos_rets: list[float] = []
    oos_ticker_hits = 0
    oos_trade_sum = 0
    oos_avg_trade_pcts: list[float] = []
    oos_profit_factors: list[float] = []
    oos_robust_mins: list[float] = []
    integrity_rows: list[dict[str, Any]] = []
    eval_rows: list[dict[str, Any]] = []
    # First persisted insight backtest in this pass (``save_backtest``) supplies the ledger
    # param_hash via ``BacktestParamSet.param_hash`` — same canonical blob as stored rows.
    ledger_backtest_param_hash: str | None = None
    from ...models.trading import BacktestParamSet as _LedgerBacktestParamSet

    for ticker in tickers[:5]:
        try:
            result = backtest_pattern(
                ticker=ticker,
                pattern_name=pattern.name,
                rules_json=pattern.rules_json,
                interval=bt_params["interval"],
                period=bt_params["period"],
                exit_config=getattr(pattern, "exit_config", None),
                scan_pattern_id=pattern.id,
                **bt_kw,
            )
            if not result.get("ok"):
                continue
            total += 1
            wr, ret = backtest_metrics_for_promotion_gate(result)
            eval_rows.append(
                {
                    "ticker": (ticker or "").strip().upper(),
                    "chart_time_from": result.get("chart_time_from"),
                    "chart_time_to": result.get("chart_time_to"),
                    "ohlc_bars": result.get("ohlc_bars"),
                    "in_sample_bars": result.get("in_sample_bars"),
                    "out_of_sample_bars": result.get("out_of_sample_bars"),
                    "oos_holdout_fraction": result.get("oos_holdout_fraction"),
                    "period": bt_params["period"],
                    "interval": bt_params["interval"],
                    "spread_used": result.get("spread_used"),
                    "commission_used": result.get("commission_used"),
                    "oos_win_rate": result.get("oos_win_rate"),
                    "is_win_rate": wr,
                    "trade_count": result.get("trade_count"),
                    "oos_ok": bool(result.get("oos_ok")),
                }
            )
            is_wrs.append(wr)
            if wr > 50:
                wins += 1
            returns.append(ret)
            if result.get("oos_ok") and result.get("oos_win_rate") is not None:
                oos_ticker_hits += 1
                oos_wrs.append(float(result["oos_win_rate"]))
                oos_trade_sum += int(result.get("oos_trade_count") or 0)
                if result.get("oos_return_pct") is not None:
                    oos_rets.append(float(result["oos_return_pct"]))
                oo = result.get("out_of_sample") or {}
                if isinstance(oo, dict):
                    _atp = oo.get("avg_trade_pct")
                    if _atp is not None:
                        try:
                            oos_avg_trade_pcts.append(float(_atp))
                        except (TypeError, ValueError):
                            pass
                    _pf = oo.get("profit_factor")
                    if _pf is not None:
                        try:
                            oos_profit_factors.append(float(_pf))
                        except (TypeError, ValueError):
                            pass
                _rob = result.get("oos_robustness") or {}
                if isinstance(_rob, dict) and _rob.get("oos_wr_min") is not None:
                    try:
                        oos_robust_mins.append(float(_rob["oos_wr_min"]))
                    except (TypeError, ValueError):
                        pass
            if linked_insight:
                try:
                    _bt_row = save_backtest(
                        db,
                        linked_insight.user_id,
                        result,
                        insight_id=linked_insight.id,
                        scan_pattern_id=pattern.id,
                    )
                    if ledger_backtest_param_hash is None and _bt_row is not None:
                        _psid = getattr(_bt_row, "param_set_id", None)
                        if _psid is not None:
                            _bps = db.get(_LedgerBacktestParamSet, int(_psid))
                            if _bps is not None:
                                ledger_backtest_param_hash = _bps.param_hash
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        logger.debug("[learning] test_pattern_hypothesis: non-critical operation failed", exc_info=True)
            ri = result.get("research_integrity")
            if isinstance(ri, dict):
                integrity_rows.append({"ticker": ticker, **ri})
        except Exception:
            continue

    if total == 0:
        return None

    avg_return = sum(returns) / len(returns) if returns else 0
    ticker_vote_wr = (wins / total) * 100 if total > 0 else 0
    mean_is_wr = sum(is_wrs) / len(is_wrs) if is_wrs else 0.0
    mean_oos_wr = sum(oos_wrs) / len(oos_wrs) if oos_wrs else None
    mean_oos_ret = sum(oos_rets) / len(oos_rets) if oos_rets else None
    mean_oos_exp = (
        sum(oos_avg_trade_pcts) / len(oos_avg_trade_pcts)
        if oos_avg_trade_pcts
        else None
    )
    mean_oos_pf = (
        sum(oos_profit_factors) / len(oos_profit_factors)
        if oos_profit_factors
        else None
    )
    agg_robust_min = min(oos_robust_mins) if oos_robust_mins else None

    import random

    from ...config import settings as _oset

    n_boot = int(getattr(_oset, "brain_oos_bootstrap_iterations", 0) or 0)
    ci_low: float | None = None
    ci_high: float | None = None
    if n_boot > 0 and len(oos_wrs) >= 2:
        means_bt: list[float] = []
        for _ in range(n_boot):
            sample = [random.choice(oos_wrs) for _ in range(len(oos_wrs))]
            means_bt.append(sum(sample) / len(sample))
        means_bt.sort()
        lo_i = max(0, int(n_boot * 0.025))
        hi_i = min(n_boot - 1, int(n_boot * 0.975))
        ci_low = means_bt[lo_i]
        ci_high = means_bt[hi_i]

    new_confidence = max(0.1, min(0.95,
        pattern.confidence * 0.7 + (ticker_vote_wr / 100) * 0.3
    ))

    _oos_kw = brain_oos_gate_kwargs_for_pattern(pattern, oos_trade_sum)
    prom_stat, allow_active = brain_apply_oos_promotion_gate(
        origin=getattr(pattern, "origin", "") or "",
        mean_is_win_rate=mean_is_wr,
        mean_oos_win_rate=mean_oos_wr,
        oos_tickers_with_result=oos_ticker_hits,
        mean_oos_expectancy_pct=mean_oos_exp,
        mean_oos_profit_factor=mean_oos_pf,
        oos_wr_robust_min=agg_robust_min,
        oos_bootstrap_wr_ci_low=ci_low,
        **_oos_kw,
    )
    min_wr_pct = float(_oos_kw.get("min_oos_win_rate_pct", 0.0) or 0.0)
    pass_count = sum(1 for wr in oos_wrs if float(wr) >= min_wr_pct)
    per_ticker_oos_pass_ratio = (pass_count / len(oos_wrs)) if oos_wrs else 0.0
    if prom_stat == "promoted" and oos_wrs and per_ticker_oos_pass_ratio < 0.6:
        prom_stat = "rejected_oos"
        allow_active = False

    prev_ov = getattr(pattern, "oos_validation_json", None) or {}
    if not isinstance(prev_ov, dict):
        prev_ov = {}
    from .research_integrity import (
        aggregate_promotion_integrity,
        promotion_blocked_by_integrity,
    )

    oos_validation_merged: dict[str, Any] = {
        **prev_ov,
        "evaluated_at": datetime.utcnow().isoformat() + "Z",
        "expectancy_oos_mean": round(mean_oos_exp, 4) if mean_oos_exp is not None else None,
        "profit_factor_oos_mean": round(mean_oos_pf, 4) if mean_oos_pf is not None else None,
        "oos_wr_robust_min": round(agg_robust_min, 2) if agg_robust_min is not None else None,
        "bootstrap_mean_oos_wr_ci": (
            [round(ci_low, 2), round(ci_high, 2)]
            if ci_low is not None and ci_high is not None
            else None
        ),
        "per_ticker_oos_pass_ratio": round(per_ticker_oos_pass_ratio, 4),
        "per_ticker_oos_min_wr_pct": round(min_wr_pct, 4),
        "per_ticker_oos_pass_count": pass_count,
        "per_ticker_oos_total": len(oos_wrs),
        "research_integrity": aggregate_promotion_integrity(integrity_rows),
    }

    from ...models.trading import BacktestResult as _BT
    from ...models.trading import PatternTradeRow as _PTR
    from .mining_validation import check_promotion_ready

    _ptr_rows = (
        db.query(_PTR)
        .filter(
            _PTR.scan_pattern_id == pattern.id,
            _PTR.outcome_return_pct.isnot(None),
        )
        .order_by(_PTR.as_of_ts.asc())
        .all()
    )
    _ensemble_rows = [
        {
            "ret_5d": float(r.outcome_return_pct or 0.0),
            "bar_start_utc": r.as_of_ts,
        }
        for r in _ptr_rows
    ]
    _ensemble_ok, _ensemble_detail = check_promotion_ready(
        _ensemble_rows,
        min_trades=30,
        n_hypotheses_tested=max(1, int(total or 1)),
    )
    oos_validation_merged["ensemble_promotion_gate"] = _ensemble_detail
    if prom_stat == "promoted" and not _ensemble_ok:
        prom_stat = "rejected_oos"

    actual_bt_count = db.query(func.count(_BT.id)).filter(
        _BT.scan_pattern_id == pattern.id
    ).scalar() or 0

    patch: dict[str, Any] = {
        "confidence": round(new_confidence, 3),
        "win_rate": round(mean_is_wr / 100.0, 4),
        "avg_return_pct": round(avg_return, 2),
        "backtest_count": actual_bt_count,
        "evidence_count": actual_bt_count,
        "promotion_status": prom_stat,
        "oos_win_rate": round(mean_oos_wr / 100.0, 4) if mean_oos_wr is not None else None,
        "oos_avg_return_pct": round(mean_oos_ret, 2) if mean_oos_ret is not None else None,
        "oos_trade_count": oos_trade_sum if oos_trade_sum else None,
        "backtest_spread_used": bt_kw.get("spread"),
        "backtest_commission_used": bt_kw.get("commission"),
        "oos_evaluated_at": datetime.utcnow(),
        "oos_validation_json": oos_validation_merged,
    }
    if not allow_active:
        patch["active"] = False
    if prom_stat == "promoted" and getattr(_oset, "brain_paper_book_on_promotion", False):
        patch["paper_book_json"] = {
            "opened_at": datetime.utcnow().isoformat() + "Z",
            "entries": [],
        }

    from ...config import settings as _settings_bench
    bench_passes: bool | None = None
    bench_raw_for_edge: dict[str, Any] | None = None
    if _settings_bench.brain_bench_walk_forward_enabled:
        _bo = (getattr(pattern, "origin", "") or "").strip().lower()
        if _bo in _BRAIN_OOS_GATED_ORIGINS:
            try:
                _rules = json.loads(pattern.rules_json or "{}")
                _conds = _rules.get("conditions") or []
                if isinstance(_conds, list) and _conds:
                    _exit_cfg = None
                    _ec = getattr(pattern, "exit_config", None)
                    if _ec:
                        try:
                            _exit_cfg = json.loads(_ec) if isinstance(_ec, str) else _ec
                            if not isinstance(_exit_cfg, dict):
                                _exit_cfg = None
                        except (json.JSONDecodeError, TypeError, ValueError):
                            _exit_cfg = None
                    _bt_tickers = [
                        t.strip()
                        for t in _settings_bench.brain_bench_tickers.split(",")
                        if t.strip()
                    ]
                    from ..backtest_service import benchmark_walk_forward_evaluate

                    _bench_raw = benchmark_walk_forward_evaluate(
                        conditions=_conds,
                        pattern_name=pattern.name,
                        exit_config=_exit_cfg,
                        tickers=_bt_tickers,
                        period=_settings_bench.brain_bench_period,
                        interval=_settings_bench.brain_bench_interval,
                        n_windows=_settings_bench.brain_bench_n_windows,
                        min_bars_per_window=_settings_bench.brain_bench_min_bars_per_window,
                        min_positive_fold_ratio=_settings_bench.brain_bench_min_positive_fold_ratio,
                    )
                    bench_raw_for_edge = _bench_raw
                    bench_passes = bool(_bench_raw.get("passes_gate"))
                    _bench_store = _trim_bench_walk_forward_for_db(_bench_raw)
                    _bench_store["evaluated_at"] = datetime.utcnow().isoformat() + "Z"
                    _s_spread = float(_settings_bench.brain_bench_cost_stress_spread_mult)
                    _s_comm = float(_settings_bench.brain_bench_cost_stress_commission_mult)
                    _req_stress = bool(_settings_bench.brain_bench_require_stress_pass)
                    if _req_stress and _s_spread <= 1.0 and _s_comm <= 1.0:
                        _s_spread, _s_comm = 2.0, 1.5
                    if _s_spread > 1.0 or _s_comm > 1.0 or _req_stress:
                        _base_sp = float(_settings_bench.backtest_spread)
                        _base_c = float(_settings_bench.backtest_commission)
                        _stress_raw = benchmark_walk_forward_evaluate(
                            conditions=_conds,
                            pattern_name=pattern.name,
                            exit_config=_exit_cfg,
                            tickers=_bt_tickers,
                            period=_settings_bench.brain_bench_period,
                            interval=_settings_bench.brain_bench_interval,
                            n_windows=_settings_bench.brain_bench_n_windows,
                            min_bars_per_window=_settings_bench.brain_bench_min_bars_per_window,
                            min_positive_fold_ratio=_settings_bench.brain_bench_min_positive_fold_ratio,
                            spread=_base_sp * _s_spread,
                            commission=_base_c * _s_comm,
                        )
                        _bench_store["stress_passes_gate"] = bool(_stress_raw.get("passes_gate"))
                        _bench_store["stress_spread_mult"] = _s_spread
                        _bench_store["stress_commission_mult"] = _s_comm
                    patch["bench_walk_forward_json"] = _bench_store
                    try:
                        from .mining_validation import decay_signals_from_walk_forward_windows

                        _dm_sig = decay_signals_from_walk_forward_windows(
                            _bench_store.get("windows") or []
                        )
                        if _dm_sig:
                            ov2 = dict(patch.get("oos_validation_json") or {})
                            ov2["decay_monitor"] = {**(ov2.get("decay_monitor") or {}), **_dm_sig}
                            patch["oos_validation_json"] = ov2
                    except Exception:
                        logger.debug("[learning] test_pattern_hypothesis: non-critical operation failed", exc_info=True)
                    _bstat, _ballow = brain_apply_bench_promotion_gate(
                        origin=getattr(pattern, "origin", "") or "",
                        bench_summary=_bench_raw,
                        current_promotion_status=str(prom_stat),
                    )
                    if _bstat is not None:
                        patch["promotion_status"] = _bstat
                    if not _ballow:
                        patch["active"] = False
                    if _req_stress and _bench_store.get("stress_passes_gate") is False:
                        patch["promotion_status"] = "rejected_bench_stress"
                        patch["active"] = False
            except Exception as e:
                logger.warning(
                    "[learning] benchmark walk-forward failed for %s: %s",
                    pattern.name,
                    e,
                )

    _final_promo = str(patch.get("promotion_status", prom_stat))
    if promotion_blocked_by_integrity(
        oos_validation_merged.get("research_integrity") or {},
        target_status=_final_promo,
    ):
        patch["promotion_status"] = "rejected_research_integrity"
        patch["active"] = False
        oos_validation_merged = dict(oos_validation_merged)
        oos_validation_merged["research_integrity_blocked_promotion"] = True
        patch["oos_validation_json"] = oos_validation_merged

    from .edge_evidence import (
        apply_edge_evidence_veto,
        build_edge_evidence,
        resolve_gated_lifecycle_stage,
    )
    from .lifecycle import (
        lifecycle_stage_from_promotion_status,
        retire,
        transition_on_backtest,
        transition_on_promotion,
    )

    _edge_st = _oset
    _origin_lc = (getattr(pattern, "origin", "") or "").strip().lower()
    _gated_edge = _origin_lc in _BRAIN_OOS_GATED_ORIGINS
    edge_veto = False
    oos_merged = dict(patch.get("oos_validation_json") or oos_validation_merged)
    if _gated_edge and bool(getattr(_edge_st, "brain_edge_evidence_enabled", True)):
        _prev_ee = oos_merged.get("edge_evidence")
        _prev_codes = (
            (_prev_ee.get("promotion_block_codes") if isinstance(_prev_ee, dict) else None) or []
        )
        _ee = build_edge_evidence(
            mean_is_wr_pct=float(mean_is_wr),
            is_wrs=list(is_wrs),
            mean_oos_wr_pct=float(mean_oos_wr) if mean_oos_wr is not None else None,
            oos_wrs=list(oos_wrs),
            oos_ticker_hits=int(oos_ticker_hits),
            tickers_tested=int(total),
            oos_trade_sum=int(oos_trade_sum),
            bench_raw=bench_raw_for_edge,
            n_perm=int(getattr(_edge_st, "brain_edge_evidence_permutations", 400) or 400),
            seed=int(getattr(_edge_st, "brain_edge_evidence_seed", 42) or 42) + int(pattern.id),
            prev_block_codes=list(_prev_codes) if _prev_codes else None,
        )
        oos_merged["edge_evidence"] = _ee
        patch["oos_validation_json"] = oos_merged
        if bool(getattr(_edge_st, "brain_edge_evidence_gate_enabled", False)):
            edge_veto, _ = apply_edge_evidence_veto(
                _ee,
                max_is_perm_p=getattr(_edge_st, "brain_edge_evidence_max_is_perm_p", None),
                max_oos_perm_p=float(
                    getattr(_edge_st, "brain_edge_evidence_max_oos_perm_p", 0.2) or 0.2
                ),
                max_wf_perm_p=float(
                    getattr(_edge_st, "brain_edge_evidence_max_wf_perm_p", 0.25) or 0.25
                ),
                require_wf_when_available=bool(
                    getattr(_edge_st, "brain_edge_evidence_require_wf_when_available", False)
                ),
            )
            oos_merged["edge_evidence"] = _ee
            patch["oos_validation_json"] = oos_merged
            if edge_veto:
                patch["promotion_status"] = "pending_oos"
                patch["active"] = True

    if _gated_edge:
        oos_merged = dict(patch.get("oos_validation_json") or oos_validation_merged)
        from .edge_evidence import apply_phase2_hygiene_nudges
        from .research_integrity import rules_json_fingerprint
        from .selection_bias import (
            build_outcome_fingerprint,
            build_research_run_key,
            build_selection_bias_contract,
            build_validation_slice_key,
            record_validation_slice_use,
            selection_bias_skip_contract,
        )

        _rules_for_fp: list[dict[str, Any]] = []
        try:
            _rj = json.loads(pattern.rules_json or "{}")
            _rules_for_fp = list(_rj.get("conditions") or [])
        except Exception:
            pass
        _rfp = rules_json_fingerprint(_rules_for_fp if _rules_for_fp else None)

        if bool(getattr(_edge_st, "brain_selection_bias_enabled", True)):
            if eval_rows:
                _slice_key = build_validation_slice_key(
                    origin=_origin_lc,
                    asset_class=(getattr(pattern, "asset_class", None) or "all"),
                    timeframe=tf,
                    hypothesis_family=getattr(pattern, "hypothesis_family", None),
                    eval_rows=eval_rows,
                )
                _ofp = build_outcome_fingerprint(eval_rows)
                _rrk = build_research_run_key(
                    slice_key=_slice_key,
                    scan_pattern_id=int(pattern.id),
                    rules_fingerprint=_rfp,
                    outcome_fingerprint=_ofp,
                )
                _ins = record_validation_slice_use(
                    db,
                    research_run_key=_rrk,
                    slice_key=_slice_key,
                    scan_pattern_id=int(pattern.id),
                    rules_fingerprint=_rfp,
                    param_hash=ledger_backtest_param_hash,
                )
                oos_merged["selection_bias"] = build_selection_bias_contract(
                    db, slice_key=_slice_key, ledger_inserted=_ins
                )
            else:
                oos_merged["selection_bias"] = selection_bias_skip_contract("no_eval_rows")

        if bool(getattr(_edge_st, "brain_parameter_stability_enabled", False)):
            from .parameter_stability import compute_parameter_stability, pick_stability_tickers

            _st_seed = int(getattr(_edge_st, "brain_parameter_stability_seed", 123) or 123) + int(
                pattern.id
            )
            _st_k = int(getattr(_edge_st, "brain_parameter_stability_ticker_subset_size", 2) or 2)
            st_tick, _ = pick_stability_tickers(
                [r.get("ticker") or "" for r in eval_rows],
                k=_st_k,
                seed=_st_seed,
            )
            _baseline_st = (
                float(mean_oos_wr) if mean_oos_wr is not None else float(mean_is_wr)
            )
            oos_merged["parameter_stability"] = compute_parameter_stability(
                pattern_name=pattern.name,
                rules_json=pattern.rules_json,
                stability_tickers=st_tick,
                baseline_score=_baseline_st,
                backtest_pattern_fn=backtest_pattern,
                bt_params=bt_params,
                bt_kw=bt_kw,
                exit_config=getattr(pattern, "exit_config", None),
                scan_pattern_id=int(pattern.id),
                max_variant_evals=int(
                    getattr(_edge_st, "brain_parameter_stability_max_variant_evals", 6) or 6
                ),
                rel_pass_tol=float(
                    getattr(_edge_st, "brain_parameter_stability_neighbor_rel_tol", 0.12) or 0.12
                ),
                abs_floor=float(
                    getattr(_edge_st, "brain_parameter_stability_neighbor_abs_floor", 40.0) or 40.0
                ),
            )

        if bool(getattr(_edge_st, "brain_phase2_hygiene_nudge_enabled", True)):
            _ee2 = oos_merged.get("edge_evidence")
            if isinstance(_ee2, dict):
                apply_phase2_hygiene_nudges(
                    _ee2,
                    parameter_stability=oos_merged.get("parameter_stability"),
                    selection_bias=oos_merged.get("selection_bias"),
                    oos_validation=oos_merged,
                )
        patch["oos_validation_json"] = oos_merged

    _promo_for_lifecycle = str(patch.get("promotion_status", prom_stat))
    if _gated_edge and bool(getattr(_edge_st, "brain_edge_evidence_enabled", True)):
        if bool(getattr(_edge_st, "brain_edge_evidence_gate_enabled", False)):
            _ls_edge = resolve_gated_lifecycle_stage(
                promotion_status=_promo_for_lifecycle,
                edge_gate_ran=True,
                edge_veto=edge_veto,
            )
            patch["lifecycle_stage"] = (
                _ls_edge
                if _ls_edge is not None
                else lifecycle_stage_from_promotion_status(_promo_for_lifecycle)
            )
        else:
            patch["lifecycle_stage"] = lifecycle_stage_from_promotion_status(_promo_for_lifecycle)
    else:
        patch["lifecycle_stage"] = lifecycle_stage_from_promotion_status(_promo_for_lifecycle)

    update_pattern(db, pattern.id, patch)

    # Lifecycle FSM transitions based on promotion outcome
    _final_status = str(patch.get("promotion_status", prom_stat))
    try:
        if _final_status == "promoted":
            transition_on_promotion(db, pattern)
        elif _final_status in ("pending_oos", "backtested"):
            transition_on_backtest(db, pattern, oos_pass=True)
        elif _final_status.startswith("rejected"):
            retire(db, pattern, reason=f"promotion_gate_{_final_status}")
    except Exception as e:
        logger.debug("[learning] Lifecycle transition after promotion: %s", e)

    try:
        db.refresh(pattern)
    except Exception:
        pass

    _oos_log = f"{mean_oos_wr:.0f}%" if mean_oos_wr is not None else "n/a"
    _bench_log = (
        f", bench_pass={bench_passes}"
        if bench_passes is not None
        else ""
    )
    logger.info(
        f"[learning] Tested pattern '{pattern.name}': "
        f"IS_WR≈{mean_is_wr:.0f}%, OOS_WR≈{_oos_log}, "
        f"promo={patch.get('promotion_status', prom_stat)}, conf={new_confidence:.2f}"
        f"{_bench_log}"
    )

    return {
        "pattern_id": pattern.id,
        "name": pattern.name,
        "tickers_tested": total,
        "win_rate": mean_is_wr,
        "oos_win_rate": mean_oos_wr,
        "promotion_status": patch.get("promotion_status", prom_stat),
        "avg_return": avg_return,
        "new_confidence": new_confidence,
        "bench_passes_gate": bench_passes,
    }


def evolve_patterns(db: Session, min_evidence: int = 5, min_confidence: float = 0.2) -> int:
    """Prune low-confidence patterns and promote high-confidence ones.

    Returns the number of patterns modified.
    """
    from ...models.trading import ScanPattern

    _PROTECTED_ORIGINS = {
        "builtin", "user_seeded", "seed", "user",
        "exit_variant", "entry_variant", "combo_variant", "tf_variant", "scope_variant",
    }

    patterns = db.query(ScanPattern).filter_by(active=True).all()
    modified = 0

    for p in patterns:
        if p.origin in _PROTECTED_ORIGINS:
            if (p.confidence or 0) >= 0.7 and (p.score_boost or 0) < 2.0:
                p.score_boost = min((p.score_boost or 0) + 0.5, 3.0)
                modified += 1
                logger.info(f"[learning] Promoted pattern: {p.name} boost→{p.score_boost:.1f}")
            continue

        if p.parent_id is not None:
            continue

        if (p.evidence_count or 0) >= min_evidence and (p.confidence or 0) < min_confidence:
            if (p.win_rate or 0) >= 0.4:
                continue
            p.active = False
            modified += 1
            logger.info(f"[learning] Deactivated low-confidence pattern: {p.name} (conf={p.confidence:.2f})")
            continue

        if (p.confidence or 0) >= 0.7 and (p.score_boost or 0) < 2.0:
            p.score_boost = min((p.score_boost or 0) + 0.5, 3.0)
            modified += 1
            logger.info(f"[learning] Promoted pattern: {p.name} boost→{p.score_boost:.1f}")

    if modified:
        db.commit()
    return modified


# ── Ticker-scope classification ────────────────────────────────────

def recompute_ticker_scope(db: Session, pattern_id: int) -> str | None:
    """Classify a pattern as universal / sector / ticker_specific from its
    backtest results and persist the outcome.

    Returns the new scope string, or None when there isn't enough data.
    """
    from ...models.trading import ScanPattern, TradingInsight, BacktestResult
    from .backtest_engine import TICKER_TO_SECTOR

    pat = db.query(ScanPattern).get(pattern_id)
    if not pat:
        return None

    insight_ids = [
        r[0] for r in db.query(TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id == pattern_id)
        .all()
    ]
    if not insight_ids:
        return None

    bts = (
        db.query(BacktestResult)
        .filter(
            BacktestResult.related_insight_id.in_(insight_ids),
            BacktestResult.trade_count > 0,
        )
        .all()
    )
    from .market_data import is_crypto as _is_crypto

    ac = (pat.asset_class or "all").strip().lower()
    if ac in ("stock", "equity", "equities"):
        ac = "stocks"
    if ac == "crypto":
        bts = [b for b in bts if _is_crypto(b.ticker or "")]
    elif ac == "stocks":
        bts = [b for b in bts if not _is_crypto(b.ticker or "")]

    if len(bts) < 5:
        return pat.ticker_scope

    winners = [b for b in bts if (b.return_pct or 0) > 0]
    if not winners:
        return pat.ticker_scope

    sector_wins: dict[str, int] = {}
    ticker_wins: dict[str, int] = {}
    for w in winners:
        t = w.ticker or ""
        ticker_wins[t] = ticker_wins.get(t, 0) + 1
        sector = TICKER_TO_SECTOR.get(t, "unknown")
        sector_wins[sector] = sector_wins.get(sector, 0) + 1

    total_wins = len(winners)

    top_tickers = sorted(ticker_wins.items(), key=lambda x: -x[1])
    top3_ticker_wins = sum(c for _, c in top_tickers[:3])
    if total_wins >= 3 and top3_ticker_wins / total_wins >= 0.70 and len(top_tickers) <= 5:
        new_scope = "ticker_specific"
        scope_list = [t for t, _ in top_tickers[:5]]
        pat.ticker_scope = new_scope
        pat.scope_tickers = json.dumps(scope_list)
        return new_scope

    top_sectors = sorted(sector_wins.items(), key=lambda x: -x[1])
    top2_sector_wins = sum(c for _, c in top_sectors[:2])
    unique_sectors = {s for s in sector_wins if s != "unknown"}
    if total_wins >= 3 and top2_sector_wins / total_wins >= 0.60 and len(unique_sectors) <= 3:
        new_scope = "sector"
        scope_list = [s for s, _ in top_sectors[:2] if s != "unknown"]
        if scope_list:
            pat.ticker_scope = new_scope
            pat.scope_tickers = json.dumps(scope_list)
            return new_scope

    pat.ticker_scope = "universal"
    pat.scope_tickers = None
    return "universal"


# ── Entry condition mutation operators ─────────────────────────────

_COMPLEMENTARY_POOL: list[dict[str, Any]] = [
    {"indicator": "rsi_14", "op": ">", "value": 50},
    {"indicator": "rsi_14", "op": "<", "value": 40},
    {"indicator": "adx", "op": ">", "value": 25},
    {"indicator": "macd_histogram", "op": ">", "value": 0},
    {"indicator": "volume_ratio", "op": ">=", "value": 2.0},
    {"indicator": "price", "op": ">", "ref": "ema_20"},
    {"indicator": "price", "op": ">", "ref": "ema_50"},
    {"indicator": "price", "op": ">", "ref": "sma_50"},
    {"indicator": "bb_squeeze", "op": "==", "value": True},
    {"indicator": "daily_change_pct", "op": ">=", "value": 3.0},
    {"indicator": "gap_pct", "op": ">", "value": 2.0},
    {"indicator": "stochastic_k", "op": "<", "value": 30},
    {"indicator": "bb_pct", "op": "<", "value": 0.2},
    {"indicator": "ema_stack", "op": "==", "value": True},
    {"indicator": "resistance_retests", "op": ">=", "value": 2,
     "params": {"tolerance_pct": 1.5, "lookback": 20}},
]


def _tweak_threshold(cond: dict[str, Any]) -> dict[str, Any] | None:
    """Shift a numeric threshold by +/-10-20%.  Returns None for non-numeric."""
    import random
    mutated = dict(cond)
    val = mutated.get("value")
    if mutated.get("ref"):
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        direction = random.choice([-1, 1])
        pct = random.uniform(0.10, 0.20) * direction
        new_val = round(val * (1 + pct), 2) if val != 0 else round(random.uniform(-5, 5), 2)
        mutated["value"] = new_val
        return mutated
    if isinstance(val, list) and len(val) == 2:
        idx = random.randint(0, 1)
        direction = random.choice([-1, 1])
        pct = random.uniform(0.10, 0.20) * direction
        new_list = list(val)
        old = float(new_list[idx])
        new_list[idx] = round(old * (1 + pct), 2) if old != 0 else round(random.uniform(-5, 5), 2)
        if new_list[0] > new_list[1]:
            new_list[0], new_list[1] = new_list[1], new_list[0]
        mutated["value"] = new_list
        return mutated
    return None


_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "tolerance_pct": (0.5, 5.0),
    "lookback": (5, 60),
}


def _tweak_params(cond: dict[str, Any]) -> dict[str, Any] | None:
    """Shift a random numeric param (e.g. tolerance_pct, lookback) by +/-10-25%.

    Returns None when the condition has no tweakable params.
    """
    import random
    params = cond.get("params")
    if not params or not isinstance(params, dict):
        return None
    numeric_keys = [k for k, v in params.items() if isinstance(v, (int, float))]
    if not numeric_keys:
        return None
    key = random.choice(numeric_keys)
    old_val = params[key]
    direction = random.choice([-1, 1])
    pct = random.uniform(0.10, 0.25) * direction
    new_val = round(old_val * (1 + pct), 2) if old_val != 0 else round(random.uniform(1, 10), 2)
    lo, hi = _PARAM_BOUNDS.get(key, (None, None))
    if lo is not None:
        new_val = max(lo, new_val)
    if hi is not None:
        new_val = min(hi, new_val)
    if isinstance(old_val, int):
        new_val = int(round(new_val))
    mutated = dict(cond)
    mutated["params"] = {**params, key: new_val}
    return mutated


def _find_weakest_condition(
    conditions: list[dict[str, Any]],
    loss_report: dict[str, Any] | None,
) -> int | None:
    """Return index of the condition most likely causing losses.

    Uses ``condition_pass_rates`` from the loss report: a condition that
    passes on nearly ALL losing tickers is not filtering losers out, so it
    is the weakest link.  Conversely a condition that rarely passes is too
    strict (but at least it keeps losers out).
    """
    if not loss_report or not conditions:
        return None
    pass_rates = loss_report.get("condition_pass_rates", {})
    if not pass_rates:
        return None

    worst_idx, worst_rate = None, -1.0
    for i, cond in enumerate(conditions):
        key = f"{cond.get('indicator', '')}{cond.get('op', '')}{cond.get('value', '')}"
        rate = pass_rates.get(key, 0.5)
        if rate > worst_rate:
            worst_rate = rate
            worst_idx = i
    return worst_idx


def _pick_complementary_condition(
    existing: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select a random indicator condition not already present."""
    import random
    existing_inds = {c.get("indicator") for c in existing}
    candidates = [c for c in _COMPLEMENTARY_POOL if c["indicator"] not in existing_inds]
    if not candidates:
        return None
    import copy
    picked = copy.deepcopy(random.choice(candidates))
    return picked


def _cross_breed_condition(
    db: "Session",
    existing: list[dict[str, Any]],
    exclude_pattern_id: int | None = None,
) -> dict[str, Any] | None:
    """Grab a condition from a high-performing unrelated pattern."""
    import random
    from ...models.trading import ScanPattern

    top_patterns = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.rules_json.isnot(None),
            ScanPattern.win_rate > 0.5,
        )
        .order_by(ScanPattern.win_rate.desc())
        .limit(20)
        .all()
    )
    existing_inds = {c.get("indicator") for c in existing}
    for _ in range(10):
        if not top_patterns:
            break
        donor = random.choice(top_patterns)
        if donor.id == exclude_pattern_id:
            continue
        try:
            rules = json.loads(donor.rules_json)
            donor_conds = rules.get("conditions", [])
        except (json.JSONDecodeError, TypeError):
            continue
        novel = [c for c in donor_conds if c.get("indicator") not in existing_inds]
        if novel:
            return dict(random.choice(novel))
    return None


def _mutate_entry_conditions(
    conditions: list[dict[str, Any]],
    loss_report: dict[str, Any] | None = None,
    db: "Session | None" = None,
    pattern_id: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Produce a mutated copy of entry conditions, guided by loss analysis.

    Returns ``(new_conditions, mutation_label)`` describing what changed.
    """
    import random
    if not conditions:
        return list(conditions), "no-op"

    conds = [dict(c) for c in conditions]

    weakest = _find_weakest_condition(conds, loss_report)

    has_params = any(c.get("params") for c in conds)
    ops = ["tweak_threshold", "remove_weakest", "add_complementary"]
    if has_params:
        ops.append("tweak_params")
    if db:
        ops.append("cross_breed")
    if weakest is None:
        ops = [o for o in ops if o != "remove_weakest"]
    if len(conds) <= 1:
        ops = [o for o in ops if o != "remove_weakest"]

    op = random.choice(ops)

    if op == "tweak_threshold":
        numeric_idxs = [
            i for i, c in enumerate(conds)
            if isinstance(c.get("value"), (int, float, list)) and not isinstance(c.get("value"), bool) and not c.get("ref")
        ]
        if numeric_idxs:
            idx = random.choice(numeric_idxs)
            tweaked = _tweak_threshold(conds[idx])
            if tweaked:
                old_val = conds[idx].get("value")
                conds[idx] = tweaked
                ind = tweaked.get("indicator", "?")
                return conds, f"tweak-{ind}-{old_val}->{tweaked['value']}"
        op = "add_complementary"

    if op == "tweak_params":
        param_idxs = [i for i, c in enumerate(conds) if c.get("params")]
        if param_idxs:
            idx = random.choice(param_idxs)
            tweaked = _tweak_params(conds[idx])
            if tweaked:
                old_params = conds[idx].get("params", {})
                conds[idx] = tweaked
                ind = tweaked.get("indicator", "?")
                diff = {k: tweaked["params"][k] for k in tweaked["params"]
                        if tweaked["params"][k] != old_params.get(k)}
                diff_str = ",".join(f"{k}={v}" for k, v in diff.items())
                return conds, f"tweak-params-{ind}-{diff_str}"
        op = "tweak_threshold"

    if op == "remove_weakest" and weakest is not None and len(conds) > 1:
        removed = conds.pop(weakest)
        return conds, f"drop-{removed.get('indicator', '?')}"

    if op == "cross_breed" and db:
        new_cond = _cross_breed_condition(db, conds, exclude_pattern_id=pattern_id)
        if new_cond:
            conds.append(new_cond)
            return conds, f"cross-{new_cond.get('indicator', '?')}"

    if op == "add_complementary" or True:
        new_cond = _pick_complementary_condition(conds)
        if new_cond:
            conds.append(new_cond)
            return conds, f"add-{new_cond.get('indicator', '?')}"

    return conds, "no-op"


# ── Pattern evolution ──────────────────────────────────────────────

_EXIT_VARIANTS: list[dict[str, Any]] = [
    {
        "label": "No-BOS-breakout",
        "config": {
            "use_bos": False, "atr_mult": 3.0, "max_bars": 50,
            "bos_buffer_pct": 0, "bos_grace_bars": 0,
        },
    },
    {
        "label": "BOS-tight",
        "config": {
            "use_bos": True, "atr_mult": 2.0, "max_bars": 25,
            "bos_buffer_pct": 0.003, "bos_grace_bars": 3,
        },
    },
    {
        "label": "BOS-moderate",
        "config": {
            "use_bos": True, "atr_mult": 2.5, "max_bars": 35,
            "bos_buffer_pct": 0.008, "bos_grace_bars": 4,
        },
    },
    {
        "label": "BOS-wide",
        "config": {
            "use_bos": True, "atr_mult": 3.0, "max_bars": 50,
            "bos_buffer_pct": 0.015, "bos_grace_bars": 6,
        },
    },
]

_MAX_ACTIVE_VARIANTS = 8


def _create_variant_child(
    db: "Session",
    parent: "ScanPattern",
    *,
    origin: str,
    variant_label: str,
    rules_json: str | None = None,
    exit_config_json: str | None = None,
    timeframe: str | None = None,
    ticker_scope: str | None = None,
    scope_tickers_json: str | None = None,
) -> "ScanPattern":
    """Low-level helper: create a child ScanPattern + linked TradingInsight."""
    from ...models.trading import ScanPattern, TradingInsight

    child_name = f"{parent.name} [{variant_label}]"
    child = ScanPattern(
        name=child_name,
        description=parent.description,
        rules_json=rules_json or parent.rules_json,
        origin=origin,
        asset_class=parent.asset_class,
        timeframe=timeframe or getattr(parent, "timeframe", "1d") or "1d",
        confidence=parent.confidence,
        evidence_count=0,
        backtest_count=0,
        score_boost=0.0,
        min_base_score=parent.min_base_score,
        active=True,
        parent_id=parent.id,
        exit_config=exit_config_json or parent.exit_config,
        variant_label=variant_label,
        generation=(parent.generation or 0) + 1,
        ticker_scope=ticker_scope or getattr(parent, "ticker_scope", "universal") or "universal",
        scope_tickers=scope_tickers_json if scope_tickers_json is not None else getattr(parent, "scope_tickers", None),
        hypothesis_family=getattr(parent, "hypothesis_family", None),
    )
    db.add(child)
    db.flush()

    user_ids = [
        r[0] for r in db.query(TradingInsight.user_id)
        .filter(
            TradingInsight.pattern_description.like(f"{parent.name}%"),
            TradingInsight.active.is_(True),
        )
        .distinct()
        .all()
    ]
    if not user_ids:
        user_ids = [None]
    for uid in user_ids:
        db.add(TradingInsight(
            user_id=uid,
            scan_pattern_id=child.id,
            pattern_description=f"{child_name} — {parent.description or ''}",
            hypothesis_family=getattr(parent, "hypothesis_family", None),
            confidence=parent.confidence,
            evidence_count=0,
            win_count=0,
            loss_count=0,
            active=True,
        ))
    return child


def fork_exit_variants(db: Session, pattern_id: int) -> list[int]:
    """Clone *pattern_id* into exit-strategy variant children."""
    from ...models.trading import ScanPattern

    parent = db.query(ScanPattern).get(pattern_id)
    if not parent or not parent.active:
        return []

    existing_children = (
        db.query(ScanPattern)
        .filter(ScanPattern.parent_id == pattern_id, ScanPattern.active.is_(True))
        .count()
    )
    if existing_children >= _MAX_ACTIVE_VARIANTS:
        return []

    slots = _MAX_ACTIVE_VARIANTS - existing_children
    existing_labels = set(
        r[0] for r in db.query(ScanPattern.variant_label)
        .filter(ScanPattern.parent_id == pattern_id)
        .all() if r[0]
    )

    created_ids: list[int] = []
    for variant in _EXIT_VARIANTS:
        if len(created_ids) >= slots:
            break
        if variant["label"] in existing_labels:
            continue
        child = _create_variant_child(
            db, parent,
            origin="exit_variant",
            variant_label=variant["label"],
            exit_config_json=json.dumps(variant["config"]),
        )
        created_ids.append(child.id)
        logger.info(
            "[learning] Forked exit variant: %s (parent=%d, gen=%d)",
            child.name, parent.id, child.generation,
        )

    if created_ids:
        db.commit()
    return created_ids


def fork_entry_variants(
    db: Session,
    pattern_id: int,
    loss_report: dict[str, Any] | None = None,
    max_variants: int = 3,
) -> list[int]:
    """Create entry-condition variants by mutating indicator thresholds."""
    from ...models.trading import ScanPattern

    parent = db.query(ScanPattern).get(pattern_id)
    if not parent or not parent.active or not parent.rules_json:
        return []

    existing_children = (
        db.query(ScanPattern)
        .filter(ScanPattern.parent_id == pattern_id, ScanPattern.active.is_(True))
        .count()
    )
    if existing_children >= _MAX_ACTIVE_VARIANTS:
        return []

    try:
        rules = json.loads(parent.rules_json)
        conditions = rules.get("conditions", [])
    except (json.JSONDecodeError, TypeError):
        return []
    if not conditions:
        return []

    slots = min(max_variants, _MAX_ACTIVE_VARIANTS - existing_children)
    existing_labels = set(
        r[0] for r in db.query(ScanPattern.variant_label)
        .filter(ScanPattern.parent_id == pattern_id)
        .all() if r[0]
    )

    created_ids: list[int] = []
    attempts = 0
    while len(created_ids) < slots and attempts < slots * 3:
        attempts += 1
        new_conds, label = _mutate_entry_conditions(
            conditions, loss_report=loss_report, db=db, pattern_id=pattern_id,
        )
        if label == "no-op":
            continue
        short_label = f"entry-{label}"[:40]
        if short_label in existing_labels:
            continue

        new_rules = json.dumps({"conditions": new_conds})
        child = _create_variant_child(
            db, parent,
            origin="entry_variant",
            variant_label=short_label,
            rules_json=new_rules,
        )
        existing_labels.add(short_label)
        created_ids.append(child.id)
        logger.info(
            "[learning] Forked entry variant: %s (parent=%d, gen=%d)",
            child.name, parent.id, child.generation,
        )

    if created_ids:
        db.commit()
    return created_ids


def fork_combo_variants(db: Session, pattern_id: int) -> list[int]:
    """Create a cross-bred variant by grafting a condition from a top pattern."""
    from ...models.trading import ScanPattern

    parent = db.query(ScanPattern).get(pattern_id)
    if not parent or not parent.active or not parent.rules_json:
        return []

    existing_children = (
        db.query(ScanPattern)
        .filter(ScanPattern.parent_id == pattern_id, ScanPattern.active.is_(True))
        .count()
    )
    if existing_children >= _MAX_ACTIVE_VARIANTS:
        return []

    try:
        rules = json.loads(parent.rules_json)
        conditions = rules.get("conditions", [])
    except (json.JSONDecodeError, TypeError):
        return []

    donor_cond = _cross_breed_condition(db, conditions, exclude_pattern_id=pattern_id)
    if not donor_cond:
        return []

    label = f"cross-{donor_cond.get('indicator', '?')}"[:40]
    existing_labels = set(
        r[0] for r in db.query(ScanPattern.variant_label)
        .filter(ScanPattern.parent_id == pattern_id)
        .all() if r[0]
    )
    if label in existing_labels:
        return []

    new_conds = [dict(c) for c in conditions] + [donor_cond]
    new_rules = json.dumps({"conditions": new_conds})
    child = _create_variant_child(
        db, parent,
        origin="combo_variant",
        variant_label=label,
        rules_json=new_rules,
    )
    db.commit()
    logger.info(
        "[learning] Forked combo variant: %s (parent=%d, gen=%d)",
        child.name, parent.id, child.generation,
    )
    return [child.id]


_TIMEFRAME_POOL = ["1m", "5m", "15m", "1h", "4h", "1d"]

_TIMEFRAME_ADJACENCY: dict[str, list[str]] = {
    "1m":  ["5m", "15m"],
    "5m":  ["1m", "15m", "1h"],
    "15m": ["5m", "1h", "4h"],
    "1h":  ["15m", "4h", "1d"],
    "4h":  ["1h", "1d", "15m"],
    "1d":  ["4h", "1h", "15m"],
}


def fork_timeframe_variants(
    db: Session,
    pattern_id: int,
    max_variants: int = 2,
) -> list[int]:
    """Create children that test the same pattern on different timeframes.

    A breakout pattern discovered on daily might work even better on 4h, 1h,
    or even 5m.  Prioritizes adjacent timeframes first (e.g. 1d -> 4h, 1h)
    then explores further out.  All timeframes are available for any asset.
    """
    from ...models.trading import ScanPattern
    import random

    parent = db.query(ScanPattern).get(pattern_id)
    if not parent or not parent.active:
        return []

    existing_children = (
        db.query(ScanPattern)
        .filter(ScanPattern.parent_id == pattern_id, ScanPattern.active.is_(True))
        .count()
    )
    if existing_children >= _MAX_ACTIVE_VARIANTS:
        return []

    parent_tf = getattr(parent, "timeframe", "1d") or "1d"
    existing_labels = set(
        r[0] for r in db.query(ScanPattern.variant_label)
        .filter(ScanPattern.parent_id == pattern_id)
        .all() if r[0]
    )

    adjacent = _TIMEFRAME_ADJACENCY.get(parent_tf, [])
    remaining = [tf for tf in _TIMEFRAME_POOL if tf != parent_tf and tf not in adjacent]
    random.shuffle(remaining)
    candidate_tfs = list(adjacent) + remaining

    slots = min(max_variants, _MAX_ACTIVE_VARIANTS - existing_children)

    created_ids: list[int] = []
    for tf in candidate_tfs:
        if len(created_ids) >= slots:
            break
        label = f"tf-{tf}"
        if label in existing_labels:
            continue
        child = _create_variant_child(
            db, parent,
            origin="tf_variant",
            variant_label=label,
            timeframe=tf,
        )
        existing_labels.add(label)
        created_ids.append(child.id)
        logger.info(
            "[learning] Forked timeframe variant: %s (%s -> %s, parent=%d, gen=%d)",
            child.name, parent_tf, tf, parent.id, child.generation,
        )

    if created_ids:
        db.commit()
    return created_ids


def fork_scope_variants(
    db: Session,
    pattern_id: int,
    max_variants: int = 2,
) -> list[int]:
    """Create children that test the same pattern with narrower or broader
    ticker scope.

    From a universal parent:  create sector + ticker_specific children
    based on the best-performing sectors/tickers from backtests.
    From a ticker_specific parent:  create a universal child to test if
    the pattern generalises.
    """
    from ...models.trading import ScanPattern, TradingInsight, BacktestResult
    from .backtest_engine import TICKER_TO_SECTOR
    import random

    parent = db.query(ScanPattern).get(pattern_id)
    if not parent or not parent.active:
        return []

    existing_children = (
        db.query(ScanPattern)
        .filter(ScanPattern.parent_id == pattern_id, ScanPattern.active.is_(True))
        .count()
    )
    if existing_children >= _MAX_ACTIVE_VARIANTS:
        return []

    existing_labels = set(
        r[0] for r in db.query(ScanPattern.variant_label)
        .filter(ScanPattern.parent_id == pattern_id)
        .all() if r[0]
    )

    insight_ids = [
        r[0] for r in db.query(TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id == pattern_id)
        .all()
    ]
    winners: list[str] = []
    if insight_ids:
        winners = [
            r[0] for r in db.query(BacktestResult.ticker)
            .filter(
                BacktestResult.related_insight_id.in_(insight_ids),
                BacktestResult.trade_count > 0,
                BacktestResult.return_pct > 0,
            )
            .all()
            if r[0]
        ]

    parent_scope = getattr(parent, "ticker_scope", "universal") or "universal"
    slots = min(max_variants, _MAX_ACTIVE_VARIANTS - existing_children)
    created_ids: list[int] = []

    if parent_scope == "universal" and winners:
        sector_counts: dict[str, int] = {}
        ticker_counts: dict[str, int] = {}
        for t in winners:
            ticker_counts[t] = ticker_counts.get(t, 0) + 1
            s = TICKER_TO_SECTOR.get(t, "unknown")
            if s != "unknown":
                sector_counts[s] = sector_counts.get(s, 0) + 1

        if sector_counts and len(created_ids) < slots:
            top_sector = max(sector_counts, key=sector_counts.get)
            label = f"scope-sector-{top_sector}"[:40]
            if label not in existing_labels:
                child = _create_variant_child(
                    db, parent,
                    origin="scope_variant",
                    variant_label=label,
                    ticker_scope="sector",
                    scope_tickers_json=json.dumps([top_sector]),
                )
                existing_labels.add(label)
                created_ids.append(child.id)
                logger.info(
                    "[learning] Forked scope variant: %s (universal->sector:%s, parent=%d)",
                    child.name, top_sector, parent.id,
                )

        if ticker_counts and len(created_ids) < slots:
            top_tickers = sorted(ticker_counts, key=ticker_counts.get, reverse=True)[:5]
            label = f"scope-tickers-{','.join(top_tickers[:3])}"[:40]
            if label not in existing_labels:
                child = _create_variant_child(
                    db, parent,
                    origin="scope_variant",
                    variant_label=label,
                    ticker_scope="ticker_specific",
                    scope_tickers_json=json.dumps(top_tickers),
                )
                existing_labels.add(label)
                created_ids.append(child.id)
                logger.info(
                    "[learning] Forked scope variant: %s (universal->tickers:%s, parent=%d)",
                    child.name, top_tickers, parent.id,
                )

    elif parent_scope in ("ticker_specific", "sector") and len(created_ids) < slots:
        label = "scope-universal"
        if label not in existing_labels:
            child = _create_variant_child(
                db, parent,
                origin="scope_variant",
                variant_label=label,
                ticker_scope="universal",
                scope_tickers_json=None,
            )
            existing_labels.add(label)
            created_ids.append(child.id)
            logger.info(
                "[learning] Forked scope variant: %s (%s->universal, parent=%d)",
                child.name, parent_scope, parent.id,
            )

    if created_ids:
        db.commit()
    return created_ids


def fork_pattern_variants(
    db: Session,
    pattern_id: int,
    loss_report: dict[str, Any] | None = None,
) -> list[int]:
    """Fork entry, exit, combo, timeframe, AND scope variants for a pattern.

    Orchestrates all five axes of evolution.
    """
    created: list[int] = []
    created.extend(fork_exit_variants(db, pattern_id))
    created.extend(fork_entry_variants(db, pattern_id, loss_report=loss_report))
    created.extend(fork_combo_variants(db, pattern_id))
    created.extend(fork_timeframe_variants(db, pattern_id))
    created.extend(fork_scope_variants(db, pattern_id))
    return created


def _mutate_exit_config(config: dict[str, Any]) -> dict[str, Any]:
    """Create a small random mutation of an exit config."""
    import random
    mutated = dict(config)

    atr = mutated.get("atr_mult", 2.0)
    mutated["atr_mult"] = round(atr * random.uniform(0.85, 1.15), 2)
    mutated["atr_mult"] = max(1.0, min(5.0, mutated["atr_mult"]))

    max_bars = mutated.get("max_bars", 25)
    mutated["max_bars"] = max(10, min(80, max_bars + random.randint(-5, 5)))

    if mutated.get("use_bos"):
        buf = mutated.get("bos_buffer_pct", 0.003)
        mutated["bos_buffer_pct"] = round(
            max(0.001, min(0.03, buf * random.uniform(0.8, 1.2))), 4
        )
        grace = mutated.get("bos_grace_bars", 3)
        mutated["bos_grace_bars"] = max(2, min(10, grace + random.randint(-1, 1)))

    return mutated


def _analyze_variant_losses(
    db: Session,
    pattern_id: int,
) -> dict[str, Any] | None:
    """Analyze losing BacktestResult records for a pattern to identify failure patterns.

    Returns a ``loss_report`` dict with:
    - ``losing_tickers``: list of tickers where the pattern lost money
    - ``condition_pass_rates``: for each condition, the fraction of losing
      tickers where it was met (high = not filtering losers = weak)
    - ``sector_losses``: count of losses by sector
    - ``avg_losing_return``: average return among losing backtests
    """
    from ...models.trading import ScanPattern, TradingInsight, BacktestResult

    pattern = db.query(ScanPattern).get(pattern_id)
    if not pattern or not pattern.rules_json:
        return None

    insight_ids = [
        r[0] for r in db.query(TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id == pattern_id)
        .all()
    ]
    if not insight_ids:
        return None

    losing_bts = (
        db.query(BacktestResult.ticker, BacktestResult.return_pct)
        .filter(
            BacktestResult.related_insight_id.in_(insight_ids),
            BacktestResult.trade_count > 0,
            BacktestResult.return_pct < 0,
        )
        .all()
    )
    if len(losing_bts) < 3:
        return None

    losing_tickers = list({bt[0] for bt in losing_bts})
    avg_losing_return = round(
        sum(bt[1] for bt in losing_bts) / len(losing_bts), 2
    )

    from .backtest_engine import SECTOR_TICKERS
    ticker_to_sector: dict[str, str] = {}
    for sector, tickers in SECTOR_TICKERS.items():
        for t in tickers:
            ticker_to_sector[t] = sector

    sector_losses: dict[str, int] = {}
    for t in losing_tickers:
        sec = ticker_to_sector.get(t, "other")
        sector_losses[sec] = sector_losses.get(sec, 0) + 1

    try:
        rules = json.loads(pattern.rules_json)
        conditions = rules.get("conditions", [])
    except (json.JSONDecodeError, TypeError):
        conditions = []

    condition_pass_rates: dict[str, float] = {}
    if conditions:
        for cond in conditions:
            key = f"{cond.get('indicator', '')}{cond.get('op', '')}{cond.get('value', '')}"
            condition_pass_rates[key] = 0.5

    report: dict[str, Any] = {
        "losing_tickers": losing_tickers[:30],
        "condition_pass_rates": condition_pass_rates,
        "sector_losses": sector_losses,
        "avg_losing_return": avg_losing_return,
        "total_losses": len(losing_bts),
    }

    try:
        log_learning_event(
            db, None, "loss_analysis",
            f"Loss analysis for '{pattern.name}': {len(losing_bts)} losing backtests, "
            f"avg return {avg_losing_return}%, sectors={sector_losses}",
        )
    except Exception:
        logger.debug("[learning] _analyze_variant_losses: non-critical operation failed", exc_info=True)

    return report



def _get_evolution_insights(db: Session) -> dict[str, Any]:
    """Gather insights from specialized mining to guide evolution.
    
    Returns a dict with:
    - synergies: list of signal combo insights (for combo variant creation)
    - fakeouts: list of fakeout pattern insights (for variant penalization)
    - timeframe_perf: dict of asset_type -> best_timeframe (for timeframe variants)
    - exit_tweaks: list of exit optimization insights (for exit variants)
    """
    result = {
        "synergies": [],
        "fakeouts": [],
        "timeframe_perf": {},
        "exit_tweaks": [],
    }
    
    try:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=30)
        
        recent_insights = (
            db.query(TradingInsight)
            .filter(
                TradingInsight.active.is_(True),
                TradingInsight.confidence >= 0.4,
                TradingInsight.created_at >= cutoff,
            )
            .all()
        )
        
        for insight in recent_insights:
            desc = (insight.pattern_description or "").lower()
            
            if "synergy" in desc or "combo" in desc or "+" in desc[:50]:
                result["synergies"].append({
                    "id": insight.id,
                    "description": insight.pattern_description,
                    "confidence": insight.confidence,
                })
            elif "fakeout" in desc or "false break" in desc:
                result["fakeouts"].append({
                    "id": insight.id,
                    "description": insight.pattern_description,
                    "confidence": insight.confidence,
                })
            elif "timeframe" in desc and "achieves" in desc:
                if "stock" in desc:
                    if "1d" in desc:
                        result["timeframe_perf"]["stocks"] = "1d"
                    elif "15m" in desc or "intraday" in desc:
                        result["timeframe_perf"]["stocks"] = "15m"
                elif "crypto" in desc:
                    if "1d" in desc:
                        result["timeframe_perf"]["crypto"] = "1d"
                    elif "4h" in desc:
                        result["timeframe_perf"]["crypto"] = "4h"
            elif "exit" in desc and ("atr" in desc or "stop" in desc or "target" in desc):
                result["exit_tweaks"].append({
                    "id": insight.id,
                    "description": insight.pattern_description,
                    "confidence": insight.confidence,
                })
        
        logger.info(
            "[evolution] Gathered %d synergies, %d fakeouts, %d timeframe prefs, %d exit tweaks from insights",
            len(result["synergies"]), len(result["fakeouts"]),
            len(result["timeframe_perf"]), len(result["exit_tweaks"]),
        )
    except Exception as e:
        logger.warning("[evolution] Failed to gather insights: %s", e)
    
    return result


def evolve_pattern_strategies(db: Session) -> dict[str, Any]:
    """Full evolutionary loop: entry, exit, AND combo variants.

    1. **Fork** — root patterns without children get initial variants
       across all three axes (exit, entry, combo).
    2. **Compare** — variants with enough backtests are ranked; the best
       is promoted and underperformers are deactivated.
    3. **Loss analysis** — inspect why the worst variants lost.
    4. **Guided mutate** — use loss reports to guide next-gen mutations
       instead of pure randomness.
    5. **Journal** — log rich LearningEvents summarising what was learned.

    Returns summary stats for logging.
    """
    from ...config import settings as _evo_settings
    from ...models.trading import (
        ScanPattern, TradingInsight, BacktestResult, LearningEvent,
    )
    from .evolution_objective import compute_variant_fitness

    stats: dict[str, Any] = {
        "forked_exit": 0, "forked_entry": 0, "forked_combo": 0,
        "promoted": 0, "deactivated": 0,
        "mutated_exit": 0, "mutated_entry": 0,
        "loss_reports": 0,
        "insights_consumed": 0,
    }

    # Gather insights from specialized mining to guide evolution
    evo_insights = _get_evolution_insights(db)
    fakeout_patterns = {i["description"][:100].lower() for i in evo_insights.get("fakeouts", [])}
    synergy_combos = evo_insights.get("synergies", [])
    preferred_timeframes = evo_insights.get("timeframe_perf", {})

    # ── 1. Fork phase ────────────────────────────────────────────────
    root_patterns = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.parent_id.is_(None),
            ScanPattern.origin.notin_(["exit_variant", "entry_variant", "combo_variant", "tf_variant", "scope_variant"]),
        )
        .all()
    )

    _root_ids = [rp.id for rp in root_patterns]
    _child_counts_q = (
        db.query(ScanPattern.parent_id, func.count(ScanPattern.id))
        .filter(ScanPattern.parent_id.in_(_root_ids), ScanPattern.active.is_(True))
        .group_by(ScanPattern.parent_id)
        .all()
    ) if _root_ids else []
    _child_count_map = {pid: cnt for pid, cnt in _child_counts_q}

    for rp in root_patterns:
        child_count = _child_count_map.get(rp.id, 0)
        if child_count == 0:
            exit_ids = fork_exit_variants(db, rp.id)
            stats["forked_exit"] += len(exit_ids)
            entry_ids = fork_entry_variants(db, rp.id, max_variants=2)
            stats["forked_entry"] += len(entry_ids)
            combo_ids = fork_combo_variants(db, rp.id)
            stats["forked_combo"] += len(combo_ids)
            tf_ids = fork_timeframe_variants(db, rp.id, max_variants=2)
            stats["forked_tf"] = stats.get("forked_tf", 0) + len(tf_ids)
            scope_ids = fork_scope_variants(db, rp.id, max_variants=2)
            stats["forked_scope"] = stats.get("forked_scope", 0) + len(scope_ids)

    # ── 2. Compare phase ─────────────────────────────────────────────
    parents_with_children = (
        db.query(ScanPattern.parent_id)
        .filter(ScanPattern.parent_id.isnot(None), ScanPattern.active.is_(True))
        .distinct()
        .all()
    )

    min_bt_per_variant = max(1, int(getattr(_evo_settings, "brain_evolution_min_trades", 5)))
    loss_reports_by_parent: dict[int, dict[str, Any]] = {}

    for (parent_id,) in parents_with_children:
        parent = db.query(ScanPattern).get(parent_id)
        if not parent:
            continue

        siblings = (
            db.query(ScanPattern)
            .filter(ScanPattern.parent_id == parent_id, ScanPattern.active.is_(True))
            .all()
        )
        if len(siblings) < 2:
            continue

        variant_scores: list[tuple] = []

        for sib in siblings:
            bts = (
                db.query(BacktestResult)
                .join(
                    TradingInsight,
                    TradingInsight.id == BacktestResult.related_insight_id,
                )
                .filter(
                    TradingInsight.scan_pattern_id == sib.id,
                    BacktestResult.trade_count > 0,
                )
                .all()
            )
            if not bts:
                bts = (
                    db.query(BacktestResult)
                    .join(
                        TradingInsight,
                        TradingInsight.id == BacktestResult.related_insight_id,
                    )
                    .filter(
                        TradingInsight.pattern_description.like(f"{sib.name}%"),
                        BacktestResult.trade_count > 0,
                    )
                    .all()
                )
            if len(bts) < min_bt_per_variant:
                variant_scores = []
                break

            sharpes = [bt.sharpe for bt in bts if bt.sharpe is not None]
            avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0
            wins = sum(1 for bt in bts if (bt.return_pct or 0) > 0)
            wr = wins / len(bts) if bts else 0
            
            # Apply insight-based adjustments
            insight_adj = 0.0
            sib_name_lower = (sib.name or "").lower()
            sib_label_lower = (sib.variant_label or "").lower()
            
            # Penalize variants that match known fakeout patterns
            for fakeout_desc in fakeout_patterns:
                if any(k in sib_name_lower or k in sib_label_lower 
                       for k in ["low_vol", "rejection", "false"] if k in fakeout_desc):
                    insight_adj -= 0.2
                    stats["insights_consumed"] = stats.get("insights_consumed", 0) + 1
                    break
            
            # Boost combo variants matching synergy insights
            if sib.origin == "combo_variant" and synergy_combos:
                sib_rules = sib.rules_json or ""
                for syn in synergy_combos:
                    syn_desc = (syn.get("description") or "").lower()
                    if ("rsi" in syn_desc and "rsi" in sib_rules.lower()) or \
                       ("macd" in syn_desc and "macd" in sib_rules.lower()) or \
                       ("volume" in syn_desc and "vol" in sib_rules.lower()):
                        insight_adj += 0.15 * syn.get("confidence", 0.5)
                        stats["insights_consumed"] = stats.get("insights_consumed", 0) + 1
            
            adj_sharpe = avg_sharpe + insight_adj
            avg_ret = sum(float(bt.return_pct or 0) for bt in bts) / len(bts)
            fitness = compute_variant_fitness(
                adj_sharpe, wr, avg_ret, len(bts), settings=_evo_settings,
            )
            variant_scores.append((sib, fitness, adj_sharpe, wr, len(bts)))

        if not variant_scores or len(variant_scores) < 2:
            continue

        variant_scores.sort(key=lambda x: x[1], reverse=True)
        winner = variant_scores[0][0]
        winner_sharpe = variant_scores[0][2]
        winner_wr = variant_scores[0][3]

        if winner.origin == "exit_variant" and winner.exit_config:
            parent.exit_config = winner.exit_config
            parent.updated_at = datetime.utcnow()
            stats["promoted"] += 1
        elif winner.origin == "tf_variant" and winner.timeframe:
            parent.timeframe = winner.timeframe
            parent.updated_at = datetime.utcnow()
            stats["promoted"] += 1
        elif winner.origin == "scope_variant":
            parent.ticker_scope = getattr(winner, "ticker_scope", "universal") or "universal"
            parent.scope_tickers = getattr(winner, "scope_tickers", None)
            parent.updated_at = datetime.utcnow()
            stats["promoted"] += 1
        elif winner.origin in ("entry_variant", "combo_variant") and winner.rules_json:
            parent.rules_json = winner.rules_json
            parent.updated_at = datetime.utcnow()
            stats["promoted"] += 1

        logger.info(
            "[learning] Promoted variant '%s' (%s) → parent '%s' "
            "(sharpe=%.2f, wr=%.0f%%)",
            winner.variant_label, winner.origin, parent.name,
            winner_sharpe, winner_wr * 100,
        )

        # ── 3. Loss analysis for worst variant ───────────────────────
        worst = variant_scores[-1][0]
        worst_sharpe = variant_scores[-1][2]
        worst_wr = variant_scores[-1][3]
        loss_report = None
        if worst_wr < 0.5:
            loss_report = _analyze_variant_losses(db, worst.id)
            if loss_report:
                loss_reports_by_parent[parent_id] = loss_report
                stats["loss_reports"] += 1

        for loser, _l_fit, l_sharpe, l_wr, l_bt_count in variant_scores[1:]:
            if l_bt_count < 8:
                continue
            sharpe_gap = winner_sharpe - l_sharpe
            wr_gap = winner_wr - l_wr
            if sharpe_gap > 0.5 or wr_gap > 0.3:
                loser.active = False
                stats["deactivated"] += 1
                logger.info(
                    "[learning] Deactivated underperformer: '%s' (%s, sharpe=%.2f vs %.2f, wr=%.0f%% vs %.0f%%)",
                    loser.variant_label, loser.origin, l_sharpe, winner_sharpe,
                    l_wr * 100, winner_wr * 100,
                )

        try:
            evt = LearningEvent(
                user_id=None,
                event_type="pattern_evolution",
                description=(
                    f"Evolution for '{parent.name}': winner={winner.variant_label} "
                    f"({winner.origin}, sharpe={winner_sharpe:.2f}, wr={winner_wr:.0%}). "
                    f"Compared {len(variant_scores)} variants across exit/entry/combo/timeframe/scope axes."
                    + (f" Loss report: avg_losing_return={loss_report['avg_losing_return']}%, "
                       f"sectors={loss_report.get('sector_losses', {})}"
                       if loss_report else "")
                ),
                confidence_before=parent.confidence,
                confidence_after=parent.confidence,
            )
            db.add(evt)
        except Exception:
            logger.debug("[learning] evolve_pattern_strategies: non-critical operation failed", exc_info=True)

        # ── 4. Guided mutate phase ───────────────────────────────────
        active_children = (
            db.query(func.count(ScanPattern.id))
            .filter(ScanPattern.parent_id == parent_id, ScanPattern.active.is_(True))
            .scalar()
        )
        mutations_to_spawn = min(2, _MAX_ACTIVE_VARIANTS - active_children)

        if mutations_to_spawn > 0:
            parent_loss = loss_reports_by_parent.get(parent_id)

            # Exit mutation (if winner was exit-type or parent has exit_config)
            if winner.exit_config:
                try:
                    base_config = json.loads(winner.exit_config)
                except (json.JSONDecodeError, TypeError):
                    base_config = None
                if base_config:
                    mutated = _mutate_exit_config(base_config)
                    mut_label = (
                        f"mut-g{(winner.generation or 0) + 1}-"
                        f"atr{mutated['atr_mult']:.1f}"
                    )
                    existing = db.query(ScanPattern).filter(
                        ScanPattern.parent_id == parent_id,
                        ScanPattern.variant_label == mut_label,
                    ).first()
                    if not existing:
                        _create_variant_child(
                            db, parent,
                            origin="exit_variant",
                            variant_label=mut_label,
                            exit_config_json=json.dumps(mutated),
                        )
                        stats["mutated_exit"] += 1
                        mutations_to_spawn -= 1

            # Entry mutation guided by loss report
            if mutations_to_spawn > 0 and winner.rules_json:
                try:
                    w_rules = json.loads(winner.rules_json)
                    w_conds = w_rules.get("conditions", [])
                except (json.JSONDecodeError, TypeError):
                    w_conds = []
                if w_conds:
                    new_conds, label = _mutate_entry_conditions(
                        w_conds, loss_report=parent_loss, db=db,
                        pattern_id=parent_id,
                    )
                    if label != "no-op":
                        ent_label = f"guided-{label}"[:40]
                        existing = db.query(ScanPattern).filter(
                            ScanPattern.parent_id == parent_id,
                            ScanPattern.variant_label == ent_label,
                        ).first()
                        if not existing:
                            _create_variant_child(
                                db, parent,
                                origin="entry_variant",
                                variant_label=ent_label,
                                rules_json=json.dumps({"conditions": new_conds}),
                            )
                            stats["mutated_entry"] += 1
                            logger.info(
                                "[learning] Guided entry mutation: '%s' for '%s' "
                                "(loss_report=%s)",
                                ent_label, parent.name,
                                "yes" if parent_loss else "random",
                            )

    db.commit()
    logger.info("[learning] Pattern evolution complete: %s", stats)
    return stats


_CYCLE_UI_DIGEST_KEYS = frozenset({
    "prescreen_candidates", "tickers_scored", "snapshots_taken",
    "patterns_discovered", "patterns_boosted", "returns_backfilled",
    "queue_backtests_run", "backtests_run", "hypotheses_tested",
    "hypotheses_challenged", "proposals_generated", "signal_events", "ml_trained",
    "promoted_fast_eval", "data_provider", "elapsed_s", "interrupted", "error",
    "funnel_snapshot", "brain_resource_budget", "live_depromotion",
    "patterns_refined", "weights_evolved", "real_trade_adjustments",
    "queue_pending", "queue_empty",
})


def build_cycle_ui_digest(report: dict[str, Any]) -> dict[str, Any]:
    """Subset of learning report for cross-process Brain UI (JSON-serializable)."""
    out: dict[str, Any] = {"updated_at": datetime.utcnow().isoformat() + "Z"}
    for k in _CYCLE_UI_DIGEST_KEYS:
        if k in report and report[k] is not None:
            out[k] = report[k]
    return out


# Maintainer: Trading Brain Network tab + live status strings come from
# ``learning_cycle_architecture`` (single source of truth). Mesh topology lives in
# ``brain_graph_nodes`` / migrations; projection schema in
# ``brain_neural_mesh.projection.NEURAL_PROJECTION_SCHEMA_VERSION``.
# Use apply_learning_cycle_step_status(...); after each step commit call
# ``notify_learning_cycle_step_committed`` via ``_finish_lc_step`` in ``run_learning_cycle``.
# Each step must keep: # graph-node: cluster_id/step_sid (see learning_cycle_architecture).


def run_scheduled_market_snapshots(db: Session, user_id: int | None) -> dict[str, Any]:
    """Daily + intraday ``trading_snapshots`` writes for APScheduler (``brain_market_snapshots`` job)."""
    from ...config import settings as _snap_sched_settings
    from .brain_io_concurrency import io_workers_for_snapshot_batch
    from .scanner import build_snapshot_ticker_universe

    _sw = io_workers_for_snapshot_batch(_snap_sched_settings)
    logger.info(
        "[chili_brain_io] scheduled_market_snapshots_start universe_build=1 snapshot_workers=%s",
        _sw,
    )
    top_tickers, _drv = build_snapshot_ticker_universe(db, user_id)
    daily_count = take_snapshots_parallel(db, top_tickers, bar_interval="1d")
    intra_count = _take_intraday_crypto_snapshots(db, top_tickers)

    vitals_refresh: dict[str, Any] = {}
    try:
        from .setup_vitals import monitored_tickers_for_vitals, refresh_ticker_vitals_batch

        mon = set(monitored_tickers_for_vitals(db))
        uni = {str(x).strip().upper() for x in top_tickers}
        batch = sorted(mon & uni) if mon & uni else sorted(uni)[:40]
        vitals_refresh["1d"] = refresh_ticker_vitals_batch(db, batch, "1d")
        vitals_refresh["1h"] = refresh_ticker_vitals_batch(db, batch, "1h")
    except Exception as e:
        logger.warning("[learning] vitals refresh after snapshots failed: %s", e)
        vitals_refresh = {"error": str(e)}

    return {
        "ok": True,
        "snapshots_taken_daily": daily_count,
        "intraday_snapshots_taken": intra_count,
        "snapshots_taken": daily_count + intra_count,
        "universe_size": len(top_tickers),
        "snapshot_driver": _drv.get("snapshot_driver"),
        "tickers": top_tickers,
        "vitals_refresh": vitals_refresh,
    }


def run_learning_cycle(
    db: Session,
    user_id: int | None,
    full_universe: bool = True,
) -> dict[str, Any]:
    """Learning cycle: backfill -> mine -> backtest -> journal -> signals (no inline market snapshots).

    Prescreen, deep scan, and **market snapshots** are scheduler/source jobs
    (``run_scheduled_market_snapshots`` / ``brain_market_snapshots``). Snapshot counts in the
    cycle report are zeroed; the durable ``market_snapshots_batch`` outcome is emitted when the
    scheduler job completes.
    """
    from .journal import daily_market_journal, check_signal_events

    _shutting_down.clear()  # allow new cycle (e.g. after worker was stopped)
    if _learning_status["running"]:
        from ...config import settings
        stale_s = max(300, int(getattr(settings, "learning_cycle_stale_seconds", 10800)))
        started = _learning_status.get("started_at")
        if started:
            try:
                elapsed_s = (datetime.utcnow() - datetime.fromisoformat(started)).total_seconds()
                if elapsed_s <= stale_s:
                    return {"ok": False, "reason": "Learning cycle already in progress"}
                logger.warning(
                    "[learning] Clearing stale running flag (%.0fs > %ds cap)",
                    elapsed_s,
                    stale_s,
                )
            except Exception:
                return {"ok": False, "reason": "Learning cycle already in progress"}
        else:
            logger.warning("[learning] Clearing running flag without started_at")
        _learning_status["running"] = False
    if _shutting_down.is_set():
        return {"ok": False, "reason": "Server is shutting down"}

    _brain_lease_enforcement_ctx.clear()
    from ...config import settings as _lease_settings
    if getattr(_lease_settings, "brain_cycle_lease_enforcement_enabled", False):
        from ...trading_brain.infrastructure.lease_dedicated_session import (
            brain_lease_enforcement_log_peer_on_denial,
            brain_lease_enforcement_try_acquire_dedicated,
            brain_lease_holder_id,
        )

        _lease_ttl = max(
            60, int(getattr(_lease_settings, "learning_cycle_stale_seconds", 10800))
        )
        _hid = brain_lease_holder_id()
        try:
            _acquired_lease = brain_lease_enforcement_try_acquire_dedicated(
                holder_id=_hid,
                lease_seconds=_lease_ttl,
            )
        except Exception as e:
            logger.error(
                "[brain_lease_enforcement] lease_acquire_error scope=global: %s",
                e,
                exc_info=True,
            )
            return {"ok": False, "reason": "Learning cycle lease unavailable"}
        if not _acquired_lease:
            brain_lease_enforcement_log_peer_on_denial()
            return {"ok": False, "reason": "Learning cycle lease already held"}
        _brain_lease_enforcement_ctx["acquired"] = True
        _brain_lease_enforcement_ctx["holder_id"] = _hid

    _learning_status["running"] = True
    _learning_status["phase"] = "starting"
    _learning_status["correlation_id"] = str(uuid.uuid4())
    _learning_status["secondary_miners_skipped"] = False
    _learning_status["nodes_completed"] = 0
    _learning_status["clusters_completed"] = 0
    _learning_status["patterns_found"] = 0
    _learning_status["tickers_processed"] = 0
    _learning_status["started_at"] = datetime.utcnow().isoformat()
    _learning_status["step_timings"] = {}
    start = time.time()
    report: dict[str, Any] = {}
    cycle_budget = BrainResourceBudget.from_settings()

    _provider = (
        "Massive" if _use_massive() else
        "Polygon" if _use_polygon() else
        "yfinance"
    )
    _learning_status["data_provider"] = _provider
    logger.info(f"[learning] Starting learning cycle — primary data provider: {_provider}")
    try:
        from ...config import settings as _lc_io_settings
        from .brain_io_concurrency import (
            effective_cpu_budget,
            io_workers_for_predictions,
            io_workers_for_snapshot_batch,
            io_workers_high,
            io_workers_med,
        )

        logger.info(
            "[chili_brain_io] learning_cycle_start correlation_id=%s effective_cpus=%.1f "
            "snapshot_workers=%s prediction_workers=%s high=%s med=%s provider=%s",
            _learning_status.get("correlation_id"),
            effective_cpu_budget(_lc_io_settings),
            io_workers_for_snapshot_batch(_lc_io_settings),
            io_workers_for_predictions(_lc_io_settings),
            io_workers_high(_lc_io_settings),
            io_workers_med(_lc_io_settings),
            _provider,
        )
    except Exception:
        pass

    def _step_time(name: str, t0: float, extra: str = "") -> None:
        elapsed = round(time.time() - t0, 1)
        _learning_status["step_timings"][name] = elapsed
        suffix = f" | {extra}" if extra else ""
        logger.info(f"[learning] Step '{name}' took {elapsed}s{suffix}")

    def _commit_step() -> None:
        try:
            if _brain_lease_enforcement_ctx.get("acquired"):
                from ...config import settings as _ls
                from ...trading_brain.infrastructure.lease_dedicated_session import (
                    brain_lease_enforcement_refresh_soft_dedicated,
                )

                brain_lease_enforcement_refresh_soft_dedicated(
                    holder_id=str(_brain_lease_enforcement_ctx["holder_id"]),
                    lease_seconds=max(
                        60,
                        int(getattr(_ls, "learning_cycle_stale_seconds", 10800)),
                    ),
                )
            db.commit()
        except Exception as e:
            logger.warning("[learning] Step commit failed: %s", e)

    def _finish_lc_step(cluster_id: str, step_sid: str, step_started: float, extra: str = "") -> None:
        """Commit ORM work for one architecture step, then notify neural mesh (best-effort)."""
        _commit_step()
        try:
            from .brain_neural_mesh.publisher import notify_learning_cycle_step_committed

            notify_learning_cycle_step_committed(
                db,
                cluster_id=cluster_id,
                step_sid=step_sid,
                elapsed_sec=round(time.time() - float(step_started), 2),
                extra=extra or "",
                correlation_id=_learning_status.get("correlation_id"),
            )
        except Exception:
            logger.debug("[learning] mesh notify after step failed (ignored)", exc_info=True)

    interrupted = False
    report_error: str | None = None

    try:
        from ...config import settings

        _node_step = 0
        _cluster_done: set[str] = set()

        _total_nodes = sum(
            len(c.steps)
            for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
            if c.id != SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID
        )
        _total_clusters = sum(
            1 for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
            if c.id != SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID
        )
        _learning_status["total_nodes"] = _total_nodes
        _learning_status["total_clusters"] = _total_clusters

        def _bump_node(cluster_id: str | None = None) -> None:
            nonlocal _node_step
            _node_step += 1
            _learning_status["nodes_completed"] = _node_step
            if cluster_id and cluster_id not in _cluster_done:
                cluster_def = next(
                    (c for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS if c.id == cluster_id),
                    None,
                )
                if cluster_def:
                    cluster_sids = {s.sid for s in cluster_def.steps}
                    done_in_cluster = sum(
                        1 for k in _learning_status.get("step_timings", {})
                        if k in cluster_sids
                    )
                    if done_in_cluster >= len(cluster_sids):
                        _cluster_done.add(cluster_id)
                        _learning_status["clusters_completed"] = len(_cluster_done)

        def _finish_secondary_step(cluster_id: str, step_sid: str, step_started: float, extra: str = "") -> None:
            _finish_lc_step(cluster_id, step_sid, step_started, extra)

        # Prescreen + full scan are batch jobs (scheduler); hydrate report from DB only.
        _pre_updates, top_tickers, _ = load_prescreen_scan_and_universe(db, user_id)
        report.update(_pre_updates)
        _learning_status["tickers_processed"] = len(top_tickers)

        report["snapshots_taken_daily"] = 0
        report["intraday_snapshots_taken"] = 0
        report["snapshots_taken"] = 0

        # Step 3: Backfill future returns + predicted scores
        step_start = time.time()
        # graph-node: c_state/backfill
        apply_learning_cycle_step_status(_learning_status, "c_state", "backfill")
        filled = backfill_future_returns(db)
        scores_filled = backfill_predicted_scores(db, limit=1000)
        report["returns_backfilled"] = filled
        report["scores_backfilled"] = scores_filled
        _bump_node("c_state")
        _step_time("backfill", step_start,
                    f"{filled} returns + {scores_filled} scores via {_provider}")
        _finish_lc_step(
            "c_state",
            "backfill",
            step_start,
            f"{filled} returns + {scores_filled} scores via {_provider}",
        )

        # Step 4: Confidence decay (prune stale insights early)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_state/decay
        apply_learning_cycle_step_status(_learning_status, "c_state", "decay")
        decay_result = decay_stale_insights(db, user_id)
        report["insights_decayed"] = decay_result.get("decayed", 0)
        report["insights_pruned"] = decay_result.get("pruned", 0)
        _bump_node("c_state")
        _step_time("confidence_decay", step_start,
                    f"{decay_result.get('decayed', 0)} decayed, {decay_result.get('pruned', 0)} pruned")
        _finish_lc_step(
            "c_state",
            "decay",
            step_start,
            f"{decay_result.get('decayed', 0)} decayed, {decay_result.get('pruned', 0)} pruned",
        )

        # D1: Dead asset auto-cleanup — remove tickers with 3+ consecutive fetch failures
        try:
            dead_tickers = _find_dead_tickers(db, top_tickers)
            if dead_tickers:
                top_tickers = [t for t in top_tickers if t not in dead_tickers]
                logger.info("[learning] Auto-excluded %d dead/delisted tickers: %s", len(dead_tickers), dead_tickers)
                report["dead_tickers_excluded"] = list(dead_tickers)
        except Exception as e:
            logger.debug("[learning] Dead ticker cleanup skipped: %s", e)

        # Step 5: Mine patterns
        # graph-node: c_discovery/mine
        apply_learning_cycle_step_status(_learning_status, "c_discovery", "mine")
        step_start = time.time()
        discoveries = mine_patterns(db, user_id, ticker_universe=top_tickers)
        report["patterns_discovered"] = len(discoveries)
        _learning_status["patterns_found"] = len(discoveries)
        _bump_node("c_discovery")
        _step_time("mine", step_start,
                    f"{len(discoveries)} patterns from OHLCV via {_provider}")
        _finish_lc_step(
            "c_discovery",
            "mine",
            step_start,
            f"{len(discoveries)} patterns from OHLCV via {_provider}",
        )

        # Step 6: Active pattern seeking (boost under-sampled patterns)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_discovery/seek
        apply_learning_cycle_step_status(_learning_status, "c_discovery", "seek")
        seek_result = seek_pattern_data(db, user_id)
        report["patterns_boosted"] = seek_result.get("sought", 0)
        _bump_node("c_discovery")
        _step_time("active_seek", step_start,
                    f"{seek_result.get('sought', 0)} boosted")
        _finish_lc_step(
            "c_discovery",
            "seek",
            step_start,
            f"{seek_result.get('sought', 0)} boosted",
        )

        # Step 7: Backtest TradingInsights (legacy; optional — ScanPattern queue is canonical)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # bt_insights removed — ScanPattern queue is the canonical backtest path.

        # Backtest ScanPatterns from priority queue
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_validation/bt_queue
        apply_learning_cycle_step_status(_learning_status, "c_validation", "bt_queue")
        from ...config import settings as _bw_settings

        _delegate = bool(getattr(_bw_settings, "brain_work_delegate_queue_from_cycle", False)) and bool(
            getattr(_bw_settings, "brain_work_ledger_enabled", True)
        )
        if _delegate:
            from .backtest_queue import get_queue_status

            qs = get_queue_status(db, use_cache=False)
            queue_result = {
                "backtests_run": 0,
                "patterns_processed": 0,
                "queue_exploration_added": 0,
                "queue_empty": qs.get("queue_empty", True),
                "queue_skipped_for_work_ledger": True,
                **qs,
            }
        else:
            queue_result = _auto_backtest_from_queue(db, user_id)
        report["queue_backtests_run"] = queue_result.get("backtests_run", 0)
        report["queue_patterns_processed"] = queue_result.get("patterns_processed", 0)
        report["queue_exploration_added"] = queue_result.get("queue_exploration_added", 0)
        report["queue_pending"] = queue_result.get("pending", 0)
        report["queue_empty"] = queue_result.get("queue_empty", True)
        report["backtests_run"] = int(report.get("backtests_run", 0)) + int(
            queue_result.get("backtests_run", 0)
        )
        _bump_node("c_validation")
        _step_time("backtest_queue", step_start,
                   f"{queue_result.get('patterns_processed', 0)} patterns, "
                   f"{queue_result.get('pending', 0)} still pending")
        _finish_lc_step(
            "c_validation",
            "bt_queue",
            step_start,
            f"{queue_result.get('patterns_processed', 0)} patterns, "
            f"{queue_result.get('pending', 0)} still pending",
        )

        # Step 9: Fork / compare / promote ScanPattern variants (exit, entry, combo, …)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_evolution/variants
        apply_learning_cycle_step_status(_learning_status, "c_evolution", "variants")
        try:
            evo_stats = evolve_pattern_strategies(db)
            report["evolution"] = evo_stats
        except Exception as e:
            logger.warning("[learning] evolve_pattern_strategies failed: %s", e)
            report["evolution"] = {}
        _bump_node("c_evolution")
        _step_time(
            "pattern_variant_evolution",
            step_start,
            str(report.get("evolution", {})),
        )
        _finish_lc_step(
            "c_evolution",
            "variants",
            step_start,
            str(report.get("evolution", {})),
        )

        # Step 10: Self-validation & weight evolution (with dynamic hypotheses)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_evolution/hypotheses
        apply_learning_cycle_step_status(_learning_status, "c_evolution", "hypotheses")
        evolve_result = validate_and_evolve(db, user_id)
        report["hypotheses_tested"] = evolve_result.get("hypotheses_tested", 0)
        report["hypotheses_challenged"] = evolve_result.get("challenged", 0)
        report["real_trade_adjustments"] = evolve_result.get("real_trade_adjustments", 0)
        report["weights_evolved"] = evolve_result.get("weights_evolved", 0)
        report["hypothesis_patterns_spawned"] = sum(
            1 for d in evolve_result.get("details", []) if d.get("spawned_pattern_id")
        )
        _bump_node("c_evolution")
        _step_time("evolve", step_start,
                    f"{evolve_result.get('hypotheses_tested', 0)} hypotheses, "
                    f"{evolve_result.get('weights_evolved', 0)} weights evolved")
        _finish_lc_step(
            "c_evolution",
            "hypotheses",
            step_start,
            f"{evolve_result.get('hypotheses_tested', 0)} hypotheses, "
            f"{evolve_result.get('weights_evolved', 0)} weights evolved",
        )

        # Step 11: Breakout outcome learning
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start_bo = time.time()
        # graph-node: c_evolution/breakout
        apply_learning_cycle_step_status(_learning_status, "c_evolution", "breakout")
        bo_result = learn_from_breakout_outcomes(db, user_id)
        report["breakout_patterns_learned"] = bo_result.get("patterns_learned", 0)

        # Closed-trade → ScanPattern feedback (runs in same step; fast query)
        try:
            tf_result = update_pattern_stats_from_closed_trades(db, user_id)
            report["trade_feedback_patterns"] = tf_result.get("patterns_updated", 0)
        except Exception as _tf_err:
            logger.warning("[learning] Trade feedback failed: %s", _tf_err)
            report["trade_feedback_patterns"] = 0

        # Pattern-monitor decision learning (fast; reads resolved decisions).
        try:
            _md_result = learn_from_monitor_decisions(db, user_id)
            report["monitor_decisions_reviewed"] = _md_result.get("decisions_reviewed", 0)
        except Exception as _md_err:
            logger.warning("[learning] Monitor decision learning failed: %s", _md_err)
            report["monitor_decisions_reviewed"] = 0

        _bump_node("c_evolution")
        _step_time("breakout_outcomes", step_start_bo,
                    f"{bo_result.get('patterns_learned', 0)} patterns from "
                    f"{bo_result.get('total_resolved', 0)} resolved alerts, "
                    f"{report.get('trade_feedback_patterns', 0)} trade-feedback updates")
        _finish_lc_step(
            "c_evolution",
            "breakout",
            step_start_bo,
            f"{bo_result.get('patterns_learned', 0)} patterns from "
            f"{bo_result.get('total_resolved', 0)} resolved alerts, "
            f"{report.get('trade_feedback_patterns', 0)} trade-feedback updates",
        )

        # Secondary miners (optional — disable for faster cycles)
        def _mark_secondary_skipped() -> None:
            nonlocal _node_step
            _node_step += 8
            _learning_status["nodes_completed"] = _node_step
            _learning_status["secondary_miners_skipped"] = True

        run_secondary_miners_phase(
            db,
            user_id,
            settings=settings,
            cycle_budget=cycle_budget,
            report=report,
            learning_status=_learning_status,
            bump_node=_bump_node,
            step_time=_step_time,
            finish_lc_step=_finish_secondary_step,
            shutting_down_is_set=_shutting_down.is_set,
            mark_secondary_skipped=_mark_secondary_skipped,
        )

        report["brain_resource_budget"] = cycle_budget.to_report_dict()

        # Step 20: Market journal
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_journal/journal
        apply_learning_cycle_step_status(_learning_status, "c_journal", "journal")
        journal = daily_market_journal(db, user_id)
        report["journal_written"] = journal is not None
        _bump_node("c_journal")
        _step_time("journal", step_start)
        _finish_lc_step("c_journal", "journal", step_start, "")

        # Step 21: Signal events
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_journal/signals
        apply_learning_cycle_step_status(_learning_status, "c_journal", "signals")
        events = check_signal_events(db, user_id)
        report["signal_events"] = len(events)
        _bump_node("c_journal")
        _step_time("signals", step_start, f"{len(events)} events")
        _finish_lc_step("c_journal", "signals", step_start, f"{len(events)} events")

        # Step 22: Train pattern meta-learner
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_meta_learning/ml
        apply_learning_cycle_step_status(_learning_status, "c_meta_learning", "ml")
        from .pattern_ml import get_meta_learner, apply_ml_feedback
        _meta = get_meta_learner()
        ml_result = _meta.train(db)
        report["ml_trained"] = ml_result.get("ok", False)
        report["ml_accuracy"] = ml_result.get("cv_accuracy", 0)
        if ml_result.get("ok"):
            _fb = apply_ml_feedback(db, _meta.get_pattern_importances())
            report["ml_feedback_boosted"] = _fb.get("boosted", 0)
            report["ml_feedback_penalised"] = _fb.get("penalised", 0)
            log_learning_event(
                db, user_id, "ml_feedback",
                f"Pattern meta-learner trained: CV acc={ml_result.get('cv_accuracy',0)}%, "
                f"{ml_result.get('active_patterns',0)} patterns, "
                f"{_fb.get('boosted',0)} boosted, {_fb.get('penalised',0)} penalised",
            )
        _bump_node("c_meta_learning")
        _step_time("ml_train", step_start,
                    f"acc={ml_result.get('cv_accuracy', 0):.3f}"
                    if ml_result.get("ok") else "skipped")
        _finish_lc_step(
            "c_meta_learning",
            "ml",
            step_start,
            f"acc={ml_result.get('cv_accuracy', 0):.3f}" if ml_result.get("ok") else "skipped",
        )

        # Pattern engine — discover, test, evolve (before proposals)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_decisioning/pattern_engine
        apply_learning_cycle_step_status(_learning_status, "c_decisioning", "pattern_engine")
        try:
            pe_result = _run_pattern_engine_cycle(db, user_id)
            report["patterns_discovered_engine"] = pe_result.get("hypotheses_generated", 0)
            report["patterns_tested"] = pe_result.get("patterns_tested", 0)
            report["patterns_evolved"] = pe_result.get("patterns_evolved", 0)
        except Exception as e:
            logger.warning(f"[trading] Pattern engine cycle failed: {e}")
        _bump_node("c_decisioning")
        _step_time("pattern_engine", step_start, "done")
        _finish_lc_step("c_decisioning", "pattern_engine", step_start, "done")

        # Strategy proposals (after pattern engine)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        # graph-node: c_decisioning/proposals
        apply_learning_cycle_step_status(_learning_status, "c_decisioning", "proposals")
        try:
            from .alerts import generate_strategy_proposals
            proposals = generate_strategy_proposals(db, user_id)
            report["proposals_generated"] = len(proposals)
        except Exception as e:
            logger.warning(f"[trading] Strategy proposal generation failed: {e}")
            report["proposals_generated"] = 0
        _bump_node("c_decisioning")
        _step_time("proposals", step_start,
                    f"{report.get('proposals_generated', 0)} generated")
        _finish_lc_step(
            "c_decisioning",
            "proposals",
            step_start,
            f"{report.get('proposals_generated', 0)} generated",
        )

        # Cycle AI report (deep study) — synthesize cycle into stored markdown
        report["cycle_ai_report_id"] = None
        if not _shutting_down.is_set():
            step_start = time.time()
            # graph-node: c_control/cycle_report
            apply_learning_cycle_step_status(_learning_status, "c_control", "cycle_report")
            report["elapsed_s_pre_report"] = round(time.time() - start, 1)
            try:
                from .learning_cycle_report import generate_and_store_cycle_report

                rid = generate_and_store_cycle_report(db, user_id, report)
                report["cycle_ai_report_id"] = rid
            except Exception as e:
                logger.warning("[trading] Cycle AI report failed: %s", e)
            _bump_node("c_control")
            _step_time(
                "cycle_ai_report",
                step_start,
                f"id={report.get('cycle_ai_report_id')}" if report.get("cycle_ai_report_id") else "failed",
            )
            _finish_lc_step(
                "c_control",
                "cycle_report",
                step_start,
                f"id={report.get('cycle_ai_report_id')}" if report.get("cycle_ai_report_id") else "failed",
            )

        # Live vs research depromotion (optional)
        if not _shutting_down.is_set():
            step_start_dep = time.time()
            # graph-node: c_control/depromote
            apply_learning_cycle_step_status(_learning_status, "c_control", "depromote")
            try:
                report["live_depromotion"] = run_live_pattern_depromotion(db)
            except Exception as e:
                logger.warning("[learning] live depromotion failed: %s", e)
                report["live_depromotion"] = {"ok": False, "error": str(e)}
            try:
                from .live_drift import run_live_drift_refresh

                report["live_drift"] = run_live_drift_refresh(db)
            except Exception as e:
                logger.warning("[learning] live drift refresh failed: %s", e)
                report["live_drift"] = {"ok": False, "error": str(e)}
            try:
                from .execution_robustness import run_execution_robustness_refresh

                report["execution_robustness"] = run_execution_robustness_refresh(db)
            except Exception as e:
                logger.warning("[learning] execution robustness refresh failed: %s", e)
                report["execution_robustness"] = {"ok": False, "error": str(e)}
            try:
                from .brain_neural_mesh.publisher import notify_learning_cycle_step_committed

                notify_learning_cycle_step_committed(
                    db,
                    cluster_id="c_control",
                    step_sid="depromote",
                    elapsed_sec=round(time.time() - step_start_dep, 2),
                    extra="",
                    correlation_id=_learning_status.get("correlation_id"),
                )
            except Exception:
                logger.debug("[learning] mesh notify depromote failed (ignored)", exc_info=True)

        # Step 14: Finalize + log
        step_start_fin = time.time()
        # graph-node: c_control/finalize
        apply_learning_cycle_step_status(_learning_status, "c_control", "finalize")
        elapsed = time.time() - start
        report["data_provider"] = _provider
        log_learning_event(
            db, user_id, "scan",
            f"Learning cycle ({_provider}): "
            f"{report.get('prescreen_candidates', 0)} pre-screened, "
            f"scored {report['tickers_scored']}, {report['snapshots_taken']} snapshots, "
            f"{report['patterns_discovered']} patterns, "
            f"{report.get('patterns_boosted', 0)} boosted, "
            f"{report.get('backtests_run', 0)} backtests, "
            f"{report.get('hypotheses_tested', 0)} hypotheses tested "
            f"({report.get('hypotheses_challenged', 0)} challenged), "
            f"{report.get('real_trade_adjustments', 0)} real-trade adjustments, "
            f"{report.get('patterns_refined', 0)} refined, "
            f"{report.get('weights_evolved', 0)} weights evolved, "
            f"{report['signal_events']} signals, "
            f"ML={'trained' if report.get('ml_trained') else 'skipped'}, "
            f"{report.get('proposals_generated', 0)} proposals — {elapsed:.0f}s",
        )
        _bump_node("c_control")
        _finish_lc_step(
            "c_control",
            "finalize",
            step_start_fin,
            f"elapsed={elapsed:.0f}s",
        )

    except InterruptedError:
        interrupted = True
        logger.info("[trading] Learning cycle interrupted by shutdown")
        report["interrupted"] = True
    except Exception as e:
        report_error = str(e)
        logger.error(f"[trading] Learning cycle error: {e}")
        report["error"] = str(e)
        try:
            log_learning_event(db, user_id, "error", f"Learning cycle failed: {e}")
        except Exception:
            logger.debug("[learning] run_learning_cycle: non-critical operation failed", exc_info=True)
    finally:
        elapsed = time.time() - start
        _lease_release_holder = _brain_lease_enforcement_ctx.get("holder_id")
        _lease_release_acquired = bool(_brain_lease_enforcement_ctx.get("acquired"))
        _brain_lease_enforcement_ctx.clear()
        _learning_status["running"] = False
        _learning_status["phase"] = "idle"
        _learning_status["current_step"] = ""
        _learning_status["graph_node_id"] = ""
        _learning_status["current_cluster_id"] = ""
        _learning_status["current_step_sid"] = ""
        _learning_status["current_cluster_index"] = -1
        _learning_status["current_step_index"] = -1
        _learning_status["correlation_id"] = None
        _learning_status["secondary_miners_skipped"] = False
        _learning_status["last_run"] = datetime.utcnow().isoformat()
        _learning_status["last_duration_s"] = round(elapsed, 1)
        persist_learning_live_snapshot_force()
        report["elapsed_s"] = round(elapsed, 1)
        logger.info(
            "[chili_brain_io] learning_cycle_end elapsed_s=%s correlation_done=1 error=%s",
            report["elapsed_s"],
            report.get("error"),
        )
        report["step_timings"] = dict(_learning_status.get("step_timings", {}))
        report["funnel_snapshot"] = {
            "prescreen_candidates": report.get("prescreen_candidates"),
            "tickers_scored": report.get("tickers_scored"),
            "snapshots_taken": report.get("snapshots_taken"),
            "patterns_discovered": report.get("patterns_discovered"),
            "returns_backfilled": report.get("returns_backfilled"),
            "queue_backtests_run": report.get("queue_backtests_run"),
            "backtests_run": report.get("backtests_run"),
            "live_depromotion": report.get("live_depromotion"),
        }
        try:
            _snap = get_research_funnel_snapshot(db)
            report["funnel_snapshot"]["promotion_active"] = _snap.get(
                "promotion_status_active"
            )
            report["funnel_snapshot"]["queue_pending"] = (_snap.get("queue") or {}).get(
                "pending"
            )
            _learning_status["last_cycle_funnel"] = dict(report["funnel_snapshot"])
            _learning_status["last_cycle_budget"] = report.get("brain_resource_budget")
        except Exception:
            logger.debug("[learning] run_learning_cycle: non-critical operation failed", exc_info=True)

        if _lease_release_acquired and _lease_release_holder:
            from ...trading_brain.infrastructure.lease_dedicated_session import (
                brain_lease_enforcement_release_dedicated,
            )

            brain_lease_enforcement_release_dedicated(
                holder_id=str(_lease_release_holder)
            )

    if not interrupted:
        try:
            report["promoted_fast_eval"] = refresh_promoted_prediction_cache(db)
        except Exception as _pfe:
            logger.warning("[learning] Promoted prediction cache at cycle end failed: %s", _pfe)
            report["promoted_fast_eval"] = {"ok": False, "error": str(_pfe)}

    # Phase C shadow hook: PIT audit of active scan patterns. Purely advisory
    # — no pattern is mutated. Any failure is swallowed. When
    # brain_pit_audit_mode == "off" the helper returns [] without DB work.
    try:
        from .pit_audit import audit_and_record_active as _pit_audit_and_record
        _pit_results = _pit_audit_and_record(db)
        if _pit_results:
            try:
                db.commit()
            except Exception:
                logger.debug("[pit_audit] commit failed", exc_info=True)
            report["pit_audit"] = {
                "patterns_audited": len(_pit_results),
                "patterns_clean": sum(1 for r in _pit_results if r.agree_bool),
                "patterns_violating": sum(1 for r in _pit_results if not r.agree_bool),
            }
    except Exception:
        logger.debug("[pit_audit] shadow hook failed", exc_info=True)

    try:
        from ..brain_worker_signals import persist_last_cycle_digest_json

        persist_last_cycle_digest_json(db, build_cycle_ui_digest(report))
    except Exception as _ui_d:
        logger.warning("[learning] persist cycle UI digest failed: %s", _ui_d)

    logger.info(
        f"[learning] Learning cycle finished in {elapsed:.0f}s "
        f"(provider={report.get('data_provider', 'unknown')}): {report}"
    )
    if not interrupted and not report.get("error"):
        try:
            from .brain_neural_mesh import publisher as _nm_pub

            _nm_pub.publish_learning_cycle_completed(db, elapsed_s=float(elapsed))
            db.commit()
        except Exception as _nm_e:
            logger.warning("[learning] neural mesh cycle publish failed (ignored): %s", _nm_e)
            try:
                db.rollback()
            except Exception:
                logger.debug("[learning] run_learning_cycle: non-critical operation failed", exc_info=True)
    return {"ok": True, **report}


