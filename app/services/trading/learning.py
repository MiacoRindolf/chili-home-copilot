"""Learning: pattern mining, deep study, learning cycles, brain stats."""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import func

from ...models.trading import (
    LearningEvent, MarketSnapshot, ScanResult, TradingInsight, Trade,
)
from .market_data import (
    fetch_quote, fetch_quotes_batch, fetch_ohlcv_df, get_indicator_snapshot,
    get_vix, get_volatility_regime, DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS,
    _use_massive, _use_polygon,
)
from .portfolio import get_watchlist, get_trade_stats, get_insights, save_insight, get_trade_stats_by_pattern
from .brain_resource_budget import BrainResourceBudget
from .snapshot_bar_ops import (
    dedupe_sample_rows,
    normalize_bar_start_utc,
    try_insert_insight_evidence,
    upsert_market_snapshot,
)
from .learning_cycle_architecture import (
    apply_learning_cycle_step_status,
    apply_learning_cycle_step_status_progress,
)

logger = logging.getLogger(__name__)

_CPU_COUNT = os.cpu_count() or 4
_IO_WORKERS_HIGH = min(80, max(24, _CPU_COUNT * 3))  # IO-heavy data fetching (64 GB / 32 cores)
_IO_WORKERS_MED = min(48, max(16, _CPU_COUNT * 2))   # mixed IO/CPU work
_IO_WORKERS_LOW = min(32, max(10, _CPU_COUNT))        # lighter parallel tasks

_shutting_down = threading.Event()

# Stale-while-revalidate cache for get_current_predictions
_pred_cache: dict[str, Any] = {"results": [], "ts": 0.0}
_PRED_CACHE_TTL = 180       # 3 min fresh
_PRED_CACHE_STALE_TTL = 600  # 10 min stale-while-revalidate
_pred_refreshing = False
_pred_refresh_lock = threading.Lock()


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


