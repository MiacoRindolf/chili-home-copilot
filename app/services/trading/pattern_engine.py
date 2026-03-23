"""Composable pattern engine for breakout/screener patterns.

Provides a JSON-based DSL for defining patterns, an evaluator that checks
patterns against pre-computed indicators, and CRUD helpers for the
``ScanPattern`` model.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern  # noqa: E402

logger = logging.getLogger(__name__)

# ── Builtin patterns (seeded on first run) ─────────────────────────────

_BUILTIN_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "Momentum Breakout",
        "description": (
            "RSI > 65 with full EMA stack (20/50/100) bullish, "
            "3+ resistance retests, and post-retest consolidation. "
            "Inspired by Minervini SEPA / Qullamaggie setups."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "score_boost": 2.5,
        "min_base_score": 4.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "rsi_14", "op": ">", "value": 65},
                {"indicator": "price", "op": ">", "ref": "ema_20"},
                {"indicator": "price", "op": ">", "ref": "ema_50"},
                {"indicator": "price", "op": ">", "ref": "ema_100"},
                {"indicator": "resistance_retests", "op": ">=", "value": 3,
                 "params": {"tolerance_pct": 1.5, "lookback": 20}},
            ],
        }),
    },
    {
        "name": "BB Squeeze Breakout",
        "description": (
            "Bollinger Band squeeze with ADX < 20 (consolidation), "
            "RSI in neutral zone, declining volume — classic coiling setup."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "score_boost": 2.0,
        "min_base_score": 4.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "bb_squeeze", "op": "==", "value": True},
                {"indicator": "adx", "op": "<", "value": 20},
                {"indicator": "rsi_14", "op": "between", "value": [40, 65]},
            ],
        }),
    },
    {
        "name": "VWAP Reclaim + Volume",
        "description": (
            "Price reclaims VWAP from below with relative volume >= 1.5 "
            "and MACD histogram turning positive."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "vwap_reclaim", "op": "==", "value": True},
                {"indicator": "rel_vol", "op": ">=", "value": 1.5},
                {"indicator": "macd_hist", "op": ">", "value": 0},
            ],
        }),
    },
    {
        "name": "Tight Range + Volume Contraction",
        "description": (
            "NR4/NR7 narrow range combined with VCP (2+ contractions) "
            "near resistance — classic volatility-contraction breakout."
        ),
        "origin": "builtin",
        "asset_class": "stocks",
        "score_boost": 2.0,
        "min_base_score": 4.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "narrow_range", "op": "any_of", "value": ["NR4", "NR7"]},
                {"indicator": "vcp_count", "op": ">=", "value": 2},
                {"indicator": "dist_to_resistance_pct", "op": "<=", "value": 3.0},
            ],
        }),
    },
]

# Community / link-attributed seeds (insert once by name; not ``origin=builtin``).
_COMMUNITY_SEED_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "Reddit r/Daytrading — IBS mean reversion (vaanam-dev)",
        "description": (
            "Community seed from u/vaanam-dev (r/Daytrading). "
            "**Entry (both):** (1) close < 10-day high minus 2.5×(25-day average high "
            "minus 25-day average low). (2) IBS = (close−low)/(high−low) < 0.3. "
            "CHILI backtests use the standard pattern engine exits (ATR trail, max hold, BOS), "
            "not the author’s discretionary exit — treat as inspiration, not a replica."
        ),
        "origin": "user_seeded",
        "asset_class": "stocks",
        "timeframe": "1d",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "pullback_stretch_entry", "op": "==", "value": True},
                {"indicator": "ibs", "op": "<", "value": 0.3},
            ],
        }),
    },
]


def seed_builtin_patterns(db: Session) -> int:
    """Insert builtin patterns if they don't already exist. Returns count added."""
    existing = {p.name for p in db.query(ScanPattern).filter_by(origin="builtin").all()}
    added = 0
    from ...services.backtest_service import infer_pattern_timeframe
    import json as _json2

    for bp in _BUILTIN_PATTERNS:
        if bp["name"] in existing:
            continue
        conds = []
        try:
            conds = _json2.loads(bp["rules_json"]).get("conditions", [])
        except Exception:
            pass
        tf = bp.get("timeframe") or infer_pattern_timeframe(
            conds, name=bp["name"], asset_class=bp.get("asset_class", "all"),
        )
        p = ScanPattern(
            name=bp["name"],
            description=bp.get("description", ""),
            rules_json=bp["rules_json"],
            origin=bp["origin"],
            asset_class=bp.get("asset_class", "all"),
            timeframe=tf,
            score_boost=bp.get("score_boost", 0.0),
            min_base_score=bp.get("min_base_score", 0.0),
            confidence=0.5,
            active=True,
        )
        db.add(p)
        added += 1
    if added:
        db.commit()
        logger.info("[pattern_engine] Seeded %d builtin patterns", added)
    return added


