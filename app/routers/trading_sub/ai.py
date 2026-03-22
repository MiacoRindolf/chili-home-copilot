"""AI Brain / learning endpoints for the trading module."""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from ...db import DATA_DIR
from ...deps import get_db, get_identity_ctx
from ...json_safe import to_jsonable
from ...models.trading import LearningCycleAiReport
from ...logger import log_info, new_trace_id
from ...prompts import load_prompt
from ...services import trading_service as ts
from ...services import trading_scheduler
from ...services import ticker_universe

router = APIRouter(tags=["trading-ai"])
_log = logging.getLogger(__name__)

_TRADING_PROMPT: str | None = None
_TRADING_PROMPT_MTIME: float = 0.0


def _get_trading_prompt() -> str:
    """Load trading analyst prompt, auto-reloading when the file changes."""
    global _TRADING_PROMPT, _TRADING_PROMPT_MTIME
    from pathlib import Path
    prompt_path = Path(__file__).resolve().parent.parent.parent / "prompts" / "trading_analyst.txt"
    try:
        current_mtime = prompt_path.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    if _TRADING_PROMPT is None or current_mtime != _TRADING_PROMPT_MTIME:
        _TRADING_PROMPT = load_prompt("trading_analyst")
        _TRADING_PROMPT_MTIME = current_mtime
    return _TRADING_PROMPT


# ── AI Analysis ────────────────────────────────────────────────────────