def brain_pattern_backtest_friction_kwargs() -> dict[str, Any]:
    """Spread, commission, and OOS holdout for pattern hypothesis backtests."""
    from ...config import settings

    return {
        "spread": float(settings.backtest_spread),
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
) -> tuple[str, bool]:
    """Return ``(promotion_status, allow_active)``.

    ``allow_active`` False means the caller should set ``ScanPattern.active = False``.

    Discovery-phase miners never call this — only backtest evidence paths. Optional aggregate
    OOS trade floor (``min_oos_aggregate_trades``) reduces promotion on thin short-horizon samples.
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
    if min_oos_aggregate_trades is not None and int(min_oos_aggregate_trades) > 0:
        if int(oos_aggregate_trade_count or 0) < int(min_oos_aggregate_trades):
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


def run_live_pattern_depromotion(db: Session) -> dict[str, Any]:
    """Demote patterns when live closed-trade win rate lags research OOS by a margin."""
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
    for row in patterns:
        n_live = int(row.get("live_closed_trades") or 0)
        if n_live < min_n:
            continue
        oos_wr = row.get("research_oos_win_rate_pct")
        live_wr = row.get("live_win_rate_pct")
        pid = row.get("scan_pattern_id")
        if oos_wr is None or live_wr is None or pid is None:
            continue
        if float(live_wr) < float(oos_wr) - max_gap:
            p = db.query(ScanPattern).filter(ScanPattern.id == int(pid)).first()
            if p and p.active and (p.promotion_status or "") == "promoted":
                p.active = False
                p.promotion_status = "degraded_live"
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

    return {"ok": True, "demoted": demoted, "decay_monitor_updated": touched}


# ── Learning Event Logger (extracted to learning_events.py) ───────────
from .learning_events import log_learning_event, get_learning_events  # noqa: F401 — re-export for backward compat


# ── AI Self-Learning ──────────────────────────────────────────────────

def analyze_closed_trade(db: Session, trade: Trade) -> str | None:
    """Called after a trade is closed. Asks the AI to review and extract patterns."""
    from ...prompts import load_prompt
    from ... import openai_client
    from ...logger import log_info, new_trace_id
    from .journal import add_journal_entry

    trace_id = new_trace_id()

    snap_data = ""
    if trade.indicator_snapshot:
        try:
            snap_data = json.dumps(json.loads(trade.indicator_snapshot), indent=2)
        except Exception:
            snap_data = trade.indicator_snapshot

    pnl_label = "PROFIT" if (trade.pnl or 0) > 0 else "LOSS"
    trade_summary = (
        f"Ticker: {trade.ticker}\n"
        f"Direction: {trade.direction}\n"
        f"Entry: ${trade.entry_price} on {trade.entry_date}\n"
        f"Exit: ${trade.exit_price} on {trade.exit_date}\n"
        f"P&L: ${trade.pnl} ({pnl_label})\n"
        f"Indicator snapshot at exit:\n{snap_data}"
    )

    existing_insights = get_insights(db, trade.user_id, limit=10)
    insight_text = ""
    if existing_insights:
        insight_text = "\n".join(
            f"- [{ins.confidence:.0%}] {ins.pattern_description}"
            for ins in existing_insights
        )

    user_msg = (
        f"A trade was just closed. Analyze it and extract trading patterns.\n\n"
        f"## Trade Details\n{trade_summary}\n\n"
        f"## Existing Learned Patterns\n{insight_text or 'None yet.'}\n\n"
        f"Instructions:\n"
        f"1. Explain why this trade was a {pnl_label} based on the indicator state.\n"
        f"2. Extract 1-3 reusable patterns as JSON array:\n"
        f'   [{{"pattern": "description", "confidence": 0.0-1.0}}]\n'
        f"3. If an existing pattern is confirmed, note its description so we can boost its confidence.\n"
        f"4. Put the JSON array on a line starting with PATTERNS:"
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
        reply = result.get("reply", "")
    except Exception as e:
        log_info(trace_id, f"[trading] post-trade analysis error: {e}")
        return None

    _extract_and_store_patterns(db, trade.user_id, reply, existing_insights,
                                trade_won=trade.pnl is not None and trade.pnl > 0)

    add_journal_entry(
        db, trade.user_id,
        content=f"[AI] Trade #{trade.id} ({trade.ticker} {pnl_label} ${trade.pnl}): {reply[:500]}",
        trade_id=trade.id,
    )

    return reply


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
        pass


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
        snap = get_indicator_snapshot(ticker, bar_interval)
        period = "3mo" if bar_interval == "1d" else "60d"
        df = fetch_ohlcv_df(ticker, period=period, interval=bar_interval)
        if df is None or df.empty:
            return ticker, None, None, None, None, None, None, bar_interval, None
        price = float(df.iloc[-1]["Close"])
        bar_start = normalize_bar_start_utc(df.index[-1])
        quote = {"price": price}
        sent_score, sent_count = _fetch_news_sentiment(ticker)
        pe, mcap = _fetch_fundamentals(ticker)
        return ticker, snap, quote, sent_score, sent_count, pe, mcap, bar_interval, bar_start
    except Exception:
        return ticker, None, None, None, None, None, None, bar_interval, None


def take_snapshots_parallel(
    db: Session,
    tickers: list[str],
    max_workers: int = _IO_WORKERS_HIGH,
    *,
    bar_interval: str = "1d",
) -> int:
    """Take snapshots for many tickers using a thread pool.

    Data fetching runs in parallel; DB writes happen sequentially on the
    calling thread to avoid SQLAlchemy session issues.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

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
                pass

    _t0 = time.time()
    fetched: list[tuple] = []
    total = len(tickers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_snapshot_data, t, bar_interval): t for t in tickers}
        for future in as_completed(futures):
            if _shutting_down.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                fetched.append(future.result())
            except Exception:
                pass
            # Progress logging every 100 tickers
            done = len(fetched)
            if done % 100 == 0 or done == total:
                elapsed = round(time.time() - _t0, 1)
                logger.info(f"[learning] Snapshot progress: {done}/{total} ({elapsed}s)")
                apply_learning_cycle_step_status_progress(
                    _learning_status, "c_state", "snapshots", done, total,
                )

    _fetch_elapsed = round(time.time() - _t0, 1)
    logger.info(
        f"[learning] Snapshot data fetch: {len(fetched)}/{len(tickers)} tickers "
        f"in {_fetch_elapsed}s ({max_workers} workers) interval={bar_interval}"
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
            pass
    if count:
        db.commit()
    return count


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
                pass

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

    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
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


def _mine_from_history(ticker: str, bar_interval: str = "1d") -> list[dict]:
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange
    from ta.volume import OnBalanceVolumeIndicator
    from .scanner import _detect_resistance_retests, _detect_narrow_range, _detect_vcp

    period = "6mo" if bar_interval == "1d" else "90d"
    min_len = 60 if bar_interval == "1d" else 120
    try:
        df = fetch_ohlcv_df(ticker, period=period, interval=bar_interval)
        if df.empty or len(df) < min_len:
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
    for i in range(50, len(df) - 10):
        price = float(close.iloc[i])
        if price <= 0:
            continue
        ret_5d = (float(close.iloc[i + 5]) - price) / price * 100
        ret_10d = (float(close.iloc[i + 10]) - price) / price * 100 if i + 10 < len(df) else None

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
            "bar_interval": bar_interval,
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


def mine_patterns(db: Session, user_id: int | None) -> list[str]:
    """Discover patterns from historical price data + existing snapshots."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ...config import settings
    from .market_data import ALL_SCAN_TICKERS as _ALL_TICKERS

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
        pass

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
        pass

    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
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
    MIN_SAMPLES = 3
    _cpcv_on = getattr(settings, "brain_mining_purged_cpcv_enabled", True)

    def _check(filtered, label):
        if len(filtered) < MIN_SAMPLES:
            return
        if _cpcv_on:
            from .mining_validation import mined_candidate_passes_purged_segments
            ok, _ = mined_candidate_passes_purged_segments(filtered)
            if not ok:
                return
        avg_5d = sum(r["ret_5d"] for r in filtered) / len(filtered)
        avg_10d_vals = [r["ret_10d"] for r in filtered if r.get("ret_10d") is not None]
        avg_10d = (sum(avg_10d_vals) / len(avg_10d_vals)) if avg_10d_vals else None
        wins = sum(1 for r in filtered if r["ret_5d"] > 0)
        wr = wins / len(filtered) * 100
        if avg_5d > 0.2 or (avg_5d > 0 and wr >= 55):
            ret_str = f"{avg_5d:+.1f}%/5d"
            if avg_10d is not None:
                ret_str += f", {avg_10d:+.1f}%/10d"
            pattern = f"{label} -> avg {ret_str} ({wr:.0f}% win, {len(filtered)} samples){regime_tag}"
            discoveries.append(pattern)
            save_insight(db, user_id, pattern, confidence=min(0.9, wr / 100),
                         wins=wins, losses=len(filtered) - wins)

    logger.info(f"[mine_patterns] Mining from {len(all_rows)} historical data points")

    _check([r for r in all_rows if r["rsi"] < 30], "RSI oversold (<30)")
    _check([r for r in all_rows if r["rsi"] > 70], "RSI overbought (>70) — sell signal")
    _check([r for r in all_rows if 30 <= r["rsi"] < 40], "RSI near-oversold (30-40)")
    _check([r for r in all_rows if r["macd"] > r["macd_sig"]], "MACD bullish crossover")
    _check([r for r in all_rows if r["macd_hist"] > 0 and r["macd"] < 0],
           "MACD histogram positive while MACD negative (early reversal)")
    _check([r for r in all_rows if r["bb_pct"] < 0.1],
           "Price below lower Bollinger Band (<10%)")
    _check([r for r in all_rows if r["bb_pct"] > 0.9],
           "Price above upper Bollinger Band (>90%) — sell signal")
    _check([r for r in all_rows if r["adx"] > 30 and r["rsi"] < 40],
           "Strong trend (ADX>30) + RSI<40 (trending oversold)")
    _check([r for r in all_rows if r["adx"] < 15],
           "No trend (ADX<15) — range-bound, mean reversion expected")
    _check([r for r in all_rows if r["ema_stack"]],
           "EMA stacking bullish (Price > EMA20 > EMA50 > EMA100)")
    _check([r for r in all_rows
            if r["rsi"] < 35 and r["macd"] > r["macd_sig"] and r["bb_pct"] < 0.2],
           "Triple confluence: RSI<35 + MACD bullish + near lower BB")
    _check([r for r in all_rows
            if r["rsi"] > 55 and r["adx"] > 25 and r["macd"] > r["macd_sig"]],
           "Momentum confluence: RSI>55 + ADX>25 + MACD bullish (trend continuation)")

    atr_vals = [r["atr"] for r in all_rows if r["atr"] > 0]
    if atr_vals:
        atr_median = sorted(atr_vals)[len(atr_vals) // 2]
        _check([r for r in all_rows if r["atr"] > atr_median * 1.5 and r["rsi"] < 35],
               "High volatility + oversold RSI (capitulation bounce)")
        _check([r for r in all_rows if 0 < r["atr"] < atr_median * 0.5],
               "Low volatility squeeze — breakout expected")

    crypto = [r for r in all_rows if r["is_crypto"]]
    if crypto:
        _check([r for r in crypto if r["rsi"] < 25],
               "Crypto deep oversold (RSI<25)")
        _check([r for r in crypto if r["rsi"] < 35 and r["macd_hist"] > 0],
               "Crypto RSI<35 + MACD histogram positive — reversal")

    _check([r for r in all_rows if r["above_sma20"] and r["rsi"] > 50 and r["adx"] > 20],
           "Above SMA20 + RSI>50 + ADX>20 (healthy uptrend)")
    _check([r for r in all_rows if r["stoch_k"] < 20],
           "Stochastic oversold (K<20)")
    _check([r for r in all_rows if r["bb_pct"] < 0.15 and r["macd_hist"] > 0],
           "Lower BB + MACD turning positive (bounce setup)")
    _check([r for r in all_rows if r["above_sma20"] and r["ema_stack"] and r["adx"] > 20],
           "Full alignment: EMA stack + above SMA20 + ADX>20 (strong trend)")

    # Stochastic + MACD confluence
    _check([r for r in all_rows if r["stoch_k"] < 20 and r["macd_hist"] > 0],
           "Stochastic oversold + MACD turning positive (double bottom signal)")
    _check([r for r in all_rows if r["stoch_k"] > 80 and r["macd_hist"] < 0],
           "Stochastic overbought + MACD turning negative — sell signal")

    # EMA stack with RSI confirmation
    _check([r for r in all_rows if r["ema_stack"] and 40 <= r["rsi"] <= 60],
           "EMA stack + RSI neutral zone (healthy trend, not overextended)")

    # Extreme RSI with trend
    _check([r for r in all_rows if r["rsi"] < 25 and r["adx"] > 20],
           "Deep oversold RSI<25 in trending market (sharp reversal setup)")

    # Consolidation breakout
    _check([r for r in all_rows if r["bb_pct"] > 0.5 and r["bb_pct"] < 0.7
            and r["adx"] < 20 and r["macd_hist"] > 0],
           "Mid-BB range + low ADX + MACD positive (consolidation breakout)")

    # Bearish divergence patterns
    _check([r for r in all_rows if r["rsi"] > 60 and r["macd_hist"] < 0 and r["adx"] > 25],
           "RSI>60 but MACD negative + strong trend — bearish divergence sell signal")

    # Volume spike patterns
    vol_rows = [r for r in all_rows if r.get("vol_ratio") is not None]
    if vol_rows:
        _check([r for r in vol_rows if r["vol_ratio"] > 2.0 and r["rsi"] < 40],
               "Volume spike 2x+ with RSI<40 (capitulation / accumulation)")
        _check([r for r in vol_rows if r["vol_ratio"] > 2.0 and r["ema_stack"]],
               "Volume spike 2x+ with EMA stack (breakout confirmation)")
        _check([r for r in vol_rows if r["vol_ratio"] > 1.5 and r["macd_hist"] > 0
                and r["rsi"] > 50],
               "Volume surge + MACD positive + RSI>50 (momentum ignition)")

    # Gap patterns
    gap_rows = [r for r in all_rows if r.get("gap_pct") is not None]
    if gap_rows:
        _check([r for r in gap_rows if r["gap_pct"] > 2.0 and r["rsi"] < 70],
               "Gap up >2% with RSI not overbought (momentum gap)")
        _check([r for r in gap_rows if r["gap_pct"] < -2.0 and r["rsi"] < 30],
               "Gap down >2% into oversold RSI (gap-fill reversal)")

    # ── Momentum pullback patterns (inspired by day-trade best practices) ──

    # MACD positive + high relative volume + pullback = bread-and-butter entry
    if vol_rows:
        _check([r for r in vol_rows if r["vol_ratio"] > 5.0
                and r["macd"] > r["macd_sig"] and r["macd_hist"] > 0
                and r["rsi"] < 65],
               "MACD positive + volume surge 5x+ (momentum pullback setup)")

    # Topping tail warning (upper wick dominance on high volume)
    _check([r for r in all_rows
            if r["rsi"] > 60 and r.get("vol_ratio") is not None
            and r["vol_ratio"] > 2.0 and r["macd_hist"] < 0],
           "High RSI + volume spike + MACD turning negative (topping/reversal warning)")

    # MACD flipped negative after extended run = setup invalidated
    _check([r for r in all_rows
            if r["macd"] < r["macd_sig"] and r["macd_hist"] < 0
            and r["rsi"] > 40 and r["adx"] > 20],
           "MACD flipped negative in active trend — setup invalidated (avoid entry)")

    # Low float + strong gapper + MACD confirmation
    if gap_rows and vol_rows:
        _check([r for r in gap_rows
                if r["gap_pct"] > 10.0 and r["macd_hist"] > 0
                and r.get("vol_ratio") is not None and r["vol_ratio"] > 3.0],
               "10%+ gapper + MACD positive + high volume (high-conviction momentum)")

    # First pullback with clean volume profile
    _check([r for r in all_rows
            if r["rsi"] > 45 and r["rsi"] < 65
            and r["macd"] > r["macd_sig"] and r["macd_hist"] > 0
            and r["ema_stack"] and r.get("vol_ratio") is not None
            and r["vol_ratio"] > 1.5],
           "First pullback: MACD+, EMA stack, rising volume (bread-and-butter entry)")

    # Extended pullback (7+ candles = dead setup) — captured as sell signal
    _check([r for r in all_rows
            if r["rsi"] < 35 and r["macd_hist"] < 0
            and r["adx"] > 15 and not r["ema_stack"]],
           "Extended pullback with MACD negative + broken EMA stack — setup dead")

    # ── Stochastic divergence patterns ──
    _check([r for r in all_rows if r.get("stoch_bull_div")],
           "Stochastic bullish divergence (price lower low, stoch higher low)")
    _check([r for r in all_rows if r.get("stoch_bear_div")],
           "Stochastic bearish divergence (price higher high, stoch lower high) — sell signal")
    _check([r for r in all_rows if r.get("stoch_bull_div") and r["macd_hist"] > 0],
           "Stoch bullish divergence + MACD turning positive (reversal confirmation)")
    _check([r for r in all_rows if r.get("stoch_bear_div") and r["macd_hist"] < 0],
           "Stoch bearish divergence + MACD turning negative (top confirmation)")

    # ── Multi-indicator confluence patterns ──
    _check([r for r in all_rows
            if r["rsi"] < 35 and r["stoch_k"] < 25 and r["bb_pct"] < 0.15],
           "Triple oversold confluence: RSI<35 + Stoch<25 + BB<0.15")
    _check([r for r in all_rows
            if r["adx"] > 30 and r["stoch_k"] < 20 and r["ema_stack"]],
           "Trend pullback to oversold: ADX>30 + Stoch<20 + EMA stack")
    _check([r for r in all_rows
            if r.get("stoch_bull_div") and r["rsi"] < 40 and r["bb_pct"] < 0.25],
           "Multi-signal reversal: stoch bull divergence + RSI<40 + near lower BB")

    # ── News sentiment + technical confluence patterns ──
    sent_rows = [r for r in all_rows if r.get("news_sentiment") is not None]
    if len(sent_rows) >= 5:
        _check([r for r in sent_rows if r["news_sentiment"] > 0.15 and r["rsi"] < 35],
               "Bullish news + RSI oversold (<35) — contrarian catalyst")
        _check([r for r in sent_rows if r["news_sentiment"] < -0.15 and r["rsi"] > 70],
               "Bearish news + RSI overbought (>70) — sell signal confluence")
        _check([r for r in sent_rows if r["news_sentiment"] > 0.15 and r["macd_hist"] > 0
                and r["ema_stack"]],
               "Bullish news + MACD positive + EMA stack — momentum confirmation")
        _check([r for r in sent_rows if r["news_sentiment"] < -0.15 and r["macd_hist"] < 0],
               "Bearish news + MACD negative — downtrend confirmation")
        _check([r for r in sent_rows if r.get("news_count", 0) >= 5
                and r.get("vol_ratio") is not None and r["vol_ratio"] > 2],
               "High news volume (5+) + high trading volume (2x) — event-driven breakout")
        _check([r for r in sent_rows if r["news_sentiment"] > 0.2 and r["stoch_k"] < 25],
               "Strong bullish news + stochastic oversold — high-probability bounce")
        _check([r for r in sent_rows if abs(r["news_sentiment"]) < 0.05
                and r["adx"] > 30 and r["rsi"] < 40],
               "Neutral news + strong trend (ADX>30) + RSI<40 — trend pullback, no catalyst fear")

    try:
        if getattr(settings, "brain_regime_mining_enabled", True):
            from .regime_mining import run_regime_gated_mining_checks

            run_regime_gated_mining_checks(all_rows, _check)
    except Exception:
        pass

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

    logger.info(f"[mine_patterns] Discovered {len(discoveries)} patterns from {len(all_rows)} data points")
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
    from .prescreener import get_prescreened_candidates

    insights = get_insights(db, user_id, limit=50)
    under_sampled = [
        ins for ins in insights
        if ins.evidence_count < 20 and ins.confidence > 0.4 and ins.active
    ]
    if not under_sampled:
        return {"sought": 0, "note": "no under-sampled patterns"}

    try:
        seek_tickers = get_prescreened_candidates(include_crypto=True, max_total=600)
    except Exception:
        from .market_data import ALL_SCAN_TICKERS
        seek_tickers = list(ALL_SCAN_TICKERS)

    seek_tickers = seek_tickers[:400]
    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    extra_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in seek_tickers}
        for f in as_completed(futs):
            if _shutting_down.is_set():
                break
            try:
                extra_rows.extend(f.result())
            except Exception:
                pass

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
    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in mine_tickers}
        for f in as_completed(futs):
            try:
                rows.extend(f.result())
            except Exception:
                pass

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
        pass

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

        MIN_DELTA = 1.5  # Require 1.5% difference to be meaningful
        delta = avg_a - avg_b
        if hyp.expected_winner == "a":
            confirmed = delta > MIN_DELTA
        else:
            confirmed = -delta > MIN_DELTA

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


# ── Intraday Breakout Pattern Mining (15m data) ──────────────────────

def _mine_intraday_breakout_patterns(ticker: str) -> list[dict]:
    """Mine 15m OHLCV for short-term breakout patterns (minutes to hours).

    Returns rows of indicator + pattern states with 4h and 8h forward returns.
    """
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    try:
        df = fetch_ohlcv_df(ticker, period="5d", interval="15m")
        if df.empty or len(df) < 80:
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
    bars_4h = 16   # 4h / 15m = 16 bars
    bars_8h = 32

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


def _mine_intraday_one_ticker(ticker: str, budget: BrainResourceBudget | None) -> list[dict]:
    if budget is not None and not budget.try_ohlcv("intraday_compression", 1):
        return []
    try:
        return _mine_intraday_breakout_patterns(ticker)
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
    """Phase-A discovery on 15m data: compression / pre-expansion hypotheses only.

    Tags ``hypothesis_family=compression_expansion``. Does not promote ScanPatterns;
    OOS promotion remains on backtest paths only. Optional ``budget`` caps OHLCV and row volume.
    """
    from .market_data import DEFAULT_CRYPTO_TICKERS, DEFAULT_SCAN_TICKERS
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tickers = list(DEFAULT_CRYPTO_TICKERS)[:30] + list(DEFAULT_SCAN_TICKERS)[:30]

    rows: list[dict] = []
    _workers = _IO_WORKERS_LOW
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_intraday_one_ticker, t, budget): t for t in tickers}
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

    if len(rows) < 30:
        return {
            "tested": 0,
            "note": "insufficient intraday data",
            "hypothesis_family": BRAIN_HYPOTHESIS_FAMILY_COMPRESSION,
        }

    discoveries = 0
    _fam = BRAIN_HYPOTHESIS_FAMILY_COMPRESSION

    # Hypothesis: BB squeeze -> 4h positive returns
    sq = [r for r in rows if r["bb_squeeze"]]
    no_sq = [r for r in rows if not r["bb_squeeze"]]
    if len(sq) >= 10 and len(no_sq) >= 10:
        avg_sq = sum(r["ret_4h"] for r in sq) / len(sq)
        avg_no = sum(r["ret_4h"] for r in no_sq) / len(no_sq)
        wr_sq = sum(1 for r in sq if r["ret_4h"] > 0) / len(sq) * 100
        if avg_sq > avg_no and avg_sq > 0.1:
            w = sum(1 for r in sq if r["ret_4h"] > 0)
            save_insight(
                db, user_id,
                f"Intraday: BB squeeze -> {avg_sq:+.2f}% avg 4h return, "
                f"{wr_sq:.0f}%wr (n={len(sq)}) vs non-squeeze {avg_no:+.2f}%",
                confidence=min(0.80, wr_sq / 100),
                wins=w, losses=len(sq) - w,
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
        save_insight(
            db, user_id,
            f"Intraday: squeeze + low vol {avg_low:+.2f}%/4h "
            f"vs squeeze + high vol {avg_high:+.2f}%/4h "
            f"(n={len(sq_vol_low)}+{len(sq_vol_high)})",
            confidence=0.5,
            wins=w_low, losses=len(sq_vol_low) - w_low,
            hypothesis_family=_fam,
        )
        discoveries += 1

    # Hypothesis: NR7 -> expansion profitable within 8h
    nr7s = [r for r in rows if r["nr7"]]
    if len(nr7s) >= 10:
        avg_nr7 = sum(r["ret_8h"] for r in nr7s) / len(nr7s)
        wr_nr7 = sum(1 for r in nr7s if r["ret_8h"] > 0) / len(nr7s) * 100
        w_nr7 = sum(1 for r in nr7s if r["ret_8h"] > 0)
        save_insight(
            db, user_id,
            f"Intraday: NR7 (narrow range 7) -> {avg_nr7:+.2f}% avg 8h return, "
            f"{wr_nr7:.0f}%wr (n={len(nr7s)})",
            confidence=min(0.75, wr_nr7 / 100),
            wins=w_nr7, losses=len(nr7s) - w_nr7,
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
        f"Mined {len(rows)} intraday bars from {len(tickers)} tickers, "
        f"{discoveries} breakout pattern discoveries [{_fam}]"
        + (f", bridge={bridge_n}" if bridge_n else ""),
    )

    return {
        "rows_mined": len(rows),
        "tickers": len(tickers),
        "discoveries": discoveries,
        "hypothesis_family": _fam,
        "scanpattern_bridge_created": bridge_n,
    }


def _mine_high_vol_one_ticker(ticker: str, budget: BrainResourceBudget | None) -> list[dict]:
    """15m rows where volatility is already expanded (ATR or BB width in top quartile vs 50-bar window)."""
    from ta.momentum import RSIIndicator
    from ta.trend import MACD, EMAIndicator, ADXIndicator
    from ta.volatility import BollingerBands, AverageTrueRange

    if budget is not None and not budget.try_ohlcv("high_vol_regime", 1):
        return []
    try:
        df = fetch_ohlcv_df(ticker, period="5d", interval="15m")
        if df.empty or len(df) < 80:
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
    bars_4h = 16
    bars_8h = 32

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
    """Phase-A discovery: crypto 15m bars in *expanded* vol (distinct from compression miner)."""
    from ...config import settings
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not getattr(settings, "brain_high_vol_miner_enabled", True):
        return {"discoveries": 0, "rows_mined": 0, "tickers": 0, "skipped": True}

    tickers = [t for t in DEFAULT_CRYPTO_TICKERS if str(t).endswith("-USD")][:30]
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_IO_WORKERS_LOW) as pool:
        futs = {pool.submit(_mine_high_vol_one_ticker, t, budget): t for t in tickers}
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

    if len(rows) < 30:
        return {
            "tested": 0,
            "note": "insufficient high-vol miner data",
            "hypothesis_family": BRAIN_HYPOTHESIS_FAMILY_HIGH_VOL,
        }

    discoveries = 0
    _fam = BRAIN_HYPOTHESIS_FAMILY_HIGH_VOL

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
                f"Crypto high-vol regime (15m): expanded ATR/BB vs calm — "
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
                f"Crypto high-vol 15m: EMA bullish stack {ab:+.2f}%/4h vs not "
                f"{ae:+.2f}%/4h (n={len(hv_bull)}+{len(hv_bear)})",
                confidence=0.55,
                wins=w_b, losses=len(hv_bull) - w_b,
                hypothesis_family=_fam,
            )
            discoveries += 1

    log_learning_event(
        db, user_id, "high_vol_pattern_mining",
        f"Mined {len(rows)} crypto 15m bars, {discoveries} high-vol regime insights [{_fam}]",
    )

    return {
        "rows_mined": len(rows),
        "tickers": len(tickers),
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
        resolved = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome != "pending",
            BreakoutAlert.resolved_at >= cutoff,
        ).order_by(BreakoutAlert.resolved_at.desc()).limit(500).all()
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
        
        # Also group by scan_pattern_id via related insight
        if alert.related_insight_id:
            insight = db.query(TradingInsight).get(alert.related_insight_id)
            if insight and insight.scan_pattern_id:
                pattern_outcomes[insight.scan_pattern_id].append({
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
        pattern.trade_count = (pattern.trade_count or 0) + total
        pattern.updated_at = datetime.utcnow()
        
        patterns_updated += 1
        
        logger.info(
            "[learning] Updated ScanPattern '%s' (id=%d) from real trades: "
            "win_rate=%.1f%% (was %.1f%%), avg_return=%.2f%% (was %.2f%%), n=%d",
            pattern.name, pattern.id,
            pattern.win_rate * 100, old_wr * 100,
            pattern.avg_return_pct, old_ret,
            total,
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
                pass
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
            pass

    for a in winners:
        try:
            sigs = _json.loads(a.signals_snapshot) if a.signals_snapshot else []
            for i in range(len(sigs)):
                for j in range(i + 1, min(i + 3, len(sigs))):
                    combo = tuple(sorted([sigs[i][:30], sigs[j][:30]]))
                    winner_sig_combos[combo] += 1
        except Exception:
            pass

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
            pass

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
    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futs = {pool.submit(_mine_from_history, t): t for t in mine_tickers}
        for f in as_completed(futs):
            if _shutting_down.is_set():
                break
            try:
                all_rows.extend(f.result())
            except Exception:
                pass

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
                    wr = wins / len(filtered) * 100
                    composite = avg_5d * 0.6 + (wr / 100) * 0.4

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
                        f"(avg {best_score:.2f}, {best_wr:.0f}%wr, n={best_n}) "
                        f"— refined from '{original_desc}'"
                    )
                    save_insight(
                        db, user_id, refined_label,
                        confidence=min(0.85, best_wr / 100),
                        wins=best_wins, losses=best_n - best_wins,
                    )
                    refined_count += 1
                    log_learning_event(
                        db, user_id, "pattern_refinement",
                        f"Refined '{original_desc}': best threshold "
                        f"{field}{op}{best_variant} "
                        f"({best_wr:.0f}%wr, n={best_n})",
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
        pass

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
        pass

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
        pass

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
        pass

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


# ── Multi-Signal Prediction Engine ────────────────────────────────────

def compute_prediction(indicator_data: dict) -> float:
    """Compute a directional prediction score from indicator data.

    Returns a score from -10 (strongly bearish) to +10 (strongly bullish).
    Each signal contributes a weighted vote. The final score is the sum
    clamped to [-10, +10].

    Signals used (with weights):
      RSI (2.0), MACD histogram (1.5), MACD crossover (1.0),
      EMA alignment (1.5), Bollinger Band position (1.0),
      Stochastic (1.0), ADX trend strength (0.5), Volume (0.5)
    """
    score = 0.0

    rsi_data = indicator_data.get("rsi", {})
    macd_data = indicator_data.get("macd", {})
    bb_data = indicator_data.get("bbands", {})
    stoch_data = indicator_data.get("stoch", {})
    adx_data = indicator_data.get("adx", {})
    ema20_data = indicator_data.get("ema_20", {})
    ema50_data = indicator_data.get("ema_50", {})
    ema100_data = indicator_data.get("ema_100", {})
    sma20_data = indicator_data.get("sma_20", {})
    obv_data = indicator_data.get("obv", {})
    atr_data = indicator_data.get("atr", {})

    rsi = rsi_data.get("value") if rsi_data else None
    if rsi is not None:
        if rsi < 25:
            score += 2.0
        elif rsi < 35:
            score += 1.5
        elif rsi < 45:
            score += 0.5
        elif rsi > 75:
            score -= 2.0
        elif rsi > 65:
            score -= 1.5
        elif rsi > 55:
            score -= 0.5

    macd_hist = macd_data.get("histogram") if macd_data else None
    macd_line = macd_data.get("macd") if macd_data else None
    macd_sig = macd_data.get("signal") if macd_data else None
    if macd_hist is not None:
        if macd_hist > 0:
            score += min(1.5, macd_hist * 10)
        else:
            score -= min(1.5, abs(macd_hist) * 10)
    if macd_line is not None and macd_sig is not None:
        if macd_line > macd_sig:
            score += 1.0
        elif macd_line < macd_sig:
            score -= 1.0

    e20 = ema20_data.get("value") if ema20_data else None
    e50 = ema50_data.get("value") if ema50_data else None
    e100 = ema100_data.get("value") if ema100_data else None
    sma20 = sma20_data.get("value") if sma20_data else None
    if e20 is not None and e50 is not None and e100 is not None:
        if e20 > e50 > e100:
            score += 1.5
        elif e20 < e50 < e100:
            score -= 1.5
        elif e20 > e50:
            score += 0.5
        elif e20 < e50:
            score -= 0.5

    bb_upper = bb_data.get("upper") if bb_data else None
    bb_lower = bb_data.get("lower") if bb_data else None
    if bb_upper and bb_lower and bb_upper > bb_lower:
        bb_mid = (bb_upper + bb_lower) / 2
        bb_range = bb_upper - bb_lower
        if sma20 is not None:
            bb_pos = (sma20 - bb_lower) / bb_range
        elif e20 is not None:
            bb_pos = (e20 - bb_lower) / bb_range
        else:
            bb_pos = 0.5
        if bb_pos < 0.15:
            score += 1.0
        elif bb_pos < 0.3:
            score += 0.5
        elif bb_pos > 0.85:
            score -= 1.0
        elif bb_pos > 0.7:
            score -= 0.5

    stoch_k = stoch_data.get("k") if stoch_data else None
    if stoch_k is not None:
        if stoch_k < 20:
            score += 1.0
        elif stoch_k < 30:
            score += 0.5
        elif stoch_k > 80:
            score -= 1.0
        elif stoch_k > 70:
            score -= 0.5

    adx_val = adx_data.get("adx") if adx_data else None
    if adx_val is not None and adx_val > 25:
        score *= 1.0 + min(0.5, (adx_val - 25) / 50)

    return max(-10.0, min(10.0, round(score, 2)))


def predict_direction(score: float) -> str:
    """Convert prediction score to a human-readable direction."""
    if score >= 3.0:
        return "bullish"
    elif score >= 1.0:
        return "slightly_bullish"
    elif score <= -3.0:
        return "bearish"
    elif score <= -1.0:
        return "slightly_bearish"
    return "neutral"


def predict_confidence(score: float) -> int:
    """Convert absolute prediction score to a confidence percentage (0-100)."""
    return min(100, int(abs(score) * 10))


def _build_prediction_tickers(db: Session, explicit: list[str] | None) -> list[str]:
    """Build a diverse ticker list for predictions from multiple sources."""
    if explicit:
        return explicit

    seen: set[str] = set()
    result: list[str] = []

    def _add(t: str):
        u = t.upper()
        if u not in seen:
            seen.add(u)
            result.append(u)

    recent_scans = (
        db.query(ScanResult.ticker, ScanResult.score)
        .order_by(ScanResult.scanned_at.desc())
        .limit(500)
        .all()
    )
    top_scanned = sorted(set((r.ticker, r.score) for r in recent_scans), key=lambda x: x[1], reverse=True)
    for ticker, _ in top_scanned[:60]:
        _add(ticker)

    try:
        from .prescreener import get_trending_crypto
        for t in get_trending_crypto()[:30]:
            _add(t)
    except Exception:
        pass

    try:
        from ..ticker_universe import get_all_crypto_tickers
        for t in get_all_crypto_tickers(n=120)[:40]:
            _add(t)
    except Exception:
        for t in DEFAULT_CRYPTO_TICKERS[:20]:
            _add(t)

    try:
        wl_items = get_watchlist(db, user_id=None)
        for item in wl_items[:20]:
            _add(item.ticker)
    except Exception:
        pass

    if len(result) < 40:
        for t in DEFAULT_SCAN_TICKERS[:30]:
            _add(t)
        for t in DEFAULT_CRYPTO_TICKERS[:15]:
            _add(t)

    return result


def _indicator_data_to_flat_snapshot(
    ind_data: dict[str, Any], price: float | None,
) -> dict[str, Any]:
    """Convert nested ``get_indicator_snapshot()`` output to flat dict for ``evaluate_patterns()``."""
    snap: dict[str, Any] = {}
    if price is not None:
        snap["price"] = price

    rsi_val = (ind_data.get("rsi") or {}).get("value")
    if rsi_val is not None:
        snap["rsi_14"] = rsi_val

    for ema_key in ("ema_20", "ema_50", "ema_100"):
        v = (ind_data.get(ema_key) or {}).get("value")
        if v is not None:
            snap[ema_key] = v

    sma20 = (ind_data.get("sma_20") or {}).get("value")
    if sma20 is not None:
        snap["sma_20"] = sma20

    macd_hist = (ind_data.get("macd") or {}).get("histogram")
    if macd_hist is not None:
        snap["macd_hist"] = macd_hist

    adx_val = (ind_data.get("adx") or {}).get("adx")
    if adx_val is not None:
        snap["adx"] = adx_val

    atr_val = (ind_data.get("atr") or {}).get("value")
    if atr_val is not None:
        snap["atr"] = atr_val

    bb = ind_data.get("bbands") or {}
    bb_upper = bb.get("upper")
    bb_lower = bb.get("lower")
    if bb_upper and bb_lower and bb_upper > bb_lower:
        bandwidth = (bb_upper - bb_lower) / ((bb_upper + bb_lower) / 2)
        snap["bb_squeeze"] = bandwidth < 0.04

    obv = (ind_data.get("obv") or {}).get("value")
    if obv is not None:
        snap["obv"] = obv

    stoch_k = (ind_data.get("stoch") or {}).get("k")
    if stoch_k is not None:
        snap["stoch_k"] = stoch_k

    rsi7 = (ind_data.get("rsi_7") or {}).get("value")
    if rsi7 is not None:
        snap["rsi_7"] = rsi7

    vz = (ind_data.get("volume_z_20") or {}).get("value")
    if vz is not None:
        snap["vol_z_20"] = vz

    rv = (ind_data.get("realized_vol_20") or {}).get("value")
    if rv is not None:
        snap["realized_vol_20"] = rv

    roc10 = (ind_data.get("roc_10") or {}).get("value")
    if roc10 is not None:
        snap["roc_10"] = roc10

    bb_pb = (ind_data.get("bb_pct_b") or {}).get("value")
    if bb_pb is not None:
        snap["bb_pct_b"] = bb_pb

    atrp = (ind_data.get("atr_percentile_60") or {}).get("value")
    if atrp is not None:
        snap["atr_percentile_60"] = atrp

    eq = ind_data.get("equity_regime")
    if isinstance(eq, dict) and eq.get("regime"):
        snap["regime"] = eq.get("regime")

    lv1 = ind_data.get("learned_v1")
    if isinstance(lv1, dict) and lv1.get("schema_version") == 1:
        snap["learned_v1_skew"] = lv1.get("return_skew_20")
        snap["learned_v1_range_pct"] = lv1.get("range_pct_20d")

    return snap


def _predict_single_ticker(
    ticker: str,
    quotes_map: dict[str, dict],
    vix: float | None,
    vol_regime: dict,
    meta_learner_ready: bool,
    meta_predict_fn,
    active_patterns: list | None = None,
) -> dict | None:
    """Predict a single ticker using the pattern-driven ML brain.

    Scoring tiers (graceful degradation):
      1. Meta-learner trained  -> probability from pattern feature model
      2. Patterns exist, no ML -> weighted soft-match fallback
      3. No patterns            -> neutral (score=0)
    """
    from .pattern_engine import evaluate_patterns_with_strength
    from .pattern_ml import extract_pattern_features

    try:
        snapshot = get_indicator_snapshot(ticker)
        if not snapshot or len(snapshot) < 3:
            return None
        ind_data = {k: v for k, v in snapshot.items() if k not in ("ticker", "interval")}

        quote = quotes_map.get(ticker)
        if not quote:
            quote = fetch_quote(ticker)
        price = quote["price"] if quote else None

        if not active_patterns:
            return None

        from .backtest_engine import TICKER_TO_SECTOR as _T2S
        _ticker_sector = _T2S.get(ticker)
        applicable_patterns = []
        for _pat in active_patterns:
            _scope = getattr(_pat, "ticker_scope", "universal") or "universal"
            if _scope == "universal":
                applicable_patterns.append(_pat)
            elif _scope == "ticker_specific":
                try:
                    _st = json.loads(getattr(_pat, "scope_tickers", None) or "[]")
                except (json.JSONDecodeError, TypeError):
                    _st = []
                if ticker in _st:
                    applicable_patterns.append(_pat)
            elif _scope == "sector":
                if _ticker_sector:
                    try:
                        _ss = json.loads(getattr(_pat, "scope_tickers", None) or "[]")
                    except (json.JSONDecodeError, TypeError):
                        _ss = []
                    if _ticker_sector in _ss:
                        applicable_patterns.append(_pat)
                else:
                    applicable_patterns.append(_pat)
            else:
                applicable_patterns.append(_pat)

        if not applicable_patterns:
            return None

        flat_snap = _indicator_data_to_flat_snapshot(ind_data, price) if price else {}
        if not flat_snap:
            return None

        matches = evaluate_patterns_with_strength(flat_snap, applicable_patterns)
        matched_patterns: list[dict] = []
        for m in matches:
            raw_wr = m.get("win_rate")
            wr_pct = round(raw_wr) if raw_wr is not None and raw_wr > 1 else (round(raw_wr * 100) if raw_wr else None)
            matched_patterns.append({
                "name": m["name"],
                "win_rate": wr_pct,
                "pattern_id": m.get("pattern_id"),
                "match_quality": m.get("match_quality"),
                "conditions_met": m.get("conditions_met"),
                "conditions_total": m.get("conditions_total"),
                "avg_strength": m.get("avg_strength"),
            })

        # --- Tier 1: meta-learner ---
        meta_prob = None
        if meta_learner_ready:
            pat_features = extract_pattern_features(active_patterns, flat_snap)
            meta_prob = meta_predict_fn(pat_features)

        if meta_prob is not None:
            blended_score = round((meta_prob - 0.5) * 20, 2)
        elif matched_patterns:
            # --- Tier 2: soft-match fallback ---
            pattern_score = 0.0
            for m in matches:
                raw_wr = m.get("win_rate") or 0.5
                wr = raw_wr / 100.0 if raw_wr > 1 else raw_wr
                quality = m.get("match_quality", 1.0)
                strength = m.get("avg_strength", 0.5)
                contrib = m.get("score_boost", 1.0) * max(0.5, wr) * quality * max(0.3, strength)
                pattern_score += contrib
            blended_score = max(-10.0, min(10.0, round(pattern_score, 2)))
        else:
            # --- Tier 3: neutral ---
            blended_score = 0.0

        regime = vol_regime.get("regime", "normal")
        if regime == "extreme":
            blended_score *= 0.6
        elif regime == "elevated":
            if abs(blended_score) < 3:
                blended_score *= 0.8

        blended_score = max(-10.0, min(10.0, round(blended_score, 2)))
        direction = predict_direction(blended_score)
        confidence = predict_confidence(blended_score)

        atr_val = (ind_data.get("atr") or {}).get("value")
        _cr = ticker.upper().endswith("-USD")
        _rd = 8 if _cr else 6
        stop = target = rr = pos_size_pct = None
        _vol_pct = (atr_val / price * 100) if price and atr_val else 0
        _stop_mult = 2.5 if _vol_pct > 3 else 2.0
        if price and atr_val and atr_val > 0:
            if blended_score > 0:
                stop = round(price - atr_val * _stop_mult, _rd)
                target = round(price + atr_val * 3.0, _rd)
            elif blended_score < 0:
                stop = round(price + atr_val * _stop_mult, _rd)
                target = round(price - atr_val * 3.0, _rd)
            if stop is not None and target is not None:
                risk = abs(price - stop)
                reward = abs(target - price)
                rr = round(reward / risk, 2) if risk > 0 else 0
                pos_size_pct = round(min(5.0, 1.0 / (risk / price * 100)) * 100 / 100, 2) if price > 0 else None

        return {
            "ticker": ticker,
            "price": price,
            "score": blended_score,
            "meta_ml_probability": round(meta_prob, 4) if meta_prob is not None else None,
            "direction": direction,
            "confidence": confidence,
            "signals": _explain_prediction(matched_patterns, blended_score),
            "matched_patterns": matched_patterns or [],
            "vix_regime": regime,
            "suggested_stop": stop,
            "suggested_target": target,
            "risk_reward": rr,
            "position_size_pct": pos_size_pct,
        }
    except Exception:
        return None


def get_current_predictions(db: Session, tickers: list[str] | None = None) -> list[dict]:
    """Generate live predictions for a set of tickers.

    Blends rule-based scores with ML probabilities and adjusts for
    volatility regime. Includes risk-management fields (stop, target, R:R).
    Uses ThreadPoolExecutor to process tickers in parallel for speed.

    When *tickers* is None (the common case from Top Picks), results are
    cached with stale-while-revalidate: 3 min fresh, 10 min stale.
    Explicit ticker lists bypass the cache.

    Phase 7: candidate-authoritative mirror reads require a **non-empty** explicit
    ticker list. ``None``, empty list, cache/SWR refresh, and inferred universes
    stay ``explicit_api_tickers=False`` (legacy-authoritative for mirror).
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
    return results


def _get_current_predictions_impl(
    db: Session,
    tickers: list[str] | None,
    *,
    explicit_api_tickers: bool = False,
    active_patterns_override: list | None = None,
    max_ticker_batch: int = 400,
) -> list[dict]:
    """Core prediction logic (no cache).  Pattern-driven ML pipeline.

    ``active_patterns_override``: when set, use instead of ``get_active_patterns(db)``.
    ``max_ticker_batch``: cap after universe build (fast eval may use a lower cap).
    """
    from .pattern_engine import get_active_patterns
    from .pattern_ml import get_meta_learner

    # Phase 7 choke-point: never mark implicit/inferred universes as API-explicit.
    if tickers is None or not tickers:
        explicit_api_tickers = False

    tickers = _build_prediction_tickers(db, tickers)
    _cap = max(1, min(int(max_ticker_batch), 800))
    ticker_batch = tickers[:_cap]

    vix = get_vix()
    vol_regime = get_volatility_regime(vix)

    meta = get_meta_learner()
    meta_ready = meta.is_ready()

    if active_patterns_override is not None:
        _active_patterns = list(active_patterns_override)
    else:
        try:
            _active_patterns = get_active_patterns(db)
        except Exception:
            _active_patterns = []

    if not _active_patterns:
        return []

    quotes_map = fetch_quotes_batch(ticker_batch)

    _workers = _IO_WORKERS_HIGH if (_use_massive() or _use_polygon()) else _IO_WORKERS_MED
    results = []
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futures = {
            pool.submit(
                _predict_single_ticker,
                t, quotes_map, vix, vol_regime,
                meta_ready, meta.predict,
                _active_patterns,
            ): t
            for t in ticker_batch
        }
        for fut in as_completed(futures):
            entry = fut.result()
            if entry is not None:
                results.append(entry)

    results.sort(key=lambda x: abs(x["score"]), reverse=True)

    from ...config import settings as _pred_mirror_settings
    from ...trading_brain.infrastructure.prediction_ops_log import (
        DUAL_WRITE_FAIL,
        DUAL_WRITE_NA,
        DUAL_WRITE_OK,
        DUAL_WRITE_SKIP_EMPTY,
        READ_ERROR,
        READ_NA,
        format_chili_prediction_ops_line,
        universe_fingerprint_fp16,
    )

    dual_write_outcome = DUAL_WRITE_NA
    dw_enabled = False
    try:
        dw_enabled = bool(getattr(_pred_mirror_settings, "brain_prediction_dual_write_enabled", False))
        if not dw_enabled:
            pass
        elif not results:
            dual_write_outcome = DUAL_WRITE_SKIP_EMPTY
        else:
            from ...trading_brain.infrastructure.prediction_line_mapper import (
                prediction_universe_fingerprint,
            )
            from ...trading_brain.infrastructure.prediction_mirror_session import (
                brain_prediction_mirror_write_dedicated,
            )

            _fp = prediction_universe_fingerprint(ticker_batch)
            brain_prediction_mirror_write_dedicated(
                legacy_rows=results,
                universe_fingerprint=_fp,
                ticker_count=len(ticker_batch),
            )
            dual_write_outcome = DUAL_WRITE_OK
    except Exception:
        logger.warning("[brain_prediction_dual_write] hook failed (legacy return preserved)", exc_info=True)
        if dw_enabled and results:
            dual_write_outcome = DUAL_WRITE_FAIL

    _fp16 = universe_fingerprint_fp16(ticker_batch)
    from ...trading_brain.infrastructure.prediction_read_phase5 import (
        PredictionReadOpsMeta,
        phase5_apply_prediction_read,
    )

    read_meta = PredictionReadOpsMeta(read=READ_NA, fp16=_fp16)
    try:
        results, read_meta = phase5_apply_prediction_read(
            results=results,
            ticker_batch=ticker_batch,
            explicit_api_tickers=explicit_api_tickers,
        )
    except Exception:
        logger.warning("[brain_prediction_read] hook_failed legacy preserved", exc_info=True)
        read_meta = PredictionReadOpsMeta(read=READ_ERROR, fp16=_fp16)

    if getattr(_pred_mirror_settings, "brain_prediction_ops_log_enabled", False):
        logger.info(
            format_chili_prediction_ops_line(
                dual_write=dual_write_outcome,
                read=read_meta.read,
                explicit_api_tickers=explicit_api_tickers,
                fp16=read_meta.fp16,
                snapshot_id=read_meta.snapshot_id,
                line_count=read_meta.line_count,
            )
        )

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


def run_promoted_pattern_fast_eval(db: Session) -> dict[str, Any]:
    """Scheduler entrypoint: refresh promoted-only cache when a full cycle is not running."""
    from ...config import settings

    if not getattr(settings, "brain_fast_eval_enabled", True):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    if get_learning_status().get("running"):
        return {"ok": True, "skipped": True, "reason": "learning_cycle_active"}

    return refresh_promoted_prediction_cache(db)


def _explain_prediction(
    matched_patterns: list[dict] | None,
    score: float,
) -> list[str]:
    """Generate human-readable explanations from matched patterns."""
    reasons: list[str] = []

    if matched_patterns:
        for mp in matched_patterns:
            wr = mp.get("win_rate")
            met = mp.get("conditions_met")
            total = mp.get("conditions_total")
            strength = mp.get("avg_strength")
            label = mp["name"]
            parts: list[str] = []
            if wr is not None:
                parts.append(f"{wr}% WR")
            if met is not None and total is not None and met < total:
                parts.append(f"{met}/{total} conditions")
            if strength is not None and strength < 1.0:
                parts.append(f"{round(strength * 100)}% strength")
            if parts:
                label += f" ({', '.join(parts)})"
            reasons.append(f"Pattern: {label}")

    if not reasons:
        if abs(score) < 0.5:
            reasons.append("No active patterns matched — neutral")
        else:
            reasons.append("Weak pattern signals")

    return reasons


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
    from ..ticker_universe import get_ticker_count
    from .scanner import get_scan_status

    total_patterns = db.query(TradingInsight).filter(
        TradingInsight.user_id == user_id, TradingInsight.active.is_(True),
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

    # Backfill predicted_score on snapshots that have indicator_data but no score
    backfill_predicted_scores(db, limit=500)

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
                snap.predicted_score = pred_score

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
        pass

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
        pass

    return {
        "total_patterns": total_patterns,
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
    "steps_completed": 0,
    "total_steps": 14,
    "patterns_found": 0,
    "tickers_processed": 0,
    "step_timings": {},
    "data_provider": None,
    "last_cycle_funnel": None,
    "last_cycle_budget": None,
}

# Phase 2: per-process context for brain DB shadow writes (cleared in shadow finally).
_brain_shadow_ctx: dict[str, Any] = {}

# Phase 3: global cycle lease enforcement (dedicated sessions; cleared in run_learning_cycle finally).
_brain_lease_enforcement_ctx: dict[str, Any] = {}


def get_learning_status() -> dict[str, Any]:
    status = dict(_learning_status)
    if status.get("running") and status.get("started_at"):
        try:
            started = datetime.fromisoformat(status["started_at"])
            status["elapsed_s"] = round((datetime.utcnow() - started).total_seconds(), 1)
        except Exception:
            pass
    try:
        from ...config import settings as _st

        if not getattr(_st, "brain_status_dual_read_enabled", False):
            return status
        from ...db import SessionLocal
        from ...trading_brain.wiring import dual_read_compare_status

        _sdb = SessionLocal()
        try:
            dual_read_compare_status(status, _sdb)
        finally:
            _sdb.close()
    except Exception as e:
        logger.warning("[brain_status_dual_read] skipped: %s", e)
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

        # Step A: LLM-based hypothesis discovery from internal data
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
    """Use LLM + recent trading data to propose new ScanPattern hypotheses.

    Analyzes recent high-scoring breakouts and missed opportunities to
    generate new pattern rules that the current patterns don't capture.
    """
    from .pattern_engine import create_pattern, list_patterns, get_active_patterns
    from ...models.trading import TradingInsight, ScanPattern

    existing = list_patterns(db)
    existing_names = {p["name"].lower() for p in existing}

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

    insight_summaries = []
    for ins in insights[:15]:
        insight_summaries.append(
            f"- {ins.pattern_description} (confidence={ins.confidence:.2f}, seen={ins.evidence_count}x)"
        )

    existing_summaries = []
    for p in existing[:10]:
        existing_summaries.append(f"- {p['name']}: {p.get('description', '')}")

    prompt = (
        "You are a quantitative trading pattern researcher. Based on these high-confidence "
        "learned trading insights, propose 1-3 NEW composable breakout patterns that are NOT "
        "already covered by the existing patterns.\n\n"
        "## Learned Insights:\n" + "\n".join(insight_summaries) + "\n\n"
        "## Existing Patterns:\n" + ("\n".join(existing_summaries) if existing_summaries else "(none)") + "\n\n"
        "For each proposed pattern, respond with JSON array. Each element:\n"
        '{"name": "...", "description": "...", "conditions": [{"indicator": "...", "op": "...", "value": ...}], '
        '"score_boost": 1.5, "min_base_score": 4.0}\n\n'
        "Available indicators: rsi_14, ema_9, ema_20, ema_50, ema_100, price, bb_squeeze, "
        "bb_squeeze_firing, adx, rel_vol, macd_hist, resistance_retests, "
        "dist_to_resistance_pct, narrow_range, vcp_count, vwap_reclaim, "
        "daily_change_pct, gap_pct, "
        "bullish_engulfing, bearish_engulfing, hammer, inverted_hammer, "
        "morning_star, doji.\n"
        "Available ops: >, >=, <, <=, ==, between, any_of.\n"
        "Respond ONLY with the JSON array, no other text."
    )

    try:
        from ..llm_caller import call_llm
        response = call_llm(prompt, max_tokens=1500)
        if not response:
            return []

        import json as _json
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        proposals = _json.loads(text)
        if not isinstance(proposals, list):
            proposals = [proposals]

        created = []
        for prop in proposals[:max_hypotheses]:
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
        logger.warning(f"[learning] Pattern hypothesis discovery failed: {e}")
        return []


def _find_insight_for_pattern(db: Session, pattern) -> Any | None:
    """Find the TradingInsight linked to a ScanPattern (for persisting backtests)."""
    from ...models.trading import TradingInsight
    return (
        db.query(TradingInsight)
        .filter(TradingInsight.scan_pattern_id == pattern.id)
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
    bt_kw = brain_pattern_backtest_friction_kwargs()

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
                    save_backtest(
                        db,
                        linked_insight.user_id,
                        result,
                        insight_id=linked_insight.id,
                        scan_pattern_id=pattern.id,
                    )
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
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
        "research_integrity": aggregate_promotion_integrity(integrity_rows),
    }

    patch: dict[str, Any] = {
        "confidence": round(new_confidence, 3),
        "win_rate": round(mean_is_wr, 1),
        "avg_return_pct": round(avg_return, 2),
        "backtest_count": (pattern.backtest_count or 0) + total,
        "evidence_count": (pattern.evidence_count or 0) + total,
        "promotion_status": prom_stat,
        "oos_win_rate": round(mean_oos_wr, 1) if mean_oos_wr is not None else None,
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
                        pass
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

    update_pattern(db, pattern.id, patch)

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
    {"indicator": "macd_hist", "op": ">", "value": 0},
    {"indicator": "rel_vol", "op": ">=", "value": 2.0},
    {"indicator": "price", "op": ">", "ref": "ema_20"},
    {"indicator": "price", "op": ">", "ref": "ema_50"},
    {"indicator": "price", "op": ">", "ref": "sma_50"},
    {"indicator": "bb_squeeze", "op": "==", "value": True},
    {"indicator": "daily_change_pct", "op": ">=", "value": 3.0},
    {"indicator": "gap_pct", "op": ">", "value": 2.0},
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
        pass

    return report


def evolve_exit_strategies(db: Session) -> dict[str, Any]:
    """Legacy alias — delegates to the full pattern evolution engine."""
    return evolve_pattern_strategies(db)


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

    for rp in root_patterns:
        child_count = (
            db.query(func.count(ScanPattern.id))
            .filter(ScanPattern.parent_id == rp.id, ScanPattern.active.is_(True))
            .scalar()
        )
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
            pass

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
# app.services.trading.learning_cycle_architecture (single source of truth).
# Use apply_learning_cycle_step_status(...); bump brain_network_graph meta.graph_version
# when changing public graph shape.


def run_learning_cycle(
    db: Session,
    user_id: int | None,
    full_universe: bool = True,
) -> dict[str, Any]:
    """Complete learning cycle: pre-filter -> scan -> snapshot -> backfill -> mine -> backtest -> journal -> signals.

    Uses the prescreener to narrow thousands of tickers to ~200-400
    interesting candidates before deep-scoring, making the cycle 10-30x
    faster than scanning the raw universe.
    """
    from .scanner import run_full_market_scan, _scan_status
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
    _learning_status["steps_completed"] = 0
    _learning_status["total_steps"] = 25
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

    def _step_time(name: str, t0: float, extra: str = "") -> None:
        elapsed = round(time.time() - t0, 1)
        _learning_status["step_timings"][name] = elapsed
        suffix = f" | {extra}" if extra else ""
        logger.info(f"[learning] Step '{name}' took {elapsed}s{suffix}")

    def _commit_step() -> None:
        try:
            from ...trading_brain.wiring import brain_shadow_before_commit

            brain_shadow_before_commit(
                db,
                ctx=_brain_shadow_ctx,
                learning_status=_learning_status,
            )
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

    interrupted = False
    report_error: str | None = None

    try:
        from ...config import settings
        from ...trading_brain.wiring import brain_shadow_begin_cycle

        brain_shadow_begin_cycle(
            db,
            ctx=_brain_shadow_ctx,
            full_universe=full_universe,
            data_provider=_provider,
            learning_status=_learning_status,
        )

        # Step 1: Pre-filter with Massive.com + yfinance screener
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_universe", "prefilter")
        from .prescreener import get_prescreened_candidates, get_prescreen_status
        candidates = get_prescreened_candidates()
        ps = get_prescreen_status()
        report["prescreen_candidates"] = len(candidates)
        report["prescreen_sources"] = ps.get("sources", {})
        _learning_status["tickers_processed"] = len(candidates)
        _learning_status["steps_completed"] = 1
        _step_time("pre-filter", step_start, f"{len(candidates)} candidates")
        _commit_step()

        # Step 2: Deep-score candidates
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_universe", "scan")
        scan_results = run_full_market_scan(db, user_id, use_full_universe=full_universe)
        report["tickers_scanned"] = _scan_status["tickers_total"]
        report["tickers_scored"] = len(scan_results)
        _learning_status["tickers_processed"] = len(scan_results)
        _learning_status["steps_completed"] = 2
        _step_time("scan", step_start, f"{len(scan_results)} scored via {_provider}")
        _commit_step()

        # Step 3: Snapshots (parallel, top 500 + watchlist)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_state", "snapshots")
        top_tickers = [r["ticker"] for r in scan_results[:800]]
        watchlist = get_watchlist(db, user_id)
        for w in watchlist:
            if w.ticker not in top_tickers:
                top_tickers.append(w.ticker)
        snap_count = take_snapshots_parallel(db, top_tickers, bar_interval="1d")
        try:
            from ...config import settings as _snap_settings
            if _snap_settings.brain_intraday_snapshots_enabled:
                crypto_sn = [
                    t for t in top_tickers if t.endswith("-USD")
                ][: max(1, int(_snap_settings.brain_intraday_max_tickers))]
                for raw_iv in _snap_settings.brain_intraday_intervals.split(","):
                    iv = raw_iv.strip()
                    if iv and iv != "1d" and crypto_sn:
                        snap_count += take_snapshots_parallel(
                            db, crypto_sn, bar_interval=iv,
                        )
        except Exception:
            pass
        report["snapshots_taken"] = snap_count
        _learning_status["steps_completed"] = 3
        _step_time("snapshots", step_start,
                    f"{snap_count}/{len(top_tickers)} tickers via {_provider}")
        _commit_step()

        # Step 4: Backfill future returns + predicted scores
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_state", "backfill")
        filled = backfill_future_returns(db)
        scores_filled = backfill_predicted_scores(db, limit=1000)
        report["returns_backfilled"] = filled
        report["scores_backfilled"] = scores_filled
        _learning_status["steps_completed"] = 4
        _step_time("backfill", step_start,
                    f"{filled} returns + {scores_filled} scores via {_provider}")
        _commit_step()

        # Step 4b: Confidence decay (prune stale insights early)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_state", "decay")
        decay_result = decay_stale_insights(db, user_id)
        report["insights_decayed"] = decay_result.get("decayed", 0)
        report["insights_pruned"] = decay_result.get("pruned", 0)
        _learning_status["steps_completed"] = 5
        _step_time("confidence_decay", step_start,
                    f"{decay_result.get('decayed', 0)} decayed, {decay_result.get('pruned', 0)} pruned")
        _commit_step()

        # Step 6: Mine patterns
        apply_learning_cycle_step_status(_learning_status, "c_discovery", "mine")
        step_start = time.time()
        discoveries = mine_patterns(db, user_id)
        report["patterns_discovered"] = len(discoveries)
        _learning_status["patterns_found"] = len(discoveries)
        _learning_status["steps_completed"] = 6
        _step_time("mine", step_start,
                    f"{len(discoveries)} patterns from OHLCV via {_provider}")
        _commit_step()

        # Step 5b: Active pattern seeking (boost under-sampled patterns)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_discovery", "seek")
        seek_result = seek_pattern_data(db, user_id)
        report["patterns_boosted"] = seek_result.get("sought", 0)
        _learning_status["steps_completed"] = 7
        _step_time("active_seek", step_start,
                    f"{seek_result.get('sought', 0)} boosted")
        _commit_step()

        # Step 6: Backtest TradingInsights (legacy; optional — ScanPattern queue is canonical)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_validation", "bt_insights")
        if getattr(settings, "brain_insight_backtest_on_cycle", False):
            bt_count = _auto_backtest_patterns(db, user_id)
            report["backtests_run"] = bt_count
            report["insight_backtests_skipped"] = False
            _step_time("backtest_insights", step_start, f"{bt_count} insight backtests via {_provider}")
        else:
            bt_count = 0
            report["insight_backtests_skipped"] = True
            _step_time("backtest_insights", step_start, "skipped (brain_insight_backtest_on_cycle=false)")
        _commit_step()

        # Step 6b: Backtest ScanPatterns from priority queue
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_validation", "bt_queue")
        queue_result = _auto_backtest_from_queue(db, user_id)
        report["queue_backtests_run"] = queue_result.get("backtests_run", 0)
        report["queue_patterns_processed"] = queue_result.get("patterns_processed", 0)
        report["queue_exploration_added"] = queue_result.get("queue_exploration_added", 0)
        report["queue_pending"] = queue_result.get("pending", 0)
        report["queue_empty"] = queue_result.get("queue_empty", True)
        report["backtests_run"] = bt_count + queue_result.get("backtests_run", 0)
        _learning_status["steps_completed"] = 8
        _step_time("backtest_queue", step_start,
                   f"{queue_result.get('patterns_processed', 0)} patterns, "
                   f"{queue_result.get('pending', 0)} still pending")
        _commit_step()

        # Step 6c: Fork / compare / promote ScanPattern variants (exit, entry, combo, …)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_evolution", "variants")
        try:
            evo_stats = evolve_pattern_strategies(db)
            report["evolution"] = evo_stats
        except Exception as e:
            logger.warning("[learning] evolve_pattern_strategies failed: %s", e)
            report["evolution"] = {}
        _learning_status["steps_completed"] = 9
        _step_time(
            "pattern_variant_evolution",
            step_start,
            str(report.get("evolution", {})),
        )
        _commit_step()

        # Step 8: Self-validation & weight evolution (with dynamic hypotheses)
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_evolution", "hypotheses")
        evolve_result = validate_and_evolve(db, user_id)
        report["hypotheses_tested"] = evolve_result.get("hypotheses_tested", 0)
        report["hypotheses_challenged"] = evolve_result.get("challenged", 0)
        report["real_trade_adjustments"] = evolve_result.get("real_trade_adjustments", 0)
        report["weights_evolved"] = evolve_result.get("weights_evolved", 0)
        report["hypothesis_patterns_spawned"] = sum(
            1 for d in evolve_result.get("details", []) if d.get("spawned_pattern_id")
        )
        _learning_status["steps_completed"] = 10
        _step_time("evolve", step_start,
                    f"{evolve_result.get('hypotheses_tested', 0)} hypotheses, "
                    f"{evolve_result.get('weights_evolved', 0)} weights evolved")
        _commit_step()

        # Step 8b: Breakout outcome learning
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start_bo = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_evolution", "breakout")
        bo_result = learn_from_breakout_outcomes(db, user_id)
        report["breakout_patterns_learned"] = bo_result.get("patterns_learned", 0)
        _learning_status["steps_completed"] = 11
        _step_time("breakout_outcomes", step_start_bo,
                    f"{bo_result.get('patterns_learned', 0)} patterns from "
                    f"{bo_result.get('total_resolved', 0)} resolved alerts")
        _commit_step()

        # Steps 8c–8j: Secondary miners (optional — disable for faster cycles)
        if getattr(settings, "brain_secondary_miners_on_cycle", True):
            # Step 8c: Intraday breakout pattern mining (15m data)
            if _shutting_down.is_set():
                raise InterruptedError("shutdown")
            step_start_id = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_secondary", "intraday_hv")
            intra_result = mine_intraday_patterns(db, user_id, cycle_budget)
            hv_result = mine_high_vol_regime_patterns(db, user_id, cycle_budget)
            report["intraday_discoveries"] = intra_result.get("discoveries", 0)
            report["high_vol_discoveries"] = hv_result.get("discoveries", 0)
            _learning_status["steps_completed"] = 12
            _step_time(
                "intraday_mining",
                step_start_id,
                f"compression {intra_result.get('discoveries', 0)} / high_vol {hv_result.get('discoveries', 0)} "
                f"from {intra_result.get('rows_mined', 0)} + {hv_result.get('rows_mined', 0)} bars",
            )
            _commit_step()

            # Step 8d: Refine patterns (parameter sweeping)
            if _shutting_down.is_set():
                raise InterruptedError("shutdown")
            step_start = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_secondary", "refine")
            refine_result = refine_patterns(db, user_id)
            report["patterns_refined"] = refine_result.get("refined", 0)
            _learning_status["steps_completed"] = 13
            _step_time("refine", step_start,
                        f"{refine_result.get('refined', 0)} patterns refined")
            _commit_step()

            # Step 8e: Exit optimization learning
            if _shutting_down.is_set():
                raise InterruptedError("shutdown")
            step_start = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_secondary", "exit")
            exit_result = learn_exit_optimization(db, user_id)
            report["exit_adjustments"] = exit_result.get("adjustments", 0)
            _learning_status["steps_completed"] = 14
            _step_time("exit_optimization", step_start,
                        f"{exit_result.get('adjustments', 0)} adjustments")
            _commit_step()

            # Step 8f: Fakeout pattern mining
            if _shutting_down.is_set():
                raise InterruptedError("shutdown")
            step_start = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_secondary", "fakeout")
            fakeout_result = mine_fakeout_patterns(db, user_id)
            report["fakeout_patterns"] = fakeout_result.get("patterns_found", 0)
            _learning_status["steps_completed"] = 15
            _step_time("fakeout_mining", step_start,
                        f"{fakeout_result.get('patterns_found', 0)} fakeout patterns")
            _commit_step()

            # Step 8g: Position sizing feedback
            if _shutting_down.is_set():
                raise InterruptedError("shutdown")
            step_start = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_secondary", "sizing")
            sizing_result = tune_position_sizing(db, user_id)
            report["sizing_adjustments"] = sizing_result.get("adjustments", 0)
            _learning_status["steps_completed"] = 16
            _step_time("position_sizing", step_start,
                        f"{sizing_result.get('adjustments', 0)} sizing adjustments")
            _commit_step()

            # Step 8h: Inter-alert learning
            if _shutting_down.is_set():
                raise InterruptedError("shutdown")
            step_start = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_secondary", "inter_alert")
            inter_result = learn_inter_alert_patterns(db, user_id)
            report["inter_alert_insights"] = inter_result.get("insights", 0)
            _learning_status["steps_completed"] = 17
            _step_time("inter_alert", step_start,
                        f"{inter_result.get('insights', 0)} inter-alert insights")
            _commit_step()

            # Step 8i: Timeframe performance learning
            if _shutting_down.is_set():
                raise InterruptedError("shutdown")
            step_start = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_secondary", "timeframe")
            tf_result = learn_timeframe_performance(db, user_id)
            report["timeframe_insights"] = tf_result.get("insights", 0)
            _learning_status["steps_completed"] = 18
            _step_time("timeframe_learning", step_start,
                        f"{tf_result.get('insights', 0)} timeframe insights")
            _commit_step()

            # Step 8j: Signal synergy mining
            if _shutting_down.is_set():
                raise InterruptedError("shutdown")
            step_start = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_secondary", "synergy")
            synergy_result = mine_signal_synergies(db, user_id)
            report["synergies_found"] = synergy_result.get("synergies_found", 0)
            _learning_status["steps_completed"] = 19
            _step_time("synergy_mining", step_start,
                        f"{synergy_result.get('synergies_found', 0)} synergies found")
            _commit_step()
        else:
            report["secondary_miners_skipped"] = True
            _learning_status["steps_completed"] = 19
            logger.info("[learning] Secondary miners skipped (brain_secondary_miners_on_cycle=false)")

        report["brain_resource_budget"] = cycle_budget.to_report_dict()

        # Step 19: Market journal
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_journal", "journal")
        journal = daily_market_journal(db, user_id)
        report["journal_written"] = journal is not None
        _learning_status["steps_completed"] = 20
        _step_time("journal", step_start)
        _commit_step()

        # Step 10: Signal events
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_journal", "signals")
        events = check_signal_events(db, user_id)
        report["signal_events"] = len(events)
        _learning_status["steps_completed"] = 21
        _step_time("signals", step_start, f"{len(events)} events")
        _commit_step()

        # Step 11: Train pattern meta-learner
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_meta", "ml")
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
        _learning_status["steps_completed"] = 22
        _step_time("ml_train", step_start,
                    f"acc={ml_result.get('cv_accuracy', 0):.3f}"
                    if ml_result.get("ok") else "skipped")
        _commit_step()

        # Step 12: Generate strategy proposals
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_meta", "proposals")
        try:
            from .alerts import generate_strategy_proposals
            proposals = generate_strategy_proposals(db, user_id)
            report["proposals_generated"] = len(proposals)
        except Exception as e:
            logger.warning(f"[trading] Strategy proposal generation failed: {e}")
            report["proposals_generated"] = 0
        _learning_status["steps_completed"] = 23
        _step_time("proposals", step_start,
                    f"{report.get('proposals_generated', 0)} generated")
        _commit_step()

        # Step 13b: Pattern engine — discover, test, evolve
        if _shutting_down.is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(_learning_status, "c_meta", "pattern_engine")
        try:
            pe_result = _run_pattern_engine_cycle(db, user_id)
            report["patterns_discovered_engine"] = pe_result.get("hypotheses_generated", 0)
            report["patterns_tested"] = pe_result.get("patterns_tested", 0)
            report["patterns_evolved"] = pe_result.get("patterns_evolved", 0)
        except Exception as e:
            logger.warning(f"[trading] Pattern engine cycle failed: {e}")
        _step_time("pattern_engine", step_start, "done")
        _commit_step()

        # Step 13c: Cycle AI report (deep study) — synthesize cycle into stored markdown
        report["cycle_ai_report_id"] = None
        if not _shutting_down.is_set():
            step_start = time.time()
            apply_learning_cycle_step_status(_learning_status, "c_meta", "cycle_report")
            report["elapsed_s_pre_report"] = round(time.time() - start, 1)
            try:
                from .learning_cycle_report import generate_and_store_cycle_report

                rid = generate_and_store_cycle_report(db, user_id, report)
                report["cycle_ai_report_id"] = rid
            except Exception as e:
                logger.warning("[trading] Cycle AI report failed: %s", e)
            _learning_status["steps_completed"] = 24
            _step_time(
                "cycle_ai_report",
                step_start,
                f"id={report.get('cycle_ai_report_id')}" if report.get("cycle_ai_report_id") else "failed",
            )
            _commit_step()
        else:
            _learning_status["steps_completed"] = 24

        # Step 13d: Live vs research depromotion (optional)
        if not _shutting_down.is_set():
            apply_learning_cycle_step_status(_learning_status, "c_meta", "depromote")
            try:
                report["live_depromotion"] = run_live_pattern_depromotion(db)
            except Exception as e:
                logger.warning("[learning] live depromotion failed: %s", e)
                report["live_depromotion"] = {"ok": False, "error": str(e)}

        # Step 14: Finalize + log
        apply_learning_cycle_step_status(_learning_status, "c_meta", "finalize")
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
        _commit_step()
        _learning_status["steps_completed"] = 25
        _commit_step()

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
            pass
    finally:
        elapsed = time.time() - start
        _lease_release_holder = _brain_lease_enforcement_ctx.get("holder_id")
        _lease_release_acquired = bool(_brain_lease_enforcement_ctx.get("acquired"))
        _brain_lease_enforcement_ctx.clear()
        _learning_status["running"] = False
        _learning_status["phase"] = "idle"
        _learning_status["current_step"] = ""
        _learning_status["last_run"] = datetime.utcnow().isoformat()
        _learning_status["last_duration_s"] = round(elapsed, 1)
        report["elapsed_s"] = round(elapsed, 1)
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
            pass

        try:
            from ...trading_brain.wiring import brain_shadow_finally

            brain_shadow_finally(
                db,
                ctx=_brain_shadow_ctx,
                learning_status=_learning_status,
                interrupted=interrupted,
                report_error=report_error,
            )
        except Exception as e:
            logger.warning("[brain_shadow] finally hook failed (ignored): %s", e)

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

    try:
        from ..brain_worker_signals import persist_last_cycle_digest_json

        persist_last_cycle_digest_json(db, build_cycle_ui_digest(report))
    except Exception as _ui_d:
        logger.warning("[learning] persist cycle UI digest failed: %s", _ui_d)

    logger.info(
        f"[learning] Learning cycle finished in {elapsed:.0f}s "
        f"(provider={report.get('data_provider', 'unknown')}): {report}"
    )
    return {"ok": True, **report}


def should_run_learning() -> bool:
    if _learning_status["running"]:
        return False
    last = _learning_status.get("last_run")
    if last is None:
        return True
    try:
        from ...config import settings
        cooldown = max(1, settings.learning_interval_hours)
        last_dt = datetime.fromisoformat(last)
        return datetime.utcnow() - last_dt > timedelta(hours=cooldown)
    except Exception:
        return True