def seed_community_patterns(db: Session) -> int:
    """Insert link-attributed community patterns if no row with the same name exists."""
    existing = {p.name for p in db.query(ScanPattern).all()}
    added = 0
    from ...services.backtest_service import infer_pattern_timeframe
    import json as _json2

    for bp in _COMMUNITY_SEED_PATTERNS:
        if bp["name"] in existing:
            continue
        conds = []
        try:
            conds = _json2.loads(bp["rules_json"]).get("conditions", [])
        except Exception:
            pass
        tf = bp.get("timeframe") or infer_pattern_timeframe(
            conds,
            name=bp["name"],
            asset_class=bp.get("asset_class", "all"),
        )
        p = ScanPattern(
            name=bp["name"],
            description=bp.get("description", ""),
            rules_json=bp["rules_json"],
            origin=bp.get("origin", "user_seeded"),
            asset_class=bp.get("asset_class", "all"),
            timeframe=tf,
            score_boost=bp.get("score_boost", 0.0),
            min_base_score=bp.get("min_base_score", 0.0),
            confidence=0.5,
            active=True,
        )
        db.add(p)
        added += 1
    if added:
        db.commit()
        logger.info("[pattern_engine] Seeded %d community-linked patterns", added)
    return added


# ── Pattern evaluation ─────────────────────────────────────────────────

def get_active_patterns(db: Session, asset_class: str = "all") -> list[ScanPattern]:
    """Fetch all active patterns, optionally filtered by asset class."""
    q = db.query(ScanPattern).filter_by(active=True)
    patterns = q.all()
    return [p for p in patterns if p.asset_class in ("all", asset_class)]


def evaluate_patterns(
    indicators: dict[str, Any],
    patterns: list[ScanPattern],
) -> list[dict[str, Any]]:
    """Check which patterns match the given indicator snapshot.

    ``indicators`` is a flat dict like::

        {
            "price": 145.3,
            "rsi_14": 72.5,
            "ema_20": 140.1, "ema_50": 135.2, "ema_100": 128.0,
            "bb_squeeze": True,
            "adx": 18.5,
            "rel_vol": 2.3,
            "resistance_retests": 4,
            "dist_to_resistance_pct": 1.2,
            "macd_hist": 0.05,
            "vwap_reclaim": False,
            "narrow_range": "NR7",
            "vcp_count": 2,
            ...
        }

    Returns a list of matched patterns with their score boosts.
    """
    matches: list[dict[str, Any]] = []

    for pattern in patterns:
        try:
            rules = json.loads(pattern.rules_json)
        except (json.JSONDecodeError, TypeError):
            continue

        conditions = rules.get("conditions", [])
        if not conditions:
            continue

        all_met = True
        for cond in conditions:
            if not _eval_condition(cond, indicators):
                all_met = False
                break

        if all_met:
            matches.append({
                "pattern_id": pattern.id,
                "name": pattern.name,
                "score_boost": pattern.score_boost,
                "min_base_score": pattern.min_base_score,
                "confidence": pattern.confidence,
                "win_rate": pattern.win_rate,
            })

    return matches


def _eval_condition(cond: dict, indicators: dict[str, Any]) -> bool:
    """Evaluate a single condition against indicators."""
    ind_key = cond.get("indicator", "")
    op = cond.get("op", "")
    value = cond.get("value")
    ref = cond.get("ref")

    actual = indicators.get(ind_key)
    if actual is None:
        return False

    if ref:
        ref_val = indicators.get(ref)
        if ref_val is None:
            return False
        value = ref_val

    try:
        if op == ">":
            return float(actual) > float(value)
        elif op == ">=":
            return float(actual) >= float(value)
        elif op == "<":
            return float(actual) < float(value)
        elif op == "<=":
            return float(actual) <= float(value)
        elif op == "==":
            return actual == value
        elif op == "!=":
            return actual != value
        elif op == "between":
            if isinstance(value, list) and len(value) == 2:
                return float(value[0]) <= float(actual) <= float(value[1])
        elif op == "any_of":
            if isinstance(value, list):
                return actual in value
        elif op == "not_in":
            if isinstance(value, list):
                return actual not in value
    except (TypeError, ValueError):
        return False

    return False


