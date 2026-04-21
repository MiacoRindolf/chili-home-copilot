"""Single source of truth for trading-subsystem structured log prefixes.

Every ``[foo]`` prefix used by a trading / trading-brain logger lives here,
so:

1. **Ops can grep for any one prefix** and land in a single file that
   documents its contract, callsites, and alerting significance.
2. **Refactors do not silently rename a prefix** — a typo during a rename
   would miss every callsite in one pass, breaking the log-based alerts
   in prod with no compile-time warning. Constants catch it.
3. **Phase 7's release-blocking grep stays stable.** The
   ``PREDICTION_OPS`` constant's string value is frozen by contract
   (CLAUDE.md Hard Rule 5); changing it would invalidate
   ``scripts/check_chili_prediction_ops_release_blocker.ps1``.

## Contract

Each prefix is a ``"[name]"`` string (square brackets included). Loggers
should emit ``f"{PREFIX} <fields>"`` — prefix first, then key=value pairs
separated by spaces.

## Adding a new prefix

1. Add a constant here with a docstring explaining the surface + fields.
2. Update ``docs/TRADING_SLO.md`` if the prefix implies an SLO.
3. Update ``docs/CONTRIBUTOR_SAFETY.md`` if the prefix is release-gated.
4. Use it at the callsite via ``from .ops_log_prefixes import <NAME>``.

## Do not

- Do not construct a prefix at runtime (e.g. ``f"[{dynamic}]"``). Observability
  alerts pattern-match on the literal string; dynamic construction hides it.
- Do not rename ``PREDICTION_OPS`` without a formal Phase 7+ change. The
  release-blocker script and the frozen authority contract both depend on
  the exact literal ``[chili_prediction_ops]``.
"""
from __future__ import annotations


# ── Prediction mirror (FROZEN, Phase 7) ────────────────────────────────
#
# PREDICTION_OPS is the single line emitted once per
# ``_get_current_predictions_impl`` when ``brain_prediction_ops_log_enabled``
# is True. Its string value is the grep pattern used by:
#   - scripts/check_chili_prediction_ops_release_blocker.ps1
#   - docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md validation step
# Any change to this literal is an authority-contract change and requires
# a new phase per CLAUDE.md Hard Rule 5.
PREDICTION_OPS = "[chili_prediction_ops]"


# ── Brain I/O + learning cycle ─────────────────────────────────────────
#
# Surfaces: app/services/trading/brain_io_concurrency.py, learning.py,
# learning_predictions.py. Emits concurrency-profile snapshots, snapshot
# batch progress, learning-cycle start/end markers.
CHILI_BRAIN_IO = "[chili_brain_io]"


# ── Market-data fetch layer (Phase B) ──────────────────────────────────
#
# Surface: app/services/trading/auto_trader.py (``_ohlcv_summary`` +
# ``_current_price``). Emits structured outcomes per attempt
# (source=ohlcv|quote, kind=ok|empty|timeout|transport|upstream|exhausted,
# ticker=..., attempt=..., err=...).  Exhausted lines escalate to WARNING.
CHILI_MARKET_DATA = "[chili_market_data]"


# ── Broker-equity TTL cache (Phase B) ──────────────────────────────────
#
# Surface: app/services/trading/auto_trader_rules.py
# (``resolve_effective_capital``). Kinds:
# hit_fresh / miss_refresh / miss_no_data / stale_serve / stale_expired /
# disabled. Gated behind chili_autotrader_broker_equity_cache_enabled.
CHILI_RISK_CACHE = "[chili_risk_cache]"


# ── Bracket reconciliation + watchdog ──────────────────────────────────
#
# Surface: bracket_reconciliation_service.py. BRACKET_RECONCILIATION
# covers the sweep loop and per-trade classification. BRACKET_WATCHDOG is
# the drift/latency guard emitted by the same service (distinct prefix so
# ops can alert on watchdog-only without bracket-sweep noise).
BRACKET_RECONCILIATION = "[bracket_reconciliation]"
BRACKET_WATCHDOG = "[bracket_watchdog]"


# ── Bracket writer ────────────────────────────────────────────────────
#
# Surface: bracket_writer_g2.py (resize_stop, place_missing_stop,
# cancel_order). The "_g2" suffix is historical (generation 2 after the
# original bracket writer); do not rename without an ADR.
BRACKET_WRITER_G2 = "[bracket_writer_g2]"


# ── Drift escalation + execution event lag ─────────────────────────────
#
# drift_escalation_watchdog.py:  emits on multi-hit drift breaches.
# execution_event_lag.py:         p50/p95 order→ack latency with ok/WARN/ERROR.
DRIFT_ESCALATION = "[drift_escalation]"
EXECUTION_EVENT_LAG = "[execution_event_lag]"


__all__ = [
    "PREDICTION_OPS",
    "CHILI_BRAIN_IO",
    "CHILI_MARKET_DATA",
    "CHILI_RISK_CACHE",
    "BRACKET_RECONCILIATION",
    "BRACKET_WATCHDOG",
    "BRACKET_WRITER_G2",
    "DRIFT_ESCALATION",
    "EXECUTION_EVENT_LAG",
]
