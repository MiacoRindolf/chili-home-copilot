"""Pattern Engine: CRUD, Pine export, LLM-powered suggest, pattern backtest, research.

Extracted from the main trading router.
"""
from __future__ import annotations

import json as _json
import logging
import re
from typing import Any, Literal, cast

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


_MECHANICAL_INDICATORS: tuple[tuple[str, str], ...] = (
    (r"\bema[\s_-]?200\b", "ema_200"),
    (r"\bema[\s_-]?100\b", "ema_100"),
    (r"\bema[\s_-]?50\b", "ema_50"),
    (r"\bema[\s_-]?21\b", "ema_21"),
    (r"\bema[\s_-]?20\b", "ema_20"),
    (r"\bema[\s_-]?9\b", "ema_9"),
    (r"\bsma[\s_-]?200\b", "sma_200"),
    (r"\bsma[\s_-]?100\b", "sma_100"),
    (r"\bsma[\s_-]?50\b", "sma_50"),
    (r"\bsma[\s_-]?20\b", "sma_20"),
    (r"\brsi(?:[\s_-]?14)?\b", "rsi_14"),
    (r"\bmacd(?:[\s_-]?hist(?:ogram)?)\b", "macd_hist"),
    (r"\bmacd[\s_-]?hist(?:ogram)?\b", "macd_hist"),
    (r"\badx\b", "adx"),
    (r"\b(?:relative\s+volume|rel[\s_-]?vol|rvol|volume\s+ratio)\b", "rel_vol"),
    (r"\bresistance\s+retests?\b", "resistance_retests"),
    (
        r"\b(?:distance|dist)\s+to\s+resistance(?:\s+pct|\s+percent|%)?\b",
        "dist_to_resistance_pct",
    ),
    (r"\bvcp(?:\s+count)?\b", "vcp_count"),
    (r"\bprice\b", "price"),
    (r"\bvwap\b", "vwap_reclaim"),
)

_MECHANICAL_OPERATOR_PATTERNS: tuple[tuple[str, str], ...] = (
    (r">=", ">="),
    (r"<=", "<="),
    (r"==", "=="),
    (r"!=", "!="),
    (r">", ">"),
    (r"<", "<"),
    (r"=", "=="),
    (r"\bat\s+least\b", ">="),
    (r"\bno\s+less\s+than\b", ">="),
    (r"\bgreater\s+than\s+or\s+equal\s+to\b", ">="),
    (r"\babove\s+or\s+equal\s+to\b", ">="),
    (r"\bover\s+or\s+equal\s+to\b", ">="),
    (r"\bat\s+most\b", "<="),
    (r"\bno\s+more\s+than\b", "<="),
    (r"\bless\s+than\s+or\s+equal\s+to\b", "<="),
    (r"\bbelow\s+or\s+equal\s+to\b", "<="),
    (r"\bgreater\s+than\b", ">"),
    (r"\babove\b", ">"),
    (r"\bover\b", ">"),
    (r"\bless\s+than\b", "<"),
    (r"\bunder\b", "<"),
    (r"\bbelow\b", "<"),
    (r"\bequals?\b", "=="),
    (r"\bis\b", "=="),
)

_NUMBER_RE = r"[-+]?\d+(?:\.\d+)?"


def _mechanical_split_segments(description: str) -> list[str]:
    protected = re.sub(
        rf"(\bbetween\s+{_NUMBER_RE})\s+and\s+({_NUMBER_RE})",
        r"\1 __MECH_AND__ \2",
        description.strip().lower(),
    )
    parts = re.split(r"\s*(?:,|;|\+|\bwith\b|\bplus\b|\band\b)\s*", protected)
    return [
        p.replace("__MECH_AND__", "and").replace("__mech_and__", "and").strip()
        for p in parts
        if p.strip()
    ]


def _mechanical_indicators(segment: str) -> list[str]:
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for pattern, indicator in _MECHANICAL_INDICATORS:
        match = re.search(pattern, segment, flags=re.IGNORECASE)
        if match and indicator not in seen:
            found.append((match.start(), indicator))
            seen.add(indicator)
    return [indicator for _, indicator in sorted(found, key=lambda item: item[0])]


