"""Non-LLM pattern hypothesis proposals from snapshots + insight text.

Used by ``discover_pattern_hypotheses`` to avoid premium LLM calls while staying
data-driven (lift vs baseline on ``future_return_5d``).
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

# Candidate rules: human label + conditions (DSL aligned with pattern_engine).
_STAT_CANDIDATES: list[tuple[str, str, list[dict[str, Any]]]] = [
    (
        "Oversold RSI MACD Histogram Positive",
        "Snapshots where RSI is oversold and MACD histogram has turned positive "
        "(mean-reversion bounce setup).",
        [
            {"indicator": "rsi_14", "op": "<", "value": 36},
            {"indicator": "macd_hist", "op": ">", "value": 0},
        ],
    ),
    (
        "Tight BB RSI Moderate ADX Trend",
        "Bollinger %B compressed with RSI not extreme and ADX showing trend interest.",
        [
            {"indicator": "bb_pct_b", "op": "<", "value": 0.25},
            {"indicator": "rsi_14", "op": "<", "value": 55},
            {"indicator": "adx", "op": ">", "value": 18},
        ],
    ),
    (
        "Stochastic Oversold MACD Confirm",
        "Slow stochastic oversold with positive MACD histogram.",
        [
            {"indicator": "stoch_k", "op": "<", "value": 22},
            {"indicator": "macd_hist", "op": ">", "value": 0},
        ],
    ),
    (
        "RSI Moderate Pullback ADX Strong",
        "Pullback in an established trend: RSI mid-range, ADX elevated.",
        [
            {"indicator": "rsi_14", "op": "<", "value": 48},
            {"indicator": "rsi_14", "op": ">", "value": 28},
            {"indicator": "adx", "op": ">", "value": 22},
        ],
    ),
    (
        "Deep BB Lower RSI Capitulation",
        "Price near lower Bollinger band with very low RSI.",
        [
            {"indicator": "bb_pct_b", "op": "<", "value": 0.15},
            {"indicator": "rsi_14", "op": "<", "value": 32},
        ],
    ),
]

# Insight substring -> one condition (keyword scan, case-insensitive).
_INSIGHT_KEYWORD_CONDITIONS: list[tuple[re.Pattern, dict[str, Any]]] = [
    (re.compile(r"\brsi\b.*(?:oversold|<\s*35|under\s*35)", re.I), {"indicator": "rsi_14", "op": "<", "value": 35}),
    (re.compile(r"oversold", re.I), {"indicator": "rsi_14", "op": "<", "value": 36}),
    (re.compile(r"\bmacd\b.*(?:positive|cross|histogram)", re.I), {"indicator": "macd_hist", "op": ">", "value": 0}),
    (re.compile(r"\badx\b.*(?:>\s*20|strong|trend)", re.I), {"indicator": "adx", "op": ">", "value": 20}),
    (re.compile(r"bollinger|bb\s*%|percent\s*b", re.I), {"indicator": "bb_pct_b", "op": "<", "value": 0.2}),
    (re.compile(r"stochastic|stoch\b", re.I), {"indicator": "stoch_k", "op": "<", "value": 25}),
    (re.compile(r"volume.*(?:spike|surge|>\s*2)", re.I), {"indicator": "rel_vol", "op": ">", "value": 2.0}),
    (re.compile(r"vwap|reclaim", re.I), {"indicator": "vwap_reclaim", "op": "==", "value": True}),
]


def _flat_matches_condition(flat: dict[str, Any], cond: dict[str, Any]) -> bool:
    ind = cond.get("indicator")
    op = (cond.get("op") or ">").strip()
    val = cond.get("value")
    if not ind or ind not in flat:
        return False
    cur = flat[ind]
    if cur is None:
        return False
    try:
        if isinstance(val, bool):
            return bool(cur) == val
        c = float(cur)
        v = float(val)
    except (TypeError, ValueError):
        return False
    if op == "<":
        return c < v
    if op == "<=":
        return c <= v
    if op == ">":
        return c > v
    if op == ">=":
        return c >= v
    if op == "==":
        return abs(c - v) < 1e-9
    if op == "!=":
        return abs(c - v) >= 1e-9
    return False


def _flat_matches_all(flat: dict[str, Any], conditions: list[dict[str, Any]]) -> bool:
    return all(_flat_matches_condition(flat, c) for c in conditions)


def _conditions_fingerprint(conditions: list[dict[str, Any]]) -> str:
    return json.dumps(conditions, sort_keys=True, default=str)


def mine_proposals_from_snapshots(
    db: Session,
    *,
    max_proposals: int = 5,
    min_samples: int = 22,
    min_lift_pct: float = 0.03,
    snapshot_limit: int = 900,
) -> list[dict[str, Any]]:
    """Rank pre-defined condition sets by mean 5d forward return lift vs baseline."""
    from ...models.trading import MarketSnapshot
    from .learning_predictions import _indicator_data_to_flat_snapshot

    rows = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.future_return_5d.isnot(None))
        .filter(MarketSnapshot.indicator_data.isnot(None))
        .order_by(MarketSnapshot.id.desc())
        .limit(snapshot_limit)
        .all()
    )
    if len(rows) < min_samples:
        return []

    parsed: list[tuple[dict[str, Any], float]] = []
    for s in rows:
        try:
            fr = float(s.future_return_5d or 0)
        except (TypeError, ValueError):
            continue
        ind = s.indicator_data
        if isinstance(ind, str):
            try:
                ind = json.loads(ind)
            except json.JSONDecodeError:
                continue
        if not isinstance(ind, dict):
            continue
        flat = _indicator_data_to_flat_snapshot(ind, float(s.close_price or 0) or None)
        parsed.append((flat, fr))

    if len(parsed) < min_samples:
        return []

    baseline = sum(fr for _, fr in parsed) / len(parsed)
    scored: list[tuple[float, int, str, str, list[dict[str, Any]]]] = []

    for title, desc, conds in _STAT_CANDIDATES:
        matched = [fr for flat, fr in parsed if _flat_matches_all(flat, conds)]
        n = len(matched)
        if n < min_samples:
            continue
        mean_ret = sum(matched) / n
        lift = mean_ret - baseline
        if mean_ret > 0 and lift >= min_lift_pct:
            scored.append((lift, n, title, desc, conds))

    scored.sort(key=lambda x: -x[0])
    out: list[dict[str, Any]] = []
    seen_fp: set[str] = set()
    for lift, n, title, desc, conds in scored:
        fp = _conditions_fingerprint(conds)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        matched = [fr for flat, fr in parsed if _flat_matches_all(flat, conds)]
        mean_ret = sum(matched) / max(n, 1)
        safe = re.sub(r"[^a-zA-Z0-9]+", "_", title)[:40].strip("_")
        name = f"Stat_{safe}_{int(lift * 100)}"
        out.append(
            {
                "name": name[:120],
                "description": (
                    f"{desc} (n={n}, mean 5d ret={mean_ret:.3f}%, lift vs baseline={lift:.3f}%)."
                ),
                "conditions": conds,
                "score_boost": min(1.8, 1.15 + min(0.5, lift / 2.0)),
                "min_base_score": 4.0,
            }
        )
        if len(out) >= max_proposals:
            break

    return out


def mine_proposals_from_insights(
    insights: list[Any],
    *,
    existing_condition_fps: set[str],
    max_proposals: int = 2,
) -> list[dict[str, Any]]:
    """Build 2-condition patterns from keyword hits in insight text (no LLM)."""
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set(existing_condition_fps)

    for ins in insights:
        text = (getattr(ins, "pattern_description", None) or "")[:2000]
        if len(text) < 12:
            continue
        conds: list[dict[str, Any]] = []
        seen_inds: set[str] = set()
        for rx, c in _INSIGHT_KEYWORD_CONDITIONS:
            if rx.search(text) and c["indicator"] not in seen_inds:
                conds.append(dict(c))
                seen_inds.add(c["indicator"])
            if len(conds) >= 4:
                break
        if len(conds) < 2:
            continue
        pair = conds[:2]
        fp = _conditions_fingerprint(pair)
        if fp in seen:
            continue
        seen.add(fp)
        slug = "_".join(str(pair[i]["indicator"]) for i in range(2))[:50]
        proposals.append(
            {
                "name": f"Insight_{slug}_{getattr(ins, 'id', 0)}",
                "description": f"From insight (conf={getattr(ins, 'confidence', 0):.2f}): {text[:240]}…",
                "conditions": pair,
                "score_boost": 1.35,
                "min_base_score": 4.0,
            }
        )
        if len(proposals) >= max_proposals:
            break

    return proposals
