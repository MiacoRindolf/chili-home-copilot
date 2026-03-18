"""AI Brain / learning endpoints for the trading module."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...logger import log_info, new_trace_id
from ...prompts import load_prompt
from ...services import trading_service as ts
from ...services import trading_scheduler
from ...services import ticker_universe

router = APIRouter(tags=["trading-ai"])

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


@router.get("/api/trading/brain/thesis")
def api_brain_thesis(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    thesis = ts.generate_market_thesis(db, ctx["user_id"])
    return JSONResponse({"ok": True, **thesis})


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


@router.get("/api/trading/scan/status")
def api_scan_status():
    return JSONResponse({
        "ok": True,
        "scan": ts.get_scan_status(),
        "learning": ts.get_learning_status(),
        "prescreen": ts.get_prescreen_status(),
        "scheduler": trading_scheduler.get_scheduler_info(),
    })


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
    ctx = get_identity_ctx(request, db)
    result = ts.deep_study(db, ctx["user_id"])
    return JSONResponse(result)


@router.post("/api/trading/learn/retrain-ml")
def api_retrain_ml(request: Request, db: Session = Depends(get_db)):
    result = ts.train_ml_model(db)
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


@router.get("/api/trading/learn/patterns")
def api_learned_patterns(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    from ...models.trading import TradingInsight, BacktestResult, ScanPattern

    all_insights = db.query(TradingInsight).filter(
        TradingInsight.user_id == ctx["user_id"],
    ).order_by(TradingInsight.confidence.desc()).limit(200).all()

    scan_patterns_by_name: dict[str, ScanPattern] = {}
    scan_patterns_by_id: dict[int, ScanPattern] = {}
    for sp in db.query(ScanPattern).all():
        scan_patterns_by_name[sp.name.lower()] = sp
        scan_patterns_by_id[sp.id] = sp

    insight_ids = [ins.id for ins in all_insights]
    bt_tickers_map: dict[int, list[str]] = {}
    bt_wl_map: dict[int, tuple[int, int]] = {}
    if insight_ids:
        for row in (
            db.query(
                BacktestResult.related_insight_id,
                BacktestResult.ticker,
                BacktestResult.return_pct,
                BacktestResult.trade_count,
            )
            .filter(
                BacktestResult.related_insight_id.in_(insight_ids),
                BacktestResult.related_insight_id.isnot(None),
            )
            .all()
        ):
            bt_tickers_map.setdefault(row[0], []).append(row[1])
            if (row[3] or 0) > 0:
                w, l = bt_wl_map.get(row[0], (0, 0))
                if (row[2] or 0) > 0:
                    bt_wl_map[row[0]] = (w + 1, l)
                else:
                    bt_wl_map[row[0]] = (w, l + 1)

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

        bt_tickers = bt_tickers_map.get(ins.id, [])
        all_tickers = list(set(tickers_found + bt_tickers))
        sectors = _detect_sectors(all_tickers)
        is_crypto = "crypto" in sectors

        bt_wl = bt_wl_map.get(ins.id)
        if bt_wl:
            wc, lc = bt_wl
        else:
            wc = ins.win_count or 0
            lc = ins.loss_count or 0
        real_wr = round(wc / max(1, wc + lc) * 100, 1) if (wc + lc) > 0 else None

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
        Trade, BacktestResult,
    )

    insight = db.query(TradingInsight).filter(
        TradingInsight.id == pattern_id,
        TradingInsight.user_id == ctx["user_id"],
    ).first()
    if not insight:
        return JSONResponse({"ok": False, "reason": "Pattern not found"}, status_code=404)

    desc = insight.pattern_description or ""

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

    # 4. Backtest results: first by direct link, then by keyword matching
    backtests_out = []
    seen_bt_ids: set[int] = set()
    try:
        linked_bts = (
            db.query(BacktestResult)
            .filter(BacktestResult.related_insight_id == pattern_id)
            .order_by(BacktestResult.ran_at.desc())
            .limit(60)
            .all()
        )
        for bt in linked_bts:
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

        if len(backtests_out) < 15:
            bt_keywords = _extract_keywords_for_matching(desc, min_len=5)
            all_bts = (
                db.query(BacktestResult)
                .order_by(BacktestResult.ran_at.desc())
                .limit(200)
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
                0 if (x.get("trade_count") or 0) > 0 else 1,  # trades first
                -(x.get("return_pct") or 0),                   # highest return
                -x.get("relevance", 0),
            )
        )
    except Exception:
        pass

    # 5. Compute live aggregate stats from actual evidence
    computed_stats = _compute_evidence_stats(timeline, hypotheses, trades_out, backtests_out)

    # Compute win/loss directly from actual BacktestResult records (not the
    # cumulative counters on TradingInsight which can drift from duplicates).
    bt_with_trades = [b for b in backtests_out if (b.get("trade_count") or 0) > 0]
    bt_wins = sum(1 for b in bt_with_trades if (b.get("return_pct") or 0) > 0)
    bt_losses = len(bt_with_trades) - bt_wins
    bt_total = bt_wins + bt_losses
    bt_win_rate = round(bt_wins / max(1, bt_total) * 100, 1) if bt_total > 0 else None

    return JSONResponse({
        "ok": True,
        "insight": {
            "id": insight.id,
            "pattern": desc,
            "confidence": round(insight.confidence * 100, 1),
            "evidence_count": insight.evidence_count,
            "win_count": bt_wins,
            "loss_count": bt_losses,
            "win_rate": bt_win_rate,
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


@router.get("/api/trading/learn/patterns/{pattern_id}/evolution")
def api_pattern_evolution(pattern_id: int, request: Request, db: Session = Depends(get_db)):
    """Return the full evolution tree for a pattern's ScanPattern lineage."""
    ctx = get_identity_ctx(request, db)
    from ...models.trading import (
        TradingInsight, ScanPattern, TradingHypothesis, BacktestResult,
    )
    from sqlalchemy import func as sa_func, case as sa_case

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
    queue = [root]
    while queue:
        node = queue.pop(0)
        children = (
            db.query(ScanPattern)
            .filter(ScanPattern.parent_id == node.id)
            .order_by(ScanPattern.generation, ScanPattern.id)
            .all()
        )
        all_patterns.extend(children)
        queue.extend(children)

    all_sp_ids = [p.id for p in all_patterns]

    bt_stats: dict[int, dict] = {}
    insight_by_sp: dict[int, int] = {}
    for row in (
        db.query(TradingInsight.scan_pattern_id, TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id.in_(all_sp_ids))
        .all()
    ):
        insight_by_sp.setdefault(row[0], row[1])

    linked_insight_ids = list(insight_by_sp.values())
    if linked_insight_ids:
        for row in (
            db.query(
                BacktestResult.related_insight_id,
                sa_func.count(BacktestResult.id),
                sa_func.sum(
                    sa_case(
                        (BacktestResult.return_pct > 0, 1), else_=0,
                    )
                ),
                sa_func.avg(BacktestResult.return_pct),
            )
            .filter(
                BacktestResult.related_insight_id.in_(linked_insight_ids),
                BacktestResult.trade_count > 0,
            )
            .group_by(BacktestResult.related_insight_id)
            .all()
        ):
            ins_id, total, wins, avg_ret = row
            for sp_id_k, ins_id_k in insight_by_sp.items():
                if ins_id_k == ins_id:
                    bt_stats[sp_id_k] = {
                        "total": total or 0,
                        "wins": int(wins or 0),
                        "avg_return_pct": round(float(avg_ret or 0), 2),
                    }

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
        losses = total - wins
        wr = round(wins / max(1, total) * 100, 1) if total > 0 else None

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