def _mechanical_between_condition(segment: str, indicator: str) -> dict[str, Any] | None:
    match = re.search(
        rf"\bbetween\s+({_NUMBER_RE})\s+(?:and|to|-)\s+({_NUMBER_RE})\b",
        segment,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    lo = float(match.group(1))
    hi = float(match.group(2))
    return {"indicator": indicator, "op": "between", "value": [min(lo, hi), max(lo, hi)]}


def _mechanical_operator(segment: str) -> tuple[str, int] | None:
    matches: list[tuple[int, int, str]] = []
    for pattern, op in _MECHANICAL_OPERATOR_PATTERNS:
        match = re.search(pattern, segment, flags=re.IGNORECASE)
        if match:
            matches.append((match.start(), match.end(), op))
    if not matches:
        return None
    _start, end, op = sorted(matches, key=lambda item: item[0])[0]
    return op, end


def _mechanical_number_after(segment: str, start: int) -> float | None:
    match = re.search(_NUMBER_RE, segment[start:])
    if not match:
        return None
    return float(match.group(0))


def _mechanical_condition_from_segment(segment: str) -> dict[str, Any] | None:
    if not segment:
        return None

    if re.search(r"\b(?:bb|bollinger)\b.*\bsqueeze\b|\bsqueeze\b.*\b(?:bb|bollinger)\b", segment):
        return {"indicator": "bb_squeeze", "op": "==", "value": True}
    if re.search(
        r"\bvwap\b.*\b(?:reclaim|cross|break|above|over)\b|"
        r"\b(?:reclaim|cross|break|above|over)\b.*\bvwap\b",
        segment,
    ):
        return {"indicator": "vwap_reclaim", "op": "==", "value": True}
    nr_match = re.search(r"\bnr\s*([47])\b", segment)
    if nr_match:
        return {"indicator": "narrow_range", "op": "==", "value": f"NR{nr_match.group(1)}"}
    if re.search(r"\bnarrow\s+range\b", segment):
        return {"indicator": "narrow_range", "op": "any_of", "value": ["NR4", "NR7"]}

    indicators = _mechanical_indicators(segment)
    if not indicators:
        return None

    between = _mechanical_between_condition(segment, indicators[0])
    if between:
        return between

    op_match = _mechanical_operator(segment)
    if not op_match:
        return None
    op, value_start = op_match

    if len(indicators) >= 2:
        return {"indicator": indicators[0], "op": op, "ref": indicators[1]}

    value = _mechanical_number_after(segment, value_start)
    if value is None:
        return None
    return {"indicator": indicators[0], "op": op, "value": value}


def _mechanical_pattern_name(conditions: list[dict[str, Any]]) -> str:
    labels: list[str] = []
    for condition in conditions:
        label = str(condition.get("indicator", "pattern")).replace("_", " ").upper()
        if label not in labels:
            labels.append(label)
    return " + ".join(labels[:4]) + " Setup"


def _mechanical_pattern_suggestion(description: str) -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for segment in _mechanical_split_segments(description):
        condition = _mechanical_condition_from_segment(segment)
        if not condition:
            continue
        signature = _json.dumps(condition, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        conditions.append(condition)

    if len(conditions) < 2:
        return None

    return {
        "name": _mechanical_pattern_name(conditions),
        "description": description,
        "conditions": conditions,
        "score_boost": 1.0,
        "min_base_score": 4.0,
        "source": "mechanical",
    }


# ── Pattern CRUD ─────────────────────────────────────────────────────────────

@router.get("/patterns")
def api_list_patterns(
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    from ...services.trading.public_api import list_patterns
    patterns = list_patterns(db, active_only=active_only)
    return JSONResponse({"ok": True, "patterns": json_safe(patterns)})


@router.post("/patterns")
def api_create_pattern(
    body: _CreatePatternBody,
    db: Session = Depends(get_db),
):
    from ...services.trading.public_api import create_pattern
    try:
        p = create_pattern(db, body.model_dump())
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
    data = {k: v for k, v in body.model_dump().items() if v is not None}
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
    from ...services.trading.public_api import create_pattern
    from ...models.trading import TradingHypothesis, TradingInsight

    prompt = (
        "Convert this trading pattern description into a structured JSON pattern rule.\n\n"
        "Respond with a JSON object:\n"
        '{"name": "Short descriptive name", "description": "...", '
        '"conditions": [{"indicator": "...", "op": "...", "value": ...}], '
        '"score_boost": 1.5, "min_base_score": 4.0}\n\n'
        "Available indicators: rsi_14, ema_20, ema_50, ema_100, price, bb_squeeze, adx, "
        "rel_vol, macd_hist, resistance_retests, dist_to_resistance_pct, narrow_range, "
        "vcp_count, vwap_reclaim.\n"
        "Available ops: >, >=, <, <=, ==, between, any_of.\n"
        "For 'price' comparisons use 'ref' key pointing to indicator name.\n"
        "Respond ONLY with the JSON object.\n\n"
        f'Description: "{body.description}"'
    )

    try:
        suggestion_source = "mechanical"
        parsed = _mechanical_pattern_suggestion(body.description)
        if parsed is None:
            from ...services.llm_caller import call_llm

            suggestion_source = "llm"
            resp = call_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
                trace_id="pattern-suggest",
                purpose="pattern_suggest",
            )
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
            "suggestion_source": suggestion_source,
        })
    except _json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Could not parse suggestion response as JSON"}, status_code=500)
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