def _condition_has_data(cond: dict, indicators: dict[str, Any]) -> bool:
    """Check whether the indicator data required for a condition is available."""
    ind_key = cond.get("indicator", "")
    if indicators.get(ind_key) is None:
        return False
    ref = cond.get("ref")
    if ref and indicators.get(ref) is None:
        return False
    return True


def evaluate_patterns_soft(
    indicators: dict[str, Any],
    patterns: list[ScanPattern],
    min_eval_ratio: float = 0.5,
) -> list[dict[str, Any]]:
    """Partial-match pattern evaluation for prediction context.

    Unlike ``evaluate_patterns`` which requires ALL conditions to pass,
    this function skips conditions whose indicator data is unavailable
    (e.g. resistance_retests in a prediction snapshot). A pattern matches
    when at least *min_eval_ratio* of conditions are evaluable AND all
    evaluable conditions pass. Returns a ``match_quality`` (0-1) equal
    to the fraction of total conditions that were evaluated and passed.
    """
    matches: list[dict[str, Any]] = []

    for pattern in patterns:
        try:
            rules = json.loads(pattern.rules_json)
        except (json.JSONDecodeError, TypeError):
            continue

        conditions = rules.get("conditions", [])
        if not conditions:
            continue

        evaluable = []
        for cond in conditions:
            if _condition_has_data(cond, indicators):
                evaluable.append(cond)

        total = len(conditions)
        n_eval = len(evaluable)
        if n_eval < max(1, total * min_eval_ratio):
            continue

        all_pass = True
        for cond in evaluable:
            if not _eval_condition(cond, indicators):
                all_pass = False
                break

        if all_pass:
            match_quality = n_eval / total
            matches.append({
                "pattern_id": pattern.id,
                "name": pattern.name,
                "score_boost": pattern.score_boost,
                "min_base_score": pattern.min_base_score,
                "confidence": pattern.confidence,
                "win_rate": pattern.win_rate,
                "match_quality": round(match_quality, 2),
                "conditions_met": n_eval,
                "conditions_total": total,
            })

    return matches


def evaluate_patterns_with_strength(
    indicators: dict[str, Any],
    patterns: list[ScanPattern],
    min_eval_ratio: float = 0.5,
) -> list[dict[str, Any]]:
    """Partial-match evaluation with continuous condition strengths.

    Like ``evaluate_patterns_soft`` but also computes per-condition strength
    (0-1) via ``pattern_ml.compute_condition_strength``, returning
    ``avg_strength`` and a ``strengths`` list alongside the match result.
    """
    from .pattern_ml import compute_condition_strength

    matches: list[dict[str, Any]] = []

    for pattern in patterns:
        try:
            rules = json.loads(pattern.rules_json)
        except (json.JSONDecodeError, TypeError):
            continue

        conditions = rules.get("conditions", [])
        if not conditions:
            continue

        evaluable = [c for c in conditions if _condition_has_data(c, indicators)]
        total = len(conditions)
        n_eval = len(evaluable)
        if n_eval < max(1, total * min_eval_ratio):
            continue

        all_pass = True
        for cond in evaluable:
            if not _eval_condition(cond, indicators):
                all_pass = False
                break

        if all_pass:
            strengths = [
                round(compute_condition_strength(c, indicators), 3)
                for c in evaluable
            ]
            avg_strength = sum(strengths) / len(strengths) if strengths else 0.0
            match_quality = n_eval / total
            matches.append({
                "pattern_id": pattern.id,
                "name": pattern.name,
                "score_boost": pattern.score_boost,
                "min_base_score": pattern.min_base_score,
                "confidence": pattern.confidence,
                "win_rate": pattern.win_rate,
                "match_quality": round(match_quality, 2),
                "conditions_met": n_eval,
                "conditions_total": total,
                "avg_strength": round(avg_strength, 3),
                "strengths": strengths,
            })

    return matches