def _compute_evidence_stats(
    timeline: list, hypotheses: list, trades: list, backtests: list,
) -> dict:
    """Compute live aggregate stats from the actual evidence data."""
    stats: dict = {}

    # From hypotheses: average confirmation rate
    if hypotheses:
        tested = [h for h in hypotheses if h.get("times_tested", 0) > 0]
        if tested:
            avg_confirm = sum(h["confirm_rate"] for h in tested) / len(tested)
            stats["hypothesis_confirm_rate"] = round(avg_confirm, 1)
            stats["hypotheses_tested"] = len(tested)

    # From trades: real trade win rate and P&L
    if trades:
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = sum(1 for t in closed if t["pnl"] > 0)
            stats["trade_win_rate"] = round(wins / len(closed) * 100, 1)
            stats["trade_count"] = len(closed)
            stats["total_pnl"] = round(sum(t["pnl"] for t in closed), 2)

    # From backtests: average return and win rate
    if backtests:
        stats["backtest_avg_return"] = round(
            sum(b["return_pct"] for b in backtests) / len(backtests), 1
        )
        stats["backtest_avg_win_rate"] = round(
            sum(b["win_rate"] for b in backtests) / len(backtests), 1
        )
        stats["backtest_count"] = len(backtests)

    # From timeline: count of confirmations vs challenges
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
