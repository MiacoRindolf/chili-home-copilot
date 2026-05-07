"""Fast-path execution gates (F4).

A gate is a pure function that takes ``(alert, context)`` and returns
``GateResult(allow=bool, reason=str, detail=dict)``. The executor runs
all gates and ANDs their decisions: ANY gate denial blocks the trade.

Gates are intentionally pure-Python and side-effect-free so they can be
unit-tested in isolation and so the order in which the executor calls
them doesn't affect outcomes. They never touch the database directly —
the executor passes whatever state they need via ``context`` (open
positions count, session notional spent, etc.).

The mode interlock (paper vs live) is a gate too, not a special case:
this lets the live path require BOTH ``CHILI_FAST_PATH_MODE=live`` AND
the explicit authorization flag. If either is missing, the gate
forces ``mode=paper`` for the decision; the executor then skips any
broker call. There is intentionally no override path that bypasses the
interlock — the gate's deny-reason gets written to fast_executions so
operators can see why a live decision was downgraded to paper.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


# Default thresholds — tuned conservatively. Override via env where
# noted; constants stay here so the gate semantics are visible.
ALERT_RECENCY_MAX_AGE_S = 60.0
"""An alert older than this is stale; reject. Snapshot-replay alerts
on container restart can be hours old, so this gate is the primary
guard against acting on backfill."""

MIN_SIGNAL_SCORE = 0.30
"""Below this, the alert isn't worth the round-trip. Default is set
LOW (0.30) for paper-mode validation — we want to see fills happening
so the autopilot view has something to display. Operators should
tighten this to ~0.50+ before flipping to live; override via
CHILI_FAST_PATH_EXEC_MIN_SCORE."""

MAX_SPREAD_BPS = 8.0
"""Don't enter into a wide market — slippage on a market order would
eat the edge. Wider than 8 bps suggests a thin book or news event."""

DEFAULT_NOTIONAL_USD = 25.0
"""Per-trade notional in USD. Deliberately tiny for paper validation;
F7 will replace this with Kelly-fraction sizing."""

DAILY_NOTIONAL_BUDGET_USD = 500.0
"""Cumulative notional traded today. When exceeded, reject regardless
of signal quality. Override via CHILI_FAST_PATH_EXEC_DAILY_USD."""

MAX_OPEN_POSITIONS_PER_PAIR = 1
"""No pyramiding in the fast lane — one entry per ticker at a time."""


# ── Result + Context types ───────────────────────────────────────────


@dataclass
class GateResult:
    """Outcome of one gate. ``allow=False`` means this gate denies the
    trade; the executor will reject the alert citing ``reason``.

    ``detail`` is freeform (dict) for the postmortem JSONB column.
    """
    name: str
    allow: bool
    reason: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class ExecContext:
    """All the state a gate might need, gathered by the executor before
    invoking gates so each gate stays pure.

    The executor populates this from the in-memory book (best bid/ask),
    the in-memory open-position counter (paper) or a broker query
    (live), and the running daily-notional accumulator.
    """
    now_wall: datetime
    """Decision time, naive UTC (matches DB convention)."""

    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_bps: float = 0.0
    """Top-of-book at decision time, taken from the in-memory book."""

    open_positions_for_ticker: int = 0
    daily_notional_used_usd: float = 0.0

    mode: str = "paper"
    """``paper`` or ``live`` — what the supervisor was configured with."""

    live_authorized: bool = False
    """Operator has set CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED=1.
    If False, the mode_interlock gate downgrades any live decision."""

    engine: Any = None
    """Optional read-only SQLAlchemy engine for calibration lookup.
    F6.5: gate_calibrated_tradeability uses it to query
    fast_signal_decay. Pure-gate semantics preserved -- the engine
    is opaque to the gate, used only for SELECT statements via the
    ``app.services.trading.fast_path.calibration`` helpers. Optional
    so cold-start / unit tests still work without DB."""


# ── Individual gates ─────────────────────────────────────────────────


def gate_recency(alert: dict, ctx: ExecContext,
                 *, max_age_s: float = ALERT_RECENCY_MAX_AGE_S) -> GateResult:
    """Reject alerts whose ``fired_at`` is older than max_age_s.

    Important: protects against snapshot-replay alerts. ws_client logs
    historical bars on subscribe; the scanner sees them as bar_close
    events; their fired_at can be hours old. Without this gate the
    executor would happily act on yesterday's signal.
    """
    fired_at = alert.get("fired_at")
    if not isinstance(fired_at, datetime):
        return GateResult("recency", False, "fired_at_missing_or_invalid",
                          {"raw": str(fired_at)})
    age = (ctx.now_wall - fired_at).total_seconds()
    if age > max_age_s:
        return GateResult("recency", False, "alert_too_old",
                          {"age_s": float(age), "max_age_s": float(max_age_s)})
    if age < -5.0:
        # Wall-clock skew larger than 5s is suspicious; reject rather
        # than silently allowing a future-dated alert.
        return GateResult("recency", False, "alert_in_future_skew",
                          {"age_s": float(age)})
    return GateResult("recency", True, "", {"age_s": float(age)})


def gate_min_score(alert: dict, ctx: ExecContext,
                   *, min_score: float = MIN_SIGNAL_SCORE) -> GateResult:
    score = float(alert.get("signal_score") or 0.0)
    if score < min_score:
        return GateResult("min_score", False, "score_below_threshold",
                          {"score": float(score), "min": float(min_score)})
    return GateResult("min_score", True, "", {"score": float(score)})


def gate_calibrated_tradeability(alert: dict, ctx: ExecContext) -> GateResult:
    """F6.5: empirical-edge gate.

    Consults ``fast_signal_decay`` via
    ``calibration.is_score_tradeable`` to deny signals whose score
    bucket has historically NOT cleared the trading-cost bar
    (mean_return_at_best_horizon > TRADEABLE_COST_MULT × cost).

    Three outcomes:
      - calibrated tradeable (True)  -> allow with verdict='tradeable'
      - calibrated not-tradeable     -> deny with reason='not_tradeable'
      - insufficient data (None)     -> allow with verdict='no_data',
        leaving gate_min_score's static threshold to gate the trade

    Engine missing from ctx (e.g. unit-test path): allows
    unconditionally with verdict='no_engine'. Keeps gate semantics
    backwards-compatible with existing tests.
    """
    if ctx.engine is None:
        return GateResult("calibration", True, "", {"verdict": "no_engine"})
    try:
        from .calibration import is_score_tradeable
        verdict = is_score_tradeable(
            ctx.engine,
            ticker=str(alert.get("ticker") or ""),
            alert_type=str(alert.get("alert_type") or ""),
            signal_score=float(alert.get("signal_score") or 0.0),
        )
    except Exception as exc:
        # Calibration failures should NOT block trades -- fall through
        # to gate_min_score's static check.
        return GateResult(
            "calibration", True, "",
            {"verdict": "lookup_failed", "error": str(exc)[:120]},
        )
    if verdict is False:
        return GateResult(
            "calibration", False, "signal_not_tradeable",
            {"verdict": "false",
             "reason": "calibrated_mean_below_tradeable_threshold"},
        )
    if verdict is True:
        return GateResult(
            "calibration", True, "",
            {"verdict": "true"},
        )
    # None: insufficient data
    return GateResult(
        "calibration", True, "",
        {"verdict": "insufficient_data"},
    )


def gate_negative_edge_excluded(alert: dict, ctx: ExecContext) -> GateResult:
    """F6.5: statistically-significant negative edge auto-block.

    Distinct from gate_calibrated_tradeability (which fails signals
    that don't beat the trading-cost bar). This gate fails signals
    whose *upper* 95% CI on mean_return is below zero -- i.e., we're
    confident the true expected return is negative. Such signals are
    not just "uneconomic to trade", they're "expected to lose money
    even if free to trade."

    Three outcomes:
      - statistically negative (n>=30, mean+2*stderr<0) -> deny
        with reason='negative_edge', evidence in detail
      - non-negative or insufficient data -> allow (other gates
        still apply)
      - engine missing or lookup failure -> allow (preserves
        backwards compatibility with unit tests)

    Order in DEFAULT_GATES: AFTER gate_calibrated_tradeability
    (so a signal that fails cost-bar AND is negative reports the
    cost-bar reason; we keep the more specific/actionable
    rejection visible at the top of the diagnosis chain) but
    BEFORE gate_capacity (capacity is per-pair state; this is
    per-signal state).
    """
    if ctx.engine is None:
        return GateResult("negative_edge", True, "",
                          {"verdict": "no_engine"})
    try:
        from .calibration import is_negative_edge_excluded
        excluded, evidence = is_negative_edge_excluded(
            ctx.engine,
            ticker=str(alert.get("ticker") or ""),
            alert_type=str(alert.get("alert_type") or ""),
            signal_score=float(alert.get("signal_score") or 0.0),
        )
    except Exception as exc:
        return GateResult(
            "negative_edge", True, "",
            {"verdict": "lookup_failed", "error": str(exc)[:120]},
        )
    if excluded:
        return GateResult(
            "negative_edge", False, "negative_edge",
            evidence,
        )
    return GateResult("negative_edge", True, "", evidence)


def gate_cost_aware_admission(alert: dict, ctx: ExecContext) -> GateResult:
    """f-fastpath-universe-rotation Step 5 (2026-05-07): cost-aware
    admission gate.

    Rejects any signal whose calibrated best-Sharpe-horizon
    ``mean_return`` does not clear ``2 * (taker_fee_bps +
    spread_bps_at_decision_time)``. This is the **right** form of the
    economic-line check — the F6.5 ``gate_calibrated_tradeability``
    uses a static cost multiplier; this gate uses the live spread as
    measured at the decision moment, which is what the round-trip
    actually pays.

    No-op when ``settings.cost_aware_admission_enabled`` is False (the
    default). Off-by-default keeps switchover bit-identical to current.

    The factor of 2 accounts for the round-trip: pay the fee + cross
    the spread on entry AND on exit. Coinbase taker fee defaults to
    5 bps in ``settings.cost_aware_taker_fee_bps``; live spread comes
    from ``ctx.spread_bps`` (the same in-memory book the existing
    spread-sanity gate reads).

    Engine missing (unit tests): allows with verdict='no_engine' for
    parity with the other calibration gates.

    Insufficient calibration data: allows with verdict='no_data' so
    new pairs (in shadow window) aren't auto-rejected before
    ``decay_miner`` accumulates samples.
    """
    from .settings import load as _load_fp_settings

    fp_settings = _load_fp_settings()
    if not getattr(fp_settings, "cost_aware_admission_enabled", False):
        return GateResult(
            "cost_aware_admission", True, "",
            {"verdict": "disabled"},
        )

    if ctx.engine is None:
        return GateResult(
            "cost_aware_admission", True, "",
            {"verdict": "no_engine"},
        )

    ticker = str(alert.get("ticker") or "")
    alert_type = str(alert.get("alert_type") or "")
    signal_score = float(alert.get("signal_score") or 0.0)

    # Cost = round-trip in bps: 2 * (taker_fee + spread). Spread comes
    # from the LIVE book (ctx) so a momentarily-wide top-of-book gates
    # the trade even if the calibrated mean cleared a static threshold.
    taker_fee_bps = float(fp_settings.cost_aware_taker_fee_bps or 0.0)
    spread_bps = float(ctx.spread_bps or 0.0)
    cost_bps = 2.0 * (taker_fee_bps + spread_bps)
    cost_return = cost_bps / 10000.0  # bps -> return units (mean_return is fraction)

    try:
        from .calibration import _fetch_bucket_rows, _best_sharpe_row
        from .decay_miner import score_bucket as _score_bucket

        bucket = _score_bucket(signal_score)
        rows = _fetch_bucket_rows(
            ctx.engine, ticker=ticker, alert_type=alert_type, bucket=bucket,
        )
    except Exception as exc:
        # Lookup failures shouldn't block; mirrors gate_calibrated_tradeability.
        return GateResult(
            "cost_aware_admission", True, "",
            {"verdict": "lookup_failed", "error": str(exc)[:120]},
        )

    if not rows:
        return GateResult(
            "cost_aware_admission", True, "",
            {"verdict": "no_data", "score_bucket": bucket,
             "cost_bps": cost_bps},
        )

    best_row = _best_sharpe_row(rows)
    if best_row is None:
        return GateResult(
            "cost_aware_admission", True, "",
            {"verdict": "insufficient_data", "score_bucket": bucket,
             "cost_bps": cost_bps},
        )

    mean_return = float(best_row.get("mean_return") or 0.0)
    horizon_s = int(best_row.get("horizon_s") or 0)
    sample_count = int(best_row.get("sample_count") or 0)
    mean_bps = mean_return * 10000.0
    cleared = mean_return >= cost_return

    detail = {
        "verdict": "cleared" if cleared else "below_cost",
        "score_bucket": bucket,
        "best_horizon_s": horizon_s,
        "sample_count": sample_count,
        "mean_return_bps": round(mean_bps, 4),
        "cost_bps": round(cost_bps, 4),
        "taker_fee_bps": round(taker_fee_bps, 4),
        "spread_bps": round(spread_bps, 4),
    }
    if not cleared:
        return GateResult(
            "cost_aware_admission", False, "below_round_trip_cost", detail,
        )
    return GateResult("cost_aware_admission", True, "", detail)


def gate_spread_sanity(alert: dict, ctx: ExecContext,
                       *, max_spread_bps: float = MAX_SPREAD_BPS) -> GateResult:
    """Reject when the live book is too wide. We grab spread from
    ``ctx`` (current top-of-book), NOT from the alert's features
    (which can be stale by a fraction of a second)."""
    sp = float(ctx.spread_bps or 0.0)
    # If we have NO book at all (best_bid/ask zero), we can't size
    # safely; treat that as a hard deny rather than letting through.
    if ctx.best_bid <= 0.0 or ctx.best_ask <= 0.0:
        return GateResult("spread_sanity", False, "no_top_of_book",
                          {"best_bid": ctx.best_bid, "best_ask": ctx.best_ask})
    if sp > max_spread_bps:
        return GateResult("spread_sanity", False, "spread_too_wide",
                          {"spread_bps": float(sp), "max_bps": float(max_spread_bps)})
    return GateResult("spread_sanity", True, "", {"spread_bps": float(sp)})


# F8b: signal-class-specific ticker allowlists.
#
# F8a-evaluation-rerun-2 verified n=43 distinct realized exits on the
# pullback signal with bimodal per-ticker edge:
#   BTC-USD:  +5.66 bps avg, 62.5% win rate (n=8)
#   SOL-USD:  +3.34 bps avg, 38.5% win rate (n=13)
#   ETH-USD:  -6.44 bps avg, 30.0% win rate (n=10)
#   DOGE-USD: -14.39 bps avg, 16.7% win rate (n=12)
# Restricting to the positive subset is necessary to make the signal
# class production-eligible. Hard-coded set is fine while we have
# one signal class with a known split; if/when more signal classes
# add their own allowlists, extract to a config artifact.
PULLBACK_LONG_ALLOWLIST: frozenset[str] = frozenset({"BTC-USD", "SOL-USD"})


def gate_pullback_ticker_allowed(alert: dict, ctx: ExecContext) -> GateResult:
    """F8b: per-signal-class ticker allowlist.

    Pass-through for any alert NOT of type ``volume_breakout_pullback_long``.
    For pullback alerts, only tickers in ``PULLBACK_LONG_ALLOWLIST``
    proceed; others rejected with a per-ticker reason so the postmortem
    is self-documenting.
    """
    alert_type = str(alert.get("alert_type") or "")
    if alert_type != "volume_breakout_pullback_long":
        return GateResult("pullback_ticker", True, "",
                          {"verdict": "not_pullback_long_signal"})
    ticker = str(alert.get("ticker") or "")
    if ticker in PULLBACK_LONG_ALLOWLIST:
        return GateResult("pullback_ticker", True, "",
                          {"verdict": "ticker_allowed", "ticker": ticker})
    return GateResult(
        "pullback_ticker", False,
        f"pullback_ticker_not_allowed:{ticker}",
        {"verdict": "blocked", "ticker": ticker,
         "allowlist": sorted(PULLBACK_LONG_ALLOWLIST)},
    )


def gate_capacity(alert: dict, ctx: ExecContext,
                  *, max_per_pair: int = MAX_OPEN_POSITIONS_PER_PAIR) -> GateResult:
    if ctx.open_positions_for_ticker >= max_per_pair:
        return GateResult("capacity", False, "pair_already_held",
                          {"open": int(ctx.open_positions_for_ticker),
                           "max": int(max_per_pair)})
    return GateResult("capacity", True, "",
                      {"open": int(ctx.open_positions_for_ticker)})


def gate_daily_budget(alert: dict, ctx: ExecContext,
                      *, daily_max_usd: float = DAILY_NOTIONAL_BUDGET_USD,
                      planned_notional_usd: float = DEFAULT_NOTIONAL_USD) -> GateResult:
    used = float(ctx.daily_notional_used_usd or 0.0)
    projected = used + planned_notional_usd
    if projected > daily_max_usd:
        return GateResult("daily_budget", False, "budget_exhausted",
                          {"used_usd": used, "planned_usd": planned_notional_usd,
                           "daily_max_usd": daily_max_usd})
    return GateResult("daily_budget", True, "",
                      {"used_usd": used, "projected_usd": projected})


def gate_mode_interlock(alert: dict, ctx: ExecContext) -> GateResult:
    """The trade-off this gate enforces:

        IF settings say live AND operator authorized live, allow live.
        IF settings say live AND operator did NOT authorize, downgrade
            to paper (allow=True with reason='live_not_authorized').
        IF settings say paper, always allow as paper.

    The downgrade-to-paper case is interesting: we DO want the trade
    to record (so the operator sees that a live attempt was wanted),
    but it must execute as paper. The executor reads ``ctx.mode`` AFTER
    the gate has run; this gate may *mutate* ctx.mode (the only side
    effect in the gate suite, deliberately scoped here).
    """
    raw_mode = (ctx.mode or "paper").strip().lower()
    if raw_mode == "live" and not ctx.live_authorized:
        # Force-downgrade to paper. Document via reason for the
        # decision row; the gate still ALLOWS the trade through (it
        # will execute as paper).
        ctx.mode = "paper"
        return GateResult("mode_interlock", True, "live_not_authorized_downgraded_to_paper",
                          {"requested_mode": "live", "effective_mode": "paper",
                           "live_authorized_env_set": False})
    if raw_mode not in ("paper", "live"):
        # Any unknown mode is forced to paper.
        ctx.mode = "paper"
        return GateResult("mode_interlock", True, "unknown_mode_forced_to_paper",
                          {"requested_mode": raw_mode, "effective_mode": "paper"})
    return GateResult("mode_interlock", True, "",
                      {"effective_mode": ctx.mode})


# ── Composite runner ─────────────────────────────────────────────────


# Default gate list; the executor uses this. Tests can pass custom
# lists. Order doesn't matter for correctness (each gate evaluates
# independently) but mode_interlock is run FIRST so any mode mutation
# is visible to subsequent gates if they care.
DEFAULT_GATES: tuple[Callable[[dict, ExecContext], GateResult], ...] = (
    gate_mode_interlock,
    gate_recency,
    gate_min_score,
    # Negative-edge gate runs BEFORE the cost-bar gate so that
    # statistically-negative signals (volume_breakout_long buckets
    # under current data) report 'negative_edge' as the primary
    # rejection reason rather than the more generic 'signal_not_
    # tradeable'. Brief's gate-order paragraph said "after"; brief's
    # verification SQL looks for 'negative_edge%' in reject_reason --
    # which requires this ordering. Both gates still always run
    # (run_gates collects every result into gates_json) so the
    # postmortem detail is unchanged either way.
    gate_negative_edge_excluded,
    gate_calibrated_tradeability,
    # f-fastpath-universe-rotation Step 5 (2026-05-07): cost-aware
    # admission gate. Off-by-default
    # (cost_aware_admission_enabled=False) so switchover is bit-
    # identical. When enabled, runs AFTER the static
    # gate_calibrated_tradeability so its more-specific "below_round_
    # trip_cost" reason dominates as the primary rejection in
    # postmortems; the calibrated gate's pre-existing detail is
    # preserved in gates_json regardless.
    gate_cost_aware_admission,
    # F8b: signal-class-specific ticker allowlist runs AFTER the
    # calibrated-edge gates so that a calibrated negative-edge or
    # not-tradeable verdict still reports as the primary reject
    # reason (more actionable than the allowlist filter), and BEFORE
    # the price-sanity / capacity / budget gates because the
    # allowlist is purely a signal-eligibility check that doesn't
    # care about per-decision state.
    gate_pullback_ticker_allowed,
    gate_spread_sanity,
    gate_capacity,
    gate_daily_budget,
)


@dataclass
class GateRunResult:
    allow: bool
    """All gates allowed."""
    deny_reason: str
    """Empty if allowed; otherwise the FIRST denying gate's reason."""
    results: list[GateResult]
    """Every gate's individual result, for the JSONB postmortem."""


def run_gates(alert: dict, ctx: ExecContext,
              gates: tuple = DEFAULT_GATES) -> GateRunResult:
    """Run the gate list. Returns a composite decision."""
    out: list[GateResult] = []
    deny_reason = ""
    allow_overall = True
    for gate_fn in gates:
        res = gate_fn(alert, ctx)
        out.append(res)
        if not res.allow and allow_overall:
            allow_overall = False
            deny_reason = f"{res.name}:{res.reason}"
    return GateRunResult(allow=allow_overall, deny_reason=deny_reason, results=out)


# ── Env loaders ──────────────────────────────────────────────────────


def env_overrides() -> dict:
    """Read tunable thresholds from environment so operators can tighten
    paper-mode behavior without redeploying code."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name) or default)
        except (TypeError, ValueError):
            return default
    return {
        "min_score": _f("CHILI_FAST_PATH_EXEC_MIN_SCORE", MIN_SIGNAL_SCORE),
        "max_spread_bps": _f("CHILI_FAST_PATH_EXEC_MAX_SPREAD_BPS", MAX_SPREAD_BPS),
        "daily_max_usd": _f("CHILI_FAST_PATH_EXEC_DAILY_USD", DAILY_NOTIONAL_BUDGET_USD),
        "default_notional_usd": _f("CHILI_FAST_PATH_EXEC_NOTIONAL_USD", DEFAULT_NOTIONAL_USD),
    }


def is_live_authorized() -> bool:
    """Single source of truth for the live-trade kill switch.

    BOTH conditions must hold for live placement:
        - CHILI_FAST_PATH_MODE=live (settings)
        - CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED=1 (operator-explicit env)

    If either is missing, the executor must downgrade to paper. There
    is intentionally NO third bypass — operators must actively flip the
    AUTHORIZED flag to send real Coinbase orders.
    """
    raw = (os.environ.get("CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


__all__ = [
    "GateResult", "ExecContext", "GateRunResult",
    "gate_recency", "gate_min_score", "gate_calibrated_tradeability",
    "gate_negative_edge_excluded", "gate_pullback_ticker_allowed",
    "gate_cost_aware_admission",
    "gate_spread_sanity",
    "gate_capacity", "gate_daily_budget", "gate_mode_interlock",
    "DEFAULT_GATES", "run_gates",
    "ALERT_RECENCY_MAX_AGE_S", "MIN_SIGNAL_SCORE", "MAX_SPREAD_BPS",
    "DEFAULT_NOTIONAL_USD", "DAILY_NOTIONAL_BUDGET_USD",
    "MAX_OPEN_POSITIONS_PER_PAIR",
    "env_overrides", "is_live_authorized",
]
