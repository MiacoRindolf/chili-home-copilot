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

# ── Builtin intraday patterns (1m / 5m / 15m) ──────────────────────────
# Curated core for intraday timeframes so tf_variant/exit_variant spawning
# has parents to inherit from. Each entry sets ``timeframe`` and
# ``hypothesis_family`` explicitly — no inference, no NULL family.
# Indicators referenced should already exist in the indicator pipeline; any
# pattern that needs a not-yet-implemented detector references it via
# ``meta.detector`` (same convention as ``rsi_fib_fvg_pullback`` above) and
# evaluates to ``False`` until the detector is wired up.
_BUILTIN_INTRADAY_PATTERNS: list[dict[str, Any]] = [
    # ── 1m: scalping / micro-momentum ─────────────────────────────────
    {
        "name": "1m Opening Range Break + Volume",
        "description": (
            "Price breaks the high of the first N opening-range bars on the 1m "
            "with relative volume >= 2.0. Classic intraday breakout entry."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "1m",
        "hypothesis_family": "opening_range",
        "score_boost": 1.5,
        "min_base_score": 3.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "opening_range_break", "op": "==", "value": True},
                {"indicator": "rel_vol", "op": ">=", "value": 2.0},
            ],
            "meta": {
                "type": "opening_range_break",
                "side": "bullish",
                "or_window_minutes": 15,
                "detector": "opening_range_break_1m",
                "requires_intraday_session": True,
            },
        }),
    },
    {
        "name": "1m VWAP Reclaim",
        "description": (
            "Price closes back above session VWAP after trading below it, "
            "with rel_vol >= 1.5 — fast mean-reversion reclaim."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "1m",
        "hypothesis_family": "mean_reversion",
        "score_boost": 1.0,
        "min_base_score": 3.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "vwap_reclaim", "op": "==", "value": True},
                {"indicator": "rel_vol", "op": ">=", "value": 1.5},
            ],
            "meta": {"type": "vwap_reclaim", "side": "bullish"},
        }),
    },
    {
        "name": "1m Tape-Speed Burst",
        "description": (
            "Volume burst (rel_vol >= 3) with RSI rising and MACD histogram "
            "expanding — momentum ignition on the tape."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "1m",
        "hypothesis_family": "momentum_continuation",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "rel_vol", "op": ">=", "value": 3.0},
                {"indicator": "rsi_14", "op": ">", "value": 55},
                {"indicator": "macd_hist", "op": ">", "value": 0},
            ],
            "meta": {"type": "tape_speed_burst", "side": "bullish"},
        }),
    },
    {
        "name": "1m Liquidity Sweep + Reverse",
        "description": (
            "Wick pierces prior swing low/high then closes back inside the range "
            "— stop-run reversal pattern."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "1m",
        "hypothesis_family": "liquidity_sweep",
        "score_boost": 1.5,
        "min_base_score": 3.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "liquidity_sweep_reverse", "op": "==", "value": True},
            ],
            "meta": {
                "type": "liquidity_sweep",
                "side": "bullish",
                "swing_lookback": 20,
                "detector": "liquidity_sweep_reverse_1m",
            },
        }),
    },
    {
        "name": "1m Stoch Bull Cross + RSI > 50",
        "description": (
            "Stochastic %K crosses above %D from oversold while RSI > 50 — "
            "fast trend-with-pullback re-entry."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "1m",
        "hypothesis_family": "momentum_continuation",
        "score_boost": 1.0,
        "min_base_score": 2.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "stoch_bull_cross", "op": "==", "value": True},
                {"indicator": "rsi_14", "op": ">", "value": 50},
            ],
            "meta": {"type": "stoch_pullback_entry", "side": "bullish"},
        }),
    },

    # ── 5m: intraday momentum / structure ─────────────────────────────
    {
        "name": "5m Opening Drive",
        "description": (
            "First three 5m bars trend in one direction (HH/HL or LH/LL) with "
            "ADX >= 20 — opening-drive momentum continuation."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "5m",
        "hypothesis_family": "opening_range",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "opening_drive", "op": "==", "value": True},
                {"indicator": "adx", "op": ">=", "value": 20},
            ],
            "meta": {
                "type": "opening_drive",
                "side": "bullish",
                "drive_bars": 3,
                "detector": "opening_drive_5m",
                "requires_intraday_session": True,
            },
        }),
    },
    {
        "name": "5m First Pullback to VWAP",
        "description": (
            "After an opening drive, first pullback that holds VWAP with "
            "RSI > 45 — high-probability continuation entry."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "5m",
        "hypothesis_family": "momentum_continuation",
        "score_boost": 2.0,
        "min_base_score": 4.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "vwap_pullback_hold", "op": "==", "value": True},
                {"indicator": "rsi_14", "op": ">", "value": 45},
                {"indicator": "price", "op": ">", "ref": "vwap"},
            ],
            "meta": {
                "type": "vwap_pullback_continuation",
                "side": "bullish",
                "detector": "vwap_pullback_hold_5m",
            },
        }),
    },
    {
        "name": "5m EMA9/EMA21 Bull Cross + Volume",
        "description": (
            "Fast EMA9 crosses above EMA21 with volume expansion (rel_vol >= 1.5) "
            "and price > VWAP — intraday trend-flip entry."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "5m",
        "hypothesis_family": "momentum_continuation",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "ema_9", "op": ">", "ref": "ema_21"},
                {"indicator": "rel_vol", "op": ">=", "value": 1.5},
                {"indicator": "price", "op": ">", "ref": "vwap"},
            ],
            "meta": {"type": "ema_cross_intraday", "side": "bullish"},
        }),
    },
    {
        "name": "5m NR4 Coil Breakout",
        "description": (
            "Narrow Range 4 (smallest range of last 4 bars) followed by a "
            "breakout bar with volume — coil release."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "5m",
        "hypothesis_family": "compression_expansion",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "narrow_range", "op": "any_of", "value": ["NR4", "NR7"]},
                {"indicator": "rel_vol", "op": ">=", "value": 1.5},
            ],
            "meta": {"type": "nr_coil_break", "side": "bullish"},
        }),
    },
    {
        "name": "5m HOD Reclaim with Volume",
        "description": (
            "Price reclaims the session high-of-day after a pullback, with "
            "rel_vol >= 1.5 — momentum continuation through prior resistance."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "5m",
        "hypothesis_family": "momentum_continuation",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "hod_reclaim", "op": "==", "value": True},
                {"indicator": "rel_vol", "op": ">=", "value": 1.5},
            ],
            "meta": {
                "type": "hod_reclaim",
                "side": "bullish",
                "detector": "hod_reclaim_5m",
                "requires_intraday_session": True,
            },
        }),
    },
    {
        "name": "5m Failed Breakdown Reversal",
        "description": (
            "Session low broken intraday but price closes back above the prior "
            "low within 2 bars — failed-breakdown reversal."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "5m",
        "hypothesis_family": "mean_reversion",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "failed_breakdown", "op": "==", "value": True},
                {"indicator": "rsi_14", "op": ">", "value": 35},
            ],
            "meta": {
                "type": "failed_breakdown_reversal",
                "side": "bullish",
                "detector": "failed_breakdown_5m",
            },
        }),
    },

    # ── 15m: intraday swing ───────────────────────────────────────────
    {
        "name": "15m Inside Bar Breakout",
        "description": (
            "Inside bar (range fully within prior bar) followed by a breakout "
            "of the mother bar's high — classic compression release."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "15m",
        "hypothesis_family": "compression_expansion",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "inside_bar_break", "op": "==", "value": True},
                {"indicator": "rel_vol", "op": ">=", "value": 1.2},
            ],
            "meta": {
                "type": "inside_bar_break",
                "side": "bullish",
                "detector": "inside_bar_break_15m",
            },
        }),
    },
    {
        "name": "15m BB Squeeze + ADX < 20",
        "description": (
            "Intraday Bollinger Band squeeze with ADX < 20 (low trend strength) "
            "and RSI in the 40-65 neutral zone — pre-expansion coil."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "15m",
        "hypothesis_family": "compression_expansion",
        "score_boost": 2.0,
        "min_base_score": 4.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "bb_squeeze", "op": "==", "value": True},
                {"indicator": "adx", "op": "<", "value": 20},
                {"indicator": "rsi_14", "op": "between", "value": [40, 65]},
            ],
            "meta": {"type": "bb_squeeze_intraday", "side": "neutral"},
        }),
    },
    {
        "name": "15m MACD Histogram Flip + RSI > 50",
        "description": (
            "MACD histogram flips from negative to positive while RSI > 50 — "
            "momentum-shift entry on the 15m."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "15m",
        "hypothesis_family": "momentum_continuation",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "macd_hist_flip_positive", "op": "==", "value": True},
                {"indicator": "rsi_14", "op": ">", "value": 50},
            ],
            "meta": {
                "type": "macd_flip_continuation",
                "side": "bullish",
                "detector": "macd_hist_flip_positive",
            },
        }),
    },
    {
        "name": "15m EMA Stack Reclaim",
        "description": (
            "Price reclaims the bullish EMA stack (above 9/21/50) after a "
            "pullback, with RSI > 50 — trend-continuation entry."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "15m",
        "hypothesis_family": "momentum_continuation",
        "score_boost": 2.0,
        "min_base_score": 4.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "price", "op": ">", "ref": "ema_9"},
                {"indicator": "price", "op": ">", "ref": "ema_21"},
                {"indicator": "price", "op": ">", "ref": "ema_50"},
                {"indicator": "ema_9", "op": ">", "ref": "ema_21"},
                {"indicator": "rsi_14", "op": ">", "value": 50},
            ],
            "meta": {"type": "ema_stack_reclaim", "side": "bullish"},
        }),
    },
    {
        "name": "15m Higher-Low Trend Continuation",
        "description": (
            "Confirmed higher-low structure with RSI > 55 and price above EMA21 "
            "— intraday trend-continuation buy."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "15m",
        "hypothesis_family": "momentum_continuation",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "higher_low_confirmed", "op": "==", "value": True},
                {"indicator": "rsi_14", "op": ">", "value": 55},
                {"indicator": "price", "op": ">", "ref": "ema_21"},
            ],
            "meta": {
                "type": "higher_low_continuation",
                "side": "bullish",
                "detector": "higher_low_confirmed_15m",
            },
        }),
    },
    {
        "name": "15m Failed Breakdown Reversal",
        "description": (
            "Prior swing low broken on the 15m but price closes back above it "
            "within 2 bars and RSI > 35 — failed-breakdown reversal."
        ),
        "origin": "builtin",
        "asset_class": "all",
        "timeframe": "15m",
        "hypothesis_family": "mean_reversion",
        "score_boost": 1.5,
        "min_base_score": 3.5,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "failed_breakdown", "op": "==", "value": True},
                {"indicator": "rsi_14", "op": ">", "value": 35},
            ],
            "meta": {
                "type": "failed_breakdown_reversal",
                "side": "bullish",
                "detector": "failed_breakdown_15m",
            },
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
    {
        "name": "RSI + Fib 0.382 + FVG Pullback (Tagalog seed)",
        "description": (
            "Community seed: bullish pullback continuation. "
            "HTF RSI must have exceeded 75 (strong trend), LTF RSI > 50 "
            "(still supportive). Price retraces to Fibonacci 0.382 level of "
            "the impulse leg. A Fair Value Gap must be present in confluence "
            "with the Fib zone. Inspired by Filipino trader pullback criteria."
        ),
        "origin": "user_seeded",
        "asset_class": "all",
        "timeframe": "1h",
        "score_boost": 2.0,
        "min_base_score": 4.0,
        "rules_json": json.dumps({
            "conditions": [
                {"indicator": "1d:rsi_14", "op": ">", "value": 75},
                {"indicator": "rsi_14", "op": ">", "value": 50},
                {"indicator": "fib_382_zone_hit", "op": "==", "value": True},
                {"indicator": "fvg_fib_confluence", "op": "==", "value": True},
            ],
            "meta": {
                "type": "pullback_continuation",
                "side": "bullish",
                "htf": "1d",
                "ltf": "1h",
                "fib_target": 0.382,
                "fib_tolerance_pct": 0.5,
                "fvg_fib_overlap_tolerance_pct": 0.5,
                "requires_cross_tf": True,
                "detector": "rsi_fib_fvg_pullback",
            },
        }),
    },
]


def seed_builtin_patterns(db: Session) -> int:
    """Insert builtin patterns if they don't already exist. Returns count added.

    Covers daily/multi-tf patterns from ``_BUILTIN_PATTERNS`` plus intraday
    (1m/5m/15m) patterns from ``_BUILTIN_INTRADAY_PATTERNS``. Both pass through
    ``hypothesis_family`` so the family taxonomy is populated from day one.
    """
    existing = {p.name for p in db.query(ScanPattern).filter_by(origin="builtin").all()}
    added = 0
    from ...services.backtest_service import infer_pattern_timeframe
    import json as _json2

    for bp in (*_BUILTIN_PATTERNS, *_BUILTIN_INTRADAY_PATTERNS):
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
            hypothesis_family=bp.get("hypothesis_family"),
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
            hypothesis_family=bp.get("hypothesis_family"),
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
    current_regime: str | None = None,
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

    When *current_regime* is provided (e.g. ``"risk_on"``), each matched
    pattern's ``score_boost`` is modulated by its regime affinity data:
    patterns that perform well in the current regime get a boost, while
    those that underperform are suppressed.

    Returns a list of matched patterns with their score boosts.
    """
    from .regime import inject_regime_into_indicators
    indicators = inject_regime_into_indicators(indicators)

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
            boost = pattern.score_boost
            if current_regime:
                boost = regime_adjusted_score_boost(pattern, current_regime)
            matches.append({
                "pattern_id": pattern.id,
                "name": pattern.name,
                "score_boost": boost,
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
        raw_c = rules.get("conditions", [])
        if isinstance(raw_c, dict):
            raw_c = [raw_c]
        if isinstance(raw_c, list):
            conditions = [c for c in raw_c if isinstance(c, dict) and c]
    except Exception:
        conditions = []
    if not conditions and (data.get("description") or data.get("name")):
        from ...services.trading.backtest_engine import _parse_conditions_from_description

        blob = f"{data.get('name') or ''} | {data.get('description') or ''}"
        parsed = _parse_conditions_from_description(blob)
        if parsed:
            conditions = parsed
            rules_json = _json.dumps({"conditions": conditions})

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
    old_promo = (p.promotion_status or "").strip()
    old_lc = (p.lifecycle_stage or "").strip()
    surface_keys = ("promotion_status", "lifecycle_stage")
    touch_surface = any(k in data for k in surface_keys)
    for key in ("name", "description", "rules_json", "active", "score_boost",
                "min_base_score", "confidence", "evidence_count", "win_rate",
                "avg_return_pct", "backtest_count", "asset_class", "timeframe",
                "promotion_status", "lifecycle_stage", "oos_win_rate", "oos_avg_return_pct",
                "oos_trade_count", "backtest_spread_used", "backtest_commission_used",
                "oos_evaluated_at", "bench_walk_forward_json", "hypothesis_family",
                "oos_validation_json", "queue_tier", "paper_book_json"):
        if key in data:
            setattr(p, key, data[key])
    db.flush()
    if touch_surface:
        from .brain_work.promotion_surface import emit_promotion_surface_change

        emit_promotion_surface_change(
            db,
            scan_pattern_id=int(pattern_id),
            old_promotion_status=old_promo,
            old_lifecycle_stage=old_lc,
            new_promotion_status=(p.promotion_status or "").strip(),
            new_lifecycle_stage=(p.lifecycle_stage or "").strip(),
            source="update_pattern",
        )
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
        "timeframe": p.timeframe,
        "confidence": p.confidence,
        "evidence_count": p.evidence_count,
        "win_rate": p.win_rate,
        "avg_return_pct": p.avg_return_pct,
        "backtest_count": p.backtest_count,
        "score_boost": p.score_boost,
        "min_base_score": p.min_base_score,
        "active": p.active,
        "parent_id": getattr(p, "parent_id", None),
        "generation": getattr(p, "generation", 0),
        "ticker_scope": getattr(p, "ticker_scope", "universal"),
        "lifecycle_stage": getattr(p, "lifecycle_stage", None) or "candidate",
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
        "regime_affinity_json": getattr(p, "regime_affinity_json", None),
    }


# ── Regime-aware score modulation ─────────────────────────────────────

def regime_adjusted_score_boost(
    pattern: ScanPattern,
    current_regime: str,
    *,
    boost_factor: float = 1.3,
    suppress_factor: float = 0.4,
    min_samples: int = 10,
) -> float:
    """Return a regime-modulated ``score_boost`` for *pattern*.

    If the pattern has regime affinity data and the current regime has
    enough samples:
    - If win rate in current regime >= 55%: multiply score_boost by *boost_factor*
    - If win rate in current regime < 45%: multiply score_boost by *suppress_factor*
    - Otherwise: return unmodified score_boost

    Falls back to the pattern's base ``score_boost`` if no affinity data
    is available.
    """
    base = pattern.score_boost or 0.0
    affinity = getattr(pattern, "regime_affinity_json", None)
    if not affinity or not isinstance(affinity, dict):
        return base

    regime_data = affinity.get(current_regime)
    if not regime_data or not isinstance(regime_data, dict):
        return base

    n = regime_data.get("n", 0)
    if n < min_samples:
        return base

    wr = regime_data.get("win_rate", 0.5)
    if wr >= 0.55:
        return round(base * boost_factor, 4)
    elif wr < 0.45:
        return round(base * suppress_factor, 4)
    return base
