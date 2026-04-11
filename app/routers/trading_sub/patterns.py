"""Pattern Engine: CRUD, Pine export, LLM-powered suggest, pattern backtest, research.

Extracted from the main trading router.
"""
from __future__ import annotations

import json as _json
import logging
from typing import Literal, cast

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...schemas.trading import PatternBacktestRequest
from ._utils import json_safe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading-patterns"])


# ── Pydantic models for pattern endpoints ────────────────────────────────────

class _CreatePatternBody(BaseModel):
    name: str
    description: str = ""
    rules_json: str = "{}"
    origin: str = "user"
    asset_class: str = "all"
    score_boost: float = 0.0
    min_base_score: float = 0.0


class _PatternBody(BaseModel):
    name: str | None = None
    description: str | None = None
    rules_json: str | None = None
    active: bool | None = None
    score_boost: float | None = None
    min_base_score: float | None = None
    asset_class: str | None = None


class _SuggestPatternBody(BaseModel):
    description: str


# ── Pattern CRUD ─────────────────────────────────────────────────────────────

@router.get("/patterns")
def api_list_patterns(
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    from ...services.trading.public_api import list_patterns
    patterns = list_patterns(db, active_only=active_only)
    return JSONResponse({"ok": True, "patterns": patterns})


@router.post("/patterns")
def api_create_pattern(
    body: _CreatePatternBody,
    db: Session = Depends(get_db),
):
    from ...services.trading.public_api import create_pattern
    try:
        p = create_pattern(db, body.dict())
        return JSONResponse({"ok": True, "pattern": {"id": p.id, "name": p.name}})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.put("/patterns/{pattern_id}")
def api_update_pattern(
    pattern_id: int,
    body: _PatternBody,
    db: Session = Depends(get_db),
):
    from ...services.trading.public_api import update_pattern
    data = {k: v for k, v in body.dict().items() if v is not None}
    p = update_pattern(db, pattern_id, data)
    if not p:
        return JSONResponse({"ok": False, "error": "Pattern not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/patterns/{pattern_id}")
def api_delete_pattern(
    pattern_id: int,
    db: Session = Depends(get_db),
):
    from ...services.trading.public_api import delete_pattern
    ok = delete_pattern(db, pattern_id)
    return JSONResponse({"ok": ok})


# ── Pine Script export ───────────────────────────────────────────────────────

@router.get("/patterns/{pattern_id}/export/pine")
def api_export_pattern_pine(
    pattern_id: int,
    kind: str = "strategy",
    db: Session = Depends(get_db),
):
    """Export pattern rules as TradingView Pine Script v5 (best-effort; see ``warnings``).

    ``kind=strategy`` (default): ``strategy()`` for Strategy Tester.
    ``kind=indicator``: ``indicator()`` with plotshape / alerts.
    """
    from ...models.trading import ScanPattern
    from ...services.trading.public_api import scan_pattern_to_pine

    k = (kind or "strategy").strip().lower()
    if k not in ("strategy", "indicator"):
        return JSONResponse(
            {"ok": False, "error": "kind must be strategy or indicator"},
            status_code=400,
        )

    p = db.query(ScanPattern).get(pattern_id)
    if not p:
        return JSONResponse({"ok": False, "error": "Pattern not found"}, status_code=404)
    pine, warnings = scan_pattern_to_pine(
        p, kind=cast(Literal["strategy", "indicator"], k)
    )
    return JSONResponse(
        {
            "ok": True,
            "pine": pine,
            "warnings": warnings,
            "pattern_id": p.id,
            "name": p.name,
            "kind": k,
        }
    )


# ── LLM-powered pattern suggest ─────────────────────────────────────────────

@router.post("/patterns/suggest")
def api_suggest_pattern(
    body: _SuggestPatternBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Parse a natural language pattern description into a ScanPattern, TradingHypothesis, and TradingInsight."""
    from ...services.llm_caller import call_llm
    from ...services.trading.public_api import create_pattern
    from ...models.trading import TradingHypothesis, TradingInsight

    prompt = (
        "Convert this trading pattern description into a structured JSON pattern rule.\n\n"
        f'Description: "{body.description}"\n\n'
        "Respond with a JSON object:\n"
        '{"name": "Short descriptive name", "description": "...", '
        '"conditions": [{"indicator": "...", "op": "...", "value": ...}], '
        '"score_boost": 1.5, "min_base_score": 4.0}\n\n'
        "Available indicators: rsi_14, ema_20, ema_50, ema_100, price, bb_squeeze, adx, "
        "rel_vol, macd_hist, resistance_retests, dist_to_resistance_pct, narrow_range, "
        "vcp_count, vwap_reclaim.\n"
        "Available ops: >, >=, <, <=, ==, between, any_of.\n"
        "For 'price' comparisons use 'ref' key pointing to indicator name.\n"
        "Respond ONLY with the JSON object."
    )

    try:
        resp = call_llm(prompt, max_tokens=800)
        if not resp:
            return JSONResponse({"ok": False, "error": "LLM returned empty response"}, status_code=500)

        text = resp.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        parsed = _json.loads(text)
        conditions = parsed.get("conditions", [])
        pattern_data = {
            "name": parsed.get("name", "Custom Pattern"),
            "description": parsed.get("description", body.description),
            "rules_json": _json.dumps({"conditions": conditions}),
            "origin": "user",
            "score_boost": parsed.get("score_boost", 1.0),
            "min_base_score": parsed.get("min_base_score", 4.0),
            "confidence": 0.0,
            "active": True,
        }
        p = create_pattern(db, pattern_data)

        hypothesis_id = None
        if len(conditions) >= 2:
            cond_a_parts = {c.get("indicator", "?"): c for c in conditions}
            partial = conditions[:-1]
            cond_b_parts = {c.get("indicator", "?"): c for c in partial}

            hyp_desc = (
                f"Full pattern '{p.name}' ({len(conditions)} conditions) "
                f"outperforms partial ({len(partial)} conditions, "
                f"without {conditions[-1].get('indicator', '?')})"
            )
            existing = db.query(TradingHypothesis).filter(
                TradingHypothesis.description == hyp_desc,
            ).first()
            if not existing:
                hyp = TradingHypothesis(
                    description=hyp_desc,
                    condition_a=_json.dumps(cond_a_parts),
                    condition_b=_json.dumps(cond_b_parts),
                    expected_winner="a",
                    origin="user",
                    status="pending",
                    related_pattern_id=p.id,
                )
                db.add(hyp)
                db.commit()
                db.refresh(hyp)
                hypothesis_id = hyp.id

        ctx = get_identity_ctx(request, db)
        uid = ctx.get("user_id")
        insight_desc = (
            f"{p.name} — {p.description or body.description} [User-suggested pattern]"
        )
        existing_insight = db.query(TradingInsight).filter(
            TradingInsight.pattern_description.like(f"{p.name}%"),
            TradingInsight.user_id == uid,
        ).first()
        if not existing_insight:
            insight = TradingInsight(
                user_id=uid,
                scan_pattern_id=p.id,
                pattern_description=insight_desc,
                confidence=0.5,
                evidence_count=1,
                active=True,
                win_count=0,
                loss_count=0,
            )
            db.add(insight)
            db.commit()

        return JSONResponse({
            "ok": True,
            "pattern": {
                "id": p.id, "name": p.name, "description": p.description,
                "rules_json": p.rules_json, "score_boost": p.score_boost,
            },
            "hypothesis_id": hypothesis_id,
        })
    except _json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Could not parse LLM response as JSON"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Pattern backtest ─────────────────────────────────────────────────────────

@router.post("/patterns/{pattern_id}/backtest")
def api_backtest_pattern(
    pattern_id: int,
    ticker: str = Query("AAPL"),
    interval: str = Query("1d"),
    period: str = Query("1y"),
    db: Session = Depends(get_db),
    body: PatternBacktestRequest | None = Body(default=None),
):
    from ...services.backtest_service import backtest_pattern, get_backtest_params
    from ...services.trading.public_api import resolve_to_scan_pattern

    req = body or PatternBacktestRequest()
    p = resolve_to_scan_pattern(db, pattern_id)
    if not p:
        return JSONResponse({"ok": False, "error": "Pattern not found"}, status_code=404)
    tf = getattr(p, "timeframe", "1d") or "1d"
    bt_params = get_backtest_params(tf)
    use_interval = interval if interval != "1d" else bt_params["interval"]
    use_period = period if period != "1y" else bt_params["period"]
    if req.interval:
        use_interval = req.interval
    if req.period:
        use_period = req.period
    from ...config import settings

    use_ticker = (req.ticker or ticker).strip().upper()
    cash = req.cash if req.cash is not None else 100_000.0
    commission = req.commission if req.commission is not None else float(settings.backtest_commission)
    spread = req.spread if req.spread is not None else float(settings.backtest_spread)
    result = backtest_pattern(
        ticker=use_ticker,
        pattern_name=p.name,
        rules_json=p.rules_json,
        interval=use_interval,
        period=use_period,
        exit_config=getattr(p, "exit_config", None),
        cash=cash,
        commission=commission,
        spread=spread,
        oos_holdout_fraction=req.oos_holdout_fraction,
        rules_json_override=req.rules_json_override,
        append_conditions=req.append_conditions,
        exit_config_overlay=req.exit_config,
    )
    if not result.get("ok"):
        return JSONResponse(json_safe(result), status_code=400)
    return JSONResponse(json_safe(result))


# ── Web research ─────────────────────────────────────────────────────────────

@router.post("/patterns/research")
def api_trigger_pattern_research(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Trigger web pattern research manually."""
    from ...services.trading.public_api import run_web_pattern_research
    background_tasks.add_task(run_web_pattern_research, db=None)
    return JSONResponse({"ok": True, "message": "Web pattern research started in background"})


@router.get("/patterns/research/status")
def api_pattern_research_status():
    """Get the current status of web pattern research."""
    from ...services.trading.public_api import get_research_status
    return JSONResponse({"ok": True, **get_research_status()})