@router.get("/api/trading/brain/stats")
def api_brain_stats(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    stats = ts.get_brain_stats(db, ctx["user_id"])
    # region agent log
    try:
        from ...debug_agent_log import agent_log, safe_db_fingerprint

        agent_log(
            "H1",
            "trading_sub.ai.api_brain_stats",
            "brain_stats_identity",
            {
                "user_id": ctx.get("user_id"),
                "is_guest": ctx.get("is_guest"),
                "total_patterns": stats.get("total_patterns"),
                "db_fingerprint": safe_db_fingerprint(),
            },
        )
    except Exception:
        pass
    # endregion
    return JSONResponse({"ok": True, **stats})


@router.get("/api/trading/brain/confidence-history")
def api_confidence_history(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    history = ts.get_confidence_history(db, ctx["user_id"])
    return JSONResponse({"ok": True, "data": history})


@router.get("/api/trading/brain/activity")
def api_brain_activity(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    events = ts.get_learning_events(db, ctx["user_id"], limit=50)
    return JSONResponse({"ok": True, "events": [
        {
            "id": e.id,
            "event_type": e.event_type,
            "description": e.description,
            "confidence_before": e.confidence_before,
            "confidence_after": e.confidence_after,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]})


@router.get("/api/trading/brain/volatility")
def api_volatility_regime():
    regime = ts.get_volatility_regime()
    return JSONResponse({"ok": True, **regime})


@router.get("/api/trading/brain/predictions")
def api_brain_predictions(
    request: Request,
    db: Session = Depends(get_db),
    tickers: str = Query("", description="Comma-separated tickers to predict"),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers else None
    predictions = ts.get_current_predictions(db, ticker_list)
    return JSONResponse({"ok": True, "predictions": predictions})


@router.get("/api/trading/brain/status")
def api_trading_brain_status(request: Request, db: Session = Depends(get_db)):
    """Learning pipeline + brain worker snapshot (alias for external Trading Brain UIs).

    If your UI is served over **HTTPS** (e.g. ``https://localhost:5001``), the CHILI API
    must also use **HTTPS** on the configured port — otherwise the browser blocks the call
    (mixed content). See ``docs/TRADING_BRAIN_HTTPS.md``.
    """
    learning: dict = {}
    try:
        learning = ts.get_learning_status()
    except Exception as e:
        _log.warning("api_trading_brain_status learning: %s", e)
        learning = {"running": False, "phase": "error", "error": str(e)}

    # Reuse worker status handler (defined below; resolved at call time).
    worker_resp = api_brain_worker_status(request, db)
    try:
        raw = worker_resp.body
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        worker = json.loads(raw.decode("utf-8"))
    except Exception as e:
        _log.warning("api_trading_brain_status worker decode: %s", e)
        worker = {"ok": False, "error": "worker_status_unavailable"}

    return JSONResponse({"ok": True, "learning": learning, "worker": worker})


@router.get("/api/trading/brain/thesis")
def api_brain_thesis(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    thesis = ts.generate_market_thesis(db, ctx["user_id"])
    return JSONResponse({"ok": True, **thesis})


def _cycle_reports_base_query(db: Session, user_id: int | None):
    q = db.query(LearningCycleAiReport)
    if user_id is not None:
        return q.filter(LearningCycleAiReport.user_id == user_id)
    return q.filter(LearningCycleAiReport.user_id.is_(None))


@router.get("/api/trading/brain/cycle-reports")
def api_brain_cycle_reports(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    ctx = get_identity_ctx(request, db)
    uid = ctx["user_id"]
    base = _cycle_reports_base_query(db, uid)
    total = base.count()
    # region agent log
    try:
        from ...debug_agent_log import agent_log, safe_db_fingerprint

        agent_log(
            "H3",
            "trading_sub.ai.api_brain_cycle_reports",
            "cycle_reports_scope",
            {
                "user_id": uid,
                "is_guest": ctx.get("is_guest"),
                "total_reports": total,
                "query_branch": "user_id_match" if uid is not None else "user_id_is_null_only",
                "db_fingerprint": safe_db_fingerprint(),
            },
        )
    except Exception:
        pass
    # endregion
    rows = (
        base.order_by(LearningCycleAiReport.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = []
    for r in rows:
        preview_src = (r.content or "").strip().replace("\r\n", "\n")
        preview = preview_src[:200] + ("…" if len(preview_src) > 200 else "")
        if not preview and preview_src:
            preview = preview_src[:200]
        items.append({
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "preview": preview,
            "metrics": r.metrics_json if isinstance(r.metrics_json, dict) else {},
        })
    return JSONResponse({"ok": True, "items": items, "total": total, "offset": offset, "limit": limit})


@router.get("/api/trading/brain/cycle-reports/{report_id}")
def api_brain_cycle_report_detail(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    uid = ctx["user_id"]
    row = db.query(LearningCycleAiReport).filter(LearningCycleAiReport.id == report_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if uid is None:
        if row.user_id is not None:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif row.user_id != uid:
        raise HTTPException(status_code=403, detail="Forbidden")
    metrics = row.metrics_json if isinstance(row.metrics_json, dict) else {}
    return JSONResponse({
        "ok": True,
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "content": row.content or "",
        "metrics": metrics,
    })


@router.post("/api/trading/learn/weekly-review")
def api_weekly_review(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    review = ts.weekly_performance_review(db, ctx["user_id"])
    return JSONResponse({"ok": True, "review": review or "No trades to review yet."})


@router.post("/api/trading/scan/full")
def api_full_scan(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    status = ts.get_learning_status()

    if status["running"]:
        return JSONResponse({
            "ok": False,
            "message": "Learning cycle already in progress",
            "status": status,
        })

    from ...db import SessionLocal

    def _bg_full_learn(user_id):
        sdb = SessionLocal()
        try:
            ts.run_learning_cycle(sdb, user_id, full_universe=True)
        finally:
            sdb.close()

    background_tasks.add_task(_bg_full_learn, ctx["user_id"])
    return JSONResponse({
        "ok": True,
        "message": "Full market learning cycle started in background",
        "universe": ticker_universe.get_ticker_count(),
    })


def _safe_scan_status_part(label: str, fn, default):
    """Run *fn*; on failure log and return *default* so the Brain UI still gets 200 JSON."""
    try:
        return fn()
    except Exception:
        _log.exception("api_scan_status: %s failed", label)
        return default


@router.get("/api/trading/scan/status")
def api_scan_status():
    """Aggregate scan / learning / prescreen / scheduler state for the Brain UI.

    Values are passed through :func:`to_jsonable` so numpy/pandas scalars and
    non-finite floats cannot break JSON encoding (Starlette uses ``allow_nan=False``).

    Each subsystem is loaded independently so one exception cannot blank the whole response.
    """
    payload = {
        "ok": True,
        "scan": _safe_scan_status_part("scan", ts.get_scan_status, {}),
        "learning": _safe_scan_status_part("learning", ts.get_learning_status, {}),
        "prescreen": _safe_scan_status_part("prescreen", ts.get_prescreen_status, {}),
        "scheduler": _safe_scan_status_part(
            "scheduler",
            trading_scheduler.get_scheduler_info,
            {"running": False, "jobs": []},
        ),
    }
    try:
        return JSONResponse(to_jsonable(payload))
    except Exception:
        _log.exception("api_scan_status: JSON encode failed after to_jsonable")
        # Last resort: minimal safe payload (still 200 so DevTools isn't full of red)
        return JSONResponse(
            {
                "ok": True,
                "scan": {},
                "learning": {},
                "prescreen": {},
                "scheduler": {"running": False, "jobs": []},
                "encode_error": True,
            }
        )


@router.get("/api/trading/universe")
def api_ticker_universe():
    counts = ticker_universe.get_ticker_count()
    return JSONResponse({"ok": True, **counts})


@router.post("/api/trading/universe/refresh")
def api_refresh_universe():
    counts = ticker_universe.refresh_ticker_cache()
    return JSONResponse({"ok": True, "message": "Ticker cache refreshed", **counts})


@router.post("/api/trading/learn/trigger")
def api_trigger_learning(background_tasks: BackgroundTasks, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)

    if ts.get_learning_status()["running"]:
        return JSONResponse({"ok": False, "message": "Already running"})

    from ...db import SessionLocal

    def _bg(user_id):
        sdb = SessionLocal()
        try:
            ts.run_learning_cycle(sdb, user_id, full_universe=True)
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Learning cycle triggered"})


@router.post("/api/trading/learn/deep-study")
def api_deep_study(request: Request, db: Session = Depends(get_db)):
    try:
        ctx = get_identity_ctx(request, db)
        result = ts.deep_study(db, ctx["user_id"])
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/trading/learn/retrain-ml")
def api_retrain_ml(request: Request, db: Session = Depends(get_db)):
    from app.services.trading.pattern_ml import get_meta_learner, apply_ml_feedback
    meta = get_meta_learner()
    result = meta.train(db)
    if result.get("ok"):
        imps = meta.get_pattern_importances()
        fb = apply_ml_feedback(db, imps)
        result["feedback"] = fb
    return JSONResponse({"ok": True, **result})


@router.get("/api/trading/brain/accuracy-detail")
def api_accuracy_detail(
    request: Request,
    db: Session = Depends(get_db),
    type: str = Query("all", description="all|stock|crypto|strong"),
    limit: int = Query(20, ge=1, le=100),
):
    rows = ts.get_accuracy_detail(db, detail_type=type, limit=limit)
    return JSONResponse({"ok": True, "rows": rows, "total": len(rows)})


@router.post("/api/trading/learn/dedup-patterns")
def api_dedup_patterns(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    result = ts.dedup_existing_patterns(db, ctx["user_id"])
    return JSONResponse({"ok": True, **result})


@router.post("/api/trading/learn/patterns/{pattern_id}/demote")
def api_demote_pattern(pattern_id: int, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    from ...models.trading import TradingInsight
    ins = db.query(TradingInsight).filter(
        TradingInsight.id == pattern_id,
        TradingInsight.user_id == ctx["user_id"],
    ).first()
    if not ins:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)
    ins.active = False
    db.commit()
    ts.log_learning_event(
        db, ctx["user_id"], "demotion",
        f"Pattern manually demoted: {ins.pattern_description[:100]}",
        confidence_before=ins.confidence,
        related_insight_id=ins.id,
    )
    return JSONResponse({"ok": True, "message": "Pattern demoted"})


@router.get("/api/trading/learn/backfill-status")
def api_backfill_status():
    """Return progress of the background backtest backfill."""
    from ...main import _backfill_state
    return JSONResponse({"ok": True, **_backfill_state})


# ── Canonical backtest stats (single source of truth for WR) ──────────

def compute_pattern_bt_stats(
    db: Session,
    scan_pattern_ids: list[int],
    user_id: int,
) -> dict[int, dict]:
    """Return canonical backtest stats per scan_pattern_id.

    Single source of truth for win rate, used by every endpoint.
    Returns {sp_id: {"wins": int, "losses": int, "total": int,
                     "win_rate": float|None, "avg_return_pct": float|None,
                     "tickers": list[str]}}.
    """
    if not scan_pattern_ids:
        return {}

    from sqlalchemy import func as sa_func, case as sa_case
    from ...models.trading import TradingInsight, BacktestResult

    sp_to_insights: dict[int, list[int]] = {}
    for row in (
        db.query(TradingInsight.scan_pattern_id, TradingInsight.id)
        .filter(
            TradingInsight.scan_pattern_id.in_(scan_pattern_ids),
            TradingInsight.user_id == user_id,
        )
        .all()
    ):
        sp_to_insights.setdefault(row[0], []).append(row[1])

    all_insight_ids: list[int] = []
    for ids in sp_to_insights.values():
        all_insight_ids.extend(ids)

    if not all_insight_ids:
        return {}

    ins_to_sp: dict[int, int] = {}
    for sp_id, ins_ids in sp_to_insights.items():
        for iid in ins_ids:
            ins_to_sp[iid] = sp_id

    stats: dict[int, dict] = {}

    for row in (
        db.query(
            BacktestResult.related_insight_id,
            sa_func.count(BacktestResult.id),
            sa_func.sum(sa_case((BacktestResult.return_pct > 0, 1), else_=0)),
            sa_func.avg(BacktestResult.return_pct),
        )
        .filter(
            BacktestResult.related_insight_id.in_(all_insight_ids),
            BacktestResult.trade_count > 0,
        )
        .group_by(BacktestResult.related_insight_id)
        .all()
    ):
        ins_id, total, wins, avg_ret = row
        sp_id = ins_to_sp.get(ins_id)
        if sp_id is None:
            continue
        existing = stats.get(sp_id)
        if existing:
            old_total = existing["total"]
            existing["total"] += (total or 0)
            existing["wins"] += int(wins or 0)
            new_total = existing["total"]
            existing["avg_return_pct"] = round(
                (existing["avg_return_pct"] * old_total + float(avg_ret or 0) * (total or 0)) / max(1, new_total), 2
            )
        else:
            stats[sp_id] = {
                "total": total or 0,
                "wins": int(wins or 0),
                "avg_return_pct": round(float(avg_ret or 0), 2),
            }

    for row in (
        db.query(BacktestResult.related_insight_id, BacktestResult.ticker)
        .filter(BacktestResult.related_insight_id.in_(all_insight_ids))
        .all()
    ):
        sp_id = ins_to_sp.get(row[0])
        if sp_id is None:
            continue
        stats.setdefault(sp_id, {"total": 0, "wins": 0, "avg_return_pct": None})
        stats[sp_id].setdefault("tickers", []).append(row[1])

    for sp_id, s in stats.items():
        total = s["total"]
        wins = s["wins"]
        losses = total - wins
        s["losses"] = losses
        s["win_rate"] = round(wins / max(1, total) * 100, 1) if total > 0 else None
        s["tickers"] = list(set(s.get("tickers", [])))

    return stats


def _resolve_scan_pattern_id_for_insight(db: Session, insight) -> int | None:
    """Best-effort ScanPattern id for aggregating backtests (single resolver)."""
    from ...services.trading.pattern_resolution import (
        resolve_scan_pattern_id_for_insight as _pattern_id_from_insight,
    )

    return _pattern_id_from_insight(db, insight)


def _sync_win_stats_in_description(
    desc: str,
    win_rate: float | None,
    wins: int,
    losses: int,
) -> str:
    """Replace stale '(NN% win, MMM samples)' in description with live numbers."""
    if win_rate is None or wins + losses <= 0:
        return desc
    total = wins + losses
    repl = f"({win_rate}% win, {total} samples)"
    patterns = [
        # Standard: (61% win, 152 samples)
        r"\(\s*\d+(?:\.\d+)?%\s*win\s*,\s*\d+\s*samples?\s*\)",
        # "wins" typo / variant
        r"\(\s*\d+(?:\.\d+)?%\s*wins?\s*,\s*\d+\s*samples?\s*\)",
    ]
    new_desc = desc
    for pat in patterns:
        new_desc, n = re.subn(pat, repl, new_desc, count=1, flags=re.IGNORECASE)
        if n:
            return new_desc
    # Any parenthetical that contains a % win clause (avoids matching "(RSI<25)")
    new_desc, n = re.subn(
        r"\(\s*\d+(?:\.\d+)?%\s*win[^)]*\)",
        repl,
        desc,
        count=1,
        flags=re.IGNORECASE,
    )
    return new_desc if n else desc


def _sibling_insight_ids_for_pattern(
    db: Session,
    primary_insight_id: int,
    scan_pattern_id: int | None,
    sp_resolved_id: int | None,
) -> list[int]:
    """Insight ids that share the same ScanPattern (for backtest linkage)."""
    from ...models.trading import TradingInsight

    sid = scan_pattern_id or sp_resolved_id
    if not sid:
        return [primary_insight_id]
    ids = [
        r[0]
        for r in db.query(TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id == sid)
        .all()
    ]
    if primary_insight_id not in ids:
        ids = [*ids, primary_insight_id]
    return ids


def _evidence_backtest_asset_universe(
    db: Session,
    desc: str,
    scan_pattern_id: int | None,
    insight_id: int | None = None,
) -> str:
    """Match backtest_engine: crypto-only / stocks-only / all for evidence aggregation."""
    from ...models.trading import ScanPattern
    from ...services.trading.backtest_engine import (
        _extract_context,
        effective_backtest_asset_universe,
    )

    ac = None
    if scan_pattern_id:
        sp = db.query(ScanPattern).get(scan_pattern_id)
        if sp:
            ac = getattr(sp, "asset_class", None)
    ctx = _extract_context(desc or "", db=db, insight_id=insight_id)
    return effective_backtest_asset_universe(ac, ctx)


def _compute_deduped_backtest_win_stats(
    db: Session,
    sibling_insight_ids: list[int],
    desc: str,
    *,
    linked_limit: int = 500,
    asset_universe: str = "all",
) -> dict:
    """Same backtest list + win stats as the pattern evidence modal (dedupe: latest per ticker/strategy)."""
    from ...models.trading import BacktestResult
    from ...services.trading.market_data import is_crypto as _is_crypto_bt

    backtests_out: list[dict] = []
    seen_bt_ids: set[int] = set()
    seen_bt_keys: set[tuple[str, str]] = set()
    try:
        linked_bts = (
            db.query(BacktestResult)
            .filter(BacktestResult.related_insight_id.in_(sibling_insight_ids))
            .order_by(BacktestResult.ran_at.desc())
            .limit(linked_limit)
            .all()
        )
        for bt in linked_bts:
            if asset_universe == "crypto" and not _is_crypto_bt(bt.ticker or ""):
                continue
            if asset_universe == "stocks" and _is_crypto_bt(bt.ticker or ""):
                continue
            dedup_key = (bt.ticker or "", bt.strategy_name or "")
            if dedup_key in seen_bt_keys:
                continue
            seen_bt_keys.add(dedup_key)
            seen_bt_ids.add(bt.id)
            backtests_out.append({
                "id": bt.id,
                "ticker": bt.ticker,
                "strategy_name": bt.strategy_name,
                "return_pct": bt.return_pct,
                "win_rate": bt.win_rate,
                "sharpe": bt.sharpe,
                "max_drawdown": bt.max_drawdown,
                "trade_count": bt.trade_count,
                "ran_at": bt.ran_at.isoformat() if bt.ran_at else None,
                "params": bt.params,
                "relevance": 100,
            })

        # Keyword padding from global backtests muddles crypto vs stock evidence; skip when restricted.
        if len(backtests_out) < 15 and asset_universe == "all":
            bt_keywords = _extract_keywords_for_matching(desc, min_len=5)
            all_bts = (
                db.query(BacktestResult)
                .order_by(BacktestResult.ran_at.desc())
                .limit(3000)
                .all()
            )
            for bt in all_bts:
                if bt.id in seen_bt_ids:
                    continue
                strat_lower = (bt.strategy_name or "").lower()
                params_lower = (bt.params or "").lower()
                combined = strat_lower + " " + params_lower
                hits = sum(1 for kw in bt_keywords if kw in combined)
                if hits >= 2:
                    backtests_out.append({
                        "id": bt.id,
                        "ticker": bt.ticker,
                        "strategy_name": bt.strategy_name,
                        "return_pct": bt.return_pct,
                        "win_rate": bt.win_rate,
                        "sharpe": bt.sharpe,
                        "max_drawdown": bt.max_drawdown,
                        "trade_count": bt.trade_count,
                        "ran_at": bt.ran_at.isoformat() if bt.ran_at else None,
                        "params": bt.params,
                        "relevance": hits,
                    })
                    if len(backtests_out) >= 15:
                        break
        backtests_out.sort(
            key=lambda x: (
                0 if (x.get("trade_count") or 0) > 0 else 1,
                -(x.get("return_pct") or 0),
                -x.get("relevance", 0),
            )
        )
    except Exception:
        pass

    bt_with_trades = [b for b in backtests_out if (b.get("trade_count") or 0) > 0]
    bt_wins = sum(1 for b in bt_with_trades if (b.get("return_pct") or 0) > 0)
    bt_losses = len(bt_with_trades) - bt_wins
    bt_win_rate = (
        round(bt_wins / max(1, len(bt_with_trades)) * 100, 1) if bt_with_trades else None
    )
    bt_avg_return = (
        round(sum(b.get("return_pct") or 0 for b in bt_with_trades) / len(bt_with_trades), 1)
        if bt_with_trades else None
    )
    return {
        "backtests_out": backtests_out,
        "bt_wins": bt_wins,
        "bt_losses": bt_losses,
        "bt_win_rate": bt_win_rate,
        "bt_avg_return": bt_avg_return,
    }


def _deduped_win_rate_progress_series(
    db: Session,
    sibling_insight_ids: list[int],
    desc: str = "",
    *,
    row_limit: int = 4000,
    asset_universe: str = "all",
) -> list[dict]:
    """Chronological series: after each qualifying backtest, deduped win rate (latest per ticker/strategy).

    Includes sibling-linked runs plus the same keyword-matched global rows used to pad the
    evidence backtest list, so the line chart aligns with what users see in the Backtests tab.
    """
    from ...models.trading import BacktestResult
    from ...services.trading.market_data import is_crypto as _is_crypto_bt

    seen_ids: set[int] = set()
    rows: list = []
    for bt in (
        db.query(BacktestResult)
        .filter(BacktestResult.related_insight_id.in_(sibling_insight_ids))
        .order_by(BacktestResult.ran_at.asc())
        .limit(row_limit)
        .all()
    ):
        if asset_universe == "crypto" and not _is_crypto_bt(bt.ticker or ""):
            continue
        if asset_universe == "stocks" and _is_crypto_bt(bt.ticker or ""):
            continue
        rows.append(bt)
        seen_ids.add(bt.id)

    if (desc or "").strip() and asset_universe == "all":
        bt_keywords = _extract_keywords_for_matching(desc, min_len=5)
        if bt_keywords:
            # Newest-first pool (matches evidence keyword padding), not oldest-N rows.
            for bt in (
                db.query(BacktestResult)
                .order_by(BacktestResult.ran_at.desc())
                .limit(3000)
                .all()
            ):
                if bt.id in seen_ids:
                    continue
                strat_lower = (bt.strategy_name or "").lower()
                params_lower = (bt.params or "").lower()
                combined = strat_lower + " " + params_lower
                hits = sum(1 for kw in bt_keywords if kw in combined)
                if hits >= 2:
                    rows.append(bt)
                    seen_ids.add(bt.id)

    rows.sort(
        key=lambda b: (
            b.ran_at.timestamp() if b.ran_at else 0.0,
            b.id,
        )
    )

    state: dict[tuple[str, str], BacktestResult] = {}
    points: list[dict] = []
    last_ts: int | None = None
    for bt in rows:
        if (bt.trade_count or 0) <= 0:
            continue
        ran_at = bt.ran_at
        if not ran_at:
            continue
        key = (bt.ticker or "", bt.strategy_name or "")
        state[key] = bt
        wins = sum(1 for v in state.values() if (v.return_pct or 0) > 0)
        total = len(state)
        losses = total - wins
        wr = round(wins / max(1, total) * 100, 1)
        ts = int(ran_at.timestamp())
        if last_ts is not None and ts <= last_ts:
            ts = last_ts + 1
        last_ts = ts
        points.append({
            "time": ts,
            "ran_at": ran_at.isoformat(),
            "win_rate": wr,
            "wins": wins,
            "losses": losses,
            "deduped_runs": total,
        })
    return points


@router.get("/api/trading/learn/patterns")
def api_learned_patterns(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    from ...models.trading import TradingInsight, ScanPattern

    all_insights = db.query(TradingInsight).filter(
        TradingInsight.user_id == ctx["user_id"],
    ).order_by(TradingInsight.confidence.desc()).limit(200).all()
    # region agent log
    try:
        from ...debug_agent_log import agent_log, safe_db_fingerprint

        agent_log(
            "H1",
            "trading_sub.ai.api_learned_patterns",
            "insights_for_user",
            {
                "user_id": ctx["user_id"],
                "is_guest": ctx.get("is_guest"),
                "insights_count": len(all_insights),
                "db_fingerprint": safe_db_fingerprint(),
            },
        )
    except Exception:
        pass
    # endregion

    scan_patterns_by_name: dict[str, ScanPattern] = {}
    scan_patterns_by_id: dict[int, ScanPattern] = {}
    for sp in db.query(ScanPattern).all():
        scan_patterns_by_name[sp.name.lower()] = sp
        scan_patterns_by_id[sp.id] = sp

    _SECTOR_TICKERS: dict[str, set[str]] = {
        "tech": {"AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AVGO","ORCL","CRM","ADBE","AMD","INTC","QCOM","TXN","NFLX","DDOG","NET","SNOW","PLTR","SHOP"},
        "finance": {"JPM","V","MA","BAC","GS","MS","AXP","BLK","SCHW","CME","HOOD","SOFI","COIN","SQ","PYPL"},
        "healthcare": {"UNH","JNJ","LLY","ABBV","MRK","PFE","TMO","AMGN","GILD","VRTX","REGN","ISRG","MRNA"},
        "consumer": {"WMT","COST","HD","LOW","TGT","PG","KO","PEP","MCD","SBUX","NKE","LULU","CMG"},
        "industrial": {"CAT","DE","HON","UPS","BA","LMT","RTX","GE","EMR","ETN","AXON"},
        "energy": {"XOM","CVX","COP","SLB","EOG","MPC","OXY","HAL","ENPH","FSLR"},
    }

    def _detect_sectors(tickers: list[str]) -> list[str]:
        has_crypto = any(t.endswith("-USD") for t in tickers)
        sectors: list[str] = []
        if has_crypto:
            sectors.append("crypto")
        for sec, sec_set in _SECTOR_TICKERS.items():
            if any(t in sec_set for t in tickers):
                sectors.append(sec)
        return sectors

    active = []
    demoted = []
    for ins in all_insights:
        desc = ins.pattern_description or ""
        desc_lower = desc.lower()
        if any(w in desc_lower for w in ("bullish", "oversold", "buy", "uptrend", "gained", "above")):
            signal_type = "bullish"
        elif any(w in desc_lower for w in ("bearish", "overbought", "sell", "downtrend", "lost", "below")):
            signal_type = "bearish"
        else:
            signal_type = "neutral"
        win_match = re.search(r"(\d+(?:\.\d+)?)%\s*win", desc)
        ret_match = re.search(r"([+-]?\d+(?:\.\d+)?)%\s*(?:avg|average|return)", desc)
        ticker_match = re.findall(r"\b([A-Z]{1,5}(?:-USD)?)\b", desc)
        tickers_found = [t for t in ticker_match if len(t) >= 2 and t not in {
            "RSI", "MACD", "EMA", "SMA", "ADX", "ATR", "AND", "THE", "FOR",
            "OBV", "MFI", "CCI", "SAR", "USD", "AVG", "NET", "LOW", "HIGH",
        }][:5]

        sp_resolved = _resolve_scan_pattern_id_for_insight(db, ins)
        scan_pid = getattr(ins, "scan_pattern_id", None)
        sibs = _sibling_insight_ids_for_pattern(db, ins.id, scan_pid, sp_resolved)
        _bt_univ = _evidence_backtest_asset_universe(
            db, desc, sp_resolved, insight_id=ins.id,
        )
        panel = _compute_deduped_backtest_win_stats(
            db, sibs, desc, asset_universe=_bt_univ,
        )
        wc = int(panel["bt_wins"])
        lc = int(panel["bt_losses"])
        real_wr = panel["bt_win_rate"]
        bt_tickers = list({
            b["ticker"] for b in panel["backtests_out"] if b.get("ticker")
        })
        all_tickers = list(set(tickers_found + bt_tickers))
        sectors = _detect_sectors(all_tickers)
        is_crypto = "crypto" in sectors

        # Win rate / W–L here come only from deduped BacktestResult rows (same source as evidence modal).
        # Do not fall back to TradingInsight.win_count — those can be stale or from pre-cleanup runs.

        pattern_display = (
            _sync_win_stats_in_description(desc, real_wr, wc, lc)
            if real_wr is not None and (wc + lc) > 0
            else desc
        )

        linked_sp = None
        sp_id = getattr(ins, "scan_pattern_id", None)
        if sp_id:
            linked_sp = scan_patterns_by_id.get(sp_id)
        if not linked_sp:
            name_part = desc.split("\u2014")[0].split(" - ")[0].strip().lower()
            linked_sp = scan_patterns_by_name.get(name_part)
        variant_info = None
        if linked_sp and linked_sp.parent_id is not None:
            parent_sp = scan_patterns_by_id.get(linked_sp.parent_id)
            variant_info = {
                "label": linked_sp.variant_label,
                "parent_id": linked_sp.parent_id,
                "generation": linked_sp.generation or 0,
                "exit_config": linked_sp.exit_config,
                "origin": linked_sp.origin,
                "parent_name": parent_sp.name if parent_sp else None,
            }
        best_exit = None
        if linked_sp and linked_sp.parent_id is None and linked_sp.exit_config:
            best_exit = linked_sp.variant_label or "evolved"

        entry = {
            "id": ins.id,
            "pattern": desc,
            "pattern_display": pattern_display,
            "confidence": round(ins.confidence * 100, 1),
            "evidence_count": ins.evidence_count,
            "active": ins.active,
            "signal_type": signal_type,
            "win_rate": real_wr,
            "win_count": wc,
            "loss_count": lc,
            "avg_return": float(ret_match.group(1)) if ret_match else None,
            "example_tickers": tickers_found,
            "bt_tickers": list(set(bt_tickers))[:8],
            "is_crypto": is_crypto,
            "sectors": sectors,
            "created_at": ins.created_at.isoformat(),
            "last_seen": ins.last_seen.isoformat() if ins.last_seen else None,
            "variant": variant_info,
            "best_exit": best_exit,
            "scan_pattern_id": linked_sp.id if linked_sp else None,
            "parent_scan_pattern_id": linked_sp.parent_id if linked_sp else None,
            "ticker_scope": getattr(linked_sp, "ticker_scope", "universal") if linked_sp else "universal",
            "scope_tickers": getattr(linked_sp, "scope_tickers", None) if linked_sp else None,
        }
        if ins.active:
            active.append(entry)
        else:
            demoted.append(entry)

    return JSONResponse({
        "ok": True,
        "active": active,
        "demoted": demoted,
        "total_active": len(active),
        "total_demoted": len(demoted),
    })


@router.get("/api/trading/learn/patterns/{pattern_id}/evidence")
def api_pattern_evidence(pattern_id: int, request: Request, db: Session = Depends(get_db)):
    """Assemble comprehensive evidence for a pattern from all available data sources."""
    ctx = get_identity_ctx(request, db)
    from ...models.trading import (
        TradingInsight, LearningEvent, TradingHypothesis,
        Trade,
    )

    insight = db.query(TradingInsight).filter(
        TradingInsight.id == pattern_id,
        TradingInsight.user_id == ctx["user_id"],
    ).first()
    if not insight:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)

    desc = insight.pattern_description or ""
    sp_resolved_id = _resolve_scan_pattern_id_for_insight(db, insight)
    sibling_ids = _sibling_insight_ids_for_pattern(
        db, pattern_id, getattr(insight, "scan_pattern_id", None), sp_resolved_id
    )

    # 1. Learning events linked to this insight
    events = (
        db.query(LearningEvent)
        .filter(LearningEvent.related_insight_id == pattern_id)
        .order_by(LearningEvent.created_at.desc())
        .limit(30)
        .all()
    )
    timeline = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "description": e.description,
            "confidence_before": e.confidence_before,
            "confidence_after": e.confidence_after,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]

    # 2. Related hypotheses — match by keyword overlap or pattern_id link
    keywords = _extract_keywords_for_matching(desc)
    all_hyps = db.query(TradingHypothesis).limit(100).all()
    hypotheses = []
    for h in all_hyps:
        h_desc = (h.description or "").lower()
        if any(kw in h_desc for kw in keywords):
            confirm_rate = (
                (h.times_confirmed or 0) / max(1, h.times_tested or 1)
            )
            last_result = None
            if h.last_result_json:
                try:
                    last_result = json.loads(h.last_result_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            hypotheses.append({
                "id": h.id,
                "description": h.description,
                "status": h.status,
                "origin": h.origin,
                "times_tested": h.times_tested or 0,
                "times_confirmed": h.times_confirmed or 0,
                "times_rejected": h.times_rejected or 0,
                "confirm_rate": round(confirm_rate * 100, 1),
                "expected_winner": h.expected_winner,
                "condition_a": h.condition_a,
                "condition_b": h.condition_b,
                "last_result": last_result,
                "last_tested_at": h.last_tested_at.isoformat() if h.last_tested_at else None,
            })

    # 3. Matching trades by pattern_tags
    trades_out = []
    try:
        trade_keywords = _extract_keywords_for_matching(desc, min_len=3)
        all_trades = (
            db.query(Trade)
            .filter(Trade.user_id == ctx["user_id"], Trade.pattern_tags.isnot(None))
            .order_by(Trade.entry_date.desc())
            .limit(200)
            .all()
        )
        for t in all_trades:
            tags_lower = (t.pattern_tags or "").lower()
            if any(kw in tags_lower for kw in trade_keywords):
                trades_out.append({
                    "id": t.id,
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl": t.pnl,
                    "status": t.status,
                    "entry_date": t.entry_date.isoformat() if t.entry_date else None,
                    "exit_date": t.exit_date.isoformat() if t.exit_date else None,
                    "pattern_tags": t.pattern_tags,
                })
                if len(trades_out) >= 20:
                    break
    except Exception:
        pass

    # 4–5. Backtests + win rate: deduped list (latest per ticker/strategy), same as pattern cards
    _ev_univ = _evidence_backtest_asset_universe(
        db, desc, sp_resolved_id, insight_id=pattern_id,
    )
    panel = _compute_deduped_backtest_win_stats(
        db, sibling_ids, desc, asset_universe=_ev_univ,
    )
    backtests_out = panel["backtests_out"]
    bt_wins = int(panel["bt_wins"])
    bt_losses = int(panel["bt_losses"])
    bt_win_rate = panel["bt_win_rate"]
    bt_avg_return = panel["bt_avg_return"]
    win_rate_progress = _deduped_win_rate_progress_series(
        db, sibling_ids, desc, asset_universe=_ev_univ,
    )

    pattern_display = (
        _sync_win_stats_in_description(desc, bt_win_rate, bt_wins, bt_losses)
        if bt_win_rate is not None and (bt_wins + bt_losses) > 0
        else desc
    )

    computed_stats = _compute_evidence_stats(
        timeline, hypotheses, trades_out, backtests_out,
        bt_wins, bt_losses, bt_win_rate, bt_avg_return,
        backtest_total_displayed=len(backtests_out),
    )

    return JSONResponse({
        "ok": True,
        "resolved_scan_pattern_id": sp_resolved_id,
        "pattern_display": pattern_display,
        "win_rate_progress": win_rate_progress,
        "insight": {
            "id": insight.id,
            "pattern": desc,
            "pattern_display": pattern_display,
            "confidence": round(insight.confidence * 100, 1),
            "evidence_count": insight.evidence_count,
            # Same source as "backtests" tab: deduped BacktestResult rows with trades only
            "win_count": bt_wins,
            "loss_count": bt_losses,
            "win_rate": bt_win_rate,
            "has_saved_backtest_stats": (bt_wins + bt_losses) > 0,
            "active": insight.active,
            "created_at": insight.created_at.isoformat(),
            "last_seen": insight.last_seen.isoformat() if insight.last_seen else None,
        },
        "computed_stats": computed_stats,
        "timeline": timeline,
        "hypotheses": hypotheses,
        "trades": trades_out,
        "backtests": backtests_out,
    })


@router.get("/api/trading/learn/backtest/{bt_id}")
def api_get_stored_backtest(bt_id: int, request: Request, db: Session = Depends(get_db)):
    """Return stored BacktestResult by id. Used so the expanded chart matches the table header.

    Both header and chart come from the same DB row. Returns 404 if not found or not accessible.
    """
    ctx = get_identity_ctx(request, db)
    from ...models.trading import BacktestResult, TradingInsight

    bt = db.query(BacktestResult).filter(BacktestResult.id == bt_id).first()
    if not bt:
        return JSONResponse({"ok": False, "error": "Backtest not found"}, status_code=404)

    # Verify user can see this: either their insight, shared (user_id=None), or sibling pattern
    ins = db.query(TradingInsight).filter(
        TradingInsight.id == bt.related_insight_id,
    ).first() if bt.related_insight_id else None
    if ins:
        if ins.user_id is None:
            pass  # shared/orphan, allow
        elif ins.user_id == ctx["user_id"]:
            pass  # own insight, allow
        else:
            # Other user's insight: allow only if user has an insight for same pattern (sibling)
            sp_id = getattr(ins, "scan_pattern_id", None)
            if sp_id:
                has_sibling = db.query(TradingInsight.id).filter(
                    TradingInsight.scan_pattern_id == sp_id,
                    TradingInsight.user_id == ctx["user_id"],
                ).limit(1).first()
                if not has_sibling:
                    return JSONResponse({"ok": False, "error": "Access denied"}, status_code=404)

    eq = []
    try:
        if bt.equity_curve:
            eq = json.loads(bt.equity_curve) if isinstance(bt.equity_curve, str) else (bt.equity_curve or [])
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "id": bt.id,
        "ticker": bt.ticker,
        "strategy_name": bt.strategy_name,
        "return_pct": float(bt.return_pct) if bt.return_pct is not None else None,
        "win_rate": float(bt.win_rate) if bt.win_rate is not None else None,
        "trade_count": bt.trade_count or 0,
        "sharpe": float(bt.sharpe) if bt.sharpe is not None else None,
        "max_drawdown": float(bt.max_drawdown) if bt.max_drawdown is not None else None,
        "equity_curve": eq,
        "params": bt.params,
    })


def _access_backtest_row(
    ctx: dict,
    db: Session,
    bt,
    ins,
) -> bool:
    """Return True if the user may read/update this BacktestResult row."""
    if not ins:
        return True
    if ins.user_id is None:
        return True
    if ins.user_id == ctx.get("user_id"):
        return True
    sp_id = getattr(ins, "scan_pattern_id", None)
    if sp_id:
        has_sibling = db.query(TradingInsight.id).filter(
            TradingInsight.scan_pattern_id == sp_id,
            TradingInsight.user_id == ctx["user_id"],
        ).limit(1).first()
        return bool(has_sibling)
    return False


@router.post("/api/trading/learn/backtest/{bt_id}/rerun")
def api_rerun_stored_backtest(bt_id: int, request: Request, db: Session = Depends(get_db)):
    """Re-run using **brain** ``period``/``interval`` for ``ScanPattern.timeframe``
    (``get_brain_backtest_window`` — same as ``smart_backtest_insight``) and current rules.

    Use this for the evidence mini-chart so it matches Chili brain batch backtests.

    Results can still differ from an older save if OHLCV, rules, or providers changed.
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...models.trading import BacktestResult, TradingInsight, ScanPattern
    from ...services.backtest_service import (
        backtest_pattern,
        get_brain_backtest_window,
        save_backtest,
    )

    bt = db.query(BacktestResult).filter(BacktestResult.id == bt_id).first()
    if not bt:
        return JSONResponse({"ok": False, "error": "Backtest not found"}, status_code=404)

    ins = (
        db.query(TradingInsight).filter(TradingInsight.id == bt.related_insight_id).first()
        if bt.related_insight_id
        else None
    )
    if ins and not _access_backtest_row(ctx, db, bt, ins):
        return JSONResponse({"ok": False, "error": "Access denied"}, status_code=404)

    sp_id = bt.scan_pattern_id or (getattr(ins, "scan_pattern_id", None) if ins else None)
    if not sp_id:
        return JSONResponse({"ok": False, "error": "No ScanPattern linked to this backtest"}, status_code=400)
    p = db.query(ScanPattern).get(sp_id)
    if not p:
        return JSONResponse({"ok": False, "error": "Pattern not found"}, status_code=404)

    tf = getattr(p, "timeframe", "1d") or "1d"
    use_period, use_interval = get_brain_backtest_window(tf)

    result = backtest_pattern(
        ticker=bt.ticker,
        pattern_name=p.name,
        rules_json=p.rules_json,
        interval=use_interval,
        period=use_period,
        exit_config=getattr(p, "exit_config", None),
        cash=100_000.0,
        commission=0.001,
    )
    if not result.get("ok"):
        return JSONResponse(
            {"ok": False, "error": result.get("error", "backtest failed")},
            status_code=400,
        )

    uid = ins.user_id if ins else bt.user_id
    rec = save_backtest(
        db, uid, result,
        insight_id=bt.related_insight_id,
        scan_pattern_id=int(sp_id),
    )
    ran_at = getattr(rec, "ran_at", None)
    out = {
        **result,
        "ok": True,
        "backtest_id": rec.id,
        "ran_at": ran_at.isoformat() if ran_at else None,
    }
    return JSONResponse(to_jsonable(out))


@router.post("/api/trading/learn/backtest/{bt_id}/refresh")
async def api_refresh_backtest(bt_id: int, request: Request, db: Session = Depends(get_db)):
    """Update stored BacktestResult with fresh backtest results. Keeps header and chart in sync."""
    ctx = get_identity_ctx(request, db)
    from datetime import datetime as dt
    from ...models.trading import BacktestResult, TradingInsight

    bt = db.query(BacktestResult).filter(BacktestResult.id == bt_id).first()
    if not bt:
        return JSONResponse({"ok": False, "error": "Backtest not found"}, status_code=404)

    ins = db.query(TradingInsight).filter(
        TradingInsight.id == bt.related_insight_id,
    ).first() if bt.related_insight_id else None
    if ins:
        if ins.user_id is None:
            pass
        elif ins.user_id == ctx["user_id"]:
            pass
        else:
            sp_id = getattr(ins, "scan_pattern_id", None)
            if sp_id:
                has_sibling = db.query(TradingInsight.id).filter(
                    TradingInsight.scan_pattern_id == sp_id,
                    TradingInsight.user_id == ctx["user_id"],
                ).limit(1).first()
                if not has_sibling:
                    return JSONResponse({"ok": False, "error": "Access denied"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    ret = body.get("return_pct")
    bt.return_pct = float(ret) if ret is not None else bt.return_pct
    wr = body.get("win_rate")
    bt.win_rate = float(wr) if wr is not None else bt.win_rate
    sh = body.get("sharpe")
    bt.sharpe = float(sh) if sh is not None else None
    md = body.get("max_drawdown")
    bt.max_drawdown = float(md) if md is not None else bt.max_drawdown
    bt.trade_count = int(body.get("trade_count", bt.trade_count) or 0)
    if body.get("equity_curve") is not None:
        bt.equity_curve = json.dumps(body["equity_curve"]) if isinstance(body["equity_curve"], list) else body["equity_curve"]
    bt.ran_at = dt.utcnow()
    db.commit()
    db.refresh(bt)

    return JSONResponse({
        "ok": True,
        "id": bt.id,
        "return_pct": float(bt.return_pct),
        "win_rate": float(bt.win_rate),
        "sharpe": float(bt.sharpe) if bt.sharpe is not None else None,
        "max_drawdown": float(bt.max_drawdown),
        "trade_count": bt.trade_count,
        "ran_at": bt.ran_at.isoformat(),
    })


@router.get("/api/trading/learn/patterns/{pattern_id}/evolution")
def api_pattern_evolution(pattern_id: int, request: Request, db: Session = Depends(get_db)):
    """Return the full evolution tree for a pattern's ScanPattern lineage."""
    ctx = get_identity_ctx(request, db)
    from ...models.trading import (
        TradingInsight, ScanPattern, TradingHypothesis,
    )

    insight = db.query(TradingInsight).filter(
        TradingInsight.id == pattern_id,
        TradingInsight.user_id == ctx["user_id"],
    ).first()
    if not insight:
        return JSONResponse({"ok": False, "reason": "not found"}, 404)

    sp_id = getattr(insight, "scan_pattern_id", None)
    if not sp_id:
        return JSONResponse({"ok": True, "root": None, "current_scan_pattern_id": None})

    current_sp = db.query(ScanPattern).get(sp_id)
    if not current_sp:
        return JSONResponse({"ok": True, "root": None, "current_scan_pattern_id": None})

    root = current_sp
    visited = {root.id}
    while root.parent_id is not None:
        parent = db.query(ScanPattern).get(root.parent_id)
        if not parent or parent.id in visited:
            break
        visited.add(parent.id)
        root = parent

    all_patterns = [root]
    current_ids = [root.id]
    while current_ids:
        children = (
            db.query(ScanPattern)
            .filter(ScanPattern.parent_id.in_(current_ids))
            .order_by(ScanPattern.generation, ScanPattern.id)
            .all()
        )
        all_patterns.extend(children)
        current_ids = [c.id for c in children]

    all_sp_ids = [p.id for p in all_patterns]
    bt_stats = compute_pattern_bt_stats(db, all_sp_ids, ctx["user_id"])

    hyp_by_sp: dict[int, list] = {sp_id: [] for sp_id in all_sp_ids}
    for h in (
        db.query(TradingHypothesis)
        .filter(TradingHypothesis.related_pattern_id.in_(all_sp_ids))
        .all()
    ):
        confirm_rate = (
            (h.times_confirmed or 0) / max(1, h.times_tested or 1)
        )
        hyp_by_sp.setdefault(h.related_pattern_id, []).append({
            "id": h.id,
            "description": h.description,
            "status": h.status,
            "confirm_rate": round(confirm_rate * 100, 1),
            "times_tested": h.times_tested or 0,
        })

    sp_map = {p.id: p for p in all_patterns}
    children_map: dict[int, list[int]] = {}
    for p in all_patterns:
        if p.parent_id is not None:
            children_map.setdefault(p.parent_id, []).append(p.id)

    def _build_node(sp: "ScanPattern") -> dict:
        stats = bt_stats.get(sp.id, {})
        total = stats.get("total", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        wr = stats.get("win_rate")

        exit_cfg = None
        if sp.exit_config:
            try:
                exit_cfg = json.loads(sp.exit_config)
            except (json.JSONDecodeError, TypeError):
                pass

        child_ids = children_map.get(sp.id, [])
        children_nodes = [
            _build_node(sp_map[cid]) for cid in child_ids if cid in sp_map
        ]

        origin = sp.origin or ""
        if "entry" in origin:
            mutation_type = "entry"
        elif "combo" in origin or "cross" in origin:
            mutation_type = "combo"
        elif "exit" in origin or "mut-" in (sp.variant_label or ""):
            mutation_type = "exit"
        else:
            mutation_type = "root"

        return {
            "id": sp.id,
            "name": sp.name,
            "generation": sp.generation or 0,
            "variant_label": sp.variant_label,
            "origin": sp.origin,
            "mutation_type": mutation_type,
            "active": sp.active,
            "exit_config": exit_cfg,
            "win_rate": wr,
            "wins": wins,
            "losses": losses,
            "avg_return_pct": stats.get("avg_return_pct"),
            "backtest_count": total,
            "is_current": sp.id == current_sp.id,
            "hypotheses": hyp_by_sp.get(sp.id, []),
            "children": children_nodes,
        }

    return JSONResponse({
        "ok": True,
        "root": _build_node(root),
        "current_scan_pattern_id": current_sp.id,
    })


# ── Backtest Queue Management ────────────────────────────────────────────

@router.get("/api/trading/backtest-queue/status")
def api_backtest_queue_status(db: Session = Depends(get_db)):
    """Get the current backtest queue status."""
    from app.services.trading.backtest_queue import get_queue_status
    status = get_queue_status(db)
    return JSONResponse({"ok": True, **status})


@router.post("/api/trading/patterns/{pattern_id}/boost")
def api_boost_pattern(pattern_id: int, db: Session = Depends(get_db)):
    """Boost a ScanPattern to the front of the backtest queue."""
    from app.services.trading.backtest_queue import boost_pattern
    from ...models.trading import ScanPattern
    
    pattern = db.query(ScanPattern).get(pattern_id)
    if not pattern:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)
    
    success = boost_pattern(db, pattern_id, priority=100)
    if success:
        return JSONResponse({
            "ok": True,
            "message": f"Pattern '{pattern.name}' boosted to front of queue",
            "pattern_id": pattern_id,
        })
    return JSONResponse({"ok": False, "reason": "Failed to boost pattern"}, status_code=500)


@router.post("/api/trading/patterns/{pattern_id}/clear-boost")
def api_clear_pattern_boost(pattern_id: int, db: Session = Depends(get_db)):
    """Clear the boost priority for a ScanPattern."""
    from app.services.trading.backtest_queue import clear_boost
    from ...models.trading import ScanPattern
    
    pattern = db.query(ScanPattern).get(pattern_id)
    if not pattern:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)
    
    success = clear_boost(db, pattern_id)
    if success:
        return JSONResponse({
            "ok": True,
            "message": f"Boost cleared for pattern '{pattern.name}'",
            "pattern_id": pattern_id,
        })
    return JSONResponse({"ok": False, "reason": "Failed to clear boost"}, status_code=500)


@router.get("/api/trading/backtest-queue/pending")
def api_backtest_queue_pending(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    """Get the list of pending patterns in the backtest queue."""
    from app.services.trading.backtest_queue import get_pending_patterns
    
    patterns = get_pending_patterns(db, limit=limit)
    return JSONResponse({
        "ok": True,
        "patterns": [
            {
                "id": p.id,
                "name": p.name,
                "origin": p.origin,
                "priority": p.backtest_priority,
                "last_backtest_at": p.last_backtest_at.isoformat() if p.last_backtest_at else None,
                "win_rate": p.win_rate,
                "active": p.active,
            }
            for p in patterns
        ],
        "count": len(patterns),
    })


def _compute_evidence_stats(
    timeline: list,
    hypotheses: list,
    trades: list,
    backtests: list,
    bt_wins: int = 0,
    bt_losses: int = 0,
    bt_win_rate: float | None = None,
    bt_avg_return: float | None = None,
    backtest_total_displayed: int | None = None,
) -> dict:
    """Compute live aggregate stats from the actual evidence data.

    When bt_avg_return is provided, it is used for backtest_avg_return
    (computed from displayed backtests with trades). Otherwise falls back
    to averaging all backtests.
    """
    stats: dict = {}

    if hypotheses:
        tested = [h for h in hypotheses if h.get("times_tested", 0) > 0]
        if tested:
            avg_confirm = sum(h["confirm_rate"] for h in tested) / len(tested)
            stats["hypothesis_confirm_rate"] = round(avg_confirm, 1)
            stats["hypotheses_tested"] = len(tested)

    if trades:
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = sum(1 for t in closed if t["pnl"] > 0)
            stats["trade_win_rate"] = round(wins / len(closed) * 100, 1)
            stats["trade_count"] = len(closed)
            stats["total_pnl"] = round(sum(t["pnl"] for t in closed), 2)

    bt_total = bt_wins + bt_losses
    if bt_total > 0:
        stats["backtest_avg_win_rate"] = bt_win_rate
        stats["backtest_count"] = bt_total
    if bt_avg_return is not None:
        stats["backtest_avg_return"] = round(bt_avg_return, 1)
    elif backtests:
        stats["backtest_avg_return"] = round(
            sum(b.get("return_pct", 0) for b in backtests) / len(backtests), 1
        )

    if timeline:
        stats["confirmations"] = sum(
            1 for e in timeline if e.get("event_type") in ("discovery", "real_trade_validation")
        )
        stats["challenges"] = sum(
            1 for e in timeline if e.get("event_type") == "hypothesis_challenged"
        )

    return stats


def _extract_keywords_for_matching(text: str, min_len: int = 4) -> list[str]:
    """Extract meaningful lowercase keywords from a pattern description for fuzzy matching."""
    stop_words = {
        "chili", "validated", "challenge", "discovery", "data", "says", "otherwise",
        "confirmed", "actually", "outperform", "outperforms", "samples", "better",
        "with", "than", "from", "that", "this", "entries", "positive", "negative",
        "above", "below", "signal", "pattern", "average", "return", "based",
        "when", "have", "been", "more", "less", "into", "over", "under",
        "trend", "following", "bullish", "bearish", "rate", "combo", "bonus",
        "synergy", "baseline", "versus", "compared", "strong", "weak",
        "high", "very", "just", "only", "also", "some", "each", "every",
    }
    words = re.findall(r'[a-z_]+', text.lower())
    keywords = []
    seen = set()
    for w in words:
        if len(w) >= min_len and w not in stop_words and w not in seen:
            seen.add(w)
            keywords.append(w)
    return keywords[:15]


# ── Pattern trade analytics (evidence-driven evolution) ─────────────────

@router.get("/api/trading/brain/pattern/{pattern_id}/trade-analytics")
def api_pattern_trade_analytics(
    request: Request,
    pattern_id: int,
    db: Session = Depends(get_db),
    window_days: int = Query(180, ge=30, le=730),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_trade_analysis import analyze_pattern_trades

    report = analyze_pattern_trades(db, pattern_id, window_days=window_days)
    return JSONResponse({"ok": True, "report": report.to_json()})


@router.post("/api/trading/brain/pattern/{pattern_id}/evidence/propose")
def api_pattern_evidence_propose(
    request: Request,
    pattern_id: int,
    db: Session = Depends(get_db),
    window_days: int = Query(180, ge=30, le=730),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_evidence_service import propose_from_analysis

    hyps = propose_from_analysis(db, pattern_id, window_days=window_days, user_id=ctx.get("user_id"))
    return JSONResponse({
        "ok": True,
        "created": len(hyps),
        "ids": [h.id for h in hyps],
    })


@router.post("/api/trading/brain/evidence/{hypothesis_id}/walk-forward")
def api_pattern_evidence_walk_forward(
    request: Request,
    hypothesis_id: int,
    db: Session = Depends(get_db),
    is_days: int = Query(90, ge=20, le=400),
    oos_days: int = Query(90, ge=20, le=400),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_evidence_service import walk_forward_validate

    out = walk_forward_validate(db, hypothesis_id, is_days=is_days, oos_days=oos_days)
    return JSONResponse(out)


@router.post("/api/trading/brain/evidence/{hypothesis_id}/apply")
def api_pattern_evidence_apply(
    request: Request,
    hypothesis_id: int,
    db: Session = Depends(get_db),
    dry_run: bool = Query(True),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_evolution_apply import apply_evidence_hypothesis

    out = apply_evidence_hypothesis(
        db, hypothesis_id, dry_run=dry_run, user_id=ctx.get("user_id"),
    )
    return JSONResponse(out)


@router.get("/api/trading/brain/pattern/{pattern_id}/trade-ml")
def api_pattern_trade_ml(
    request: Request,
    pattern_id: int,
    db: Session = Depends(get_db),
    window_days: int = Query(365, ge=60, le=900),
):
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest"):
        return JSONResponse({"ok": False, "error": "Sign in required"}, status_code=401)
    from ...services.trading.pattern_trade_ml import train_on_pattern_trades

    return JSONResponse(train_on_pattern_trades(db, pattern_id, window_days=window_days))


# ── Brain Worker Control ────────────────────────────────────────────────

from pathlib import Path

_BRAIN_WORKER_STATUS_FILE = DATA_DIR / "brain_worker_status.json"
_BRAIN_WORKER_STOP_SIGNAL = DATA_DIR / "brain_worker_stop"
_BRAIN_WORKER_PAUSE_SIGNAL = DATA_DIR / "brain_worker_pause"
_BRAIN_WORKER_WAKE_SIGNAL = DATA_DIR / "brain_worker_wake"


def _merge_brain_worker_db_fields(db: Session, payload: dict) -> None:
    """Attach PostgreSQL control row fields for UI / debugging."""
    try:
        from ...services.brain_worker_signals import (
            get_worker_control_snapshot,
            heartbeat_is_stale,
        )

        ctrl = get_worker_control_snapshot(db)
        if ctrl is None:
            return
        payload["db_wake_requested"] = bool(ctrl.wake_requested)
        payload["db_stop_requested"] = bool(ctrl.stop_requested)
        if ctrl.last_heartbeat_at:
            payload["last_heartbeat_at"] = ctrl.last_heartbeat_at.isoformat() + "Z"
        payload["heartbeat_stale"] = heartbeat_is_stale(ctrl.last_heartbeat_at)
    except Exception:
        pass


def _clear_stale_brain_worker_status_if_needed(db: Session, force: bool) -> dict:
    """Remove stale brain_worker_status.json when PID is dead, unknown+stale heartbeat, or force."""
    from ...services.brain_worker_signals import get_worker_control_snapshot, heartbeat_is_stale

    extra: dict = {"stale_status_cleared": False}
    if not _BRAIN_WORKER_STATUS_FILE.exists():
        return extra

    ctrl = get_worker_control_snapshot(db)
    stale_hb = heartbeat_is_stale(ctrl.last_heartbeat_at if ctrl else None)

    try:
        with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
            data = json.load(f)
        pid = data.get("pid")
    except Exception:
        return extra

    if not pid:
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    liv = _worker_liveness(int(pid) if pid else None)
    if liv == "dead":
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    if force:
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    if liv == "unknown" and stale_hb:
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    if liv == "alive" and stale_hb:
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
            extra["stale_status_cleared"] = True
        except OSError:
            pass
        return extra

    return extra


def _brain_worker_liveness(pid: int) -> str:
    """Return 'alive' | 'dead' | 'unknown'.

    'unknown' = psutil could not inspect the PID (e.g. AccessDenied on Windows).
    Callers must NOT delete brain_worker_status.json when unknown — that broke wake.
    """
    try:
        import psutil
    except ImportError:
        return "unknown"

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return "dead"
    except psutil.AccessDenied:
        return "unknown"
    except Exception:
        return "unknown"

    try:
        if not proc.is_running():
            return "dead"
    except psutil.NoSuchProcess:
        return "dead"
    except Exception:
        return "unknown"

    try:
        name = (proc.name() or "").lower()
        cmd = " ".join(proc.cmdline() or []).lower()
    except (psutil.AccessDenied, Exception):
        return "unknown"

    if "brain_worker" in cmd or "brain_worker.py" in cmd:
        return "alive"
    if "python" in name or name in ("py.exe", "py") or name.startswith("python"):
        return "alive"
    return "dead"


def _worker_liveness(pid: int | None) -> str:
    """Prefer Docker Compose ``brain-worker`` container state when that container exists."""
    try:
        from ...services.brain_worker_docker import find_brain_worker_container, brain_worker_liveness_for_ui

        if find_brain_worker_container() is not None:
            return brain_worker_liveness_for_ui()
    except Exception:
        pass
    if pid is not None:
        return _brain_worker_liveness(int(pid))
    return "dead"


@router.get("/api/trading/brain/worker/status")
def api_brain_worker_status(request: Request, db: Session = Depends(get_db)):
    """Get brain worker status.
    
    Non-blocking: reads status file first, then checks process liveness.
    DB identity check is done but failures are tolerated to avoid blocking
    when the database is locked by the brain worker.
    """
    # Try to get identity, but don't block on DB lock
    try:
        ctx = get_identity_ctx(request, db)
        if ctx.get("demo"):
            return JSONResponse({"ok": False, "error": "Demo users cannot access worker status"}, status_code=403)
    except Exception:
        # DB likely locked — proceed anyway for status reads
        pass
    
    default_stopped = {
        "ok": True,
        "status": "stopped",
        "pid": None,
        "current_step": "",
        "current_progress": "",
        "last_cycle": {},
        "totals": {},
    }
    if not _BRAIN_WORKER_STATUS_FILE.exists():
        out = {**default_stopped}
        _merge_brain_worker_db_fields(db, out)
        return JSONResponse(out)

    try:
        with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return JSONResponse(default_stopped)
    except (json.JSONDecodeError, OSError) as e:
        # Corrupt or partial write (e.g. worker wrote mid-read) — return stopped so UI doesn't 500
        return JSONResponse({**default_stopped, "error": str(e)})

    pid = data.get("pid")
    liv = _worker_liveness(int(pid) if pid else None)
    if liv == "dead":
        data["status"] = "stopped"
        try:
            _BRAIN_WORKER_STATUS_FILE.unlink()
        except Exception:
            pass
    elif liv == "unknown":
        data["pid_liveness"] = "unknown"

    # Merge live queue status so UI can show pending when worker says "Queue empty"
    try:
        from app.services.trading.backtest_queue import get_queue_status
        qstatus = get_queue_status(db)
        data["queue_pending_live"] = qstatus.get("pending", 0)
        data["queue_empty_live"] = qstatus.get("queue_empty", True)
    except Exception:
        data["queue_pending_live"] = 0
        data["queue_empty_live"] = True

    _merge_brain_worker_db_fields(db, data)

    return JSONResponse({"ok": True, **data})


@router.post("/api/trading/brain/worker/start")
async def api_brain_worker_start(request: Request, db: Session = Depends(get_db)):
    """Start the Docker Compose ``brain-worker`` service (not a subprocess on this host)."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot control worker"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        body = {}

    force = bool(body.get("force"))
    interval = body.get("interval", 30)

    from ...services.brain_worker_docker import brain_worker_container_running, brain_worker_start_docker

    stale_meta = _clear_stale_brain_worker_status_if_needed(db, force)

    if brain_worker_container_running():
        return JSONResponse(
            {
                "ok": False,
                "error": "Worker already running (Docker)",
                "mode": "docker",
                **stale_meta,
            },
        )

    if _BRAIN_WORKER_STATUS_FILE.exists():
        try:
            with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
                data = json.load(f)
            pid = data.get("pid")
            if pid:
                from ...services.brain_worker_signals import (
                    get_worker_control_snapshot,
                    heartbeat_is_stale,
                )

                liv = _worker_liveness(int(pid) if pid else None)
                ctrl = get_worker_control_snapshot(db)
                stale_hb = heartbeat_is_stale(ctrl.last_heartbeat_at if ctrl else None)
                if liv == "alive" and not stale_hb:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "Worker already running",
                            "pid": pid,
                        },
                    )
                if liv == "unknown" and not stale_hb:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "Worker may already be running (PID could not be verified). Use force=true to clear status and start.",
                            "pid": pid,
                        },
                    )
        except Exception:
            pass

    if _BRAIN_WORKER_STOP_SIGNAL.exists():
        _BRAIN_WORKER_STOP_SIGNAL.unlink()
    if _BRAIN_WORKER_PAUSE_SIGNAL.exists():
        _BRAIN_WORKER_PAUSE_SIGNAL.unlink()

    try:
        from ...services.brain_worker_signals import clear_stop_requested, clear_worker_heartbeat

        clear_stop_requested(db)
        clear_worker_heartbeat(db)
        db.commit()
    except Exception as e:
        _log.warning("brain worker start: could not reset DB control row: %s", e)
        try:
            db.rollback()
        except Exception:
            pass

    res = brain_worker_start_docker()
    if not res.get("ok"):
        return JSONResponse(
            {
                "ok": False,
                "error": res.get("error", "docker_start_failed"),
                "hint": res.get("hint"),
                **stale_meta,
            },
            status_code=500,
        )
    return JSONResponse(
        {
            "ok": True,
            "mode": "docker",
            "interval_requested": interval,
            "interval_note": "Cycle interval is configured on the brain-worker service in docker-compose.yml (not from this request).",
            "docker": res,
            **stale_meta,
        }
    )


@router.post("/api/trading/brain/worker/stop")
def api_brain_worker_stop(request: Request, db: Session = Depends(get_db)):
    """Stop the brain worker gracefully."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot control worker"}, status_code=403)

    try:
        from ...services.brain_worker_signals import set_stop_requested

        set_stop_requested(db)
        db.commit()
    except Exception as e:
        _log.exception("brain worker stop DB failed")
        return JSONResponse({"ok": False, "error": f"Could not set DB stop: {e}"}, status_code=500)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _BRAIN_WORKER_STOP_SIGNAL.touch()
    except OSError:
        pass

    from ...services.brain_worker_docker import brain_worker_stop_docker

    dr = brain_worker_stop_docker()
    if not dr.get("ok"):
        return JSONResponse(
            {
                "ok": False,
                "error": dr.get("error", "docker_stop_failed"),
                "message": "Database stop was set; Docker stop failed.",
                "docker": dr,
            },
            status_code=500,
        )
    return JSONResponse(
        {
            "ok": True,
            "message": "Stop queued in database; brain-worker container stopped.",
            "docker": dr,
        }
    )


@router.post("/api/trading/brain/worker/pause")
def api_brain_worker_pause(request: Request, db: Session = Depends(get_db)):
    """Toggle pause state of the brain worker."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot control worker"}, status_code=403)
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if _BRAIN_WORKER_PAUSE_SIGNAL.exists():
        _BRAIN_WORKER_PAUSE_SIGNAL.unlink()
        return JSONResponse({"ok": True, "paused": False, "message": "Worker resumed"})
    else:
        _BRAIN_WORKER_PAUSE_SIGNAL.touch()
        return JSONResponse({"ok": True, "paused": True, "message": "Worker paused"})


@router.post("/api/trading/brain/worker/wake-cycle")
def api_brain_worker_wake_cycle(request: Request, db: Session = Depends(get_db)):
    """Signal the brain worker to skip remaining idle sleep and start the next cycle soon.

    Wake is stored in PostgreSQL (``brain_worker_control``) so it works even when the
    API and worker disagree on ``data/`` paths or ``brain_worker_status.json`` is missing.
    A legacy file in ``data/brain_worker_wake`` is also touched when possible.
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot control worker"}, status_code=403)

    if _BRAIN_WORKER_PAUSE_SIGNAL.exists():
        return JSONResponse(
            {"ok": False, "error": "Worker is paused — resume first, then run next cycle."},
            status_code=400,
        )

    from ...services.brain_worker_signals import set_wake_requested

    try:
        set_wake_requested(db)
        db.commit()
    except Exception as e:
        _log.exception("brain wake DB failed")
        return JSONResponse({"ok": False, "error": f"Could not queue wake: {e}"}, status_code=500)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _BRAIN_WORKER_WAKE_SIGNAL.touch()
    except OSError:
        pass

    notes: list[str] = []
    if _BRAIN_WORKER_STATUS_FILE.exists():
        try:
            with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
                sdata = json.load(f)
            pid = sdata.get("pid")
            if pid:
                liv = _worker_liveness(int(pid) if pid else None)
                if liv == "dead":
                    notes.append(
                        "Local status PID looks dead; DB wake is still set for any worker using this database."
                    )
                elif liv == "unknown":
                    notes.append("Could not verify local PID; DB wake is authoritative.")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            notes.append("Could not read local status file; DB wake is authoritative.")
    else:
        notes.append("No brain_worker_status.json — DB wake will still reach the worker on the same database.")

    from ...services.brain_worker_signals import (
        get_worker_control_snapshot,
        heartbeat_is_stale,
    )

    ctrl = get_worker_control_snapshot(db)
    worker_heartbeat_fresh = bool(
        ctrl and ctrl.last_heartbeat_at and not heartbeat_is_stale(ctrl.last_heartbeat_at)
    )
    warnings: list[str] = []
    if not worker_heartbeat_fresh:
        warnings.append(
            "No recent worker heartbeat was detected. A separate process must be running "
            "(Start on this page runs scripts/brain_worker.py) with the same DATABASE_URL as this app. "
            "Otherwise the wake is stored but nothing will act on it. "
            'To drain the backtest queue without the worker, use "Process queue on server".'
        )

    last_hb = None
    if ctrl and ctrl.last_heartbeat_at:
        last_hb = ctrl.last_heartbeat_at.isoformat() + "Z"

    return JSONResponse(
        {
            "ok": True,
            "message": "Wake queued in database. The worker checks this every few seconds while idle.",
            "notes": notes,
            "worker_heartbeat_fresh": worker_heartbeat_fresh,
            "warnings": warnings,
            "last_heartbeat_at": last_hb,
        },
    )


@router.get("/api/trading/brain/worker/recent-activity")
def api_brain_worker_recent_activity(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    """Get recent learning activity for the brain worker dashboard."""
    from ...models.trading import LearningEvent
    from datetime import datetime, timedelta
    
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot access activity"}, status_code=403)
    
    cutoff = datetime.utcnow() - timedelta(hours=24)
    
    events = (
        db.query(LearningEvent)
        .filter(LearningEvent.created_at >= cutoff)
        .order_by(LearningEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    
    activity = []
    for e in events:
        activity.append({
            "id": e.id,
            "type": e.event_type,
            "summary": e.description[:200] if e.description else "",
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    
    return JSONResponse({"ok": True, "activity": activity})


@router.get("/api/trading/brain/worker/queue-debug")
def api_brain_worker_queue_debug(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(30, ge=1, le=200),
):
    """First N scan_pattern IDs eligible for the backtest queue (same rules as the worker).

    Signed-in users only (not demo). For debugging 'pending' vs dashboard counts.
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot access queue debug"}, status_code=403)

    from ...services.trading.backtest_queue import get_pending_patterns, get_retest_interval_days

    ids = get_pending_patterns(db, limit=limit, ids_only=True)
    rd = get_retest_interval_days()
    return JSONResponse(
        {
            "ok": True,
            "eligible_pending_pattern_ids": ids,
            "limit": limit,
            "retest_interval_days": rd,
            "note": (
                f"Same eligibility as worker queue: active patterns that are boosted, "
                f"never backtested, or last_backtest older than {rd} days."
            ),
        }
    )


@router.post("/api/trading/brain/worker/run-queue-batch")
async def api_brain_worker_run_queue_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Run a single ``_auto_backtest_from_queue`` batch in the web process (BackgroundTasks).

    Does not require ``brain_worker.py``. Useful when the worker is idle or not running.
    Optional JSON body: ``{\"batch_size\": 20}`` (must be positive int; else uses settings default).
    """
    ctx = get_identity_ctx(request, db)
    if ctx.get("demo"):
        return JSONResponse({"ok": False, "error": "Demo users cannot run queue batch"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_bs = body.get("batch_size")
    batch_size = None
    if isinstance(raw_bs, int) and raw_bs > 0:
        batch_size = raw_bs
    elif isinstance(raw_bs, str) and raw_bs.isdigit():
        batch_size = int(raw_bs)

    from ...db import SessionLocal

    def _bg_queue_batch(user_id: int, bs: int | None) -> None:
        sdb = SessionLocal()
        try:
            from ...services.trading.learning import _auto_backtest_from_queue

            result = _auto_backtest_from_queue(sdb, user_id, batch_size=bs)
            _log.info("[brain_worker] run-queue-batch finished: %s", result)
        except Exception as e:
            _log.exception("[brain_worker] run-queue-batch failed: %s", e)
        finally:
            sdb.close()

    background_tasks.add_task(_bg_queue_batch, ctx["user_id"], batch_size)
    return JSONResponse(
        {
            "ok": True,
            "message": (
                "Backtest queue batch started in background. "
                "Check server logs for lines starting with [learning] Queue backtest."
            ),
        }
    )