def build_indicator_snapshot(
    price: float,
    indicators: dict[str, Any],
    resistance: float,
    retest_info: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a flat indicator snapshot for pattern evaluation.

    Merges pre-computed indicator dict with price, resistance, retest info,
    and any extra fields into the format expected by ``evaluate_patterns``.
    """
    snap: dict[str, Any] = {"price": price}

    for k, v in indicators.items():
        snap[k] = v

    if resistance:
        snap["resistance"] = resistance
        if price:
            snap["dist_to_resistance_pct"] = round((resistance - price) / price * 100, 2)

    if retest_info:
        snap["resistance_retests"] = retest_info.get("retest_count", 0)
        snap["retest_range_tightening"] = retest_info.get("range_tightening", False)

    if extra:
        snap.update(extra)

    return snap


# ── CRUD ───────────────────────────────────────────────────────────────

def create_pattern(db: Session, data: dict[str, Any]) -> ScanPattern:
    """Create a new ScanPattern from a dict."""
    from ...services.backtest_service import infer_pattern_timeframe
    import json as _json

    rules_json = data.get("rules_json", "{}")
    conditions: list[dict] = []
    try:
        rules = _json.loads(rules_json) if rules_json else {}
        conditions = rules.get("conditions", [])
    except Exception:
        pass

    tf = data.get("timeframe") or infer_pattern_timeframe(
        conditions,
        name=data.get("name", ""),
        asset_class=data.get("asset_class", "all"),
        description=data.get("description", ""),
    )

    p = ScanPattern(
        name=data["name"],
        description=data.get("description", ""),
        rules_json=rules_json,
        origin=data.get("origin", "user"),
        asset_class=data.get("asset_class", "all"),
        timeframe=tf,
        score_boost=data.get("score_boost", 0.0),
        min_base_score=data.get("min_base_score", 0.0),
        confidence=data.get("confidence", 0.0),
        active=data.get("active", True),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def update_pattern(db: Session, pattern_id: int, data: dict[str, Any]) -> ScanPattern | None:
    p = db.query(ScanPattern).get(pattern_id)
    if not p:
        return None
    for key in ("name", "description", "rules_json", "active", "score_boost",
                "min_base_score", "confidence", "evidence_count", "win_rate",
                "avg_return_pct", "backtest_count", "asset_class", "timeframe",
                "promotion_status", "oos_win_rate", "oos_avg_return_pct", "oos_trade_count",
                "backtest_spread_used", "backtest_commission_used", "oos_evaluated_at",
                "bench_walk_forward_json", "hypothesis_family", "oos_validation_json",
                "queue_tier", "paper_book_json"):
        if key in data:
            setattr(p, key, data[key])
    db.commit()
    db.refresh(p)
    return p


def delete_pattern(db: Session, pattern_id: int) -> bool:
    p = db.query(ScanPattern).get(pattern_id)
    if not p:
        return False
    db.delete(p)
    db.commit()
    return True


def list_patterns(db: Session, active_only: bool = False) -> list[dict[str, Any]]:
    q = db.query(ScanPattern)
    if active_only:
        q = q.filter_by(active=True)
    patterns = q.order_by(ScanPattern.confidence.desc()).all()
    return [_pattern_to_dict(p) for p in patterns]


def _pattern_to_dict(p: ScanPattern) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "rules_json": p.rules_json,
        "origin": p.origin,
        "asset_class": p.asset_class,
        "confidence": p.confidence,
        "evidence_count": p.evidence_count,
        "win_rate": p.win_rate,
        "avg_return_pct": p.avg_return_pct,
        "backtest_count": p.backtest_count,
        "score_boost": p.score_boost,
        "min_base_score": p.min_base_score,
        "active": p.active,
        "promotion_status": getattr(p, "promotion_status", None) or "legacy",
        "oos_win_rate": getattr(p, "oos_win_rate", None),
        "oos_avg_return_pct": getattr(p, "oos_avg_return_pct", None),
        "oos_trade_count": getattr(p, "oos_trade_count", None),
        "backtest_spread_used": getattr(p, "backtest_spread_used", None),
        "backtest_commission_used": getattr(p, "backtest_commission_used", None),
        "oos_evaluated_at": p.oos_evaluated_at.isoformat() if getattr(p, "oos_evaluated_at", None) else None,
        "bench_walk_forward_json": getattr(p, "bench_walk_forward_json", None),
        "hypothesis_family": getattr(p, "hypothesis_family", None),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }
