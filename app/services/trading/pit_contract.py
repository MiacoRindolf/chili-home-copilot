"""Phase C: canonical PIT (point-in-time) contract for mining DSL condition
fields.

Every field that appears in ``ScanPattern.rules_json[conditions][*].indicator``
(or as a ``ref`` pointer to another indicator) must be classified as one of:

* ``pit`` — provably computed from data known at or before the bar close
  identified by ``MarketSnapshot.bar_start_at``. Safe for mining.
* ``non_pit`` — known to encode future information or labels. Lookahead.
  MUST NOT appear in any mined rule.
* ``unknown`` — neither allowlisted nor denylisted. Treated as an audit
  violation because silent regressions (new features added to
  ``trading_snapshots.indicator_data`` without being declared here) can
  leak lookahead without being caught.

This module is pure. No DB, no network, no importing the learning cycle.
Extension policy: any new indicator field added to
``trading_snapshots.indicator_data`` (or referenced in a mined rule)
MUST be added here before it can land in production.

Docs: ``docs/TRADING_BRAIN_PIT_HYGIENE_ROLLOUT.md``.
"""

from __future__ import annotations

from typing import Iterable, Literal

# Allowed indicators — sourced by cross-referencing the miner
# (app/services/trading/learning.py) and the runtime condition evaluator
# (app/services/trading/pattern_engine._eval_condition). All of these are
# either:
#   - classical backward-looking indicators (RSI, MACD, ADX, Bollinger, ATR,
#     Stochastic, EMA stack, SMAs) computed from the bar series up to and
#     including the current bar,
#   - snapshot-time fundamentals / sentiment captured at the bar close,
#   - price / volume features derived only from historical bars.
#
# Notable inclusions that look future-tainted but aren't:
#   - ``news_sentiment`` / ``news_count`` are stored in MarketSnapshot at the
#     snapshot bar and NOT re-written with post-bar news.
#   - ``predicted_score`` is the model inference stored alongside the same
#     snapshot bar (see ``MarketSnapshot.predicted_score``). It is PIT-safe
#     because it depends only on features available at bar close; it is NOT
#     a label derived from future returns.
#   - ``regime`` is the SPY regime classification at the snapshot bar.
ALLOWED_INDICATORS: frozenset[str] = frozenset(
    {
        # Price / volume / derived
        "price",
        "close",
        "open",
        "high",
        "low",
        "volume",
        "volume_ratio",
        "vol_z_20",
        "realized_vol_20",
        # Moving averages
        "sma_10",
        "sma_20",
        "sma_50",
        "sma_100",
        "sma_200",
        "ema_5",
        "ema_9",
        "ema_10",
        "ema_12",
        "ema_20",
        "ema_26",
        "ema_50",
        "ema_100",
        "ema_200",
        "ema_stack",
        # Oscillators
        "rsi_7",
        "rsi_14",
        "rsi_21",
        "stochastic_k",
        "stochastic_d",
        "stoch_bull_div",
        "stoch_bear_div",
        # MACD
        "macd",
        "macd_signal",
        "macd_histogram",
        # Trend strength
        "adx",
        "plus_di",
        "minus_di",
        # Volatility bands
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "bb_pct",
        "bb_squeeze",
        "atr",
        "atr_14",
        # Fundamentals captured at snapshot bar
        "pe_ratio",
        "market_cap_b",
        # Sentiment captured at snapshot bar
        "news_sentiment",
        "news_count",
        # Regime / classifications at snapshot bar
        "regime",
        "vix_at_snapshot",
        "is_crypto",
        "asset_class",
        # Model inference at snapshot bar
        "predicted_score",
        # Pattern truth flags at snapshot bar
        "above_sma20",
        "above_sma50",
        "above_sma200",
        "resistance_retests",
        "breakout_strength",
        # Aliases / alternate spellings for miner + built-in patterns
        "macd_hist",
        "stoch_k",
        "stoch_d",
        # Built-in / community-seed indicators (app/services/trading/pattern_engine._BUILTIN_PATTERNS
        # and _COMMUNITY_SEED_PATTERNS). All PIT-safe conceptually — derived
        # from bar-close-and-earlier data.
        "vwap_reclaim",
        "rel_vol",
        "vol_ratio",
        "narrow_range",
        "vcp_count",
        "dist_to_resistance_pct",
        "dist_to_support_pct",
        "pullback_stretch_entry",
        "ibs",
        "fib_382_zone_hit",
        "fib_618_zone_hit",
        "fvg_fib_confluence",
        "fvg_present",
        "gap_up",
        "gap_down",
        "gap_pct",
        "daily_change_pct",
        "intraday_change_pct",
        "open_to_close_pct",
        "inside_bar",
        "outside_bar",
        "doji",
        "hammer",
        "engulfing",
    }
)

# Forbidden indicators — any field that encodes future information or a label.
# These MUST NOT appear in any mined condition. A non-empty match here is a
# release blocker.
FORBIDDEN_INDICATORS: frozenset[str] = frozenset(
    {
        # Future returns (labels)
        "future_return_1d",
        "future_return_3d",
        "future_return_5d",
        "future_return_10d",
        "future_return_20d",
        "forward_return_1d",
        "forward_return_3d",
        "forward_return_5d",
        "forward_return_10d",
        "forward_return_20d",
        "forward_return",
        "future_return",
        # Triple-barrier labels (Phase D will introduce these as LABELS)
        "tp_hit",
        "sl_hit",
        "timeout_hit",
        "triple_barrier_label",
        "barrier_touch_time",
        # Realized PnL outcomes (Phase A ledger-derived)
        "realized_pnl",
        "realized_pnl_pct",
        "post_event_return",
        "post_event_drawdown",
        "post_event_max_run",
        # Explicitly forecast / expected fields that peek ahead if naively
        # stored without provenance. If legitimate point-in-time versions
        # are introduced later, they must be added to ALLOWED with a
        # distinct name (e.g. ``predicted_score_at_bar``).
        "expected_pnl",
        "expected_return",
        "forecast_return",
    }
)

ClassifyResult = Literal["pit", "non_pit", "unknown"]


_CROSS_TF_PREFIXES: frozenset[str] = frozenset(
    {"1m", "5m", "15m", "30m", "1h", "60m", "90m", "4h", "1d", "1w", "1mo"}
)


def _strip_cross_tf_prefix(indicator: str) -> str:
    """Strip a miner-style ``<tf>:`` prefix if present (e.g. ``1d:rsi_14``).

    Cross-TF features inherit the PIT classification of their base name —
    an RSI on a higher timeframe computed at or before the current bar's
    close is still PIT-safe.
    """
    if ":" not in indicator:
        return indicator
    head, _, tail = indicator.partition(":")
    if head.lower() in _CROSS_TF_PREFIXES and tail:
        return tail
    return indicator


def classify(indicator: str | None) -> ClassifyResult:
    """Classify a single indicator name as pit / non_pit / unknown.

    Case-sensitive; indicator names in ``rules_json`` are expected to already
    be lowercase per the miner's convention. Cross-timeframe prefixes
    (``1d:rsi_14`` etc.) are stripped before lookup.
    """
    if not indicator:
        return "unknown"
    ind = _strip_cross_tf_prefix(str(indicator).strip())
    if ind in FORBIDDEN_INDICATORS:
        return "non_pit"
    if ind in ALLOWED_INDICATORS:
        return "pit"
    return "unknown"


def _extract_condition_fields(conditions: Iterable[dict]) -> list[str]:
    """Extract every indicator / ref field name from a conditions list.

    Accepts both miner-style (``indicator`` + optional ``ref``) and
    scanner-style (``field``) condition shapes. Skips falsy/non-string values.
    """
    fields: list[str] = []
    for c in conditions or []:
        if not isinstance(c, dict):
            continue
        ind = c.get("indicator") or c.get("field")
        if isinstance(ind, str) and ind:
            fields.append(ind)
        ref = c.get("ref")
        if isinstance(ref, str) and ref:
            fields.append(ref)
    return fields


def classify_rules(rules_json: dict | str | None) -> dict:
    """Classify every field used in a ``ScanPattern.rules_json`` payload.

    Accepts the dict shape ``{"conditions": [...]}`` or a JSON string of the
    same. Returns a dict with three disjoint lists (``pit``, ``non_pit``,
    ``unknown``) of unique field names in first-seen order.
    """
    import json as _json

    rules: dict | None
    if isinstance(rules_json, str):
        try:
            rules = _json.loads(rules_json)
        except (ValueError, TypeError):
            rules = None
    elif isinstance(rules_json, dict):
        rules = rules_json
    else:
        rules = None

    if not rules or not isinstance(rules, dict):
        return {"pit": [], "non_pit": [], "unknown": []}

    conditions = rules.get("conditions")
    if not isinstance(conditions, list):
        return {"pit": [], "non_pit": [], "unknown": []}

    fields = _extract_condition_fields(conditions)

    pit: list[str] = []
    non_pit: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()

    for f in fields:
        if f in seen:
            continue
        seen.add(f)
        klass = classify(f)
        if klass == "pit":
            pit.append(f)
        elif klass == "non_pit":
            non_pit.append(f)
        else:
            unknown.append(f)

    return {"pit": pit, "non_pit": non_pit, "unknown": unknown}
