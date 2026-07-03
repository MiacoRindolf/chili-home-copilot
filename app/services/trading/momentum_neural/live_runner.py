"""Guarded live automation runner (Phase 8) — spot adapter resolved by execution_family (Phase 11 seam).

Supported families: ``coinbase_spot``, ``robinhood_spot``; other families skip with ``execution_family_not_implemented``.

Snapshot contract:
- Never overwrite ``momentum_risk`` / admission keys.
- Mutable live execution state: ``risk_snapshot_json["momentum_live_execution"]`` only.
- Boundary checks each tick via ``evaluate_proposed_momentum_automation`` (mode=live).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationSession
from ..execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    ExecutionFamilyNotImplementedError,
    normalize_execution_family,
    momentum_runner_supports_execution_family,
    resolve_live_spot_adapter_factory,
)
from ..autopilot_scope import (
    AUTOPILOT_MOMENTUM_NEURAL,
    check_autopilot_entry_gate,
)
from ..governance import is_kill_switch_active
from ..venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
    is_fresh_enough,
)
from .persistence import append_trading_automation_event
from ..decision_ledger import (
    finalize_packet_after_simulated_exit,
    mark_packet_executed,
    record_packet_execution_intent,
    run_momentum_entry_decision,
)
from ..deployment_ladder_service import record_trade_outcome_metrics
from .risk_evaluator import evaluate_proposed_momentum_automation
from .risk_policy import (
    RISK_SNAPSHOT_KEY,
    compute_risk_first_quantity,
    equity_relative_notional_cap,
    liquidity_capped_notional,
    max_loss_circuit_decision,
    policy_float_cap,
    policy_int_cap,
    adaptive_reentry_cooldown_seconds,
    reentry_after_stop_allowed,
)
from .paper_execution import (
    _classify_cadence,
    cushion_adaptive_trail_stop,
    breakeven_stop_after_partial,
    class_aware_reward_risk,
    double_top_tighten_decision,
    effective_stop_atr_pct,
    flag_breakout_add_decision,
    iceberg_seller_score,
    measured_move_exit_enabled,
    measured_move_scale_exit_decision,
    ofi_exhaustion_lock,
    pullback_add_decision,
    pyramid_add_decision,
    pyramid_blend_on_fill,
    regime_atr_pct,
    runner_trail_stop,
    scale_grid_levels,
    scale_out_fraction,
    scale_out_quantity,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
    tape_accel_reversal_exit,
    utc_iso,
)
from .persistence import variant_for_id
from .live_fsm import (
    LIVE_RUNNER_RUNNABLE_STATES,
    STATE_ARMED_PENDING_RUNNER,
    STATE_LIVE_BAILOUT,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_COOLDOWN,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_ERROR,
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    assert_transition_live,
)
from .session_lifecycle import is_operator_paused
from .strategy_params import normalize_strategy_params
from .entry_gates import (
    _entry_extension_veto,
    _entry_flow_veto,
    _l2_entry_confirm,
    breakout_failed_to_hold,
)
from .entry_gates import (
    TAPE_HOLD_VALID_WAIT_REASONS,
    tape_confirmed_hold_trigger,
    tape_confirms_hold,
)
from .entry_gates import (
    _dip_buy_in_rth_window,
    _l2_big_buyer_bid_starter,
    add_into_halt_ok,
    round_number_entry_context,
)
from .hold_signals import (
    percentile as _percentile,
    smart_hold_band_frac,
    smart_hold_decision,
)

_log = logging.getLogger(__name__)

KEY_LIVE_EXEC = "momentum_live_execution"

AdapterFactory = Callable[[], Any]


# ── REPLAY v3 P0a — SIMULATED CLOCK (the single chokepoint) ──────────────────────
# The whole live FSM reads time through ``_utcnow()`` (and the few ET / aware-UTC
# helpers below, which all DERIVE from it). A process-global, async/thread-safe
# ``ContextVar`` lets the Replay v3 harness FREEZE the runner's clock at a historical
# instant without forking a single line of the FSM. Default is ``None`` — and when it
# is ``None`` (ALWAYS in prod, since only the replay harness ever sets it) ``_utcnow()``
# returns the real ``datetime.utcnow()`` on the EXACT same code path as before, so prod
# is BYTE-IDENTICAL. The ContextVar resets automatically on block/exception exit, so it
# can never leak a frozen clock into a real lane. See docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §3.1.
_SIM_NOW: contextvars.ContextVar[Optional[datetime]] = contextvars.ContextVar(
    "_chili_replay_sim_now", default=None
)


def _utcnow() -> datetime:
    v = _SIM_NOW.get()
    return v if v is not None else datetime.utcnow()


def set_sim_clock(ts: Optional[datetime]) -> "contextvars.Token[Optional[datetime]]":
    """Push a simulated 'now' onto the clock chokepoint (replay harness only).

    ``ts`` is interpreted as naive-UTC — the codebase's dominant convention and what
    ``_utcnow()`` returns in prod (``datetime.utcnow()``). A tz-aware ``ts`` is normalized
    to naive-UTC so the sim instant matches the prod shape exactly. Returns the
    ``contextvars.Token`` so the caller can ``reset_sim_clock(token)`` to restore the prior
    value (prefer the ``replay_clock`` context manager, which does this for you)."""
    if ts is not None and ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return _SIM_NOW.set(ts)


def reset_sim_clock(token: "contextvars.Token[Optional[datetime]]") -> None:
    """Pop the simulated clock, restoring the value before the matching ``set_sim_clock``."""
    _SIM_NOW.reset(token)


@contextlib.contextmanager
def replay_clock(ts: Optional[datetime]) -> Iterator[None]:
    """Context manager freezing ``_utcnow()`` (and the aware-UTC / ET helpers) at ``ts``.

    Pure ContextVar push/pop — async- and thread-safe, and auto-resets on normal exit AND
    on exception (the ``finally`` always restores the prior value), so a frozen clock can
    never escape the block into prod. Nests correctly: an inner ``replay_clock`` restores
    the OUTER sim time on exit, not ``None``. Prod never enters this manager, so prod stays
    byte-identical."""
    token = set_sim_clock(ts)
    try:
        yield
    finally:
        reset_sim_clock(token)


def _utcnow_aware() -> datetime:
    """tz-AWARE UTC 'now', derived from the same chokepoint as ``_utcnow()``.

    Prod path is byte-identical to ``datetime.now(timezone.utc)``: with no sim clock,
    ``_utcnow()`` is ``datetime.utcnow()`` (naive UTC) and stamping ``tzinfo=utc`` yields the
    same instant ``datetime.now(timezone.utc)`` would (both are the real wall clock in UTC).
    Under the replay clock it returns the injected sim instant as aware-UTC."""
    return _utcnow().replace(tzinfo=timezone.utc)


def _now_in_tz(zone: Any) -> datetime:
    """'now' projected into ``zone`` (e.g. ET), derived from the clock chokepoint so the sim
    clock governs the ET wall-clock window checks too. ``zone`` is a ``ZoneInfo``/tzinfo. Prod
    path is byte-identical to ``datetime.now(zone)``: with no sim clock the converted instant
    is the real wall clock in ``zone``; under replay it is the sim instant projected there."""
    return _utcnow().replace(tzinfo=timezone.utc).astimezone(zone)


# ── REPLAY v3 P1 — RECORDED-OHLCV PROVIDER SEAM (the one heavy NETWORK read) ──────
# ``fetch_ohlcv_df`` is the single heavy external read inside ``tick_live_session`` (15m
# ATR / expected-move / the pullback + volume triggers — ~13 call sites). To replay the
# FSM hermetically the harness must serve those bars from RECORDED data instead of hitting
# Massive/Polygon/yfinance. We mirror the ``_SIM_NOW`` clock pattern exactly: a process-
# global, async/thread-safe ``ContextVar`` holding an OPTIONAL provider callable. Default is
# ``None`` — and when it is ``None`` (ALWAYS in prod, since only the replay harness ever sets
# it) ``_replay_aware_fetch_ohlcv_df`` calls the REAL ``fetch_ohlcv_df`` with the EXACT same
# args on the EXACT same code path as before, so prod is BYTE-IDENTICAL. The ContextVar
# resets automatically on block/exception exit, so a replay provider can never leak into a
# real lane. Every in-tick ``fetch_ohlcv_df`` invocation routes through this ONE wrapper.
# The provider signature mirrors ``fetch_ohlcv_df`` itself:
#     provider(ticker, *, interval, period) -> pandas.DataFrame
# See docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §3.2 / §6 (the OHLCV provider seam, P1).
_REPLAY_OHLCV_PROVIDER: contextvars.ContextVar[Optional[Callable[..., Any]]] = (
    contextvars.ContextVar("_chili_replay_ohlcv_provider", default=None)
)


def _replay_aware_fetch_ohlcv_df(
    ticker: str, interval: str = "1d", period: str = "6mo"
) -> Any:
    """Route a runner OHLCV read through the replay provider when one is installed, else the
    real ``fetch_ohlcv_df``.

    PROD (no provider set — ALWAYS): imports + calls the real ``fetch_ohlcv_df(ticker,
    interval=interval, period=period)`` exactly as the call sites did before this seam — same
    import, same positional/keyword args, same cache. BYTE-IDENTICAL.

    REPLAY (provider installed): calls ``provider(ticker, interval=interval, period=period)``
    so the bars come from recorded data with ZERO network I/O. The provider is responsible for
    serving bars as-of the sim clock (``_utcnow()``)."""
    provider = _REPLAY_OHLCV_PROVIDER.get()
    if provider is not None:
        return provider(ticker, interval=interval, period=period)
    from ..market_data import fetch_ohlcv_df as _real_fetch_ohlcv_df

    return _real_fetch_ohlcv_df(ticker, interval=interval, period=period)


def set_replay_ohlcv_provider(
    provider: Optional[Callable[..., Any]],
) -> "contextvars.Token[Optional[Callable[..., Any]]]":
    """Install a recorded-OHLCV provider on the seam (replay harness only). Returns the
    ``contextvars.Token`` so the caller can ``reset_replay_ohlcv_provider(token)`` (prefer the
    ``replay_ohlcv_provider`` context manager, which does this for you)."""
    return _REPLAY_OHLCV_PROVIDER.set(provider)


def reset_replay_ohlcv_provider(
    token: "contextvars.Token[Optional[Callable[..., Any]]]",
) -> None:
    """Pop the OHLCV provider, restoring the value before the matching set."""
    _REPLAY_OHLCV_PROVIDER.reset(token)


@contextlib.contextmanager
def replay_ohlcv_provider(provider: Optional[Callable[..., Any]]) -> Iterator[None]:
    """Context manager installing ``provider`` as the runner's OHLCV source for the block.

    Pure ContextVar push/pop — async/thread-safe, auto-resets on normal exit AND on exception
    (the ``finally`` always restores the prior value), so a replay provider can never escape
    the block into prod. Nests correctly. Prod never enters this manager, so prod stays
    byte-identical."""
    token = set_replay_ohlcv_provider(provider)
    try:
        yield
    finally:
        reset_replay_ohlcv_provider(token)


# ── CHUNK 3 — S4 FAST EXECUTOR: rail-governed calls + RTT measurement ─────────────
# A process-local exponential-moving-average of the MEASURED rail round-trip time, used
# by the inline micro-repeg (inter-repeg delay) and the fast ack-poll (poll cadence) so
# both adapt to the rail's real latency instead of a fixed clock. Bounded scalar (no
# growth); seeded None until the first measured call. docs/DESIGN/MOMENTUM_ENGINE.md §3.
_RAIL_RTT_EMA_S: float | None = None
_RAIL_RTT_ALPHA = 0.3  # EMA smoothing on the measured RTT


def _note_rail_rtt(elapsed_s: float) -> None:
    """Fold a measured rail RTT into the process-local EMA (clamped to a sane band so a
    one-off stall / clock skew can't poison the adaptive delays)."""
    global _RAIL_RTT_EMA_S
    try:
        s = float(elapsed_s)
    except (TypeError, ValueError):
        return
    if not (s >= 0.0) or s > 30.0:  # NaN-safe + reject absurd measurements
        return
    if _RAIL_RTT_EMA_S is None:
        _RAIL_RTT_EMA_S = s
    else:
        _RAIL_RTT_EMA_S = (1.0 - _RAIL_RTT_ALPHA) * _RAIL_RTT_EMA_S + _RAIL_RTT_ALPHA * s


def _measured_rail_rtt_s() -> float | None:
    return _RAIL_RTT_EMA_S


def _rail_lane_key(sess) -> str:
    """Token-bucket key. ONE bucket per (user) lane so ALL of that user's lane rail
    calls (places + polls across every session/symbol) share the SAME budget — that is
    what bounds multi-admission. Falls back to the global lane bucket if user is unset."""
    try:
        return f"momentum:{int(getattr(sess, 'user_id', 0) or 0)}"
    except (TypeError, ValueError):
        return "momentum"


def _governed_get_order(adapter, oid, *, sess=None):
    """Rate-governed wrapper around ``adapter.get_order`` (a LIST endpoint sharing the
    rail budget). Acquires a token (flag-OFF ⇒ instant pass-through, byte-identical),
    times the call into the RTT EMA, and feeds the outcome back to the governor (a 429
    halves the rate). Returns the EXACT ``(order, freshness)`` tuple ``get_order`` does;
    on a governor DEFER returns ``(None, None)`` so the caller treats it as 'not yet
    confirmed' (the resting order persists — never a silent drop, it is logged)."""
    import time as _time

    from .rail_governor import acquire_rail, note_rail_outcome

    res = acquire_rail(settings, lane_key=_rail_lane_key(sess))
    if not res.acquired:
        _log.info(
            "[momentum_s4] rail governor DEFER get_order oid=%s waited=%.3fs rps=%.3f",
            oid, res.waited_s, res.refill_rps,
        )
        return None, None
    _t0 = _time.monotonic()
    try:
        out = adapter.get_order(str(oid))
    except Exception as exc:  # measure + feed back even on failure
        # A 429 on the POLL path now SURFACES (the adapter re-raises the rate-limit case
        # instead of masking it as a benign None) so the governor can HALVE the rate —
        # without this, repeated poll-side 429s read as successes and WIDEN the rate INTO
        # the rate limit. note_rail_outcome only halves on a recognized rate-limit signal;
        # any other exception is NEUTRAL (neither widen nor halve).
        note_rail_outcome(settings, exc, lane_key=_rail_lane_key(sess))
        raise
    _note_rail_rtt(_time.monotonic() - _t0)
    # get_order returns (order, freshness). A 429 surfaces as the EXCEPTION above; a bare
    # None order here is AMBIGUOUS (not-found vs a swallowed transient) so it is NEUTRAL —
    # only an order object carrying a 429-shaped status halves the rate, a real order
    # widens it. A None must NOT be counted as a success (that would falsely widen toward
    # the limit on a transient poll error). [poll-path 429 unmasking, 2026-06-27]
    _ord = out[0] if isinstance(out, tuple) and out else None
    if _ord is not None:
        note_rail_outcome(settings, _ord, lane_key=_rail_lane_key(sess))
    return out


def _fast_ack_poll_entry(adapter, oid, *, sess, interval_window_s: float):
    """CHUNK 3-B — FAST ACK-POLL. Poll ``get_order`` repeatedly WITHIN the current tick
    to confirm an entry fill without waiting for the next external WS/batch tick. The
    cadence rides the MEASURED rail RTT (seed -> geometric widen); the TOTAL window is
    bounded by ``interval_window_s`` (rest_bars * entry_interval, the EXISTING ack-timeout
    backstop) AND a hard iteration cap (belt-and-suspenders). Every poll goes through the
    rail governor (shared budget). Returns the FIRST order object that is done/filled, or
    the LAST polled order (so the caller's existing open/cancel/repeg logic runs exactly
    as today on a non-fill). Flag OFF ⇒ a single governed poll (one `get_order`), so the
    confirm is byte-identical to the deployed tick-coupled path (modulo the governor,
    which is itself byte-identical when OFF)."""
    import time as _time

    no, fr = _governed_get_order(adapter, oid, sess=sess)
    # Already terminal/filled on the first look, or fast-poll disabled ⇒ done.
    if not bool(getattr(settings, "chili_momentum_entry_fast_poll_enabled", True)):
        return no, fr
    if no is not None and (_order_done_for_entry(no) or float(getattr(no, "filled_size", 0) or 0.0) > 0.0):
        return no, fr
    seed = float(getattr(settings, "chili_momentum_entry_fast_poll_seed_interval_s", 0.25) or 0.25)
    widen = float(getattr(settings, "chili_momentum_entry_fast_poll_widen_factor", 1.6) or 1.6)
    max_iters = int(getattr(settings, "chili_momentum_entry_fast_poll_max_iters", 12) or 12)
    # Cadence base = the measured RTT if we have one (poll about as fast as the rail
    # answers), else the conservative seed. Never below the seed (no busy-spin).
    rtt = _measured_rail_rtt_s()
    interval = max(seed, rtt) if (rtt is not None and rtt > 0) else seed
    window = max(0.0, float(interval_window_s))
    if window <= 0.0:
        return no, fr
    # IDLE-IN-TRANSACTION GUARD (2026-06-27): `tick_live_session` holds a
    # `SELECT ... FOR UPDATE NOWAIT` row lock on the session for the WHOLE call, so any
    # sleeping done here pins a DB connection in an OPEN transaction (and blocks
    # broker-sync / the reconciler from touching this session). The geometric widen to
    # max_iters could otherwise sleep ~100s+ at a 5m interval (the rest_bars*interval
    # window is NOT the binding cap there). Bound the TOTAL in-tick wall-clock to a small
    # ceiling so worst-case lock-hold is single-digit seconds (ONE documented knob,
    # default 5s). The fast-poll is a latency OPTIMIZER, not the ack-timeout backstop —
    # an unfilled order still falls through to the existing event-driven cancel/repeg
    # path on the NEXT tick, with the row lock RELEASED in between.
    _fp_hard_ceiling_s = float(
        getattr(settings, "chili_momentum_entry_fast_poll_max_wall_s", 5.0) or 5.0
    )
    window = min(window, max(0.0, _fp_hard_ceiling_s))
    deadline = _time.monotonic() + window
    iters = 0
    polls = 1
    while iters < max_iters and _time.monotonic() < deadline:
        sleep_s = min(interval, max(0.0, deadline - _time.monotonic()))
        if sleep_s > 0:
            _time.sleep(sleep_s)
        _no, _fr = _governed_get_order(adapter, oid, sess=sess)
        polls += 1
        iters += 1
        interval = interval * widen  # geometric widening
        if _no is None:
            # governor DEFER or a transient None — keep the last good `no`, retry.
            continue
        no, fr = _no, _fr
        if _order_done_for_entry(no) or float(getattr(no, "filled_size", 0) or 0.0) > 0.0:
            _log.info(
                "[momentum_s4] fast-poll CONFIRM oid=%s after polls=%d rtt=%s",
                oid, polls, (round(rtt, 4) if rtt else None),
            )
            break
    return no, fr


def _governed_place(adapter, place_fn, *, sess=None, **kwargs):
    """Rate-governed wrapper around an order PLACE (place_limit_order_gtc /
    place_market_order). Acquires a token (flag-OFF ⇒ instant pass-through,
    byte-identical), times the call into the RTT EMA, and feeds the place result back to
    the governor (a 429 halves the rate, a clean place widens it). On a governor DEFER
    returns a synthetic ``{"ok": False, "error": "rail_governor_deferred", ...}`` so the
    caller's existing not-ok branch re-watches / retries next tick (never a silent
    drop)."""
    import time as _time

    from .rail_governor import acquire_rail, note_rail_outcome

    res = acquire_rail(settings, lane_key=_rail_lane_key(sess))
    if not res.acquired:
        _log.info(
            "[momentum_s4] rail governor DEFER place waited=%.3fs rps=%.3f",
            res.waited_s, res.refill_rps,
        )
        return {
            "ok": False,
            "error": "rail_governor_deferred",
            "deferred": True,
            "client_order_id": kwargs.get("client_order_id"),
        }
    _t0 = _time.monotonic()
    try:
        out = place_fn(**kwargs)
    except Exception as exc:
        note_rail_outcome(settings, exc, lane_key=_rail_lane_key(sess))
        raise
    _note_rail_rtt(_time.monotonic() - _t0)
    note_rail_outcome(settings, out, lane_key=_rail_lane_key(sess))
    return out


# ── P0: per-(symbol, day) DailyContext cache (NO new per-tick fetch) ──────────────
# The blue-sky entry trigger + the overhead-supply veto need the daily-chart context
# (overhead supply, clear-sky room, swing/gap/rejection levels). The DailyContext is a
# frozen dataclass that must NOT be persisted into the JSON `le` snapshot, so it lives
# in this in-process cache (hard max size + per-day key = its TTL — a new trading day
# evicts the prior entry). The daily bars are fetched at most ONCE per symbol per day
# across the whole process, never on the tick path. docs/DESIGN/MOMENTUM_LANE.md
_DAILY_CTX_CACHE: dict[str, Any] = {}
_DAILY_CTX_CACHE_MAX = 512


def _daily_ctx_cached(symbol: str, *, price: float | None = None) -> Any:
    """Return the cached DailyContext for ``symbol`` on the current UTC day, computing
    (and caching) it from a single ``1d``/``max`` OHLCV fetch on the first call of the
    day. Bounded (``_DAILY_CTX_CACHE_MAX``) with the day baked into the key so stale
    entries fall out as the date rolls. Fail-OPEN to ``None`` on any error (the entry
    path then degrades to daily-blind — never blocked). Equities only (the caller gates
    out crypto)."""
    try:
        _day = _utcnow().strftime("%Y%m%d")
        _key = f"{str(symbol).upper()}|{_day}"
        if _key in _DAILY_CTX_CACHE:
            return _DAILY_CTX_CACHE[_key]
        _dc_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)
        from .daily_levels import compute_daily_context as _dc_compute

        _lb = int(getattr(settings, "chili_momentum_daily_lookback_days", 20) or 20)
        _df = _dc_fetch(symbol, interval="1d", period="max")
        _px = float(price) if (price is not None and price > 0) else None
        _ctx = _dc_compute(_df, lookback=_lb, price=_px, entry_context=True)
        # bound the cache: drop the oldest-day entries when it grows past the cap.
        if len(_DAILY_CTX_CACHE) >= _DAILY_CTX_CACHE_MAX:
            for _k in sorted(_DAILY_CTX_CACHE.keys())[: max(1, _DAILY_CTX_CACHE_MAX // 4)]:
                _DAILY_CTX_CACHE.pop(_k, None)
        _DAILY_CTX_CACHE[_key] = _ctx
        return _ctx
    except Exception:
        return None


# ── REGIME-ADAPTIVE FRONT-SIDE STRENGTH DISTRIBUTION (kill the fixed 0.25/0.75) ──
# The front-side size-tilt ramp used to take s_lo/s_hi/defer from the function defaults
# (0.25/0.75/0.15) — FIXED magic numbers that ignore the regime. The strength score is
# ALREADY self-normalized per name (Kaufman-ER spine + saturating sigmoids), so the
# CROSS-NAME distribution of recently-computed scores reflects the CURRENT regime: hot
# tape ⇒ scores cluster HIGH, cold tape ⇒ LOW. We keep a small, BOUNDED, TTL'd rolling
# cache of the most-recent strength scores (cross-name) and, once warm, derive the ramp
# anchors as the p25/p75/p15 of that live distribution — the operator's "percentile-
# within-batch" pattern. The cache obeys the repo convention (CLAUDE.md: "Caches must
# have hard max size + TTL"): a hard sample cap + a per-sample timestamp TTL, both ONE
# documented base each (FLOORS, not scattered caps).
#
#   * Window   : last _FRONTSIDE_DIST_MAX samples (256) OR last _FRONTSIDE_DIST_TTL_S
#                seconds (30 min) — whichever binds first. Documented base = both.
#   * Warm-up  : need >= _FRONTSIDE_DIST_MIN_K (30) fresh samples before adapting; below
#                that we return None ⇒ the caller keeps the documented base 0.25/0.75/0.15
#                (COLD-START byte-identical to today).
#   * Cost     : O(window) append + one sorted-slice quantile; no fetch, never blocks.
#   * size_floor stays the ONE documented SAFETY-FLOOR (not derived here) — the irreducible
#     base the no-magic rule explicitly permits.
_FRONTSIDE_DIST: "list[tuple[float, float]]" = []  # (monotonic_ts, strength)
_FRONTSIDE_DIST_MAX = 256          # hard max samples (the cache size cap)
_FRONTSIDE_DIST_TTL_S = 30 * 60.0  # 30 min per-sample TTL (the regime window)
_FRONTSIDE_DIST_MIN_K = 30         # warm-up floor before the ramp adapts


def _frontside_dist_note(strength: float, *, now: float | None = None) -> None:
    """Append a freshly-computed cross-name front-side ``strength`` into the bounded,
    TTL'd rolling distribution. Pure in-memory; never blocks, never fetches. Evicts
    TTL-expired samples and enforces the hard max size. Non-finite / out-of-range inputs
    are dropped (the score is [0,1] by construction). Fail-silent (a cache hiccup must
    never touch the entry path)."""
    try:
        import time as _time

        s = float(strength)
        if not math.isfinite(s):
            return
        _now = float(now) if now is not None else _time.monotonic()
        _FRONTSIDE_DIST.append((_now, s))
        # TTL eviction: drop samples older than the regime window.
        _cut = _now - _FRONTSIDE_DIST_TTL_S
        if _FRONTSIDE_DIST and _FRONTSIDE_DIST[0][0] < _cut:
            _kept = [t for t in _FRONTSIDE_DIST if t[0] >= _cut]
            _FRONTSIDE_DIST[:] = _kept
        # Hard max-size eviction: keep the most-recent _FRONTSIDE_DIST_MAX.
        if len(_FRONTSIDE_DIST) > _FRONTSIDE_DIST_MAX:
            del _FRONTSIDE_DIST[: len(_FRONTSIDE_DIST) - _FRONTSIDE_DIST_MAX]
    except Exception:
        return


def _frontside_adaptive_thresholds(
    *, now: float | None = None,
) -> "tuple[float, float, float, int] | None":
    """Return ``(s_lo, s_hi, defer_below, n_fresh)`` = (p25, p75, p15) of the FRESH
    rolling front-side strength distribution + the warm sample count, or ``None`` when
    the cache is cold (< _FRONTSIDE_DIST_MIN_K fresh samples) so the caller falls back
    to the documented base (0.25/0.75/0.15 — byte-identical to today). O(window): one
    TTL filter + a single sorted-slice quantile (``statistics.quantiles``). Fail-CLOSED
    to ``None`` (⇒ base) on any error — adapting must never throw onto the entry path."""
    try:
        import time as _time
        import statistics as _stats

        _now = float(now) if now is not None else _time.monotonic()
        _cut = _now - _FRONTSIDE_DIST_TTL_S
        fresh = [s for (t, s) in _FRONTSIDE_DIST if t >= _cut and math.isfinite(s)]
        if len(fresh) < _FRONTSIDE_DIST_MIN_K:
            return None
        # statistics.quantiles(n=100) -> 99 cut points; index i is the (i+1)-th percentile.
        qs = _stats.quantiles(fresh, n=100, method="inclusive")
        p15 = float(qs[14])  # 15th percentile
        p25 = float(qs[24])  # 25th percentile
        p75 = float(qs[74])  # 75th percentile
        # Guard the ramp monotonicity (lo < hi) — degenerate (all-equal) dist ⇒ base.
        if not (math.isfinite(p15) and math.isfinite(p25) and math.isfinite(p75)):
            return None
        if not (p25 < p75):
            return None
        return p25, p75, p15, len(fresh)
    except Exception:
        return None


# ── Bounded momentum exit-submit retries (2026-06-07 audit) ───────────────
# A wedged exit (an unsellable-dust residual, a stale balance, a transient
# broker error) used to re-submit the flatten order on EVERY pulse — session
# 52 burned 1,500+ 'Insufficient balance' submits before being cancelled.
# Cap the broker submit RATE (exponential backoff between attempts) and the
# TOTAL attempts, then escalate to the broker-zero / dust reconcile so the
# wedged session clears itself (or surfaces a terminal error) instead of
# hammering the venue API. Settings-derived with documented defaults — one
# knob each, not scattered magic numbers.
_EXIT_SUBMIT_MAX_ATTEMPTS = int(
    getattr(settings, "chili_momentum_exit_submit_max_attempts", 8) or 8
)
_EXIT_SUBMIT_BACKOFF_BASE_SECONDS = float(
    getattr(settings, "chili_momentum_exit_submit_backoff_base_seconds", 5.0) or 5.0
)
_EXIT_SUBMIT_BACKOFF_MAX_SECONDS = float(
    getattr(settings, "chili_momentum_exit_submit_backoff_max_seconds", 300.0) or 300.0
)


def _exit_submit_backoff_seconds(attempts: int) -> float:
    """Exponential backoff (base * 2^(attempts-1)) capped at the max."""
    if attempts <= 0:
        return 0.0
    delay = _EXIT_SUBMIT_BACKOFF_BASE_SECONDS * (2.0 ** (attempts - 1))
    return min(delay, _EXIT_SUBMIT_BACKOFF_MAX_SECONDS)


def _policy_caps(snap: dict[str, Any]) -> dict[str, Any]:
    caps = snap.get("momentum_policy_caps")
    return caps if isinstance(caps, dict) else {}


def _live_exec(snap: dict[str, Any]) -> dict[str, Any]:
    le = snap.get(KEY_LIVE_EXEC)
    return dict(le) if isinstance(le, dict) else {}


def _commit_le(sess: TradingAutomationSession, le: dict[str, Any]) -> None:
    snap = dict(sess.risk_snapshot_json or {})
    snap[KEY_LIVE_EXEC] = le
    sess.risk_snapshot_json = snap
    # Force the JSON column dirty. When two commits happen in one tick around an
    # intervening flush (e.g. the scale-out: _apply_confirmed_live_partial_exit
    # flushes via its event emit, THEN the breakeven move mutates the same nested
    # position dict), the reassigned snapshot can compare EQUAL to the flush-pinned
    # baseline (shared nested refs) and SQLAlchemy skips the UPDATE — silently
    # losing the second mutation. flag_modified guarantees it persists.
    try:
        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        # sess may be a lightweight test double (SimpleNamespace) with no ORM
        # instance state; the dirty-flag only matters for real mapped sessions.
        pass


def _emit(
    db: Session,
    sess: TradingAutomationSession,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    append_trading_automation_event(
        db,
        sess.id,
        event_type,
        payload,
        correlation_id=sess.correlation_id,
        source_node_id="momentum_live_runner",
    )


def _finalize_live_decision_after_exit(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    realized_pnl_usd: float,
    slip_bps: float,
) -> None:
    pid = le.get("entry_decision_packet_id")
    if not pid:
        return
    try:
        finalize_packet_after_simulated_exit(
            db,
            packet_id=int(pid),
            realized_pnl_usd=realized_pnl_usd,
            slippage_bps=slip_bps,
        )
        record_trade_outcome_metrics(
            db,
            session_id=int(sess.id),
            variant_id=int(sess.variant_id),
            user_id=sess.user_id,
            mode="live",
            realized_pnl_usd=realized_pnl_usd,
            slippage_bps=slip_bps,
            missed_fill=False,
            partial_fill=False,
            cumulative_session_pnl_usd=float(le.get("realized_pnl_usd") or 0.0),
        )
    except Exception:
        _log.debug("live decision packet finalize skipped session=%s", sess.id, exc_info=True)


def _scan_pattern_id_for_session(db: Session, sess: TradingAutomationSession) -> int | None:
    try:
        variant = variant_for_id(db, int(sess.variant_id))
        sid = getattr(variant, "scan_pattern_id", None) if variant is not None else None
        return int(sid) if sid is not None else None
    except Exception:
        return None


def _record_live_entry_ledger_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    quantity: float,
    fill_price: float,
    fee: float = 0.0,
) -> None:
    try:
        from .. import economic_ledger as _ledger

        if not _ledger.mode_is_active():
            return
        _ledger.record_automation_session_entry_fill(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=_scan_pattern_id_for_session(db, sess),
            ticker=sess.symbol,
            quantity=quantity,
            fill_price=fill_price,
            fee=float(fee or 0.0),
            venue=sess.venue,
            mode="live",
            decision_packet_id=int(le["entry_decision_packet_id"]) if le.get("entry_decision_packet_id") else None,
            provenance={
                "runner": "momentum_live_runner",
                "entry_order_id": le.get("entry_order_id"),
                "entry_client_order_id": le.get("entry_client_order_id"),
            },
        )
    except Exception:
        _log.debug("live economic ledger entry hook skipped session=%s", sess.id, exc_info=True)


def _record_live_exit_ledger_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    quantity: float,
    entry_price: float,
    fill_price: float,
    realized_pnl_usd: float,
    reason: str,
    fee: float = 0.0,
) -> None:
    try:
        from .. import economic_ledger as _ledger

        if not _ledger.mode_is_active():
            return
        scan_pattern_id = _scan_pattern_id_for_session(db, sess)
        dpid = int(le["entry_decision_packet_id"]) if le.get("entry_decision_packet_id") else None
        _ledger.record_automation_session_exit_fill(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=scan_pattern_id,
            ticker=sess.symbol,
            quantity=quantity,
            fill_price=fill_price,
            entry_price=entry_price,
            realized_pnl_usd=realized_pnl_usd,
            fee=float(fee or 0.0),
            venue=sess.venue,
            mode="live",
            decision_packet_id=dpid,
            provenance={
                "runner": "momentum_live_runner",
                "reason": reason,
                "exit_order_id": le.get("exit_order_id"),
                "exit_client_order_id": le.get("exit_client_order_id"),
            },
        )
        _ledger.reconcile_automation_session(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=scan_pattern_id,
            ticker=sess.symbol,
            legacy_pnl=float(le.get("realized_pnl_usd") or realized_pnl_usd),
            mode="live",
            provenance={"runner": "momentum_live_runner", "reason": reason},
        )
    except Exception:
        _log.debug("live economic ledger exit hook skipped session=%s", sess.id, exc_info=True)


def _record_live_partial_exit_ledger_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    quantity: float,
    entry_price: float,
    fill_price: float,
    realized_pnl_usd: float,
    reason: str,
    fee: float = 0.0,
) -> None:
    try:
        from .. import economic_ledger as _ledger

        if not _ledger.mode_is_active():
            return
        scan_pattern_id = _scan_pattern_id_for_session(db, sess)
        dpid = int(le["entry_decision_packet_id"]) if le.get("entry_decision_packet_id") else None
        _ledger.record_automation_session_partial_exit_fill(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=scan_pattern_id,
            ticker=sess.symbol,
            quantity=quantity,
            fill_price=fill_price,
            entry_price=entry_price,
            realized_pnl_usd=realized_pnl_usd,
            fee=float(fee or 0.0),
            venue=sess.venue,
            mode="live",
            decision_packet_id=dpid,
            provenance={
                "runner": "momentum_live_runner",
                "reason": reason,
                "exit_order_id": le.get("exit_order_id"),
                "exit_client_order_id": le.get("exit_client_order_id"),
            },
        )
    except Exception:
        _log.debug("live economic ledger partial exit hook skipped session=%s", sess.id, exc_info=True)


def _fill_log_asset_class(sess: TradingAutomationSession) -> str:
    """crypto for the Coinbase-style spot lane, else equity (RH/Alpaca)."""
    if str(sess.symbol or "").upper().endswith("-USD"):
        return "crypto"
    fam = str(getattr(sess, "execution_family", "") or "").lower()
    return "crypto" if "coinbase" in fam else "equity"


def _fill_log_decision_packet_id(sess: TradingAutomationSession) -> int | None:
    try:
        snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
        le = snap.get(KEY_LIVE_EXEC) if isinstance(snap.get(KEY_LIVE_EXEC), dict) else {}
        dpid = le.get("entry_decision_packet_id")
        return int(dpid) if dpid else None
    except Exception:
        return None


def _record_fill_outcome_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    side: str,
    fill_source: str,
    broker_order_id: str | None,
    fill_price: float | None,
    qty: float | None,
    fees_usd: float | None,
    order_status: str | None,
    intended_price: float | None,
    spread_bps_at_decision: float | None,
    entry_price: float | None = None,
    exit_reason: str | None = None,
    realized_pnl_usd: float | None = None,
    pnl_gross_usd: float | None = None,
    fill_ts: datetime | None = None,
    entry_l2_snapshot: dict | None = None,
    raw: dict | None = None,
) -> None:
    """FILL_OUTCOME_LOG (mig308) — record ONE row per real broker fill leg.

    KILL-SWITCH + LIVE-ONLY: returns IMMEDIATELY (before ANY DB work or broker read)
    when the flag is off or the session is not live — byte-identical, no new SQL.
    FAIL-OPEN: the whole body is guarded; the INSERT runs inside a SAVEPOINT
    (``begin_nested``) so a write error rolls back ONLY the insert and never poisons
    the shared trade transaction. IDEMPOTENT: ``leg_seq`` is the next int per
    (session_id, side) and the INSERT is ``ON CONFLICT (session_id, side, leg_seq)
    DO NOTHING`` — a retried/repegged poll cannot double-insert. Stage-1 write-only.
    """
    # Kill-switch + live-mode gate FIRST — no DB work, no broker read when off/paper.
    if not getattr(settings, "chili_momentum_fill_log_enabled", False):
        return
    if str(getattr(sess, "mode", "") or "").lower() != "live":
        return
    try:
        from sqlalchemy import text as _text

        _raw_json = None
        if raw is not None:
            try:
                _raw_json = json.loads(json.dumps(raw, default=str)[:8000])
            except Exception:
                _raw_json = None
        params = {
            "session_id": int(sess.id),
            "user_id": sess.user_id,
            "symbol": sess.symbol,
            "side": str(side),
            "mode": "live",
            "asset_class": _fill_log_asset_class(sess),
            "execution_family": str(getattr(sess, "execution_family", "") or "") or None,
            "fill_source": str(fill_source),
            "broker_order_id": str(broker_order_id) if broker_order_id else None,
            "broker_fill_price": _float_or_none(fill_price),
            "qty": _float_or_none(qty),
            "fees_usd": _float_or_none(fees_usd),
            "order_status": str(order_status) if order_status else None,
            "fill_ts": fill_ts or _utcnow(),
            "realized_pnl_usd": _float_or_none(realized_pnl_usd),
            "pnl_gross_usd": _float_or_none(pnl_gross_usd),
            "intended_price": _float_or_none(intended_price),
            "entry_price": _float_or_none(entry_price),
            "spread_bps_at_decision": _float_or_none(spread_bps_at_decision),
            "exit_reason": (str(exit_reason)[:40] if exit_reason else None),
            "decision_packet_id": (
                int(le_dpid) if (le_dpid := _fill_log_decision_packet_id(sess)) is not None else None
            ),
            "entry_l2_snapshot_json": (
                json.loads(json.dumps(entry_l2_snapshot, default=str))
                if isinstance(entry_l2_snapshot, dict) else None
            ),
            "raw_json": _raw_json,
        }
        # SAVEPOINT: the insert (incl. its guarded leg_seq read) is fully isolated —
        # any error rolls back ONLY this nested block, leaving the trade txn clean.
        with db.begin_nested():
            # IDEMPOTENT BY broker_order_id (2026-06-27 duplicate-fill root cause):
            # a recycled watcher / repeg / late-fill sweep can re-poll the SAME real
            # broker order — and because leg_seq is MAX+1 per (session, side), the
            # ON CONFLICT (session_id, side, leg_seq) clause can NEVER catch that
            # repeat (it always picks a fresh leg_seq), double-logging one fill.
            # When the broker assigned an order id, SKIP cleanly if a row for this
            # (session, side, broker_order_id) already exists — the leg is logged.
            # broker_order_id IS NULL (paper / synthetic / broker-zero escape) keeps
            # the leg_seq path unchanged. Same SAVEPOINT, no new flag (already gated
            # by chili_momentum_fill_log_enabled above).
            if params["broker_order_id"] is not None:
                _dup = db.execute(
                    _text(
                        "SELECT 1 FROM momentum_fill_outcomes "
                        "WHERE session_id = :sid AND side = :side "
                        "AND broker_order_id = :boid LIMIT 1"
                    ),
                    {
                        "sid": params["session_id"],
                        "side": params["side"],
                        "boid": params["broker_order_id"],
                    },
                ).scalar()
                if _dup is not None:
                    return  # already logged this broker order leg — idempotent skip
            row = db.execute(
                _text(
                    "SELECT COALESCE(MAX(leg_seq), -1) + 1 FROM momentum_fill_outcomes "
                    "WHERE session_id = :sid AND side = :side"
                ),
                {"sid": params["session_id"], "side": params["side"]},
            ).scalar()
            params["leg_seq"] = int(row or 0)
            db.execute(
                _text(
                    "INSERT INTO momentum_fill_outcomes ("
                    " session_id, leg_seq, user_id, symbol, side, mode, asset_class,"
                    " execution_family, fill_source, broker_order_id, broker_fill_price,"
                    " qty, fees_usd, order_status, fill_ts, realized_pnl_usd, pnl_gross_usd,"
                    " intended_price, entry_price, spread_bps_at_decision, exit_reason,"
                    " decision_packet_id, entry_l2_snapshot_json, raw_json"
                    ") VALUES ("
                    " :session_id, :leg_seq, :user_id, :symbol, :side, :mode, :asset_class,"
                    " :execution_family, :fill_source, :broker_order_id, :broker_fill_price,"
                    " :qty, :fees_usd, :order_status, :fill_ts, :realized_pnl_usd, :pnl_gross_usd,"
                    " :intended_price, :entry_price, :spread_bps_at_decision, :exit_reason,"
                    " :decision_packet_id,"
                    " CAST(:entry_l2_snapshot_json AS JSONB), CAST(:raw_json AS JSONB)"
                    ") ON CONFLICT (session_id, side, leg_seq) DO NOTHING"
                ),
                {
                    **params,
                    "entry_l2_snapshot_json": (
                        json.dumps(params["entry_l2_snapshot_json"])
                        if params["entry_l2_snapshot_json"] is not None else None
                    ),
                    "raw_json": (
                        json.dumps(params["raw_json"]) if params["raw_json"] is not None else None
                    ),
                },
            )
    except Exception:
        _log.debug("[momentum_fill_log] write skipped session=%s side=%s", sess.id, side, exc_info=True)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe_mult(x: Any) -> float:
    """LOW-7 fail-NEUTRAL: sanitize ONE size-multiplier factor before it enters the product.
    A non-finite (NaN/inf) or NEGATIVE multiplier from any upstream helper is coerced to 1.0
    (neutral) so it cannot poison the combined product — a negative would flip the sign of the
    budget and a NaN would make the whole budget NaN, each silently KILLING the fill instead of
    sizing it. A valid (finite, >= 0) factor passes through unchanged, so the happy path and the
    downstream 3x clamp + max_notional ceiling are untouched."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(v) or v < 0.0:
        return 1.0
    return v


def _order_total_fees_usd(no: Any) -> float | None:
    """Broker-reported commission on an order, from the raw payload.

    Coinbase Advanced Trade returns ``total_fees`` on every order; Robinhood
    equities have no such field (commission ~0) so this returns None and the
    caller books 0 — same as the old behavior. This is the live half of the
    2026-06-13 fee-truth fix: fees the broker actually charged must reach the
    economic ledger and the session PnL, not be silently dropped.
    """
    try:
        raw = getattr(no, "raw", None) or {}
        val = raw.get("total_fees")
        if val is None:
            val = raw.get("totalFees")
        if val is None:
            return None
        fee = float(val)
    except (TypeError, ValueError):
        return None
    return fee if math.isfinite(fee) and fee >= 0.0 else None


def _record_live_exit_intent_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    reason: str,
    product_id: str,
    quantity: float,
    client_order_id: str | None,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        qty = _float_or_none(quantity)
        bid_f = _float_or_none(bid)
        ask_f = _float_or_none(ask)
        mid_f = _float_or_none(mid)
        spread_bps = None
        if bid_f is not None and ask_f is not None and mid_f and mid_f > 0:
            spread_bps = max(0.0, (ask_f - bid_f) / mid_f * 10_000.0)
        pos = le.get("position")
        pos = pos if isinstance(pos, dict) else {}
        ref_px = bid_f if bid_f is not None else mid_f
        intent: dict[str, Any] = {
            "surface": "momentum_live_runner_exit",
            "session_id": int(sess.id),
            "state": sess.state,
            "side": "sell",
            "order_type": "market",
            "reason": reason,
            "product_id": product_id,
            "quantity": qty,
            "base_size": _fmt_base_size(qty) if qty and qty > 0 else None,
            "client_order_id": client_order_id,
            "bid": bid_f,
            "ask": ask_f,
            "mid": mid_f,
            "spread_bps": spread_bps,
            "reference_notional_usd": (qty * ref_px) if qty is not None and ref_px is not None else None,
            "avg_entry_price": _float_or_none(pos.get("avg_entry_price")),
            "stop_price": _float_or_none(pos.get("stop_price")),
            "target_price": _float_or_none(pos.get("target_price")),
            "opened_at_utc": pos.get("opened_at_utc"),
            "recorded_at_utc": _utcnow().isoformat(),
        }
        if extra:
            intent.update(dict(extra))
        intents = list(le.get("exit_execution_intents") or [])
        intents.append(intent)
        le["exit_execution_intents"] = intents[-10:]
        le["last_exit_intent"] = intent
        packet_id = int(le["entry_decision_packet_id"]) if le.get("entry_decision_packet_id") else None
        record_packet_execution_intent(db, packet_id, intent)
    except Exception:
        _log.debug("live exit intent hook skipped session=%s reason=%s", sess.id, reason, exc_info=True)


def _submit_live_market_exit(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    product_id: str,
    quantity: float,
    client_order_id: str,
    reason: str,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    extra: dict[str, Any] | None = None,
    hard_floor_price: float | None = None,
) -> dict[str, Any]:
    now = _utcnow()
    attempts = int(le.get("exit_submit_attempts", 0) or 0)

    # Backoff gate — do NOT place another broker order until the scheduled
    # retry time. Returns a synthetic deferred result (no broker call, no
    # attempt increment) so the caller stays in its exit state and retries
    # on a later pulse without hammering the venue API.
    next_retry_raw = le.get("exit_next_retry_at_utc")
    if next_retry_raw:
        try:
            next_retry = datetime.fromisoformat(
                str(next_retry_raw).replace("Z", "+00:00")
            ).replace(tzinfo=None)
            if now < next_retry:
                return {
                    "ok": False,
                    "error": "exit_retry_backoff",
                    "deferred": True,
                    "retry_at_utc": next_retry.isoformat(),
                    "attempts": attempts,
                }
        except Exception:
            pass

    # Max-attempts cap — stop submitting and signal escalation to the
    # broker-zero / dust reconcile (handled in _live_exit_submit_succeeded).
    if attempts >= _EXIT_SUBMIT_MAX_ATTEMPTS:
        return {
            "ok": False,
            "error": "exit_retry_cap_exceeded",
            "cap_exceeded": True,
            "attempts": attempts,
        }

    # Sell-into-strength invariant: a resting scale-out limit may be working this
    # position. Cancel it FIRST and adopt any fill it caught, then clamp the sell
    # quantity to the true remainder — the one chokepoint every exit path crosses.
    quantity = _cancel_scale_limit_and_clamp(
        db, sess, adapter, le=le, requested_qty=quantity, reason=reason
    )
    if quantity <= 0:
        _emit(db, sess, "live_exit_noop_scale_limit_consumed", {"reason": reason})
        return {"ok": False, "error": "no_remaining_quantity", "noop": True}
    # AGENTIC COVERING-SELL RELEASE (2026-06-23 strand fix): the TRACKED scale-out is
    # cancelled above, but an UNTRACKED resting sell — or a cancel not yet propagated at
    # the broker — still locks shares, so the full exit is rejected 'Not enough shares to
    # sell' (-> 8 retries -> live_error -> stranded naked). Cancel ANY working agentic sell
    # for this symbol so the whole position is sellable; re-runs each attempt to clear the
    # propagation race. Agentic-only; spot/crypto byte-identical. Kill-switch default-ON.
    if (
        bool(getattr(settings, "chili_momentum_exit_cancel_covering_sells", True))
        and normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
    ):
        _ncov = _cancel_agentic_covering_sells(adapter, sess.symbol)
        if _ncov:
            _emit(db, sess, "live_exit_cancelled_covering_sells", {"count": _ncov, "reason": reason})
    # BROKER-QTY CLAMP (2026-06-12 quant pass v2 A6): sell what the BROKER
    # says we hold, not what the session remembers — selling phantom shares
    # produced the "Not enough shares to sell"/"cannot be sold short" reject
    # storms (37/40 RH rejects, 8 stuck Alpaca sessions). A SUCCESSFUL fetch
    # showing less than requested clamps; a failed fetch changes nothing.
    try:
        _bq = adapter.get_position_quantity(product_id) if hasattr(adapter, "get_position_quantity") else None
        if _bq is None:
            _fam = normalize_execution_family(sess.execution_family)
            if _fam == EXECUTION_FAMILY_ROBINHOOD_SPOT:
                from ...broker_service import get_open_position_quantity as _rh_qty

                _bq = _rh_qty(sess.symbol)
            elif _fam == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
                # Agentic fallback (mirror of the spot branch): if the adapter on this
                # image lacks get_position_quantity, read the agentic book directly so
                # the clamp still fires. Fail-safe: any error -> _bq stays None ->
                # behavior unchanged. spot/crypto paths are untouched.
                try:
                    if hasattr(adapter, "get_position_quantity"):
                        _bq = adapter.get_position_quantity(product_id)
                except Exception:
                    _bq = None
        if _bq is not None and float(_bq) >= 0 and float(_bq) < float(quantity) - 1e-9:
            _emit(db, sess, "live_exit_qty_clamped_to_broker", {
                "requested": float(quantity), "broker_qty": float(_bq), "reason": reason,
            })
            quantity = float(_bq)
            if quantity <= 0:
                return {"ok": False, "error": "no_remaining_quantity", "noop": True,
                        "broker_zero": True}
    except Exception:
        pass

    _record_live_exit_intent_safe(
        db,
        sess,
        le=le,
        reason=reason,
        product_id=product_id,
        quantity=quantity,
        client_order_id=client_order_id,
        bid=bid,
        ask=ask,
        mid=mid,
        extra=extra,
    )
    attempts += 1
    le["exit_submit_attempts"] = attempts
    le["exit_next_retry_at_utc"] = (
        now + timedelta(seconds=_exit_submit_backoff_seconds(attempts))
    ).isoformat()
    # EXIT LADDER (2026-06-12 exit study: 30/70 exits filled WORSE than the
    # planned stop, −15.7R ≈ $428/wk — naked market sells crossing wide books).
    # Attempt 1: marketable LIMIT at bid − guard (mirror of the entry's
    # ask-guard; sits AT/UNDER the bid = immediately matchable, slip capped).
    # Attempt 2: 4× guard. Attempt 3+: market (the old behavior as the floor).
    # Kill-switch/operator/EOD flatten intent = OUT NOW = market immediately.
    # An unfilled limit is re-pegged by the poll loop (repeg knob) — each
    # repeg re-enters here with attempts+1, walking the ladder.
    # HOURS-AWARE EQUITY EXIT (2026-06-16 — BEEM/AHMA stranded-long bug). In
    # premarket/after-hours Robinhood REJECTS a regular-hours order ("no order_id"),
    # so a premarket equity entry whose stop breached could NOT be flattened — the
    # sell never placed → 8 rejects → live_error → naked long with no working stop
    # (exactly the AHMA position the operator had to exit by hand). The ENTRY and
    # scale-out already pass the ext-hours overrides (which DO work premarket); only
    # this reactive exit was hours-blind. Mirror the entry idiom: when an RH equity
    # session is non-regular, pass the RH-only overrides AND force a marketable LIMIT
    # (a bare market order is rejected in extended hours even WITH the override).
    # Overrides are RH-only kwargs — pass them ONLY for robinhood_spot (the coinbase
    # adapter does not accept them; crypto + regular-hours stay byte-identical).
    _exit_extended = False
    if normalize_execution_family(sess.execution_family) in (
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    ):
        try:
            from .market_profile import market_session_now

            _exit_extended = market_session_now(sess.symbol) != "regular"
        except Exception:
            _exit_extended = False
    # ⚠️ 2026-06-23 STRANDED-EXIT FIX: use extended_hours, NOT all_day_hours. all_day_hours
    # is RH's 24-HOUR market — only valid for the few 24h-eligible names; for ~every Ross
    # low-float mover RH rejects it ("untradable for 24 hour trading"), and the AGENTIC MCP
    # adapter (this lane) has NO all_day_hours->fallback (robinhood_spot.py:909 does; the MCP
    # rail does not), so a premarket/after-hours STOP-OUT could not flatten -> naked stranded
    # long (the exact AHMA/SMCX class). extended_hours covers pre+regular+post (the lane's
    # 04:00-20:00 ET window) and is accepted for ALL equities. Mirrors the entry-side fix.
    _ext_kwargs: dict[str, Any] = (
        {"market_hours_override": "extended_hours", "extended_hours_override": True}
        if _exit_extended
        else {}
    )

    # HARD MAX-LOSS-CIRCUIT FLOOR (2026-06-17): when the caller supplies an absolute
    # loss-anchored floor (avg - K*stop_distance), OVERRIDE the entire bid-relative
    # ladder — skip the attempt<=2 bid-guard branch, the extended-hours 8×-bid branch,
    # AND the attempt-3+ naked-MARKET fallback. Place a SINGLE marketable-but-CAPPED
    # sell at exactly the floor (no repeg, partial fills final, unfilled remainder
    # bounded by the existing structural stop). Anchored to entry+structural-risk, NOT
    # a falling bid, so a deep gap-through fill is mechanically impossible. Pass the RH
    # ext-hours overrides through (_ext_kwargs) so a premarket equity floor still places.
    # hard_floor_price=None => byte-identical legacy ladder for every existing caller.
    _floor_override = None
    try:
        if hard_floor_price is not None and float(hard_floor_price) > 0:
            _floor_override = float(hard_floor_price)
    except (TypeError, ValueError):
        _floor_override = None
    _urgent = str(reason or "") in ("kill_switch_flatten", "operator_flatten")
    _lim_px = None
    if _floor_override is not None:
        _lim_px = _floor_override
    elif not _urgent and attempts <= 2:
        _g = (_notional_guard_multiplier() - 1.0) * (1.0 if attempts <= 1 else 4.0)
        _ref = None
        for _cand in (bid, mid):
            try:
                if _cand and float(_cand) > 0:
                    _ref = float(_cand)
                    break
            except (TypeError, ValueError):
                continue
        if _ref is not None:
            _lim_px = _ref * (1.0 - _g)
    # Extended-hours equity: a market order is rejected outright, so ALWAYS price a
    # marketable limit — even on an urgent flatten or the attempt-3+ market fallback.
    # Cross the bid HARD (8× guard) so it fills immediately, like the market order it
    # replaces. Regular hours / crypto: _exit_extended is False → branch unchanged.
    if _floor_override is None and _exit_extended and _lim_px is None:
        _ref = None
        for _cand in (bid, mid):
            try:
                if _cand and float(_cand) > 0:
                    _ref = float(_cand)
                    break
            except (TypeError, ValueError):
                continue
        if _ref is not None:
            _lim_px = _ref * (1.0 - (_notional_guard_multiplier() - 1.0) * 8.0)
    if _lim_px is not None and hasattr(adapter, "place_limit_order_gtc"):
        # TICK-VALID SELL PRICE (SMCX premarket stranded-position fix, 2026-06-22): an
        # RH-agentic equity limit finer than a penny on a $1+ stock is rejected by
        # place_equity_order (SEC/NMS Rule 612) -> isError -> exit retry cap exhausted
        # -> STRANDED POSITION. This reactive trail-stop priced bid*0.9975 =
        # 11.98*0.9975 = 11.95005 (sub-penny via the attempts<=2 rung) and was rejected,
        # while the ENTRY (_fmt_limit_price_buy) and the resting SCALE-OUT
        # (_fmt_limit_price_sell) both penny-round and DID fill premarket. Use the SAME
        # penny-FLOOR helper for RH equity sells (a lower sell limit is strictly MORE
        # marketable -> never starves the fill). Crypto (coinbase) keeps its fine
        # 6-decimal precision byte-identical.
        _is_rh_equity_exit = normalize_execution_family(sess.execution_family) in (
            EXECUTION_FAMILY_ROBINHOOD_SPOT,
            EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        )
        _exit_limit_str = (
            _fmt_limit_price_sell(_lim_px)
            if _is_rh_equity_exit
            else f"{_lim_px:.6f}".rstrip("0").rstrip(".")
        )
        _lim_kwargs: dict[str, Any] = dict(
            product_id=product_id,
            side="sell",
            base_size=_fmt_base_size(quantity),
            limit_price=_exit_limit_str,
            client_order_id=client_order_id,
        )
        if _ext_kwargs:
            _lim_kwargs["extended_hours"] = True
            _lim_kwargs.update(_ext_kwargs)
        result = adapter.place_limit_order_gtc(**_lim_kwargs) or {}
        le["exit_order_type"] = "limit"
        le["exit_limit_price"] = _lim_px
        if _floor_override is not None:
            le["exit_floor_order"] = True
        if _exit_extended:
            le["exit_session_extended"] = True
    else:
        _mkt_kwargs: dict[str, Any] = dict(
            product_id=product_id,
            side="sell",
            base_size=_fmt_base_size(quantity),
            client_order_id=client_order_id,
        )
        if _ext_kwargs:
            _mkt_kwargs.update(_ext_kwargs)
        result = adapter.place_market_order(**_mkt_kwargs) or {}
        le["exit_order_type"] = "market"
        le.pop("exit_limit_price", None)
    le["exit_order_id"] = result.get("order_id")
    le["exit_client_order_id"] = result.get("client_order_id") or client_order_id
    le["exit_place_result"] = {"ok": result.get("ok"), "error": result.get("error")}
    if result.get("ok"):
        le["pending_exit_reason"] = reason
        le["pending_exit_quantity"] = float(quantity)
        le["pending_exit_submitted_at_utc"] = now.isoformat()
        # Accepted by the broker — reset the retry state so a later,
        # independent exit (e.g. re-exit of a remainder) starts fresh.
        le["exit_submit_attempts"] = 0
        le.pop("exit_next_retry_at_utc", None)
    # Persist the counter/backoff state so it survives across pulses (the
    # caller's flush/commit writes sess.risk_snapshot_json to the DB).
    _commit_le(sess, le)
    return result


def _is_unsellable_dust(symbol: str, qty: float) -> bool:
    """True when `qty` of `symbol`'s base asset is below what Coinbase will accept as
    a SELL — below the product base_min_size, OR whose notional (qty x price) is below
    quote_min_size (typically $1). Such a residual can never be flattened, so for
    reconcile purposes it is effectively ZERO: leaving it 'live' makes the exit loop
    re-submit doomed sells forever ('Insufficient balance'). Conservative — any failure
    to determine the venue minimums returns False so a real, sellable position is never
    false-reconciled. (This is the dust that wedged session 52 / CTSI: 3.65 units ~=
    $0.09, below the $1 quote_min but above the strict ~0 check.)"""
    try:
        if not qty or float(qty) <= 0.0:
            return True
        from ...coinbase_service import get_coinbase_rest_client

        client = get_coinbase_rest_client()
        if client is None:
            return False
        sym = str(symbol or "").upper()
        product_id = sym if "-" in sym else f"{sym}-USD"
        prod = client.get_product(product_id)
        pd = prod if isinstance(prod, dict) else (getattr(prod, "__dict__", {}) or {})
        base_min = float(pd.get("base_min_size") or 0.0)
        quote_min = float(pd.get("quote_min_size") or 0.0)
        price = float(pd.get("price") or 0.0)
        if base_min > 0.0 and float(qty) < base_min:
            return True  # below the venue's minimum sellable size
        if quote_min > 0.0 and price > 0.0 and (float(qty) * price) < quote_min:
            return True  # notional below the venue's minimum order value
        return False
    except Exception:
        return False


def _broker_balance_confirms_zero(symbol: str) -> bool:
    """True when a SUCCESSFUL Coinbase fetch shows the symbol's base asset is ~0 OR an
    UNSELLABLE-DUST residual (below the venue's min sell size / notional). A
    failed/disconnected fetch returns False so it never triggers a false reconcile
    (mirrors the M5a safe-fetch rule). (crypto/coinbase only)

    The strict ~0 (1e-9) check alone was DEFEATED by dust: a position sold down to a
    fractional remainder the venue rejects as an order (CTSI 3.65 units = $0.09 < $1
    quote_min) left the exit loop re-submitting doomed sells forever. Dust IS
    effectively zero for reconcile purposes."""
    try:
        from ...coinbase_service import get_accounts_raw

        accts = get_accounts_raw()
        if not accts:
            return False  # disconnected / fetch failed -> unknown, do NOT reconcile
        base = str(symbol or "").upper().split("-", 1)[0]
        for a in accts:
            if not isinstance(a, dict):
                continue
            if str(a.get("currency") or "").upper() != base:
                continue
            bal = a.get("available_balance", {})
            hold = a.get("hold", {})
            v = (
                float((bal.get("value") if isinstance(bal, dict) else 0) or 0)
                + float((hold.get("value") if isinstance(hold, dict) else 0) or 0)
            )
            if v <= 1e-9:
                return True
            return _is_unsellable_dust(symbol, v)  # non-zero but unsellable dust == zero
        return True  # base wallet absent in a successful fetch -> confirmed zero
    except Exception:
        return False


def _broker_position_confirms_zero(sess: TradingAutomationSession) -> bool:
    """Family-agnostic broker-truth flat check for the exit-retry-cap reconcile.
    Coinbase: balance/dust check (existing). Robinhood: open-position quantity
    (2026-06-11 INDP: the reconcile was Coinbase-only, so an RH phantom position
    looped 8 flatten retries into LIVE_ERROR while the broker was already flat).
    Unknown family / failed fetch -> False (fail safe, surface the error)."""
    fam = normalize_execution_family(sess.execution_family)
    if fam == EXECUTION_FAMILY_COINBASE_SPOT:
        return _broker_balance_confirms_zero(sess.symbol)
    if fam == EXECUTION_FAMILY_ROBINHOOD_SPOT:
        try:
            from ...broker_service import get_open_position_quantity

            q = get_open_position_quantity(sess.symbol)
        except Exception:
            return False
        return q is not None and float(q) <= 1e-6
    return False


def _live_exit_submit_succeeded(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    result: dict[str, Any],
    reason: str,
) -> bool:
    # Backoff deferral — no broker order was placed this pulse (rate-limited
    # by _submit_live_market_exit). Not a real failure: stay in the exit
    # state and retry after the backoff window WITHOUT recording a failure
    # or emitting an event (avoids the per-pulse event spam that itself was
    # part of the wedged-session problem). (2026-06-07 audit.)
    if result.get("deferred"):
        return False

    # Max-attempts cap reached — stop re-submitting and escalate. If the
    # broker is flat (or only unsellable dust remains) the position already
    # left, so reconcile to EXITED; otherwise a real sellable position keeps
    # failing to flatten for a non-balance reason → surface a terminal error
    # for operator attention instead of looping forever.
    if result.get("cap_exceeded"):
        _emit(
            db, sess, "live_exit_retry_cap_exceeded",
            {
                "reason": reason,
                "attempts": result.get("attempts"),
                "max_attempts": _EXIT_SUBMIT_MAX_ATTEMPTS,
            },
        )
        if _broker_position_confirms_zero(sess):
            le["position"] = None
            le["last_exit_reason"] = (reason or "exit") + "_retry_cap_broker_zero_reconcile"
            le.pop("pending_exit_reason", None)
            le.pop("pending_exit_quantity", None)
            le.pop("pending_exit_submitted_at_utc", None)
            le["exit_submit_attempts"] = 0
            le.pop("exit_next_retry_at_utc", None)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_EXITED)
            _emit(
                db, sess, "live_exit_reconciled_broker_zero",
                {
                    "reason": reason,
                    "note": "exit retry cap reached and broker holds 0/dust — reconciled to exited",
                },
            )
            return True
        # GENUINELY STRANDED POSITION (2026-06-16): the exit hit the retry cap AND
        # the broker still HOLDS the position (not zero/dust) — a real naked long with
        # no working exit (this is what stranded BEEM/AHMA premarket before the
        # hours-aware-exit fix). Emit a LOUD, distinct alert so it is never lost in the
        # cosmetic arm-twin live_errors (those are blocked AT ARM — no position, no
        # money at risk). The operator's monitoring keys on this event to take over.
        _held_qty = None
        try:
            from ...broker_service import get_open_position_quantity as _gq

            if normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_ROBINHOOD_SPOT:
                _held_qty = _gq(sess.symbol)
        except Exception:
            _held_qty = None
        _emit(
            db, sess, "live_exit_stranded_position",
            {
                "severity": "critical",
                "reason": reason,
                "symbol": sess.symbol,
                "execution_family": sess.execution_family,
                "broker_held_qty": (float(_held_qty) if _held_qty is not None else None),
                "attempts": result.get("attempts"),
                "last_error": (le.get("exit_place_result") or {}).get("error"),
                "note": (
                    "exit retry cap reached and broker STILL HOLDS the position — "
                    "naked long, no working exit; operator action required"
                ),
            },
        )
        _log.error(
            "[momentum_live] STRANDED POSITION sess=%s %s qty=%s — exit retry cap "
            "exceeded and broker still holds; needs operator flatten",
            sess.id, sess.symbol, _held_qty,
        )
        le["last_exit_submit_failed"] = {
            "reason": reason,
            "error": "exit_retry_cap_exceeded",
            "attempts": result.get("attempts"),
            "broker_held_qty": (float(_held_qty) if _held_qty is not None else None),
            "stranded": True,
            "recorded_at_utc": _utcnow().isoformat(),
        }
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        return False

    if result.get("ok") and le.get("exit_order_id"):
        return True
    missing_order_id = bool(result.get("ok")) and not le.get("exit_order_id")
    # BUGFIX: an exit/bailout sell that fails with "insufficient balance" while the
    # broker CONFIRMS zero means the position already left (sold externally / a
    # prior fill we missed) — retrying loops forever on insufficient balance and
    # pins the slot. Reconcile to EXITED instead of spinning. (coinbase only;
    # confirmed-zero only — never on a failed balance fetch.)
    _err = str(result.get("error") or "").lower()
    if (
        ("insufficient balance" in _err or "insufficient_balance" in _err)
        and normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_COINBASE_SPOT
        and _broker_balance_confirms_zero(sess.symbol)
    ):
        le["position"] = None
        le["last_exit_reason"] = (reason or "exit") + "_broker_zero_reconcile"
        le.pop("pending_exit_reason", None)
        le.pop("pending_exit_quantity", None)
        le.pop("pending_exit_submitted_at_utc", None)
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_EXITED)
        _emit(
            db, sess, "live_exit_reconciled_broker_zero",
            {"reason": reason, "note": "broker holds 0 — position already exited externally; not retrying"},
        )
        return True
    # BUGFIX (2026-06-16, the SLNH/LION phantom spin): the broker-qty clamp in
    # _submit_live_market_exit returns broker_zero=True ONLY when a SUCCESSFUL broker
    # read found the held qty at 0 (None / a failed fetch never sets it). That means
    # the position is a PHANTOM — the session still thinks it holds shares, but the
    # broker holds none (sold externally, a prior fill we missed, or the entry never
    # actually filled). The generic failed path below returns False, so the trail /
    # max-hold loop re-submits the SAME exit every tick forever (SLNH sess 5033 spun
    # no_remaining_quantity for HOURS; LION sess 4996 the same) — pinning the slot AND
    # showing a phantom position + phantom unrealized P&L in the cockpit (operator saw
    # "profit" on a position that did not exist). Confirm with a second INDEPENDENT
    # broker read (same belt-and-suspenders as the retry-cap reconcile @766) so a
    # one-off spurious 0 can't close a real position, then reconcile to EXITED instead
    # of spinning. Family-agnostic (Robinhood + Coinbase); never fires on a None/failed
    # read (broker_zero is unset) so an API hiccup degrades to the safe retry path.
    # AREA A — BROKER-ZERO CLOSE (2026-06-25, the FCUV live_bailout loop). The
    # broker-qty clamp sets broker_zero=True ONLY when a SUCCESSFUL broker read found
    # the held qty <= 0 (None / a failed/exception fetch NEVER sets it — see
    # _submit_live_market_exit @680-687). That successful read IS the confirmed-zero.
    # The legacy second read (_broker_position_confirms_zero) does NOT handle the
    # robinhood_agentic_mcp family (it returns False for it @933-950), so a
    # broker-FLAT agentic bailout (FCUV sess 8791) re-confirmed False every tick,
    # fell through to the generic failure path, and looped live_bailout FOREVER —
    # pinning a concurrency slot on a position the broker already holds at 0.
    # FIX (flag-gated, default-ON): trust broker_zero=True from the successful clamp
    # read and reconcile to EXITED WITHOUT the second read. The second read only ADDS
    # a failure dependency for a result we already confirmed. Fail-safe is preserved:
    # broker_zero is unset on any None/failed/exception read, so an API hiccup still
    # degrades to the safe retry path (never closes on uncertainty). flag-OFF =
    # byte-identical legacy double-read.
    _trust_clamp_zero = bool(
        getattr(settings, "chili_momentum_broker_zero_trust_clamp_enabled", True)
    )
    # FIX B (2026-06-25, the FCUV live_bailout phantom): require N CONSECUTIVE
    # successful broker_zero=True clamp reads before reconciling — a single spurious
    # 0 must NEVER abandon a real position. broker_zero is set ONLY on a SUCCESSFUL
    # clamp read (None/failed/exception never set it — _submit_live_market_exit
    # @680-688), so any uncertainty already degrades to the safe retry path. Here we
    # additionally count consecutive confirmations across exit pulses: a non-zero /
    # absent broker_zero resets the streak (below), so only a STABLE confirmed-flat
    # broker (FCUV: broker holds 0 every pulse) reaches N and closes the loop. This is
    # what makes the bailout path satisfy the confirmed-flat reconcile (the agentic
    # family fell through the legacy second read and looped live_bailout forever).
    if result.get("broker_zero") is True:
        _confirm_n = 1
        try:
            _confirm_n = max(1, int(getattr(settings, "chili_momentum_broker_zero_confirm_reads", 2) or 2))
        except (TypeError, ValueError):
            _confirm_n = 2
        _streak = int(le.get("broker_zero_confirm_streak") or 0) + 1
        le["broker_zero_confirm_streak"] = _streak
        # The legacy single-read trust-clamp (flag default-ON) is preserved as the
        # _confirm_n=1 case; the second independent read remains available for the
        # flag-OFF (legacy double-read) path. Reconcile only once BOTH the consecutive-
        # confirm count is met AND the trust/second-read condition holds.
        _read_ok = _trust_clamp_zero or _broker_position_confirms_zero(sess)
        if _streak < _confirm_n:
            # Not yet confirmed flat enough times — stay in the exit/bailout state,
            # arm a short retry, and re-confirm on the next pulse. NOT a failure: do
            # not record last_exit_submit_failed (that is the spam FCUV produced).
            le["exit_next_retry_at_utc"] = (
                _utcnow() + timedelta(seconds=_exit_submit_backoff_seconds(int(le.get("exit_submit_attempts", 0) or 0)))
            ).isoformat()
            _commit_le(sess, le)
            _emit(
                db, sess, "live_exit_broker_zero_confirming",
                {
                    "reason": reason,
                    "confirm_streak": _streak,
                    "confirm_reads_required": _confirm_n,
                    "note": "broker-qty clamp read 0 — confirming flat over N consecutive reads before reconcile",
                },
            )
            return False
        if _read_ok:
            le["position"] = None
            le["last_exit_reason"] = (reason or "exit") + "_broker_zero_reconcile"
            le.pop("pending_exit_reason", None)
            le.pop("pending_exit_quantity", None)
            le.pop("pending_exit_submitted_at_utc", None)
            le.pop("broker_zero_confirm_streak", None)
            le["exit_submit_attempts"] = 0
            le.pop("exit_next_retry_at_utc", None)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_EXITED)
            _emit(
                db, sess, "live_exit_reconciled_broker_zero",
                {
                    "reason": reason,
                    "error": result.get("error"),
                    "trusted_clamp_read": bool(_trust_clamp_zero),
                    "confirm_streak": _streak,
                    "confirm_reads_required": _confirm_n,
                    "note": (
                        "broker-qty clamp read 0 on N consecutive reads (successful read = "
                        "confirmed flat) — phantom/already-exited position; reconciled to "
                        "EXITED, not retrying (was spinning no_remaining_quantity / live_bailout)"
                    ),
                },
            )
            return True
    else:
        # Any read that is NOT a successful confirmed-zero (a real held qty, a failed
        # fetch, or an absent broker_zero) breaks the confirmation chain — reset so a
        # later spurious 0 cannot accumulate against stale prior confirmations.
        if le.get("broker_zero_confirm_streak"):
            le.pop("broker_zero_confirm_streak", None)
    failed = {
        "reason": reason,
        "result": {
            "ok": result.get("ok"),
            "error": result.get("error") or ("missing_exit_order_id" if missing_order_id else None),
        },
        "exit_client_order_id": le.get("exit_client_order_id"),
        "recorded_at_utc": _utcnow().isoformat(),
    }
    le["last_exit_submit_failed"] = failed
    le.pop("pending_exit_reason", None)
    le.pop("pending_exit_quantity", None)
    le.pop("pending_exit_submitted_at_utc", None)
    _commit_le(sess, le)
    _emit(db, sess, "live_exit_submit_failed", failed)
    return False


def _order_done_for_exit(no: NormalizedOrder) -> bool:
    st = (no.status or "").lower()
    if st in ("filled", "done", "closed"):
        return float(no.filled_size or 0.0) > 1e-12 and no.average_filled_price is not None
    if no.filled_size > 1e-12:
        return st in ("cancelled", "canceled", "expired", "failed")
    return False


def _order_terminal_without_exit_fill(no: NormalizedOrder) -> bool:
    st = (no.status or "").lower()
    if st in ("cancelled", "canceled", "expired", "failed", "rejected"):
        return float(no.filled_size or 0.0) <= 1e-12
    if st in ("filled", "done", "closed"):
        return float(no.filled_size or 0.0) <= 1e-12 or no.average_filled_price is None
    return False


def _poll_live_exit_fill(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    reason: str,
    quantity: float,
) -> dict[str, Any]:
    oid = le.get("exit_order_id")
    if not oid:
        _emit(db, sess, "live_exit_pending_unconfirmed", {"reason": reason, "why": "missing_exit_order_id"})
        return {"filled": False, "pending": True, "why": "missing_exit_order_id"}
    try:
        no, _ = adapter.get_order(str(oid))
    except Exception:
        _log.debug("live exit order poll failed session=%s order_id=%s", sess.id, oid, exc_info=True)
        no = None
    if no is None:
        _emit(db, sess, "live_exit_pending_unconfirmed", {"reason": reason, "order_id": oid, "why": "order_missing"})
        return {"filled": False, "pending": True, "why": "order_missing"}

    filled_size = float(no.filled_size or 0.0)
    avg_px = _float_or_none(no.average_filled_price)
    full_fill = _order_done_for_exit(no) and avg_px is not None and filled_size + 1e-12 >= float(quantity) * 0.999
    # FILL-BY-SIZE (2026-06-12 SMU/RZLV phantoms): RH kept reporting the stop
    # sell as status "open" while filled_size was already FULL — the status-
    # string gate spun live_exit_pending_confirmation forever and the session
    # held a phantom position. An order that has filled its full size with a
    # known average price IS done, whatever the status string says.
    if not full_fill and avg_px is not None and filled_size + 1e-12 >= float(quantity) * 0.999:
        full_fill = True
    if full_fill:
        le.pop("exit_pending_first_seen_utc", None)
        # Fee truth (2026-06-13): stash the broker-reported commission so the
        # completion fn (which never sees the order object) books it into the
        # ledger and nets it out of session PnL. le is the existing poll →
        # complete side channel — no caller signatures change.
        le["last_exit_fee_usd"] = _order_total_fees_usd(no)
        # FILL_OUTCOME_LOG (mig308): stash the REAL polled broker exit truth so the
        # completer flags fill_source='broker_confirmed' (vs the reconstructed
        # broker-zero path). Side channel only — does not alter any behavior.
        le["last_exit_broker_truth"] = {
            "broker_order_id": str(oid) if oid else None,
            "order_status": no.status,
            "avg_px": avg_px,
            "filled_size": filled_size,
        }
        _commit_le(sess, le)
        return {"filled": True, "fill_price": avg_px, "filled_size": filled_size, "order_status": no.status}

    terminal_status = (no.status or "").lower() in ("filled", "done", "closed", "cancelled", "canceled", "expired", "failed")
    if terminal_status and filled_size > 1e-12 and avg_px is not None:
        le["last_exit_fee_usd"] = _order_total_fees_usd(no)
        le["last_exit_broker_truth"] = {
            "broker_order_id": str(oid) if oid else None,
            "order_status": no.status,
            "avg_px": avg_px,
            "filled_size": filled_size,
        }
        _commit_le(sess, le)
        return {
            "filled": False,
            "partial": True,
            "fill_price": avg_px,
            "filled_size": filled_size,
            "order_status": no.status,
        }

    if _order_terminal_without_exit_fill(no):
        failed = {
            "reason": reason,
            "order_id": oid,
            "order_status": no.status,
            "filled_size": filled_size,
            "recorded_at_utc": _utcnow().isoformat(),
        }
        le["last_exit_terminal_no_fill"] = failed
        le.pop("pending_exit_reason", None)
        le.pop("pending_exit_quantity", None)
        le.pop("pending_exit_submitted_at_utc", None)
        _commit_le(sess, le)
        _emit(db, sess, "live_exit_terminal_no_fill", failed)
        return {"filled": False, "failed": True, "why": "terminal_no_fill", "order_status": no.status}

    pending = {
        "reason": reason,
        "order_id": oid,
        "order_status": no.status,
        "filled_size": filled_size,
        "expected_quantity": float(quantity),
        "recorded_at_utc": _utcnow().isoformat(),
    }
    if filled_size > 1e-12:
        pending["why"] = "partial_exit_fill_pending"
        if avg_px is not None:
            pending["average_filled_price"] = avg_px
    else:
        pending["why"] = "exit_fill_pending"
    # LIMIT REPEG (2026-06-12 exit ladder): an exit LIMIT that hasn't filled
    # within the repeg window is resting above a falling market — cancel it
    # and clear pending state so the next pulse re-submits one rung down the
    # ladder (wider guard, then market). Market orders never repeg.
    # MAX-LOSS-CIRCUIT FLOOR (2026-06-17): a floor-anchored remainder must NOT chase
    # down — the floor IS the loss cap. Skip the repeg; the unfilled remainder is left
    # resting at the absolute floor (bounded by the existing structural stop). Keyed on
    # exit_floor_anchored, set ONLY by the circuit (RH path) — legacy limits unchanged.
    if le.get("exit_order_type") == "limit" and not le.get("exit_floor_anchored"):
        _sub_at = le.get("pending_exit_submitted_at_utc")
        try:
            _sub_age = (
                (_utcnow() - datetime.fromisoformat(str(_sub_at))).total_seconds()
                if _sub_at else 0.0
            )
        except (TypeError, ValueError):
            _sub_age = 0.0
        _repeg_s = float(getattr(settings, "chili_momentum_exit_limit_repeg_seconds", 20.0) or 20.0)
        if _repeg_s > 0 and _sub_age > _repeg_s and filled_size <= 1e-12:
            try:
                adapter.cancel_order(str(oid))
            except Exception:
                pass
            le.pop("exit_order_id", None)
            le.pop("exit_order_type", None)
            le.pop("pending_exit_reason", None)
            le.pop("pending_exit_quantity", None)
            le.pop("pending_exit_submitted_at_utc", None)
            le.pop("exit_pending_first_seen_utc", None)
            _commit_le(sess, le)
            _emit(db, sess, "live_exit_limit_repegged", {
                "reason": reason, "order_id": oid, "age_s": round(_sub_age, 1),
            })
            return {"filled": False, "repegged": True, "why": "limit_repeg"}

    # STUCK-PENDING ESCAPE (2026-06-12): if the order status never goes
    # terminal but the BROKER confirms the position is gone, the exit
    # happened — finalize instead of spinning forever. Deadline-gated and
    # broker-truth-gated (a failed positions fetch never reconciles).
    first_seen = le.get("exit_pending_first_seen_utc")
    if not first_seen:
        le["exit_pending_first_seen_utc"] = _utcnow().isoformat()
        _commit_le(sess, le)
    else:
        try:
            _age = (_utcnow() - datetime.fromisoformat(str(first_seen))).total_seconds()
        except (TypeError, ValueError):
            _age = 0.0
        if _age > 90.0 and _broker_position_confirms_zero(sess):
            fill_px = avg_px or _float_or_none(getattr(no, "price", None))
            le.pop("exit_pending_first_seen_utc", None)
            _commit_le(sess, le)
            _emit(db, sess, "live_exit_reconciled_broker_zero", {
                "reason": reason, "order_id": oid, "order_status": no.status,
                "fill_price": fill_px,
                "note": "order status never went terminal; broker confirms flat",
            })
            if fill_px is not None:
                return {"filled": True, "fill_price": fill_px,
                        "filled_size": filled_size or float(quantity),
                        "order_status": no.status, "reconciled": "broker_zero"}
    le["last_exit_pending_confirmation"] = pending
    _commit_le(sess, le)
    _emit(db, sess, "live_exit_pending_confirmation", pending)
    return {"filled": False, "pending": True, **pending}


def _complete_confirmed_live_exit(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    quantity: float,
    entry_price: float,
    fill_price: float,
    reason: str,
    slip_bps: float,
    sell_result: dict[str, Any] | None = None,
) -> float:
    pnl_gross = (float(fill_price) - float(entry_price)) * float(quantity)
    notional_basis = abs(float(entry_price) * float(quantity))
    # Fee truth (2026-06-13): net the broker-reported commissions out of the
    # session's realized PnL — the exit order's own fee (stashed by the poll)
    # plus any entry-side fee not yet booked (charged once, at the FULL exit,
    # so partial-exit accounting needs no fractional allocation). Sessions
    # whose exits reconcile without an order poll (broker-zero escape) book 0,
    # exactly the old behavior.
    _exit_fee = _float_or_none(le.pop("last_exit_fee_usd", None)) or 0.0
    _entry_fee = _float_or_none(le.pop("entry_fee_usd_unbooked", None)) or 0.0
    fees_usd = max(0.0, _exit_fee) + max(0.0, _entry_fee)
    pnl = pnl_gross - fees_usd
    le["fees_usd_total"] = float(le.get("fees_usd_total") or 0.0) + fees_usd
    le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
    le["last_exit_price"] = float(fill_price)
    le["last_exit_entry_price"] = float(entry_price)
    le["last_exit_quantity"] = float(quantity)
    le["last_exit_notional_basis_usd"] = notional_basis
    le["last_exit_return_bps"] = (pnl / notional_basis) * 10_000.0 if notional_basis > 1e-12 else None
    _record_live_exit_ledger_safe(
        db,
        sess,
        le=le,
        quantity=float(quantity),
        entry_price=float(entry_price),
        fill_price=float(fill_price),
        realized_pnl_usd=pnl,
        reason=reason,
        fee=fees_usd,
    )
    # FILL_OUTCOME_LOG (mig308) — Hook B: full exit. fill_source is broker_confirmed
    # ONLY when the poll captured a real broker average fill price; the broker-zero
    # reconcile-finalize path (no order poll → price RECONSTRUCTED, the CAST −$253
    # case) is flagged 'reconstructed' so day-net can quarantine it.
    _bt = le.pop("last_exit_broker_truth", None)
    _bt = _bt if isinstance(_bt, dict) else None
    _record_fill_outcome_safe(
        db,
        sess,
        side="exit",
        fill_source="broker_confirmed" if _bt is not None else "reconstructed",
        broker_order_id=(_bt or {}).get("broker_order_id"),
        fill_price=float(fill_price),
        qty=float(quantity),
        fees_usd=fees_usd,
        order_status=(_bt or {}).get("order_status"),
        intended_price=_float_or_none(le.get("last_exit_intended_price")),
        spread_bps_at_decision=_float_or_none(le.get("entry_spread_bps_at_decision")),
        entry_price=float(entry_price),
        exit_reason=reason,
        realized_pnl_usd=pnl,
        pnl_gross_usd=pnl_gross,
        raw={"slip_bps": slip_bps, "fees_usd": fees_usd, "reconciled": (_bt is None)},
    )
    _finalize_live_decision_after_exit(db, sess, le=le, realized_pnl_usd=pnl, slip_bps=slip_bps)
    le["last_exit_reason"] = reason
    # Shake-out learning: stash the inputs (incl. the REAL momentum stop/target,
    # still on the position here) so a deferred job can judge whether the thesis
    # worked AFTER we exited — was the stop too tight? — instead of the learner
    # seeing a shallow loss. (post_exit_excursion.py; docs/DESIGN/MOMENTUM_LANE.md)
    _exit_pos = le.get("position") if isinstance(le.get("position"), dict) else {}
    le["post_exit_excursion_pending"] = {
        "symbol": sess.symbol,
        "entry_price": float(entry_price),
        "exit_price": float(fill_price),
        "original_stop": _exit_pos.get("stop_price"),
        "original_target": _exit_pos.get("target_price"),
        "side_long": True,
        "exit_reason": reason,
        "realized_pnl": pnl,
        "exit_time_utc": _utcnow().isoformat(),
        "horizon_seconds": int(getattr(settings, "chili_momentum_post_exit_horizon_seconds", 1800) or 1800),
        "state": "pending",
    }
    le["position"] = None
    le.pop("pending_exit_reason", None)
    le.pop("pending_exit_quantity", None)
    le.pop("pending_exit_submitted_at_utc", None)
    _commit_le(sess, le)
    _safe_transition(db, sess, STATE_LIVE_EXITED)
    payload = {"reason": reason, "pnl_usd": pnl, "fill_price": float(fill_price)}
    if fees_usd > 0.0:
        payload["pnl_gross_usd"] = pnl_gross
        payload["fees_usd"] = fees_usd
    if sell_result is not None:
        payload["sell_result"] = sell_result
    _emit(db, sess, "live_exit_filled", payload)
    return pnl


def _apply_confirmed_live_partial_exit(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    filled_quantity: float,
    entry_price: float,
    fill_price: float,
    reason: str,
) -> float:
    pos = le.get("position")
    pos = dict(pos) if isinstance(pos, dict) else {}
    current_qty = _float_or_none(pos.get("quantity")) or 0.0
    qty = min(max(float(filled_quantity), 0.0), current_qty)
    # Fee truth (2026-06-13): a partial exit nets only ITS OWN order's
    # commission; the entry-side fee is booked once at the final full exit.
    _exit_fee = max(0.0, _float_or_none(le.pop("last_exit_fee_usd", None)) or 0.0)
    pnl = (float(fill_price) - float(entry_price)) * qty - _exit_fee
    notional_basis = abs(float(entry_price) * qty)
    remaining = max(0.0, current_qty - qty)
    le["fees_usd_total"] = float(le.get("fees_usd_total") or 0.0) + _exit_fee
    le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
    le["last_partial_exit_price"] = float(fill_price)
    le["last_partial_exit_reason"] = reason
    le["last_partial_exit_quantity"] = qty
    le["last_partial_exit_notional_basis_usd"] = notional_basis
    le["last_partial_exit_return_bps"] = (pnl / notional_basis) * 10_000.0 if notional_basis > 1e-12 else None
    pos["quantity"] = remaining
    pos["partial_taken"] = True
    le["position"] = pos
    le.pop("pending_exit_reason", None)
    le.pop("pending_exit_quantity", None)
    le.pop("pending_exit_submitted_at_utc", None)
    _record_live_partial_exit_ledger_safe(
        db,
        sess,
        le=le,
        quantity=qty,
        entry_price=float(entry_price),
        fill_price=float(fill_price),
        realized_pnl_usd=pnl,
        reason=reason,
        fee=_exit_fee,
    )
    # FILL_OUTCOME_LOG (mig308) — Hook C: partial / scale-out leg. The scale-out path
    # (_scale_out_to_runner) calls THIS completer before popping the flag, so a truthy
    # pending_exit_is_scale_out here means side='scale_out'; else a plain partial. Same
    # broker-truth side channel as the full exit (broker_confirmed vs reconstructed).
    _bt = le.pop("last_exit_broker_truth", None)
    _bt = _bt if isinstance(_bt, dict) else None
    _record_fill_outcome_safe(
        db,
        sess,
        side="scale_out" if le.get("pending_exit_is_scale_out") else "partial_exit",
        fill_source="broker_confirmed" if _bt is not None else "reconstructed",
        broker_order_id=(_bt or {}).get("broker_order_id"),
        fill_price=float(fill_price),
        qty=qty,
        fees_usd=_exit_fee,
        order_status=(_bt or {}).get("order_status"),
        intended_price=_float_or_none(le.get("last_exit_intended_price")),
        spread_bps_at_decision=_float_or_none(le.get("entry_spread_bps_at_decision")),
        entry_price=float(entry_price),
        exit_reason=reason,
        realized_pnl_usd=pnl,
        pnl_gross_usd=(float(fill_price) - float(entry_price)) * qty,
        raw={"fees_usd": _exit_fee, "remaining": remaining, "reconciled": (_bt is None)},
    )
    _commit_le(sess, le)
    _emit(
        db,
        sess,
        "live_partial_exit_filled",
        {"reason": reason, "qty": qty, "remain": remaining, "pnl_usd": pnl, "fill_price": float(fill_price)},
    )
    return pnl


def _scale_out_to_runner(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    filled_quantity: float,
    entry_price: float,
    fill_price: float,
    reason: str,
) -> float:
    """Ross first-target scale-out: bank the partial, move the BALANCE stop to
    breakeven, and HOLD the remainder as the runner (transition to TRAILING).

    Reuses ``_apply_confirmed_live_partial_exit`` for the partial bookkeeping/ledger,
    then ratchets the runner's stop to entry ("adjust my stop to my entry price on
    the balance"), clears the scale-out pending markers, and arms TRAILING so the
    chandelier trail (above) carries the runner up for the tail. The breakeven move
    is derived (= entry); the trail is derived (frozen entry ATR). One knob total:
    the scale-out fraction. (docs/DESIGN/MOMENTUM_LANE.md)"""
    pnl = _apply_confirmed_live_partial_exit(
        db,
        sess,
        le=le,
        filled_quantity=filled_quantity,
        entry_price=entry_price,
        fill_price=fill_price,
        reason=reason,
    )
    le.pop("pending_exit_is_scale_out", None)
    pos = le.get("position")
    if isinstance(pos, dict):
        old_stop = _float_or_none(pos.get("stop_price"))
        be_stop = breakeven_stop_after_partial(
            float(entry_price),
            float(old_stop if old_stop is not None else entry_price),
            side_long=True,
        )
        pos["stop_price"] = be_stop
        pos["scaled_out_at_utc"] = _utcnow().isoformat()
        pos["scale_out_fraction"] = scale_out_fraction(symbol=sess.symbol, vol_pctl=_adaptive_scale_vol_pctl(le))
        # Measured-move winner-management (flag-gated, default OFF): FREEZE the
        # name's first impulse leg at the first-target scale-out — the breakout high
        # so far (HWM) is the impulse high, entry the leg base. The runner-loop trail
        # later projects this name's OWN leg height to a measured-move target (scale
        # a fraction) and watches for a double-top retest. Frozen here ONCE; both are
        # inert no-ops when the flag is off (byte-identical). docs/DESIGN/MOMENTUM_LANE.md
        try:
            _imp_high = _float_or_none(pos.get("high_water_mark")) or _float_or_none(entry_price)
            pos["impulse_leg_high"] = _imp_high
            pos["impulse_leg_entry"] = _float_or_none(entry_price)
        except Exception:
            pass
        le["position"] = pos
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_TRAILING)
        _emit(
            db,
            sess,
            "live_scaled_out_to_runner",
            {
                "reason": reason,
                "partial_qty": float(filled_quantity),
                "runner_qty": _float_or_none(pos.get("quantity")),
                "breakeven_stop": be_stop,
                "partial_pnl_usd": pnl,
            },
        )
    return pnl


# ── BATCH E(1) — MULTI-LEVEL SCALE-OUT GRID ────────────────────────────────────
# A LADDER over the SAME single scale-out chokepoint: sell successive tranche
# fractions at successive R-multiple / round-number targets, trailing the runner
# between rungs. The grid is computed ONCE (frozen on the position) the first time
# the position is held with the flag on; thereafter each rung advances `target_price`
# to the next level so the existing target-trigger re-fires. NO new decrement path —
# every tranche routes through `_apply_confirmed_live_partial_exit` + clamps to the
# remaining held qty; the cumulative fraction is < 1.0 so a runner always remains.
# Flag OFF => the grid is empty => the lane takes the single scale-out then trails
# (byte-identical). docs/DESIGN/MOMENTUM_LANE.md


def _resolve_scale_grid(pos: dict[str, Any], symbol: str | None) -> list[list[float]]:
    """Return the frozen ladder ``[[target_px, fraction], ...]`` for this position.

    Computed ONCE off the FROZEN entry + the position's initial stop (the risk
    anchor), then stored on ``pos['scale_grid']`` so a later breakeven-stop ratchet
    cannot re-scale the rung prices. Empty list when the flag is off or the config is
    degenerate (the caller then falls back to the single scale-out — byte-identical)."""
    # Flag OFF => never touch the position dict (byte-identical persisted JSON).
    if not bool(getattr(settings, "chili_momentum_scale_grid_enabled", False)):
        return []
    cached = pos.get("scale_grid")
    if isinstance(cached, list):
        return [list(x) for x in cached if isinstance(x, (list, tuple)) and len(x) == 2]
    try:
        entry = float(pos.get("avg_entry_price") or 0.0)
        # The ladder is anchored on the ORIGINAL risk: prefer the frozen entry stop, not a
        # later breakeven-ratcheted stop, so the R levels stay fixed across rungs.
        stop = _float_or_none(pos.get("scale_grid_anchor_stop"))
        if stop is None:
            stop = _float_or_none(pos.get("stop_price"))
    except (TypeError, ValueError):
        entry, stop = 0.0, None
    levels = scale_grid_levels(entry, float(stop) if stop is not None else 0.0, side_long=True, symbol=symbol)
    pos["scale_grid"] = [[float(px), float(fr)] for px, fr in levels]
    return [list(x) for x in pos["scale_grid"]]


def _scale_grid_active(pos: dict[str, Any], symbol: str | None) -> bool:
    """True iff a multi-level grid is in force AND has un-fired rungs remaining."""
    grid = _resolve_scale_grid(pos, symbol)
    if len(grid) < 2:  # 0/1 rung => no ladder; the single scale-out path handles it
        return False
    idx = int(pos.get("scale_grid_idx") or 0)
    return idx < len(grid)


def _scale_out_grid_step(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    filled_quantity: float,
    entry_price: float,
    fill_price: float,
    reason: str,
) -> float:
    """Bank ONE ladder tranche through the shared partial-exit chokepoint, then either
    advance to the NEXT rung (keep the runner, re-target, return to TRAILING) or, on the
    LAST rung, finish like the single scale-out (breakeven + trail the final runner).

    Reuses ``_apply_confirmed_live_partial_exit`` for the bookkeeping/ledger (the SAME
    single decrement path), so this never opens a second oversell surface. INVARIANT-A:
    the stop only ratchets to breakeven (never loosened). The remainder is always > 0
    because the cumulative grid fraction is < 1.0 (enforced in ``scale_grid_levels``)."""
    pnl = _apply_confirmed_live_partial_exit(
        db,
        sess,
        le=le,
        filled_quantity=filled_quantity,
        entry_price=entry_price,
        fill_price=fill_price,
        reason=reason,
    )
    le.pop("pending_exit_is_scale_out", None)
    pos = le.get("position")
    if not isinstance(pos, dict):
        return pnl
    grid = _resolve_scale_grid(pos, sess.symbol)
    idx = int(pos.get("scale_grid_idx") or 0) + 1  # this rung just filled
    pos["scale_grid_idx"] = idx
    # Move the balance stop to breakeven on the FIRST rung (Ross "adjust to entry on the
    # balance"); ratchet-only on later rungs (INVARIANT-A — never loosened).
    old_stop = _float_or_none(pos.get("stop_price"))
    be_stop = breakeven_stop_after_partial(
        float(entry_price),
        float(old_stop if old_stop is not None else entry_price),
        side_long=True,
    )
    pos["stop_price"] = be_stop
    pos["scaled_out_at_utc"] = _utcnow().isoformat()
    more = idx < len(grid)
    if more:
        # Re-target the next rung so the existing target-trigger re-fires; KEEP partial_taken
        # FALSE (more tranches remain) and hold the runner in TRAILING between rungs.
        next_px = float(grid[idx][0])
        pos["target_price"] = next_px
        pos["partial_taken"] = False  # re-arm the scale-out trigger for the next rung
        le["position"] = pos
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_TRAILING)
        _emit(
            db,
            sess,
            "live_scale_grid_tranche",
            {
                "reason": reason,
                "rung": idx,
                "of_rungs": len(grid),
                "tranche_qty": float(filled_quantity),
                "runner_qty": _float_or_none(pos.get("quantity")),
                "next_target": next_px,
                "breakeven_stop": be_stop,
                "tranche_pnl_usd": pnl,
            },
        )
    else:
        # Last rung filled: the remainder is the final RUNNER — trail it (single-scale finish).
        le["position"] = pos
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_TRAILING)
        _emit(
            db,
            sess,
            "live_scaled_out_to_runner",
            {
                "reason": reason,
                "partial_qty": float(filled_quantity),
                "runner_qty": _float_or_none(pos.get("quantity")),
                "breakeven_stop": be_stop,
                "partial_pnl_usd": pnl,
                "scale_grid_final_rung": idx,
            },
        )
    return pnl


def _fmt_limit_price_sell(p: float) -> str:
    """Penny-FLOOR for sell limits >= $1 (never ask above the intended level)."""
    if p >= 1.0:
        return f"{math.floor(p * 100.0) / 100.0:.2f}"
    return f"{p:.4f}"


def _place_scale_out_limit(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    product_id: str,
    target_px: float,
    filled: float,
    prod: Any,
) -> None:
    """Sell INTO strength (Ross): rest a GTC LIMIT for the scale-out fraction AT
    the first target the moment the entry fills — the partial executes while the
    pop is still paying the level, instead of a reactive market sell after the
    trigger (which pays the give-back). Fail-open: any failure here leaves the
    reactive market scale-out path fully in charge."""
    try:
        _eq_shares = not str(sess.symbol or "").upper().endswith("-USD")
        inc = prod.base_increment if prod else (1.0 if _eq_shares else None)
        mn = prod.base_min_size if prod else (1.0 if _eq_shares else None)
        # E(1): when a multi-level grid is in force, rest the FIRST RUNG's fraction at the
        # first rung's price (the reactive market path takes the later rungs). The grid
        # anchor stop is frozen on the position at entry; build the ladder off it. Flag OFF
        # / no ladder => the single scale_out_fraction at target_px (byte-identical).
        _rest_frac = scale_out_fraction(symbol=sess.symbol, vol_pctl=_adaptive_scale_vol_pctl(le))
        _rest_px = float(target_px)
        try:
            _pos = le.get("position") if isinstance(le.get("position"), dict) else {}
            _grid = _resolve_scale_grid(_pos, sess.symbol)
            if len(_grid) >= 2:  # an actual ladder
                _rest_frac = float(_grid[0][1])
                _rest_px = float(_grid[0][0])
        except Exception:
            _rest_frac = scale_out_fraction(symbol=sess.symbol, vol_pctl=_adaptive_scale_vol_pctl(le))
            _rest_px = float(target_px)
        scale_qty, _runner_qty, can_split = scale_out_quantity(
            current_qty=float(filled),
            original_qty=float(filled),
            fraction=_rest_frac,
            base_increment=inc,
            base_min_size=mn,
        )
        if not can_split or scale_qty <= 0:
            return
        target_px = _rest_px
        _ext = False
        try:
            from .market_profile import market_session_now

            _ext = market_session_now(sess.symbol) != "regular"
        except Exception:
            _ext = False
        cid = f"chili_ml_sol_{sess.id}_{uuid.uuid4().hex[:12]}"
        res = adapter.place_limit_order_gtc(
            product_id=product_id,
            side="sell",
            base_size=_fmt_base_size(scale_qty),
            limit_price=_fmt_limit_price_sell(float(target_px)),
            client_order_id=cid,
            extended_hours=_ext,
        ) or {}
        if res.get("ok") and res.get("order_id"):
            le["scale_limit_order_id"] = str(res["order_id"])
            le["scale_limit_px"] = float(target_px)
            le["scale_limit_qty"] = float(scale_qty)
            le["scale_limit_adopted_qty"] = 0.0
            _commit_le(sess, le)
            _emit(db, sess, "scale_out_limit_placed", {
                "order_id": le["scale_limit_order_id"],
                "qty": float(scale_qty), "limit_price": float(target_px),
                "extended_hours": _ext,
            })
        else:
            _emit(db, sess, "scale_out_limit_place_failed", {
                "error": str(res.get("error"))[:120], "fallback": "reactive_market_scale_out",
            })
    except Exception:
        logger.warning(
            "[live_runner] scale-out limit placement failed sess=%s (reactive path covers)",
            sess.id, exc_info=True,
        )


def _cancel_scale_limit_and_clamp(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    requested_qty: float,
    reason: str,
) -> float:
    """OVERSELL INVARIANT for sell-into-strength: before ANY market exit, cancel
    the resting scale-out limit and adopt whatever it already filled (cancel-race
    safe), then clamp the requested sell quantity to the TRUE remaining position.
    Without this, the resting limit and the market exit could both execute and
    flip the account short. Called from the single exit chokepoint so every path
    (stop / trail / bailout / kill-switch / EOD / max-hold) is covered."""
    oid = le.get("scale_limit_order_id")
    if not oid:
        return float(requested_qty)
    try:
        try:
            adapter.cancel_order(str(oid))
        except Exception:
            pass
        no, _ = adapter.get_order(str(oid))
        filled = float(getattr(no, "filled_size", 0) or 0) if no is not None else 0.0
        adopted = float(le.get("scale_limit_adopted_qty") or 0.0)
        new_fill = max(0.0, filled - adopted)
        if new_fill > 0:
            pos = le.get("position") if isinstance(le.get("position"), dict) else {}
            px = float(getattr(no, "average_filled_price", 0) or 0) or float(le.get("scale_limit_px") or 0)
            # Fee truth: this adopt path never goes through the exit poll, so
            # stash the order's commission for the partial bookkeeping here.
            le["last_exit_fee_usd"] = _order_total_fees_usd(no)
            _apply_confirmed_live_partial_exit(
                db, sess, le=le, filled_quantity=new_fill,
                entry_price=float(pos.get("avg_entry_price") or 0),
                fill_price=px, reason="scale_out_limit_fill",
            )
            le["scale_limit_adopted_qty"] = adopted + new_fill
        _emit(db, sess, "scale_out_limit_cancelled", {
            "order_id": str(oid), "filled_qty": filled, "for_exit": reason,
        })
    except Exception:
        logger.warning(
            "[live_runner] scale-limit cancel-adopt failed sess=%s", sess.id, exc_info=True
        )
    finally:
        le.pop("scale_limit_order_id", None)
        _commit_le(sess, le)
    pos2 = le.get("position") if isinstance(le.get("position"), dict) else {}
    remaining = float(_float_or_none(pos2.get("quantity")) or 0.0)
    return max(0.0, min(float(requested_qty), remaining))


_OPEN_ORDER_STATES_FOR_CANCEL = frozenset(
    {"open", "confirmed", "queued", "unconfirmed", "partially_filled", "pending", "accepted", "new"}
)


def _cancel_agentic_covering_sells(adapter: Any, symbol: str) -> int:
    """Cancel ANY working SELL on the pinned agentic account for ``symbol`` so a
    full-position stop/trail/bailout isn't rejected 'Not enough shares to sell' by a
    resting partial-target that locks shares (the 2026-06-23 strand bug:
    PALI/LILA/RDGT/AIIO -> 8 rejects -> live_error). Generalizes the tracked-only
    _cancel_scale_limit_and_clamp (catches an UNTRACKED sell) and, by re-running each
    exit attempt, clears a cancel-propagation race. Mirrors crypto
    _cancel_coinbase_open_sell_orders. Best-effort; returns count cancelled."""
    n = 0
    try:
        if not (hasattr(adapter, "get_agentic_open_orders") and hasattr(adapter, "cancel_order")):
            return 0
        for o in (adapter.get_agentic_open_orders(symbol=symbol) or []):
            try:
                get = o.get if isinstance(o, dict) else (lambda k, d=None: getattr(o, k, d))
                side = str(get("side") or "").lower()
                state = str(get("state") or get("status") or "").lower()
                oid = get("id") or get("order_id")
                if side == "sell" and oid and state in _OPEN_ORDER_STATES_FOR_CANCEL:
                    adapter.cancel_order(str(oid))
                    n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


def _safe_transition(db: Session, sess: TradingAutomationSession, new_state: str) -> None:
    old = sess.state
    if old == new_state:
        return
    assert_transition_live(old, new_state)
    sess.state = new_state
    sess.updated_at = _utcnow()
    from .feedback_emit import emit_feedback_after_terminal_transition
    from .outcome_extract import session_terminal_for_feedback

    if session_terminal_for_feedback(sess.mode or "live", new_state):
        emit_feedback_after_terminal_transition(db, sess)


# Pre-entry, no-position states that a DETERMINISTIC policy decline can terminalize
# cleanly from. A decline reached from any of these never owned capital, so routing it
# to live_cancelled cannot abandon a live position.
_PRE_ENTRY_DECLINE_STATES = frozenset(
    {STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE, STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE}
)


def _clean_decline_terminal_enabled() -> bool:
    """ON by default (no-dark-flags): a deterministic PRE-ENTRY policy decline terminalizes
    as the CLEAN live_cancelled state, not the alarm-coloured live_error — cutting the
    recurring live_error noise + reaper churn so the REAL errors (zero-fill, place isError)
    stand out. Flag OFF => byte-identical legacy (decline => live_error). FAIL-SAFE: any
    settings read error keeps the change ON (the safe, noise-reducing default)."""
    try:
        return bool(getattr(settings, "chili_momentum_clean_decline_terminal_enabled", True))
    except Exception:
        return True


def _decline_terminal(
    db: Session,
    sess: TradingAutomationSession,
    *,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Terminalize a DETERMINISTIC pre-entry POLICY decline (a known risk-eval BLOCK —
    no_bbo / not-live-eligible / spread-too-wide / product-not-tradable — on a name that
    never held a position) in the CLEAN live_cancelled state with the decline reason
    recorded, instead of the alarm-coloured live_error.

    This NEVER weakens a risk block: the session still does NOT enter; only the terminal
    STATE/label changes. live_cancelled is already terminal across every consumer (focus-
    set, reaper, feedback learner, busy-set, canonical status). When the flag is OFF, or the
    session is somehow NOT in a pre-entry/no-position state (defensive), it falls back to the
    legacy live_error so a held position can never be short-circuited by a decline reroute."""
    if _clean_decline_terminal_enabled() and sess.state in _PRE_ENTRY_DECLINE_STATES:
        payload: dict[str, Any] = {"reason": reason, "terminal": STATE_LIVE_CANCELLED}
        if detail:
            payload.update(detail)
        _emit(db, sess, "live_declined", payload)
        _safe_transition(db, sess, STATE_LIVE_CANCELLED)
        return
    _safe_transition(db, sess, STATE_LIVE_ERROR)


def _arm_time_live_eligible_anchor(sess: TradingAutomationSession) -> str | None:
    """Read the session's arm/confirm-time live-eligibility anchor (ISO-8601 UTC) for the
    recency-grace (UPC +500% TOCTOU miss). ``confirm_live_arm`` stamps it on the snapshot
    when the session was provably live-eligible at confirm. FAIL-SAFE: absent / non-string
    ⇒ None ⇒ no grace ⇒ today's BLOCK.

    The anchor is PINNED to the arm/confirm instant and is NEVER refreshed at runtime: only
    ``operator_actions.confirm_live_arm`` writes the top-level ``live_eligible_at_utc`` stamp,
    and no runner path re-stamps it when live-eligibility is later observed True. (The nested
    ``le['live_eligible_at_utc']`` read below is therefore a dead branch — kept only as a
    defensive fall-through; nothing populates it.) This pinning is DELIBERATE and the SAFER
    behavior: because the anchor cannot move, the recency-grace window cannot creep — a slow
    (> window) arm-to-entry setup ages out of the window and safely reverts to the
    conservative BLOCK rather than holding the grace open indefinitely. The read prefers the
    (currently-unwritten) live-exec block, then the authoritative top-level confirm stamp."""
    try:
        snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
        le = snap.get(KEY_LIVE_EXEC) if isinstance(snap.get(KEY_LIVE_EXEC), dict) else {}
        for raw in (le.get("live_eligible_at_utc"), snap.get("live_eligible_at_utc")):
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    except Exception:
        return None
    return None


def _live_forward_momentum(
    db: Session, sess: TradingAutomationSession, *, as_of: datetime | None = None
) -> bool | None:
    """Live FORWARD-MOMENTUM read for the eligibility recency-grace: True when the tape is
    being fed UP right now per the codebase's CANONICAL RIDE definition — signed aggressor
    OFI level > 0 AND OFI slope >= 0 (see ``paper_execution.velocity_persistence_ride_lock``
    RIDE: ``level > 0 ∧ slope >= 0``). This is the AND (both legs agree) semantics, NOT the
    looser OR: an OR let a transient dead-cat up-bucket in a sharp selloff (level<=0 but a
    one-bar slope>0, or vice-versa) flip momentum True and grant the grace on a falling
    tape. Reuses the SAME ``_live_flow_slope`` read (LEVER 2B / the maturity-widen use) — no
    new datum, no new fetch path. Returns None on thin / stale / crypto tape
    (``_live_flow_slope`` ⇒ None) OR when EITHER OFI leg is missing, so the grace stays
    conservative (None ⇒ not-True ⇒ BLOCK held).

    ``as_of`` (REPLAY v3 P2) reads the recorded order-flow AS-OF a past instant: the runner
    passes ``_utcnow()`` (the SIM clock under replay; the real wall clock in prod, where the
    ``as_of`` default and ``_utcnow()`` are the same wall instant — BYTE-IDENTICAL). The
    ``_live_flow_slope`` ``as_of`` plumbing already exists (``pipeline.py:671``)."""
    try:
        from .pipeline import _live_flow_slope as _lfs_grace

        fs = _lfs_grace(sess.symbol, db=db, as_of=as_of)
        if not isinstance(fs, dict):
            return None
        lvl = fs.get("ofi_level")
        slp = fs.get("ofi_slope")
        # CANONICAL RIDE: BOTH legs must be present and agree (level > 0 AND slope >= 0).
        # A missing leg ⇒ None (conservative; the grace stays blocked) — we can't confirm
        # the tape is being fed up on a single available leg.
        if lvl is None or slp is None:
            return None
        try:
            return bool(float(lvl) > 0.0 and float(slp) >= 0.0)
        except (TypeError, ValueError):
            return None
    except Exception:
        return None


def runner_boundary_risk_ok(
    db: Session,
    sess: TradingAutomationSession,
    *,
    expected_move_bps: float | None = None,
    apply_eligibility_grace: bool = False,
) -> tuple[bool, dict[str, Any]]:
    if sess.user_id is None:
        return False, {"reason": "no_user"}
    # Live-eligibility RECENCY-GRACE evidence (UPC +500% TOCTOU miss). Only the ENTRY gate
    # opts in (apply_eligibility_grace=True); scale-in adds inherit the entry admission and
    # keep their own held-flicker tolerance. The evaluator stays byte-identical when neither
    # is supplied (and when the grace flag is OFF). The OFI read is best-effort and the
    # anchor is fail-safe — absent evidence ⇒ today's block.
    _recent_elig: str | None = None
    _fwd_mom: bool | None = None
    if apply_eligibility_grace:
        _recent_elig = _arm_time_live_eligible_anchor(sess)
        if _recent_elig is not None:
            # REPLAY v3 P2: thread the SIM instant into the forward-momentum read ONLY when a
            # replay clock is active, so the recorded order-flow is read AS-OF t. In prod the
            # sim clock is unset (``_SIM_NOW`` is None) ⇒ ``as_of=None`` ⇒ the live "now()"
            # window read — BYTE-IDENTICAL. (Threading a wall-clock ``as_of`` unconditionally
            # would switch the SQL window ref from DB-now to app-now and add a ``<= as_of``
            # cap, so it is gated on the replay clock to preserve prod behavior exactly.)
            _grace_as_of = _SIM_NOW.get()
            _fwd_mom = _live_forward_momentum(db, sess, as_of=_grace_as_of)
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(sess.user_id),
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode="live",
        execution_family=normalize_execution_family(sess.execution_family),
        exclude_session_id=int(sess.id),
        expected_move_bps=expected_move_bps,
        recent_live_eligible_at_utc=_recent_elig,
        live_forward_momentum=_fwd_mom,
    )
    return bool(ev.get("allowed", False)), ev


def _entry_live_eligible_ok(
    db: Session, sess: TradingAutomationSession, via: Any
) -> bool:
    """``via.live_eligible`` for the ENTRY-detection / entry-revalidation re-reads, WITH the
    SAME recency-grace the boundary-risk gate applies (UPC +500% TOCTOU miss).

    The boundary-risk gate (``runner_boundary_risk_ok(apply_eligibility_grace=True)``) tolerates
    a ``live_eligible`` FLICKER at the entry instant when the name was live-eligible at
    arm/confirm within the grace window AND forward momentum is present. But the runner ALSO
    re-reads ``via.live_eligible`` RAW at three downstream entry-detection sites (the
    ``watching_live`` ``_score_ok``, the ``live_entry_candidate -> live_pending_entry``
    transition, and the ``live_pending_entry`` pre-submit guard) — and those raw re-reads
    reverted the entry to ``watching_live`` on the very flicker the gate just tolerated, so the
    grace could NEVER actually let the name ENTER. This helper closes that gap by REUSING the
    EXACT same grace evidence + decision (``_arm_time_live_eligible_anchor`` +
    ``_live_forward_momentum`` + ``risk_evaluator._live_eligible_recency_grace_active``) — it
    does NOT re-implement the grace.

    FAIL-SAFE + flag-gated: when ``via.live_eligible`` is already True this is a pure pass-through
    (True). When False, the grace is consulted; with the grace flag OFF (or no anchor / no
    momentum / out-of-window) it returns False — BYTE-IDENTICAL to the prior raw ``via.live_eligible``
    read. Only a positive-evidence flicker (recent eligible at arm + forward momentum) returns True."""
    try:
        if bool(getattr(via, "live_eligible", False)):
            return True
        anchor = _arm_time_live_eligible_anchor(sess)
        if anchor is None:
            return False
        from .risk_evaluator import _live_eligible_recency_grace_active
        from .risk_policy import MomentumAutomationRiskPolicy

        policy = MomentumAutomationRiskPolicy.from_settings()
        if not policy.live_eligible_recency_grace_enabled:
            return False  # flag OFF ⇒ byte-identical to the raw via.live_eligible read
        fwd = _live_forward_momentum(db, sess, as_of=_SIM_NOW.get())
        active, _ = _live_eligible_recency_grace_active(
            policy=policy,
            recent_live_eligible_at_utc=anchor,
            live_forward_momentum=fwd,
        )
        return bool(active)
    except Exception:
        # Any unexpected error ⇒ fall back to the conservative raw read (FAIL-SAFE block).
        return bool(getattr(via, "live_eligible", False))


def _only_transient_freshness_block(ev: dict[str, Any]) -> bool:
    """True iff the boundary-risk evaluation failed EXCLUSIVELY on the transient
    ``viability_freshness`` check — a stale snapshot the equity refresh will renew —
    i.e. there is at least one failing check and EVERY failing check is the freshness
    one. Used to re-watch (retry) a freshly-armed session instead of terminally
    ERRORing it on a staleness blip. FAIL-SAFE: any unexpected shape / parse error
    returns False so the caller keeps its conservative hard-error.

    Keys on the structured ``checks`` list (``_check`` dicts: ``id`` + ``ok``), not
    free-text, so it never matches a kill-switch / drawdown / cap failure."""
    try:
        checks = ev.get("checks")
        if not isinstance(checks, list) or not checks:
            return False
        failed = [c for c in checks if isinstance(c, dict) and not c.get("ok", True)]
        if not failed:
            return False
        return all(str(c.get("id") or "") == "viability_freshness" for c in failed)
    except Exception:
        return False


def _only_held_eligibility_flicker_block(ev: dict[str, Any]) -> bool:
    """True iff the boundary-risk evaluation failed EXCLUSIVELY on the neural-viability
    eligibility / freshness checks (``live_eligible`` and/or ``viability_freshness``) —
    a stale/flickering snapshot, NOT a hard risk block (kill-switch, drawdown, daily-loss,
    position cap). Used by GATE 5: an add to an ALREADY-HELD winner inherits the entry's
    admission, so this transient flicker must not refuse it. FAIL-SAFE: any unexpected
    shape / no failing checks ⇒ False (keep the conservative refusal). Keys on the
    structured ``checks`` ids, never free text, so it can never match a real risk block."""
    try:
        checks = ev.get("checks")
        if not isinstance(checks, list) or not checks:
            return False
        failed = [c for c in checks if isinstance(c, dict) and not c.get("ok", True)]
        if not failed:
            return False
        _allow = {"live_eligible", "viability_freshness"}
        return all(str(c.get("id") or "") in _allow for c in failed)
    except Exception:
        return False


# A4: per-symbol last-rescore monotonic timestamps (rate-limit to the adaptive tape cadence).
# Hard-capped dict (CLAUDE.md: caches need a max size); on overflow the whole map clears (cheap).
_A4_RESCORE_LAST: dict[str, float] = {}
_A4_RESCORE_MAX = 4096


def _maybe_rescore_eligibility_block(
    db: Session, sess: TradingAutomationSession, ev: dict[str, Any]
) -> bool:
    """A4 MID-MOVE ELIGIBILITY-FLIP RE-SCORE. At a viability block whose ONLY failing checks are
    ``live_eligible`` / ``viability_freshness``, when the session is ARMED and its own tick
    evidence shows running-up continuation (``_live_forward_momentum``: signed OFI level>0 AND
    slope>=0), invoke the SAME single-symbol re-score the tape-delta feeder uses
    (``run_momentum_neural_tick`` with freshness_ts=now), rate-limited PER SYMBOL to the adaptive
    tape cadence (clamp of the p50 tape inter-row gap). The re-score may flip eligibility True OR
    legitimately confirm False.

    Returns True iff a re-score was performed this tick (the caller re-reads viability / re-runs
    the boundary gate). FAIL-CLOSED: flag off / not an eligibility-only block / not ARMED / not
    running-up / rate-limited / any error => returns False and the ORIGINAL block stands. NEVER
    forces eligibility — it only asks the scorer to look again against fresh evidence."""
    try:
        if not bool(getattr(settings, "chili_momentum_eligibility_block_rescore_enabled", True)):
            return False
        # ARMED / queued only (a freshly-armed setup that never held a position).
        if sess.state not in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
            return False
        # ONLY eligibility/freshness failing (never re-score past a real risk block).
        if not _only_held_eligibility_flicker_block(ev):
            return False
        sym = str(sess.symbol or "").strip().upper()
        if not sym or sym.endswith("-USD"):
            return False  # equity lane only (the tape-delta feeder is equity tape)
        # Running-up continuation (the existing ross_event predicate): signed OFI level>0 AND
        # slope>=0. None/False (thin/falling tape) => do NOT re-score (fail-closed, block stands).
        if _live_forward_momentum(db, sess, as_of=_utcnow()) is not True:
            return False
        # Adaptive per-symbol rate-limit = clamp(p50 tape inter-row gap, floor, 15).
        import time as _time

        now_mono = _time.monotonic()
        try:
            from .nbbo_tape import tape_inter_row_gap_p50_seconds

            _p50 = tape_inter_row_gap_p50_seconds(db)
        except Exception:
            _p50 = None
        _floor = float(getattr(settings, "chili_momentum_tape_delta_min_seconds", 5.0) or 5.0)
        _floor = max(1.0, _floor)
        cadence = _floor if _p50 is None else min(15.0, max(_floor, float(_p50)))
        _last = _A4_RESCORE_LAST.get(sym)
        if _last is not None and (now_mono - _last) < cadence:
            return False  # arrived sooner than the adaptive cadence — self-throttle (block stands)
        if len(_A4_RESCORE_LAST) >= _A4_RESCORE_MAX:
            _A4_RESCORE_LAST.clear()
        _A4_RESCORE_LAST[sym] = now_mono
        # Re-score via the SAME single-symbol path the tape-delta feeder uses (freshness_ts=now
        # is stamped inside run_momentum_neural_tick). Own commit; idempotent (symbol,variant)
        # upsert downstream. FAIL-CLOSED: any error => the block stands.
        from .pipeline import run_momentum_neural_tick

        run_momentum_neural_tick(db, meta={"tickers": [sym], "ross_signals": {sym: {"ticker": sym, "direction": "long"}}})
        try:
            db.commit()
        except Exception:
            db.rollback()
            return False
        _emit(db, sess, "eligibility_block_rescored", {"symbol": sym, "cadence_s": round(cadence, 2)})
        return True
    except Exception:
        _log.debug("[momentum_live] A4 eligibility-block re-score skipped", exc_info=True)
        return False


def _round_base_size(qty: float, increment: Optional[float], min_sz: Optional[float]) -> float:
    if qty <= 0:
        return 0.0
    if increment and increment > 0:
        q = math.floor(qty / increment) * increment
    else:
        q = round(qty, 8)
    if min_sz and q + 1e-12 < min_sz:
        return 0.0
    return float(q)


def _fmt_base_size(q: float) -> str:
    s = f"{q:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_limit_price_buy(p: float) -> str:
    """Format a BUY limit price as a venue-safe tick string. Prices >= $1 (the
    equity Ross band is $1-$20) round UP to the penny so the marketable buy stays
    marketable (limit at/above the ask); sub-$1 (crypto / penny names) passes finer
    precision for the venue adapter to quantize to its own increment. Rounding UP on
    a buy never makes the limit LESS marketable, so the fill is not starved. Pure +
    side-effect-free for unit testing. (docs/DESIGN/MOMENTUM_LANE.md)"""
    try:
        if not math.isfinite(p) or p <= 0:
            return "0"
        if p >= 1.0:
            ticked = math.ceil(p * 100.0 - 1e-9) / 100.0
            return f"{ticked:.2f}"
        s = f"{p:.8f}".rstrip("0").rstrip(".")
        return s if s else "0"
    except Exception:
        return f"{p}"


def _notional_guard_multiplier() -> float:
    try:
        raw_bps = getattr(settings, "chili_momentum_order_notional_guard_bps", 25.0)
        bps = 25.0 if raw_bps is None else float(raw_bps)
    except (TypeError, ValueError):
        bps = 25.0
    return 1.0 + max(0.0, bps) / 10_000.0


def _entry_chase_ceiling_px(*, limit_px: float, expected_move_bps: float | None) -> float:
    """Bid may drift this far ABOVE the buy limit before the resting marketable order
    is abandoned as 'left behind'. ONE base knob (bps), widened by a fraction of the
    name's own expected per-bar move (explosive names get proportionally more rope,
    quiet names almost none — never a fixed cent), HARD-CAPPED at the same adaptive
    max-spread the entry gate already enforces (the chase can never exceed the cost
    the risk model sized against). It only TOLERATES the existing resting limit — it
    never re-pegs the price up into a spike. base_bps=0 (default) ⇒ returns ``limit_px``
    (today's cancel-on-first-tick — parity). Pure + side-effect-free."""
    try:
        base_bps = float(getattr(settings, "chili_momentum_entry_chase_ceiling_bps", 0.0) or 0.0)
    except (TypeError, ValueError):
        base_bps = 0.0
    if base_bps <= 0 or limit_px <= 0:
        return limit_px
    try:
        ratio = float(getattr(settings, "chili_momentum_entry_chase_move_ratio", 0.25) or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0
    tol_bps = max(base_bps, (expected_move_bps or 0.0) * ratio)
    tol_bps = min(tol_bps, _adaptive_live_max_spread_bps(expected_move_bps))
    return limit_px * (1.0 + tol_bps / 10_000.0)


def _ask_advanced_past_limit(
    *, ask: float | None, limit_px: float, expected_move_bps: float | None
) -> bool:
    """FIX B — fast-push detector. True when the live ASK has advanced ABOVE our
    resting buy limit by MORE than an adaptive band — i.e. the offer ran away and a
    resting marketable limit is no longer marketable (the order won't fill until
    price comes back). This is the EARLY signal of a vertical push that the existing
    bid-based ``entry_limit_left_behind`` misses (the bid lags the ask up).

    The band = ONE documented base (``..._runaway_cross_ask_band_bps``), widened by
    a fraction of the name's expected per-bar move (reuses ``chase_move_ratio`` so
    explosive names get proportionally more rope before we churn a cancel+replace),
    HARD-CAPPED at the same adaptive live spread cap the chase ceiling uses (the
    detector can never tolerate more drift than the risk budget). Debounces single-
    tick offer jitter. The eventual cross price is STILL bounded separately by
    ``_entry_repeg_price``'s cumulative ceiling. Pure + side-effect-free.

    Returns False on any invalid input (fail-closed: no escalation)."""
    try:
        if ask is None or limit_px <= 0 or ask <= 0:
            return False
    except (TypeError, ValueError):
        return False
    try:
        base_bps = float(getattr(settings, "chili_momentum_runaway_cross_ask_band_bps", 8.0) or 0.0)
    except (TypeError, ValueError):
        base_bps = 8.0
    if base_bps < 0:
        base_bps = 0.0
    try:
        ratio = float(getattr(settings, "chili_momentum_entry_chase_move_ratio", 0.25) or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0
    band_bps = max(base_bps, (expected_move_bps or 0.0) * ratio)
    band_bps = min(band_bps, _adaptive_live_max_spread_bps(expected_move_bps))
    threshold = limit_px * (1.0 + band_bps / 10_000.0)
    return ask > threshold


def _entry_repeg_price(
    *, original_limit_px: float, live_ask: float, expected_move_bps: float | None,
    vertical_confluence: float | None = None,
) -> float | None:
    """Bounded marketable RE-PEG price for a left-behind entry chase. Returns a new buy
    limit (a guarded live ask, so marketable) capped by a CUMULATIVE ceiling =
    ``original_limit_px x (1 + adaptive_max_spread_bps)`` — the ONE spread budget the risk
    model already accepts — so TOTAL entry drift (hence the 2:1 R:R against the FIXED
    structural stop) can never erode past that budget no matter how many re-pegs
    accumulate. Returns ``None`` when the live ask has already run PAST the ceiling (the
    move left for good -> cancel + re-watch) or inputs are invalid. Pure, side-effect-free.
    Bounds R:R erosion + thin-book sweep by construction (red-team corrections C + sweep)."""
    try:
        if original_limit_px <= 0 or live_ask <= 0:
            return None
    except (TypeError, ValueError):
        return None
    # Cumulative ceiling = the abs-cap by default; on a CONFIRMED-THRUST halt-resume
    # vertical (vertical_confluence supplied + flag on) it is adaptively raised toward
    # the HARD vertical_chase_max_bps so a fast resume gap actually fills. Risk-first
    # re-sizing at the chased price keeps dollar-risk pinned (recoverable).
    ceiling = original_limit_px * (
        1.0 + _vertical_chase_ceiling_bps(
            expected_move_bps=expected_move_bps, confluence=vertical_confluence
        ) / 10_000.0
    )
    if live_ask > ceiling:
        return None  # ran past the cumulative spread budget -> do not chase
    new_px = live_ask * _adaptive_notional_guard_multiplier(expected_move_bps=expected_move_bps)
    return min(new_px, ceiling)


def _vertical_chase_ceiling_bps(
    *, expected_move_bps: float | None, confluence: float | None
) -> float:
    """Adaptive cumulative-chase ceiling (bps over the ORIGINAL entry limit) for a
    CONFIRMED-THRUST halt-resume vertical. Returns the BASE abs-cap (today's behavior,
    parity) unless the master flag is on AND a real thrust confluence in [0,1] clears
    the documented floor; then it scales LINEARLY from the abs-cap (@min_confluence) up
    to a HARD ``vertical_chase_max_bps`` (@confluence=1.0). The raise NEVER exceeds the
    hard max, and a wrong chase is recoverable because the repeg re-sizes risk-first at
    the chased price (dollar-risk pinned at _eff_max_loss + the #769 circuit). Pure +
    side-effect-free. flag OFF / no confluence / confluence < floor ⇒ the abs-cap."""
    base_cap = _adaptive_live_max_spread_bps(expected_move_bps)
    try:
        if not bool(getattr(settings, "chili_momentum_vertical_chase_enabled", True)):
            return base_cap
        if confluence is None or not math.isfinite(float(confluence)):
            return base_cap
        c = max(0.0, min(1.0, float(confluence)))
        floor = float(getattr(settings, "chili_momentum_vertical_chase_min_confluence", 0.5) or 0.0)
        if c < floor:
            return base_cap
        hard_max = float(getattr(settings, "chili_momentum_vertical_chase_max_bps", 800.0) or 0.0)
        if hard_max <= base_cap:
            return base_cap
        span = 1.0 - floor
        frac = 1.0 if span <= 0 else (c - floor) / span
        raised = base_cap + frac * (hard_max - base_cap)
        return max(base_cap, min(raised, hard_max))
    except (TypeError, ValueError):
        return base_cap


def _nohalt_vertical_thrust_strong(
    *, ofi: float | None, new_high: bool | None,
    above_vwap: bool | None, rvol: float | None,
) -> bool:
    """⭐ FALLING-KNIFE GUARD for the NO-HALT deep vertical chase. Returns True ONLY for
    a genuinely strong, UP, CONFIRMED-thrust vertical that has NOT halted — the move that
    actually earns the deep 800bps fill budget without a halt-resume to vouch for it.
    FAIL-CLOSED (any leg missing / ambiguous ⇒ False ⇒ no deep budget, the abs-cap holds).

    Requires ALL of:
      • ``ofi > 0``        — buyers are LIFTING the offer (live order-flow imbalance up),
                             not a one-sided seller / a thin-book offer simply pulled up;
      • ``new_high``       — price is making a NEW HIGH above the breakout level (the ask is
                             being EATEN up, the move is progressing — NOT a fade/pullback);
      • ``above_vwap``     — above VWAP or cleanly reclaiming (a below-VWAP-falling knife
                             can NEVER unlock the deep budget);
      • ``rvol`` > floor   — front-side RVOL strength (explosive participation, not a quiet
                             drift) vs the documented explosive RVOL floor.

    This is the no-halt analogue of the halt-resume vouch: a halt-resume is itself strong
    evidence; absent it, we demand the full UP/OFI/new-high/above-VWAP/RVOL stack. Pure +
    side-effect-free. (the standing fill-on-verticals fix — project_momentum_conversion_fixes)"""
    try:
        if ofi is None or not math.isfinite(float(ofi)) or float(ofi) <= 0.0:
            return False  # fail-closed: no confirmed buy-side flow ⇒ knife risk ⇒ no deep budget
        if new_high is not True:
            return False  # not making a new high ⇒ a fade/pullback, not an up-vertical
        if above_vwap is not True:
            return False  # below-VWAP / falling ⇒ knife ⇒ refuse the deep budget
        rvol_floor = float(getattr(settings, "chili_momentum_explosive_rvol_floor", 3.0) or 3.0)
        if rvol is None or not math.isfinite(float(rvol)) or float(rvol) <= rvol_floor:
            return False  # not explosive participation ⇒ no deep budget
        return True
    except (TypeError, ValueError):
        return False


def _vertical_thrust_confluence(
    *, halt_resume_active: bool, tape_thrust_ok: bool | None,
    squeeze_pct: float | None, rvol: float | None,
    nohalt_thrust_strong: bool | None = None,
) -> float | None:
    """Confirmed-thrust confluence in [0,1] for the vertical chase. Returns None
    (⇒ no raise, parity) UNLESS the tape is explicitly thrusting (fail-closed: tape
    None/False ⇒ None) AND the move is vouched-for by EITHER:
      • a recent halt-resume (``halt_resume_active``) — the original unlock; OR
      • a CONFIRMED no-halt UP-thrust (``nohalt_thrust_strong`` — the knife-guarded
        OFI>0 + new-high + above-VWAP + RVOL stack from ``_nohalt_vertical_thrust_strong``).

    This DECOUPLES the deep fill-aggression budget from the halt-gate: a genuine no-halt
    1m new-high vertical now also unlocks the chase ceiling (the standing fill-on-verticals
    fix), while a fade / below-VWAP / OFI<=0 move stays at the abs-cap (knife guard). The
    no-halt path starts at its OWN documented floor (``..._nohalt_min_confluence``, default
    0.6 — above the 0.5 halt floor, because a no-halt vertical must clear a STRONGER bar);
    the halt path keeps the 0.5 floor. squeeze_pct (centered at 0.5) and RVOL (vs the
    explosive floor) each add a bounded share on top. Pure + side-effect-free.

    The no-halt unlock is master-flagged (``..._nohalt_thrust_enabled``): flag OFF ⇒ only a
    halt-resume can unlock ⇒ byte-identical to the prior halt-gated behavior."""
    try:
        if tape_thrust_ok is not True:
            return None  # fail-closed: no thrust confirmation ⇒ no raise
        _nohalt_on = bool(
            getattr(settings, "chili_momentum_vertical_chase_nohalt_thrust_enabled", True)
        )
        _nohalt_unlock = bool(_nohalt_on and nohalt_thrust_strong is True)
        if not (halt_resume_active or _nohalt_unlock):
            return None  # neither vouch ⇒ no raise (abs-cap holds)
        if halt_resume_active:
            score = 0.5  # halt-resume + confirmed tape is the floor of confidence
        else:
            # NO-HALT confirmed UP-thrust: its OWN floor (default 0.6 > the 0.5 halt floor)
            # because absent a halt-resume vouch we demand a genuinely strong UP bar.
            score = float(
                getattr(settings, "chili_momentum_vertical_chase_nohalt_min_confluence", 0.6)
                or 0.6
            )
            score = max(0.0, min(1.0, score))
        if squeeze_pct is not None and math.isfinite(float(squeeze_pct)):
            score += max(0.0, min(0.25, (float(squeeze_pct) - 0.5) * 0.5))
        rvol_floor = float(getattr(settings, "chili_momentum_explosive_rvol_floor", 3.0) or 3.0)
        if rvol is not None and math.isfinite(float(rvol)) and float(rvol) > rvol_floor:
            score += min(0.25, (float(rvol) - rvol_floor) * 0.05)
        return max(0.0, min(1.0, score))
    except (TypeError, ValueError):
        return None


def _entry_client_order_id(
    *, session_id: Any, correlation_id: str | None,
    trade_cycles: int, stopout_cycles: int, place_n: int,
) -> str:
    """Deterministic, recycle-UNIQUE entry client_order_id.

    The broker rejects a duplicate Reference ID (409 'must be unique'). The per-session
    ``entry_place_count`` (place_n) alone is NOT enough because it is CLEARED on recycle
    (it lives in _RECYCLE_ENTRY_STATE_KEYS) — so after a stop-out + recycle, the #1
    re-entry lever's first place restarts at place_n=1 and produces the SAME cid as the
    very first entry → collision (CTNT sid 9763, 2026-06-29). We fold the PERSISTENT
    recycle counters (``trade_cycles`` / ``stopout_cycles``, deliberately KEPT across a
    recycle) into the seed so each recycle's attempts get a distinct namespace.

    IDEMPOTENT by construction: a true RETRY of the SAME place attempt — same recycle
    cycle (same trade_cycles/stopout_cycles) and same place_n — yields the SAME cid, so a
    benign double-submit still de-dupes at the broker. Two consecutive re-entries on a
    recycled session differ because trade_cycles advanced. Pure + side-effect-free."""
    _corr = str(correlation_id or "x")
    _seed = f"{session_id}|{_corr}|entry|c{int(trade_cycles)}s{int(stopout_cycles)}|{int(place_n)}".encode("utf-8")
    _suffix = hashlib.sha1(_seed).hexdigest()[:10]
    return f"chili_ml_e_{session_id}_{_corr[:8]}_{_suffix}"[:120]


def _adaptive_notional_guard_multiplier(*, expected_move_bps: float | None) -> float:
    """Marketable-limit premium over the ask. Base = the documented notional-guard bps
    (25 today); on a volatile name widen toward a fraction of its expected move so the
    limit actually clears a wide offer, capped at the adaptive max-spread. With
    ``guard_move_ratio=0`` (default) ⇒ returns ``_notional_guard_multiplier()`` exactly
    (parity). Pure + side-effect-free."""
    base_mult = _notional_guard_multiplier()
    try:
        ratio = float(getattr(settings, "chili_momentum_entry_guard_move_ratio", 0.0) or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0
    if expected_move_bps is None or ratio <= 0:
        return base_mult
    base_bps = max(0.0, (base_mult - 1.0) * 10_000.0)
    bps = min(max(base_bps, expected_move_bps * ratio), _adaptive_live_max_spread_bps(expected_move_bps))
    return 1.0 + bps / 10_000.0


def _expected_move_bps_from_ohlcv(df: Any) -> float | None:
    """Typical recent 15m bar range in bps (ATR / last close) as an expected-move
    proxy. The BBO spread is a round-trip cost, so the adaptive spread gate
    tolerates proportionally more of it on instruments that actually move this
    much. Returns None when candle data is missing or too thin to be meaningful.
    Pure + side-effect-free for unit testing. (docs/DESIGN/MOMENTUM_LANE.md)"""
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 5:
            return None
        import pandas as pd

        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        prev_close = close.shift(1)
        true_range = pd.concat(
            [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1).dropna()
        if len(true_range) < 1:
            return None
        n = min(14, len(true_range))
        atr = float(true_range.tail(n).mean())
        last_close = float(close.iloc[-1])
        if not math.isfinite(atr) or atr <= 0 or last_close <= 0:
            return None
        return (atr / last_close) * 10_000.0
    except Exception:
        return None


def _conservative_em_fallback_bps(df: Any, price: float | None) -> float | None:
    """FIX A — CONSERVATIVE expected-move (bps) for a cold/thin frame, derived from
    the name's OWN data the stop already trusts. Used ONLY when the primary
    ``_expected_move_bps_from_ohlcv`` returns None (frame too thin to ATR) so the
    adaptive spread cap scales instead of collapsing to the 12bps floor.

    Two conservative tiers, in order of preference:
      1. RELAXED realized range — the SAME true-range basis the stop's expected-move
         uses, but tolerating as few as 2 bars (vs the primary's 5-bar floor), then
         SHRUNK by ``..._fallback_shrink`` (default 0.5). Shrinking guarantees this is
         an UNDER-estimate of the move on a thin, noisy sample -> we never loosen the
         cap more than a confident full-frame estimate would.
      2. PRICE-TIER floor — when there are literally no usable candles, ascribe a
         conservative per-bar move scaled UP for lower-priced names (which structurally
         carry wider bps spreads at the same dollar tick). A sub-$5 name gets the full
         ``..._price_tier_bps`` (default 150); it tapers to 0 by ~$20 so liquid
         higher-priced names keep the tight floor (no free loosening).

    Pure + side-effect-free. Returns None when neither tier yields a finite positive
    value (the caller then keeps the existing collapse-to-floor behavior)."""
    try:
        shrink = float(getattr(settings, "chili_momentum_spread_cap_em_fallback_shrink", 0.5) or 0.0)
    except (TypeError, ValueError):
        shrink = 0.5
    if not math.isfinite(shrink) or shrink < 0:
        shrink = 0.0
    # Tier 1: relaxed realized range (>=2 bars) on the same true-range basis as the stop.
    try:
        if df is not None and not getattr(df, "empty", True) and len(df) >= 2:
            import pandas as pd

            high = df["High"].astype(float)
            low = df["Low"].astype(float)
            close = df["Close"].astype(float)
            prev_close = close.shift(1)
            true_range = pd.concat(
                [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
                axis=1,
            ).max(axis=1).dropna()
            if len(true_range) >= 1:
                n = min(14, len(true_range))
                atr = float(true_range.tail(n).mean())
                last_close = float(close.iloc[-1])
                if math.isfinite(atr) and atr > 0 and last_close > 0:
                    em = (atr / last_close) * 10_000.0 * shrink
                    if math.isfinite(em) and em > 0:
                        return em
    except Exception:
        pass
    # Tier 2: price-tier floor (no usable candles at all).
    try:
        tier_bps = float(getattr(settings, "chili_momentum_spread_cap_em_fallback_price_tier_bps", 150.0) or 0.0)
    except (TypeError, ValueError):
        tier_bps = 150.0
    if tier_bps <= 0 or not math.isfinite(tier_bps):
        return None
    try:
        px = float(price) if price is not None else None
    except (TypeError, ValueError):
        px = None
    if px is None or not math.isfinite(px) or px <= 0:
        return None
    # Linear taper: full tier at <= $5, zero at >= $20. Lower-priced low-floats keep
    # the floor; liquid higher-priced names get no loosening from this tier.
    if px <= 5.0:
        scale = 1.0
    elif px >= 20.0:
        scale = 0.0
    else:
        scale = (20.0 - px) / 15.0
    em = tier_bps * scale
    return em if (math.isfinite(em) and em > 0) else None


def _adaptive_live_max_spread_bps(
    expected_move_bps: float | None, *, fallback_em_bps: float | None = None
) -> float:
    """Live spread cap, volatility-relative: ``max(base_floor, ratio x expected
    move)``. Reuses the shared, tested policy helper so the runner BBO gate and
    the pre-entry risk evaluator agree on the same adaptive tolerance. Reads the
    documented base floor + ratio knobs from settings (no inline magic).

    FIX A: when ``expected_move_bps`` is None (cold/thin frame) AND the flag
    ``chili_momentum_spread_cap_em_fallback_enabled`` is on, ``fallback_em_bps`` (a
    CONSERVATIVE name-own-data estimate from ``_conservative_em_fallback_bps``) is
    substituted so the cap scales instead of collapsing to the 12bps floor. The
    fallback is an under-estimate and the abs_cap still hard-caps, so a toxic wide
    spread on a genuinely small-move name STILL blocks (win-win invariant). When the
    flag is off OR no fallback is supplied, behavior is byte-identical to before."""
    from .risk_policy import adaptive_max_spread_bps

    try:
        raw_base = getattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
        base = 12.0 if raw_base is None else float(raw_base)
    except (TypeError, ValueError):
        base = 12.0
    try:
        raw_ratio = getattr(settings, "chili_momentum_risk_spread_to_expected_move_ratio", 0.5)
        ratio = 0.5 if raw_ratio is None else float(raw_ratio)
    except (TypeError, ValueError):
        ratio = 0.5
    try:
        raw_cap = getattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 300.0)
        abs_cap = 300.0 if raw_cap is None else float(raw_cap)
    except (TypeError, ValueError):
        abs_cap = 300.0
    em = expected_move_bps
    used_fallback_em = False
    if (
        em is None
        and fallback_em_bps is not None
        and bool(getattr(settings, "chili_momentum_spread_cap_em_fallback_enabled", True))
    ):
        em = fallback_em_bps
        used_fallback_em = True
    # STEP-E #15: EM-scale the abs cap so a legitimately-wide low-float (whose OWN measured
    # expected move justifies a wide ceiling) isn't clamped to 300. Applied ONLY to a REAL
    # expected_move_bps — NOT to the conservative fallback substitution (a fabricated EM must
    # not relax the hard "spread too wide -> skip" backstop; the fixed abs_cap still hard-caps
    # the fallback path). Missing EM entirely => base floor (fail-closed).
    em_scale_k: float | None = None
    if (
        not used_fallback_em
        and bool(getattr(settings, "chili_momentum_risk_spread_abs_cap_em_scale_enabled", True))
    ):
        try:
            em_scale_k = float(getattr(settings, "chili_momentum_risk_spread_abs_cap_em_scale_k", 1.0) or 1.0)
        except (TypeError, ValueError):
            em_scale_k = 1.0
    return adaptive_max_spread_bps(
        base, em, ratio, abs_cap_bps=abs_cap, abs_cap_em_scale_k=em_scale_k
    )


def _live_entry_quote_gate_applies(sess: TradingAutomationSession, le: dict[str, Any]) -> bool:
    state = sess.state
    if state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE, STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE):
        return True
    return state == STATE_LIVE_PENDING_ENTRY and not le.get("entry_submitted")


_HELD_LIVE_STATES = frozenset(
    {STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT}
)


def _stop_vol_floor_mult() -> float:
    """Fraction of the live expected-move the stop must clear to sit outside the
    noise (default 0.5). One documented knob; everything else derived."""
    try:
        v = float(getattr(settings, "chili_momentum_risk_stop_vol_floor_mult", 0.5))
    except (TypeError, ValueError):
        return 0.5
    return v if v >= 0 else 0.0


def _require_live_atr_mode() -> str:
    """WT-1 3-state knob: 'off' | 'observe' (default) | 'enforce'.

    Resolves the thin-frame stop-fallback guard mode. On a frame too thin to compute
    a LIVE expected-move, the stop-ATR sizing falls back to the regime DEFAULT (~1.5%%)
    — a noise-tight stop on a low-float that gaps THROUGH it (the -$697 tail). 'observe'
    is byte-identical to today (only emits a counter); 'enforce' abstains (no-data ->
    no-trade). Unknown/garbage values fail to the safe-but-byte-identical 'observe'."""
    try:
        v = str(getattr(settings, "chili_momentum_require_live_atr_for_entry", "observe") or "observe").strip().lower()
    except (TypeError, ValueError):
        return "observe"
    return v if v in ("off", "observe", "enforce") else "observe"


def _parse_event_times_utc(raw: str | None) -> list[datetime]:
    """Parse the comma-separated ISO-8601 UTC event-time list (E3). Pure; skips junk.
    Accepts a trailing 'Z' (mapped to +00:00); naive datetimes are assumed UTC."""
    out: list[datetime] = []
    if not raw:
        return out
    for tok in str(raw).split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            t = datetime.fromisoformat(tok.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            out.append(t.astimezone(timezone.utc))
        except (TypeError, ValueError):
            continue
    return out


def hard_no_trade_regime(
    execution_family: str | None = None,
    *,
    symbol: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """HARD no-NEW-ENTRY regime gate (Batch E(3), ENTRIES ONLY). Returns ``(blocked, reason)``.

    Two hard windows, both gated by ``chili_momentum_hard_no_trade_regime_enabled``:
      (a) EVENT STANDDOWN — within +/- ``chili_momentum_hard_no_trade_event_window_min`` of
          any scheduled high-impact event (FOMC/CPI) in
          ``chili_momentum_hard_no_trade_event_times_utc`` (a small documented list / calendar
          hook). Market-wide (applies to crypto + equity).
      (b) HARD MIDDAY — when ``chili_momentum_hard_no_trade_midday_enabled`` is on, the SAME
          10:30-14:30 ET ``in_midday_lull`` band as the soft de-weight (equity-only).

    CRITICAL: this gates only the ARMING / entry-eval path. It is NEVER consulted on an exit,
    stop, trail, bailout, scale-out, or the overnight dark-flatten — managing/closing an OPEN
    position is always allowed. OFF => ``(False, 'disabled')`` (byte-identical). Pure: reads
    only settings + the shared clock; fail-open (any error => not blocked)."""
    if not bool(getattr(settings, "chili_momentum_hard_no_trade_regime_enabled", False)):
        return False, "disabled"
    ref = now or _utcnow_aware()  # replay v3: sim-clock-governed (prod byte-identical)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    try:
        win_min = float(getattr(settings, "chili_momentum_hard_no_trade_event_window_min", 30.0) or 0.0)
    except (TypeError, ValueError):
        win_min = 0.0
    if win_min > 0:
        for ev in _parse_event_times_utc(
            getattr(settings, "chili_momentum_hard_no_trade_event_times_utc", "")
        ):
            if abs((ref - ev).total_seconds()) <= win_min * 60.0:
                return True, f"event_standdown:{ev.isoformat()}"
    if bool(getattr(settings, "chili_momentum_hard_no_trade_midday_enabled", False)):
        try:
            from .market_profile import in_midday_lull

            # in_midday_lull is equity-only (crypto always False). The arm-gate caller
            # passes the lane execution family, not a symbol — only the equity lane has a
            # midday concept, so a None/equity family evaluates the clock; crypto is exempt.
            _is_crypto = (
                bool(symbol) and str(symbol).upper().endswith("-USD")
            ) or str(execution_family or "") == "coinbase_spot"
            if not _is_crypto and in_midday_lull(symbol, now=ref):
                return True, "hard_midday_no_entry"
        except Exception:
            pass
    return False, "ok"


def _midday_viability_bump() -> float:
    """Additive raise to entry_viability_min during the 10:30-14:30 ET midday lull
    (project_profitability_levers): the live data shows a 6% midday win-rate vs 29%
    morning, so the lane should demand a HIGHER bar to admit a NEW entry in the chop.
    Kill-switch OFF (or bump<=0) => 0.0 => byte-identical (caller never raises the
    bar, never emits). Entry-side only; never reaches an exit path."""
    if not bool(getattr(settings, "chili_momentum_midday_deweight_enabled", False)):
        return 0.0
    try:
        v = float(getattr(settings, "chili_momentum_midday_viability_bump", 0.05) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def _effective_entry_viability_min(
    flat_min: float, symbol: str | None, *, now: datetime | None = None
) -> tuple[float, bool, float]:
    """Effective entry-viability bar at the WATCHING_LIVE advance, applying the midday
    de-weight. Returns ``(eff_min, in_lull, bump)``.

    OFF / bump<=0 / outside the lull / crypto  => ``(flat_min, False, bump)`` so the
    caller's ``_score_ok`` is byte-identical. Equity inside the 10:30-14:30 ET lull =>
    ``(min(0.95, flat_min + bump), True, bump)`` — a SOFT raise clamped to the schema
    ceiling. Pure + unit-testable: reads only settings + the shared clock, never reads
    or mutates a position, order, or exit. (project_profitability_levers)"""
    bump = _midday_viability_bump()
    if not bump:
        return float(flat_min), False, 0.0
    from .market_profile import in_midday_lull

    if not in_midday_lull(symbol, now=now):
        return float(flat_min), False, bump
    return min(0.95, float(flat_min) + bump), True, bump


def _raise_only_entry_floor(
    current_eff_min: float, raised_floor_snapshot: float, *, enabled: bool = True
) -> float:
    """WAVE-1 FIX-7 SCORE-FLOOR RAISE-ONLY INVARIANT (pure + unit-testable).

    The entry viability floor is composed as ``min(0.95, flat_min + midday_bump +
    run_r_bump)`` — every risk factor RAISES the bar, none lowers it. This helper is the
    invariant guard: ``raised_floor_snapshot`` is the fully-raised floor captured
    IMMEDIATELY after every raise is applied; ``current_eff_min`` is the value about to hit
    the ``_score_ok`` gate (which a future override could have LOWERED). The guard returns
    ``max(current, snapshot)`` so the gate floor can NEVER fall below the risk-raised bar
    (the codex ``ross_audio_starter`` class of override, absent on main). On main the two
    args are equal, so this is an identity today (byte-identical ``_score_ok``).
    ``enabled=False`` returns ``current_eff_min`` unchanged (rollback)."""
    try:
        cur = float(current_eff_min)
        snap = float(raised_floor_snapshot)
    except (TypeError, ValueError):
        # Fail-safe: an unusable input can never lower the floor — return the caller's
        # current value UNCHANGED (raw, not re-coerced) so the gate falls back to it.
        return current_eff_min
    if not enabled:
        return cur
    return max(cur, snap)


_INTERVAL_SECONDS: dict[str, float] = {
    "1m": 60.0, "2m": 120.0, "5m": 300.0, "15m": 900.0, "30m": 1800.0,
    "60m": 3600.0, "90m": 5400.0, "1h": 3600.0, "1d": 86400.0,
}


_DAY_PNL_CACHE: dict[str, float] = {"at": 0.0, "v": 0.0}

_SCALP_HOLD_CACHE: dict[str, Any] = {"at": 0.0, "v": None}


def _recent_scalp_median_hold_s(db: Session, user_id: int) -> "float | None":
    """LIVE-derived holding horizon for the vol-norm trail (LEVER 2A): the MEDIAN
    realized hold (seconds) of this user's recent CLOSED momentum scalps. Read from
    ``momentum_automation_outcomes.hold_seconds`` over the last N closed sessions —
    NOT a fixed clock; it tracks how long this lane's scalps actually run. 300s-cached.
    Returns None on no history (caller falls back to this session's own elapsed hold)."""
    import time as _time

    if _time.monotonic() - float(_SCALP_HOLD_CACHE["at"]) < 300.0:
        return _SCALP_HOLD_CACHE["v"]
    v: "float | None" = None
    try:
        from sqlalchemy import text as _sql

        rows = db.execute(
            _sql(
                "SELECT hold_seconds FROM momentum_automation_outcomes "
                "WHERE user_id = :u AND hold_seconds IS NOT NULL AND hold_seconds > 0 "
                "ORDER BY terminal_at DESC LIMIT 50"
            ),
            {"u": int(user_id)},
        ).fetchall()
        vals = sorted(float(r[0]) for r in rows if r[0] is not None)
        if vals:
            n = len(vals)
            v = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    except Exception:
        v = None
    _SCALP_HOLD_CACHE["at"] = _time.monotonic()
    _SCALP_HOLD_CACHE["v"] = v
    return v


def _day_realized_usd_cached(db: Session, user_id: int) -> float:
    """Today's GLOBAL realized PnL (ET), 60s-cached — the cushion input for the
    adaptive trail. Fail-safe 0.0 (= tightest patience) on any error."""
    import time as _time

    if _time.monotonic() - _DAY_PNL_CACHE["at"] < 60.0:
        return _DAY_PNL_CACHE["v"]
    v = 0.0
    try:
        from ..governance import global_realized_pnl_today_et

        v = float(global_realized_pnl_today_et(db, user_id)["total_usd"])
    except Exception:
        v = 0.0
    _DAY_PNL_CACHE["at"] = _time.monotonic()
    _DAY_PNL_CACHE["v"] = v
    return v


def _entry_interval_seconds() -> float:
    iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m").lower()
    return float(_INTERVAL_SECONDS.get(iv, 300.0))


def _opening_bell_suppresses_fresh_trigger(
    symbol: str, le: dict, *, has_position: bool, now: datetime | None = None,
    armed_at: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """HVM101 edge (A) — opening-bell suppression of FRESH triggers.

    The first ~N minutes after the 09:30 ET regular open is opening-auction whipsaw:
    a fresh curl/dip/reversal that fires here is usually noise that reverses. SUPPRESS
    a brand-new entry in that window — but NEVER a CONTINUATION of an already-armed
    premarket runner (Ross's gap-and-go that armed/watched before the bell), and never
    an already-held position's management.

    CONTINUATION exemption (the fix): a name that was ARMED / watching BEFORE the
    regular open is a premarket gap-and-go continuation, NOT a fresh open-auction
    print — so it is EXEMPT even though it has not filled yet (``has_position`` is
    False while WATCHING_LIVE). We key the continuation off the session's arm time
    (``armed_at`` = ``sess.started_at``): if the session armed before ~the open, the
    break we're confirming is a continuation of the premarket setup, not a freshly-
    armed post-open trigger. ONLY a genuinely-fresh trigger — armed at/after the open
    AND now inside the first ~N min — is suppressed. (Previously this keyed only off
    FILLED/has_position, which wrongly suppressed un-filled premarket continuations.)

    Adaptive N: base ``chili_momentum_opening_bell_suppress_base_min`` (the ONE
    documented base ~2 min), widened by the instrument's own opening volatility via a
    day-range/ATR multiple when present in ``le`` — a calmer name clears fast, a wild
    opener stays suppressed a touch longer. No scattered magic clocks.

    EQUITY/RH ONLY (microstructure edge): crypto (``-USD``) and any
    premarket/weekend/closed clock → ``minutes_since_regular_open`` is ``None`` → FAIL
    OPEN (no suppression). Returns ``(suppress, debug)``; pure + side-effect-free.
    """
    debug: dict[str, Any] = {}
    sym = str(symbol or "").upper()
    if sym.endswith("-USD"):
        return False, debug  # crypto: 24/7, no opening bell
    if has_position:
        return False, debug  # holding already — management, never a fresh trigger
    try:
        from .market_profile import minutes_since_regular_open

        _mins = minutes_since_regular_open(sym, now=now)
    except Exception:
        _mins = None
    if _mins is None or _mins < 0:
        # crypto / weekend / closed / premarket → fail open (no suppression).
        # A premarket-armed runner that is now continuing INTO the open reads as
        # _mins>=0 here; that CONTINUATION is preserved by the armed-before-open
        # exemption below (we no longer rely on it being in a held/candidate state).
        return False, debug
    # CONTINUATION: the session armed BEFORE the regular open (premarket gap-and-go).
    # ``armed_at`` is the session's start time; reuse the SAME DST-correct open clock
    # to ask "was this armed before the bell?" — if so, the break we're confirming is
    # a continuation of the premarket setup, not a fresh open-auction print → EXEMPT.
    # A tiny tolerance (a fraction of the base window) absorbs same-instant arm/score
    # timing jitter at the open without exempting a clearly post-open fresh arm.
    if armed_at is not None:
        try:
            from .market_profile import minutes_since_regular_open as _msro

            _armed_mins = _msro(sym, now=armed_at)
        except Exception:
            _armed_mins = None
        if _armed_mins is not None and _armed_mins < 0:
            debug = {
                "exempt": "premarket_armed_continuation",
                "armed_mins_since_open": round(float(_armed_mins), 2),
            }
            return False, debug
    base_min = float(getattr(settings, "chili_momentum_opening_bell_suppress_base_min", 2.0) or 0.0)
    if base_min <= 0:
        return False, debug
    # Adaptive widen: scale the base by the opener's realized volatility relative to a
    # ~5% reference opening range (Ross small-cap). A 2x-vol opener earns up to ~2x the
    # base window; clamped to [1.0, 2.0] so it can never run away.
    vol_ref = None
    for _k in ("entry_day_range_pct", "day_range_pct", "entry_stop_atr_pct", "regime_atr_pct"):
        _v = le.get(_k) if isinstance(le, dict) else None
        try:
            if _v is not None and float(_v) > 0:
                vol_ref = float(_v)
                break
        except (TypeError, ValueError):
            continue
    widen = 1.0
    if vol_ref is not None:
        widen = max(1.0, min(2.0, vol_ref / 0.05))
    suppress_window = base_min * widen
    debug = {
        "mins_since_open": round(float(_mins), 2),
        "base_min": round(base_min, 2),
        "widen": round(widen, 2),
        "suppress_window_min": round(suppress_window, 2),
    }
    return (0.0 <= _mins < suppress_window), debug


def _bid_prop_confirms_break(
    db, symbol: str, *, window_s: float, now: datetime | None = None
) -> tuple[bool, dict[str, Any]]:
    """HVM101 edge (B) — bid-prop / book-deterioration confirmer.

    A genuine break can momentarily widen the spread as the ask lifts into the next
    level — that is NORMAL and must NOT be vetoed. We only veto when the book is
    clearly DETERIORATING UNDER the move: the best-bid is net STEPPING DOWN across the
    samples (buyers backing away, not merely one noisy tick) AND the spread has blown
    out BEYOND its own short trailing median by a margin (an air pocket opening up),
    not on a single normal widen. Both conditions must hold together to veto.

    Adaptive margin: the ONE documented base ``chili_momentum_bid_prop_spread_blowout_mult``
    (~1.5×) sets how far past the trailing median the LATEST spread must blow out before
    it counts as deterioration — a name's own recent spread is the reference, no fixed
    bps clock. Bid "stepping down" is measured net (last vs first) past a tiny relative
    epsilon so micro-noise doesn't read as a falling bid.

    FAIL-OPEN by contract: thin/absent/stale L1 tape (fewer than the min samples) →
    returns ``(True, ...)`` so it NEVER blocks a break on missing data — it only ADDS a
    veto when the tape POSITIVELY shows a falling bid into a blown-out spread.

    EQUITY/RH ONLY: callers gate to non ``-USD`` symbols (crypto L1 lives elsewhere).
    Returns ``(confirmed, debug)``; pure read, never raises.
    """
    try:
        from .nbbo_tape import recent_bid_spread_tape

        _min_n = int(getattr(settings, "chili_momentum_bid_prop_min_samples", 3) or 3)
        _max_rows = max(_min_n, int(getattr(settings, "chili_momentum_bid_prop_max_samples", 8) or 8))
        tape = recent_bid_spread_tape(db, symbol, window_s=float(window_s), max_rows=_max_rows, now_utc=now)
    except Exception:
        return True, {"reason": "bid_prop_read_error_fail_open"}
    if not tape or len(tape) < max(2, _min_n):
        # Thin/absent/stale L1 → fail open (do NOT block the break).
        return True, {"reason": "bid_prop_thin_tape_fail_open", "samples": len(tape or [])}
    bids = [b for b, _ in tape]
    spreads = [s for _, s in tape]
    last_bid = bids[-1]
    first_bid = bids[0]
    # DETERIORATION half 1 — best-bid net stepping DOWN. Measured first→last past a tiny
    # relative epsilon (0.05% of the latest bid) so a single noisy down-tick on an
    # otherwise rising bid does NOT count; only a genuine backing-away (the bid is lower
    # now than where the window started) reads as deterioration.
    eps = abs(last_bid) * 0.0005
    bid_stepping_down = last_bid < first_bid - eps
    # DETERIORATION half 2 — spread BLOWN OUT beyond its own trailing median by a margin.
    # The trailing median is the name's own recent spread; the latest spread must exceed
    # it by ``blowout_mult`` to count as an air pocket (a single normal widen sits at
    # ~1.0× and is tolerated).
    _srt = sorted(spreads)
    _m = len(_srt)
    median_spread = _srt[_m // 2] if _m % 2 else (_srt[_m // 2 - 1] + _srt[_m // 2]) / 2.0
    blowout_mult = float(getattr(settings, "chili_momentum_bid_prop_spread_blowout_mult", 1.5) or 1.5)
    if blowout_mult < 1.0:
        blowout_mult = 1.0
    spread_blown_out = spreads[-1] > (median_spread * blowout_mult) + 1e-9
    # VETO only when the book is CLEARLY deteriorating: bid backing away AND spread
    # blowing out together. A normal breakout (rising/holding bid, momentary widen) does
    # not satisfy both, so it is CONFIRMED (no veto).
    deteriorating = bool(bid_stepping_down and spread_blown_out)
    confirmed = not deteriorating
    debug = {
        "samples": len(tape),
        "bid_first": round(first_bid, 6),
        "bid_last": round(last_bid, 6),
        "bid_stepping_down": bid_stepping_down,
        "spread_last_bps": round(spreads[-1], 2),
        "spread_median_bps": round(median_spread, 2),
        "spread_blowout_mult": round(blowout_mult, 2),
        "spread_blown_out": spread_blown_out,
    }
    if not confirmed:
        debug["reason"] = "bid_prop_book_deteriorating"
    return confirmed, debug


def _micro_bar_df_from_session(db, symbol: str, *, bar_seconds: int, lookback_minutes: float):
    """The raw micro-bar build off ONE session (may raise). Split out so the public
    ``_build_micro_bar_df`` can RETRY once on a fresh short-lived session (WAVE-4 ITEM-7 F2)."""
    from datetime import timedelta as _td

    from sqlalchemy import text as _text

    from .micro_bars import _resample_micro_bars

    # replay v3: anchor the lookback on the sim clock (prod byte-identical — _utcnow()
    # is naive-UTC, exactly what datetime.now(utc).replace(tzinfo=None) produced here).
    since = _utcnow() - _td(minutes=float(lookback_minutes))
    rows = db.execute(
        _text(
            "SELECT observed_at, bid, ask FROM momentum_nbbo_spread_tape "
            "WHERE symbol = :s AND observed_at >= :since AND bid > 0 AND ask > 0 "
            "ORDER BY observed_at ASC"
        ),
        {"s": str(symbol).upper(), "since": since},
    ).fetchall()
    if not rows or len(rows) < 2:
        return None
    df = _resample_micro_bars(
        [(r[0], float(r[1]), float(r[2])) for r in rows], bar_seconds=bar_seconds
    )
    # The trigger needs >=10 bars to evaluate (pullback_break_confirmation's own
    # ``len(df) < 10`` floor). A name with only 1-min snapshots resamples to far
    # fewer micro-bars, so this is exactly the SUPERSET/FAIL-SAFE boundary: too
    # sparse ⇒ return None ⇒ the caller uses the 1m df (byte-identical). Enforcing
    # the floor HERE keeps the density decision in one place.
    if df is None or getattr(df, "empty", True) or len(df) < 10:
        return None
    return df


def _build_micro_bar_df(db, symbol: str, *, bar_seconds: int, lookback_minutes: float = 30.0, meta: dict | None = None):
    """15s MICRO-PULLBACK (2026-06-15, "1m too slow"): build an OHLC micro-bar df
    from the densified tick tape (``momentum_nbbo_spread_tape`` rows for ``symbol``,
    mirrored from the IQFeed trade tape, over the last ``lookback_minutes``) so the
    first-pullback trigger can run sub-minute. Returns a Open/High/Low/Close/Volume
    DataFrame (same shape as ``fetch_ohlcv_df``) or None.

    FAIL-SAFE / SUPERSET: insufficient tick DENSITY (only the 1-min sampler exists
    for this name) ⇒ ``_resample_micro_bars`` yields <2 micro-bars ⇒ this returns
    None ⇒ the caller falls back to the 1m bars (byte-identical). The micro path can
    only ADD an earlier entry where the dense tape supports it, never break the 1m path.

    WAVE-4 ITEM-7 F1: a swallowed build EXCEPTION is no longer silent — it is logged
    with exc_info AND (when ``meta`` is supplied) recorded as ``meta["micro_error_detail"]``
    so the operator can see WHY a name silently degraded off the micro frame.
    WAVE-4 ITEM-7 F2: on a build error, RETRY ONCE on a FRESH short-lived SessionLocal
    (the tape is in-DB; a transient session error must not silently drop the micro frame
    to the 1m/5m path) before falling back. Gated by
    ``chili_momentum_micro_fallback_1m_from_ticks_enabled`` (default True); OFF ⇒ the
    legacy single-attempt swallow (byte-identical).
    """
    try:
        return _micro_bar_df_from_session(
            db, symbol, bar_seconds=bar_seconds, lookback_minutes=lookback_minutes
        )
    except Exception as exc:
        # F1: surface the swallowed error (log + meta), never re-raise into the tick loop.
        if isinstance(meta, dict):
            meta["micro_error_detail"] = repr(exc)
        _log.warning(
            "[momentum_live] micro-bar build failed sym=%s bar_s=%s: %s",
            symbol, bar_seconds, exc, exc_info=True,
        )
        # F2: retry ONCE on a fresh short-lived session (the tape is in-DB; a stale/errored
        # session must not silently drop the micro frame). OFF ⇒ legacy single-attempt.
        if bool(getattr(settings, "chili_momentum_micro_fallback_1m_from_ticks_enabled", True)):
            try:
                from ....db import SessionLocal as _SessionLocal

                _retry_db = _SessionLocal()
                try:
                    _df = _micro_bar_df_from_session(
                        _retry_db, symbol, bar_seconds=bar_seconds, lookback_minutes=lookback_minutes
                    )
                    if isinstance(meta, dict) and _df is not None:
                        meta["micro_retry_recovered"] = True
                    return _df
                finally:
                    try:
                        _retry_db.rollback()
                    except Exception:
                        pass
                    _retry_db.close()
            except Exception as exc2:
                if isinstance(meta, dict):
                    meta["micro_retry_error_detail"] = repr(exc2)
                _log.warning(
                    "[momentum_live] micro-bar retry failed sym=%s: %s", symbol, exc2, exc_info=True,
                )
        return None


def _pending_entry_cancel_reason(
    *,
    bid: float | None,
    structural_stop: float,
    limit_px: float,
    elapsed_s: float | None,
    rest_bars: float,
    interval_s: float,
    chase_ceiling_px: float = 0.0,
) -> str | None:
    """EVENT-DRIVEN pending-entry lifecycle decision (operator 2026-06-11: "the
    right question is not how many seconds — it is what INVALIDATES the order").

    Returns the cancel reason, or None to keep resting:
      * ``entry_invalidated_stop_breach`` — live bid broke the structural stop:
        the setup died; a fill now would be an instant bailout. (Checked FIRST,
        unconditional — never subject to the chase ceiling.)
      * ``entry_limit_left_behind`` — live bid is ABOVE our buy limit BY MORE THAN
        the adaptive ``chase_ceiling_px``: the move left without us. With the
        default ceiling (0 ⇒ ceiling = limit_px) this is the original
        cancel-on-first-tick; a non-zero ceiling TOLERATES the resting marketable
        limit while the bid pips just past it (the fix for cancelling orders that
        are at the front of the book and about to fill — BATL/CTNT/SDOT orphans).
        It only TOLERATES the existing resting order — it never re-pegs the price up.
      * ``entry_rest_backstop`` — the order outlived the bar evidence that
        produced it (N entry-interval BARS — no free seconds).
    Pure + side-effect-free.
    """
    if bid is not None and structural_stop > 0 and bid < structural_stop:
        return "entry_invalidated_stop_breach"
    if bid is not None and limit_px > 0:
        ceiling = chase_ceiling_px if chase_ceiling_px > limit_px else limit_px
        if bid > ceiling:
            return "entry_limit_left_behind"
    if elapsed_s is not None and elapsed_s > max(0.5, rest_bars) * interval_s:
        return "entry_rest_backstop"
    return None


def _breakout_bailout_window_seconds() -> float:
    """Early window (seconds) for the #2 breakout-or-bailout fast exit = N
    entry-interval bars. One documented knob (bars), derived from the configured
    timeframe so it stays adaptive to 1m vs 5m. (docs/DESIGN/MOMENTUM_LANE.md §8)"""
    try:
        bars = float(getattr(settings, "chili_momentum_breakout_bailout_max_bars", 2.0) or 0.0)
    except (TypeError, ValueError):
        bars = 2.0
    return max(0.0, bars) * _entry_interval_seconds()


def _via_atr_pct(via: Any) -> float | None:
    """Best-effort clean intraday-range ATR%% from a viability's regime snapshot (the
    SAME basis the entry-extension veto uses). None on any error. Pure read."""
    try:
        rg = getattr(via, "regime_snapshot_json", None)
        if not isinstance(rg, dict):
            return None
        ap = regime_atr_pct(rg)
        return None if ap is None else float(ap)
    except Exception:
        return None


def _session_is_explosive(via: Any, *, rvol: float | None = None) -> bool:
    """MASTER-gated explosiveness read for the recalibration carve-outs (bid-prop
    exempt, fast-bail lock-in). Uses the clean regime ATR%% (always present on a live
    viability) OR an optional RVOL reading. Delegates the floors + the master gate to
    entry_gates.is_explosive_mover (one source of truth). Fail-closed: any error /
    master OFF ⇒ False (no name treated as explosive ⇒ the protective gate stays)."""
    try:
        from .entry_gates import is_explosive_mover

        return bool(is_explosive_mover(_via_atr_pct(via), rvol, settings))
    except Exception:
        return False


def _adaptive_scale_vol_pctl(le: dict) -> float | None:
    """DESIGN #3 — the name's realized intraday-vol percentile in [0,1] for the
    vol-aware scale-out tilt, derived from the stashed entry_day_range_pct mapped
    through the documented reference range (chili_momentum_adaptive_scale_vol_ref_pct,
    the Ross ~5% small-cap opening range — the SAME 0.05 reference the opening-bell
    widen uses). Returns None (no tilt, byte-identical) when the day-range
    was not stashed. p>0.5 => higher-than-reference vol => smaller partial / bigger
    runner. Pure read off `le`; fail-neutral."""
    if not isinstance(le, dict):
        return None
    dr = _float_or_none(le.get("entry_day_range_pct"))
    if dr is None or dr <= 0:
        return None
    try:
        ref = float(getattr(settings, "chili_momentum_adaptive_scale_vol_ref_pct", 0.05) or 0.05)
    except (TypeError, ValueError):
        ref = 0.05
    if not math.isfinite(ref) or ref <= 0:
        ref = 0.05
    # Map range -> [0,1] with the reference at the median: range==ref -> 0.5,
    # range==2*ref -> 1.0, range->0 -> 0.0. Bounded, no batch fetch needed.
    p = 0.5 * (dr / ref)
    return max(0.0, min(1.0, p))


def _session_squeeze_rank_pct(via: Any, symbol: str) -> float | None:
    """P4 — the session's OWN within-batch squeeze percentile (``squeeze_fuel_rank_pct``), read
    from its persisted scanner row (execution_readiness_json.extra.ross_signals[SYM]) — the SAME
    source the arm-queue ranker + entry size-up read, no new fetch. Returns the [0,1] rank or
    ``None`` (no squeeze data for the name ⇒ the exit band-widen is a no-op, byte-identical). Pure."""
    try:
        _ex = via.execution_readiness_json if isinstance(via.execution_readiness_json, dict) else {}
        _extra = (_ex.get("extra") or {}) if isinstance(_ex, dict) else {}
        _sig_map = _extra.get("ross_signals") if isinstance(_extra.get("ross_signals"), dict) else {}
        _row = _sig_map.get(str(symbol or "").upper()) if isinstance(_sig_map, dict) else None
        if isinstance(_row, dict):
            return _float_or_none(_row.get("squeeze_fuel_rank_pct"))
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def _squeeze_exit_band_widen_factor(via: Any, symbol: str) -> float:
    """P4(2) — the bounded RIDE-band WIDEN factor in [1.0, max_widen] for this session, driven by
    its OWN within-batch squeeze percentile (extreme-tail only). 1.0 (no-op, byte-identical) when
    the flag is OFF, no squeeze data, or the name is below the extreme tail. INVARIANT-A SAFE: the
    caller multiplies the smart-hold / volnorm trail ``k`` by this BEFORE band placement (a wider
    candidate band lowers the trail candidate, which composes through max(stop, be, candidate) —
    never loosens a placed stop). Fail-neutral to 1.0."""
    if not bool(getattr(settings, "chili_momentum_squeeze_exit_hold_enabled", True)):
        return 1.0
    try:
        from .ross_momentum import squeeze_exit_band_widen as _sq_widen

        _rank = _session_squeeze_rank_pct(via, symbol)
        _factor, _ = _sq_widen(
            _rank,
            tail_pctl=float(getattr(settings, "chili_momentum_squeeze_exit_tail_pctl", 0.90) or 0.90),
            max_widen=float(getattr(settings, "chili_momentum_squeeze_exit_max_widen", 1.50) or 1.50),
        )
        return _safe_mult(_factor)
    except Exception:
        return 1.0


def _latest_rvol(db, symbol: str) -> float | None:
    """Best-effort latest relative-volume (volume_ratio of the most recent bar) for the
    explosive carve-outs. Reads the SESSION-scoped micro-bar df from the densified tape
    (no network, same resampler the micro-pullback re-load uses) and computes
    volume_ratio via the canonical indicator core. None on thin tape / any error (the
    caller then treats the name as non-explosive on RVOL ⇒ fail-closed). Pure read."""
    try:
        from ..indicator_core import compute_all_from_df as _rv_compute

        _df = _build_micro_bar_df(db, symbol, bar_seconds=15)
        if _df is None or getattr(_df, "empty", True) or len(_df) < 10:
            return None
        arrays = _rv_compute(_df, needed={"volume_ratio"})
        vr = arrays.get("volume_ratio") or []
        if not len(vr):
            return None
        v = vr[-1]
        return None if v is None else float(v)
    except Exception:
        return None


def _breakout_bailout_lock_in_seconds(*, explosive: bool) -> float:
    """Lock-in floor (seconds) for the fast-bail — BELOW which a momentary sub-level
    dip is NOT treated as a failed breakout. Master-gated: returns 0.0 (byte-identical,
    no lock-in) unless the explosive-recalibration master flag is ON. Explosive names
    get the (wider) explosive lock-in when configured; otherwise the base lock-in."""
    try:
        if not bool(getattr(settings, "chili_momentum_explosive_recalibration_enabled", False)):
            return 0.0
        base = max(0.0, float(getattr(settings, "chili_momentum_breakout_bailout_lock_in_seconds", 0.0) or 0.0))
        if explosive:
            expl = max(0.0, float(getattr(settings, "chili_momentum_breakout_bailout_lock_in_explosive_seconds", 0.0) or 0.0))
            return max(base, expl)
        return base
    except (TypeError, ValueError):
        return 0.0


_BREAK_RESOLUTION_CACHE: dict[str, Any] = {"at": 0.0, "v": None}


def _smart_hold_time_floor_s(db: Session, user_id: int) -> "float | None":
    """GAP-A adaptive TIME-FLOOR (seconds): the q(time_floor_q) of this user's recent
    break-resolution times = the realized holds of recent CLOSED momentum scalps (the
    same ``momentum_automation_outcomes.hold_seconds`` source as the vol-norm horizon).
    Below this, a momentary sub-band dip is suppressed (a healthy retest of the broken
    level typically resolves within the name's own typical resolution time). Falls back
    to None (caller uses 2*bar_seconds) when fewer than ``min_samples`` rows exist.
    300s-cached. Reuses the percentile primitive from hold_signals."""
    import time as _time

    if _time.monotonic() - float(_BREAK_RESOLUTION_CACHE["at"]) < 300.0:
        return _BREAK_RESOLUTION_CACHE["v"]
    v: "float | None" = None
    try:
        from sqlalchemy import text as _sql

        min_n = int(getattr(settings, "chili_momentum_smart_hold_time_floor_min_samples", 5) or 5)
        q = float(getattr(settings, "chili_momentum_smart_hold_time_floor_q", 0.25) or 0.25)
        rows = db.execute(
            _sql(
                "SELECT hold_seconds FROM momentum_automation_outcomes "
                "WHERE user_id = :u AND hold_seconds IS NOT NULL AND hold_seconds > 0 "
                "ORDER BY terminal_at DESC LIMIT 50"
            ),
            {"u": int(user_id)},
        ).fetchall()
        vals = [float(r[0]) for r in rows if r[0] is not None]
        if len(vals) >= max(1, min_n):
            v = _percentile(vals, q)
    except Exception:
        v = None
    _BREAK_RESOLUTION_CACHE["at"] = _time.monotonic()
    _BREAK_RESOLUTION_CACHE["v"] = v
    return v


def _smart_hold_breach_volume(
    db: Session, symbol: str, *, window_s: float
) -> "tuple[float | None, float | None]":
    """GAP-A volume-confirm read: returns ``(breach_window_volume, recent_median_volume)``
    for the price-breach CUT confirmation. The breach-window volume is the summed trade
    size over the most recent ``window_s`` of the equity tape; the recent median is the
    median per-window summed volume over the prior comparable windows (the name's OWN
    recent distribution — adaptive, no magic threshold). Both ``None`` (⇒ the price-CUT
    is NOT volume-confirmed ⇒ HOLD, fail-safe) on a thin tape / crypto / any error.
    Mirrors the existing iqfeed_trade_ticks read pattern."""
    s = (symbol or "").strip().upper()
    if not s or s.endswith("-USD") or db is None:
        return None, None
    try:
        w = max(1.0, float(window_s))
    except (TypeError, ValueError):
        return None, None
    try:
        from sqlalchemy import text as _sql

        # 10 comparable windows of history to form the per-window-volume distribution.
        rows = db.execute(
            _sql(
                "SELECT size, observed_at FROM iqfeed_trade_ticks WHERE symbol = :s AND "
                "observed_at > (now() at time zone 'utc') - make_interval(secs => :w) "
                "ORDER BY observed_at ASC"
            ),
            {"s": s, "w": w * 10.0},
        ).fetchall()
    except Exception:
        return None, None
    if not rows:
        return None, None
    try:
        import datetime as _dt

        parsed = [
            (float(r[0]), r[1])
            for r in rows
            if r[0] is not None and r[1] is not None
        ]
        if not parsed:
            return None, None
        latest = parsed[-1][1]
        # bucket size summed per window, anchored at the latest tick.
        buckets: dict[int, float] = {}
        for sz, ts in parsed:
            try:
                delta = (latest - ts).total_seconds()
            except (TypeError, ValueError, AttributeError):
                continue
            idx = int(delta // w)
            buckets[idx] = buckets.get(idx, 0.0) + float(sz)
        if not buckets:
            return None, None
        breach_vol = buckets.get(0)  # the most-recent (breach) window
        hist = [v for k, v in buckets.items() if k > 0]
        med = _percentile(hist, 0.5) if hist else None
        return breach_vol, med
    except Exception:
        return None, None


def _c1_iqfeed_phantom_loss(
    db: Session,
    symbol: str,
    *,
    in_process_bid: float,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """C1 IQFeed cross-check (2026-06-30, PULLBACK-SCALP-ENABLE): on the C1-trigger path
    ONLY (a max-loss breach is about to fire — rare), cross-check the in-process ``bid``
    against the freshest IQFeed tick-level NBBO mirrored into ``momentum_nbbo_spread_tape``
    (the iqfeed trade bridge writes per-tick L1 there). If the in-process bid is MATERIALLY
    below a FRESH tape bid, the in-process tick is torn/stale and the unrealized loss is
    PHANTOM — the real market is much higher (the CELZ-9920 case: in-process phantom −$148
    while IQFeed showed $4.22+).

    Returns ``(phantom, debug)``. ``phantom=True`` => C1 must SKIP this pulse.

    ADAPTIVE divergence tolerance (no fixed bps clock): the in-process bid must sit below the
    fresh tape bid by MORE than ``mult × recent_median_spread`` for the name — its OWN recent
    spread is the reference scale. When the recent tape spread is unavailable, fall back to
    the ONE documented base ``chili_momentum_max_loss_phantom_divergence_fallback_bps``.

    Recency-gated by the SAME documented floor the IQFeed-L1-first BBO uses
    (``chili_momentum_quote_freshness_floor_seconds``): a tape bid older than that window is
    NOT trusted as truth (=> not phantom; the binary stale-flag guard remains the fail-safe).

    FAIL-CLOSED toward FIRING C1: any missing/thin/stale tape, bad input, or a tape bid that
    CONFIRMS the in-process bid (also low) => ``phantom=False`` => C1 fires (a real loss is
    never suppressed). Pure read; never raises. EQUITY/RH names; crypto (-USD) tape is sparse
    so it simply returns not-phantom (fail-closed)."""
    dbg: dict[str, Any] = {"checked": False}
    try:
        ipb = float(in_process_bid)
        if not (math.isfinite(ipb) and ipb > 0):
            return False, dbg
    except (TypeError, ValueError):
        return False, dbg
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False, dbg
    try:
        from .nbbo_tape import recent_bid_spread_tape

        _window_s = float(
            getattr(settings, "chili_momentum_quote_freshness_floor_seconds", 15.0) or 15.0
        )
        # Pull the last few fresh L1 samples; the NEWEST is the IQFeed truth bid, and the
        # median spread over the window is the adaptive divergence scale.
        tape = recent_bid_spread_tape(
            db, sym, window_s=_window_s, max_rows=8, now_utc=now,
        )
    except Exception:
        return False, dbg
    if not tape:
        return False, dbg
    # recent_bid_spread_tape returns [(bid, spread_bps)] oldest→newest, recency-bounded.
    truth_bid = float(tape[-1][0])
    if not (math.isfinite(truth_bid) and truth_bid > 0):
        return False, dbg
    _spreads = [float(s) for (_b, s) in tape if math.isfinite(float(s)) and float(s) >= 0]
    _spreads.sort()
    _median_spread_bps: float | None = None
    if _spreads:
        _mid = len(_spreads) // 2
        _median_spread_bps = (
            _spreads[_mid]
            if len(_spreads) % 2 == 1
            else (_spreads[_mid - 1] + _spreads[_mid]) / 2.0
        )
    _mult = float(
        getattr(settings, "chili_momentum_max_loss_phantom_divergence_spread_mult", 3.0) or 3.0
    )
    if _median_spread_bps is not None and _median_spread_bps > 0:
        _tol_bps = _mult * _median_spread_bps
        _tol_basis = "recent_median_spread"
    else:
        _tol_bps = float(
            getattr(settings, "chili_momentum_max_loss_phantom_divergence_fallback_bps", 100.0)
            or 100.0
        )
        _tol_basis = "documented_fallback"
    # Divergence of the in-process bid BELOW the IQFeed truth bid, in bps of the truth bid.
    _divergence_bps = (truth_bid - ipb) / truth_bid * 10_000.0
    phantom = _divergence_bps > _tol_bps
    dbg = {
        "checked": True,
        "in_process_bid": round(ipb, 6),
        "iqfeed_truth_bid": round(truth_bid, 6),
        "divergence_bps": round(_divergence_bps, 2),
        "tolerance_bps": round(_tol_bps, 2),
        "tolerance_basis": _tol_basis,
        "median_spread_bps": (round(_median_spread_bps, 2) if _median_spread_bps is not None else None),
        "spread_mult": _mult,
        "samples": len(tape),
        "phantom": bool(phantom),
    }
    return bool(phantom), dbg


def bail_on_no_confirmation(
    *,
    entry_price: float,
    bid: float,
    high_water_mark: float | None,
    held_seconds: float,
    min_hold_seconds: float,
    window_seconds: float,
    buffer_bps: float,
    ofi: float | None = None,
) -> bool:
    """GAP1 — affirmative breakout-or-bailout (Warrior re-audit 2026-06-26). The deployed
    bailout is REACTIVE (price-retest-FAIL on the bid). This is the AFFIRMATIVE side: within
    [min_hold, window] seconds of the fill, BAIL only on GENUINE non-confirmation —

      * NO new high since entry above the confirmation buffer (high_water_mark <
        entry*(1+buffer)), i.e. the breakout never followed through; AND
      * the live bid is at/below the entry (near/below the fill, not extended up); AND
      * the tape is NOT accelerating up (ofi is None/unknown OR ofi <= 0). When live OFI
        shows buyers stepping IN (ofi > 0) the entry IS confirming → never bail.

    A winner that POPS then consolidates is IMMUNE: its high_water_mark prints above the
    buffer, so the first clause is False and this returns False. Pure / fail-closed (any
    bad input → False → no bail; the structural stop is unaffected)."""
    try:
        e = float(entry_price)
        b = float(bid)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(e) and math.isfinite(b)) or e <= 0 or b <= 0:
        return False
    try:
        held = float(held_seconds)
        win = float(window_seconds)
        mn = float(min_hold_seconds)
    except (TypeError, ValueError):
        return False
    if win <= 0 or held < mn or held > win:
        return False
    # CONFIRMATION = a new high above the buffer over entry. high_water_mark is the
    # peak bid since entry; absent it, treat the current bid as the best high seen
    # (fail-closed toward NOT bailing if it has extended).
    try:
        bps = max(0.0, float(buffer_bps))
    except (TypeError, ValueError):
        bps = 0.0
    confirm_level = e * (1.0 + bps / 10_000.0)
    hwm = None
    try:
        if high_water_mark is not None and math.isfinite(float(high_water_mark)):
            hwm = float(high_water_mark)
    except (TypeError, ValueError):
        hwm = None
    peak = max(hwm if hwm is not None else b, b)
    if peak >= confirm_level:
        return False  # a new high above the buffer = the breakout confirmed → immune
    # No follow-through high. Require the bid to be at/below entry (non-confirmation),
    # NOT merely consolidating a few bps up.
    if b > e:
        return False
    # Tape must not be accelerating up. Unknown OFI (None) does not block the bail
    # (the no-new-high + bid<=entry conditions already establish non-confirmation),
    # but a positive OFI (buyers stepping in) is live confirmation → do NOT bail.
    try:
        if ofi is not None and float(ofi) > 0.0:
            return False
    except (TypeError, ValueError):
        pass
    return True


def instant_bid_below_fill_cut(
    *,
    entry_price: float,
    bid: float,
    held_seconds: float,
    window_seconds: float,
    margin_bps: float,
) -> bool:
    """GAP2 — instant bid-below-fill cut (Warrior re-audit 2026-06-26). Right after the
    fill, if the live BID has dropped BELOW the fill price by MORE than spread noise (the
    move failed at the entry tick), cut FAST instead of waiting for the structural stop.

    The noise discriminator is ``margin_bps``: the bid must be < entry*(1 - margin/1e4),
    so a bid merely sitting at/just under the fill (the normal spread on a fresh long) does
    NOT trigger — only a genuine bid-collapse below the fill does. Tight window (first few
    seconds). Pure / fail-closed (bad input → False → no cut)."""
    try:
        e = float(entry_price)
        b = float(bid)
        held = float(held_seconds)
        win = float(window_seconds)
        mbps = max(0.0, float(margin_bps))
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(e) and math.isfinite(b)) or e <= 0 or b <= 0:
        return False
    if win <= 0 or held < 0 or held > win:
        return False
    threshold = e * (1.0 - mbps / 10_000.0)
    return b < threshold


def instant_bid_above_fill_confirm_failed(
    *,
    entry_price: float,
    bid: float,
    high_water_mark: float | None,
    held_seconds: float,
    window_seconds: float,
    margin_bps: float,
) -> bool:
    """LOCATE #7 — the POSITIVE MIRROR of ``instant_bid_below_fill_cut``. In the first
    ``window_seconds`` after the fill the live BID should hold AT/ABOVE the fill (within
    ``margin_bps`` noise) as positive confirmation. Returns ``True`` (confirmation FAILED ⇒
    feed the existing bail) ONLY when, AT THE END of the window (``held_seconds`` near the
    window edge), the bid is NOT at/above the fill AND the position never made a confirming
    high (the high-water mark never cleared the fill+margin). A position that DID print a
    confirming high is IMMUNE (the entry confirmed). This NEVER adds a new exit reason — the
    caller routes it into the SAME instant-bid-below / no-confirmation bailout it already
    gates. Pure / fail-CLOSED (any bad input ⇒ False ⇒ no cut)."""
    try:
        e = float(entry_price)
        b = float(bid)
        held = float(held_seconds)
        win = float(window_seconds)
        mbps = max(0.0, float(margin_bps))
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(e) and math.isfinite(b)) or e <= 0 or b <= 0:
        return False
    # Only evaluate at the END of the confirmation window (give the bid the full window to
    # confirm); before that, never cut. After the window, the structural stop owns it.
    if win <= 0 or held < win or held > win * 2.0:
        return False
    confirm_floor = e * (1.0 - mbps / 10_000.0)
    # If the position EVER made a confirming high (hwm cleared the fill+margin), it confirmed.
    try:
        if high_water_mark is not None and math.isfinite(float(high_water_mark)):
            if float(high_water_mark) >= e * (1.0 + mbps / 10_000.0):
                return False
    except (TypeError, ValueError):
        pass
    # No confirming high AND the bid is below the fill (minus noise) at the window edge ⇒
    # the entry never confirmed → feed the bail.
    return b < confirm_floor


def _regime_holdtime_band_mult(*, explosive: bool) -> float:
    """GAP3 (regime-conditioned hold-time, Warrior re-audit 2026-06-26): the give-back
    band multiplier for the runner cushion-trail, conditioned on the ENTRY regime via the
    deployed explosiveness classifier. HOT/explosive ⇒ hot_mult (>= 1.0, hold the runner
    through red LONGER); COLD/non-explosive ⇒ cold_mult (<= 1.0, cut chop QUICKER).
    Returns 1.0 (byte-identical, no scaling) unless the GAP3 flag is ON. The trail is
    ratchet-only, so a hot mult can only DECLINE to tighten the live stop — it never
    widens an existing stop (no risk added)."""
    try:
        if not bool(getattr(settings, "chili_momentum_regime_holdtime_enabled", False)):
            return 1.0
        if explosive:
            m = float(getattr(settings, "chili_momentum_regime_holdtime_hot_mult", 1.25) or 1.25)
            return max(1.0, m)
        m = float(getattr(settings, "chili_momentum_regime_holdtime_cold_mult", 0.85) or 0.85)
        return min(1.0, max(1e-6, m))
    except (TypeError, ValueError):
        return 1.0


def _held_position_keeps_exit_on_boundary_fail(state: str, has_position: Any) -> bool:
    """A held momentum position must keep its EXIT/stop management even when the
    entry-oriented boundary risk eval (viability freshness / caps / concurrency)
    refuses. The stop/target is a SAFETY mechanism that must always run — only the
    kill-switch force-exits. Returns True when the tick must fall through to the
    exit handler instead of blocking. (docs/DESIGN/MOMENTUM_LANE.md)"""
    return bool(has_position) and state in _HELD_LIVE_STATES


def _adaptive_quote_max_age_seconds(db: Any, symbol: str, base_max_age: float) -> float:
    """ADAPTIVE stale-quote window: scale the freshness ceiling to how fast THIS name
    actually trades, so "stale" means old RELATIVE to its own cadence — not a fixed clock.
    A name printing every ~20s is fresh at ~20-60s; a halted/quiet name (no recent ticks)
    stays at the conservative base floor (correctly stale). One documented knob
    (cadence_mult K) + safety bounds (floor = never tighter than the venue base; ceiling
    caps it). Reads the live IQFeed trade cadence (iqfeed_trade_ticks).
    (operator 2026-06-25: "gawin mong adaptive" — no fixed 15s magic clock.)"""
    base = float(base_max_age or 0.0)
    floor = max(base, float(getattr(settings, "chili_momentum_quote_freshness_floor_seconds", 15.0) or 15.0))
    ceil = float(getattr(settings, "chili_momentum_quote_freshness_ceiling_seconds", 120.0) or 120.0)
    k = float(getattr(settings, "chili_momentum_quote_freshness_cadence_mult", 3.0) or 3.0)
    # FIX B1 — extended-hours-aware CEILING. Pre/post-market names trade at a much slower
    # cadence; the regular-hours ceiling perpetually caps the cadence-scaled window so a
    # slow-but-LIVE ext-hours mover is flagged stale and the entry trigger never gets a turn
    # (stale_bbo peaks 16:00-19:00 ET). Raise ONLY the ceiling during extended hours; the
    # FLOOR (the conservative window for genuinely no-tick / halted names below) is untouched,
    # and the cadence-scaling math is unchanged — a name with no recent ticks still returns
    # the floor. OFF => byte-identical (regular ceiling always).
    if bool(getattr(settings, "chili_momentum_ext_hours_quote_age_enabled", True)):
        try:
            from .market_profile import market_session_now

            if market_session_now(symbol) in ("premarket", "afterhours"):
                _ext_ceil = float(
                    getattr(settings, "chili_momentum_ext_hours_quote_ceiling_seconds", 300.0) or 300.0
                )
                if _ext_ceil > ceil:
                    ceil = _ext_ceil
        except Exception:
            pass
    if ceil < floor:
        ceil = floor
    try:
        from sqlalchemy import text as _text
        window = 120.0
        n = db.execute(
            _text(
                "SELECT count(*) FROM iqfeed_trade_ticks "
                "WHERE symbol = :s AND observed_at > now() - make_interval(secs => :w)"
            ),
            {"s": symbol, "w": window},
        ).scalar() or 0
        if int(n) <= 0:
            return floor  # not actively trading -> conservative (halted/quiet stays stale)
        avg_interval = window / float(int(n))
        return float(max(floor, min(ceil, k * avg_interval)))
    except Exception:
        return floor


def _refetch_bbo_secondary(symbol: str) -> tuple[Any, str] | None:
    """FIX B2 — refetch a fresh BBO from the SECONDARY market-data chain (the documented
    priority Massive WS -> Polygon -> Massive REST snapshot, which is the in-process
    equivalent of the RH MCP ``get_equity_quotes`` equity leg) when the PRIMARY entry tick
    is STALE. Returns ``(NormalizedTicker, source_label)`` or ``None`` if no source gives a
    usable bid/ask. Each leg stamps a FRESH ``FreshnessMeta`` (the read just happened), so a
    valid result is fresh by construction; the caller re-runs the SAME validation on it —
    invalid books (ask<bid, non-positive) are still rejected. Cheap + fail-silent: any error
    in a leg falls through to the next, and a total miss returns ``None`` (the stale verdict
    stands)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None

    def _mk(bid: float | None, ask: float | None, last: float | None, source: str):
        try:
            b = float(bid) if bid else None
            a = float(ask) if ask else None
        except (TypeError, ValueError):
            b = a = None
        if not (b and a and b > 0 and a > 0):
            return None
        mid = (b + a) / 2.0
        spread_abs = a - b
        spread_bps = (spread_abs / mid) * 10_000.0 if mid > 0 else None
        return NormalizedTicker(
            product_id=sym, bid=b, ask=a, mid=mid,
            spread_abs=spread_abs, spread_bps=spread_bps,
            last_price=(float(last) if last else None),
            # replay v3: stamp freshness at the sim clock so the runner's stale-quote
            # checks compare sim-to-sim (prod byte-identical — _utcnow_aware() == now(utc)).
            freshness=FreshnessMeta(retrieved_at_utc=_utcnow_aware()),
            raw={"source": source},
        ), source

    # 1) Massive WebSocket (push-fresh NBBO when the feed is live in this process).
    try:
        from ...massive_client import get_ws_quote

        ws_q = get_ws_quote(sym)
        if ws_q is not None:
            r = _mk(getattr(ws_q, "bid", None), getattr(ws_q, "ask", None),
                    getattr(ws_q, "price", None), "massive_ws")
            if r is not None:
                return r
    except Exception:
        pass
    # 2) Polygon snapshot (carries lastQuote bid/ask).
    try:
        from ... import polygon_client as _poly

        pq = _poly.get_last_quote(sym) or {}
        r = _mk(pq.get("bid"), pq.get("ask"), pq.get("last_price"), "polygon")
        if r is not None:
            return r
    except Exception:
        pass
    # 3) Massive REST snapshot (the documented top-priority REST source; in-process stand-in
    #    for the RH MCP equity-quote leg, which is not exposed as a synchronous fn here).
    try:
        from ...massive_client import get_last_quote as _massive_last_quote

        mq = _massive_last_quote(sym) or {}
        r = _mk(mq.get("bid"), mq.get("ask"), mq.get("last_price"), "massive_rest")
        if r is not None:
            return r
    except Exception:
        pass
    return None


def _quote_quality_block(
    tick: Any, freshness: Any, max_spread_bps: float | None = None,
    *, symbol: str | None = None, db: Any = None,
) -> dict[str, Any] | None:
    meta = getattr(tick, "freshness", None) or freshness
    # FIX B2 telemetry: set when a secondary refetch replaced a stale primary tick; stamped
    # into whichever verdict (block or pass) the re-validated quote produces. Always bound
    # (no local-shadow UnboundLocalError class).
    _refetched_meta: dict[str, Any] | None = None
    if meta is not None and not is_fresh_enough(meta):
        # The venue's FIXED base window flagged stale — re-check against the name's
        # cadence-scaled window. PURELY ADDITIVE: only ever WIDENS (can un-block a
        # slow-but-live name trading at its normal pace), never newly-blocks. A halted
        # name (no recent ticks) keeps the conservative floor and stays stale.
        _age = float(meta.age_seconds())
        _max_age = float(getattr(meta, "max_age_seconds", 0.0) or 0.0)
        if symbol and db is not None:
            try:
                _max_age = _adaptive_quote_max_age_seconds(db, str(symbol), _max_age)
            except Exception:
                pass
        if _age > _max_age:
            # FIX B2 — the PRIMARY tick is genuinely stale. Before blocking, refetch the BBO
            # ONCE from the secondary market-data chain (Massive WS -> Polygon -> Massive REST)
            # and re-run the SAME validation on the fresh quote below. This ONLY changes WHICH
            # quote we validate, never an entry condition; invalid books are still rejected.
            # OFF => byte-identical (the original stale_bbo block stands).
            _refetched_meta: dict[str, Any] | None = None
            if (
                symbol
                and bool(getattr(settings, "chili_momentum_entry_quote_refetch_enabled", True))
            ):
                try:
                    _rf = _refetch_bbo_secondary(str(symbol))
                except Exception:
                    _rf = None
                if _rf is not None:
                    _rf_tick, _rf_source = _rf
                    _rf_meta = getattr(_rf_tick, "freshness", None)
                    # The refetched quote must itself be fresh (it was just read, so it is) —
                    # if for any reason it is NOT, fall through to the original stale block.
                    if _rf_meta is None or is_fresh_enough(_rf_meta):
                        _refetched_meta = {
                            "refetch_source": _rf_source,
                            "refetch_age_seconds": round(
                                float(_rf_meta.age_seconds()) if _rf_meta is not None else 0.0, 4
                            ),
                            "primary_age_seconds": round(_age, 4),
                            "primary_max_age_seconds": round(_max_age, 2),
                        }
                        tick = _rf_tick  # validate the FRESH quote from here down
            if _refetched_meta is None:
                # LOG-ONLY diagnostics (FIX A): classify WHY this is a stale_bbo so the
                # operator can tell freshness vs IQFeed-L1 COVERAGE. The source tells us
                # whether the freshest available quote came from the tick-level IQFeed L1
                # mirror (source='iqfeed_l1', ~1-2s) or a slower fallback (massive_ws / RH,
                # 10-270s). A stale block on a NON-iqfeed source = missing L1 coverage on
                # this name (the #1 killer's coverage face); a stale block ON iqfeed = a
                # real age-breach (truly no recent print -> suspected halt). NO behavior
                # change — the stale_bbo block stands exactly as before.
                if bool(getattr(settings, "chili_momentum_quote_block_diagnostics", True)):
                    try:
                        _src = None
                        _raw = getattr(tick, "raw", None)
                        if isinstance(_raw, dict):
                            _src = _raw.get("source")
                        _from_iqfeed = (_src == "iqfeed_l1")
                        _log.info(
                            "[momentum_live] quote_block_diag symbol=%s reason=stale_bbo "
                            "kind=%s source=%s age_seconds=%.2f max_age_seconds=%.2f "
                            "iqfeed_l1_covered=%s",
                            str(symbol),
                            "real_age_breach" if _from_iqfeed else "missing_iqfeed_l1_coverage",
                            _src,
                            _age,
                            _max_age,
                            _from_iqfeed,
                        )
                    except Exception:
                        pass
                return {
                    "reason": "stale_bbo",
                    "age_seconds": round(_age, 4),
                    "max_age_seconds": round(_max_age, 2),
                }
    try:
        mid = float(getattr(tick, "mid", 0.0) or 0.0)
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
    except (TypeError, ValueError):
        return {"reason": "invalid_bbo"}
    if mid <= 0 or bid <= 0 or ask <= 0 or ask < bid:
        _inv = {"reason": "invalid_bbo", "bid": bid, "ask": ask, "mid": mid}
        if _refetched_meta:
            _inv.update(_refetched_meta)
            _inv["failed_check"] = "invalid_bbo"
        return _inv
    try:
        spread_bps = float(getattr(tick, "spread_bps", None))
    except (TypeError, ValueError):
        spread_bps = ((ask - bid) / mid) * 10_000.0
    if not math.isfinite(spread_bps):
        spread_bps = ((ask - bid) / mid) * 10_000.0
    # max_spread_bps is the caller-supplied ADAPTIVE tolerance (volatility-relative);
    # fall back to the documented base floor when absent or invalid.
    raw_max_spread = max_spread_bps
    if raw_max_spread is None:
        raw_max_spread = getattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
    try:
        # A 0.0 cap is a deliberate "block all" and is preserved; only None / NaN /
        # inf / unparseable values fall back to the documented default.
        max_spread = 12.0 if raw_max_spread is None else float(raw_max_spread)
        if not math.isfinite(max_spread):
            max_spread = 12.0
    except (TypeError, ValueError):
        max_spread = 12.0
    if spread_bps > max_spread:
        _ws = {
            "reason": "wide_bbo_spread",
            "spread_bps": round(spread_bps, 4),
            "max_spread_bps": max_spread,
            "bid": bid,
            "ask": ask,
            "mid": mid,
        }
        if _refetched_meta:
            _ws.update(_refetched_meta)
            _ws["failed_check"] = "wide_bbo_spread"
        # LOG-ONLY diagnostics (FIX A): a wide-spread block is the THIRD face of the #1
        # killer (freshness / coverage / spread). Emit a structured line so the operator
        # can read how often the spread cap (now adaptive) is the binding constraint and
        # by how much the live spread exceeds it. NO behavior change.
        if bool(getattr(settings, "chili_momentum_quote_block_diagnostics", True)):
            try:
                _src = None
                _raw = getattr(tick, "raw", None)
                if isinstance(_raw, dict):
                    _src = _raw.get("source")
                _log.info(
                    "[momentum_live] quote_block_diag symbol=%s reason=wide_bbo_spread "
                    "source=%s spread_bps=%.2f max_spread_bps=%.2f over_by_bps=%.2f",
                    str(symbol),
                    _src,
                    spread_bps,
                    max_spread,
                    spread_bps - max_spread,
                )
            except Exception:
                pass
        return _ws
    # FIX B2: a secondary refetch RESCUED a stale primary tick (the fresh quote passed all
    # validation). Surface a no-block telemetry marker so the entry caller records that the
    # entry seam was unblocked by a refetch (A/B), then proceed exactly as a clean pass.
    if _refetched_meta:
        try:
            _log.info(
                "[momentum_live] entry_quote_refetch_rescue symbol=%s source=%s "
                "primary_age=%.2fs refetch_age=%.2fs",
                str(symbol),
                _refetched_meta.get("refetch_source"),
                float(_refetched_meta.get("primary_age_seconds") or 0.0),
                float(_refetched_meta.get("refetch_age_seconds") or 0.0),
            )
        except Exception:
            pass
    return None


def _order_done_for_entry(no: NormalizedOrder) -> bool:
    st = (no.status or "").lower()
    if st in ("filled", "done", "closed"):
        return True
    if no.filled_size > 1e-12:
        return st in ("cancelled", "canceled", "expired", "failed")
    return False


# Terminal order statuses (done — never "still working"). Anything NOT in here and
# not empty is a live/resting order. The old allow-list of OPEN statuses missed
# Robinhood's "working"/"confirmed"/"queued"/"unconfirmed"/"partially_filled" — a
# placed-but-unfilled equity order then fell through ``_order_open`` to the
# ``entry_order_state`` live_error branch and was ORPHANED on the broker (the lane's
# first real RH equity order, HIHO 2026-06-09, did exactly this). Allow-listing
# "done" states instead means any current/future broker open status is handled by
# the ack-timeout (cancel + re-watch) path rather than erroring. docs/DESIGN/MOMENTUM_LANE.md
_ORDER_TERMINAL_STATUSES = frozenset(
    {"filled", "done", "closed", "cancelled", "canceled", "expired", "failed", "rejected", "voided"}
)


def _order_open(no: NormalizedOrder) -> bool:
    """True while an order is still live on the venue (resting / unfilled), so the
    runner waits or ack-timeout-cancels it instead of erroring + orphaning it. Empty
    / "unknown" status is treated as open (indeterminate -> never abandon)."""
    st = (no.status or "").lower()
    if st in ("", "unknown"):
        return True
    return st not in _ORDER_TERMINAL_STATUSES


# ── Entry-order HISTORY: no fill is ever untracked, no stacking ───────────────
# Every placed entry order id is kept in le["entry_order_ids_all"] (the ack-timeout
# may wipe the ACTIVE pointer, never the history) with a per-id resolution map in
# le["entry_orders_resolved"] ({oid: "adopted"|"void"}). Two invariants follow:
#   1. LATE-FILL SWEEP — every pre-entry tick re-checks unresolved ids; an order
#      that filled AFTER the ack-timeout abandoned it (venue cancels are async — the
#      cancel can lose the race by SECONDS, far past #567's immediate re-fetch) is
#      re-pointed + adopted instead of becoming an unmanaged orphan.
#   2. PRE-SUBMIT GUARD — while ANY id is unresolved the runner may NOT place a new
#      entry order, making position-stacking structurally impossible.
# [BATL 2026-06-10: 5 ack-timeout cancels all lost the race -> 5 untracked fills
# stacked 4,954 sh / ~$8k with no lane stop; operator had to manage it by hand.]
_ENTRY_ORDER_HISTORY_MAX = 20  # bound the json; resolution keeps the live set ~1


def _unresolved_entry_order_ids(le: dict) -> list[str]:
    """Placed entry order ids with no terminal resolution yet, EXCLUDING the active
    pointer (the normal pending-entry handler owns that one)."""
    hist = le.get("entry_order_ids_all") or []
    resolved = le.get("entry_orders_resolved") or {}
    active = str(le.get("entry_order_id") or "")
    return [str(o) for o in hist if str(o) not in resolved and str(o) != active]


def _record_entry_order_placed(le: dict, order_id) -> None:
    if not order_id:
        return
    hist = [str(o) for o in (le.get("entry_order_ids_all") or [])]
    if str(order_id) not in hist:
        hist.append(str(order_id))
    le["entry_order_ids_all"] = hist[-_ENTRY_ORDER_HISTORY_MAX:]


def _mark_entry_order_resolved(le: dict, order_id, outcome: str) -> None:
    res = dict(le.get("entry_orders_resolved") or {})
    res[str(order_id)] = outcome
    le["entry_orders_resolved"] = res


# ── RECYCLE entry/position lifecycle reset (2026-06-27 duplicate-fill root cause) ──
# At COOLDOWN -> WATCHING_LIVE the session RECYCLES into a fresh watcher. Today it keeps
# the PRIOR trade's entry-order / position state in `le`, so the recycled watcher's first
# WATCHING tick re-runs the late-fill sweep (`_unresolved_entry_order_ids` /
# `_sweep_unresolved_entry_orders`) and the pre-submit poll against ITS OWN already-filled
# entry order -> re-points + adopts a SECOND phantom position (AREC sid 9331: two entry
# rows same broker_order_id, 221sh x2 -> stuck live_bailout spin).
#
# `_RECYCLE_ENTRY_STATE_KEYS` is the COMPLETE set of per-trade lifecycle keys cleared on
# recycle so the next cycle starts as a clean watcher with NO entry order to re-poll. It
# COVERS every key the entry-poll / adoption path reads to decide "do I already have an
# entry order to adopt?" — the load-bearing five: entry_order_id, entry_order_ids_all,
# entry_orders_resolved, entry_submitted, position (read at lines ~2279/4186/4220/4366 +
# _unresolved_entry_order_ids). The rest are entry-sizing, pending-entry, repeg, scale-out,
# pyramid/anticipation/micropullback add-leg, exit-order, stop/breach, max-loss-circuit and
# halt-entry markers that all belong to the trade that just closed.
#
# NOT cleared (must persist across cycles): the symbol/variant identity (sess columns, not
# le), cooldown bookkeeping (handled inline), trade_cycles, the cumulative daily-loss
# accounting (realized_pnl_usd / fees_usd_total), the session-discipline counters
# (per_symbol_fatigue / win_cycle_fatigue / *_fatigue_derate / green_day_graduation /
# prior_day_pnl_damper / *_size* selection tilts), the symbol-level halt-resume CHAIN counters
# (halt_chain_up_count / halt_down_consecutive_count — they track the SYMBOL across cycles), the
# sizing audit metadata that is recomputed fresh every entry pass (streak_risk / cushion_risk /
# schedule_risk / liquidity_risk / *_size* / *_cap, never read back as cross-cycle state), the
# deferred post-exit-excursion learning marker (post_exit_excursion_pending — a separate horizon
# job consumes it AFTER the exit), the per-tick telemetry (tick_count / last_mid / last_tick_utc /
# last_quote_quality_gate), and the last_exit_* / last_partial_exit_* summary fields (read by
# summarize_live_execution + the recycle event). See _record_fill_outcome_safe +
# tests/test_momentum_recycle_no_phantom.py.
_RECYCLE_ENTRY_STATE_KEYS: tuple[str, ...] = (
    # ── load-bearing: the entry-order / position adoption gate ──
    "entry_order_id",
    "entry_order_ids_all",
    "entry_orders_resolved",
    "entry_submitted",
    "position",
    # ── entry submit / sizing / pricing context ──
    "entry_submit_utc",
    "entry_client_order_id",
    "entry_limit_price",
    "entry_original_limit_px",
    "entry_order_type",
    "entry_place_count",
    "entry_place_result",
    "entry_repeg_count",
    "entry_chunk_order_ids",
    "entry_decision_packet_id",
    "entry_features",
    "entry_l2_snapshot",
    "entry_regime_snapshot_json",
    "entry_sizing",
    "entry_resize_basis",
    "entry_want_qty",
    "entry_stop_atr_pct",
    "entry_stop_model",
    "entry_dollar_vol",
    "entry_expected_move_bps",
    "entry_squeeze_pct",
    "entry_rvol",
    "entry_above_vwap",
    "entry_vertical_confluence",
    "entry_realized_high",
    "entry_day_range_pct",
    "entry_notional_guard",
    "entry_slip_bps_ref",
    "entry_spread_bps_at_decision",
    "entry_trigger_reason",
    "entry_trigger_debug",
    "entry_liq_mult",
    "entry_session_extended",
    "entry_session_overnight",
    "entry_fee_usd_unbooked",
    "admission_viability_score",
    "breakout_level_price",
    "watch_break_level",
    # ── pending-exit / exit-order lifecycle ──
    "exit_order_id",
    "exit_client_order_id",
    "exit_order_type",
    "exit_limit_price",
    "exit_place_result",
    "exit_execution_intents",
    "exit_floor_anchored",
    "exit_floor_order",
    "exit_next_retry_at_utc",
    "exit_pending_first_seen_utc",
    "exit_submit_attempts",
    "exit_session_extended",
    "exit_candle1m_min",
    "exit_candle1m_exh",
    "pending_exit_reason",
    "pending_exit_quantity",
    "pending_exit_submitted_at_utc",
    "pending_exit_is_scale_out",
    "last_exit_pending_confirmation",
    "broker_zero_confirm_streak",
    # ── scale-out / runner ladder ──
    "scale_limit_order_id",
    "scale_limit_px",
    "scale_limit_qty",
    "scale_limit_adopted_qty",
    "scale_limit_source",
    "ladder_cooldown_until_utc",
    "measured_move_partial_armed",
    "exhaustion_lock_partial_armed",
    # ── pyramid add legs ──
    "pyramid_order_id",
    "pyramid_limit_px",
    "pyramid_pending_R0",
    "pyramid_add_count",
    "pyramid_place_count",
    "pyramid_submit_retry_count",
    "pyramid_prev_stop",
    "pyramid_entry_stop_ref",
    "pyramid_risk_anchor_usd",
    "pyramid_confirm_ofi",
    "pyramid_discrete_trigger",
    # ── anticipation probe / remainder ──
    "anticipation_add_order_id",
    "anticipation_add_limit_px",
    "anticipation_armed",
    "anticipation_completed",
    "anticipation_full_qty",
    "anticipation_probe_qty",
    "anticipation_probe_fraction",
    "anticipation_remainder_qty",
    "anticipation_place_count",
    # ── micropullback re-entry add leg ──
    "micropullback_reentry_order_id",
    "micropullback_reentry_limit_px",
    "micropullback_reentry_count",
    "micropullback_reentry_cooldown_until_utc",
    "micropullback_reentry_pending_R0",
    "micropullback_confirm_ofi",
    "micropullback_confirm_trade_flow",
    "micropullback_last_shelf",
    "micropullback_pending_dip_low",
    "micropullback_prev_stop",
    # ── Ross buy-the-dip / pullback add leg ──
    "pullback_add_order_id",
    "pullback_add_limit_px",
    "pullback_add_count",
    "pullback_add_cooldown_until_utc",
    "pullback_add_pending_R0",
    "pullback_add_pending_low",
    "pullback_add_last_low",
    "pullback_add_prev_stop",
    "pullback_add_confirm_strength",
    "pullback_add_confirm_ofi",
    # ── Ross add-on-flag-breakout leg ──
    "flag_breakout_add_order_id",
    "flag_breakout_add_limit_px",
    "flag_breakout_add_count",
    "flag_breakout_add_cooldown_until_utc",
    "flag_breakout_add_pending_R0",
    "flag_breakout_add_pending_high",
    "flag_breakout_add_last_high",
    "flag_breakout_add_prev_stop",
    "flag_breakout_add_confirm_strength",
    "flag_breakout_add_confirm_ofi",
    # ── stop / breach / max-loss circuit / excursion markers ──
    "structural_stop_price",
    "structural_stop_atr_pct",
    "stop_breach_pending_utc",
    "stop_breach_chop_holds",
    "max_loss_circuit_fired",
    "max_loss_circuit_floor_price",
    "prev_signed_tape_accel",
    "last_bailout_trigger",
    "benched_backside_hod",
    # ── halt-entry markers tied to the closed position (NOT the symbol-level halt
    #    CHAIN counters halt_chain_up_count / halt_down_consecutive_count, which track
    #    the SYMBOL's resume sequence across watcher cycles and are kept) ──
    "halt_entry_size_mult",
    "halt_resumption_open",
    "halt_resumed_at_utc",
    "halt_down_pre_ref_bid",
    "halt_down_last_resume_utc",
    "suspected_halt_since_utc",
    "halt_stale_streak",
    "halt_level",
    # ── EMA-trail / cadence carry computed against the closed position ──
    "ema5m_min",
    "ema5m_val",
    "cadence_prev_ema5m",
    "cadence_prev_rvol",
    "cadence_cls",
)
# DELIBERATELY KEPT (NOT in the reset set): post_exit_excursion_pending. It is the deferred
# shake-out-learning marker for the trade that JUST closed; a separate horizon job
# (post_exit_excursion.run_post_exit_excursion_pass, ~30min horizon) labels it AFTER the exit
# and the cooldown (typically seconds–minutes) is far shorter than that horizon. Clearing it on
# recycle would DROP that learning signal before the horizon elapses — a regression. The exit
# path OVERWRITES (not appends) this marker on the next real exit, and the labeler already skips
# sessions that re-entered a holding state, so carrying it forward cannot mis-label the new trade.


def _reset_entry_state_on_recycle(le: dict) -> list[str]:
    """Pop every per-trade entry/position lifecycle key so the recycled watcher starts
    CLEAN (no entry order for the late-fill sweep / pre-submit poll to re-adopt). Returns
    the keys that were actually present (for the live_recycled audit). Identity, cooldown
    bookkeeping, trade_cycles, cumulative PnL/fees and discipline counters are preserved
    (they are not in _RECYCLE_ENTRY_STATE_KEYS)."""
    cleared: list[str] = []
    for k in _RECYCLE_ENTRY_STATE_KEYS:
        if k in le:
            le.pop(k, None)
            cleared.append(k)
    return cleared


def _sweep_unresolved_entry_orders(adapter, db, sess, le: dict) -> bool:
    """Resolve abandoned entry orders against venue truth. Returns True when a LATE
    FILL was found and the session was re-pointed at it (state -> PENDING_ENTRY so
    the existing fill-handler adopts it with the normal stop/target on the next
    pass). Cancelled-with-zero-fill ids are marked void (unblocks the submit guard);
    still-open / indeterminate ids stay unresolved (the guard keeps blocking new
    submits — fail-safe: rather not trade than buy a second clip)."""
    for oid in _unresolved_entry_order_ids(le):
        try:
            no, _ = adapter.get_order(str(oid))
        except Exception:
            continue  # indeterminate -> stays unresolved (guard keeps holding)
        if no is None:
            continue
        # OPEN-WITH-FILLS (2026-06-11 INDP): RH can leave an order in state
        # "open" with shares already filled — a cancel that silently failed
        # plus a later fill. We OWN those shares; waiting for a terminal state
        # that never comes left 612sh unmanaged at a generic -29% bracket.
        # Best-effort cancel the open remainder (single clip), then adopt.
        if _order_open(no) and float(no.filled_size or 0.0) > 0.0:
            try:
                adapter.cancel_order(str(oid))
            except Exception:
                logger.debug("[momentum_live] open-with-fills remainder cancel failed", exc_info=True)
        if _order_done_for_entry(no) or float(no.filled_size or 0.0) > 0.0:
            # LATE FILL — re-point the session at the real order and let the
            # hardened pending-entry fill-handler adopt it (position + stop/target).
            le["entry_order_id"] = str(oid)
            le["entry_submitted"] = True
            _mark_entry_order_resolved(le, oid, "adopted")
            _commit_le(sess, le)
            _emit(db, sess, "entry_late_fill_repointed", {
                "order_id": str(oid),
                "venue_status": no.status,
                "filled_size": float(no.filled_size or 0.0),
            })
            # Walk the LEGAL FSM chain to pending-entry (watching -> candidate ->
            # pending; the FSM has no watching -> pending shortcut).
            if sess.state == STATE_WATCHING_LIVE:
                _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
            if sess.state == STATE_LIVE_ENTRY_CANDIDATE:
                _safe_transition(db, sess, STATE_LIVE_PENDING_ENTRY)
            db.flush()
            return True
        if not _order_open(no):
            # terminal with zero fill (cancelled/expired/rejected clean) — safe to
            # forget; unblocks the pre-submit guard.
            _mark_entry_order_resolved(le, oid, "void")
            _commit_le(sess, le)
    return False


# ── Halt awareness (LULD circuit breakers on Ross low-floats) ─────────────────
# A halt is observable as a SUSTAINED quote freeze: `chili_momentum_halt_stale_ticks`
# consecutive stale_bbo ticks mark a suspected halt; when quotes return, entries are
# blocked for `chili_momentum_halt_resume_cooldown_seconds` (the post-resume whipsaw
# window) while watching continues. A session HOLDING a position into a halt raises a
# loud `position_halted` event (the software stop cannot execute until resume).
# [KMRK 2026-06-10: resumed through $6.81→$3.01→$5.13→$4.35→$3.33; the lane bought the
# middle of the whipsaw and the exit had to rest through the next halt.]


def _venue_broker_connected(ef: str) -> bool:
    """Cheap per-venue connectivity probe used as a tick preflight. robinhood:
    in-memory session flag (+1 local DB read at most when cold — the underlying
    is_connected may attempt one cooldown-capped re-auth, still far cheaper than a
    per-tick quote call hanging to network timeout on a dead venue). coinbase:
    TTL-cached account ping. Other adapters (alpaca: REST-per-call, no session
    concept) and any probe error return True (fail-open)."""
    try:
        if ef == "robinhood_spot":
            from ...broker_service import is_connected as _rh_connected

            return bool(_rh_connected())
        if ef == EXECUTION_FAMILY_COINBASE_SPOT:
            from ...coinbase_service import is_connected as _cb_connected

            return bool(_cb_connected())
    except Exception:
        return True
    return True


def _halt_stale_ticks_threshold() -> int:
    try:
        return max(2, int(getattr(settings, "chili_momentum_halt_stale_ticks", 3) or 3))
    except (TypeError, ValueError):
        return 3


def _halt_resume_cooldown_seconds() -> float:
    try:
        return max(0.0, float(getattr(settings, "chili_momentum_halt_resume_cooldown_seconds", 120.0) or 120.0))
    except (TypeError, ValueError):
        return 120.0


def _preserve_resume_dip_reason(
    *, trigger_ok: bool, pullback_reason: str, resume_dip_reject: str | None,
) -> str:
    """R7 (WAVE-4 ITEM-2a) — resolve the primary trigger reason after the pullback re-run.

    When a suspected halt just resumed, ``halt_resume_dip_trigger`` owns the tape (Ross's
    sanctioned post-resume entry). If it REJECTS, the ladder UNCONDITIONALLY re-runs
    ``momentum_pullback_trigger``, which clobbers ``_trigger_reason``/``_pb_debug`` — so the
    resume-dip's actionable "why the resumption dip did not fire" was silently lost.

    Rule: keep the pullback's reason ONLY when it produced a DIFFERENT actionable result —
    a FIRE (``trigger_ok``) or a TICK-ARMED wait the event loop can act on. Otherwise (the
    pullback merely produced another inert wait) restore the resume-dip reject as the
    primary reason — that window belonged to resume-dip. Pure / no side effects.

    ``resume_dip_reject is None`` ⇒ the resume-dip path did not run (no resumed halt) ⇒
    return the pullback reason unchanged (byte-identical to the pre-R7 behavior)."""
    if resume_dip_reject is None:
        return pullback_reason
    if trigger_ok:
        return pullback_reason  # a fire is a different, actionable result — keep it.
    try:
        from .entry_gates import TICK_ARMED_WAIT_REASONS as _TAWR
    except Exception:
        _TAWR = frozenset()
    if pullback_reason in _TAWR:
        return pullback_reason  # a tick-armed wait the event loop acts on — keep it.
    return resume_dip_reject  # another inert wait — restore the resume-dip reject.


def _mark_suspected_halt(db, sess, le: dict, tick: Any = None, *, detail: dict | None = None) -> None:
    """Set the suspected-halt marker + all the downstream flags/telemetry ONCE at halt
    onset. Shared by BOTH inference paths — the quote-staleness streak
    (``_register_stale_quote_tick``) AND the R6 print-recency inference
    (``_register_print_recency_halt_check``) — so a halt detected either way lights the
    exact same lifecycle (suspected_halt_since_utc, GAP 2/3 halt_level capture, the
    suspected_halt_detected + position_halted events). Idempotent: a no-op if a halt is
    already in force (``suspected_halt_since_utc`` set)."""
    if le.get("suspected_halt_since_utc"):
        return
    le["suspected_halt_since_utc"] = _utcnow().isoformat()
    # MED-3 fail-SAFE: clear any PRIOR resume markers at the onset of a NEW halt so a
    # RE-HALT does not read stale resume data (halt_resumed_at_utc / halt_resumption_open
    # are stamped on resume but were never cleared at a fresh halt onset). The resume-stamp
    # logic is unchanged; this only prevents the re-halt window from inheriting the last
    # resume. Use pop (not set-None) so a FIRST halt with no prior resume leaves the keys
    # ABSENT — byte-identical when no resume has occurred yet.
    le.pop("halt_resumed_at_utc", None)
    le.pop("halt_resumption_open", None)
    # GAP 2/3 — capture the halt_level (last good price) once, at halt onset. Only
    # when a resume-direction / false-halt flag is ON and a tick is available; the
    # bid is the last good top-of-book before the book went dark (mid as fallback).
    try:
        if (
            tick is not None
            and (
                bool(getattr(settings, "chili_momentum_halt_resumption_direction_enabled", False))
                or bool(getattr(settings, "chili_momentum_false_halt_avoid_enabled", False))
                # The add-into-halt master needs halt_level for its H2/H3 leg (now
                # self-sufficient under the master). Master OFF + sub-flags OFF ⇒ no
                # halt_level is written ⇒ byte-identical.
                or bool(getattr(settings, "chili_momentum_add_into_halt_enabled", False))
            )
        ):
            _hl = None
            try:
                _hl = float(getattr(tick, "bid", None) or getattr(tick, "mid", None) or 0) or None
            except (TypeError, ValueError):
                _hl = None
            if _hl is not None and _hl > 0:
                le["halt_level"] = _hl
    except Exception:
        pass
    _emit(db, sess, "suspected_halt_detected", dict(detail or {}))
    pos = le.get("position")
    if isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0:
        _log.warning(
            "[momentum_live] POSITION HALTED symbol=%s session=%s qty=%s — software stop "
            "cannot execute until the halt resumes; exit will price at the resume open.",
            sess.symbol, sess.id, pos.get("quantity"),
        )
        _emit(db, sess, "position_halted", {
            "quantity": pos.get("quantity"),
            "avg_entry_price": pos.get("avg_entry_price"),
            "stop_price": pos.get("stop_price"),
        })


def _register_stale_quote_tick(db, sess, le: dict, tick: Any = None) -> None:
    """Count a consecutive stale-quote tick; at the threshold mark a suspected halt
    (and alert loudly if a real position is held into it).

    GAP 2/3 (Warrior re-audit): when ``tick`` is supplied AND either the resumption-
    direction or false-halt flag is ON, also capture ``halt_level`` (the LAST GOOD price
    at the moment the halt is detected) so the resume-direction read can compare the
    resumption open against it. The capture is the only behaviour change and is fully
    flag-gated (both flags OFF ⇒ no halt_level is written ⇒ byte-identical)."""
    streak = int(le.get("halt_stale_streak") or 0) + 1
    le["halt_stale_streak"] = streak
    if streak == _halt_stale_ticks_threshold() and not le.get("suspected_halt_since_utc"):
        _mark_suspected_halt(db, sess, le, tick, detail={"stale_tick_streak": streak})
    # TIER-2 OVERNIGHT PRICE-BUS-DARK KILL: a position held OVERNIGHT into a stale book is
    # the dangerous case — the software stop can only act on the NEXT fresh tick, so a dark
    # bus means the held position is unprotected. When overnight trading is on AND a position
    # is held overnight AND the quote has been stale beyond chili_momentum_overnight_max_stale_sec
    # (0 => derive from the halt-stale threshold), emit a CRITICAL + set a flatten-intent flag
    # the next-fresh-tick exit path honors; the runner already refuses to act on a stale book,
    # so this never submits into the dark — it flattens on resume. New arming is gated off by
    # is_overnight_now's safety-aware tradeability tier. Flag OFF / not overnight => no-op.
    try:
        if getattr(settings, "chili_momentum_overnight_trading_enabled", False):
            from .market_profile import is_overnight_now as _is_overnight_now

            pos = le.get("position")
            if (
                isinstance(pos, dict)
                and float(pos.get("quantity") or 0) > 0
                and _is_overnight_now(sess.symbol)
            ):
                _ovn_stale = float(getattr(settings, "chili_momentum_overnight_max_stale_sec", 0.0) or 0.0)
                _ticks_thresh = _halt_stale_ticks_threshold()
                # 0 => derive: the halt-stale tick threshold is the natural overnight bound.
                _stale_streak = int(le.get("halt_stale_streak") or 0)
                _trip = (
                    _stale_streak >= _ticks_thresh
                    if _ovn_stale <= 0
                    else _stale_streak >= max(1, _ticks_thresh)
                )
                if _trip and not le.get("overnight_pricebus_dark_flagged"):
                    le["overnight_pricebus_dark_flagged"] = True
                    le["overnight_flatten_on_fresh"] = True
                    _commit_le(sess, le)
                    _log.critical(
                        "[momentum_live] OVERNIGHT PRICE-BUS DARK symbol=%s session=%s qty=%s "
                        "stale_streak=%s — no broker stop overnight; will flatten at the next "
                        "fresh tick + arm nothing new. If the bus is reliably dark overnight, "
                        "disable overnight trading.",
                        sess.symbol, sess.id, pos.get("quantity"), _stale_streak,
                    )
                    _emit(db, sess, "overnight_pricebus_dark", {
                        "quantity": pos.get("quantity"),
                        "stale_streak": _stale_streak,
                    })
                # FIX A part (2) — PROACTIVE FLATTEN AT FIRST STALE ONSET (2026-06-25).
                # The on-fresh flatten (overnight_flatten_on_fresh, honored in
                # _register_fresh_quote_tick) is NOT sufficient on its own: a FULLY DARK
                # bus delivers NO fresh tick, so the position would ride the dark naked
                # (no software stop fires — the loss circuit/stop are quote-dependent —
                # and RH has no overnight stop order). CONSERVATIVE: at the FIRST onset of
                # staleness (default onset_ticks=1 = this stale tick, while we still have
                # the last good book) request a flatten through the operator-flatten
                # chokepoint so an exit order is submitted/resting at the broker rather
                # than leaving the position unprotected. The flatten still routes the
                # normal exit/cancel path (cancel scale-out -> broker-qty clamp -> place ->
                # confirm -> reconcile) — no oversell, no orphan. Kill-switch flag OFF =>
                # this entire block is a no-op (legacy: overnight_flatten_on_fresh set but
                # never read; the documented naked-overnight risk).
                if (
                    getattr(settings, "chili_momentum_overnight_dark_flatten_enabled", False)
                    and not le.get("overnight_dark_flatten_requested")
                    and not le.get("operator_flatten_requested_utc")
                ):
                    try:
                        _onset = max(1, int(getattr(settings, "chili_momentum_overnight_dark_flatten_onset_ticks", 1) or 1))
                    except (TypeError, ValueError):
                        _onset = 1
                    if _stale_streak >= _onset:
                        le["overnight_dark_flatten_requested"] = _utcnow().isoformat()
                        _commit_le(sess, le)
                        _log.critical(
                            "[momentum_live] OVERNIGHT DARK-FLATTEN (proactive) symbol=%s "
                            "session=%s qty=%s stale_streak=%s onset_ticks=%s — flattening at the "
                            "last good tick (dark bus delivers no fresh tick; naked overnight is "
                            "not allowed). Routes the operator-flatten chokepoint.",
                            sess.symbol, sess.id, pos.get("quantity"), _stale_streak, _onset,
                        )
                        _emit(db, sess, "overnight_dark_flatten_requested", {
                            "quantity": pos.get("quantity"),
                            "stale_streak": _stale_streak,
                            "onset_ticks": _onset,
                            "trigger": "proactive_first_onset",
                        })
    except Exception:
        pass


def _print_recency_halt_states() -> frozenset:
    """The lane states in which a print-recency halt inference is meaningful — a name
    we are actively WATCHING for entry or HOLDING. (An armed-but-pre-watch or terminal
    session has no live tape stake, so we never infer a halt there.)"""
    return frozenset({
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
        STATE_LIVE_PENDING_ENTRY,
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
    })


def _register_print_recency_halt_check(db, sess, le: dict, tick: Any = None) -> None:
    """R6 (WAVE-4 ITEM-1) — INDEPENDENT print-recency halt inference.

    The quote-freshness halt path (``_register_stale_quote_tick``) starved since 2026-06-26:
    a secondary BBO refetch stamps FRESH meta on cached quotes, so ``stale_bbo`` never
    returns → ``suspected_halt`` went 602/day → 0 and ``halt_resume_dip`` has NEVER fired.
    But a real LULD halt STOPS THE TRADE PRINTS even when the (cached) quote meta looks
    fresh. This second, quote-independent path infers a halt from the TAPE going silent:
    for a WATCHED/HELD name that was RECENTLY ACTIVE, if the equity trade tape
    (``iqfeed_trade_ticks``) shows no prints for an ADAPTIVE window (a multiple of the
    name's recent median inter-print gap; floor ~30s) while the market is open, we mark a
    suspected halt via ``_mark_suspected_halt`` — lighting the EXACT same downstream
    lifecycle the quote path does (so ``halt_resume_dip`` can finally fire on the return).

    FAIL-CLOSED by design (never false-halt a quiet name):
      * no tape data ⇒ no inference (``print_recency_state`` returns None);
      * not recently active (< min prints in the recent-active window) ⇒ no inference;
      * market not open (data session) ⇒ no inference;
      * a halt already in force ⇒ no-op (``_mark_suspected_halt`` is idempotent).

    Flag ``chili_momentum_halt_print_recency_enabled`` (default True). OFF ⇒ this entire
    function is a no-op ⇒ byte-identical to the quote-only path."""
    try:
        if not bool(getattr(settings, "chili_momentum_halt_print_recency_enabled", True)):
            return
        # Only a name we are actively watching/holding, and only on the equity tape
        # (crypto has no iqfeed_trade_ticks — print_recency_state returns None there).
        if getattr(sess, "state", None) not in _print_recency_halt_states():
            return
        if le.get("suspected_halt_since_utc"):
            return  # a halt is already in force (either path) — nothing to add.
        sym = (getattr(sess, "symbol", None) or "").strip().upper()
        if not sym or sym.endswith("-USD"):
            return
        # Market must be OPEN (a data session) — a silent tape overnight/closed is normal,
        # not a halt. Fail-closed: any error here ⇒ no inference.
        try:
            from .market_profile import is_data_session_now as _is_data_session_now

            if not _is_data_session_now(sym):
                return
        except Exception:
            return

        try:
            _gap_mult = max(1.0, float(getattr(settings, "chili_momentum_halt_print_gap_multiple", 8.0) or 8.0))
        except (TypeError, ValueError):
            _gap_mult = 8.0
        try:
            _gap_floor = max(1.0, float(getattr(settings, "chili_momentum_halt_print_gap_floor_seconds", 30.0) or 30.0))
        except (TypeError, ValueError):
            _gap_floor = 30.0
        try:
            _active_window = max(1.0, float(getattr(settings, "chili_momentum_halt_print_recent_active_seconds", 300.0) or 300.0))
        except (TypeError, ValueError):
            _active_window = 300.0
        try:
            _active_min = max(1, int(getattr(settings, "chili_momentum_halt_print_recent_active_min_prints", 5) or 5))
        except (TypeError, ValueError):
            _active_min = 5

        from .nbbo_tape import print_recency_state

        st = print_recency_state(
            db,
            sym,
            recent_active_window_s=_active_window,
            gap_sample_window_s=max(_active_window, _gap_floor * _gap_mult),
            now_utc=_utcnow(),
        )
        if not st:
            return  # no tape data ⇒ fail-closed (no inference).
        last_age = st.get("last_print_age_s")
        recent_n = int(st.get("recent_print_count") or 0)
        median_gap = st.get("median_gap_s")
        if last_age is None:
            return
        # FAIL-CLOSED activity requirement: the name must have been RECENTLY ACTIVE (enough
        # prints in the lookback) — a quiet never-active name is never inferred as halted.
        if recent_n < _active_min:
            return
        # ADAPTIVE no-print window: a multiple of the recent median gap, floored. A name
        # that normally prints every 2s halts far faster than one printing every 20s.
        if median_gap is not None and median_gap > 0:
            _window = max(_gap_floor, float(median_gap) * _gap_mult)
        else:
            _window = _gap_floor
        if float(last_age) < _window:
            return  # tape is still printing within the adaptive window — not halted.
        _mark_suspected_halt(
            db, sess, le, tick,
            detail={
                "source": "print_recency",
                "last_print_age_s": round(float(last_age), 2),
                "no_print_window_s": round(_window, 2),
                "median_gap_s": (round(float(median_gap), 3) if median_gap is not None else None),
                "recent_print_count": recent_n,
            },
        )
        _log.warning(
            "[momentum_live] SUSPECTED HALT (print-recency) symbol=%s session=%s "
            "last_print_age=%.1fs window=%.1fs median_gap=%s recent_prints=%s — tape went "
            "silent while quotes look fresh; lighting the halt lifecycle so resume-dip can fire.",
            sym, sess.id, float(last_age), _window,
            (round(float(median_gap), 2) if median_gap is not None else None), recent_n,
        )
    except Exception:
        # Never let the halt inference break the tick loop — fail-closed (no inference).
        _log.debug("[momentum_live] print-recency halt check skipped", exc_info=True)


def _entry_pricebook_snapshot(symbol: str) -> dict | None:
    """One-shot Nasdaq TotalView depth snapshot (RH pricebook — the same book
    Legend's bid/ask windows render) at the entry decision. Returns a compact
    dict: top-of-book sizes + 5-level depth totals + signed imbalance in
    [-1, 1] (the convention viability's Phase 4a rules score). Fail-open:
    crypto / no Gold entitlement / endpoint error -> None.

    Depth caveat (research 2026-06-11): the pricebook is Nasdaq-venue-only
    depth, not the consolidated book — partial for names routed elsewhere.
    """
    sym = (symbol or "").strip().upper()
    if not sym or sym.endswith("-USD"):
        return None
    try:
        import robin_stocks.robinhood as rh

        pb = rh.stocks.get_pricebook_by_symbol(sym)
        if not isinstance(pb, dict):
            return None
        bids = pb.get("bids") or []
        asks = pb.get("asks") or []
        if not bids and not asks:
            return None

        def _lvl(side: list, n: int = 5) -> tuple[float, float]:
            tot = 0.0
            top = 0.0
            for i, lv in enumerate(side[:n]):
                try:
                    q = float(lv.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                tot += q
                if i == 0:
                    top = q
            return top, tot

        bid_top, bid5 = _lvl(bids)
        ask_top, ask5 = _lvl(asks)
        tot5 = bid5 + ask5
        return {
            "src": "rh_pricebook_totalview",
            "bid_top": bid_top, "ask_top": ask_top,
            "bid5": round(bid5, 0), "ask5": round(ask5, 0),
            "imbalance5": round((bid5 - ask5) / tot5, 4) if tot5 > 0 else None,
            "levels": min(len(bids), len(asks)),
        }
    except Exception:
        return None


def _register_fresh_quote_tick(db, sess, le: dict, tick: Any = None) -> None:
    """Quote is live again: clear the streak; if a suspected halt was in force, mark
    the RESUME (starts the entry cooldown) so the lane does not buy the whipsaw.

    GAP 1 (Warrior re-audit): when the halt-chain risk gate is ON, on each resume of a
    suspected halt update a PER-SYMBOL consecutive halt-UP counter (le['halt_chain_up_
    count']): increment when the name resumes UP (resumption price at/above the captured
    halt_level — a limit-up halt in a chain) and RESET to 0 when it resumes DOWN/flat (a
    halt-down or fade ends the up-chain). The counter is consumed by halt_chain_risk_gate
    at entry to block/de-weight an over-extended halt-chain long. Flag OFF ⇒ the counter
    is never touched ⇒ byte-identical."""
    le["halt_stale_streak"] = 0
    if le.get("suspected_halt_since_utc"):
        # GAP 1 — update the consecutive halt-UP chain counter (flag-gated). Read the
        # captured halt_level (GAP 2/3 capture) and the resumption price; up ⇒ +1, else
        # reset. Fail-open: any miss leaves the counter unchanged (never blocks on a bug).
        try:
            # The add-into-halt MASTER flag also needs the chain count (its H1 leg is now
            # self-sufficient under the master, independent of the sub-flag) — capture it
            # when EITHER the standalone chain gate OR the add-into-halt master is ON. Both
            # OFF ⇒ the counter is never touched ⇒ byte-identical.
            if bool(getattr(settings, "chili_momentum_halt_chain_risk_gate_enabled", False)) or bool(
                getattr(settings, "chili_momentum_add_into_halt_enabled", False)
            ):
                _hl = _float_or_none(le.get("halt_level"))
                _resume_px = None
                if tick is not None:
                    try:
                        _resume_px = float(getattr(tick, "bid", None) or getattr(tick, "mid", None) or 0) or None
                    except (TypeError, ValueError):
                        _resume_px = None
                _prev = int(le.get("halt_chain_up_count") or 0)
                if _hl is not None and _resume_px is not None and _hl > 0:
                    if _resume_px >= _hl * (1.0 - 1e-9):
                        le["halt_chain_up_count"] = _prev + 1
                    else:
                        le["halt_chain_up_count"] = 0  # resumed down/fade ends the up-chain
                else:
                    # No directional read available ⇒ count it as a halt-up (conservative:
                    # an unclassified halt still extends the chain → tighter, not looser).
                    le["halt_chain_up_count"] = _prev + 1
        except Exception:
            pass
        # Capture the RESUMPTION price (the first fresh price on resume) so the
        # add-into-halt H2/H3 (resumption-direction / false-halt) legs can compare it to
        # the captured halt_level. Flag-gated: only written when a halt-family direction
        # flag is ON, so an OFF lane is byte-identical (no new le key). Fail-open on a miss.
        try:
            if (
                bool(getattr(settings, "chili_momentum_halt_resumption_direction_enabled", False))
                or bool(getattr(settings, "chili_momentum_false_halt_avoid_enabled", False))
                # The add-into-halt master needs the resumption price for its H2/H3 leg.
                # Master OFF + sub-flags OFF ⇒ no halt_resumption_open is written ⇒
                # byte-identical.
                or bool(getattr(settings, "chili_momentum_add_into_halt_enabled", False))
            ) and tick is not None:
                _ro_px = None
                try:
                    _ro_px = float(
                        getattr(tick, "open", None)
                        or getattr(tick, "bid", None)
                        or getattr(tick, "mid", None)
                        or 0
                    ) or None
                except (TypeError, ValueError):
                    _ro_px = None
                if _ro_px is not None and _ro_px > 0:
                    le["halt_resumption_open"] = _ro_px
        except Exception:
            pass
        le.pop("suspected_halt_since_utc", None)
        le["halt_resumed_at_utc"] = _utcnow().isoformat()
        _emit(db, sess, "halt_resumed", {
            "entry_cooldown_seconds": _halt_resume_cooldown_seconds(),
            "halt_chain_up_count": le.get("halt_chain_up_count"),
        })
        # Persist the resume marker NOW — the halt_resume_dip trigger keys its
        # entry window off it, so it must survive a process restart mid-window
        # (other le mutations ride the next commit; this one is load-bearing).
        _commit_le(sess, le)
    # FIX A part (1) — HONOR overnight_flatten_on_fresh (2026-06-25). The dark-bus
    # detector (_register_stale_quote_tick) SET this flag but it was NEVER READ. On
    # the FIRST fresh tick after an overnight dark-bus, flatten the position through
    # the operator-flatten chokepoint (the bus is back, so the exit can fill at the
    # resume book) rather than re-arming the trade on a book that just went dark
    # overnight. Belt to the proactive-onset suspenders: if the bus is only briefly
    # dark and a fresh tick DOES arrive, this closes the still-held position; if the
    # bus stays fully dark, the proactive onset flatten already requested the exit.
    # Kill-switch flag OFF => the flag is cleared but no flatten is requested (legacy:
    # set-but-never-read). Request via the same operator-flatten chokepoint marker.
    if le.get("overnight_flatten_on_fresh"):
        le.pop("overnight_flatten_on_fresh", None)
        if (
            getattr(settings, "chili_momentum_overnight_dark_flatten_enabled", False)
            and isinstance(le.get("position"), dict)
            and float((le.get("position") or {}).get("quantity") or 0) > 0
            and not le.get("overnight_dark_flatten_requested")
            and not le.get("operator_flatten_requested_utc")
        ):
            le["overnight_dark_flatten_requested"] = _utcnow().isoformat()
            _emit(db, sess, "overnight_dark_flatten_requested", {
                "quantity": (le.get("position") or {}).get("quantity"),
                "trigger": "on_fresh_after_dark",
            })
        _commit_le(sess, le)


def _halt_resume_cooldown_active(le: dict) -> bool:
    """True while we are inside the post-resume whipsaw window (entries blocked)."""
    raw = le.get("halt_resumed_at_utc")
    if not raw:
        return False
    try:
        resumed = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return (_utcnow() - resumed).total_seconds() < _halt_resume_cooldown_seconds()


def summarize_live_execution(snap: Any) -> dict[str, Any]:
    if not isinstance(snap, dict):
        return {}
    le = snap.get(KEY_LIVE_EXEC)
    if not isinstance(le, dict):
        return {}
    pos = le.get("position")
    out: dict[str, Any] = {
        "tick_count": le.get("tick_count"),
        "last_tick_utc": le.get("last_tick_utc"),
        "entry_order_id": le.get("entry_order_id"),
        "entry_client_order_id": le.get("entry_client_order_id"),
        "exit_order_id": le.get("exit_order_id"),
        "exit_client_order_id": le.get("exit_client_order_id"),
        "realized_pnl_usd": le.get("realized_pnl_usd"),
        "fees_usd": le.get("fees_usd"),
        "last_mid": le.get("last_mid"),
        "last_exit_reason": le.get("last_exit_reason"),
        "last_exit_intent": le.get("last_exit_intent") if isinstance(le.get("last_exit_intent"), dict) else None,
        "exit_execution_intent_count": len(le.get("exit_execution_intents") or []),
        "pending_exit_reason": le.get("pending_exit_reason"),
        "pending_exit_quantity": le.get("pending_exit_quantity"),
        "pending_exit_submitted_at_utc": le.get("pending_exit_submitted_at_utc"),
        "last_exit_pending_confirmation": (
            le.get("last_exit_pending_confirmation")
            if isinstance(le.get("last_exit_pending_confirmation"), dict)
            else None
        ),
        "last_partial_exit_reason": le.get("last_partial_exit_reason"),
        "last_partial_exit_price": le.get("last_partial_exit_price"),
        "last_quote_quality_gate": (
            le.get("last_quote_quality_gate") if isinstance(le.get("last_quote_quality_gate"), dict) else None
        ),
        "last_exit_notional_basis_usd": le.get("last_exit_notional_basis_usd"),
        "last_exit_return_bps": le.get("last_exit_return_bps"),
        "last_partial_exit_notional_basis_usd": le.get("last_partial_exit_notional_basis_usd"),
        "last_partial_exit_return_bps": le.get("last_partial_exit_return_bps"),
        "cooldown_until_utc": le.get("cooldown_until_utc"),
    }
    if isinstance(pos, dict):
        out["in_position"] = True
        out["avg_entry_price"] = pos.get("avg_entry_price")
        out["quantity"] = pos.get("quantity")
        out["original_quantity"] = pos.get("original_quantity")
        out["notional_usd"] = pos.get("notional_usd")
        out["stop_price"] = pos.get("stop_price")
        out["target_price"] = pos.get("target_price")
        out["high_water_mark"] = pos.get("high_water_mark")
        # Ross asymmetric exit state: did we take the first-target partial yet, and
        # what's the runner riding on?
        out["partial_taken"] = bool(pos.get("partial_taken"))
        out["scaled_out_at_utc"] = pos.get("scaled_out_at_utc")
        out["scale_out_fraction"] = pos.get("scale_out_fraction")
    else:
        out["in_position"] = False
    return out


def list_runnable_live_sessions(db: Session, *, limit: int = 25) -> list[TradingAutomationSession]:
    lim = max(1, min(int(limit), 200))
    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(LIVE_RUNNER_RUNNABLE_STATES),
        )
        .order_by(TradingAutomationSession.updated_at.asc())
        .limit(lim)
        .all()
    )
    return [row for row in rows if not is_operator_paused(row.risk_snapshot_json)]


# Momentum-lane advisory-lock namespace ("ML"), distinct from auto_trader's 0x4154
# ("AT"). The decouple_watching fill-boundary lock key is (this << 32) | user_id, so
# in pg_locks the namespace lands in ``classid`` and the user in ``objid``.
_MOMENTUM_LANE_LOCK_NS = 0x4D4C


def cleanup_leaked_lane_locks(db: Session) -> int:
    """Terminate orphan sessions holding a momentum-lane advisory lock (decouple B1).

    ``pg_advisory_xact_lock`` self-releases on commit/rollback, which covers every
    NORMAL path of both dispatchers. The gap it does NOT cover: a worker force-killed
    (deploy signal / supervisor) mid-submit, before its txn boundary — that leaves the
    lane lock held by an idle-in-transaction backend, wedging EVERY subsequent entry
    for that user until the backend times out. A wedged lane is the safe-failure
    direction (blocks entries, never over-leverages), but should still be cleaned.

    Mirrors ``auto_trader._cleanup_leaked_advisory_locks``: run once per batch (NOT
    per session-tick — it commits). Cheap, idempotent, best-effort; never raises."""
    from sqlalchemy import text as _sql_text

    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return 0
    try:
        threshold_s = max(60, int(getattr(settings, "chili_momentum_lane_leak_cleanup_threshold_s", 120) or 120))
    except Exception:
        threshold_s = 120
    try:
        rows = db.execute(
            _sql_text(
                "SELECT pa.pid, EXTRACT(EPOCH FROM (NOW() - pa.state_change))::int AS age_s, pa.state "
                "FROM pg_stat_activity pa "
                "JOIN pg_locks l ON l.pid = pa.pid "
                "WHERE l.locktype = 'advisory' "
                "  AND l.classid::int = :ns "
                "  AND pa.state IN ('idle in transaction', 'idle in transaction (aborted)') "
                "  AND EXTRACT(EPOCH FROM (NOW() - pa.state_change)) > :thr"
            ),
            {"ns": _MOMENTUM_LANE_LOCK_NS, "thr": threshold_s},
        ).fetchall()
        killed = 0
        for r in rows or []:
            pid, age_s, state = int(r[0]), int(r[1] or 0), r[2]
            try:
                db.execute(_sql_text("SELECT pg_terminate_backend(:p)"), {"p": pid})
                killed += 1
                _log.warning(
                    "[live_runner] lane-lock janitor: terminated leaked session pid=%s "
                    "state=%s age=%ss (orphan lock from an abandoned tick)",
                    pid, state, age_s,
                )
            except Exception as e:
                _log.debug("[live_runner] lane-lock janitor terminate pid=%s failed: %s", pid, e)
        if killed:
            db.commit()
        return killed
    except Exception as e:
        _log.debug("[live_runner] lane-lock janitor pass failed: %s", e)
        return 0


# B3 — agentic-account orphan backstop. The broker-sync reconciler runs on the MAIN
# Robinhood account and is BLIND to the isolated Agentic account, and the lane places no
# broker-side stop — so a position that filled on the agentic rail then lost its session
# (cancel-races-fill / restart) is an unmanaged orphan with no stop at RH. This sweep
# SURFACES such orphans (error-log + event) so the operator / monitor can act. It is
# INERT unless the agentic rail is the active equity rail, and rate-limited.
# RESIDUAL: detect+surface only — auto-adopt/flatten is the final hardening before
# FULLY-unattended operation (see docs/DESIGN/ROBINHOOD_AGENTIC_MCP.md §10).
_AGENTIC_SWEEP_INTERVAL = timedelta(seconds=60)
_agentic_sweep_last = [datetime.min]


def _maybe_sweep_agentic_orphans(db: Session) -> None:
    """Rate-limited, agentic-rail-only orphan detection. Fail-soft — never blocks the lane."""
    try:
        from ..execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP

        rail = str(getattr(settings, "chili_equity_execution_rail", "") or "").strip().lower()
        if rail != EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
            return  # inert until the operator opts the equity lane onto the agentic rail
        if not str(getattr(settings, "chili_robinhood_agentic_mcp_account_number", "") or "").strip():
            return
        now = _utcnow()  # replay v3: sim-clock-governed (prod byte-identical)
        if now - _agentic_sweep_last[0] < _AGENTIC_SWEEP_INTERVAL:
            return
        _agentic_sweep_last[0] = now
        from ..venue.rh_agentic_orphan_sweep import sweep_agentic_orphans

        report = sweep_agentic_orphans(db)
        if report.orphan_symbols:
            _log.error(
                "[live_runner] B3 agentic orphan sweep: %d UNMANAGED position(s) %s "
                "(account_tail=%s) — no broker-side stop at RH; needs adopt/flatten",
                len(report.orphan_symbols), report.orphan_symbols, report.account_tail,
            )
    except Exception:
        _log.debug("[live_runner] agentic orphan sweep skipped (non-fatal)", exc_info=True)


def run_live_runner_batch(
    db: Session,
    *,
    limit: int = 25,
    adapter_factory: Optional[AdapterFactory] = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    _maybe_sweep_agentic_orphans(db)
    for sess in list_runnable_live_sessions(db, limit=limit):
        try:
            out.append(tick_live_session(db, int(sess.id), adapter_factory=adapter_factory))
        except Exception:
            _log.warning("[live_runner] tick failed session=%s", sess.id, exc_info=True)
            out.append({"ok": False, "session_id": sess.id, "error": "tick_exception"})
    return out


_RECONCILE_TICK_INTERVAL = 5  # only reconcile every Nth tick
_reconcile_counters: dict[int, int] = {}


def _reconcile_venue_position(adapter: Any, db: Session, sess: Any, product_id: str) -> None:
    """Rate-limited venue reconciliation: detect orphaned orders or stale positions."""
    sid = int(sess.id)
    _reconcile_counters[sid] = _reconcile_counters.get(sid, 0) + 1
    if _reconcile_counters[sid] % _RECONCILE_TICK_INTERVAL != 0:
        return
    try:
        le = _live_exec(dict(sess.risk_snapshot_json or {}))
        st = sess.state
        entry_oid = le.get("entry_order_id")
        has_pos = isinstance(le.get("position"), dict)

        if not entry_oid:
            return

        # Check if venue has filled order but session hasn't caught up
        if st in (STATE_LIVE_PENDING_ENTRY,) and not has_pos:
            no, _ = adapter.get_order(str(entry_oid))
            if no and no.status == "filled" and float(no.filled_size or 0) > 0:
                _log.warning(
                    "[live_runner] Reconcile: venue shows filled entry for session=%s but state=%s — next tick will process",
                    sid, st,
                )
                _emit(db, sess, "reconcile_stale_entry_detected", {"order_id": entry_oid, "venue_status": no.status})

        # Check if session thinks it has position but venue shows nothing
        if st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING) and has_pos:
            exit_oid = le.get("exit_order_id")
            if exit_oid:
                no, _ = adapter.get_order(str(exit_oid))
                if no and no.status == "filled":
                    _log.warning(
                        "[live_runner] Reconcile: venue shows filled exit for session=%s — marking for review",
                        sid,
                    )
                    _emit(db, sess, "reconcile_orphaned_exit_detected", {"order_id": exit_oid})
    except Exception as e:
        _log.debug("[live_runner] reconcile failed for session=%s: %s", sid, e)


def tick_live_session(
    db: Session,
    session_id: int,
    *,
    adapter_factory: Optional[AdapterFactory] = None,
) -> dict[str, Any]:
    if not settings.chili_momentum_live_runner_enabled:
        return {"ok": True, "skipped": "live_runner_disabled"}

    try:
        sess = (
            db.query(TradingAutomationSession)
            .filter(
                TradingAutomationSession.id == int(session_id),
                TradingAutomationSession.mode == "live",
            )
            .with_for_update(nowait=True)
            .one_or_none()
        )
    except Exception:
        return {"ok": True, "skipped": "concurrent_tick"}
    if sess is None:
        return {"ok": False, "error": "not_found"}
    if is_operator_paused(sess.risk_snapshot_json):
        return {"ok": True, "skipped": "operator_paused", "state": sess.state}
    ef = normalize_execution_family(sess.execution_family)
    if not momentum_runner_supports_execution_family(ef):
        return {"ok": True, "skipped": "execution_family_not_implemented", "execution_family": ef}
    try:
        factory = adapter_factory or resolve_live_spot_adapter_factory(ef)
    except ExecutionFamilyNotImplementedError:
        return {"ok": True, "skipped": "execution_family_not_implemented", "execution_family": ef}
    # ORDER CHUNKING (item 2, DEFAULT OFF ⇒ byte-identical): wrap the resolved factory so the
    # entry place_limit_order_gtc is split into N venue blocks for queue priority. When the
    # flag is OFF or blocks<=1, maybe_wrap_chunking returns the factory UNCHANGED (the exact
    # same adapter object), so every place_*_order is byte-identical. The wrapper is transparent
    # to every other VenueAdapter method (delegates) and folds child broker_order_ids onto the
    # parent for the existing dedupe/orphan reconciliation. NEW order-path: do not enable
    # without soak (the agentic rail's duplicate-fill history).
    try:
        from ..venue.chunking_adapter import maybe_wrap_chunking as _maybe_wrap_chunking

        factory = _maybe_wrap_chunking(factory)
    except Exception:
        pass  # fail-closed to the base factory (byte-identical)
    adapter = factory()
    if not adapter.is_enabled():
        return {"ok": True, "skipped": "coinbase_adapter_unavailable"}

    # Venue-connectivity preflight: never carry this tick (which HOLDS the session's
    # FOR-UPDATE row lock) into broker calls against a DISCONNECTED venue — those
    # calls hang toward network timeout while the transaction sits idle (the residual
    # #565 sibling holder). Cheap in-memory/cached probes; fail-OPEN (rather tick
    # than wrongly freeze a session on a probe error). Ticks resume automatically
    # when the broker reconnects.
    if not _venue_broker_connected(ef):
        return {"ok": True, "skipped": "venue_broker_not_connected", "execution_family": ef}

    if sess.state not in LIVE_RUNNER_RUNNABLE_STATES:
        return {"ok": True, "skipped": "not_runnable", "state": sess.state}

    product_id = sess.symbol.upper().strip()
    if ef == EXECUTION_FAMILY_COINBASE_SPOT:
        # Coinbase crypto convention: ensure the BASE-USD pair suffix.
        if not product_id.endswith("-USD"):
            product_id = f"{product_id}-USD"
    # robinhood_spot: pass the symbol AS-IS — a bare equity ticker (AAPL, ARKK) or
    # an -USD RH-crypto pair. NEVER append -USD to an equity (that broke the entry:
    # AAPL -> AAPL-USD is not a Robinhood product).

    # C2: Orphaned order recovery — reconcile with venue (rate-limited)
    _reconcile_venue_position(adapter, db, sess, product_id)

    snap = dict(sess.risk_snapshot_json or {})
    if RISK_SNAPSHOT_KEY not in snap:
        _emit(db, sess, "live_error", {"reason": "missing_frozen_risk_snapshot"})
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": False, "error": "missing_risk_snapshot"}

    le = _live_exec(snap)
    mid: float | None = None
    bid: float | None = None
    ask: float | None = None

    def _kill_switch_blocks_live() -> bool:
        pol = snap.get("momentum_risk_policy_summary") or {}
        if not pol.get("disable_live_if_governance_inhibit", True):
            return False
        return is_kill_switch_active()

    def _handle_kill_switch_mid_run(flatten_reason: str = "kill_switch_flatten") -> bool:
        """Safest effort: cancel open entry order; flatten if position recorded.
        Reused by the operator FLATTEN button (flatten_reason="operator_flatten")
        so manual exits flow through the same chokepoint chain."""
        nonlocal le, snap
        if le.get("entry_order_id") and not le.get("position"):
            oid = str(le["entry_order_id"])
            cr = adapter.cancel_order(oid)
            _emit(db, sess, "live_order_cancelled", {"order_id": oid, "raw": cr})
        pos = le.get("position")
        if isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0:
            pid = pos.get("product_id") or sess.symbol
            cid = f"chili_ml_x_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=str(pid),
                quantity=float(pos["quantity"]),
                client_order_id=cid,
                reason=flatten_reason,
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"trigger": "kill_switch"},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="kill_switch_flatten"):
                return False
            _emit(db, sess, "live_exit_submitted", {"reason": flatten_reason, "result": sr})
            poll = _poll_live_exit_fill(
                db,
                sess,
                adapter,
                le=le,
                reason=flatten_reason,
                quantity=float(pos["quantity"]),
            )
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=float(pos.get("avg_entry_price") or bid or mid or 0.0),
                        fill_price=float(poll["fill_price"]),
                        reason=flatten_reason,
                    )
                return False
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=float(pos["quantity"]),
                entry_price=float(pos.get("avg_entry_price") or bid or mid or 0.0),
                fill_price=float(poll["fill_price"]),
                reason=flatten_reason,
                slip_bps=float(le.get("entry_slip_bps_ref") or 6.0),
                sell_result=sr,
            )
            return True
        _commit_le(sess, le)
        return True

    # ── Early kill switch (before venue reads) ───────────────────────────
    if _kill_switch_blocks_live() and sess.state in (
        STATE_ARMED_PENDING_RUNNER,
        STATE_QUEUED_LIVE,
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
    ):
        _emit(db, sess, "live_blocked_by_risk", {"reason": "kill_switch"})
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": True, "blocked": True, "reason": "kill_switch"}

    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == int(sess.variant_id),
        )
        .one_or_none()
    )
    if not via:
        _emit(db, sess, "live_error", {"reason": "viability_missing"})
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": False, "error": "no_viability"}
    variant = variant_for_id(db, int(sess.variant_id))
    params = normalize_strategy_params(
        variant.params_json if variant is not None else {},
        family_id=variant.family if variant is not None else None,
    )

    tick, _fr = adapter.get_best_bid_ask(product_id)
    if tick is None or tick.mid is None or tick.mid <= 0:
        _emit(db, sess, "live_blocked_by_risk", {"reason": "no_bbo"})
        if sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
            # A persistently quoteless name (thin/non-common-stock — the RVMDW warrant
            # class) is a DETERMINISTIC policy decline, not a runner error: terminalize
            # cleanly (live_cancelled) so the recurring no_bbo noise stops masking REAL
            # errors. Still blocks entry; only the terminal label changes.
            _decline_terminal(db, sess, reason="no_bbo")
        db.flush()
        return {"ok": True, "blocked": True, "reason": "no_quote"}

    # Adaptive spread tolerance (no magic 12 bps): the BBO spread is a round-trip
    # cost, so gate it relative to how far THIS instrument actually moves (its
    # realized 15m volatility). Explosive momentum names (Ross's universe) carry
    # wider absolute spreads that are still tiny vs. their move; we only ever
    # loosen above the documented floor. The 15m candles are reused below by the
    # M4.1 momentum-continuation trigger, so fetch them once per pre-entry tick.
    # (docs/DESIGN/MOMENTUM_LANE.md)
    _entry_df = None
    _expected_move_bps: float | None = None
    _adaptive_max_spread: float | None = None
    if _live_entry_quote_gate_applies(sess, le):
        try:
            fetch_ohlcv_df = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)

            _entry_df = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
        except Exception:
            _entry_df = None
        _expected_move_bps = _expected_move_bps_from_ohlcv(_entry_df)
        # FIX A: when the frame is too cold/thin to ATR (expected_move_bps None) — the
        # low-float case where the cap would collapse to the 12bps floor and block — derive
        # a CONSERVATIVE name-own-data fallback so the cap scales appropriately. Flag-gated
        # inside _adaptive_live_max_spread_bps; the win-win invariant (under-estimate +
        # abs_cap + small-move names still block) holds.
        _em_fallback_bps: float | None = None
        if _expected_move_bps is None and bool(
            getattr(settings, "chili_momentum_spread_cap_em_fallback_enabled", True)
        ):
            try:
                _em_fallback_bps = _conservative_em_fallback_bps(_entry_df, float(tick.mid))
            except Exception:
                _em_fallback_bps = None
        _adaptive_max_spread = _adaptive_live_max_spread_bps(
            _expected_move_bps, fallback_em_bps=_em_fallback_bps
        )
        # Structured A/B counterfactual: the operator can read how often the cold-frame
        # fallback engaged and how much it raised the cap above the collapse-floor.
        _collapse_floor = _adaptive_live_max_spread_bps(_expected_move_bps)
        _log.info(
            "[momentum_live] adaptive_spread symbol=%s state=%s expected_move_bps=%s "
            "em_fallback_bps=%s max_spread_bps=%.2f collapse_floor_bps=%.2f "
            "em_fallback_engaged=%s cap_delta_bps=%.2f",
            sess.symbol,
            sess.state,
            None if _expected_move_bps is None else round(_expected_move_bps, 2),
            None if _em_fallback_bps is None else round(_em_fallback_bps, 2),
            _adaptive_max_spread,
            _collapse_floor,
            bool(_expected_move_bps is None and _em_fallback_bps is not None),
            round(_adaptive_max_spread - _collapse_floor, 2),
        )

    # SKIP-FOR-LIMITS (operator 2026-06-23): the momentum entry is a marketable LIMIT whose
    # price bounds the fill cost, so the adaptive wide-spread gate is redundant. When enabled,
    # gate the ENTRY on only the abs-cap BROKEN-QUOTE ceiling (a halted/broken book still
    # rejects) — stale_bbo + invalid_bbo reliability checks inside _quote_quality_block ALWAYS
    # apply regardless. Spread becomes a sized COST (L2.2) + the bounded limit, not a veto.
    _skip_spread_gate = bool(getattr(settings, "chili_momentum_skip_spread_gate_for_limit_entry", True))
    _entry_spread_ceiling = (
        float(getattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 1500.0) or 1500.0)
        if _skip_spread_gate
        else _adaptive_max_spread
    )
    quote_block = _quote_quality_block(
        tick, _fr, max_spread_bps=_entry_spread_ceiling, symbol=sess.symbol, db=db
    )
    # Halt tracking: a SUSTAINED stale-quote streak = suspected LULD halt; quotes
    # returning = resume (starts the entry whipsaw-cooldown). A wide-but-live quote
    # is NOT a halt signal — only staleness is.
    if quote_block is not None and quote_block.get("reason") == "stale_bbo":
        _register_stale_quote_tick(db, sess, le, tick)
    else:
        _register_fresh_quote_tick(db, sess, le, tick)
        # R6 (WAVE-4 ITEM-1) — INDEPENDENT print-recency halt inference. The quote path
        # saw this tick as FRESH (the starvation case: a secondary BBO refetch stamps fresh
        # meta on cached quotes), so the stale_bbo streak never fires. But a real LULD halt
        # stops the trade PRINTS while the quote meta stays fresh — so infer the halt from
        # the tape going silent (fail-closed: watched/held + recently-active + open only).
        _register_print_recency_halt_check(db, sess, le, tick)
    # SPREAD STABILITY (2026-06-11 INDP): the instantaneous BBO passed the gate
    # for ONE tick inside a flickering, hostile spread regime — we submitted,
    # the spread blew out a second later, and the eventual fill bought a dying
    # midday book. One snapshot is an opinion; the MEDIAN of the recent tape is
    # the market. Window = 1 entry bar (derived). Fails OPEN below the sample
    # floor (thin tape coverage must not block; the instantaneous gate + ack
    # lifecycle still protect).
    if not _skip_spread_gate and quote_block is None and _live_entry_quote_gate_applies(sess, le) and _adaptive_max_spread is not None:
        try:
            from .nbbo_tape import recent_spread_median_bps

            _stab_window = (
                float(getattr(settings, "chili_momentum_spread_stability_window_bars", 1.0) or 1.0)
                * _entry_interval_seconds()
            )
            _stab = recent_spread_median_bps(db, sess.symbol, window_s=_stab_window)
            _stab_min_n = int(getattr(settings, "chili_momentum_spread_stability_min_samples", 5) or 5)
            if _stab is not None and _stab[1] >= _stab_min_n and _stab[0] > float(_adaptive_max_spread):
                quote_block = {
                    "reason": "unstable_spread",
                    "median_spread_bps": round(_stab[0], 2),
                    "samples": _stab[1],
                    "window_s": round(_stab_window, 1),
                    "max_spread_bps": float(_adaptive_max_spread),
                }
        except Exception:
            _log.debug("[momentum_live] spread stability read skipped", exc_info=True)

    if quote_block is not None:
        quote_block["expected_move_bps"] = (
            None if _expected_move_bps is None else round(_expected_move_bps, 4)
        )
        _emit(db, sess, "live_blocked_by_risk", quote_block)
        le["last_quote_quality_gate"] = quote_block
        _commit_le(sess, le)
        if sess.state in (STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_PENDING_ENTRY) and not le.get("entry_submitted"):
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
        if _live_entry_quote_gate_applies(sess, le):
            db.flush()
            return {"ok": True, "blocked": True, "reason": quote_block.get("reason")}

    mid = float(tick.mid)
    bid = float(tick.bid or mid)
    ask = float(tick.ask or mid)

    ok_b, ev = runner_boundary_risk_ok(
        db, sess, expected_move_bps=_expected_move_bps, apply_eligibility_grace=True
    )
    # A4 MID-MOVE ELIGIBILITY-FLIP RE-SCORE: when the ONLY boundary failure is eligibility /
    # freshness on an ARMED, running-up name, re-score the single symbol (freshness_ts=now) via
    # the SAME path the tape-delta feeder uses, rate-limited to the adaptive tape cadence, then
    # re-run the boundary gate ONCE against the fresh viability. The re-score may flip eligibility
    # True (the slow-curl CLRO-class name) or legitimately confirm False. FAIL-CLOSED: no
    # re-score / still blocked => the original block stands (byte-identical).
    if not ok_b and _maybe_rescore_eligibility_block(db, sess, ev):
        ok_b, ev = runner_boundary_risk_ok(
            db, sess, expected_move_bps=_expected_move_bps, apply_eligibility_grace=True
        )
    if not ok_b:
        _emit(
            db,
            sess,
            "live_blocked_by_risk",
            {"severity": ev.get("severity"), "errors": ev.get("errors")},
        )
        if sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
            # A freshly-armed session whose ONLY boundary-risk failure is a TRANSIENT
            # `viability_freshness` staleness must NOT be terminally errored — the
            # equity refresh re-scores it within the freshness window, so re-watch and
            # retry. Viability staleness was ~100% of boundary-risk blocks at the
            # open; hard-erroring here discarded freshly-armed setups before they
            # could enter. Persistent / safety failures (kill-switch, drawdown,
            # daily-loss cap, concurrency, …) still hard-error. FAIL-SAFE: anything we
            # cannot confirm is freshness-only keeps the conservative ERROR.
            # docs/DESIGN/MOMENTUM_LANE.md
            if _only_transient_freshness_block(ev):
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
            else:
                # A persistent risk-eval BLOCK (not-live-eligible / spread-too-wide / a
                # cap that is not the kill-switch — a DETERMINISTIC policy decline on a
                # name that never held a position) terminalizes cleanly (live_cancelled),
                # not as a runner error. Entry is still blocked; only the label changes.
                _decline_terminal(
                    db,
                    sess,
                    reason="risk_block",
                    detail={"severity": ev.get("severity"), "errors": ev.get("errors")},
                )
            db.flush()
            return {"ok": True, "blocked": True, "risk_evaluation": ev}
        if sess.state == STATE_LIVE_PENDING_ENTRY and le.get("entry_order_id") and not le.get("position"):
            adapter.cancel_order(str(le["entry_order_id"]))
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {"ok": True, "blocked": True, "risk_evaluation": ev}
        if _held_position_keeps_exit_on_boundary_fail(sess.state, le.get("position")):
            # BUGFIX: do NOT block a held position's stop/target on an entry-risk
            # refusal. Kill-switch still force-exits; otherwise fall through to the
            # exit handler below (it places no new entry/scale-in), so the stop is
            # always enforced even if viability went stale / a cap tripped.
            # (2026-06-12: the flatten helper was called UNGATED here — the
            # aggregate at-risk cap breach liquidated ALOY (+$15 winner) and
            # ASTN simultaneously. A cap breach must block NEW risk, never
            # market-dump working positions; only the kill switch flattens.)
            if _kill_switch_blocks_live() and _handle_kill_switch_mid_run():
                _safe_transition(db, sess, STATE_LIVE_EXITED)
                db.flush()
                return {"ok": True, "blocked": True, "risk_evaluation": ev}
            # fall through to exit management (no early return)
        else:
            db.flush()
            return {"ok": True, "blocked": True, "risk_evaluation": ev}

    # ── EOD FLATTEN (2026-06-12 QH: a 3:19 PM entry was still held 2 min
    # before the FRIDAY close — momentum scalps never hold the bell, let alone
    # a weekend). Equity positions flatten through the operator-flatten
    # chokepoint when within the lead window of the 16:00 ET close. Derived
    # from the session clock; the lead is the one documented knob.
    if sess.state in _HELD_LIVE_STATES and not str(sess.symbol or "").upper().endswith("-USD"):
        try:
            from zoneinfo import ZoneInfo as _ZI

            # replay v3: sim-clock-governed ET wall-clock (prod byte-identical).
            _now_et = _now_in_tz(_ZI("America/New_York"))
            _lead = float(getattr(settings, "chili_momentum_eod_flatten_lead_min", 5.0) or 0.0)
            _mins_to_close = (16 * 60) - (_now_et.hour * 60 + _now_et.minute)
            if (
                _lead > 0
                and _now_et.weekday() < 5
                and 0 <= _mins_to_close <= _lead
                and not le.get("operator_flatten_requested_utc")
                and not le.get("eod_flatten_done")
            ):
                le["operator_flatten_requested_utc"] = _utcnow().isoformat()
                le["eod_flatten_done"] = True
                _commit_le(sess, le)
                _emit(db, sess, "eod_flatten_triggered", {
                    "minutes_to_close": _mins_to_close, "lead_min": _lead,
                })
        except Exception:
            _log.debug("eod flatten check failed session=%s", sess.id, exc_info=True)

    # ── Operator FLATTEN (system-mediated manual exit, 2026-06-11) ────────
    # The button sets a flag; the runner honors it HERE (quotes bound) so the
    # exit flows through the one chokepoint chain (scale-out cancel ->
    # broker-qty clamp -> place -> confirm -> reconcile) instead of a
    # broker-app sell racing the system's own resting orders (CPSH/SNDG).
    if le.get("operator_flatten_requested_utc") and sess.state in _HELD_LIVE_STATES:
        le.pop("operator_flatten_requested_utc", None)
        _commit_le(sess, le)
        _flatten_done = _handle_kill_switch_mid_run(flatten_reason="operator_flatten")
        _emit(db, sess, "operator_flatten_executed" if _flatten_done else "operator_flatten_pending", {})
        if _flatten_done:
            _safe_transition(db, sess, STATE_LIVE_EXITED)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state,
                "operator_flatten": bool(_flatten_done)}

    # ── OVERNIGHT DARK-BUS FLATTEN (2026-06-25, FIX A) ────────────────────
    # _register_stale_quote_tick (proactive, first stale onset) and
    # _register_fresh_quote_tick (honoring overnight_flatten_on_fresh) set
    # overnight_dark_flatten_requested when an OVERNIGHT-held position faces a dark
    # price-bus. Honor it HERE — quotes bound — through the SAME operator-flatten
    # chokepoint (cancel scale-out -> broker-qty clamp -> place -> confirm ->
    # reconcile), so there is no oversell and no orphan. This is what makes overnight
    # SAFE: the position is never left to ride a dark bus naked (no broker stop
    # overnight). Flag-gated at the SET sites, so flag OFF => this is unreachable
    # (byte-identical legacy: flag set but never read). If the flatten cannot complete
    # this pulse (e.g. still mid-dark), the request flag PERSISTS so it retries every
    # pulse until the broker confirms flat (FIX B closes the loop on confirmed-zero).
    if le.get("overnight_dark_flatten_requested") and sess.state in _HELD_LIVE_STATES:
        _ovn_flat_done = _handle_kill_switch_mid_run(flatten_reason="overnight_pricebus_dark_flatten")
        _emit(
            db, sess,
            "overnight_dark_flatten_executed" if _ovn_flat_done else "overnight_dark_flatten_pending",
            {},
        )
        if _ovn_flat_done:
            le.pop("overnight_dark_flatten_requested", None)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_EXITED)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state,
                "overnight_dark_flatten": bool(_ovn_flat_done)}

    if _kill_switch_blocks_live() and sess.state in (
        STATE_LIVE_PENDING_ENTRY,
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
    ):
        _emit(db, sess, "live_blocked_by_risk", {"reason": "kill_switch_mid_run"})
        if _handle_kill_switch_mid_run():
            _safe_transition(db, sess, STATE_LIVE_EXITED)
        le = _live_exec(dict(sess.risk_snapshot_json or {}))
        db.flush()
        return {"ok": True, "blocked": True, "reason": "kill_switch"}

    prod: Optional[NormalizedProduct] = None
    try:
        prod, _ = adapter.get_product(product_id)
    except Exception as ex:
        _log.debug("get_product: %s", ex)
    if prod and not prod.tradable_for_spot_momentum():
        # A non-tradeable product (e.g. a warrant/non-common-stock the venue won't trade) is
        # a DETERMINISTIC policy decline. In a pre-entry/no-position state terminalize cleanly
        # (live_cancelled); _decline_terminal falls back to live_error if somehow held.
        _decline_terminal(db, sess, reason="product_not_tradable")
        db.flush()
        return {"ok": False, "error": "product_not_tradable"}

    caps = _policy_caps(snap)
    max_notional = policy_float_cap(
        caps,
        "max_notional_per_trade_usd",
        settings.chili_momentum_risk_max_notional_per_trade_usd,
    )
    try:
        cap_max_hold = int(caps.get("max_hold_seconds") or settings.chili_momentum_risk_max_hold_seconds)
    except (TypeError, ValueError):
        cap_max_hold = int(settings.chili_momentum_risk_max_hold_seconds)
    max_hold = min(int(params.get("max_hold_seconds") or cap_max_hold), cap_max_hold)

    snap = dict(sess.risk_snapshot_json or {})
    le = _live_exec(snap)
    le["tick_count"] = int(le.get("tick_count") or 0) + 1
    le["last_mid"] = mid
    le["last_tick_utc"] = utc_iso()
    _commit_le(sess, le)
    snap = dict(sess.risk_snapshot_json or {})
    le = _live_exec(snap)

    st = sess.state

    # Late-fill sweep (pre-entry states only): an entry order the ack-timeout
    # abandoned can fill SECONDS later (venue cancels are async) — re-point + adopt
    # it before doing anything else, so it becomes a managed position instead of an
    # unmanaged orphan, and so the pre-submit guard below sees venue truth.
    if st in (STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_PENDING_ENTRY) and not le.get(
        "entry_order_id"
    ):
        if _unresolved_entry_order_ids(le) and _sweep_unresolved_entry_orders(adapter, db, sess, le):
            return {"ok": True, "session_id": sess.id, "state": sess.state, "pending": "late_fill_repointed"}
        snap = dict(sess.risk_snapshot_json or {})
        le = _live_exec(snap)

    if st == STATE_ARMED_PENDING_RUNNER:
        _safe_transition(db, sess, STATE_QUEUED_LIVE)
        _emit(db, sess, "live_runner_queued", {"symbol": sess.symbol})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_QUEUED_LIVE:
        _safe_transition(db, sess, STATE_WATCHING_LIVE)
        _emit(db, sess, "live_runner_started", {"mid": mid})
        _emit(db, sess, "live_watch_started", {"product_id": product_id})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_WATCHING_LIVE:
        # HARD NO-TRADE REGIME (Batch E(3), ENTRIES ONLY): around scheduled high-impact
        # events (FOMC/CPI; +/- window) — and, optionally, hard midday — HOLD this watcher
        # (no NEW entry) for the duration. WATCHING is a PRE-POSITION state: a held position
        # lives in ENTERED/SCALING_OUT/TRAILING/BAILOUT (handled far below), so this gate
        # physically cannot block/delay an exit, stop, trail, bailout, scale-out, or dark-
        # flatten. OFF => no-op (byte-identical). Fail-open (helper returns not-blocked on error).
        if bool(getattr(settings, "chili_momentum_hard_no_trade_regime_enabled", False)):
            _ntr_blocked, _ntr_reason = hard_no_trade_regime(
                sess.execution_family, symbol=sess.symbol
            )
            if _ntr_blocked:
                _emit(db, sess, "live_entry_wait_no_trade_regime", {"reason": _ntr_reason})
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "skipped": _ntr_reason}
        # MIDDAY-LULL DE-WEIGHT (project_profitability_levers): during the 10:30-14:30 ET
        # midday chop (6% live win-rate vs 29% morning; Ross sits out midday) RAISE the
        # effective viability bar so only an exceptional mover arms. SOFT (additive bump,
        # not a ban); ENTRY-side only; crypto exempt; clamped to the 0.95 schema ceiling.
        # OFF / bump<=0 => _flat_min unchanged => _score_ok byte-identical, no emit, no import.
        _flat_min = float(params["entry_viability_min"])
        _eff_min, _midday_lull, _midday_bump = _effective_entry_viability_min(_flat_min, sess.symbol)
        # MACRO RUN-R BREAKER (project_profitability_levers L2.1): when the lane's recent
        # realized-R turns negative AND worse than its own baseline (a no-follow-through
        # regime), SOFT-raise the entry bar so fewer marginal setups arm; RELATIVE so it
        # releases when the recent stretch recovers. Entry-side ONLY (never touches exits);
        # OFF / not-triggered / thin-history => _eff_min unchanged => _score_ok byte-identical.
        try:
            from .risk_policy import run_r_viability_bump
            _rr_bump, _rr_meta = run_r_viability_bump(db, sess.execution_family)
        except Exception:
            _rr_bump, _rr_meta = 0.0, None
        if _rr_bump and _rr_bump > 0:
            _new_eff = min(0.95, float(_eff_min) + float(_rr_bump))
            _vscore = float(via.viability_score or 0)
            if _vscore >= _eff_min > 0 and _vscore < _new_eff:
                # the bump actually blocked an otherwise-passing entry — the meaningful A/B event
                _emit(db, sess, "live_run_r_deweighted", {
                    **(_rr_meta or {}), "eff_min_prev": round(float(_eff_min), 3),
                    "eff_min": round(_new_eff, 3), "score": round(_vscore, 3),
                })
            _eff_min = _new_eff
        # WAVE-1 FIX-7 RAISE-ONLY INTEGRITY: the risk raises (midday de-weight, run-R
        # deweight) compose LAST and are raise-only. Snapshot the fully-raised floor HERE
        # (immediately after every raise is applied); the guard clamps the gate's floor to
        # be NEVER below it — so no override or min() inserted between the bumps and the
        # _score_ok gate may silently LOWER the risk-raised bar (the codex ross_audio_starter
        # class; absent on main, guarded here for merges). No-op on main today. OFF => legacy.
        _raise_only_floor = float(_eff_min)
        _eff_min = _raise_only_entry_floor(
            _eff_min,
            _raise_only_floor,
            enabled=bool(getattr(settings, "chili_momentum_floor_raise_only_enabled", True)),
        )
        _score_ok = (
            float(via.viability_score or 0) >= _eff_min
            # live_eligible WITH the recency-grace (UPC TOCTOU): a flicker the boundary gate
            # tolerates must not be re-blocked here. Flag OFF / no evidence ⇒ raw via.live_eligible
            # (byte-identical). See _entry_live_eligible_ok.
            and _entry_live_eligible_ok(db, sess, via)
        )
        # M4.1: require an active momentum-continuation trigger (price > EMA-9 +
        # volume surge) on top of the viability score — Ross enters on confirmed
        # strength, never on a stale score. No confirmation -> WAIT this tick.
        # (docs/DESIGN/MOMENTUM_LANE.md)
        # M4.2: trigger mode (config, default "hybrid") — Ross-style pullback-break
        # on 1m/5m (price breaks the pullback high after a shallow, EMA-9-holding
        # pullback, with a volume spike) PREFERRED, with momentum_volume (15m
        # price>EMA-9 + volume) as the fallback. live + on, fallback-safe.
        _trigger_ok, _trigger_reason = True, "score_only"
        _pb_debug = {}
        # R7 (WAVE-4 ITEM-2b) — per-detector reject map, always defined (the emit reads it
        # even on the _score_ok=False path). Populated as the trigger ladder runs below.
        _reject_map: dict[str, Any] = {}
        if _score_ok:
            try:
                from .entry_gates import momentum_pullback_trigger, momentum_volume_confirmation
                fetch_ohlcv_df = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)

                _mode = str(getattr(settings, "chili_momentum_entry_trigger_mode", "hybrid") or "hybrid").lower()
                _interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                _trigger_ok, _trigger_reason = False, "trigger_wait"
                # R7 (WAVE-4 ITEM-2b) — the wait event carries ONE reason (whatever the LAST
                # detector to run left in _trigger_reason), so a quiet detector's reject is
                # invisible and untunable. Accumulate each detector's own reject reason in
                # _reject_map (detector -> reason) as the ladder runs; surfaced on the
                # live_entry_trigger_wait payload. Telemetry-only, no behavior change.
                # R7 (WAVE-4 ITEM-2a) — the resume-dip reject reason, preserved before the
                # unconditional pullback re-run clobbers _trigger_reason/_pb_debug below.
                _resume_dip_reject: str | None = None
                if _mode in ("hybrid", "pullback_break"):
                    try:
                        _df_pb = fetch_ohlcv_df(sess.symbol, interval=_interval, period="5d")
                        if _df_pb is not None and not getattr(_df_pb, "empty", True):
                            # Halt-resume DIP first (Ross 06-10 DSY: "on the resumption
                            # I bought the dip"): when a suspected halt just resumed,
                            # the specialized dip trigger owns the tape for its window —
                            # it demands dip+hold+reclaim structure, stronger evidence
                            # than the generic pullback-break gives this fast a move.
                            _resumed_at = le.get("halt_resumed_at_utc")
                            if _resumed_at:
                                try:
                                    from .entry_gates import halt_resume_dip_trigger

                                    # GAP 2/3 (Warrior re-audit): pass the captured
                                    # halt_level so the trigger can read the resumption
                                    # DIRECTION (false-halt avoid / conviction modifier).
                                    # None when neither flag captured one ⇒ byte-identical.
                                    _trigger_ok, _trigger_reason, _pb_debug = halt_resume_dip_trigger(
                                        _df_pb, entry_interval=_interval,
                                        halt_resumed_at_utc=_resumed_at,
                                        halt_level=_float_or_none(le.get("halt_level")),
                                    )
                                    # R7 (ITEM-2a): PRESERVE the resume-dip reject reason before
                                    # the pullback re-run overwrites _trigger_reason below. The
                                    # resume-dip trigger OWNED the tape for this window (Ross's
                                    # sanctioned post-resume entry); its reject is the actionable
                                    # "why the resumption dip did not fire" and must not be lost.
                                    if not _trigger_ok:
                                        _resume_dip_reject = _trigger_reason
                                        _reject_map["halt_resume_dip"] = _trigger_reason
                                except Exception:
                                    _trigger_ok = False
                                    _resume_dip_reject = "halt_resume_dip_error"
                                    _reject_map["halt_resume_dip"] = "halt_resume_dip_error"
                            if not _trigger_ok:
                                # Shared trigger (parity): paper calls the SAME helper, so
                                # both paths take the identical Ross pullback-break entry
                                # (vol-aware, candle/VWAP/MACD, runaway). docs/DESIGN/MOMENTUM_LANE.md §8
                                # live_price = the CURRENT ask: when the completed-bar
                                # structure is valid and the live tick is already trading
                                # through the level, fire NOW (tick-break) instead of a
                                # bar-close later — Ross enters on the breaking tick.
                                _live_px = None
                                try:
                                    if tick is not None:
                                        _live_px = float(tick.ask or tick.mid or 0) or None
                                except Exception:
                                    _live_px = None
                                # 15s MICRO-PULLBACK (2026-06-15, "1m too slow"): when
                                # enabled, run the trigger on a 15s micro-bar df built
                                # from the densified tick tape so a micro-pullback break
                                # INSIDE a 1m bar fires sub-minute. The first-pullback
                                # branch in pullback_break_confirmation activates only
                                # when entry_interval == chili_momentum_first_pullback_interval
                                # (set both to '15s' to arm). FAIL-SAFE: insufficient
                                # tick density ⇒ _build_micro_bar_df returns None ⇒ fall
                                # back to the 1m df (byte-identical, no-op when off).
                                _df_trig, _iv_trig = _df_pb, _interval
                                if bool(getattr(settings, "chili_momentum_micropull_enabled", False)):
                                    _bar_s = int(getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15)
                                    # ITEM-7 F1: pass a meta dict so a swallowed micro build error
                                    # surfaces (micro_error_detail) instead of a silent degrade.
                                    _micro_meta: dict[str, Any] = {}
                                    _df_micro = _build_micro_bar_df(db, sess.symbol, bar_seconds=_bar_s, meta=_micro_meta)
                                    if _df_micro is not None and len(_df_micro) >= 10:
                                        _df_trig, _iv_trig = _df_micro, "15s"
                                    elif _micro_meta and isinstance(_pb_debug, dict):
                                        _pb_debug.update(_micro_meta)
                                _trigger_ok, _trigger_reason, _pb_debug = momentum_pullback_trigger(
                                    _df_trig, entry_interval=_iv_trig, live_price=_live_px,
                                    symbol=sess.symbol, db=db,
                                )
                                # R7 (ITEM-2b): record the pullback-break detector's own reject.
                                if not _trigger_ok:
                                    _reject_map["momentum_pullback"] = _trigger_reason
                                # R7 (ITEM-2a): the pullback re-run above is UNCONDITIONAL after a
                                # resume-dip reject and clobbers _trigger_reason. Restore the
                                # resume-dip reject as the primary reason UNLESS the pullback
                                # produced a DIFFERENT actionable result (a fire, or a tick-armed
                                # wait the event loop can act on). Pure decision -> testable helper.
                                _trigger_reason = _preserve_resume_dip_reason(
                                    trigger_ok=_trigger_ok,
                                    pullback_reason=_trigger_reason,
                                    resume_dip_reject=_resume_dip_reject,
                                )
                            # HVM101 (C): two ADDITIVE entry triggers wired into the SAME
                            # ladder (flag-gated INSIDE each detector). Each returns the
                            # shared (ok, reason, debug) shape with pullback_low /
                            # pullback_high under the IDENTICAL keys, so the structural
                            # stop + breakout-or-bailout machinery below is reused
                            # unchanged. Only run when nothing earlier fired (the pullback
                            # break owns the tape first); each is a no-op + byte-identical
                            # when its own kill-switch flag is OFF.
                            if not _trigger_ok:
                                try:
                                    from .entry_gates import flush_dip_buy_confirmation

                                    _fd_ok, _fd_reason, _fd_debug = flush_dip_buy_confirmation(
                                        _df_trig, entry_interval=_iv_trig, live_price=_live_px,
                                        symbol=sess.symbol, now=None,
                                    )
                                    if _fd_ok:
                                        _trigger_ok, _trigger_reason, _pb_debug = _fd_ok, _fd_reason, _fd_debug
                                        # GAP 5 BIG-BUYER-ON-BID starter (Warrior re-audit):
                                        # an ENABLER overlay (NEVER a veto — it cannot block).
                                        # When a flush-dip starter fires, read the bid-side L2
                                        # mirror: a large stacked BUYER on the bid near a half/
                                        # whole dollar (with the existing spread caveat) CONFIRMS
                                        # the dip-buy starter. Surface it as a conviction
                                        # annotation on the dip-fire (FAIL-CLOSED inside: flag
                                        # OFF / no L2 / wide spread ⇒ None ⇒ no annotation, no
                                        # behavior change). docs/DESIGN/MOMENTUM_LANE.md
                                        try:
                                            _bbp = _l2_big_buyer_bid_starter(
                                                sess.symbol, db=db, l2_as_of=None,
                                                price=_live_px,
                                                atr_pct=(
                                                    _fd_debug.get("atr_pct")
                                                    if isinstance(_fd_debug, dict) else None
                                                ),
                                            )
                                            if _bbp is not None and isinstance(_pb_debug, dict):
                                                _pb_debug["big_buyer_bid"] = _bbp[1]
                                        except Exception:
                                            pass
                                    else:  # R7 (ITEM-2b): record the flush-dip detector reject.
                                        _reject_map["flush_dip_buy"] = _fd_reason
                                except Exception:
                                    pass
                            if not _trigger_ok:
                                try:
                                    from .entry_gates import TICK_ARMED_WAIT_REASONS, vwap_reclaim_confirmation

                                    _vr_ok, _vr_reason, _vr_debug = vwap_reclaim_confirmation(
                                        _df_trig, entry_interval=_iv_trig, live_price=_live_px,
                                        symbol=sess.symbol,
                                    )
                                    if not _vr_ok:  # R7 (ITEM-2b): record the vwap-reclaim reject.
                                        _reject_map["vwap_reclaim"] = _vr_reason
                                    if _vr_ok:
                                        _trigger_ok, _trigger_reason, _pb_debug = _vr_ok, _vr_reason, _vr_debug
                                    elif (
                                        _vr_reason in TICK_ARMED_WAIT_REASONS
                                        and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                    ):
                                        # Surface the VWAP-reclaim WAIT so tick-speed dispatch
                                        # re-evaluates the instant price reclaims VWAP (the
                                        # pullback path produced only a terminal wait).
                                        _trigger_reason, _pb_debug = _vr_reason, _vr_debug
                                except Exception:
                                    pass
                            # BATCH B (FIX 2): HOT-TAPE WICK-RECLAIM — the extreme-volatility
                            # variant of the VWAP-reclaim, GATED to hot/parabolic tape ONLY
                            # (is_explosive_mover RVOL/ATR floors inside the detector; cold
                            # tape -> never fires). Re-enter the retrace into a big upper-wick
                            # rejection candle after a low-volume flush; stop below the wick
                            # low. Returns the shared (ok, reason, debug) with pullback_low/
                            # pullback_high under the IDENTICAL keys, so the structural-stop +
                            # bailout machinery below is reused unchanged. No-op + byte-
                            # identical when its kill-switch flag is OFF.
                            if not _trigger_ok:
                                try:
                                    from .entry_gates import wick_reclaim_confirmation

                                    _wr_ok, _wr_reason, _wr_debug = wick_reclaim_confirmation(
                                        _df_trig, entry_interval=_iv_trig, live_price=_live_px,
                                        symbol=sess.symbol,
                                    )
                                    if _wr_ok:
                                        _trigger_ok, _trigger_reason, _pb_debug = _wr_ok, _wr_reason, _wr_debug
                                except Exception:
                                    pass
                            # BATCH D: MICRO-PULLBACK AS PRIMARY — the 1-candle shallow flag as
                            # an INITIAL entry (not just a post-fill re-load), GATED to HOT tape
                            # (_is_hot_tape, like the wick-reclaim) so it does not over-fire on
                            # slow names. A dip-family fire (runs only when nothing earlier fired);
                            # returns the shared (ok, reason, debug) with pullback_low/high under
                            # the IDENTICAL keys. No-op + byte-identical when its kill-switch is
                            # OFF. docs/DESIGN/MOMENTUM_LANE.md
                            if not _trigger_ok:
                                try:
                                    from .entry_gates import micro_pullback_primary_confirmation

                                    _mp_ok, _mp_reason, _mp_debug = micro_pullback_primary_confirmation(
                                        _df_trig, entry_interval=_iv_trig, live_price=_live_px,
                                        symbol=sess.symbol, db=db,
                                    )
                                    if _mp_ok:
                                        _trigger_ok, _trigger_reason, _pb_debug = _mp_ok, _mp_reason, _mp_debug
                                except Exception:
                                    pass
                            # BATCH A: HOD-break + flat-top BREAKOUT triggers + setup-selector.
                            # CHILI's ladder above is ALL dip/pullback/reclaim — a straight-up
                            # HOD runner that never pulls back produces NO fills. These detect a
                            # CONSOLIDATION BASE under the day high and fire the break to a new
                            # HOD (anti-chase: a tested base, never a vertical blow-off — the
                            # detector vetoes backside / rolled-over / over-extended). Each is
                            # flag-gated INSIDE the detector (OFF -> no-op, byte-identical) and
                            # returns the shared (ok, reason, debug) with pullback_low/high under
                            # the IDENTICAL keys, so the structural-stop + bailout machinery below
                            # is reused unchanged. SETUP-SELECTOR: when a dip-family trigger AND a
                            # breakout BOTH fired this bar, pick the best reward:risk (not first-
                            # clears-gates). docs/DESIGN/MOMENTUM_LANE.md
                            try:
                                from .entry_gates import (
                                    TICK_ARMED_WAIT_REASONS,
                                    hod_break_confirmation,
                                    select_best_setup,
                                )

                                # P0: DAILY CONTEXT INTO ENTRIES. Build the DailyContext ONCE
                                # per (symbol, day) in an in-process TTL/size-bounded cache and
                                # feed it to the blue-sky trigger + the overhead veto so the
                                # entry path is no longer daily-blind to overhead supply. The
                                # daily bars are fetched at most once/day/symbol across the whole
                                # process — NO new per-tick network fetch (the DailyContext is a
                                # frozen dataclass and is NOT persisted into the JSON `le`
                                # snapshot — it lives in the module cache only). Equities only
                                # (crypto has no daily-S&R regime here). Gated on the two P0
                                # flags: BOTH off ⇒ no fetch, _daily_ctx stays None ⇒ the entry
                                # path is byte-identical. docs/DESIGN/MOMENTUM_LANE.md
                                _daily_ctx = None
                                try:
                                    _blue_on = bool(getattr(settings, "chili_momentum_blue_sky_entry_enabled", False))
                                    _ovh_on = bool(getattr(settings, "chili_momentum_overhead_veto_enabled", False))
                                    if (_blue_on or _ovh_on) and not str(sess.symbol).upper().endswith("-USD"):
                                        _daily_ctx = _daily_ctx_cached(sess.symbol, price=_live_px)
                                except Exception:
                                    _daily_ctx = None

                                # The dip-family result so far (a FIRE is a candidate for the
                                # selector; a WAIT is preserved as the fallback below).
                                _dip_fire = (
                                    (_trigger_ok, _trigger_reason, _pb_debug) if _trigger_ok else None
                                )
                                _breakouts: list = []
                                for _ft in (False, True):  # HOD break, then flat-top
                                    try:
                                        _hb_ok, _hb_reason, _hb_dbg = hod_break_confirmation(
                                            _df_trig, entry_interval=_iv_trig, flat_top=_ft,
                                            live_price=_live_px, symbol=sess.symbol, db=db,
                                        )
                                    except Exception:
                                        _hb_ok, _hb_reason, _hb_dbg = False, "hod_break_error", {}
                                    if _hb_ok:
                                        _breakouts.append((_hb_ok, _hb_reason, _hb_dbg))
                                    elif (
                                        _hb_reason in TICK_ARMED_WAIT_REASONS
                                        and _hb_dbg.get("pullback_high")
                                        and not _trigger_ok
                                        and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                    ):
                                        # Surface the breakout WAIT so tick-speed dispatch fires
                                        # the instant the live ask trades through the base level
                                        # (the dip ladder produced only a terminal wait).
                                        _trigger_reason, _pb_debug = _hb_reason, _hb_dbg

                                # P0: BLUE-SKY BREAK — the dedicated NEW multi-period/all-time
                                # high break with NO overhead resistance (clear sky), DISTINCT
                                # from hod_break (high-of-DAY only). Reads the session-cached
                                # DailyContext (NO new per-tick fetch); flag-gated INSIDE the
                                # detector (OFF / no DailyContext -> no-op, byte-identical) and
                                # returns the shared (ok, reason, debug) with pullback_low/high
                                # under the IDENTICAL keys, joining the SAME candidate set so the
                                # setup-selector picks the best R:R. docs/DESIGN/MOMENTUM_LANE.md
                                try:
                                    from .entry_gates import blue_sky_break_confirmation

                                    _bsk_ok, _bsk_reason, _bsk_dbg = blue_sky_break_confirmation(
                                        _df_trig, entry_interval=_iv_trig, daily_ctx=_daily_ctx,
                                        live_price=_live_px, symbol=sess.symbol, db=db,
                                    )
                                    if _bsk_ok:
                                        _breakouts.append((_bsk_ok, _bsk_reason, _bsk_dbg))
                                    elif (
                                        _bsk_reason in TICK_ARMED_WAIT_REASONS
                                        and isinstance(_bsk_dbg, dict)
                                        and _bsk_dbg.get("pullback_high")
                                        and not _trigger_ok
                                        and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                    ):
                                        # Surface the blue-sky WAIT so tick-speed dispatch fires
                                        # the instant the ask trades through the new-high level.
                                        _trigger_reason, _pb_debug = _bsk_reason, _bsk_dbg
                                except Exception:
                                    pass

                                # BATCH C: ABCD (SS101 #013) + DOUBLE-BOTTOM breakout triggers.
                                # Both need a swing-pivot scanner the lane lacked; built ATR-noise-
                                # filtered so chop is not read as structure. Each is flag-gated INSIDE
                                # the detector (OFF -> no-op, byte-identical) and returns the shared
                                # (ok, reason, debug) with pullback_low/high under the IDENTICAL keys,
                                # so the structural-stop + bailout machinery below is reused unchanged.
                                # They join the breakout candidate set so the SAME setup-selector picks
                                # the best R:R among dip-family + HOD/flat-top + ABCD/double-bottom.
                                # Run AFTER the existing ladder (additive). docs/DESIGN/MOMENTUM_LANE.md
                                try:
                                    from .entry_gates import (
                                        cup_and_handle_confirmation,
                                        inverse_head_shoulders_confirmation,
                                        ross_abcd_confirmation,
                                        ross_double_bottom_confirmation,
                                    )

                                    for _bc_fn in (
                                        ross_abcd_confirmation,
                                        ross_double_bottom_confirmation,
                                        inverse_head_shoulders_confirmation,
                                        cup_and_handle_confirmation,
                                    ):
                                        try:
                                            _bc_ok, _bc_reason, _bc_dbg = _bc_fn(
                                                _df_trig, entry_interval=_iv_trig,
                                                live_price=_live_px, symbol=sess.symbol, db=db,
                                            )
                                        except Exception:
                                            _bc_ok, _bc_reason, _bc_dbg = False, "batch_c_error", {}
                                        if _bc_ok:
                                            _breakouts.append((_bc_ok, _bc_reason, _bc_dbg))
                                        elif (
                                            _bc_reason in TICK_ARMED_WAIT_REASONS
                                            and isinstance(_bc_dbg, dict)
                                            and _bc_dbg.get("pullback_high")
                                            and not _trigger_ok
                                            and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                        ):
                                            # Surface the ABCD/double-bottom WAIT so tick-speed
                                            # dispatch fires the instant the ask trades through the
                                            # B-high / neckline (the ladder gave only a terminal wait).
                                            _trigger_reason, _pb_debug = _bc_reason, _bc_dbg
                                except Exception:
                                    pass

                                # BATCH D: OPENING-RANGE BREAKOUT (ORB) + RED-TO-GREEN. ORB =
                                # break of the first-N-min opening range (session-time-windowed,
                                # equity-RTH only). RED-TO-GREEN = a name below the session open
                                # reclaiming it on a bottoming-tail reversal. Both BREAKOUT-family
                                # fires that join the SAME candidate set so the setup-selector picks
                                # the best R:R; both flag-gated INSIDE the detector (OFF -> no-op,
                                # byte-identical) and return the shared (ok, reason, debug) with
                                # pullback_low/high under the IDENTICAL keys. Run AFTER the existing
                                # ladder (additive). No lookahead (ranges/levels from completed bars;
                                # the live tick break is the only intrabar use). docs/DESIGN/MOMENTUM_LANE.md
                                try:
                                    from .entry_gates import (
                                        bottom_reversal_confirmation,
                                        ma_vwap_pullback_confirmation,
                                        opening_range_breakout_confirmation,
                                        red_to_green_confirmation,
                                    )

                                    for _bd_fn in (
                                        opening_range_breakout_confirmation,
                                        red_to_green_confirmation,
                                        # SS101 #019 BOTTOM REVERSAL — N consecutive reds then
                                        # the first green close (counter-trend bounce); shares the
                                        # red_to_green signature + the shared (ok,reason,debug)+
                                        # pullback_high/low contract so it joins the SAME candidate
                                        # set the setup-selector arbitrates by R:R. Flag-gated
                                        # INSIDE the detector (default OFF -> no-op, byte-identical).
                                        bottom_reversal_confirmation,
                                        # SS101 #014 MOVING-AVERAGE / VWAP PULLBACK — the
                                        # cooler-market EMA-cascade dip-buy (DEEPER than the
                                        # shallow first-pullback, ALL-DAY unlike the morning-
                                        # only deep-reclaim, dips TO the 9/20-EMA not VWAP):
                                        # fire on the EMA reclaim, stop = the pullback low.
                                        # Shares the (ok,reason,debug)+pullback_high/low
                                        # contract so it joins the SAME candidate set the
                                        # setup-selector arbitrates by R:R. Flag-gated INSIDE
                                        # the detector (default OFF -> no-op, byte-identical).
                                        ma_vwap_pullback_confirmation,
                                    ):
                                        try:
                                            # now=None -> the live real clock (the runner
                                            # is live-only; the session-window read uses the
                                            # DST-correct market-profile open helper).
                                            # l2_as_of=None = the LIVE default (newest
                                            # book snapshot; read_ladder_distribution emits
                                            # the original SQL). Threaded EXPLICITLY so the
                                            # _l2_entry_veto inside each Batch-D gate
                                            # (red_to_green / ORB / bottom_reversal /
                                            # ma_vwap_pullback) reads the live book like the
                                            # other gates — the hidden-seller / big-seller
                                            # veto fires for these too. PRESERVES fail-open:
                                            # no L2 data ⇒ _NULL read ⇒ veto returns None ⇒
                                            # unchanged. Byte-identical (the gate already
                                            # defaults l2_as_of=None).
                                            _bd_ok, _bd_reason, _bd_dbg = _bd_fn(
                                                _df_trig, entry_interval=_iv_trig,
                                                live_price=_live_px, symbol=sess.symbol,
                                                now=None, db=db, l2_as_of=None,
                                            )
                                        except Exception:
                                            _bd_ok, _bd_reason, _bd_dbg = False, "batch_d_error", {}
                                        if _bd_ok:
                                            _breakouts.append((_bd_ok, _bd_reason, _bd_dbg))
                                        elif (
                                            _bd_reason in TICK_ARMED_WAIT_REASONS
                                            and isinstance(_bd_dbg, dict)
                                            and _bd_dbg.get("pullback_high")
                                            and not _trigger_ok
                                            and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                        ):
                                            # Surface the ORB / red-to-green WAIT so tick-speed
                                            # dispatch fires the instant the ask trades through the
                                            # OR-high / open level (the ladder gave only a terminal wait).
                                            _trigger_reason, _pb_debug = _bd_reason, _bd_dbg
                                except Exception:
                                    pass

                                # SS101 #012: BULL FLAG -- the DEEPER (50-70% retrace) 2-3
                                # candle pullback that holds the 9-EMA, then breaks the prior
                                # pullback swing high. DISTINCT from first_pullback (SHALLOW
                                # only) and deep_reclaim (MORNING-only); ALL-DAY. A BREAKOUT-
                                # family fire that joins the SAME candidate set so the setup-
                                # selector picks the best R:R; flag-gated INSIDE the detector
                                # (OFF -> no-op, byte-identical) and returns the shared (ok,
                                # reason, debug) with pullback_low/high under the IDENTICAL
                                # keys. Runs AFTER the existing ladder (additive). No lookahead
                                # (swing high from completed bars; the live tick break is the
                                # only intrabar use). docs/DESIGN/MOMENTUM_LANE.md
                                try:
                                    from .entry_gates import bull_flag_confirmation

                                    # l2_as_of=None = the LIVE default (newest book snapshot).
                                    # Threaded EXPLICITLY so the _l2_entry_veto inside
                                    # bull_flag_confirmation reads the live book like the other
                                    # gates. PRESERVES fail-open (no L2 data ⇒ None ⇒ unchanged);
                                    # byte-identical (the gate already defaults l2_as_of=None).
                                    _bf_ok, _bf_reason, _bf_dbg = bull_flag_confirmation(
                                        _df_trig, entry_interval=_iv_trig,
                                        live_price=_live_px, symbol=sess.symbol,
                                        now=None, db=db, l2_as_of=None,
                                    )
                                    if _bf_ok:
                                        _breakouts.append((_bf_ok, _bf_reason, _bf_dbg))
                                    elif (
                                        _bf_reason in TICK_ARMED_WAIT_REASONS
                                        and isinstance(_bf_dbg, dict)
                                        and _bf_dbg.get("pullback_high")
                                        and not _trigger_ok
                                        and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                    ):
                                        # Surface the bull-flag WAIT so tick-speed dispatch
                                        # fires the instant the ask trades through the swing
                                        # high (the ladder gave only a terminal wait).
                                        _trigger_reason, _pb_debug = _bf_reason, _bf_dbg
                                except Exception:
                                    pass

                                # GAP 1 WEDGE break + GAP 3 ABSORPTION/SNAP (Warrior re-audit):
                                # two NEW breakout-family triggers that join the SAME candidate
                                # set so the setup-selector arbitrates them by R:R. Each is flag-
                                # gated INSIDE the detector (default OFF -> returns disabled before
                                # any compute, byte-identical), carries the SAME chase-guards (tape
                                # REQUIRED+fail-closed via tape_confirms_hold INSIDE the detector,
                                # _hod_extension_ok + _detect_back_side + front_side_state +
                                # _l2_entry_veto) and returns the shared (ok, reason, debug) with
                                # pullback_low/high under the IDENTICAL keys, so the structural-stop
                                # + bailout machinery below is reused unchanged. l2_as_of=None = the
                                # LIVE default. docs/DESIGN/MOMENTUM_LANE.md
                                try:
                                    from .entry_gates import absorption_snap_entry, wedge_break_entry

                                    for _wa_fn in (wedge_break_entry, absorption_snap_entry):
                                        try:
                                            _wa_ok, _wa_reason, _wa_dbg = _wa_fn(
                                                _df_trig, entry_interval=_iv_trig,
                                                live_price=_live_px, symbol=sess.symbol,
                                                now=None, db=db, l2_as_of=None,
                                            )
                                        except Exception:
                                            _wa_ok, _wa_reason, _wa_dbg = False, "wedge_absorption_error", {}
                                        if _wa_ok:
                                            _breakouts.append((_wa_ok, _wa_reason, _wa_dbg))
                                        elif (
                                            _wa_reason in TICK_ARMED_WAIT_REASONS
                                            and isinstance(_wa_dbg, dict)
                                            and _wa_dbg.get("pullback_high")
                                            and not _trigger_ok
                                            and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                        ):
                                            # Surface the wedge / absorption WAIT so tick-speed
                                            # dispatch fires the instant the ask trades through the
                                            # wedge apex / absorption level (the ladder gave only a
                                            # terminal wait).
                                            _trigger_reason, _pb_debug = _wa_reason, _wa_dbg
                                except Exception:
                                    pass

                                # GAP-B: TIGHT-MOMENTUM FALSE-BREAK-REVERSAL / VWAP-RECLAIM — a NEW
                                # trigger family. On a COMPRESSED (coiled) tape with REQUIRED order-
                                # flow + a self-relative volume surge, fire on a false-breakout
                                # REVERSAL (pierce L -> fail/flush below L -> rip back & reclaim L) OR
                                # a VWAP-reclaim from below. Carries the SAME four chase-guards (tape
                                # REQUIRED+fail-closed via tape_confirms_hold INSIDE the detector,
                                # _hod_extension_ok + _detect_back_side + front_side_state +
                                # _l2_entry_veto) and returns the shared (ok, reason, debug) with
                                # pullback_low/high under the IDENTICAL keys, so the structural-stop
                                # + bailout machinery below + the setup-selector are reused unchanged.
                                # MUTUALLY EXCLUSIVE per-tick with raw_break/break_retest: when GAP-B
                                # is ON it joins the SAME candidate set the setup-selector arbitrates,
                                # which returns a SINGLE choice (no double-fire). Flag-gated INSIDE
                                # the detector (default OFF -> disabled before any compute, byte-
                                # identical). l2_as_of=None = the LIVE default. docs/DESIGN/MOMENTUM_LANE.md
                                try:
                                    from .entry_gates import false_break_reclaim_confirmation

                                    _fbr_ok, _fbr_reason, _fbr_dbg = false_break_reclaim_confirmation(
                                        _df_trig, entry_interval=_iv_trig,
                                        live_price=_live_px, symbol=sess.symbol,
                                        now=None, db=db, l2_as_of=None,
                                    )
                                    if _fbr_ok:
                                        _breakouts.append((_fbr_ok, _fbr_reason, _fbr_dbg))
                                    elif (
                                        _fbr_reason in TICK_ARMED_WAIT_REASONS
                                        and isinstance(_fbr_dbg, dict)
                                        and _fbr_dbg.get("pullback_high")
                                        and not _trigger_ok
                                        and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                    ):
                                        # Surface the GAP-B WAIT so tick-speed dispatch fires the
                                        # instant the ask trades through the reclaim level (the
                                        # ladder gave only a terminal wait).
                                        _trigger_reason, _pb_debug = _fbr_reason, _fbr_dbg
                                except Exception:
                                    pass

                                # LOCATE #2/#4/#5/#6: four NEW scalp/dip triggers. Each carries
                                # the SAME chase-guards (tape REQUIRED+fail-closed via
                                # tape_confirms_hold INSIDE the detector, _hod_extension_ok +
                                # _detect_back_side + front_side_state + _l2_entry_veto) and
                                # returns the shared (ok, reason, debug) with pullback_low/high
                                # under the IDENTICAL keys, so the structural-stop + bailout
                                # machinery below + the setup-selector are reused unchanged.
                                # ask_thins/sub_vwap_trap are DIP-family (join the dip-fire slot
                                # when no breakout wins); pulling_away/premarket_pivot are
                                # BREAKOUT-family. Each flag-gated INSIDE the detector (default
                                # OFF -> disabled before any compute, byte-identical). l2_as_of=
                                # None = the LIVE default. docs/DESIGN/MOMENTUM_LANE.md
                                try:
                                    from .entry_gates import (
                                        ask_thins_dip_entry,
                                        premarket_pivot_macd_entry,
                                        pulling_away_roc_entry,
                                        sub_vwap_trap_entry,
                                    )

                                    for _sd_fn in (
                                        ask_thins_dip_entry,
                                        sub_vwap_trap_entry,
                                        pulling_away_roc_entry,
                                        premarket_pivot_macd_entry,
                                    ):
                                        try:
                                            _sd_ok, _sd_reason, _sd_dbg = _sd_fn(
                                                _df_trig, entry_interval=_iv_trig,
                                                live_price=_live_px, symbol=sess.symbol,
                                                now=None, db=db, l2_as_of=None,
                                            )
                                        except Exception:
                                            _sd_ok, _sd_reason, _sd_dbg = False, "scalp_dip_error", {}
                                        if _sd_ok:
                                            _breakouts.append((_sd_ok, _sd_reason, _sd_dbg))
                                        elif (
                                            _sd_reason in TICK_ARMED_WAIT_REASONS
                                            and isinstance(_sd_dbg, dict)
                                            and _sd_dbg.get("pullback_high")
                                            and not _trigger_ok
                                            and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                        ):
                                            # Surface the WAIT so tick-speed dispatch fires the
                                            # instant the ask trades through the level (the ladder
                                            # gave only a terminal wait).
                                            _trigger_reason, _pb_debug = _sd_reason, _sd_dbg
                                except Exception:
                                    pass

                                if _breakouts:
                                    # SETUP-SELECTOR: choose the best R:R among the dip-family fire
                                    # (if any) and the breakout fire(s). Flag OFF -> the first fire
                                    # wins (legacy ladder order) -> byte-identical.
                                    if bool(getattr(settings, "chili_momentum_setup_selector_enabled", True)):
                                        _cands = ([_dip_fire] if _dip_fire else []) + _breakouts
                                        _sel_ok, _sel_reason, _sel_dbg = select_best_setup(
                                            _cands, symbol=sess.symbol,
                                            atr_pct=_pb_debug.get("atr_pct") if isinstance(_pb_debug, dict) else None,
                                            daily_ctx=_daily_ctx,
                                        )
                                        _trigger_ok, _trigger_reason, _pb_debug = _sel_ok, _sel_reason, _sel_dbg
                                    elif not _trigger_ok:
                                        # selector OFF: take the first breakout only if no dip fire.
                                        _trigger_ok, _trigger_reason, _pb_debug = _breakouts[0]
                            except Exception:
                                pass
                    except Exception:
                        _trigger_ok = False
                if not _trigger_ok and _mode != "pullback_break":
                    _df = _entry_df  # reuse the adaptive-spread 15m candles if present
                    if _df is None:
                        _df = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
                    if _df is None or getattr(_df, "empty", True):
                        _trigger_ok, _trigger_reason = False, "no_data_wait"
                    else:
                        _trigger_ok, _trigger_reason = momentum_volume_confirmation(_df)
            except Exception:
                _trigger_ok, _trigger_reason = False, "trigger_error_wait"
        # BATCH B (FIX 1): STICKY BACK-SIDE BENCH. The per-tick front_side_state /
        # _detect_back_side vetoes inside the trigger recompute backside EACH tick, so a
        # name that rolled over midday gets RE-ARMED on the next MACD pivot — chasing a dead,
        # rolled-over top. Ross BENCHES a name once it is on the back side for the rest of the
        # move. Once a CONFIRMED session backside latches le["benched_backside_hod"], the name
        # stays benched (and is NOT re-armed) until the MANDATORY UN-BENCH: a GENUINE NEW HIGH
        # above the benched-at HOD clears the marker (a real new leg can still trade — never a
        # permanent ban). Runs whenever the score qualifies (so the un-bench can clear even on
        # a tick that produced no trigger); only VETOES when a trigger actually fired. Flag
        # OFF -> the marker is never set/read -> byte-identical. Fail-OPEN (never benches on a
        # bug). docs/STRATEGY/CC_REPORTS/2026-06-25_batch-b.md
        if _score_ok and bool(
            getattr(settings, "chili_momentum_sticky_backside_bench_enabled", True)
        ):
            try:
                from .entry_gates import evaluate_sticky_backside_bench
                fetch_ohlcv_df = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)

                _bench_iv = str(
                    getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m"
                )
                _bench_df = fetch_ohlcv_df(sess.symbol, interval=_bench_iv, period="5d")
                _bench_px = None
                try:
                    if tick is not None:
                        _bench_px = float(tick.ask or tick.mid or 0) or None
                except Exception:
                    _bench_px = None
                _benched, _bench_reason, _bench_hod_out, _bench_dbg = evaluate_sticky_backside_bench(
                    _bench_df,
                    benched_at_hod=_float_or_none(le.get("benched_backside_hod")),
                    live_price=_bench_px,
                )
                _prev_benched = le.get("benched_backside_hod") is not None
                if _benched:
                    le["benched_backside_hod"] = float(_bench_hod_out) if _bench_hod_out is not None else le.get("benched_backside_hod")
                    if not _prev_benched:
                        # WAVE-4 ITEM-5(b): PERSIST the bench marker the instant it latches — a
                        # missing commit on the MARKER MUTATION was the permanent-ban hardener
                        # (a process restart mid-tick could lose the un-bench that a later commit
                        # would have carried). Commit each marker state-change atomically here.
                        _commit_le(sess, le)
                        _emit(db, sess, "live_entry_backside_benched", {
                            "reason": _bench_reason, "benched_at_hod": le.get("benched_backside_hod"),
                            **_bench_dbg,
                        })
                    if _trigger_ok:
                        # VETO the fired trigger — the name is benched on the back side.
                        _prev_trigger = _trigger_reason
                        _trigger_ok = False
                        _trigger_reason = "backside_benched"
                        _emit(db, sess, "live_entry_backside_bench_veto", {
                            "blocked_trigger": _prev_trigger, "reason": _bench_reason,
                            "benched_at_hod": le.get("benched_backside_hod"), **_bench_dbg,
                        })
                elif _prev_benched:
                    # MANDATORY UN-BENCH: a genuine new high OR (WAVE-4 ITEM-5) a fresh VWAP-
                    # reclaim CROSS-from-below cleared the bench -> drop the marker so the name
                    # can be armed/entered again on a fresh leg.
                    le.pop("benched_backside_hod", None)
                    # WAVE-4 ITEM-5(b): _commit_le IMMEDIATELY after the pop — the missing commit
                    # is exactly what hardens a permanent ban (the marker survives a restart if the
                    # drop is never persisted). MUST ship with the VWAP-reclaim un-bench (a).
                    _commit_le(sess, le)
                    _emit(db, sess, "live_entry_backside_unbenched", {
                        "reason": _bench_reason, **_bench_dbg,
                    })
                elif _bench_reason == "front_side_vwap_reclaim":
                    # FIX D COUNTERFACTUAL: the below-VWAP bench WOULD have latched here, but
                    # the name is RECLAIMING VWAP from below -> NOT benched. Emit a structured
                    # counter so the operator can read the FIX-D un-bench rate + the trigger
                    # that was preserved (vs the old behaviour that ate SDOT/ILLR) and flip
                    # chili_momentum_backside_vwap_reclaim_enabled off if net-negative. This is
                    # a SAVE, not an entry: every downstream chase-guard still gates the fill.
                    _emit(db, sess, "live_entry_backside_vwap_reclaim_exception", {
                        "fix": "D",
                        "preserved_trigger": (_trigger_reason if _trigger_ok else None),
                        "trigger_ok": bool(_trigger_ok),
                        **_bench_dbg,
                    })
            except Exception as _bench_exc:
                # FIX-19(b): keep FAIL-OPEN (any error -> no bench change, never strand a name)
                # but EMIT a counted instrumentation event instead of a silent bare pass — a
                # backside-bench read that keeps throwing was invisible (the QXL/NXTS chase-guard
                # class), so surface it (rate-countable, per-symbol) for the operator.
                _bench_err_n = int(le.get("backside_bench_error_count") or 0) + 1
                le["backside_bench_error_count"] = _bench_err_n
                try:
                    _emit(db, sess, "live_entry_backside_bench_error", {
                        "error": str(_bench_exc)[:200],
                        "error_type": type(_bench_exc).__name__,
                        "count": _bench_err_n,
                    })
                except Exception:
                    pass  # the instrumentation emit itself must never break the fill path
                _log.warning(
                    "[momentum_live] sticky backside-bench read failed sym=%s (fail-open, count=%d): %s",
                    sess.symbol, _bench_err_n, _bench_exc,
                )
        # GAP 1 + GAP 2 (Warrior re-audit) — HALT-CHAIN RISK GATE + RESUMPTION SIZE
        # MODIFIER, applied ONLY to a halt-resume-dip entry that fired (it shares ALL the
        # existing chase-guards — the bench veto above, the bid-prop confirmer + opening-
        # bell below still run, and the tape-REQUIRED / extension / structural-stop gates
        # downstream are untouched). GAP 1: when the per-symbol consecutive halt-UP count
        # reaches the block threshold, VETO the long (over-extended halt chain); below it,
        # de-weight. GAP 2: fold the resumption_size_mult (from the trigger debug) into the
        # same halt size lever. The combined multiplier is stashed in le["halt_entry_size_
        # mult"] for the sizing path; a clear when not a halt-resume entry keeps it from
        # leaking. RISK-REDUCING + conviction only — it can BLOCK or shrink (and a bounded
        # GAP-2 boost capped by the 3x clamp + max_notional). Both flags OFF ⇒ gate returns
        # disabled + no mult is read ⇒ byte-identical.
        if _trigger_ok and _trigger_reason == "halt_resume_dip_ok":
            try:
                from .entry_gates import halt_chain_risk_gate

                _hc_block, _hc_mult, _hc_reason, _hc_dbg = halt_chain_risk_gate(
                    consecutive_halt_up_count=int(le.get("halt_chain_up_count") or 0),
                )
                if _hc_block:
                    _prev_trigger = _trigger_reason
                    _trigger_ok = False
                    _trigger_reason = "halt_chain_blocked"
                    le.pop("halt_entry_size_mult", None)
                    _emit(db, sess, "live_entry_halt_chain_blocked", {
                        "blocked_trigger": _prev_trigger, "reason": _hc_reason, **_hc_dbg,
                    })
                else:
                    # Compose the GAP-1 chain de-weight with the GAP-2 resumption-direction
                    # modifier (annotated by the trigger in _pb_debug). Both default 1.0.
                    _gap2_mult = 1.0
                    try:
                        if isinstance(_pb_debug, dict):
                            _gm = _float_or_none(_pb_debug.get("resumption_size_mult"))
                            if _gm is not None and _gm > 0:
                                _gap2_mult = float(_gm)
                    except Exception:
                        _gap2_mult = 1.0
                    _combined = float(_hc_mult) * float(_gap2_mult)
                    if abs(_combined - 1.0) > 1e-9:
                        le["halt_entry_size_mult"] = _combined
                        _emit(db, sess, "live_entry_halt_size_modifier", {
                            "halt_chain_mult": round(float(_hc_mult), 4),
                            "resumption_mult": round(float(_gap2_mult), 4),
                            "combined_mult": round(_combined, 4),
                            "halt_chain_reason": _hc_reason,
                            "resumption_direction": (
                                _pb_debug.get("resumption_direction")
                                if isinstance(_pb_debug, dict) else None
                            ),
                            **_hc_dbg,
                        })
                    else:
                        le.pop("halt_entry_size_mult", None)
            except Exception:
                # fail-open: any error -> no block, no size change (never strand a name).
                le.pop("halt_entry_size_mult", None)
        elif not (_trigger_ok and _trigger_reason == "halt_resume_dip_ok"):
            # Not a halt-resume entry this tick: clear any stale halt size lever so it can
            # never leak into a non-halt entry's sizing.
            le.pop("halt_entry_size_mult", None)
        # HVM101 (A): OPENING-BELL SUPPRESSION — hold a FRESH equity trigger in the
        # first ~N min after the 09:30 ET open (opening-auction whipsaw). Equity/RH
        # ONLY; premarket continuation of an already-armed runner is preserved (the
        # held-position short-circuit + the FSM: a premarket runner is in a held/
        # candidate state, a fresh WATCHING_LIVE trigger is by definition fresh).
        # Flag-off / crypto / outside-window ⇒ no-op, byte-identical.
        if (
            _trigger_ok
            and bool(getattr(settings, "chili_momentum_opening_bell_suppression_enabled", True))
        ):
            try:
                _ob_suppress, _ob_dbg = _opening_bell_suppresses_fresh_trigger(
                    sess.symbol, le, has_position=isinstance(le.get("position"), dict),
                    armed_at=getattr(sess, "started_at", None),
                )
            except Exception:
                _ob_suppress, _ob_dbg = False, {}
            if _ob_suppress:
                _trigger_ok = False
                _prev_reason = _trigger_reason
                _trigger_reason = "opening_bell_suppressed"
                _emit(db, sess, "live_entry_opening_bell_suppressed", {
                    "suppressed_trigger": _prev_reason, **_ob_dbg,
                })
        # LOCATE #9 8AM BURST GUARD: a narrow time-windowed DISTRUST of the top-of-hour
        # burst candle (esp. 08:00 ET) — within the guard window of a top-of-hour boundary,
        # DEFER a fresh entry trigger (mirror of the opening-bell suppression). Equity/RH
        # ONLY; the burst candle is order-imbalance noise, not a tradeable break. RISK-
        # REDUCING ONLY: it can only DEFER a fresh fire (never enables/loosens). Flag OFF /
        # crypto / outside-window ⇒ no-op, byte-identical.
        if (
            _trigger_ok
            and not str(sess.symbol or "").upper().endswith("-USD")
            and bool(getattr(settings, "chili_momentum_order_burst_candle_guard_enabled", False))
        ):
            try:
                from zoneinfo import ZoneInfo as _ZIb

                # replay v3: sim-clock-governed ET wall-clock (prod byte-identical).
                _now_et_b = _now_in_tz(_ZIb("America/New_York"))
                _win_min = float(getattr(settings, "chili_momentum_order_burst_guard_window_minutes", 3.0) or 0.0)
                # minutes since the most-recent top-of-hour boundary (0..59).
                _mins_into_hour = _now_et_b.minute + _now_et_b.second / 60.0
                _burst = _win_min > 0.0 and _mins_into_hour < _win_min
            except Exception:
                _burst, _now_et_b = False, None
            if _burst:
                _trigger_ok = False
                _prev_reason = _trigger_reason
                _trigger_reason = "order_burst_candle_deferred"
                _emit(db, sess, "live_entry_order_burst_deferred", {
                    "deferred_trigger": _prev_reason,
                    "hour_et": (_now_et_b.hour if _now_et_b is not None else None),
                    "minute_et": (_now_et_b.minute if _now_et_b is not None else None),
                    "window_minutes": float(getattr(settings, "chili_momentum_order_burst_guard_window_minutes", 3.0) or 0.0),
                })
        # LOCATE #10 RED-CANDLE ENTRY BLOCK: do NOT fire a fresh entry while the CURRENT
        # entry-interval bar is RED (close < open) — Ross never buys into a red candle.
        # DEFER (stay WATCHING) when the latest completed bar on the entry frame is red.
        # RISK-REDUCING ONLY: it can only DEFER a fresh fire (never enables/loosens). Flag
        # OFF / no data ⇒ no-op (fail-OPEN — a missing frame never manufactures a defer),
        # byte-identical.
        if (
            _trigger_ok
            and bool(getattr(settings, "chili_momentum_red_candle_entry_block_enabled", False))
        ):
            try:
                _rc_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)

                _rc_iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                _rc_df = _rc_fetch(sess.symbol, interval=_rc_iv, period="5d")
                _is_red = False
                if _rc_df is not None and not getattr(_rc_df, "empty", True) and len(_rc_df) >= 1:
                    _rc_c = float(_rc_df["Close"].astype(float).iloc[-1])
                    _rc_o = float(_rc_df["Open"].astype(float).iloc[-1])
                    _is_red = _rc_c < _rc_o
            except Exception:
                _is_red = False  # fail-OPEN: a missing/degenerate frame never defers
            if _is_red:
                _trigger_ok = False
                _prev_reason = _trigger_reason
                _trigger_reason = "red_candle_entry_blocked"
                _emit(db, sess, "live_entry_red_candle_blocked", {
                    "blocked_trigger": _prev_reason,
                })
        # HVM101 (B): BID-PROP / SPREAD-TIGHTENING CONFIRMER — confirm a fired break
        # only when, over the last few L1 samples, the best-bid is non-decreasing AND
        # the spread is at/below its short trailing median (genuine backing). Equity/RH
        # ONLY (crypto L1 lives elsewhere). FAIL-OPEN on thin/absent L1 (never blocks).
        # Flag-off ⇒ no-op, byte-identical.
        if (
            _trigger_ok
            and not str(sess.symbol or "").upper().endswith("-USD")
            and bool(getattr(settings, "chili_momentum_bid_prop_confirmer_enabled", True))
        ):
            # GATE 1 (explosive-mover recalibration): BYPASS the bid-prop deterioration
            # confirmer for explosive names. A high-RVOL / extreme-ATR squeeze routinely
            # steps the bid DOWN and widens the spread mid-run (liquidity imbalance, NOT
            # weakness) — that is exactly WHEN the break is working (ILLR/WEN/BB blocked,
            # then ran). The structural pullback-break trigger already read volume +
            # structure cleanly; the confirmer is a RECONFIRM layer, so it must not re-veto
            # the whole pattern on the explosive names the lane targets. MASTER-gated +
            # sub-flag: OFF ⇒ _session_is_explosive is False ⇒ confirmer runs as today.
            _bp_explosive_exempt = (
                bool(getattr(settings, "chili_momentum_bid_prop_explosive_exempt", False))
                and _session_is_explosive(via)
            )
            if _bp_explosive_exempt:
                _bp_ok, _bp_dbg = True, {"reason": "bid_prop_explosive_exempt"}
                _emit(db, sess, "live_entry_bid_prop_explosive_exempt", {
                    "atr_pct": _via_atr_pct(via),
                    "atr_floor": float(getattr(settings, "chili_momentum_explosive_atr_pct_floor", 0.045)),
                })
            else:
                try:
                    _bp_window = (
                        float(getattr(settings, "chili_momentum_spread_stability_window_bars", 1.0) or 1.0)
                        * _entry_interval_seconds()
                    )
                    _bp_ok, _bp_dbg = _bid_prop_confirms_break(db, sess.symbol, window_s=_bp_window)
                except Exception:
                    _bp_ok, _bp_dbg = True, {"reason": "bid_prop_error_fail_open"}
            if not _bp_ok:
                _prev_reason = _trigger_reason
                _trigger_ok = False
                _trigger_reason = "bid_prop_unconfirmed_wait"
                _emit(db, sess, "live_entry_bid_prop_unconfirmed", {
                    "blocked_trigger": _prev_reason, **_bp_dbg,
                })
        # E3: equities ENTER across the EXTENDED session (pre-market → after-hours,
        # per config) so the lane catches Ross's pre-market gap-and-go; crypto is 24/7.
        # Outside-RTH entries are flagged extended_hours at placement (below) so the
        # venue routes them (Alpaca DAY+ext, RH override) instead of rejecting.
        _mkt_open = True
        try:
            from .market_profile import is_tradeable_now

            _mkt_open = bool(is_tradeable_now(sess.symbol))
        except Exception:
            _mkt_open = True
        try:
            logger.info(
                "[momentum_live] entry_branch symbol=%s state=%s mkt_open=%s score_ok=%s trigger_ok=%s trigger_reason=%s",
                sess.symbol, sess.state, _mkt_open, _score_ok, _trigger_ok, _trigger_reason,
            )
        except Exception:
            pass
        # Halt-resume whipsaw guard: right after a suspected halt resumes, price
        # discovery is violent — sit out the cooldown (watching continues, structure
        # rebuilds with fresh bars), then enter on a clean post-resume setup.
        # EXCEPTION: the halt_resume_dip trigger IS the sanctioned post-resume entry
        # (dip+hold+reclaim structure) — it may enter inside the cooldown.
        if (_score_ok and _trigger_ok and _mkt_open and _halt_resume_cooldown_active(le)
                and _trigger_reason != "halt_resume_dip_ok"):
            _emit(db, sess, "live_blocked_by_risk", {
                "reason": "halt_resume_cooldown",
                "halt_resumed_at_utc": le.get("halt_resumed_at_utc"),
                "cooldown_seconds": _halt_resume_cooldown_seconds(),
            })
            db.flush()
            return {"ok": True, "blocked": True, "reason": "halt_resume_cooldown"}
        if _score_ok and _trigger_ok and _mkt_open:
            # Ross structural stop: when the pullback-break trigger fired, stash the
            # pullback low so sizing + placement can stop just UNDER the structure
            # (not at a noise-tight ATR). The momentum_volume fallback has no
            # structure -> clear it so the vol-floored ATR stop is used instead.
            if _trigger_reason in (
                "pullback_break_ok", "pullback_break_tick_ok", "halt_resume_dip_ok",
                "deep_reclaim_ok", "deep_reclaim_tick_ok",
                "deep_reclaim_dipbuy_ok", "deep_reclaim_dipbuy_tick_ok",
                # HVM101 (C): the two new triggers carry pullback_low/high under the
                # SAME keys, so they reuse the IDENTICAL structural-stop + breakout-or-
                # bailout machinery. BATCH B (FIX 2): the hot-tape wick-reclaim carries
                # pullback_low (= the wick/flush low) + pullback_high (= the wick high)
                # under the SAME keys, so it reuses the same machinery.
                "flush_dip_buy", "vwap_reclaim", "wick_reclaim",
                # BATCH A: the HOD-break / flat-top breakouts carry pullback_low (= the
                # consolidation low / structural stop) + pullback_high (= the break level)
                # under the SAME keys, so the structural-stop + bailout machinery is reused.
                "hod_break", "hod_break_tick_ok", "flat_top_break", "flat_top_break_tick_ok",
                # BATCH C: ABCD (SS101 #013) + double-bottom carry pullback_low (= the C-low /
                # double-bottom low = structural stop) + pullback_high (= the B-high / neckline
                # break level) under the SAME keys, so the same machinery is reused.
                "abcd_break", "abcd_break_tick_ok",
                "double_bottom_break", "double_bottom_break_tick_ok",
                # LOCATE #2/#4/#5/#6: the four new scalp/dip triggers carry pullback_low
                # (= the dip/trap/base structural stop) + pullback_high (= the break level)
                # under the SAME keys, so the structural-stop + breakout-or-bailout machinery
                # is reused unchanged.
                "ask_thins_dip", "ask_thins_dip_tick",
                "sub_vwap_trap", "sub_vwap_trap_tick",
                "pulling_away_roc", "pulling_away_roc_tick",
                "premarket_pivot_macd", "premarket_pivot_macd_tick",
                # FIX C(1) BACKSTOP: the explosive RAW first-push / raw-break escapes and the
                # first-pullback fire all carry pullback_low (= the raw/pullback structural stop)
                # + pullback_high (= the break level) under the SAME debug keys (entry_gates.py
                # populates them via _evaluate_raw_break), so they MUST reuse the structural-stop
                # + breakout-or-bailout machinery. Omitting them silently dropped the structural
                # stop on the default-ON first-push path -> noise-tight vol-floored ATR stop on
                # exactly the gappy low-float names the -$697 tail came from.
                "explosive_raw_first_push_ok", "explosive_raw_first_push_tick_ok",
                "explosive_raw_break_ok", "explosive_raw_break_tick_ok",
                "first_pullback_ok", "first_pullback_tick_ok",
            ) and _pb_debug.get("pullback_low"):
                le["structural_stop_price"] = float(_pb_debug["pullback_low"])
                # #2 Breakout-or-bailout: stash the broken pullback HIGH (the breakout
                # level) so the held-position handler can fast-bail if it fails to hold
                # shortly after entry. Cleared on the momentum_volume fallback (which
                # has no structural level). (docs/DESIGN/MOMENTUM_LANE.md §8)
                if _pb_debug.get("pullback_high"):
                    le["breakout_level_price"] = float(_pb_debug["pullback_high"])
                else:
                    le.pop("breakout_level_price", None)
            else:
                le.pop("structural_stop_price", None)
                le.pop("breakout_level_price", None)
            # LOCATE #3 DIP-VELOCITY CONVICTION: scale entry SIZE by the dip ROC for a
            # dip-family fire (steeper flush snaps back harder). The multiplier is in
            # [1.0, 1+max_boost] (NEVER < 1.0) and composes multiplicatively under the SAME
            # 3x clamp + max_notional ceiling as every other size lever, so it can never
            # increase per-trade RISK past the caps. Stash it (mirroring halt_entry_size_
            # mult); cleared on a non-dip fire so it cannot leak. Flag OFF / non-dip / no ROC
            # ⇒ mult 1.0 / key absent ⇒ byte-identical. docs/DESIGN/MOMENTUM_LANE.md
            le.pop("dip_velocity_size_mult", None)
            if _trigger_reason in (
                "flush_dip_buy", "vwap_reclaim", "wick_reclaim",
                "ask_thins_dip", "ask_thins_dip_tick",
                "sub_vwap_trap", "sub_vwap_trap_tick",
            ):
                try:
                    from .entry_gates import _dip_velocity_size_mult

                    _dv_mult = _dip_velocity_size_mult(
                        dip_roc_per_bar=(
                            _float_or_none(_pb_debug.get("dip_roc_per_bar"))
                            if isinstance(_pb_debug, dict) else None
                        ),
                        atr_pct=(
                            _float_or_none(_pb_debug.get("atr_pct"))
                            if isinstance(_pb_debug, dict) else None
                        ),
                    )
                    if _dv_mult is not None and abs(float(_dv_mult) - 1.0) > 1e-9 and float(_dv_mult) > 1.0:
                        le["dip_velocity_size_mult"] = float(_dv_mult)
                        _emit(db, sess, "live_entry_dip_velocity_conviction", {
                            "trigger": _trigger_reason,
                            "dip_roc_per_bar": (_pb_debug.get("dip_roc_per_bar") if isinstance(_pb_debug, dict) else None),
                            "size_mult": round(float(_dv_mult), 4),
                        })
                except Exception:
                    le.pop("dip_velocity_size_mult", None)
            # L2 depth snapshot AT THE DECISION MOMENT (Robinhood pricebook =
            # Nasdaq TotalView, the same book Legend's bid/ask windows show).
            # One GET per entry decision — NOT a stream (streaming an unofficial
            # Gold endpoint is the access pattern that risks the whole RH
            # relationship; a handful of decision-time snapshots is not).
            # Fail-open: no Gold / non-equity / any error -> no snapshot.
            _l2 = _entry_pricebook_snapshot(sess.symbol)
            if _l2 is not None:
                le["entry_l2_snapshot"] = _l2
            else:
                le.pop("entry_l2_snapshot", None)
            # Persist the FIRED trigger reason so the entry-sizing path (which runs on a
            # LATER tick in LIVE_ENTRY_CANDIDATE, where the local _trigger_reason is no
            # longer in scope) can thread it into the adaptive spread-cost veto for the
            # RECLAIM carve-out (derate-only + permissive R base for dip/VWAP-reclaims).
            le["entry_trigger_reason"] = _trigger_reason
            # Stamp above-VWAP for the no-halt vertical-chase knife guard (read on a LATER
            # FIX-B escalation tick where the trigger debug is out of scope). The gate
            # already computed it; absent ⇒ None ⇒ the no-halt deep budget fails closed.
            if isinstance(_pb_debug, dict) and "above_vwap" in _pb_debug:
                le["entry_above_vwap"] = bool(_pb_debug.get("above_vwap"))
            else:
                le.pop("entry_above_vwap", None)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
            _emit(
                db, sess, "live_entry_candidate_detected",
                {"viability_score": via.viability_score, "trigger": _trigger_reason,
                 "structural_stop": le.get("structural_stop_price"),
                 "l2": _l2},
            )
        elif _score_ok and not _mkt_open:
            _emit(db, sess, "live_entry_wait_market_closed", {"symbol": sess.symbol})
        elif _score_ok:
            # ── FIX C: TAPE-CONFIRMED-HOLD EARLY ENTRY (additive earlier-fire) ──────────
            # The break trigger above is WAITING (e.g. waiting_for_reclaim_high) because the
            # current bar has not yet exceeded the pullback high — choppy explosive names
            # rarely give that clean break inside the watch window (then get reaped). Ross
            # enters EARLIER: he buys the pullback-HOLD bounce the moment the TAPE confirms
            # buyers, before the confirmed break. Fire NOW iff ALL hold (else fall through to
            # the existing break-wait, byte-identical):
            #   (1) the trigger is waiting on a VALID PULLBACK family (a real pullback that
            #       held the 9-EMA formed — TAPE_HOLD_VALID_WAIT_REASONS);
            #   (2) the TAPE CONFIRMS a bounce — tape_confirms_hold is REQUIRED + FAIL-CLOSED
            #       (signed_tape_accel>0 AND tick_rate>=floor; missing/thin/stale/crypto tape
            #       ⇒ no fire, keep the break path);
            #   (3) price is HOLDING/turning up off the 9-EMA band + a higher low vs the
            #       pullback low (NOT broken down) — tape_confirmed_hold_trigger;
            #   (4) NOT benched / NOT backside / NOT below VWAP (re-checked in the struct
            #       trigger via _detect_back_side + front_side_state; the sticky bench already
            #       forced _trigger_reason='backside_benched' above, which is NOT a valid wait
            #       reason, so a benched name never reaches here);
            #   (5) ALL existing entry vetoes + the quote gate still run — this only promotes
            #       WATCHING -> LIVE_ENTRY_CANDIDATE, which routes through the SAME
            #       LIVE_PENDING_ENTRY veto chain (_entry_flow_veto, _entry_extension_veto,
            #       overhead/L2 vetoes, _l2_entry_confirm, position cap, _quote_quality_block).
            # KILL-SWITCH chili_momentum_tape_hold_entry_enabled OFF ⇒ tape_confirms_hold
            # returns (False, ...) before any I/O ⇒ this whole block is a no-op (break-only).
            _tape_hold_fired = False
            try:
                logger.info(
                    "[momentum_live] tape_hold_gate symbol=%s reason=%s flag=%s in_valid=%s pb_low=%s benched=%s",
                    sess.symbol, _trigger_reason,
                    bool(getattr(settings, "chili_momentum_tape_hold_entry_enabled", False)),
                    _trigger_reason in TAPE_HOLD_VALID_WAIT_REASONS,
                    (_pb_debug.get("pullback_low") if isinstance(_pb_debug, dict) else None),
                    le.get("benched_backside_hod"),
                )
            except Exception:
                pass
            if (
                bool(getattr(settings, "chili_momentum_tape_hold_entry_enabled", False))
                and _trigger_reason in TAPE_HOLD_VALID_WAIT_REASONS
                and isinstance(_pb_debug, dict)
                and _pb_debug.get("pullback_low") is not None
                and le.get("benched_backside_hod") is None
            ):
                try:
                    _th_px = None
                    try:
                        if tick is not None:
                            _th_px = float(tick.ask or tick.mid or 0) or None
                    except Exception:
                        _th_px = None
                    # (2) REQUIRED tape confirm — fail-CLOSED.
                    _tape_ok, _tape_dbg = tape_confirms_hold(sess.symbol, db=db, settings=settings)
                    try:
                        logger.info(
                            "[momentum_live] tape_hold_tape symbol=%s tape_ok=%s accel=%s rate=%s floor=%s n=%s reason=%s",
                            sess.symbol, _tape_ok, _tape_dbg.get("signed_tape_accel"), _tape_dbg.get("tick_rate"),
                            _tape_dbg.get("tick_rate_floor"), _tape_dbg.get("n_ticks"), _tape_dbg.get("reason"),
                        )
                    except Exception:
                        pass
                    if _tape_ok:
                        # (3)+(4) structural hold + not-backside on the SAME entry-interval df.
                        _th_iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                        _th_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)

                        _th_df = _th_fetch(sess.symbol, interval=_th_iv, period="5d")
                        _th_struct_ok, _th_reason, _th_sdbg = tape_confirmed_hold_trigger(
                            _th_df,
                            pullback_high=_float_or_none(_pb_debug.get("pullback_high")),
                            pullback_low=_float_or_none(_pb_debug.get("pullback_low")),
                            live_price=_th_px,
                            retracement_threshold=float(
                                getattr(settings, "chili_momentum_pullback_retracement_threshold", 0.50) or 0.50
                            ),
                            entry_interval=_th_iv,
                        )
                        try:
                            logger.info(
                                "[momentum_live] tape_hold_struct symbol=%s struct_ok=%s reason=%s",
                                sess.symbol, _th_struct_ok, _th_reason,
                            )
                        except Exception:
                            pass
                        if _th_struct_ok:
                            # Reuse the EXACT structural-stop + breakout-level stash the break
                            # path uses (pullback_low = structural stop, pullback_high = the
                            # breakout-or-bailout level), so sizing/placement/bailout are identical.
                            le["structural_stop_price"] = float(_th_sdbg["pullback_low"])
                            if _th_sdbg.get("pullback_high"):
                                le["breakout_level_price"] = float(_th_sdbg["pullback_high"])
                            else:
                                le.pop("breakout_level_price", None)
                            _l2 = _entry_pricebook_snapshot(sess.symbol)
                            if _l2 is not None:
                                le["entry_l2_snapshot"] = _l2
                            else:
                                le.pop("entry_l2_snapshot", None)
                            le["entry_trigger_reason"] = "tape_confirmed_hold"
                            le.pop("watch_break_level", None)
                            # above-VWAP for the no-halt vertical-chase knife guard (see the
                            # break path); absent ⇒ the no-halt deep budget fails closed.
                            if isinstance(_th_sdbg, dict) and "above_vwap" in _th_sdbg:
                                le["entry_above_vwap"] = bool(_th_sdbg.get("above_vwap"))
                            else:
                                le.pop("entry_above_vwap", None)
                            _commit_le(sess, le)
                            _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
                            _emit(db, sess, "live_entry_tape_hold_fire", {
                                "blocked_wait_reason": _trigger_reason,
                                "viability_score": via.viability_score,
                                "structural_stop": le.get("structural_stop_price"),
                                "breakout_level": le.get("breakout_level_price"),
                                **{k: _tape_dbg.get(k) for k in (
                                    "signed_tape_accel", "tick_rate", "tick_rate_floor", "n_ticks")},
                                **{k: _th_sdbg.get(k) for k in (
                                    "ema9", "ema_wick", "cur_px", "above_vwap", "atr_pct")},
                                "l2": _l2,
                            })
                            _tape_hold_fired = True
                except Exception:
                    _tape_hold_fired = False  # fail-safe: any error -> fall back to break-wait
            if _tape_hold_fired:
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            # ── FIX 1: MOMENTUM-CONTINUATION ENTRY (additive new-high fire) ─────────────
            # The break trigger above is WAITING because every existing trigger needs a
            # PULLBACK / consolidation BASE — and the STRONGEST movers (WSHP +47% 40x RVOL
            # viab 0.759 breaking_major, SDOT +25% 132x RVOL viab 0.768) trend STRAIGHT UP
            # with no pullback, so they are caught + watched but NEVER enter, then reaped at
            # 300s while the lane trades weaker pullback names. Ross BUYS the continuation:
            # a fresh new high on a HIGH-CONVICTION, front-side, NOT-parabolic name with the
            # TAPE confirming buyers. Fire NOW iff ALL FIVE hold (else fall through to the
            # break-wait, byte-identical):
            #   (1) HIGH-CONVICTION ONLY — ross_score >= floor OR RVOL >= the coiling-exempt
            #       multiple OR daily_breaking_major (never a low-conviction/random name);
            #   (2) NEW HIGH / HOD break — momentum_continuation_trigger (a fresh high above
            #       the recent high, NO prior pullback / base required);
            #   (3) TAPE CONFIRMS — REQUIRED + FAIL-CLOSED (tape_confirms_hold: signed_tape_
            #       accel>0 AND tick_rate>=floor; no/thin/stale/selling/crypto tape ⇒ no fire);
            #   (4) NOT PARABOLIC — the #1 chase guard (_hod_extension_ok / _entry_extension_
            #       veto vs 9-EMA AND VWAP, inside the trigger; re-checked downstream);
            #   (5) NOT backside / NOT below-VWAP (_detect_back_side + front_side_state inside
            #       the trigger; the sticky bench already forced 'backside_benched' above, and
            #       a benched name is gated below); + the structural stop + ALL downstream
            #       LIVE_PENDING_ENTRY vetoes (_entry_flow_veto, _entry_extension_veto, L2 /
            #       overhead, _l2_entry_confirm, position cap, _quote_quality_block) still run.
            # KILL-SWITCH chili_momentum_momentum_continuation_entry_enabled OFF ⇒ the trigger
            # returns (False, ..._disabled) before any compute ⇒ this whole block is a no-op
            # (byte-identical). Runs only when the break path did NOT already fire (this is the
            # elif _score_ok: WAIT branch) and the name is NOT benched.
            _continuation_fired = False
            if (
                bool(getattr(settings, "chili_momentum_momentum_continuation_entry_enabled", False))
                and le.get("benched_backside_hod") is None
            ):
                try:
                    # (1) HIGH-CONVICTION read — ross_score / RVOL / daily_breaking_major from
                    # the session's own persisted scanner row (execution_readiness_json.extra),
                    # the SAME source the arm-queue ranker + viability tilt read. No new fetch.
                    _ross_score = None
                    _daily_breaking = False
                    try:
                        _ex = via.execution_readiness_json if isinstance(via.execution_readiness_json, dict) else {}
                        _extra = (_ex.get("extra") or {}) if isinstance(_ex, dict) else {}
                        _symu = str(sess.symbol or "").upper()
                        _rs_map = _extra.get("ross_scores") if isinstance(_extra.get("ross_scores"), dict) else {}
                        if _symu in _rs_map:
                            _ross_score = float(_rs_map[_symu] or 0.0)
                        _sig_map = _extra.get("ross_signals") if isinstance(_extra.get("ross_signals"), dict) else {}
                        _row_sig = _sig_map.get(_symu) if isinstance(_sig_map, dict) else None
                        if isinstance(_row_sig, dict):
                            _daily_breaking = bool(_row_sig.get("daily_breaking_major"))
                    except (TypeError, ValueError, AttributeError):
                        _ross_score, _daily_breaking = None, False
                    from .entry_gates import (
                        compute_intraday_rvol_fallback,
                        continuation_conviction_floors,
                        continuation_high_conviction,
                        momentum_continuation_trigger,
                    )
                    _mc_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)

                    _mc_iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                    _rvol_now = _latest_rvol(db, sess.symbol)
                    # ROW-SIGNAL PRECEDENCE: only when this name carries NO usable conviction
                    # signal (scanner-only: ross_score None, micro-bar RVOL None, not
                    # daily_breaking) fill the EMPTY RVOL axis from the 5m/5d frame the struct
                    # trigger fetches anyway — fetch it ONCE here and REUSE it downstream (zero
                    # added fetch). Kill-switch OFF ⇒ helper returns None ⇒ byte-identical.
                    _mc_df = None
                    # KILL-SWITCH GATE: the empty-signal RVOL-fallback (and its OHLCV fetch)
                    # only run when chili_momentum_conviction_rvol_fallback_enabled is ON.
                    # Flag OFF ⇒ NO fetch is hoisted here and _rvol_now stays None for
                    # scanner-only names ⇒ byte-identical to deployed 1e2eb09 (the baseline
                    # short-circuits via high_conviction=False below and issues no fetch).
                    if bool(getattr(settings, "chili_momentum_conviction_rvol_fallback_enabled", False)):
                        if _rvol_now is None and _ross_score is None and not _daily_breaking:
                            _mc_df = _mc_fetch(sess.symbol, interval=_mc_iv, period="5d")
                            _rvol_now = compute_intraday_rvol_fallback(
                                _mc_df, symbol=sess.symbol, settings_obj=settings
                            )
                    _ross_floor, _rvol_conviction_floor = continuation_conviction_floors(settings)
                    # THE shared conviction test — IDENTICAL definition at arm-time + entry-time.
                    _high_conviction = continuation_high_conviction(
                        _ross_score, _rvol_now, _daily_breaking, settings
                    )
                    try:
                        logger.info(
                            "[momentum_live] continuation_gate symbol=%s high_conv=%s ross=%s ross_floor=%s rvol=%s rvol_floor=%s breaking=%s",
                            sess.symbol, _high_conviction, _ross_score, _ross_floor,
                            _rvol_now, _rvol_conviction_floor, _daily_breaking,
                        )
                    except Exception:
                        pass
                    if _high_conviction:
                        _mc_px = None
                        try:
                            if tick is not None:
                                _mc_px = float(tick.ask or tick.mid or 0) or None
                        except Exception:
                            _mc_px = None
                        # Reuse the frame already fetched for the fallback when present; else fetch.
                        if _mc_df is None:
                            _mc_df = _mc_fetch(sess.symbol, interval=_mc_iv, period="5d")
                        # (2) NEW HIGH + (4) not parabolic + (5) not backside (inside).
                        _mc_ok, _mc_reason, _mc_dbg = momentum_continuation_trigger(
                            _mc_df, live_price=_mc_px, entry_interval=_mc_iv,
                            symbol=sess.symbol, db=db, l2_as_of=None,
                        )
                        try:
                            logger.info(
                                "[momentum_live] continuation_struct symbol=%s mc_ok=%s reason=%s pb_low=%s pb_high=%s",
                                sess.symbol, _mc_ok, _mc_reason,
                                (_mc_dbg.get("pullback_low") if isinstance(_mc_dbg, dict) else None),
                                (_mc_dbg.get("pullback_high") if isinstance(_mc_dbg, dict) else None),
                            )
                        except Exception:
                            pass
                        if _mc_ok and isinstance(_mc_dbg, dict) and _mc_dbg.get("pullback_low") is not None:
                            # (3) REQUIRED tape confirm — fail-CLOSED (no/thin/stale/selling
                            # tape ⇒ NO continuation fire; the distribution / dead-cat guard).
                            _mc_tape_ok, _mc_tape_dbg = tape_confirms_hold(
                                sess.symbol, db=db, settings=settings
                            )
                            try:
                                logger.info(
                                    "[momentum_live] continuation_tape symbol=%s tape_ok=%s accel=%s rate=%s floor=%s n=%s reason=%s",
                                    sess.symbol, _mc_tape_ok,
                                    _mc_tape_dbg.get("signed_tape_accel"), _mc_tape_dbg.get("tick_rate"),
                                    _mc_tape_dbg.get("tick_rate_floor"), _mc_tape_dbg.get("n_ticks"),
                                    _mc_tape_dbg.get("reason"),
                                )
                            except Exception:
                                pass
                            if _mc_tape_ok:
                                # Reuse the EXACT structural-stop + breakout-level stash the
                                # break path uses (pullback_low = structural stop, pullback_high
                                # = the breakout-or-bailout level), so sizing/placement/bailout
                                # are identical, then route through the SAME LIVE_ENTRY_CANDIDATE
                                # -> LIVE_PENDING_ENTRY veto chain.
                                le["structural_stop_price"] = float(_mc_dbg["pullback_low"])
                                if _mc_dbg.get("pullback_high"):
                                    le["breakout_level_price"] = float(_mc_dbg["pullback_high"])
                                else:
                                    le.pop("breakout_level_price", None)
                                _l2 = _entry_pricebook_snapshot(sess.symbol)
                                if _l2 is not None:
                                    le["entry_l2_snapshot"] = _l2
                                else:
                                    le.pop("entry_l2_snapshot", None)
                                le["entry_trigger_reason"] = "momentum_continuation"
                                le.pop("watch_break_level", None)
                                # above-VWAP for the no-halt vertical-chase knife guard (see
                                # the break path); absent ⇒ the no-halt deep budget fails closed.
                                if isinstance(_mc_dbg, dict) and "above_vwap" in _mc_dbg:
                                    le["entry_above_vwap"] = bool(_mc_dbg.get("above_vwap"))
                                else:
                                    le.pop("entry_above_vwap", None)
                                _commit_le(sess, le)
                                _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
                                _emit(db, sess, "live_entry_momentum_continuation_fire", {
                                    "blocked_wait_reason": _trigger_reason,
                                    "continuation_reason": _mc_reason,
                                    "viability_score": via.viability_score,
                                    "ross_score": _ross_score,
                                    "rvol": _rvol_now,
                                    "daily_breaking_major": _daily_breaking,
                                    "structural_stop": le.get("structural_stop_price"),
                                    "breakout_level": le.get("breakout_level_price"),
                                    **{k: _mc_tape_dbg.get(k) for k in (
                                        "signed_tape_accel", "tick_rate", "tick_rate_floor", "n_ticks")},
                                    **{k: _mc_dbg.get(k) for k in (
                                        "recent_high", "recent_low", "above_vwap", "atr_pct")},
                                    "l2": _l2,
                                })
                                _continuation_fired = True
                except Exception:
                    _continuation_fired = False  # fail-safe: any error -> fall back to break-wait
            if _continuation_fired:
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            # Stash the level we're WAITING to break so the event loop can dispatch
            # a tick-speed re-evaluation the instant a live tick crosses it (the
            # tick-break path above then fires within seconds, like Ross).
            from .entry_gates import TICK_ARMED_WAIT_REASONS

            _wl = _pb_debug.get("pullback_high") if isinstance(_pb_debug, dict) else None
            if _trigger_reason in TICK_ARMED_WAIT_REASONS and _wl:
                if le.get("watch_break_level") != float(_wl):
                    le["watch_break_level"] = float(_wl)
                    _commit_le(sess, le)
            elif le.pop("watch_break_level", None) is not None:
                _commit_le(sess, le)
            # R7 (WAVE-4 ITEM-2b): attach the per-detector reject map so quiet detectors
            # become tunable (the single `reason` is only the last detector to run). The
            # resume-dip reject (ITEM-2a) is preserved in _reject_map["halt_resume_dip"]
            # AND, when the later pullback produced only another inert wait, as the primary
            # `reason`. Telemetry-only.
            _wait_payload: dict[str, Any] = {"reason": _trigger_reason}
            if _reject_map:
                _wait_payload["detector_rejects"] = dict(_reject_map)
            _emit(db, sess, "live_entry_trigger_wait", _wait_payload)
        elif _midday_lull and via.live_eligible and float(via.viability_score or 0) >= _flat_min:
            # Forward A/B observability: this equity WOULD have advanced at the flat
            # viability bar but the midday de-weight held it back. Lets the operator
            # validate the lever LIVE (did the de-weighted names actually underperform
            # the rest-of-day?) without changing any trade. (project_profitability_levers)
            _emit(db, sess, "live_entry_midday_deweighted", {
                "viability_score": via.viability_score,
                "flat_min": _flat_min, "eff_min": _eff_min, "bump": _midday_bump,
            })
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_ENTRY_CANDIDATE:
        # live_eligible re-read WITH the recency-grace (UPC TOCTOU): a flicker the boundary gate
        # tolerated must not revert the candidate here. Flag OFF / no evidence ⇒ raw via.live_eligible.
        if float(via.viability_score or 0) < float(params["entry_revalidate_floor"]) or not _entry_live_eligible_ok(db, sess, via):
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
        else:
            _safe_transition(db, sess, STATE_LIVE_PENDING_ENTRY)
            # State-transition marker ONLY — no broker order exists yet. This used to
            # emit "live_entry_submitted" with an empty-ish payload, producing TWO
            # "submitted" events per cycle (one phantom, one real) and corrupting
            # entries-per-session / time-to-fill analytics (BATL post-mortem).
            _emit(db, sess, "live_entry_pending_place", {"note": "pending_place"})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_PENDING_ENTRY:
        # live_eligible pre-submit re-read WITH the recency-grace (UPC TOCTOU): a tolerated
        # flicker must not bounce the pending entry back to watching. Flag OFF / no evidence ⇒
        # raw via.live_eligible (byte-identical).
        if not le.get("entry_submitted") and (
            float(via.viability_score or 0) < float(params["entry_revalidate_floor"]) or not _entry_live_eligible_ok(db, sess, via)
        ):
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}
        # PRE-SUBMIT GUARD: while any previously-placed entry order is UNRESOLVED
        # (abandoned by an ack-timeout but not yet confirmed cancelled-with-zero-fill
        # by the venue), placing another order can stack a second real position on a
        # late fill of the first. Hold the submit; the sweep above resolves the ids
        # (adopt the fill / void the clean cancel) within a tick or two.
        # [BATL 2026-06-10: 5 such stacked clips -> ~$8k unmanaged.]
        if not le.get("entry_submitted"):
            _stale_oids = _unresolved_entry_order_ids(le)
            if _stale_oids:
                _emit(db, sess, "live_entry_blocked_unresolved_orders", {
                    "unresolved_order_ids": _stale_oids[:5],
                    "count": len(_stale_oids),
                })
                db.flush()
                return {
                    "ok": True, "session_id": sess.id, "state": sess.state,
                    "blocked": True, "reason": "unresolved_entry_orders",
                }
        if le.get("entry_submitted") and le.get("entry_order_id"):
            # CHUNK 3-B — FAST ACK-POLL: confirm the entry fill WITHIN this tick by
            # polling get_order at the measured rail RTT (geometric widen), bounded by the
            # EXISTING rest_bars * entry_interval ack-timeout window + a hard iter cap +
            # the rail governor. On confirm we fall straight into the SAME adopt path
            # below (no new write path). On a non-fill `no` is the last poll, so the
            # existing open/cancel/repeg logic runs exactly as today. Fast-poll OFF ⇒ a
            # single governed get_order (byte-identical confirm). The window mirrors the
            # cancel-side backstop at the bottom of this branch (rest_bars * interval).
            _fp_window_s = (
                float(getattr(settings, "chili_momentum_entry_max_rest_bars", 2.0) or 2.0)
                * float(_entry_interval_seconds())
            )
            no, _ = _fast_ack_poll_entry(
                adapter, le["entry_order_id"], sess=sess, interval_window_s=_fp_window_s,
            )
            # OPEN-WITH-FILLS (2026-06-11 INDP): RH can hold an order in state
            # "open" with shares ALREADY filled (a silently-failed cancel + a
            # later fill). Those shares are OURS the moment they exist — adopt
            # on fills, never on order-state ceremony. Cancel the open
            # remainder first (single clip; extra post-adoption fills reconcile
            # via broker-sync against broker truth).
            if no and _order_open(no) and float(no.filled_size or 0.0) > 0.0:
                try:
                    adapter.cancel_order(str(le["entry_order_id"]))
                except Exception:
                    _log.debug("[momentum_live] remainder cancel failed", exc_info=True)
                _emit(db, sess, "entry_open_with_fills_adopting", {
                    "order_id": str(le["entry_order_id"]),
                    "filled_size": float(no.filled_size or 0.0),
                })
            if no and (_order_done_for_entry(no) or float(no.filled_size or 0.0) > 0.0):
                avg = float(no.average_filled_price or ask)
                filled = float(no.filled_size or 0.0)
                if filled <= 0:
                    _emit(db, sess, "live_error", {"reason": "zero_fill"})
                    _safe_transition(db, sess, STATE_LIVE_ERROR)
                    db.flush()
                    return {"ok": False, "error": "zero_fill"}
                le["position"] = {
                    "product_id": product_id,
                    "side": "long",
                    "quantity": filled,
                    "original_quantity": filled,
                    "avg_entry_price": avg,
                    "notional_usd": filled * avg,
                    "opened_at_utc": _utcnow().isoformat(),
                    "high_water_mark": avg,
                    "stop_price": None,
                    "target_price": None,
                }
                regime = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
                _stop_atr_mult = float(params["stop_atr_mult"])
                # Reuse the vol-floored ATR frozen at sizing time so the ACTUAL stop
                # matches the stop the qty was risk-sized against (else a wider stop
                # would over-risk, a narrower one would re-introduce the shake-out).
                atrp = le.get("entry_stop_atr_pct")
                if not atrp or float(atrp) <= 0:
                    atrp = effective_stop_atr_pct(
                        regime_atr_pct(regime), _expected_move_bps,
                        stop_atr_mult=_stop_atr_mult, vol_floor_mult=_stop_vol_floor_mult(),
                    )
                stop_px, target_px = stop_target_prices(
                    avg,
                    atr_pct=float(atrp),
                    side_long=True,
                    stop_atr_mult=_stop_atr_mult,
                    target_atr_mult=float(params["target_atr_mult"]),
                    reward_risk=class_aware_reward_risk(sess.symbol),
                    realized_high=_float_or_none(le.get("entry_realized_high")),
                )
                le["position"]["stop_price"] = stop_px
                le["position"]["target_price"] = target_px
                le["admission_viability_score"] = float(via.viability_score or 0)
                _mark_entry_order_resolved(le, le.get("entry_order_id"), "adopted")
                _commit_le(sess, le)
                if le.get("entry_decision_packet_id"):
                    try:
                        mark_packet_executed(db, int(le["entry_decision_packet_id"]))
                    except Exception:
                        _log.debug("mark_packet_executed live skipped session=%s", sess.id, exc_info=True)
                # Fee truth (2026-06-13): book the broker-reported entry
                # commission and carry it on the session until the full exit
                # nets it out of realized PnL.
                _entry_fee = _order_total_fees_usd(no) or 0.0
                if _entry_fee > 0.0:
                    le["entry_fee_usd_unbooked"] = (
                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _entry_fee
                    )
                    _commit_le(sess, le)
                _record_live_entry_ledger_safe(
                    db, sess, le=le, quantity=filled, fill_price=avg, fee=_entry_fee,
                )
                # FILL_OUTCOME_LOG (mig308) — Hook A: entry fill. broker_confirmed (we
                # polled the real order object `no`). Carries the REAL decision-time
                # spread (stashed at submit) + the entry L2 snapshot so the replay can
                # reproduce the live fill. realized_pnl is None on entry.
                _entry_intended = _float_or_none(le.get("entry_limit_price"))
                _record_fill_outcome_safe(
                    db,
                    sess,
                    side="entry",
                    fill_source="broker_confirmed",
                    broker_order_id=str(no.order_id) if getattr(no, "order_id", None) else None,
                    fill_price=float(avg),
                    qty=float(filled),
                    fees_usd=_entry_fee,
                    order_status=getattr(no, "status", None),
                    intended_price=_entry_intended,
                    spread_bps_at_decision=_float_or_none(le.get("entry_spread_bps_at_decision")),
                    entry_price=None,
                    exit_reason=None,
                    realized_pnl_usd=None,
                    entry_l2_snapshot=(
                        le.get("entry_l2_snapshot") if isinstance(le.get("entry_l2_snapshot"), dict) else None
                    ),
                    raw={"entry_fee_usd": _entry_fee, "filled_size": float(filled)},
                )
                # Sell INTO strength: rest the scale-out limit AT the target now,
                # while the move is paying the level (fail-open -> reactive path).
                _place_scale_out_limit(
                    db, sess, adapter, le=le, product_id=product_id,
                    target_px=float(target_px), filled=float(filled), prod=prod,
                )
                _safe_transition(db, sess, STATE_LIVE_ENTERED)
                _emit(
                    db,
                    sess,
                    "live_entry_filled",
                    {
                        "order_id": no.order_id,
                        "avg": avg,
                        "filled_size": filled,
                    },
                )
                # ENTRY-FEATURE CAPTURE (2026-06-23): record the lookahead-free entry-moment
                # feature vector onto `le` for the winner/loser META-LABEL dataset (mirrors the
                # paper path; today entry features are PAPER-ONLY so live has none). POST-
                # transition + best-effort -> can NEVER affect the fill or management. Uses the
                # SHARED helper (parity with replay). Flag chili_momentum_live_capture_features.
                if bool(getattr(settings, "chili_momentum_live_capture_features", True)):
                    try:
                        fetch_ohlcv_df = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)
                        from .entry_features import capture_entry_features, macro_regime_features

                        _cap_df = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
                        if _cap_df is not None and len(_cap_df):
                            try:  # 5d frame -> slice to TODAY so session_vwap anchors correctly
                                _ld = _cap_df.index[-1].date()
                                _cap_df = _cap_df[_cap_df.index.date == _ld]
                            except Exception:
                                pass
                        _ef = capture_entry_features(
                            sess.symbol, fill_px=float(avg), stop=float(stop_px),
                            target=float(target_px), qty=float(filled),
                            want_qty=float(le.get("entry_want_qty") or filled),
                            spread_bps=float(le.get("entry_spread_bps_at_decision") or 0.0),
                            atr_pct=float(regime_atr_pct(regime) or 0.0),
                            stop_atr_pct_eff=float(atrp or 0.0),
                            mid=float(avg),
                            dollar_vol=(float(le["entry_dollar_vol"]) if le.get("entry_dollar_vol") else None),
                            liq_mult=float(le.get("entry_liq_mult") or 1.0),
                            fire_ts=_utcnow(), entry_fidelity="live",
                            trigger_debug=(le.get("entry_trigger_debug") if isinstance(le.get("entry_trigger_debug"), dict) else None),
                            session_df=_cap_df, l2_db=db, l2_as_of=None,
                            macro=macro_regime_features(),
                        )
                        le["entry_regime_snapshot_json"] = dict(regime)
                        if _ef:
                            le["entry_features"] = _ef
                            # Log the meta-label's EMITTED (p, de_rate) on the AUTHORITATIVE entry
                            # features -> flows into the outcome snapshot so the self-critic can later
                            # check CALIBRATION decay + output-de-rate shift (priority-1 sizer self-
                            # monitoring, wf_a7af66e3). Stored in the snapshot dict (sibling to
                            # features, NOT inside it -> never a leakage feature). Best-effort.
                            try:
                                from .meta_label import load_model, score_probability, size_multiplier

                                _ml = load_model("/app/data/_meta_label_model.json")
                                if _ml and _ml.get("status") == "trained":
                                    _pp = score_probability(_ef, _ml)
                                    _dr = size_multiplier(_ef, _ml, floor=float(getattr(
                                        settings, "chili_momentum_meta_label_min_size", 0.4)))
                                    le["entry_regime_snapshot_json"]["meta_label_emit"] = {
                                        "p": (round(float(_pp), 5) if _pp is not None else None),
                                        "de_rate": round(float(_dr), 4),
                                        "conf": round(float(_ml.get("confidence") or 0.0), 4),
                                    }
                            except Exception:
                                pass
                        _commit_le(sess, le)
                    except Exception:
                        _log.debug("entry-feature capture skipped session=%s", sess.id, exc_info=True)
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            if no and _order_open(no):
                # C3: EVENT-DRIVEN pending-entry lifecycle (operator 2026-06-11:
                # "the right question is not how many seconds — it is what
                # INVALIDATES the order"). Ross cancels when the setup dies, not
                # when a clock expires. Three triggers, all funneling into the
                # same race-guarded cancel sequence below:
                #   1. INVALIDATION — the live bid broke the structural stop:
                #      the trigger's premise is gone; a fill now would be an
                #      instant bailout. Event-driven (rides the 15s loop AND the
                #      LiveRunnerLoop pending-entry fast ticks).
                #   2. RUNAWAY — the live bid is ABOVE our buy limit: the move
                #      left without us and the order can only fill on the way
                #      back down (adverse selection). Re-watch decides the chase
                #      with a fresh limit.
                #   3. BACKSTOP — the order must not outlive the bar evidence
                #      that produced it: N entry-interval bars (knob in BARS,
                #      like breakout_bailout_max_bars — no free seconds). This
                #      also outwaits RH's "unconfirmed" review latency by
                #      construction (2 bars @1m = 120s >> the ~13s review that
                #      killed the CPSH/SNDG submits at the old 10s window).
                try:
                    _ptick, _pfr = adapter.get_best_bid_ask(product_id)
                    _pbid = float(_ptick.bid) if (_ptick is not None and _ptick.bid) else None
                    _pask = float(_ptick.ask) if (_ptick is not None and _ptick.ask) else None
                except Exception:
                    _pbid = None
                    _pask = None
                    _pfr = None
                _stop_px = float(le.get("structural_stop_price") or 0.0)
                try:
                    _lim_px = float(le.get("entry_limit_price") or 0.0)
                except (TypeError, ValueError):
                    _lim_px = 0.0
                submit_raw = le.get("entry_submit_utc")
                if submit_raw:
                    try:
                        t_sub = datetime.fromisoformat(str(submit_raw).replace("Z", "+00:00")).replace(tzinfo=None)
                        _emb = le.get("entry_expected_move_bps")
                        _chase_ceiling = _entry_chase_ceiling_px(
                            limit_px=_lim_px,
                            expected_move_bps=(float(_emb) if _emb else None),
                        )
                        _cancel_why = _pending_entry_cancel_reason(
                            bid=_pbid,
                            structural_stop=_stop_px,
                            limit_px=_lim_px,
                            elapsed_s=(_utcnow() - t_sub).total_seconds(),
                            rest_bars=float(getattr(
                                settings, "chili_momentum_entry_max_rest_bars", 2.0) or 2.0),
                            interval_s=_entry_interval_seconds(),
                            chase_ceiling_px=_chase_ceiling,
                        )
                        # ── FIX B — AGGRESSIVE REPEG/CROSS ON FAST PUSHES ───────────
                        # The bid-based cancel reasons above MISS the most Ross-like
                        # entry: a fast vertical where the ASK climbs THROUGH our
                        # resting limit first (we stop being marketable) while the bid
                        # lags below the chase ceiling — the order then sits unfilled
                        # until the ~2-bar rest-backstop fires, after the move is gone
                        # (SKYQ 56 submitted / 0 filled, test w0av0u3qy). When FIX B is
                        # enabled AND the bid-based reasons did NOT already fire a HARDER
                        # reason (stop-breach / bid-left-behind / bar-backstop are all
                        # respected — we only fill the None gap) AND the live ask has
                        # advanced past our limit by more than the adaptive band, PROMOTE
                        # the reason to `entry_limit_left_behind` so it flows through the
                        # EXACT SAME repeg machinery below (cancel-before-replace phantom-2x
                        # guard, cumulative-spread ceiling, risk-first resize, max_repegs,
                        # equity gate, fresh-quote gate). The cross is bounded by
                        # _entry_repeg_price's cumulative ceiling, so R:R can't erode.
                        # Counterfactual emit (always, even when it can't act) so the
                        # operator reads the submit/fill-rate delta and can flip OFF.
                        _fixb_on = bool(getattr(settings, "chili_momentum_runaway_cross_enabled", True))
                        _fixb_eligible_equity = not str(sess.symbol or "").upper().endswith("-USD")
                        _fixb_ask_adv = _ask_advanced_past_limit(
                            ask=_pask,
                            limit_px=_lim_px,
                            expected_move_bps=(float(_emb) if _emb else None),
                        )
                        if (
                            _fixb_on
                            and _cancel_why is None
                            and _fixb_eligible_equity
                            and _fixb_ask_adv
                            and bool(getattr(settings, "chili_momentum_entry_chase_enabled", True))
                            and int(le.get("entry_repeg_count", 0) or 0)
                                < int(getattr(settings, "chili_momentum_entry_max_repegs", 3) or 0)
                            and _pfr is not None and is_fresh_enough(_pfr)
                        ):
                            # Confirm the cross is actually reachable within the cumulative
                            # budget BEFORE committing to a cancel — if the ask already ran
                            # past the ceiling, escalating would only cancel+abandon (worse
                            # than letting the rest-backstop / re-watch handle it). Fail-
                            # closed: only promote when _entry_repeg_price yields a usable px.
                            # ── DEEP FILL-AGGRESSION UNLOCK (the standing fill-on-verticals
                            # fix): the deep 800bps chase budget is now DECOUPLED from the
                            # halt-gate. It unlocks on EITHER (a) a recent halt-resume, OR (b)
                            # a CONFIRMED no-halt UP-thrust — a genuine 1m new-high vertical
                            # that never halted. The no-halt unlock is FAIL-CLOSED + KNIFE-
                            # GUARDED (_nohalt_vertical_thrust_strong): it requires live OFI>0
                            # (buyers lifting) AND the live ask making a NEW HIGH above the
                            # breakout level (ask being eaten up) AND above-VWAP-or-reclaiming
                            # AND RVOL above the explosive floor. A fade / below-VWAP / OFI<=0
                            # / non-new-high move ⇒ _nohalt_strong False ⇒ abs-cap holds (no
                            # blind chase). The live OFI is the SAME cheap 15s-window read the
                            # entry flow-veto uses (no new heavy fetch); new-high reuses the
                            # already-stamped breakout level; above-VWAP + RVOL are stamped at
                            # submit. The chased price is STILL risk-first re-sized (fewer
                            # shares at the worse price, dollar-risk unchanged) + bounded by
                            # the #769 max-loss circuit. Else None ⇒ abs-cap (parity).
                            _halt_active = _halt_resume_cooldown_active(le)
                            _nohalt_ofi = None
                            try:
                                from .pipeline import _live_ofi_microprice as _vc_ofi_fn
                                _nohalt_ofi, _ = _vc_ofi_fn(sess.symbol, db=db)
                                _nohalt_ofi = None if _nohalt_ofi is None else float(_nohalt_ofi)
                            except Exception:
                                _nohalt_ofi = None
                            # New-high: the live ask is ABOVE the stamped breakout level (the
                            # offer is being lifted past the trigger high — price progressing
                            # UP, not fading back). Fail-closed: no level / ask ⇒ not a new high.
                            _nohalt_bk = _float_or_none(le.get("breakout_level_price"))
                            _nohalt_new_high = bool(
                                _nohalt_bk is not None and _nohalt_bk > 0
                                and _pask is not None and float(_pask) > _nohalt_bk
                            )
                            _nohalt_above_vwap = le.get("entry_above_vwap")
                            _nohalt_strong = _nohalt_vertical_thrust_strong(
                                ofi=_nohalt_ofi,
                                new_high=_nohalt_new_high,
                                above_vwap=(
                                    bool(_nohalt_above_vwap)
                                    if _nohalt_above_vwap is not None else None
                                ),
                                rvol=le.get("entry_rvol"),
                            )
                            _vc = _vertical_thrust_confluence(
                                halt_resume_active=_halt_active,
                                tape_thrust_ok=_fixb_ask_adv,
                                squeeze_pct=le.get("entry_squeeze_pct"),
                                rvol=le.get("entry_rvol"),
                                nohalt_thrust_strong=_nohalt_strong,
                            )
                            # Unlock reason for the chase event + the repeg loop (halt vouch
                            # wins when both hold; else the no-halt thrust; else neither).
                            _vc_unlock = (
                                "halt_resume" if (_vc is not None and _halt_active)
                                else "no_halt_thrust" if (_vc is not None and _nohalt_strong)
                                else "none"
                            )
                            _fixb_rp = _entry_repeg_price(
                                original_limit_px=float(le.get("entry_original_limit_px") or _lim_px or 0.0),
                                live_ask=float(_pask),
                                expected_move_bps=(float(_emb) if _emb else None),
                                vertical_confluence=_vc,
                            )
                            if _fixb_rp and _fixb_rp > _lim_px:
                                # Stash the unlocked confluence so the INLINE repeg loop below
                                # prices the placed order with the SAME deep ceiling (else the
                                # loop defaults vertical_confluence=None ⇒ abs-cap ⇒ the deep
                                # budget would never reach the actual fill). Cleared after use.
                                le["entry_vertical_confluence"] = (
                                    None if _vc is None else float(_vc)
                                )
                                _emit(db, sess, "entry_runaway_cross_triggered", {
                                    "fix": "B", "bid": _pbid, "ask": _pask,
                                    "limit": _lim_px or None,
                                    "chase_ceiling": _chase_ceiling,
                                    "repeg_target": _fixb_rp,
                                    "repeg_count": int(le.get("entry_repeg_count", 0) or 0),
                                    "vertical_confluence": (None if _vc is None else round(float(_vc), 4)),
                                    "vertical_unlock": _vc_unlock,
                                    "nohalt_ofi": (None if _nohalt_ofi is None else round(_nohalt_ofi, 4)),
                                    "nohalt_new_high": _nohalt_new_high,
                                    "nohalt_above_vwap": (
                                        None if _nohalt_above_vwap is None else bool(_nohalt_above_vwap)
                                    ),
                                    "nohalt_strong": _nohalt_strong,
                                })
                                _cancel_why = "entry_limit_left_behind"
                            else:
                                # Ask advanced but the cross would exceed the cumulative
                                # spread budget — log the counterfactual, do NOT escalate
                                # (the move left for good; the existing re-watch handles it).
                                _emit(db, sess, "entry_runaway_cross_skipped", {
                                    "fix": "B", "reason": "past_cumulative_ceiling",
                                    "bid": _pbid, "ask": _pask, "limit": _lim_px or None,
                                })
                        elif _fixb_on and _cancel_why is None and _fixb_ask_adv:
                            # Eligible signal but a hard precondition blocked the escalation
                            # (crypto / repeg budget exhausted / stale quote). Counterfactual
                            # so the operator can see how often FIX B WOULD have fired.
                            _emit(db, sess, "entry_runaway_cross_blocked", {
                                "fix": "B",
                                "equity": _fixb_eligible_equity,
                                "repeg_count": int(le.get("entry_repeg_count", 0) or 0),
                                "fresh": bool(_pfr is not None and is_fresh_enough(_pfr)),
                                "bid": _pbid, "ask": _pask, "limit": _lim_px or None,
                            })
                        if _cancel_why is not None:
                            # RACE GUARD: the order may have FILLED between the 10s
                            # ack timeout and this (<=30s-cadence) tick — illiquid
                            # small-caps fill slowly (resting limit). Re-fetch FRESH
                            # before abandoning: a filled order abandoned here is
                            # ORPHANED — it loses the lane's tight exit management and
                            # falls to g2's far structural stop. [CTNT 2026-06-09:
                            # filled @21s, ack-timeout tick @22.9s -> orphaned -> -$283.]
                            # If it filled, leave the session pending so the entry
                            # fill-handler above ADOPTS it next tick; only cancel +
                            # re-watch a genuinely-still-open order. docs/DESIGN/MOMENTUM_LANE.md
                            _fresh, _ = _governed_get_order(adapter, le["entry_order_id"], sess=sess)
                            # Adopt the moment ANY size is filled — a partial on a
                            # marketable limit means we are AT THE FRONT and the rest is
                            # in flight; cancelling now orphans the clip. Matches the
                            # late-fill sweep predicate (`_order_done_for_entry(no) or
                            # filled_size > 0`). [BATL/CTNT/SDOT orphan fix.]
                            if _fresh and (
                                float(getattr(_fresh, "filled_size", 0) or 0.0) > 0.0
                                or _order_done_for_entry(_fresh)
                            ):
                                db.flush()
                                return {
                                    "ok": True, "session_id": sess.id,
                                    "state": sess.state, "pending": "ack_timeout_filled_adopt",
                                }
                            adapter.cancel_order(str(le["entry_order_id"]))
                            # The CANCEL ITSELF can lose the race on a slow small-cap: the
                            # order can fill before/despite the cancel landing. Re-fetch ONCE
                            # MORE after cancelling and ADOPT a filled order — including a
                            # cancelled-but-filled (``_order_done_for_entry`` treats
                            # filled_size>0 + cancelled as done) — rather than abandoning a
                            # real position to an UNMANAGED orphan (no lane stop). Leave the
                            # session PENDING so the fill-handler above adopts it next tick.
                            # [SDOT 2026-06-10: 56sh / $1,608 filled while the ack-timeout
                            # cancel raced -> orphaned, operator had to exit it by hand.]
                            _post, _ = _governed_get_order(adapter, le["entry_order_id"], sess=sess)
                            if _post and _order_done_for_entry(_post):
                                db.flush()
                                return {
                                    "ok": True, "session_id": sess.id, "state": sess.state,
                                    "pending": "ack_timeout_cancel_raced_fill_adopt",
                                }
                            if _post and _order_open(_post):
                                # CANCEL NOT CONFIRMED (2026-06-11 INDP): RH left the
                                # order OPEN after our cancel and it filled minutes
                                # later, unmanaged. Never walk away from a live order —
                                # keep the session PENDING with the pointer intact; the
                                # next tick re-checks (adopts fills / retries cancel).
                                _emit(db, sess, "entry_cancel_unconfirmed", {
                                    "order_id": str(le["entry_order_id"]),
                                    "reason": _cancel_why,
                                    "venue_status": _post.status,
                                    "filled_size": float(_post.filled_size or 0.0),
                                })
                                db.flush()
                                return {
                                    "ok": True, "session_id": sess.id, "state": sess.state,
                                    "pending": "cancel_unconfirmed",
                                }
                            # UNKNOWN post-cancel state (get_order failed / not-found): NEVER
                            # place a second order while the old order's fate is indeterminate
                            # (naked double-long guard). Keep PENDING with the pointer intact;
                            # the next tick re-checks venue truth. [G1 review #4]
                            if _post is None:
                                _emit(db, sess, "entry_cancel_indeterminate", {
                                    "order_id": str(le.get("entry_order_id")),
                                    "reason": _cancel_why,
                                })
                                db.flush()
                                return {
                                    "ok": True, "session_id": sess.id, "state": sess.state,
                                    "pending": "cancel_indeterminate",
                                }
                            # MARKETABLE RE-PEG (2026-06-22 G1): a left-behind RUNAWAY is the
                            # Ross play, not a miss. Reaching here, _post is a CONFIRMED
                            # terminal-cancelled order (not done, not open, not None) -> the OLD
                            # order is definitively gone -> cancel-and-replace the resting buy UP
                            # to the live ask instead of abandoning it. SAFE (red-teamed):
                            # EQUITY-ONLY (asset-class gated, crypto -USD NEVER chased),
                            # fail-CLOSED on a stale quote, price bounded by the CUMULATIVE spread
                            # budget off the original limit (R:R + thin-book sweep), RISK-FIRST
                            # re-sized (honors max_loss, not notional-only), DAY tif (never GTC),
                            # the old order marked resolved (no orphan), capped at max_repegs. Any
                            # miss -> the cancel+re-watch below.
                            _rp_n = int(le.get("entry_repeg_count", 0) or 0)
                            _rp_max = int(getattr(settings, "chili_momentum_entry_max_repegs", 3) or 0)
                            _rp_is_equity = not str(sess.symbol or "").upper().endswith("-USD")
                            # CHUNK 3-A — INLINE MICRO-REPEG. When enabled, the bounded
                            # repegs run WITHIN this tick (loop below) instead of one repeg
                            # per external WS tick — "3 repegs over 6–45s" -> "3 over ~3
                            # RTTs." EVERY existing bound is preserved and re-evaluated each
                            # iteration: the cumulative-spread ceiling (_entry_repeg_price),
                            # the risk-first re-size, the max-repeg counter (_rp_n<_rp_max),
                            # the equity gate (_rp_is_equity), the fresh-quote gate. The
                            # aggressiveness scales with repeg_index/max (first sits at the
                            # guarded ask via the live-ask read, later iters lean toward the
                            # ceiling but NEVER past it — _entry_repeg_price caps at the
                            # cumulative budget). Inter-repeg delay is adaptive: min(measured
                            # rail RTT, the inline max-delay ceiling). Flag OFF ⇒ the loop
                            # body runs AT MOST ONCE and returns -> the current one-repeg-per-
                            # tick behavior (byte-identical). _local ask/fresh re-read each
                            # iter (seeded from the cancel-time _pask/_pfr).
                            _inline = bool(getattr(settings, "chili_momentum_entry_inline_repeg_enabled", True))
                            _rp_ask = _pask
                            _rp_fr = _pfr
                            _rp_iters = 0
                            _rp_did = False
                            # DEEP-BUDGET HANDOFF (the fill-on-verticals fix): the FIX-B
                            # escalation above stashed the unlocked thrust confluence (halt-
                            # resume OR confirmed no-halt UP-thrust) so the ACTUAL placed
                            # repeg prices off the SAME raised ceiling. Without this the loop
                            # defaulted vertical_confluence=None ⇒ the abs-cap ⇒ the deep
                            # budget would never reach the fill (latent: even the halt path
                            # never applied it to the real order). Consume + clear so it can't
                            # leak into a later non-vertical repeg. None ⇒ abs-cap (parity).
                            _rp_vc = le.pop("entry_vertical_confluence", None)
                            try:
                                _rp_vc = None if _rp_vc is None else float(_rp_vc)
                            except (TypeError, ValueError):
                                _rp_vc = None
                            while (
                                _cancel_why == "entry_limit_left_behind"
                                and bool(getattr(settings, "chili_momentum_entry_chase_enabled", True))
                                and _rp_is_equity
                                and _rp_n < _rp_max
                                and _rp_ask and _rp_ask > 0
                                and _rp_fr is not None and is_fresh_enough(_rp_fr)
                            ):
                                _rp_new = _entry_repeg_price(
                                    original_limit_px=float(le.get("entry_original_limit_px") or _lim_px or 0.0),
                                    live_ask=float(_rp_ask),
                                    expected_move_bps=(float(_emb) if _emb else None),
                                    vertical_confluence=_rp_vc,
                                )
                                _rp_maxn = float((le.get("entry_notional_guard") or {}).get("max_notional_usd") or 0.0)
                                if not (_rp_new and _rp_new > _lim_px and _rp_maxn > 0):
                                    break  # ran past the cumulative ceiling -> stop chasing
                                # RISK-FIRST re-size at the chased price (notional is the
                                # CEILING only) so a chase can't over-risk past max_loss.
                                # [G1 review #2]
                                _rb = le.get("entry_resize_basis") or {}
                                _rp_qty, _ = compute_risk_first_quantity(
                                    entry_price=_rp_new,
                                    atr_pct=float(_rb.get("atr_pct") or 0.0),
                                    max_loss_usd=float(_rb.get("max_loss_usd") or 0.0),
                                    max_notional_ceiling_usd=_rp_maxn,
                                    base_increment=float(_rb.get("base_increment") or 1.0),
                                    base_min_size=float(_rb.get("base_min_size") or 1.0),
                                    stop_atr_mult=float(_rb.get("stop_atr_mult") or 0.60),
                                )
                                if not (_rp_qty and _rp_qty >= 1.0):
                                    break
                                _rp_old_eid = le.get("entry_order_id")
                                # DUPLICATE-FILL GUARD (2026-06-27): the PRE-loop cancel
                                # at the top of this branch only retired the ORIGINAL
                                # order (O0). On EVERY subsequent inline iteration the
                                # prior repeg (O1, O2, ...) is STILL a live resting order
                                # at the broker — placing the next repeg WITHOUT cancelling
                                # it first leaves two live buy orders that can BOTH fill
                                # (phantom 2x long). So before each subsequent place,
                                # CANCEL the current `le["entry_order_id"]` and CONFIRM it
                                # terminal-cancelled, re-using the same race-guard the
                                # per-tick path uses: adopt a fill, never place a second
                                # live order while the prior repeg's fate is indeterminate.
                                # `_rp_did` is True only AFTER the first repeg placed, so on
                                # the first iteration (O0 already confirmed-cancelled above)
                                # this block is skipped -> byte-identical to the legacy
                                # one-repeg-per-tick path.
                                if _rp_did:
                                    try:
                                        adapter.cancel_order(str(_rp_old_eid))
                                    except Exception:
                                        _log.debug(
                                            "[momentum_s4] inline repeg pre-place cancel failed oid=%s",
                                            _rp_old_eid, exc_info=True,
                                        )
                                    _rp_post, _ = _governed_get_order(
                                        adapter, _rp_old_eid, sess=sess
                                    )
                                    if _rp_post and (
                                        _order_done_for_entry(_rp_post)
                                        or float(getattr(_rp_post, "filled_size", 0) or 0.0) > 0.0
                                    ):
                                        # The prior repeg filled during the cancel race —
                                        # adopt it next tick (pointer intact), never place
                                        # a second order on top of a real position.
                                        _commit_le(sess, le)
                                        db.flush()
                                        return {
                                            "ok": True, "session_id": sess.id,
                                            "state": sess.state,
                                            "pending": "inline_repeg_cancel_raced_fill_adopt",
                                        }
                                    if _rp_post is None or _order_open(_rp_post):
                                        # Cancel NOT confirmed terminal (still open) OR the
                                        # fetch failed (indeterminate): NEVER place a second
                                        # live order while the prior repeg may still fill
                                        # (naked-double-long guard). Stop chasing; keep the
                                        # session PENDING with the pointer intact so the
                                        # next tick re-checks venue truth (adopt / retry).
                                        _emit(db, sess, "entry_repeg_cancel_unconfirmed", {
                                            "order_id": str(_rp_old_eid),
                                            "venue_status": getattr(_rp_post, "status", None),
                                        })
                                        _commit_le(sess, le)
                                        db.flush()
                                        return {
                                            "ok": True, "session_id": sess.id,
                                            "state": sess.state,
                                            "pending": "inline_repeg_cancel_unconfirmed",
                                        }
                                    # else: _rp_post confirmed terminal-cancelled -> safe to
                                    # place the replacement below (old order definitively gone).
                                _rp_pn = int(le.get("entry_place_count", 0) or 0) + 1
                                le["entry_place_count"] = _rp_pn
                                # Same recycle-unique cid shape as the first entry (fold the
                                # persistent trade_cycles/stopout_cycles) so an inline re-peg
                                # after a recycle cannot collide with a prior cycle's cid.
                                _rp_cid = _entry_client_order_id(
                                    session_id=sess.id,
                                    correlation_id=sess.correlation_id,
                                    trade_cycles=int(le.get("trade_cycles") or 0),
                                    stopout_cycles=int(le.get("stopout_cycles") or 0),
                                    place_n=_rp_pn,
                                )
                                _rp_res = _governed_place(
                                    adapter, adapter.place_limit_order_gtc, sess=sess,
                                    product_id=product_id,
                                    side="buy",
                                    base_size=_fmt_base_size(_rp_qty),
                                    limit_price=_fmt_limit_price_buy(_rp_new),
                                    client_order_id=_rp_cid,
                                    time_in_force="gfd",
                                    extended_hours=bool(le.get("entry_session_extended")),
                                ) or {}
                                if not _rp_res.get("ok"):
                                    # Governor DEFER or a place reject: stop the inline loop;
                                    # the resting state is unchanged (old order already
                                    # cancelled above), so fall through to the cancel+re-watch
                                    # below — the next tick retries. Never a silent drop.
                                    break
                                # OLD order confirmed terminal-cancelled above -> mark
                                # resolved so it can never resurface as an orphan. [G1 #3]
                                _mark_entry_order_resolved(le, str(_rp_old_eid), "void")
                                _rp_old_limit = _lim_px  # capture BEFORE reassigning for the emit
                                le["entry_order_id"] = _rp_res.get("order_id")
                                le["entry_client_order_id"] = _rp_res.get("client_order_id") or _rp_cid
                                le["entry_limit_price"] = _fmt_limit_price_buy(_rp_new)
                                _rp_n = _rp_n + 1
                                le["entry_repeg_count"] = _rp_n
                                le["entry_submit_utc"] = _utcnow().isoformat()
                                le["entry_submitted"] = True
                                _record_entry_order_placed(le, _rp_res.get("order_id"))
                                _commit_le(sess, le)
                                _lim_px = _rp_new  # the new resting limit for the next iter
                                _rp_iters += 1
                                _rp_did = True
                                _emit(db, sess, "entry_repegged", {
                                    "old_limit": _rp_old_limit, "new_limit": _rp_new,
                                    "live_ask": _rp_ask, "qty": _rp_qty, "n": _rp_n,
                                    "inline": _inline, "iter": _rp_iters,
                                })
                                if not _inline:
                                    break  # one-repeg-per-tick (byte-identical legacy path)
                                # ── INLINE continuation: re-read the live quote and decide
                                # whether the move is STILL running away (left-behind) within
                                # this tick. Adaptive inter-repeg delay = min(measured RTT,
                                # the inline max-delay ceiling) — no fixed clock. Re-check the
                                # cancel reason against the FRESH bid so we never chase a name
                                # whose bid has fallen back through our (now higher) limit.
                                import time as _t_inline
                                _delay = float(getattr(settings, "chili_momentum_entry_inline_repeg_max_delay_s", 0.75) or 0.75)
                                _rtt = _measured_rail_rtt_s()
                                if _rtt is not None and _rtt > 0:
                                    _delay = min(_delay, _rtt)
                                if _delay > 0:
                                    _t_inline.sleep(_delay)
                                try:
                                    _itick, _rp_fr = adapter.get_best_bid_ask(product_id)
                                    _rp_bid = float(_itick.bid) if (_itick is not None and _itick.bid) else None
                                    _rp_ask = float(_itick.ask) if (_itick is not None and _itick.ask) else None
                                except Exception:
                                    break  # stale/failed quote -> stop (fail-closed)
                                # Recompute the cancel reason off the fresh quote: only keep
                                # looping while STILL left-behind (bid above our new limit).
                                _chase_ceiling2 = _entry_chase_ceiling_px(
                                    limit_px=_lim_px,
                                    expected_move_bps=(float(_emb) if _emb else None),
                                )
                                _cancel_why = _pending_entry_cancel_reason(
                                    bid=_rp_bid,
                                    structural_stop=_stop_px,
                                    limit_px=_lim_px,
                                    elapsed_s=0.0,  # within-tick: bar backstop n/a here
                                    rest_bars=float(getattr(
                                        settings, "chili_momentum_entry_max_rest_bars", 2.0) or 2.0),
                                    interval_s=_entry_interval_seconds(),
                                    chase_ceiling_px=_chase_ceiling2,
                                )
                                # FIX B continuation: a SUSTAINED vertical can keep the
                                # ask climbing through each fresh limit while the bid
                                # still lags below the chase ceiling. Keep the within-tick
                                # loop running on the ask-advance signal too (same adaptive
                                # band, same equity/freshness/repeg-budget bounds already
                                # re-evaluated by the while-guard; cross still capped by
                                # _entry_repeg_price). Never overrides a HARDER bid reason
                                # (stop-breach / bid-left-behind) — only fills the None gap.
                                if (
                                    _cancel_why is None
                                    and bool(getattr(settings, "chili_momentum_runaway_cross_enabled", True))
                                    and _ask_advanced_past_limit(
                                        ask=_rp_ask,
                                        limit_px=_lim_px,
                                        expected_move_bps=(float(_emb) if _emb else None),
                                    )
                                ):
                                    _cancel_why = "entry_limit_left_behind"
                            if _rp_did:
                                if _rp_iters > 1:
                                    _log.info(
                                        "[momentum_s4] inline micro-repeg %s: %d repegs in one tick (n=%d/%d)",
                                        sess.symbol, _rp_iters, _rp_n, _rp_max,
                                    )
                                db.flush()
                                return {"ok": True, "session_id": sess.id, "state": sess.state, "pending": "entry_repegged", "repegs": _rp_iters}
                            _emit(db, sess, "entry_ack_timeout", {
                                "elapsed_sec": (_utcnow() - t_sub).total_seconds(),
                                "reason": _cancel_why,
                                "bid": _pbid, "limit": _lim_px or None,
                                "structural_stop": _stop_px or None,
                            })
                            _safe_transition(db, sess, STATE_WATCHING_LIVE)
                            le["entry_submitted"] = False
                            le["entry_order_id"] = None
                            _commit_le(sess, le)
                            db.flush()
                            return {"ok": True, "session_id": sess.id, "state": sess.state, "timeout": True}
                    except Exception:
                        pass
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "pending": "entry_open"}
            # NO CONFIRMATION OBTAINED (2026-06-27): `no is None` means the entry-confirm
            # poll produced no order object — either a governor DEFER (the shared rail
            # bucket was empty so every governed poll deferred) or a transient get_order
            # failure. The order's fate is UNKNOWN, NOT confirmed-bad: a healthy resting /
            # about-to-fill entry order must NOT be pushed to LIVE_ERROR and orphaned (no
            # lane stop). Stay PENDING with entry_submitted / entry_order_id INTACT and
            # retry next tick (mirrors the cancel_indeterminate / entry_open pending
            # returns). Only a REAL order object in a bad terminal state falls through to
            # LIVE_ERROR below.
            if no is None:
                _emit(db, sess, "entry_confirm_deferred", {
                    "order_id": str(le.get("entry_order_id")),
                })
                db.flush()
                return {
                    "ok": True, "session_id": sess.id, "state": sess.state,
                    "pending": "entry_confirm_deferred",
                }
            _emit(db, sess, "live_error", {"reason": "entry_order_state", "status": no.status if no else None})
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "bad_entry_order"}

        # Submit entry once (duplicate guard)
        if le.get("entry_submitted"):
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # P1.2 — venue health circuit breaker. Gate new entries when the
        # venue's rolling-window latency / error rate crosses the threshold.
        # Fires BEFORE autopilot_mutex because venue-sick is the more
        # fundamental "stop" signal. Fails open (flag off or exception →
        # healthy) so unwired environments behave unchanged. Falling back to
        # STATE_WATCHING_LIVE keeps the session alive for retry on the next
        # pulse once the venue recovers.
        try:
            from ..venue.venue_health import (
                is_venue_degraded,
                should_auto_switch_to_paper,
                venue_degraded_reason,
                canonicalize_venue,
            )
            _venue_key = canonicalize_venue(ef)
            if is_venue_degraded(db, venue=_venue_key):
                _reason = venue_degraded_reason(db, venue=_venue_key) or "unknown"
                _auto_paper = should_auto_switch_to_paper(db, venue=_venue_key)
                _emit(
                    db,
                    sess,
                    "live_entry_blocked_by_venue_degraded",
                    {
                        "venue": _venue_key,
                        "reason": _reason,
                        "auto_switch_to_paper": _auto_paper,
                    },
                )
                if _auto_paper:
                    # Flip to paper so the session stays productive instead
                    # of stalling. Paper mode writes no events so has no
                    # effect on the venue health signal — recovery detected
                    # via live events from other sessions / manual traffic.
                    try:
                        sess.mode = "paper"
                    except Exception:
                        pass
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
                db.flush()
                return {
                    "ok": True,
                    "session_id": sess.id,
                    "state": sess.state,
                    "blocked": True,
                    "reason": "venue_degraded",
                }
        except Exception:
            # Defensive: never let a venue-health failure stall the live
            # runner. Log and continue — the rate limiter + idempotency
            # store handle the worst-case retry scenarios.
            pass

        # P0.4 — autopilot mutual exclusion. Our own active session counts as
        # the lease holder (owner_self → allowed), so this only blocks when
        # an AutoTrader v1 live Trade is already open on the same symbol/user.
        gate = check_autopilot_entry_gate(
            db,
            candidate=AUTOPILOT_MOMENTUM_NEURAL,
            symbol=sess.symbol,
            user_id=sess.user_id,
        )
        if not gate.get("allowed"):
            _emit(
                db,
                sess,
                "live_entry_blocked_by_autopilot_mutex",
                {
                    "reason": gate.get("reason"),
                    "owner": gate.get("owner"),
                    "primary": gate.get("primary"),
                    "strict": gate.get("strict"),
                },
            )
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True,
                "session_id": sess.id,
                "state": sess.state,
                "blocked": True,
                "reason": "autopilot_mutex",
            }

        # P1.4 — runtime feature-parity assertion at entry. Fetches fresh
        # OHLCV and verifies the live indicator snapshot matches the
        # canonical compute_all_from_df output. Fails open on any error /
        # flag-off so unwired environments behave identically. Soft mode
        # records + alerts without blocking; hard mode blocks critical drift.
        #
        # CRITICAL: short-circuit on the feature flag BEFORE any imports or
        # OHLCV fetch. The flag defaults OFF, so every live session fires
        # through this block — without the pre-flag guard, unwired
        # environments pay a network fetch per entry attempt. On Windows the
        # test suite observed this exhausting the ephemeral socket pool
        # (WinError 10055) under the autopilot mutex regression runs.
        _parity_blocked_feature_parity = False
        if bool(getattr(settings, "chili_feature_parity_enabled", False)):
            try:
                from ..feature_parity import (
                    DEFAULT_FEATURES as _PARITY_FEATURES,
                    check_entry_feature_parity as _check_parity,
                )
                from ..indicator_core import compute_all_from_df as _pc_compute
                _pc_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)
                _pc_df = _pc_fetch(sess.symbol, "1h", "30d")
                if _pc_df is not None and not _pc_df.empty:
                    _pc_arrays = _pc_compute(_pc_df, needed=set(_PARITY_FEATURES))
                    _pc_live: dict[str, Any] = {}
                    for _k, _v in _pc_arrays.items():
                        if isinstance(_v, list) and _v and _v[-1] is not None:
                            _pc_live[_k] = _v[-1]
                    _pc_venue = "coinbase" if str(ef).lower() in ("crypto", "coinbase_spot", "coinbase") else "robinhood"
                    _pc_result = _check_parity(
                        db,
                        ticker=sess.symbol,
                        live_snap=_pc_live,
                        reference_df=_pc_df,
                        features=_PARITY_FEATURES,
                        source="momentum_neural",
                        scan_pattern_id=getattr(variant, "scan_pattern_id", None),
                        venue=_pc_venue,
                    )
                    if not _pc_result.ok:
                        _emit(
                            db,
                            sess,
                            "live_entry_blocked_by_feature_parity",
                            {
                                "severity": _pc_result.severity,
                                "mode": _pc_result.mode,
                                "n_mismatches": _pc_result.n_mismatches,
                                "reason": _pc_result.reason,
                                "record_id": _pc_result.record_id,
                            },
                        )
                        _safe_transition(db, sess, STATE_WATCHING_LIVE)
                        db.flush()
                        _parity_blocked_feature_parity = True
            except Exception:
                # Defensive: never let a parity-check failure stall the live
                # runner. The check is an observability net, not a safety gate.
                pass
        if _parity_blocked_feature_parity:
            return {
                "ok": True,
                "session_id": sess.id,
                "state": sess.state,
                "blocked": True,
                "reason": "feature_parity",
            }

        regime_live = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        ex_live = via.execution_readiness_json if isinstance(via.execution_readiness_json, dict) else {}
        try:
            spread_bps_live = float(ex_live.get("spread_bps") or 8.0)
        except (TypeError, ValueError):
            spread_bps_live = 8.0
        try:
            slip_ref = float(ex_live.get("slippage_estimate_bps") or 6.0)
        except (TypeError, ValueError):
            slip_ref = 6.0

        decision_packet_id = None
        _perf_size_mult = 1.0  # FIX-16: default 1.0 when the decision ledger path is disabled
        if bool(getattr(settings, "brain_enable_decision_ledger", True)):
            dec = run_momentum_entry_decision(
                db,
                session=sess,
                viability=via,
                variant=variant,
                user_id=sess.user_id,
                max_notional_policy=float(max_notional),
                quote_mid=mid,
                spread_bps=spread_bps_live,
                execution_mode="live",
                regime_snapshot=regime_live,
            )
            if not dec.get("proceed"):
                alloc = dec.get("allocation") or {}
                _emit(
                    db,
                    sess,
                    "live_entry_abstain",
                    {
                        "packet_id": dec.get("packet_id"),
                        "reason": alloc.get("abstain_reason_code"),
                        "detail": alloc.get("abstain_reason_text"),
                    },
                )
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "abstained": True}
            decision_packet_id = dec.get("packet_id")
            max_notional = min(float(max_notional), float(dec["allocation"]["recommended_notional"]))
            # FIX-16 (B3): in pure-liquidity-cap mode the allocator surfaces the variant-
            # performance multiplier (DOWN-only [0.3,1.0]) here instead of folding it into the
            # notional ceiling. Apply it ONCE to the per-trade RISK BUDGET below (under the same
            # base*3.0 clamp). Legacy mode surfaces 1.0 => no double-apply (byte-identical).
            try:
                _perf_size_mult = float((dec.get("allocation") or {}).get("performance_size_mult", 1.0) or 1.0)
            except (TypeError, ValueError):
                _perf_size_mult = 1.0
        if bool(getattr(settings, "brain_decision_packet_required_for_runners", True)) and decision_packet_id is None:
            _emit(db, sess, "live_error", {"reason": "decision_packet_required_missing"})
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "decision_packet_missing"}
        le["entry_decision_packet_id"] = decision_packet_id
        le["entry_slip_bps_ref"] = slip_ref
        _commit_le(sess, le)
        snap = dict(sess.risk_snapshot_json or {})
        le = _live_exec(snap)

        # Equity rejects FRACTIONAL shares on a LIMIT / extended-hours order (RH allows
        # fractional only on type=market + regular hours), and the momentum entry is a
        # marketable LIMIT often placed premarket. A transient get_product REST miss
        # (prod=None) must therefore default to WHOLE shares for equity (inc=mn=1.0) —
        # never a None that rounds to a fractional qty the venue rejects (a wasted live
        # entry). Crypto (-USD) keeps None (its venue quantizes fractional base size).
        _equity_share_default = not str(sess.symbol or "").upper().endswith("-USD")
        inc = prod.base_increment if prod else (1.0 if _equity_share_default else None)
        mn = prod.base_min_size if prod else (1.0 if _equity_share_default else None)
        guarded_ask = ask * _adaptive_notional_guard_multiplier(expected_move_bps=_expected_move_bps)
        # Risk-first sizing (Ross-style): qty = per-trade max-loss / stop distance,
        # capped at the (conviction-scaled, equity-relative) notional ceiling — a
        # tighter stop buys MORE size at constant risk. Falls back to notional-first
        # when ATR/inputs are unusable. (docs/DESIGN/MOMENTUM_LANE.md)
        _regime = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        _stop_atr_mult = float(params.get("stop_atr_mult") or 0.60)
        # WT-1 (thin-frame stop fallback — SAFETY): when the 15m frame is too thin to
        # compute a LIVE expected-move (_expected_move_bps is None) AND there is no
        # structural pullback-low stop, the stop-ATR below collapses to the regime
        # DEFAULT (~1.5%%) — a noise-tight stop on a low-float that gaps 5-9%% THROUGH it
        # (the -$697 MTEN/SDOT/CCTG/CAST tail). 3-state knob:
        #   off     -> no-op (size off the regime fallback, byte-identical).
        #   observe -> DEFAULT: byte-identical (still trades); emit the [momentum_wt1]
        #              would_abstain counter so the operator reads intraday firing freq.
        #   enforce -> ABSTAIN (no-data -> no-trade), re-watch (the freshness re-score may
        #              warm the frame) — mirrors the _enforce_ross_price_band fail-safe.
        # A real structural stop means the stop is NOT a blind regime guess, so the guard
        # only fires when BOTH the live expected-move is missing and no structural stop
        # exists. (docs/DESIGN/MOMENTUM_LANE.md)
        _wt1_mode = _require_live_atr_mode()
        if _wt1_mode != "off" and _expected_move_bps is None:
            try:
                _wt1_struct_stop = float(le.get("structural_stop_price") or 0.0)
            except (TypeError, ValueError):
                _wt1_struct_stop = 0.0
            if _wt1_struct_stop <= 0.0:
                _wt1_blob = {
                    "reason": "thin_frame_no_live_atr",
                    "mode": _wt1_mode,
                    "symbol": sess.symbol,
                    "regime_atr_pct_fallback": round(float(regime_atr_pct(_regime) or 0.0), 6),
                    "would_abstain": True,
                }
                _log.warning(
                    "[momentum_wt1] would_abstain=1 mode=%s symbol=%s regime_atr_pct_fallback=%.6f",
                    _wt1_mode, sess.symbol, float(regime_atr_pct(_regime) or 0.0),
                )
                _emit(db, sess, "live_wt1_thin_frame", _wt1_blob)
                if _wt1_mode == "enforce":
                    le["wt1_abstain"] = _wt1_blob
                    _commit_le(sess, le)
                    if sess.state in (
                        STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_PENDING_ENTRY
                    ) and not le.get("entry_submitted"):
                        _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {"ok": True, "blocked": True, "reason": "thin_frame_no_live_atr"}
                # observe: fall through (byte-identical), counter already emitted.
        # Vol-floored stop ATR-pct: never tighter than vol_floor_mult x the live
        # expected-move, so the stop sits OUTSIDE the intraday noise (the KAIO
        # shake-out fix). Frozen in le and reused at the post-fill stop so sizing
        # and the actual stop agree. (docs/DESIGN/MOMENTUM_LANE.md)
        _eff_atr_pct = effective_stop_atr_pct(
            regime_atr_pct(_regime), _expected_move_bps,
            stop_atr_mult=_stop_atr_mult, vol_floor_mult=_stop_vol_floor_mult(),
        )
        # Ross structural stop: if the pullback-break captured a pullback low, stop
        # just UNDER that structure instead of a noise-tight ATR — but never TIGHTER
        # than the vol floor (shake-out guard). Risk-first sizing then trims qty
        # against the wider, structure-aware distance (constant $risk); the 2:1
        # target auto-scales off the actual stop distance. Fix for the lane's
        # all-stop-out streak (every exit flagged stop_too_tight). MOMENTUM_LANE.md
        _eff_atr_pct, _stop_model = structural_or_vol_floored_atr_pct(
            vol_floored_atr_pct=_eff_atr_pct,
            structural_stop_price=le.get("structural_stop_price"),
            entry_price=guarded_ask,
            stop_atr_mult=_stop_atr_mult,
        )
        le["entry_stop_atr_pct"] = _eff_atr_pct
        le["entry_stop_model"] = _stop_model
        # DESIGN #3: stash the realized intraday HEADROOM (session high) + day-range so
        # the first-target R:R can adapt to the name's OWN proven travel at fill time,
        # and the scale-out fraction can tilt by realized vol. Reuses the 15m _entry_df
        # already fetched for the adaptive spread gate (no new I/O). Best-effort; absent
        # data leaves the keys unset -> adaptive helpers fall back to base (byte-identical).
        try:
            if _entry_df is not None and not getattr(_entry_df, "empty", True) and len(_entry_df) >= 2:
                _rh = float(_entry_df["High"].astype(float).max())
                _rl = float(_entry_df["Low"].astype(float).min())
                if math.isfinite(_rh) and _rh > 0:
                    le["entry_realized_high"] = _rh
                if math.isfinite(_rh) and math.isfinite(_rl) and _rl > 0 and _rh > _rl:
                    le["entry_day_range_pct"] = (_rh - _rl) / _rl
        except Exception:
            pass
        if _stop_model == "structural_pullback":
            le["structural_stop_atr_pct"] = round(_eff_atr_pct, 6)
        # LEVER 2B — snapshot the ENTRY tape PACE (ticks/sec) so the runner-time
        # velocity/persistence RIDE-LOCK can test whether the thrust is still being fed
        # (live tick_rate >= entry_tick_rate * persist_frac). Best-effort, fail-open: a
        # thin/missing entry tape leaves it unset ⇒ the persistence test is skipped (the
        # RIDE-LOCK falls back to flow-sign alone), byte-identical to flag-off there.
        if bool(getattr(settings, "chili_momentum_velocity_persistence_exit_enabled", True)):
            try:
                from .pipeline import _live_flow_slope as _vp_entry_flow

                _vp_e = _vp_entry_flow(sess.symbol, db=db)
                if _vp_e is not None and _vp_e.get("tick_rate") is not None:
                    le["entry_tick_rate"] = float(_vp_e["tick_rate"])
            except Exception:
                pass
        # Liquidity-ceiling (SCALING_ENGINE.md): never size beyond what the NAME can absorb
        # on EXIT (Ross's "can't move 500k shares in 1-2 min"). As the account COMPOUNDS the
        # equity notional cap grows, but this binds on thin names so CHILI scales only as far
        # as each name's liquidity allows — instead of a 15%-of-$1M notional that can't exit a
        # thin low-float. Best-effort dollar-volume; fail-OPEN (no data / crypto -> unchanged).
        try:
            from .universe import snapshot_dollar_volumes as _snap_dvol
            _dvol = (_snap_dvol([sess.symbol]) or {}).get(str(sess.symbol or "").strip().upper())
        except Exception:
            _dvol = None
        _max_notional_pre_liq = max_notional
        max_notional = liquidity_capped_notional(max_notional, _dvol)
        if max_notional < _max_notional_pre_liq - 1e-9:
            le["liquidity_cap"] = {
                "dollar_volume_usd": round(float(_dvol), 0) if _dvol else None,
                "pre_liq_notional_usd": round(_max_notional_pre_liq, 2),
                "capped_notional_usd": round(max_notional, 2),
            }
        # Crypto liquidity ceiling (A1): the dvol-snapshot path above is equity-only
        # (fails open for -USD), so crypto had no ceiling. Bind it to the crypto
        # turnover cap (fraction of one minute's $-volume) from the same floor the
        # arm gate uses. Fail-open: no turnover datum -> unchanged.
        if str(sess.symbol or "").upper().endswith("-USD"):
            try:
                from .crypto_liquidity import crypto_liquidity_ok as _liq

                _ok, _det, _cap = _liq(sess.symbol, via, adapter=None)
                if _cap is not None and float(_cap) > 0 and float(_cap) < max_notional - 1e-9:
                    le["crypto_liquidity_cap"] = {
                        "pre_cap_notional_usd": round(max_notional, 2),
                        "capped_notional_usd": round(float(_cap), 2),
                        "per_min_vol_usd": _det.get("per_min_vol_usd"),
                    }
                    max_notional = float(_cap)
            except Exception:
                pass
        # Streak-adaptive risk (Ross): the per-trade max loss scales with the
        # lane's recent live win rate — bigger on a hot hand, half-size when
        # cold or after 3 straight losses. Bounds [0.5, 1.5]; fail-neutral 1.0.
        from .risk_policy import (
            cushion_risk_multiplier,
            day_open_risk_ramp_multiplier,
            green_day_graduation_multiplier,
            prior_day_pnl_damper_multiplier,
            streak_risk_multiplier,
        )

        # Segregate the streak dial by THIS lane (ef = normalized execution_family,
        # resolved above) so a crypto/paper-twin loss never de-risks the equity lane.
        _streak_mult, _streak_meta = streak_risk_multiplier(db, execution_family=ef)
        # GREEN-DAY GRADUATION (default OFF ⇒ 1.0 byte-identical): graduate to bigger size
        # only after a consecutive green-day streak (auto-derived from realized daily PnL,
        # ET calendar). A bounded UPWARD multiplier — composes into the combined ceiling
        # below, NEVER a veto. Lane-segregated by ef (same as the streak dial).
        _graduation_mult, _graduation_meta = green_day_graduation_multiplier(
            db, execution_family=ef
        )
        _base_max_loss = policy_float_cap(
            caps, "max_loss_per_trade_usd", settings.chili_momentum_risk_max_loss_per_trade_usd
        )
        # TIER-2 OVERNIGHT max-loss cap: overnight has NO broker-side stop, so the per-trade
        # loss is bounded by the SOFTWARE circuit (C1/C1b every tick) sized off a TIGHTER cap
        # = max($50 irreducible base, 0.5% of overnight buying power) — equity-relative, not a
        # magic $50. This LOWERS _base_max_loss overnight (never raises it). Flag OFF / not
        # overnight => unchanged (byte-identical).
        if (
            getattr(settings, "chili_momentum_overnight_trading_enabled", False)
            and not str(sess.symbol or "").upper().endswith("-USD")
        ):
            try:
                from .market_profile import is_overnight_now as _is_overnight_now

                if _is_overnight_now(sess.symbol):
                    _ovn_floor = float(getattr(settings, "chili_momentum_risk_max_loss_per_trade_usd", 50.0) or 50.0)
                    _ovn_irreducible = 50.0
                    _ovn_pct = float(getattr(settings, "chili_momentum_overnight_max_loss_pct_bp", 0.5) or 0.0)
                    _ovn_bp = None
                    try:
                        from .risk_policy import _account_equity_usd as _ovn_equity

                        _ovn_bp = _ovn_equity(ef)
                    except Exception:
                        _ovn_bp = None
                    _ovn_cap = _ovn_irreducible
                    if _ovn_bp and _ovn_bp > 0 and _ovn_pct > 0:
                        _ovn_cap = max(_ovn_irreducible, float(_ovn_bp) * _ovn_pct / 100.0)
                    if _ovn_cap > 0 and _ovn_cap < float(_base_max_loss):
                        le["overnight_max_loss"] = {"cap_usd": round(_ovn_cap, 2), "bp_usd": _ovn_bp}
                        _base_max_loss = _ovn_cap
            except Exception:
                pass
        le["streak_risk"] = _streak_meta
        # GREEN-DAY GRADUATION meta (only stash when it actually graduated size up, to keep
        # the OFF path byte-identical in the live-entry blob). 1.0 ⇒ nothing recorded.
        if _graduation_mult > 1.0:
            le["green_day_graduation"] = _graduation_meta
        # Day-cushion ladder (Ross 06-11): start the day at half risk; earn the
        # right to full and then aggressive size from TODAY's banked P&L.
        _cushion_mult, _cushion_meta = cushion_risk_multiplier(
            db, base_loss_usd=float(_base_max_loss)
        )
        le["cushion_risk"] = _cushion_meta
        # PRIOR-DAY OUTLIER DAMPER (HVM101/SCAL101): after a statistically outlier prior
        # session (big win OR big loss, |PnL|/equity z-scored over a trailing daily window)
        # size DOWN the next session — an emotional/variance reset that reverts toward
        # baseline risk. Only ever <=1.0 (fail-NEUTRAL 1.0 on thin/degenerate/flag-off =>
        # byte-identical). Composes with the other size-down levers under the 3x clamp below.
        _prior_day_mult, _prior_day_meta = prior_day_pnl_damper_multiplier(db, execution_family=ef)
        if _prior_day_mult < 1.0:
            le["prior_day_pnl_damper"] = _prior_day_meta
        # FIX-17 DAY-OPEN RISK RAMP (ENTRIES ONLY): the first N real entries of the ET day
        # share an ADAPTIVE fraction of the day's risk envelope so the first shots can't pre-
        # spend what the red-day reducer would only later claw back (IPW -$137). Size-DOWN
        # only, releases at entry N OR a green realized start. This is the ENTRY-fill path
        # (held states never consult it) — it CANNOT delay/shrink an exit. Composes under the
        # 3x clamp below. Fail-OPEN / flag OFF => 1.0 (byte-identical).
        _day_open_ramp_mult, _day_open_ramp_meta = day_open_risk_ramp_multiplier(
            db, execution_family=ef
        )
        if _day_open_ramp_mult < 1.0:
            le["day_open_risk_ramp"] = _day_open_ramp_meta
        # B2 ask-heavy book size-down (2026-06-12 entry study: imbalance5 <
        # -0.4 at the decision tick = 71% of chronic-late entries vs 29%,
        # Cliff's d -0.31). The L2 snapshot is already taken at candidate
        # detection; an ask-stacked book halves the risk fraction rather than
        # skipping (counterexamples exist both ways). -0.4 is the measured
        # threshold (documented constant); the fraction is the one knob.
        _l2_mult = 1.0
        try:
            _l2s = le.get("entry_l2_snapshot") or {}
            _imb = float(_l2s.get("imbalance5"))
            if _imb < -0.4:
                _l2_mult = float(getattr(settings, "chili_momentum_entry_ask_heavy_size_fraction", 0.5) or 1.0)
                le["ask_heavy_size_down"] = {"imbalance5": round(_imb, 4), "mult": _l2_mult}
        except (TypeError, ValueError):
            _l2_mult = 1.0
        # A2 schedule risk multiplier (quant pass v2, +$3k/3d premarket leg):
        # hot (04:00–10:30 ET) ×1.5, midday ×0.5, late ×0, afterhours ×0 (entries blocked
        # at arm; this is the belt for already-armed sessions). Equities only — crypto
        # rides its own 24/7 clock. Combined multipliers are capped at 3× base by the
        # clamp below; the aggregate at-risk cap still governs.
        #
        # WAVE-1 FIX-8: the map now covers "afterhours" ×0.0 AND the fall-through default
        # is 0.0 (fail-CLOSED for ANY unknown window). Previously the default was 1.0, so a
        # 16:00-20:00 ET after-hours entry (is_tradeable_now() True, schedule_window_now()
        # "closed" pre-fix) sized at FULL risk (14d AH: 1W/11L −$72.65). Exits untouched.
        _sched_mult = 1.0
        if not str(sess.symbol or "").upper().endswith("-USD"):
            try:
                from .market_profile import schedule_window_now

                _win = schedule_window_now()
                _sched_mult = {
                    "hot": 1.5, "midday": 0.5, "late": 0.0, "afterhours": 0.0,
                }.get(_win, 0.0)
                if _sched_mult != 1.0:
                    le["schedule_risk"] = {"window": _win, "mult": _sched_mult}
            except Exception:
                # Fail-CLOSED: a schedule read error must NOT size an entry at full risk.
                _sched_mult = 0.0
                _win = "unknown"
        if _sched_mult <= 0.0:
            _emit(db, sess, "live_entry_wait_late_window", {"window": _win})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state, "skipped": "late_window"}
        # TIER-2 OVERNIGHT size reduction: a multiplier (base 0.5) on the equity-relative
        # notional overnight — smaller size is the REAL protection against a gap through the
        # software stop (RH has no broker stop overnight). Equities only; composes with the
        # other size-down levers under the 3x clamp. Flag OFF / not overnight => 1.0 (no-op).
        _overnight_mult = 1.0
        if (
            getattr(settings, "chili_momentum_overnight_trading_enabled", False)
            and not str(sess.symbol or "").upper().endswith("-USD")
        ):
            try:
                from .market_profile import is_overnight_now as _is_overnight_now2

                if _is_overnight_now2(sess.symbol):
                    _overnight_mult = float(
                        getattr(settings, "chili_momentum_overnight_size_fraction", 0.5) or 1.0
                    )
                    if _overnight_mult != 1.0:
                        le["overnight_size_down"] = {"mult": _overnight_mult}
            except Exception:
                _overnight_mult = 1.0
        # L2.2 LIQUIDITY-SCALED RISK CAP (project_profitability_levers): the biggest losers
        # are wide-spread illiquid names sized too big (QXL −$229 @119bps; the −$697 low-float
        # gap-through tail). SHRINK the risk budget as the live spread eats the name's adaptive
        # tolerance — sizes the risky names DOWN without rejecting any trade (the L3 entry filter
        # killed winners; this never does). Entry sizing only, never an exit. OFF / mult==1.0 =>
        # the product is byte-identical. Replay applies the SAME helper with the SAME inputs => parity.
        _liq_mult = 1.0
        if bool(getattr(settings, "chili_momentum_liquidity_risk_cap_enabled", True)):
            try:
                from .risk_policy import spread_liquidity_risk_multiplier
                _liq_mult, _liq_meta = spread_liquidity_risk_multiplier(
                    spread_bps_live, _expected_move_bps,
                    floor=float(getattr(settings, "chili_momentum_liquidity_risk_floor", 0.5) or 0.5),
                )
                if _liq_mult < 1.0:
                    le["liquidity_risk"] = _liq_meta
            except Exception:
                _liq_mult = 1.0
        # META-LABEL DE-RATE (2026-06-23, adaptive + regime-aware, NEVER a veto): size DOWN a
        # low-edge / loser-profile entry per the meta-label model (evidence-scaled -> INERT until
        # it earns confidence from the growing live dataset; bounded [floor,1.0], never zeroes ->
        # preserves the explosive tail). Best-effort + LIGHT (in-process L2 ring + cached macro,
        # NO df refetch -> no submit latency; front_side median-imputed). Multiplies the risk
        # budget like the other size-down levers, capped by the 3x clamp. Kill-switch
        # chili_momentum_meta_label_derate_enabled.
        _meta_mult = 1.0
        if bool(getattr(settings, "chili_momentum_meta_label_derate_enabled", True)):
            try:
                from .meta_label import load_model, size_multiplier

                _mm_model = load_model("/app/data/_meta_label_model.json")
                if _mm_model and float(_mm_model.get("confidence") or 0.0) > 0.0:
                    from .entry_features import capture_entry_features, macro_regime_features

                    _mm_stop = guarded_ask * (1.0 - float(_eff_atr_pct) * float(_stop_atr_mult))
                    _mm_rr = class_aware_reward_risk(sess.symbol)
                    _mm_tgt = (guarded_ask + _mm_rr * (guarded_ask - _mm_stop)) if guarded_ask > _mm_stop else guarded_ask
                    _mm_feats = capture_entry_features(
                        sess.symbol, fill_px=float(guarded_ask), stop=float(_mm_stop),
                        target=float(_mm_tgt), qty=1.0, want_qty=1.0,
                        spread_bps=float(spread_bps_live or 0.0), atr_pct=float(_eff_atr_pct or 0.0),
                        stop_atr_pct_eff=float(_eff_atr_pct or 0.0), mid=float(guarded_ask),
                        dollar_vol=None, liq_mult=1.0, fire_ts=_utcnow(), entry_fidelity="live",
                        trigger_debug=(le.get("entry_trigger_debug") if isinstance(le.get("entry_trigger_debug"), dict) else None),
                        session_df=None, l2_db=db, l2_as_of=None, macro=macro_regime_features())
                    _mm = size_multiplier(_mm_feats or {}, _mm_model,
                                          floor=float(getattr(settings, "chili_momentum_meta_label_min_size", 0.4)))
                    if 0.0 < _mm < 1.0:
                        _meta_mult = _mm
                        le["meta_label_derate"] = {"mult": round(_mm, 4),
                                                   "conf": round(float(_mm_model.get("confidence") or 0.0), 4)}
            except Exception:
                _meta_mult = 1.0
        # WIN-CYCLE FATIGUE YELLOW down-size (Batch E(2), ENTRIES ONLY): once today's clean-win
        # count (this execution family) reaches the YELLOW band, size DOWN the NEW entry's risk
        # budget by the documented fraction (the RED band is a hard arm-gate halt, not a size of
        # 0 — an OPEN position is never de-sized). Composes with the streak/cushion/liquidity
        # levers under the same 3x clamp. This is the ENTRY-fill sizing path; it physically
        # cannot reach an exit. OFF / green / fail-open => 1.0 (byte-identical). Replay applies
        # the SAME multiplier from the SAME helper => parity. docs/DESIGN/MOMENTUM_LANE.md
        _fatigue_mult = 1.0
        if bool(getattr(settings, "chili_momentum_win_cycle_fatigue_enabled", False)):
            try:
                from .auto_arm import win_cycle_yellow_size_multiplier

                _fatigue_mult, _fatigue_meta = win_cycle_yellow_size_multiplier(db, execution_family=ef)
                if _fatigue_mult < 1.0:
                    le["win_cycle_fatigue"] = _fatigue_meta
            except Exception:
                _fatigue_mult = 1.0
        # P2 PER-SYMBOL ATTEMPT FATIGUE — YELLOW down-size (ENTRIES ONLY): the borderline last
        # allowed attempt on a ticker that has already chopped us today is taken SMALLER (the RED
        # over-cap attempt is vetoed at the arm-gate, not de-sized to 0 — an OPEN position is
        # never de-sized to nothing). This is the ENTRY-fill sizing path; it physically cannot
        # reach an exit (held states never consult fatigue). Composes with the streak/cushion/
        # liquidity/win-cycle levers under the same 3x clamp. OFF / green / fail-open => 1.0
        # (byte-identical). Replay applies the SAME multiplier from the SAME helper => parity.
        _sym_fatigue_mult = 1.0
        if bool(getattr(settings, "chili_momentum_per_symbol_fatigue_enabled", False)):
            try:
                from .auto_arm import per_symbol_fatigue_size_multiplier

                _sym_fatigue_mult, _sym_fatigue_meta = per_symbol_fatigue_size_multiplier(
                    db, sess.symbol, execution_family=ef
                )
                if _sym_fatigue_mult < 1.0:
                    le["per_symbol_fatigue"] = _sym_fatigue_meta
            except Exception:
                _sym_fatigue_mult = 1.0
        # P3 HOT/COLD-TAPE SIZE SCALING — a BOUNDED size multiplier: size UP on hot tape, DOWN on
        # cold, composed MULTIPLICATIVELY with the other levers under the SAME 3x clamp. The
        # liquidity cap (max_notional above) and the equity-relative notional ceiling remain HARD
        # ceilings — this only scales the per-trade RISK BUDGET, and compute_risk_first_quantity
        # below still caps qty at max_notional, so the mult can NEVER push notional past any cap.
        # OFF / fail-neutral => 1.0 (byte-identical). docs/DESIGN/MOMENTUM_LANE.md
        _hot_cold_mult = 1.0
        if bool(getattr(settings, "chili_momentum_hot_cold_size_enabled", False)):
            try:
                from .auto_arm import hot_cold_tape_size_multiplier

                _hc_rvol = _latest_rvol(db, sess.symbol)
                _hot_cold_mult, _hc_meta = hot_cold_tape_size_multiplier(
                    atr_pct=regime_atr_pct(_regime), rvol=_hc_rvol,
                )
                if _hot_cold_mult != 1.0:
                    le["hot_cold_size"] = _hc_meta
            except Exception:
                _hot_cold_mult = 1.0
        # GAP 2 TIME/DECISION-FATIGUE DERATE (PSY101; Ross trades best EARLY): size DOWN
        # as the session lengthens (minutes since the 09:30 ET RTH open) and/or today's
        # real entered-trade count grows. The multiplier is bounded (floor, 1.0] — it can
        # ONLY shrink the per-trade risk budget, composes multiplicatively under the SAME
        # 3x clamp below, and compute_risk_first_quantity still caps qty at max_notional,
        # so it can NEVER push notional past any ceiling. ENTRY-fill sizing path only (it
        # physically cannot reach an exit). OFF (default) / fail-neutral => 1.0 (byte-
        # identical). Replay applies the SAME helper with the SAME inputs => parity.
        _time_fatigue_mult = 1.0
        if bool(getattr(settings, "chili_momentum_fatigue_derate_enabled", False)):
            try:
                from .risk_policy import (
                    _count_real_entries_today as _fd_count,
                    _minutes_since_rth_open_et as _fd_minutes,
                    fatigue_derate_multiplier as _fd_mult,
                )

                _fd_is_crypto = str(sess.symbol or "").upper().endswith("-USD")
                _fd_trades = _fd_count(db, execution_family=ef)
                _fd_max = int(getattr(settings, "chili_momentum_daily_trade_count_base", 5) or 5)
                _time_fatigue_mult, _time_fatigue_meta = _fd_mult(
                    trade_count_today=_fd_trades,
                    max_trades_per_day=_fd_max,
                    minutes_since_open=(None if _fd_is_crypto else _fd_minutes()),
                    is_crypto=_fd_is_crypto,
                )
                if _time_fatigue_mult < 1.0:
                    le["time_fatigue_derate"] = _time_fatigue_meta
            except Exception:
                _time_fatigue_mult = 1.0
        # GAP 1 + GAP 2 (Warrior re-audit) HALT-RESUME SIZE LEVER: a halt-resume-dip
        # entry carries an optional size multiplier set upstream (le["halt_entry_size_
        # mult"]): GAP 1 de-weights an extending halt-CHAIN long (<1.0), GAP 2 boosts a
        # bullish gap-up resumption / penalises a lower resumption. It composes
        # multiplicatively under the SAME 3x clamp + hard max_notional ceiling as every
        # other lever (compute_risk_first_quantity still caps qty at max_notional, so a
        # GAP-2 boost can NEVER push notional past any ceiling). Default (no halt-resume
        # entry / both flags OFF ⇒ key absent) => 1.0 (byte-identical).
        _halt_size_mult = 1.0
        try:
            _hsm = _float_or_none(le.get("halt_entry_size_mult"))
            if _hsm is not None and _hsm > 0:
                _halt_size_mult = float(_hsm)
        except Exception:
            _halt_size_mult = 1.0
        # LOCATE #3 DIP-VELOCITY CONVICTION lever: a steeper dip-family flush carries a
        # bounded (>=1.0) size boost (_dip_velocity_size_mult, set upstream). It composes
        # multiplicatively under the SAME 3x clamp + hard max_notional ceiling, so it can
        # NEVER push notional past any ceiling. Default (non-dip / flag OFF ⇒ key absent) =>
        # 1.0 (byte-identical).
        _dip_velocity_mult = 1.0
        try:
            _dvm = _float_or_none(le.get("dip_velocity_size_mult"))
            if _dvm is not None and _dvm > 0:
                _dip_velocity_mult = float(_dvm)
        except Exception:
            _dip_velocity_mult = 1.0
        # CATALYST-CONVICTION lever (Ross E2): a STRONG, credible catalyst (the deployed
        # strong/weak/fake news grade — no new feed) carries a bounded (>=1.0) UPWARD size
        # multiplier. It composes multiplicatively under the SAME min(..., base*3.0) clamp +
        # the hard max_notional ceiling below, so it can NEVER push notional past any ceiling
        # and is NEVER a veto. Default (flag OFF / no-or-weak-or-fake catalyst) => 1.0
        # (byte-identical). Lane-agnostic (grade is per-symbol news); fail-neutral to 1.0.
        _catalyst_conviction_mult = 1.0
        if bool(getattr(settings, "chili_momentum_catalyst_conviction_enabled", False)):
            try:
                from .risk_policy import catalyst_conviction_size_multiplier

                _catalyst_conviction_mult, _cc_meta = catalyst_conviction_size_multiplier(
                    sess.symbol
                )
                if _catalyst_conviction_mult > 1.0:
                    le["catalyst_conviction_size"] = _cc_meta
            except Exception:
                _catalyst_conviction_mult = 1.0
        # PRIME-WINDOW SIZE LEVER (time-of-day schedule, default OFF ⇒ 1.0 byte-identical): a
        # BOUNDED-UPWARD (>=1.0, <= prime_window_size_mult_max) multiplier when ET is inside the
        # documented prime window (default 04:00-10:30 ET, the premarket+open drive band). It
        # composes multiplicatively into the SAME min(..., base*3.0) clamp + the hard max_notional
        # ceiling below, so a prime-window boost can NEVER push notional past base*3.0 and is NEVER
        # a veto (floor 1.0 = never a shrink). Flag OFF / outside window / any error ⇒ 1.0. The
        # FADE-DRIVEN late-day cutoff (the suppression half) lives in auto_arm (PRE-arm), not here.
        _prime_window_mult = 1.0
        if bool(getattr(settings, "chili_momentum_timeofday_schedule_enabled", False)):
            try:
                from .auto_arm import prime_window_size_multiplier

                _prime_window_mult, _pw_meta = prime_window_size_multiplier()
                if _prime_window_mult > 1.0:
                    le["prime_window_size"] = _pw_meta
            except Exception:
                _prime_window_mult = 1.0
        # LEVER 1 — EXTREME-VOL / MISSING-RVOL RISK-BOUNDED SIZE-DOWN (the win-win other half).
        # viability.py now keeps a GENUINE explosive Ross-class mover (clears the existing
        # below_explosive_floor + tradable + spread-OK) LIVE-eligible on extreme-vol or a
        # merely-MISSING rvol datum instead of blanket-blocking it (UPC +476%, Ross +$35k,
        # CHILI $0). The condition of that admission is that it is sized DOWN here so the
        # WORST-CASE dollar loss is bounded the SAME as a normal trade despite the wider vol /
        # the unconfirmed rvol. We read the persisted marker (via.explain_json) AND re-confirm
        # the live extreme-vol regime as a belt; either signal arms the down-size. The
        # multiplier is ONE documented fraction (default 0.5 = half-risk, the same conservative
        # end the ask-heavy/overnight levers use). It composes multiplicatively under the SAME
        # 3x clamp + hard max_notional ceiling below, so it can ONLY shrink the per-trade RISK
        # BUDGET (never raise it, never a veto); the max-loss circuit + structural/vol-floored
        # stop + daily-loss breaker are untouched. Flag OFF / not-risk-bounded => 1.0 (byte-
        # identical). Replay applies the SAME fraction off the SAME persisted marker => parity.
        _extreme_vol_mult = 1.0
        if bool(getattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)):
            try:
                _evx = via.explain_json if isinstance(via.explain_json, dict) else {}
                _evb_marker = bool(_evx.get("extreme_vol_risk_bounded"))
                _evb_live_regime = str(_regime.get("volatility_regime") or "").lower() == "extreme"
                if _evb_marker or _evb_live_regime:
                    _extreme_vol_mult = float(
                        getattr(settings, "chili_momentum_extreme_vol_risk_bounded_fraction", 0.5)
                        or 0.5
                    )
                    _extreme_vol_mult = max(0.0, min(1.0, _extreme_vol_mult))
                    if _extreme_vol_mult < 1.0:
                        le["extreme_vol_risk_bounded_size_down"] = {
                            "mult": round(_extreme_vol_mult, 4),
                            "marker": _evb_marker,
                            "live_regime_extreme": _evb_live_regime,
                        }
            except Exception:
                _extreme_vol_mult = 1.0
        # P4(1) SQUEEZE-FUEL ENTRY SIZE-UP (default ON; OFF / un-armed ⇒ 1.0 byte-identical):
        # a name in the TOP within-batch squeeze percentile (its OWN squeeze_fuel_rank_pct, stamped
        # by the pipeline from Ortex SI%+CTB) whose tape AGREES (live OFI > 0) AND whose news AGREES
        # (strong-catalyst member) scales the per-trade RISK BUDGET UP by a bounded, PERCENTILE-driven
        # multiplier in [1.0, max_mult] — only the most squeeze-prone, tape-confirmed, news-backed
        # names get the FULL up-size. Triple-gated from REAL data (no magic absolute SI/CTB cutoff:
        # the rank floats with the batch). Composes multiplicatively under the SAME min(..., base*3.0)
        # clamp + hard max_notional ceiling below, so it can NEVER push notional past any cap and is
        # NEVER a veto; the max-loss circuit + structural stop + daily-loss breaker are untouched.
        # Equity-only (crypto has no borrow data ⇒ no rank stamped ⇒ 1.0). Fail-neutral to 1.0.
        _squeeze_size_mult = 1.0
        if bool(getattr(settings, "chili_momentum_squeeze_entry_sizeup_enabled", True)):
            try:
                from .ross_momentum import squeeze_entry_size_multiplier as _sq_size

                _sq_extra = (ex_live.get("extra") or {}) if isinstance(ex_live, dict) else {}
                _sq_symu = str(sess.symbol or "").upper()
                _sq_sig = ((_sq_extra.get("ross_signals") or {}) if isinstance(_sq_extra.get("ross_signals"), dict) else {}).get(_sq_symu)
                _sq_rank = _float_or_none(_sq_sig.get("squeeze_fuel_rank_pct")) if isinstance(_sq_sig, dict) else None
                if _sq_rank is not None:
                    # Live OFI (tape agreement) — FRESH from the in-process flow feed, the SAME
                    # source the entry flow-veto reads (NOT the stale batch exec_json).
                    _sq_ofi = None
                    try:
                        from .pipeline import _live_ofi_microprice as _sq_ofi_fn
                        _sq_ofi, _ = _sq_ofi_fn(sess.symbol, db=db)
                        _sq_ofi = None if _sq_ofi is None else float(_sq_ofi)
                    except Exception:
                        _sq_ofi = None
                    # News agreement — strong-catalyst membership (persisted from meta into extra).
                    _sq_strong = _sq_extra.get("strong_catalyst_symbols")
                    _sq_news = bool(_sq_strong and _sq_symu in {str(x).upper() for x in _sq_strong})
                    _squeeze_size_mult, _sq_meta = _sq_size(
                        _sq_rank, ofi=_sq_ofi, news_agrees=_sq_news,
                        top_pctl=float(getattr(settings, "chili_momentum_squeeze_entry_top_pctl", 0.80) or 0.80),
                        max_mult=float(getattr(settings, "chili_momentum_squeeze_entry_max_mult", 1.50) or 1.50),
                    )
                    if _squeeze_size_mult > 1.0:
                        le["squeeze_fuel_size_up"] = _sq_meta
            except Exception:
                _squeeze_size_mult = 1.0
        # FRACTIONAL-KELLY TRIPLE-CONFLUENCE SIZE-UP (P5): size UP — and ONLY up — when
        # squeeze-fuel AND OFI AND a STRONG news catalyst ALL agree at THIS entry instant. A
        # HALF-KELLY bet off the blended conviction percentile (no magic win-rate). Composes
        # under the SAME min(.., base*3.0) clamp + hard max_notional ceiling below (never past
        # any cap), NEVER a veto. The #769 max-loss circuit (every tick downstream) still bounds
        # the realized worst case on the resulting qty. Equity-only; flag default ON; any missing
        # leg / OFF / error => 1.0 (byte-identical). Reads are cached/fail-open (no new vendor).
        _kelly_conviction_mult = 1.0
        if (
            bool(getattr(settings, "chili_momentum_kelly_conviction_enabled", True))
            and not str(sess.symbol or "").upper().endswith("-USD")  # equity-only signals
        ):
            try:
                from .risk_policy import triple_confluence_kelly_multiplier
                from .pipeline import _live_ofi_microprice as _kc_ofi_reader
                _kc_ofi, _ = _kc_ofi_reader(sess.symbol, db=db)
                _kc_sq = None
                try:
                    from .ross_momentum import squeeze_fuel_signal as _kc_sf
                    from .short_mechanics import get_short_mechanics as _kc_mech
                    _km = _kc_mech(sess.symbol) or {}
                    if _km:
                        _kc_sig = _kc_sf(
                            _km.get("short_interest_pct"), _km.get("cost_to_borrow"),
                            utilization=_km.get("utilization"),
                            is_easy_to_borrow=_km.get("is_easy_to_borrow"),
                        )
                        _kc_sq = _kc_sig.squeeze_pct
                except Exception:
                    _kc_sq = None
                _kc_rank = 0
                try:
                    from .catalyst import catalyst_grade_rank as _kc_grade
                    _kc_rank = int(_kc_grade(sess.symbol))
                except Exception:
                    _kc_rank = 0
                _kelly_conviction_mult, _kc_meta = triple_confluence_kelly_multiplier(
                    squeeze_pct=_kc_sq, ofi=_kc_ofi, news_grade_rank=_kc_rank,
                )
                if _kelly_conviction_mult > 1.0:
                    le["kelly_conviction_size"] = _kc_meta
            except Exception:
                _kelly_conviction_mult = 1.0
        # ADAPTIVE FRONT-SIDE STRENGTH SIZE-TILT (the successor to the killed binary E1
        # backside veto; project_adaptive_frontside_strength). A CONTINUOUS strength score
        # in [0,1] (Kaufman-ER spine + the live OFI level/SLOPE + signed tape, weight-
        # renormalized over PRESENT terms) maps to a SIZE-TILT multiplier in [size_floor,1.0]
        # — a VWAP-reclaim-turning-up scores HIGH (full size, the E1-killed winner), a
        # falling-knife scores LOW (down-sized, NOT vetoed). SIZE-DOWN ONLY (mult<=1.0 by
        # construction; never sizes up). It composes multiplicatively under the SAME 3x clamp +
        # hard max_notional ceiling below, so it can ONLY shrink the per-trade RISK BUDGET; the
        # #769 max-loss circuit + structural/vol-floored stop + daily-loss breaker are untouched.
        # Inputs are the SAME live OFI/tape reads the squeeze/kelly levers already perform on
        # THIS entry path (one _live_flow_slope read = ofi_level+ofi_slope; one _live_trade_flow
        # = signed tape) — NO new network/db fetch. The ER-spine closes / vwap-dist / day-range
        # are derived from the ALREADY-FETCHED `_entry_df` (the 15m/5d frame this pre-entry tick
        # pulled once at the top for the spread gate, reused below for the range target), run
        # through the SAME canonical `_today_session_frame(...) -> front_side_state(...)` the
        # entry-gate backside vetoes use — a PURE, side-effect-free read (no new network/db
        # fetch, correct symbol, ET-today-anchored so the session VWAP/range are right). The ER
        # spine takes the today-session closes; vwap_dist_sigma / day_range_pos come from that
        # same FrontSideState. Any missing leg (no _entry_df, thin/degenerate frame, error) ⇒
        # that term is simply ABSENT and the score's weight-renormalized mean uses whatever
        # remains (fail-OPEN: no informative term ⇒ strength None ⇒ mult 1.0). The flow read
        # returning None (thin/stale tape) is threaded as stale_tape ⇒ mult 1.0. Flag OFF ⇒ mult 1.0 (byte-identical). The
        # `defer` flag is ADVISORY for v1 (logged only — no new re-poll loop, to avoid starving
        # the fast premarket window). docs/DESIGN/MOMENTUM_LANE.md
        _frontside_mult = 1.0
        if bool(getattr(settings, "chili_momentum_frontside_adaptive_enabled", True)):
            try:
                from .ross_momentum import (
                    front_side_size_tilt as _fs_tilt,
                    front_side_strength_score as _fs_strength,
                )
                from .pipeline import (
                    _live_flow_slope as _fs_flow_reader,
                    _live_trade_flow as _fs_tape_reader,
                )

                # Reuse the SAME live order-flow read the recency-grace / RIDE-LOCK levers use
                # (no new fetch). None ⇒ thin/stale/crypto tape ⇒ stale_tape ⇒ fail-open mult 1.0.
                _fs_flow = _fs_flow_reader(sess.symbol, db=db)
                _fs_stale = not isinstance(_fs_flow, dict)
                _fs_ofi_level = _float_or_none(_fs_flow.get("ofi_level")) if isinstance(_fs_flow, dict) else None
                _fs_ofi_slope = _float_or_none(_fs_flow.get("ofi_slope")) if isinstance(_fs_flow, dict) else None
                try:
                    _fs_tape = _fs_tape_reader(sess.symbol, db=db)
                    _fs_tape = None if _fs_tape is None else float(_fs_tape)
                except Exception:
                    _fs_tape = None
                # ER-SPINE inputs from the ALREADY-FETCHED `_entry_df` (no new fetch): run the
                # 15m/5d frame through the SAME canonical today-slice + front_side_state the entry
                # gates use, so the session VWAP/range anchor on ET-today and the closes feed the
                # Kaufman ER. Pure / fail-OPEN: missing _entry_df / thin frame / any error ⇒ all
                # three stay None (those terms simply drop out of the weight-renormalized mean).
                _fs_closes = None
                _fs_vwap_dist = None
                _fs_range_pos = None
                try:
                    if _entry_df is not None and not getattr(_entry_df, "empty", True):
                        from .entry_gates import _today_session_frame as _fs_today_frame
                        from .ross_momentum import front_side_state as _fs_state_fn
                        _fs_sess_df = _fs_today_frame(_entry_df)
                        # FIX-19(a): blend the LIVE mid tick (fresher than the last completed
                        # close) into the front-side position read. Fail-open to close if no tick.
                        _fs_state = _fs_state_fn(_fs_sess_df, live_price=_float_or_none(mid))
                        _fs_vwap_dist = _float_or_none(getattr(_fs_state, "vwap_dist_sigma", None))
                        _fs_range_pos = _float_or_none(getattr(_fs_state, "day_range_pos", None))
                        try:
                            _fs_closes = [
                                float(c)
                                for c in _fs_sess_df["Close"].astype(float).tolist()[-12:]
                            ]
                        except Exception:
                            _fs_closes = None
                except Exception:
                    _fs_closes = _fs_vwap_dist = _fs_range_pos = None
                _fs_score = _fs_strength(
                    closes=_fs_closes,
                    vwap_dist_sigma=_fs_vwap_dist,
                    day_range_pos=_fs_range_pos,
                    ofi_level=_fs_ofi_level,
                    ofi_slope=_fs_ofi_slope,
                    signed_tape=_fs_tape,
                )
                # REGIME-ADAPTIVE RAMP ANCHORS (kill the fixed 0.25/0.75 magic): record this
                # freshly-computed strength into the bounded TTL'd cross-name distribution, then
                # — once >= K fresh samples are warm — derive s_lo/s_hi/defer = p25/p75/p15 of
                # the LIVE regime distribution (hot tape ⇒ ramp shifts up; cold ⇒ down). COLD /
                # insufficient samples / degenerate dist ⇒ None ⇒ the documented base 0.25/0.75/
                # 0.15 (byte-identical to today). size_floor STAYS the one documented safety-base.
                if _fs_score is not None:
                    _frontside_dist_note(float(_fs_score))
                _fs_defer_base = float(getattr(settings, "chili_momentum_frontside_defer_pctile", 0.15) or 0.0)
                _fs_s_lo, _fs_s_hi, _fs_defer_below = 0.25, 0.75, _fs_defer_base
                _fs_warm = False
                _fs_n_samples = 0
                _fs_adapt = _frontside_adaptive_thresholds()
                if _fs_adapt is not None:
                    _fs_s_lo, _fs_s_hi, _fs_adapt_defer, _fs_n_samples = _fs_adapt
                    # Honor the operator knob: defer disabled (pctile 0) stays disabled even
                    # when warm; otherwise the regime p15 replaces the fixed 0.15.
                    _fs_defer_below = (_fs_adapt_defer if _fs_defer_base > 0.0 else 0.0)
                    _fs_warm = True
                _frontside_mult, _fs_defer, _fs_detail = _fs_tilt(
                    _fs_score,
                    size_floor=float(getattr(settings, "chili_momentum_frontside_size_floor", 0.25) or 0.25),
                    s_lo=_fs_s_lo,
                    s_hi=_fs_s_hi,
                    defer_below=_fs_defer_below,
                    stale_tape=bool(_fs_stale),
                    enabled=True,
                )
                if _frontside_mult < 1.0 or _fs_defer:
                    le["frontside_size_tilt"] = {
                        "strength": (None if _fs_score is None else round(float(_fs_score), 4)),
                        "mult": round(float(_frontside_mult), 4),
                        "defer": bool(_fs_defer),
                        "ofi_level": _fs_ofi_level,
                        "ofi_slope": _fs_ofi_slope,
                        "signed_tape": _fs_tape,
                        "vwap_dist_sigma": _fs_vwap_dist,
                        "day_range_pos": _fs_range_pos,
                        "er_bars": (len(_fs_closes) if isinstance(_fs_closes, list) else 0),
                        "stale_tape": bool(_fs_stale),
                        # REGIME-ADAPTIVE ramp anchors actually in force this tick.
                        "adaptive_warm": bool(_fs_warm),
                        "adaptive_n": int(_fs_n_samples),
                        "s_lo": round(float(_fs_s_lo), 4),
                        "s_hi": round(float(_fs_s_hi), 4),
                        "defer_below": round(float(_fs_defer_below), 4),
                        **(_fs_detail if isinstance(_fs_detail, dict) else {}),
                    }
                _log.info(
                    "[momentum_neural] frontside size-tilt sym=%s strength=%s mult=%s defer=%s "
                    "adaptive=%s n=%s s_lo=%s s_hi=%s defer_below=%s base_max_loss=%s detail=%s",
                    sess.symbol,
                    (None if _fs_score is None else round(float(_fs_score), 4)),
                    round(float(_frontside_mult), 4),
                    bool(_fs_defer),
                    bool(_fs_warm),
                    int(_fs_n_samples),
                    round(float(_fs_s_lo), 4),
                    round(float(_fs_s_hi), 4),
                    round(float(_fs_defer_below), 4),
                    round(float(_base_max_loss), 4),
                    _fs_detail,
                )
            except Exception:
                _frontside_mult = 1.0  # fail-OPEN: a tilt error never blocks/shrinks the fill
        # ROSS RISK GAP 1 — SIZE-DOWN INTO THE 200MA / OVERHEAD RESISTANCE. Ross cuts share
        # size approaching the daily 200MA from below / into clear overhead; the ~22-factor
        # sizing chain below had NO 200MA/resistance-distance factor (full size straight into
        # the wall). The signed daily-ATR distances (dist_to_sma_200_atr / dist_to_resistance_atr)
        # are ALREADY computed on the cached, per-(symbol,day) DailyContext (no new fetch). A
        # CONTINUOUS smoothstep size-DOWN in [floor,1.0] keyed on the nearest OVERHEAD distance
        # composes as one more bounded _safe_mult factor below (under the SAME 3x clamp + hard
        # max_notional ceiling, so it can ONLY shrink the per-trade risk budget — never sizes up,
        # never vetoes). Equities only (the daily 200MA/overhead frame is a stock-chart concept;
        # crypto -USD names skip it). Flag OFF / no DailyContext / no overhead distance ⇒ mult 1.0
        # (byte-identical, fail-OPEN). docs/DESIGN/MOMENTUM_LANE.md
        _daily_room_mult = 1.0
        if (
            bool(getattr(settings, "chili_momentum_daily_room_size_down_enabled", True))
            and not str(sess.symbol or "").upper().endswith("-USD")
        ):
            try:
                from .risk_policy import daily_room_size_down_multiplier as _drm

                _dr_ctx = _daily_ctx_cached(sess.symbol, price=guarded_ask)
                _dr_d200 = _float_or_none(getattr(_dr_ctx, "dist_to_sma_200_atr", None)) if _dr_ctx is not None else None
                _dr_dres = _float_or_none(getattr(_dr_ctx, "dist_to_resistance_atr", None)) if _dr_ctx is not None else None
                _daily_room_mult, _dr_meta = _drm(_dr_d200, _dr_dres)
                if _daily_room_mult < 1.0:
                    le["daily_room_size_down"] = _dr_meta
                    _log.info(
                        "[momentum_neural] daily-room size-down sym=%s mult=%s room_atr=%s "
                        "dist_200=%s dist_res=%s base_max_loss=%s",
                        sess.symbol, round(float(_daily_room_mult), 4),
                        _dr_meta.get("room_atr"), _dr_d200, _dr_dres, round(float(_base_max_loss), 4),
                    )
            except Exception:
                _daily_room_mult = 1.0  # fail-OPEN: any error never blocks/shrinks the fill
        # ROSS RISK GAP 2 — RED-INTRADAY SIZE-DOWN (the cushion ladder, down side). Ross trades
        # SMALLER when down on the day; the cushion ladder sizes UP off banked GREEN cushion but
        # never DOWN when red intraday. A CONTINUOUS size-DOWN in [floor,1.0] keyed on today's
        # NEGATIVE realized P&L, scaled by the red depth in units of the day's per-trade risk
        # budget (self-relative — no fixed $). Composes as one more bounded _safe_mult factor
        # below (same 3x clamp + hard max_notional ceiling); green/flat ⇒ 1.0 (byte-identical).
        # The daily-loss cap + drawdown breaker remain the hard downside bound above this soft
        # de-risk. Flag OFF / fail-open ⇒ 1.0. docs/DESIGN/MOMENTUM_LANE.md
        _red_intraday_mult = 1.0
        if bool(getattr(settings, "chili_momentum_red_intraday_size_down_enabled", True)):
            try:
                from .risk_policy import red_intraday_size_down_multiplier as _rim

                _red_intraday_mult, _ri_meta = _rim(
                    db, base_loss_usd=float(_base_max_loss), user_id=sess.user_id
                )
                if _red_intraday_mult < 1.0:
                    le["red_intraday_size_down"] = _ri_meta
                    _log.info(
                        "[momentum_neural] red-intraday size-down sym=%s mult=%s day_realized=%s "
                        "red_units=%s base_max_loss=%s",
                        sess.symbol, round(float(_red_intraday_mult), 4),
                        _ri_meta.get("day_realized_usd"), _ri_meta.get("red_units"),
                        round(float(_base_max_loss), 4),
                    )
            except Exception:
                _red_intraday_mult = 1.0  # fail-OPEN: any error never blocks/shrinks the fill
        # LOW-7: sanitize EACH per-factor multiplier (fail-NEUTRAL to 1.0 on NaN/inf/negative)
        # as it enters the product so a single poisoned helper can never NaN-out or sign-flip the
        # whole budget and silently kill the fill. The 3x clamp + max_notional ceiling below are
        # unchanged; a valid product is byte-identical.
        _eff_max_loss = min(
            float(_base_max_loss) * _safe_mult(_streak_mult) * _safe_mult(_graduation_mult) * _safe_mult(_cushion_mult) * _safe_mult(_l2_mult) * _safe_mult(_sched_mult) * _safe_mult(_liq_mult) * _safe_mult(_meta_mult) * _safe_mult(_prior_day_mult) * _safe_mult(_overnight_mult) * _safe_mult(_fatigue_mult) * _safe_mult(_sym_fatigue_mult) * _safe_mult(_hot_cold_mult) * _safe_mult(_time_fatigue_mult) * _safe_mult(_halt_size_mult) * _safe_mult(_dip_velocity_mult) * _safe_mult(_catalyst_conviction_mult) * _safe_mult(_prime_window_mult) * _safe_mult(_extreme_vol_mult) * _safe_mult(_squeeze_size_mult) * _safe_mult(_kelly_conviction_mult) * _safe_mult(_frontside_mult) * _safe_mult(_daily_room_mult) * _safe_mult(_red_intraday_mult) * _safe_mult(_perf_size_mult) * _safe_mult(_day_open_ramp_mult),
            float(_base_max_loss) * 3.0,  # hard combined-multiplier ceiling (quant pass v2)
        )
        # COMBINED SIZE-DOWN FLOOR for a genuine front-side A-setup (default ON; OFF /
        # non-A-setup / fail => byte-identical). The product above has a combined CEILING
        # (base x 3.0) but NO combined FLOOR — so unbounded MULTIPLICATIVE STACKING of the
        # 23 size-DOWN multipliers (e.g. daily_room 0.40 x midday-sched 0.50 = 0.20) can
        # crush a REAL A-setup's per-trade risk far below the equity-relative base (CUPR:
        # base $122 x 0.40 x 0.50 = $24, ~0.18% risk instead of 1%). This RAISES a stacked-
        # down budget back toward the base ONLY for a confirmed front-side A-setup; it can
        # ONLY RAISE _eff_max_loss toward base, NEVER above it (the floor multiplies the
        # base by FLOOR<=1.0, and we only apply it when the realized aggregate is BELOW the
        # floor) — so dollar-risk stays risk-FIRST and <= base (1% equity). Fail-CLOSED:
        # missing/ambiguous A-setup signals => no raise (today's stacked size-down stands).
        # The combined x3.0 CEILING above, the #769 max-loss circuit, the structural/vol-
        # floored stop, the notional ceiling, and the per-broker daily-loss cap are ALL
        # untouched and still bound the worst case. Flag OFF => byte-identical.
        if (
            bool(getattr(settings, "chili_momentum_combined_size_floor_enabled", True))
            and float(_base_max_loss) > 0.0
        ):
            try:
                # A-SETUP GATE — built from signals ALREADY in scope on this entry path (no
                # new fetch / no invented signal): (1) front-side + above-VWAP, (2) forward
                # momentum (live OFI level/slope > 0), (3) viability cleared the family
                # A-setup floor (params["entry_revalidate_floor"], the bar this entry already
                # passed). Fail-CLOSED: any unbound/ambiguous signal => not an A-setup.
                _af_above_vwap = (le.get("entry_above_vwap") is True)
                try:
                    _af_ofi_fwd = (
                        (_fs_ofi_level is not None and float(_fs_ofi_level) > 0.0)
                        or (_fs_ofi_slope is not None and float(_fs_ofi_slope) > 0.0)
                    )
                except NameError:
                    _af_ofi_fwd = False  # front-side block didn't run / flag OFF => fail-closed
                _af_via = float(via.viability_score or 0.0)
                _af_via_floor = float(params["entry_revalidate_floor"])
                _is_frontside_a_setup = bool(
                    _af_above_vwap and _af_ofi_fwd and (_af_via >= _af_via_floor)
                )
                _csf_floor = float(
                    getattr(settings, "chili_momentum_combined_size_down_floor", 0.5) or 0.5
                )
                _csf_floor = max(0.0, min(1.0, _csf_floor))
                _combined_mult = float(_eff_max_loss) / float(_base_max_loss)  # realized aggregate size-down
                if _is_frontside_a_setup and _combined_mult < _csf_floor:
                    _csf_prev = float(_eff_max_loss)
                    _eff_max_loss = float(_base_max_loss) * _csf_floor
                    le["combined_size_down_floor"] = {
                        "floor": round(_csf_floor, 4),
                        "combined_mult_before": round(_combined_mult, 4),
                        "eff_before": round(_csf_prev, 2),
                        "eff_after": round(float(_eff_max_loss), 2),
                        "base_max_loss": round(float(_base_max_loss), 2),
                        "above_vwap": True,
                        "viability_score": round(_af_via, 4),
                    }
                    _log.info(
                        "[momentum_neural] combined size-down floor sym=%s eff=%s->%s "
                        "(combined_mult=%s floor=%s base=%s via=%s)",
                        sess.symbol,
                        round(_csf_prev, 2),
                        round(float(_eff_max_loss), 2),
                        round(_combined_mult, 4),
                        round(_csf_floor, 4),
                        round(float(_base_max_loss), 2),
                        round(_af_via, 4),
                    )
            except Exception:
                pass  # fail-CLOSED: any error never RAISES the budget (stacked size-down stands)
        # THIN/TOXIC-SPREAD HARD PER-TRADE LOSS CAP (default ON; OFF / not-thin => no-op,
        # byte-identical). When the name was admitted via the viability thin-spread squeeze
        # carve-out (it carries the extreme_vol_risk_bounded marker AND its live spread is
        # wide), clamp the per-trade dollar risk to a HARD fraction of the normal base so a
        # wrong vertical chase / wide-spread fill is small + recoverable. This is the worst-
        # case backstop on TOP of the extreme_vol size-down already in _eff_max_loss; it can
        # ONLY shrink (min()), never raise, and never vetoes. The #769 max-loss circuit +
        # structural/vol-floored stop + daily-loss breaker are untouched.
        if bool(getattr(settings, "chili_momentum_thin_spread_squeeze_lane_enabled", True)):
            try:
                _ts_marker = bool(_evx.get("extreme_vol_risk_bounded")) if isinstance(_evx, dict) else False
                _ts_wide = (
                    spread_bps_live is not None
                    and float(spread_bps_live) > float(
                        getattr(settings, "chili_momentum_live_eligible_max_spread_bps", 300.0) or 300.0
                    )
                )
                if _ts_marker and _ts_wide:
                    _ts_frac = float(
                        getattr(settings, "chili_momentum_thin_spread_hard_loss_fraction", 0.5) or 0.5
                    )
                    _ts_frac = max(0.05, min(1.0, _ts_frac))
                    _ts_cap = float(_base_max_loss) * _ts_frac
                    if _ts_cap < float(_eff_max_loss):
                        _eff_max_loss = _ts_cap
                        le["thin_spread_hard_loss_cap"] = {
                            "cap_usd": round(_ts_cap, 2),
                            "fraction": _ts_frac,
                            "spread_bps": round(float(spread_bps_live), 2),
                        }
            except Exception:
                pass
        # ADAPTIVE SPREAD-COST VETO/DERATE (2026-06-27, DEFAULT OFF ⇒ byte-identical):
        # judge the live entry spread RELATIVE to (a) the name's OWN recent typical spread
        # distribution (rolling p50/p75/p90 over momentum_nbbo_spread_tape) and (b) the
        # trade's expected R (round-trip spread cost as a fraction of the structural stop
        # distance). NEVER a flat bps bar — Ross low-float movers inherently trade wide
        # spreads (project_momentum_zero_fills_root_cause), so a flat veto re-creates the
        # 0-fills over-restriction. This is DERATE-ONLY (2026-06-27): it NEVER blocks an entry,
        # only sizes it DOWN (composing under the SAME 3x clamp + hard max_notional ceiling, so
        # it can NEVER push notional past any cap) — a momentum entry fires at the widest-spread
        # instant, so blocking on spread IS the 0-fills trap. At the extreme (EXTREME outlier vs
        # the name's OWN p90 AND cost eats > the documented max fraction of R) it FLOORS the size;
        # a wide-but-TYPICAL low-float spread with a good R PASSES at mult=1.0. Flag OFF / fail-
        # open => 1.0. (adaptive_spread_cost_veto_derate always returns allow=True; the `if not
        # _scv_allow` branch below is kept as a defensive guard but is currently unreachable.)
        if bool(getattr(settings, "chili_momentum_adaptive_spread_cost_veto_enabled", False)):
            try:
                from .spread_cost_veto import adaptive_spread_cost_veto_derate

                # stop_distance mirrors compute_risk_first_quantity's basis exactly.
                _scv_stop_dist = float(guarded_ask) * max(
                    0.003, float(_eff_atr_pct or 0.0) * float(_stop_atr_mult or 0.60)
                )
                _scv_allow, _scv_mult, _scv_reason, _scv_meta = adaptive_spread_cost_veto_derate(
                    symbol=sess.symbol,
                    entry_price=float(guarded_ask),
                    current_spread_bps=spread_bps_live,
                    stop_distance=_scv_stop_dist,
                    db=db,
                    flag_enabled=True,
                    # RECLAIM CARVE-OUT: thread the FIRED entry-trigger reason so a
                    # dip/VWAP-reclaim (which fires at the widest-spread moment) is
                    # DERATE-ONLY (never hard-vetoed) + judged vs the permissive R base.
                    entry_trigger_reason=le.get("entry_trigger_reason"),
                )
                if not _scv_allow:
                    le["spread_cost_veto"] = {"reason": _scv_reason, **(_scv_meta or {})}
                    _emit(db, sess, "live_entry_spread_cost_veto",
                          {"reason": _scv_reason, "meta": _scv_meta})
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state,
                            "skipped": "spread_cost_veto"}
                if 0.0 < _scv_mult < 1.0:
                    _eff_max_loss = min(
                        float(_eff_max_loss) * float(_scv_mult),
                        float(_base_max_loss) * 3.0,  # same hard combined-multiplier ceiling
                    )
                    le["spread_cost_derate"] = {"reason": _scv_reason, "mult": round(_scv_mult, 4),
                                                **(_scv_meta or {})}
            except Exception:
                pass  # fail-open: a spread-cost gate failure must never block a fill
        # Freeze the risk-first sizing inputs so a marketable re-peg (G1) can RE-SIZE
        # risk-first at the chased price instead of over-sizing off notional. [G1 review #2]
        le["entry_resize_basis"] = {
            "max_loss_usd": _eff_max_loss,
            "atr_pct": float(_eff_atr_pct),
            "stop_atr_mult": float(_stop_atr_mult),
            "base_increment": float(inc) if inc else 1.0,
            "base_min_size": float(mn) if mn else 1.0,
        }
        _rf_qty, _rf_meta = compute_risk_first_quantity(
            entry_price=guarded_ask,
            atr_pct=_eff_atr_pct,
            max_loss_usd=_eff_max_loss,
            max_notional_ceiling_usd=max_notional,
            base_increment=inc,
            base_min_size=mn,
            stop_atr_mult=_stop_atr_mult,
        )
        if _rf_qty and _rf_qty > 0:
            qty = _rf_qty
            le["entry_sizing"] = _rf_meta
        else:
            qty = _round_base_size(max_notional / guarded_ask, inc, mn)
            le["entry_sizing"] = {"model": "notional_first_fallback", "reason": _rf_meta.get("reason")}
        if qty <= 0:
            _emit(db, sess, "live_error", {"reason": "size_zero_after_rounding"})
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "size_zero"}
        # ── ANTICIPATION STARTER (item 1, DEFAULT OFF ⇒ byte-identical) ──────────────
        # Probe-then-add: submit a SMALL probe leg on the pivot break now, and ADD the
        # remainder only after the probe CONFIRMS (a real fill = the break is real) —
        # reusing the EXISTING pyramid in-flight add machinery (PHASE-1 broker-truth
        # merge + dedupe-on-broker_order_id + orphan/late-fill tracking). EQUITY-ONLY
        # (the crypto maker-only path is a distinct order shape; equity-first like the
        # pyramid add). When the probe split would leave either leg below base_min_size
        # we FALL BACK to the full single entry (byte-identical). Flag OFF ⇒ none of this
        # runs, qty unchanged. The remainder is realised by the pyramid path; it does NOT
        # bypass any veto (it only fires on a confirmed fill + passes the same in-flight
        # dedupe). docs/DESIGN/MOMENTUM_LANE.md
        le.pop("anticipation_remainder_qty", None)
        le.pop("anticipation_armed", None)
        if (
            bool(getattr(settings, "chili_momentum_anticipation_starter_enabled", False))
            and not str(sess.symbol or "").upper().endswith("-USD")
        ):
            try:
                _ant_frac = float(
                    getattr(settings, "chili_momentum_anticipation_probe_fraction", 0.25) or 0.25
                )
                _ant_frac = max(0.05, min(0.95, _ant_frac))
                _ant_full = float(qty)
                _ant_probe = _round_base_size(_ant_full * _ant_frac, inc, mn)
                _ant_rem = _round_base_size(_ant_full - _ant_probe, inc, mn)
                _ant_min = float(mn) if mn and mn > 0 else 1.0
                # Only split when BOTH legs are independently viable (>= base_min_size) and the
                # remainder is positive; else keep the single full entry (byte-identical).
                if (
                    _ant_probe is not None and _ant_rem is not None
                    and _ant_probe >= _ant_min and _ant_rem >= _ant_min
                    and (_ant_probe + _ant_rem) <= _ant_full + 1e-9
                ):
                    qty = _ant_probe
                    le["anticipation_armed"] = True
                    le["anticipation_full_qty"] = _ant_full
                    le["anticipation_probe_qty"] = _ant_probe
                    le["anticipation_remainder_qty"] = _ant_rem
                    le["anticipation_probe_fraction"] = _ant_frac
                    _emit(db, sess, "live_anticipation_probe_sized", {
                        "full_qty": _ant_full, "probe_qty": _ant_probe,
                        "remainder_qty": _ant_rem, "probe_fraction": _ant_frac,
                    })
            except Exception:
                # Fail-CLOSED to the full single entry (never a malformed split).
                le.pop("anticipation_remainder_qty", None)
                le.pop("anticipation_armed", None)
        estimated_guarded_notional = qty * guarded_ask
        if estimated_guarded_notional > max_notional + 1e-9:
            _emit(
                db,
                sess,
                "live_entry_blocked_by_notional_cap",
                {
                    "max_notional_usd": max_notional,
                    "estimated_guarded_notional_usd": estimated_guarded_notional,
                    "ask": ask,
                    "guarded_ask": guarded_ask,
                    "quantity": qty,
                    "decision_packet_id": decision_packet_id,
                },
            )
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True,
                "session_id": sess.id,
                "state": sess.state,
                "blocked": True,
                "reason": "notional_cap",
            }
        # Ross-style marketable-LIMIT entry: cap the fill at the guarded ask (ask +
        # the notional-guard buffer) instead of a market order that can SWEEP a thin
        # low-float book to a catastrophic price (the live stale_bbo / 300bps-abs-cap
        # failure mode that blocked every wide-spread name). The limit stays
        # marketable (at/above the ask) so it fills on the break, but never worse than
        # guarded_ask — the exact price the notional guard already sized against. If
        # it does not fill (the price ran away), the entry ack-timeout cancels it and
        # re-watches: a missed fill, not a chase. (docs/DESIGN/MOMENTUM_LANE.md)
        # MAKER-ONLY crypto entry (2026-06-13): the marketable guarded-ask limit
        # CROSSES and pays TAKER (~153bps RT) — the crypto plan's #1 lever
        # (maker-only, ~50bps) was built to avoid exactly this, but it was never
        # enforced on the order (post_only defaulted False; first live TAO trade's
        # fee $1.77 was 2x its gross loss). For crypto with maker-only enabled,
        # post a POST-ONLY limit AT THE BID (a true maker order). A post-only that
        # would cross is rejected by the venue (no order_id) → the existing
        # ack-timeout cancels + re-watches, exactly like a non-fill ("missed fill,
        # not a chase"). Equity + non-maker crypto keep the marketable guarded-ask.
        _maker_entry = (
            str(sess.symbol or "").upper().endswith("-USD")
            and bool(getattr(settings, "chili_coinbase_maker_only_enabled", False))
            and bid is not None
            and float(bid) > 0
        )
        if _maker_entry:
            entry_limit_px = float(bid)
            entry_limit_str = f"{entry_limit_px:.6f}".rstrip("0").rstrip(".")
        else:
            entry_limit_px = guarded_ask
            entry_limit_str = _fmt_limit_price_buy(entry_limit_px)
            # Anchor for the marketable re-peg chase: the ORIGINAL limit bounds the
            # cumulative drift (the R:R guard), and the re-peg counter resets per fresh entry.
            le["entry_original_limit_px"] = entry_limit_px
            le["entry_repeg_count"] = 0
        le["entry_notional_guard"] = {
            "max_notional_usd": max_notional,
            "ask": ask,
            "bid": bid,
            "mid": mid,
            "guarded_ask": guarded_ask,
            "estimated_guarded_notional_usd": estimated_guarded_notional,
            "quantity": qty,
            "order_type": "limit",
            "limit_price": entry_limit_str,
            "spread_bps": spread_bps_live,
            "slippage_bps_ref": slip_ref,
        }
        record_packet_execution_intent(
            db,
            decision_packet_id,
            {
                "surface": "momentum_live_runner_entry",
                "order_type": "limit",
                "limit_price": entry_limit_str,
                "side": "buy",
                "product_id": product_id,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_bps": spread_bps_live,
                "slippage_bps_ref": slip_ref,
                "max_notional_usd": max_notional,
                "guarded_ask": guarded_ask,
                "estimated_guarded_notional_usd": estimated_guarded_notional,
                "quantity": qty,
                "base_increment": inc,
                "base_min_size": mn,
                "notional_guard_multiplier": _notional_guard_multiplier(),
            },
        )
        _commit_le(sess, le)

        # B1: DETERMINISTIC entry id (idempotency). The agentic rail passes this cid as
        # ref_id; a random suffix would let a retried logical entry get a NEW id, so RH
        # could not dedup -> double-submit. Derive the suffix from stable inputs so a
        # re-submit of the SAME logical entry reuses the SAME cid. Format/length are
        # byte-identical to the old uuid form (robin_stocks ignores ref_id; only the
        # string identity matters for that path). Exit/scale/bailout cids stay random
        # this pass (documented follow-up).
        # REF-ID UNIQUENESS (2026-06-22): the agentic rail passes this cid as ref_id, so
        # it MUST be unique per place ATTEMPT — a re-watch / entry-chase re-peg that reused
        # the stable {sess|corr|entry} seed got RH "API 409: Reference ID must be unique"
        # and could NEVER re-enter (the entry-chase was dead the moment the rail unblocked).
        # Bump a per-session entry-place counter into the seed so each attempt is unique;
        # double-submit of the SAME attempt is guarded by the FSM transition to
        # pending_entry + the late-fill-sweep (tracked order_id). Counter persists via the
        # _commit_le after the place. Format/length byte-identical (robin_stocks ignores ref_id).
        _entry_place_n = int(le.get("entry_place_count", 0) or 0) + 1
        le["entry_place_count"] = _entry_place_n
        # Fold the PERSISTENT recycle counters (trade_cycles/stopout_cycles, kept across a
        # recycle) into the seed so a #1-lever re-entry after a stop-out cannot collide with
        # the first entry's cid (CTNT sid 9763 409 'Reference ID must be unique', 2026-06-29):
        # entry_place_count is RESET on recycle, so place_n=1 alone repeats. Same-attempt
        # retry (same cycle + same place_n) still reuses the SAME cid (idempotent).
        cid = _entry_client_order_id(
            session_id=sess.id,
            correlation_id=sess.correlation_id,
            trade_cycles=int(le.get("trade_cycles") or 0),
            stopout_cycles=int(le.get("stopout_cycles") or 0),
            place_n=_entry_place_n,
        )
        # Pre-market / after-hours entries must be flagged so the venue routes them
        # (Alpaca: limit + DAY tif + extended_hours; RH: extended_hours_override). In
        # the regular session this is False and the order stays a plain marketable GTC.
        try:
            from .market_profile import market_session_now

            _entry_extended = market_session_now(sess.symbol) != "regular"
        except Exception:
            _entry_extended = False
        le["entry_session_extended"] = bool(_entry_extended)
        # TIER-2 OVERNIGHT signal: when the master flag is ON and the clock is in the RH
        # overnight band, mark the entry overnight so the RH adapter routes it to
        # ``all_day_hours`` (the 24-hour market). The name reached here only via
        # is_tradeable_now's overnight branch, which already required 24h-eligibility — so
        # all_day_hours is sent ONLY for proven-eligible names (the 2026-06-23 regression
        # guard). Flag OFF / not overnight => False => extended_hours routing (byte-identical).
        _entry_overnight = False
        try:
            from .market_profile import is_overnight_now

            _entry_overnight = bool(
                getattr(settings, "chili_momentum_overnight_trading_enabled", False)
            ) and is_overnight_now(sess.symbol)
        except Exception:
            _entry_overnight = False
        le["entry_session_overnight"] = bool(_entry_overnight)
        _entry_kwargs = dict(
            product_id=product_id,
            side="buy",
            base_size=_fmt_base_size(qty),
            limit_price=entry_limit_str,
            client_order_id=cid,
            extended_hours=_entry_extended,
            # ORDER-TRUTH (2026-06-11): entry limits are DAY orders, never GTC —
            # a dead session's resting GTC buy (KMRK) filled hours later into a
            # -21.9% dump. Equity adapters map this to RH 'gfd'; crypto ignores.
            # Overnight keeps 'gfd': RH day-orders in the 24h session expire at the
            # 24h-session boundary (acceptable; a resting overnight GTC is the KMRK risk).
            time_in_force="gfd",
        )
        # Pass the overnight signal ONLY for the agentic RH equity rail (the only adapter
        # whose place_*_order accepts ``overnight`` -> all_day_hours). Crypto / robin_stocks /
        # alpaca don't take the kwarg, so they are called exactly as before (no regress).
        # normalize_execution_family + EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP are already
        # imported at MODULE level (top of file). A LOCAL re-import here made the name
        # function-local for the whole of tick_live_session and broke the earlier
        # module-level use at ~3080 with UnboundLocalError. Use the module-level names.
        if (
            _entry_overnight
            and normalize_execution_family(sess.execution_family)
            == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
        ):
            _entry_kwargs["overnight"] = True
        # MAKER-ONLY (2026-06-13): post-only so a crossing price is rejected (no
        # taker) — the ack-timeout then cancels + re-watches. Pass post_only ONLY
        # for crypto+maker (coinbase_spot supports it); the RH equity adapter does
        # NOT accept the kwarg, so equity is called exactly as before (no regress).
        if _maker_entry:
            _entry_kwargs["post_only"] = True
        # ── ATOMIC POSITION CAP (decouple_watching: B1 + B2 + B3) ────────────────
        # Positions are born at FILL (live_pending_entry → live_entered), seconds
        # after this submit. tick_live_session row-locks only its OWN row (:2001),
        # so two watchers tick in parallel and each read the same held count → both
        # submit → cap breached. Fix: an xact-scoped advisory lock keyed on
        # (user, lane) serializes the count-and-submit across worker connections
        # (batch pool + WS event loop both land here), so each submitter SEES the
        # prior one's committed in-flight order. The count therefore charges held
        # positions PLUS in-flight-submitted entries (born-but-not-yet-held) — only
        # that pair makes the cap exact under a burst (held-only would let every
        # serialized submitter read N-1). The lock auto-releases at the per-tick
        # db.commit() (event loop :247 / batch :891), so a hung worker cannot wedge
        # the lane (the auto_trader.py:1963 orphan-lock lesson). Flag OFF ⇒ this
        # entire block is a no-op — legacy single-cap path, parity-tested.
        if getattr(settings, "chili_momentum_decouple_watching_enabled", False):
            from sqlalchemy import text as _sql_text

            from .risk_evaluator import (
                aggregate_open_crypto_risk_usd,
                aggregate_open_risk_usd,
                count_inflight_entry_orders,
                count_open_positions,
                sum_inflight_entry_risk_usd,
            )
            from .risk_policy import (
                _account_equity_usd,
                admit_by_aggregate_risk,
                effective_position_cap,
                equity_relative_loss_cap,
            )

            _is_crypto = str(sess.symbol or "").upper().endswith("-USD")
            # "ML" (momentum lane) namespace in the high word — distinct from
            # auto_trader's 0x4154 "AT"; lane_key stays well under 2**63.
            _lane_key = (_MOMENTUM_LANE_LOCK_NS << 32) | (int(sess.user_id) & 0xFFFFFFFF)
            db.execute(_sql_text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lane_key})

            # ── COUNT BACKSTOP (effective_position_cap) ──────────────────────
            # When the atomic risk-budget governor (below) is ON, this count is a
            # MISCONFIG BACKSTOP — an outer ceiling, not the primary governor. The
            # primary admission is the shape-aware dollar budget. When the atomic
            # flag is OFF, this count is the SOLE governor (byte-identical to the
            # legacy decouple_watching path). docs/DESIGN/MOMENTUM_ENGINE.md §2.
            _cap = effective_position_cap(crypto=_is_crypto)
            _pos_ct = count_open_positions(db, user_id=sess.user_id, mode="live") + (
                count_inflight_entry_orders(db, user_id=sess.user_id, exclude_session_id=sess.id)
            )
            if _pos_ct >= _cap:
                _emit(db, sess, "live_entry_blocked_position_cap", {"pos_ct": _pos_ct, "cap": _cap})
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
                db.flush()
                return {
                    "ok": True, "session_id": sess.id, "state": sess.state,
                    "skipped": "position_cap_at_fill", "pos_ct": _pos_ct, "cap": _cap,
                }
            # ── ATOMIC SHAPE-AWARE RISK-BUDGET ADMISSION (CHUNK 2 PRIMARY) ────
            # The load-bearing engine-core change: admit iff the EQUITY lane's open
            # dollars-at-risk PLUS this candidate's ACTUAL (entry-stop)*fill_qty fit
            # the equity-relative aggregate budget (REUSED 0.03 fraction). This is
            # the CONTINUOUS dollars-at-risk governor that replaces the slot COUNT —
            # a flat/stuck broker-zero session holds ZERO aggregate_open_risk_usd, so
            # it can NEVER block a real entry (starvation dissolved by construction).
            # Computed INSIDE the advisory lock so a fill-burst cannot pass two
            # candidates against a stale aggregate (the second sees the first's
            # committed in-flight risk). Shape-aware: a tight-stop scalp consumes far
            # less budget than a wide-stop trade, so MORE tight scalps admit for the
            # same dollars (the count treated them equally). Crypto keeps its own
            # dollar backstop below (aggregate_open_risk_usd is equity-only by
            # design — the 2026-06-11 correlation guard). FAIL-CLOSED on unknown
            # equity / un-computable candidate risk (never size against the unknown).
            # Flag OFF ⇒ this block is a no-op; the count above is the sole governor.
            if (
                bool(getattr(settings, "chili_momentum_atomic_risk_budget_enabled", True))
                and not _is_crypto
            ):
                # FIX A (basis-INDEPENDENT budget): the 0.03 aggregate fraction was
                # calibrated against STABLE/UNLEVERED equity. Reading the default
                # buying-power basis (use_bp + apply_margin_multiple) would DOUBLE the
                # dollar ceiling on a 2x-margin account — the same "2x-margin -> 4x-risk"
                # basis bug the operator fixed for the slot COUNT (commit 0276285). Read
                # the stabilized total equity (prefer_equity routes through the last-good
                # stabilizer; apply_margin_multiple=False forces the unlevered basis), so
                # the budget ceiling is independent of margin. Still FAIL-CLOSED: a hard
                # outage returns None -> admit_by_aggregate_risk treats it as
                # equity_unavailable -> reject (never size against the unknown).
                _eq_eq = _account_equity_usd(
                    ef, apply_margin_multiple=False, prefer_equity=True
                )
                # Shape-aware candidate risk = per-share structural stop distance
                # (frozen by compute_risk_first_quantity at :7020 into _rf_meta) ×
                # the FULL intended qty. FIX B (anticipation under-charge): when the
                # anticipation starter splits the entry into a small probe leg now +
                # a pyramid remainder later (:7073), `qty` is only the PROBE — charging
                # the budget for the probe alone would admit ~4x too many anticipation
                # positions (the remainder is added under the SAME risk thesis). Budget
                # the FULL intended risk as ONE unit via anticipation_full_qty; fall back
                # to qty when the split did not arm (le has no anticipation_full_qty).
                # Mirrors max_loss_circuit_decision's structural-risk basis exactly.
                try:
                    _cand_stop_dist = float((_rf_meta or {}).get("stop_distance") or 0.0)
                except (TypeError, ValueError):
                    _cand_stop_dist = 0.0
                try:
                    _cand_full_qty = float(le.get("anticipation_full_qty") or qty)
                except (TypeError, ValueError):
                    _cand_full_qty = float(qty)
                _cand_risk_usd = _cand_stop_dist * _cand_full_qty
                # In-flight equity entries (submitted, not yet held) carry $ at-risk
                # the held-only aggregate can't see. Charge each its ACTUAL per-order
                # risk so a fill-burst can't slip dollars past the ceiling — the COUNT
                # above bounds the count atomically in the same lock; this bounds the
                # DOLLARS. Over-estimating is the safe side.
                _open_eq_risk, _ = aggregate_open_risk_usd(db, user_id=sess.user_id)
                # Per-trade loss-fraction FALLBACK. equity_relative_loss_cap(0.0, ...)
                # short-circuits to 0.0 (a non-positive fixed fallback is preserved by
                # _equity_relative_cap), so pass the SAME positive per-trade dollar
                # fallback the sizing path uses (chili_momentum_risk_max_loss_per_trade_usd)
                # — it resolves to equity x loss_fraction when equity is available and to
                # the fixed floor on an equity-fetch outage. Charging 0.0 would let an
                # in-flight fill-burst slip dollars past the ceiling.
                _per_trade_loss_fallback = float(
                    getattr(settings, "chili_momentum_risk_max_loss_per_trade_usd", 50.0) or 50.0
                )
                _per_trade_inflight_fallback = float(
                    equity_relative_loss_cap(_per_trade_loss_fallback, ef) or 0.0
                )
                # FIX C (in-flight proxy shape/multiplier-awareness): a flat
                # count * one-loss-fraction proxy under-charges a burst of HIGH-multiplier
                # entries (_eff_max_loss can be up to 3x the base via :6957). Sum the REAL
                # per-order risk each in-flight sibling persisted at submit time
                # (le['entry_inflight_risk_usd'], set below), falling back to the positive
                # flat per-trade estimate only when a sibling has none (pre-submit race /
                # older image). Excludes this submitter's own row.
                _inflight_eq_risk = sum_inflight_entry_risk_usd(
                    db,
                    user_id=sess.user_id,
                    per_trade_fallback_usd=_per_trade_inflight_fallback,
                    crypto_only=False,
                    exclude_session_id=sess.id,
                )
                _admit, _admit_meta = admit_by_aggregate_risk(
                    open_risk_usd=_open_eq_risk + _inflight_eq_risk,
                    candidate_risk_usd=_cand_risk_usd,
                    equity_usd=_eq_eq,
                )
                if not _admit:
                    _emit(db, sess, "live_entry_blocked_atomic_risk_budget", _admit_meta)
                    _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {
                        "ok": True, "session_id": sess.id, "state": sess.state,
                        "skipped": "atomic_risk_budget", "budget": _admit_meta,
                    }
            # ADAPTIVE DAILY TRADE-COUNT BUDGET (SCAL101): cap the NUMBER of fresh entries
            # per ET session — a discipline/overtrading guard distinct from the slot COUNT
            # above. Ceiling floats with regime heat (today's banked cushion) + recent
            # expectancy: tighten when the lane is cold, loosen when hot. _pos_ct (open +
            # in-flight, already computed under this lock) is the live open-entry count so a
            # fill-burst can't slip past the count. ADDITIVE/FAIL-OPEN: flag off / thin data
            # => allowed (byte-identical). Evaluated INSIDE the advisory lock so the count is
            # atomic with the position cap.
            from .risk_policy import daily_trade_count_budget_decision

            # A1(b): pass the candidate symbol so the #1 freshness-valid live-eligible mover
            # can earn the top-rank exemption when the ceiling is reached (CLRO-class names
            # must never be denied while B-names churned the budget).
            _tcb_ok, _tcb_meta = daily_trade_count_budget_decision(
                db, execution_family=ef, open_entry_count=_pos_ct, symbol=sess.symbol
            )
            if not _tcb_ok:
                _emit(db, sess, "live_entry_blocked_daily_trade_count_budget", _tcb_meta)
                # A1(c): the NEXT-day-lockout arming call is REMOVED. Hitting a per-day
                # trade-count ceiling is a normal quality-aware budget block on a BOT — NOT
                # a broken-discipline event that should lock out the next day (a misapplied
                # human-tilt rule, same class as the removed daily_trade_count human budget).
                # The 07-02 landmine: this fired 98x and armed a next-day lockout row.
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
                db.flush()
                return {
                    "ok": True, "session_id": sess.id, "state": sess.state,
                    "skipped": "daily_trade_count_budget", "budget": _tcb_meta,
                }
            if _is_crypto:
                _cryp_ct = count_open_positions(
                    db, user_id=sess.user_id, mode="live", crypto_only=True
                ) + count_inflight_entry_orders(
                    db, user_id=sess.user_id, crypto_only=True, exclude_session_id=sess.id
                )
                _bucket_cap = int(
                    getattr(settings, "chili_momentum_max_open_positions_per_correlation_bucket", 4) or 4
                )
                if _cryp_ct >= _bucket_cap:
                    _emit(db, sess, "live_entry_blocked_crypto_bucket_cap",
                          {"cryp_ct": _cryp_ct, "bucket_cap": _bucket_cap})
                    _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {
                        "ok": True, "session_id": sess.id, "state": sess.state,
                        "skipped": "crypto_bucket_cap", "cryp_ct": _cryp_ct, "bucket_cap": _bucket_cap,
                    }
                # B2 crypto dollar backstop. FAIL CLOSED when equity is unknown —
                # never size a position against an unknown account ([[feedback_adaptive_no_magic]]);
                # over-leverage is catastrophic, a missed entry during an equity-fetch
                # outage is not (exits don't pass through here, so open positions stay
                # managed). Equity is normally available live; this trips only on outage.
                _eq = _account_equity_usd(ef)
                if not _eq or float(_eq) <= 0:
                    _emit(db, sess, "live_entry_blocked_equity_unavailable", {"lane": "crypto"})
                    _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {
                        "ok": True, "session_id": sess.id, "state": sess.state,
                        "skipped": "equity_unavailable",
                    }
                _open_cryp_risk, _ = aggregate_open_crypto_risk_usd(db, user_id=sess.user_id)
                # Per-trade loss-fraction proxy. equity_relative_loss_cap(0.0, ...)
                # short-circuits to 0.0 (a non-positive fixed fallback is preserved),
                # which would NEVER charge the candidate OR in-flight crypto dollars —
                # the dollar cap would be dead. Pass the same positive per-trade dollar
                # fallback (chili_momentum_risk_max_loss_per_trade_usd) so _planned_usd
                # resolves to equity x loss_fraction (fixed floor on equity outage; we
                # already fail-closed above when equity is unknown).
                _per_trade_loss_fallback = float(
                    getattr(settings, "chili_momentum_risk_max_loss_per_trade_usd", 50.0) or 50.0
                )
                _planned_usd = float(equity_relative_loss_cap(_per_trade_loss_fallback, ef) or 0.0)
                # In-flight crypto entries (submitted, not yet filled) carry $ at-risk the
                # held-only aggregate can't see. Charge each a conservative per-trade proxy
                # (every crypto entry sizes to ~one loss-fraction) so a fill-burst can't
                # slip dollars past the ceiling — B3 already bounds the COUNT atomically in
                # the same lock; this bounds the DOLLARS. Over-estimating is the safe side.
                _inflight_cryp = count_inflight_entry_orders(
                    db, user_id=sess.user_id, crypto_only=True, exclude_session_id=sess.id
                )
                _inflight_cryp_risk = float(_inflight_cryp) * _planned_usd
                _cap_usd = float(
                    getattr(settings, "chili_momentum_max_aggregate_crypto_risk_pct_of_equity", 0.07) or 0.07
                ) * float(_eq)
                _proj_cryp_usd = _open_cryp_risk + _inflight_cryp_risk + _planned_usd
                if _cap_usd > 0 and _proj_cryp_usd > _cap_usd:
                    _emit(db, sess, "live_entry_blocked_crypto_dollar_cap", {
                        "open_crypto_risk_usd": round(_open_cryp_risk, 2),
                        "inflight_crypto_risk_usd": round(_inflight_cryp_risk, 2),
                        "planned_usd": round(_planned_usd, 2),
                        "projected_usd": round(_proj_cryp_usd, 2),
                        "cap_usd": round(_cap_usd, 2),
                    })
                    _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {
                        "ok": True, "session_id": sess.id, "state": sess.state,
                        "skipped": "crypto_dollar_cap",
                    }
            # ── FILL-BOUNDARY FINANCIAL-BREAKER RE-CHECK (safety completion) ──
            # The three FINANCIAL breakers (per-broker daily-loss, portfolio drawdown,
            # profit-giveback halt) are checked ONLY in auto_arm's arm-pass guards, not
            # at THIS fill boundary. default-ON decouple_watching lets a watcher armed
            # while the day was GREEN persist across many ticks, trigger, and submit an
            # entry AFTER the day breaches — filling into a breached day. Re-check all
            # three here, INSIDE the advisory lock (atomic with the risk-budget admit
            # above), so a breach + a fill cannot race. On ANY breach: BLOCK the entry
            # (do NOT submit), stay WATCHING (mirrors the count/risk-budget block — NOT
            # terminal; it retries next tick once the breaker clears). Reuses the EXACT
            # governance helpers auto_arm uses (broker_daily_loss_breached :3233 /
            # check_portfolio_drawdown_breaker :3211 / evaluate_profit_giveback_halt
            # :3268) — NO reimplementation. Per-broker daily-loss uses THIS session's
            # execution_family (ef). Equity + crypto both honored. The kill-switch is
            # already re-checked upstream (:4274/:4523); this ADDS the three financial
            # breakers beside it. Flag OFF ⇒ this block is a no-op (byte-identical). The
            # helpers fail-CLOSED on their own (return not-breached on internal error),
            # mirroring auto_arm's fail-open early-out semantics; a breaker that DOES
            # breach blocks. [[project_per_broker_daily_loss]] [[project_profitability_levers]]
            if bool(
                getattr(
                    settings,
                    "chili_momentum_fill_boundary_breaker_recheck_enabled",
                    True,
                )
            ):
                _breaker_block: Optional[dict] = None
                # (1) Per-broker daily-loss breaker (auto_arm.py:3230-3233 call shape).
                try:
                    from ..governance import broker_daily_loss_breached as _bdlb

                    _dl_breached, _dl_info = _bdlb(db, ef, user_id=int(sess.user_id))
                    if _dl_breached:
                        _breaker_block = {
                            "breaker": "daily_loss_cap_broker",
                            "family": _dl_info.get("family"),
                            "daily_pnl_usd": round(
                                float(_dl_info.get("realized", 0.0) or 0.0), 2
                            ),
                            "max_daily_loss_usd": round(
                                float(_dl_info.get("cap", 0.0) or 0.0), 2
                            ),
                        }
                except Exception:
                    pass
                # (2) Portfolio drawdown breaker (auto_arm.py:3209-3211 call shape).
                if _breaker_block is None:
                    try:
                        from ..portfolio_risk import (
                            check_portfolio_drawdown_breaker as _cpdb,
                        )

                        _dd_tripped, _dd_reason = _cpdb(db, int(sess.user_id))
                        if _dd_tripped:
                            _breaker_block = {
                                "breaker": "drawdown_breaker",
                                "dd_reason": _dd_reason,
                            }
                    except Exception:
                        pass
                # (3) Profit-giveback session halt (auto_arm.py:3266-3270 call shape).
                if _breaker_block is None:
                    try:
                        from .risk_evaluator import (
                            evaluate_profit_giveback_halt as _epgh,
                        )

                        _gb = _epgh(
                            db, user_id=int(sess.user_id), execution_family=ef
                        )
                        if _gb.get("halted"):
                            _breaker_block = {
                                "breaker": "profit_giveback",
                                "daily_pnl_usd": _gb.get("daily_pnl_usd"),
                                "peak_pnl_usd": _gb.get("peak_pnl_usd"),
                                "giveback_fraction": _gb.get("giveback_fraction"),
                            }
                    except Exception:
                        pass
                if _breaker_block is not None:
                    _emit(db, sess, "live_entry_blocked_by_breaker", _breaker_block)
                    _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {
                        "ok": True, "session_id": sess.id, "state": sess.state,
                        "skipped": "fill_boundary_breaker",
                        "breaker": _breaker_block.get("breaker"),
                    }
        # ── end atomic position cap; the lock releases when this tick's txn commits ─
        # ── ENTRY-TIME FLOW VETO: never BUY this exact tick into max selling. Keys on
        # LIVE flow (OFI + trade_flow), NOT the static book_imbalance the L2 seller-veto
        # reads. Applies to extreme movers too (selection vs entry-timing). ADDITIVE:
        # flag OFF or either flow absent (None) ⇒ no veto. On veto: stay WATCHING
        # (re-enter when flow flips positive). ──
        #
        # DATA SOURCE (deploy-blocker fix 2026-06-24): ofi/trade_flow are sourced FRESH
        # for sess.symbol from the SAME readers viability/entry_features use
        # (_live_ofi_microprice / _live_trade_flow) — NOT ex_live (execution_readiness_json),
        # which NEVER carries these keys (0 of 80,493 momentum_symbol_viability rows had
        # ofi/trade_flow; the batch-shared exec_json only persists batch feats, which have
        # them None). Reading ex_live made the None-guard ALWAYS fire ⇒ the veto was inert.
        # These are the EXACT readers capture_entry_features uses (so the value matches the
        # logged entry_features ofi=-1.0/trade_flow=-0.51 for the PLSM flush). Live default
        # as_of=None (crypto -> in-process Coinbase L2 ring + fast_orderbook fallback;
        # equity -> iqfeed_depth_snapshots / iqfeed_trade_ticks). Cheap (one short 15s-window
        # read) + exception-safe; any error -> None -> no veto (fail-open, byte-identical).
        try:
            from .pipeline import _live_ofi_microprice, _live_trade_flow

            _fv_ofi, _ = _live_ofi_microprice(sess.symbol, db=db)
            _fv_tf = _live_trade_flow(sess.symbol, db=db)
            _fv_ofi = None if _fv_ofi is None else float(_fv_ofi)
            _fv_tf = None if _fv_tf is None else float(_fv_tf)
        except Exception:
            _fv_ofi = _fv_tf = None
        # GATE 4 (explosive-mover recalibration): on an explosive name the STRONG-tape
        # OR-leg threshold is relaxed toward MAXIMUM selling (a thin-tape one-seller dip
        # is not "sellers winning"). MASTER + sub-flag gated; the both-bearish AND-leg is
        # UNCHANGED so a falling tape under a deteriorating book still vetoes. OFF / not
        # explosive ⇒ _fv_explosive False ⇒ byte-identical veto.
        _fv_explosive = (
            bool(getattr(settings, "chili_momentum_entry_flow_veto_explosive_exempt", False))
            and _session_is_explosive(via)
        )
        _fv_instant = _entry_flow_veto(_fv_ofi, _fv_tf, settings, explosive=_fv_explosive)
        # FIX-19(c) STICKY FLOW VETO: the per-tick veto forgets INSTANTLY, so ONE spoofy
        # non-negative print could flip a real-selling veto and fire the buy 53s later. Latch
        # the veto and only RELEASE it once flow has stayed non-veto across a rolling window.
        # A tick where flow is CLEAR starts (or continues) the release timer; a re-veto resets
        # it. Sticky-veto = latched AND the clear timer hasn't yet spanned the window. OFF /
        # no latch => byte-identical (the instantaneous veto alone gates). Fail-open on error.
        _fv_veto = _fv_instant
        _fv_sticky = False
        if bool(getattr(settings, "chili_momentum_sticky_flow_veto_enabled", True)):
            try:
                _fv_now = _utcnow().timestamp()
                _fv_window = float(getattr(settings, "chili_momentum_sticky_flow_veto_window_sec", 20.0) or 20.0)
                _fv_latched = bool(le.get("flow_veto_latched"))
                if _fv_instant:
                    # (Re)latch and reset the release timer — selling is active this tick.
                    le["flow_veto_latched"] = True
                    le.pop("flow_veto_clear_since", None)
                    _fv_veto = True
                elif _fv_latched:
                    # Latched but this tick is clear: run the release timer over the window.
                    _clear_since = _float_or_none(le.get("flow_veto_clear_since"))
                    if _clear_since is None:
                        le["flow_veto_clear_since"] = _fv_now
                        _clear_since = _fv_now
                    if (_fv_now - float(_clear_since)) >= _fv_window:
                        # Flow has stayed non-veto for the full window — release the latch.
                        le.pop("flow_veto_latched", None)
                        le.pop("flow_veto_clear_since", None)
                        _fv_veto = False
                    else:
                        # Still inside the release window — keep vetoing (sticky).
                        _fv_veto = True
                        _fv_sticky = True
            except Exception:
                # Fail-open to the instantaneous verdict (never strand a name on a latch bug).
                _fv_veto = _fv_instant
        if _fv_veto:
            _log.info(
                "[momentum_neural] entry FLOW-VETO %s: OFI=%s trade_flow=%s sticky=%s — deferring buy into selling",
                sess.symbol, _fv_ofi, _fv_tf, _fv_sticky,
            )
            _emit(db, sess, "live_entry_flow_veto", {
                "ofi": round(_fv_ofi, 4) if _fv_ofi is not None else None,
                "trade_flow": round(_fv_tf, 4) if _fv_tf is not None else None,
                "ofi_thr": float(getattr(settings, "chili_momentum_entry_flow_veto_ofi", -0.6)),
                "trade_flow_thr": float(getattr(settings, "chili_momentum_entry_flow_veto_trade_flow", -0.25)),
                "sticky": bool(_fv_sticky),
            })
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True, "session_id": sess.id, "state": sess.state,
                "skipped": "entry_flow_veto",
            }
        # Flow cleared the (possibly sticky) veto this tick — persist the released latch state.
        if bool(getattr(settings, "chili_momentum_sticky_flow_veto_enabled", True)):
            _commit_le(sess, le)
        # ── ENTRY-EXTENSION (chase) VETO: never BUY this exact tick when the entry sits
        # too far ABOVE the breakout level (bought near a local top after the move ran;
        # 06-24 RUN +19.9% / PLSM +33.8% above the break). Cap is ADAPTIVE to volatility
        # (max(floor, K·atr_pct)). entry_price = the marketable limit (entry_limit_px,
        # set just above); breakout_level = le["breakout_level_price"]; atr_pct = the
        # eff stop ATR% local. ADDITIVE: flag OFF or breakout_level/atr missing ⇒ no veto
        # (byte-identical). On veto: stay WATCHING (re-enter on a pullback toward the
        # level), NOT terminal — same defer pattern as the flow-veto + crypto_dollar_cap. ──
        try:
            _ev_lvl = _float_or_none(le.get("breakout_level_price"))
            # Extension-veto vol input = the CLEAN intraday-range proxy regime_atr_pct(_regime),
            # NOT _eff_atr_pct. _eff_atr_pct is the STOP-focused ATR after (a) effective_stop_atr_pct
            # vol-flooring and (b) the structural_or_vol_floored_atr_pct override = (entry-pullback_low)
            # /entry / stop_atr_mult. On the pullback-break path the structural override is ACTIVE, and a
            # MORE-extended chase (deeper pullback_low) INFLATES _eff_atr_pct, which would LOOSEN the cap
            # (max(floor, K*atr_pct)) on exactly the names the chase-veto must block (RUN +19.9% / PLSM
            # +33.8% slipped through). The clean regime ATR is the true intraday volatility, unaffected by
            # how deep the entry chased — so the cap stays tight as the chase extends. (06-24 recalibration.)
            _clean_regime_atr = regime_atr_pct(_regime)
            _ev_atr = float(_clean_regime_atr) if _clean_regime_atr is not None else None
            _ev_entry = float(entry_limit_px) if entry_limit_px is not None else None
        except (TypeError, ValueError):
            _ev_lvl = _ev_atr = _ev_entry = None
        # GATE 3 (explosive-mover recalibration): a true outlier squeeze is high-RVOL
        # despite a thin regime-ATR, so the clean-ATR extension cap under-leverages it.
        # When the boost flag is ON, read the latest RVOL (cheap tape resample, no
        # network) so the veto can widen the cap proportionally (hard-capped so a blow-off
        # chase still vetoes). MASTER + sub-flag gated: OFF ⇒ rvol stays None ⇒ no boost,
        # byte-identical.
        _ev_rvol = None
        if (
            bool(getattr(settings, "chili_momentum_explosive_recalibration_enabled", False))
            and bool(getattr(settings, "chili_momentum_entry_extension_rvol_boost_enabled", False))
        ):
            _ev_rvol = _latest_rvol(db, sess.symbol)
        if _entry_extension_veto(_ev_entry, _ev_lvl, _ev_atr, settings, rvol=_ev_rvol):
            _ext_cap = max(
                float(getattr(settings, "chili_momentum_entry_extension_floor_pct", 0.05)),
                float(getattr(settings, "chili_momentum_entry_extension_atr_mult", 1.0)) * max(0.0, float(_ev_atr or 0.0)),
            )
            _log.info(
                "[momentum_neural] entry EXTENSION-VETO %s: entry=%s vs break=%s (cap=%.4f atr_pct=%s) — deferring chase",
                sess.symbol, _ev_entry, _ev_lvl, _ext_cap, _ev_atr,
            )
            _emit(db, sess, "live_entry_extension_veto", {
                "entry_price": round(_ev_entry, 6) if _ev_entry is not None else None,
                "breakout_level": round(_ev_lvl, 6) if _ev_lvl is not None else None,
                "atr_pct": round(_ev_atr, 6) if _ev_atr is not None else None,
                "extension_cap_pct": round(_ext_cap, 6),
                "extension_pct": (
                    round((_ev_entry / _ev_lvl) - 1.0, 6)
                    if (_ev_entry is not None and _ev_lvl) else None
                ),
            })
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True, "session_id": sess.id, "state": sess.state,
                "skipped": "entry_extension_veto",
            }
        # ── GAP 2 ROUND-NUMBER ENTRY-TIMING CONTEXT (Warrior re-audit) — a CONTEXT modifier,
        # NOT a standalone veto: prefer a break-and-HOLD OVER a whole/half-dollar round number;
        # AVOID firing right INTO a round number from BELOW (overhead supply). It only DEFERS
        # (stay WATCHING, re-enter on the hold-over) — the EXACT extension-veto defer pattern;
        # it can NEVER terminalize or block an exit. ADDITIVE: flag OFF / no level / no round
        # number nearby ⇒ permit (byte-identical). Reuses entry_limit_px + breakout_level_price
        # + the clean regime ATR computed just above for the extension veto. ──
        _rn_ok, _rn_reason, _rn_dbg = round_number_entry_context(
            _ev_entry, _ev_lvl, _ev_atr,
        )
        if not _rn_ok:
            _log.info(
                "[momentum_neural] entry ROUND-NUMBER DEFER %s: entry=%s vs break=%s round=%s — into overhead, re-watching for hold-over",
                sess.symbol, _ev_entry, _ev_lvl, _rn_dbg.get("round_number"),
            )
            _emit(db, sess, "live_entry_round_number_defer", {
                "entry_price": round(_ev_entry, 6) if _ev_entry is not None else None,
                "breakout_level": round(_ev_lvl, 6) if _ev_lvl is not None else None,
                "reason": _rn_reason,
                **_rn_dbg,
            })
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True, "session_id": sess.id, "state": sess.state,
                "skipped": "entry_round_number_into_overhead",
            }
        # ── L2 ENTRY CONFIRMER (Phase 1, DEFER-only) — docs/DESIGN/L2_PRIMARY_SIGNAL.md ──
        # The LAST gate before submit, and it runs ONLY here (an ENTRY-only candidate that
        # cleared the chart trigger + BOTH vetoes above): a veto ALWAYS wins, we never
        # confirm into a vetoed book. TAPE-PRIMARY: require the executed tape to actively
        # confirm thrust (signed_tape_accel>0 AND tick_rate>=self-relative floor; OFI/micro
        # + rising depth-pctile secondary). CONSERVATIVE-ACTIVE: defer only on CLEAR no-tape
        # (accel<=0 AND OFI<0). On defer → stay WATCHING_LIVE + re-enter next tick (the EXACT
        # flow-veto/extension-veto defer pattern — the adaptive watch/reap bounds the slot, no
        # new hold) + emit live_l2_confirm_defer as the COUNTERFACTUAL (the would-have-entered
        # price). FAIL-OPEN: any None / thin / stale ⇒ confirm. KILL-SWITCH OFF ⇒ _l2_entry_confirm
        # returns ("confirm", ...) BEFORE any I/O ⇒ byte-identical (no extra DB read). Held /
        # position states never reach here, so a defer can NEVER block an exit/stop/flatten.
        _l2c_decision, _l2c_dbg = _l2_entry_confirm(sess.symbol, db=db, le=le, settings=settings)
        if _l2c_decision == "defer":
            _log.info(
                "[momentum_neural] entry L2-CONFIRM DEFER %s: accel=%s tick_rate=%s ofi=%s — re-watching for tape confirmation",
                sess.symbol, _l2c_dbg.get("signed_tape_accel"),
                _l2c_dbg.get("tick_rate"), _l2c_dbg.get("ofi"),
            )
            _emit(db, sess, "live_l2_confirm_defer", {
                **_l2c_dbg,
                "would_have_entered_price": (
                    float(entry_limit_px) if entry_limit_px is not None else None
                ),
            })
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True, "session_id": sess.id, "state": sess.state,
                "skipped": "l2_confirm_defer", "l2_confirm": _l2c_dbg,
            }
        # CHUNK 3-C — RAIL-GOVERNED PLACE: the token bucket shared with every other lane
        # rail call (places + get_order polls) bounds the rate so multi-admission cannot
        # flood / 429 the broker (the flooding risk Chunk 2 introduced by deleting the
        # slot count). Governor OFF ⇒ _governed_place is an instant pass-through and this
        # is byte-identical to `adapter.place_limit_order_gtc(**_entry_kwargs)`. A
        # governor DEFER returns ok=False/error=rail_governor_deferred -> the existing
        # not-ok branch below re-watches / retries next tick (never a silent drop).
        res = _governed_place(
            adapter, adapter.place_limit_order_gtc, sess=sess, **_entry_kwargs
        )
        # GOVERNOR DEFER (not a broker reject): the rail bucket was empty, so NO order was
        # submitted. Do NOT mark entry_submitted, do NOT write an entry-reject cooldown
        # (the name is fine — the rail was busy), do NOT transition to LIVE_ERROR. Stay
        # WATCHING so the next tick re-attempts once the bucket refills — the entry is
        # deferred, never dropped. This branch only runs when the governor is ON and the
        # rate is currently saturated; OFF ⇒ res never carries `deferred`.
        if res.get("deferred"):
            _emit(db, sess, "live_entry_governor_deferred", {
                "client_order_id": res.get("client_order_id"),
                "limit_price": entry_limit_str,
            })
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True, "session_id": sess.id, "state": sess.state,
                "skipped": "rail_governor_deferred",
            }
        le["entry_submitted"] = True
        le["entry_submit_utc"] = _utcnow().isoformat()
        le["entry_order_type"] = "limit"
        le["entry_limit_price"] = entry_limit_str
        # FIX C (in-flight proxy shape/multiplier-awareness): persist THIS order's REAL
        # shape-aware $-at-risk so a sibling fill-burst charges the actual per-order risk
        # (multiplier-aware) instead of a flat per-trade fallback. Mirrors the held
        # aggregate basis (entry-stop)*qty and the admission's FULL-intended-risk basis
        # (anticipation_full_qty so the probe+remainder count as ONE unit). Best-effort
        # side channel; sum_inflight_entry_risk_usd falls back to the flat estimate if
        # absent, so a failure here never under-counts to $0. Gated on the SAME flags as
        # the reader so a flag-off persisted snapshot is byte-identical (nothing writes
        # or reads this key when the budget gate is off).
        if (
            getattr(settings, "chili_momentum_decouple_watching_enabled", False)
            and bool(getattr(settings, "chili_momentum_atomic_risk_budget_enabled", True))
        ):
            try:
                _il_stop_dist = float((_rf_meta or {}).get("stop_distance") or 0.0)
                _il_full_qty = float(le.get("anticipation_full_qty") or qty)
                _il_risk = _il_stop_dist * _il_full_qty
                if _il_risk > 0:
                    le["entry_inflight_risk_usd"] = _il_risk
            except (TypeError, ValueError):
                pass
        # FILL_OUTCOME_LOG (mig308): capture the REAL decision-time BBO spread at the
        # submit pulse so the fill row (and the replay) sees the spread the gate
        # actually faced, not a later NBBO snapshot. Side channel only — no behavior.
        try:
            if mid and float(mid) > 0 and bid is not None and ask is not None:
                le["entry_spread_bps_at_decision"] = max(
                    0.0, (float(ask) - float(bid)) / float(mid) * 10_000.0
                )
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        # stash the name's expected-move so the chase ceiling can vol-widen at cancel time
        le["entry_expected_move_bps"] = (None if _expected_move_bps is None else float(_expected_move_bps))
        # Stamp the thrust-confluence inputs for the vertical chase ceiling (read by
        # _vertical_thrust_confluence on a later FIX-B escalation tick). Best-effort,
        # fail-soft: absent ⇒ the confluence falls back to the halt+tape floor (0.5).
        # Source = the name's OWN persisted within-batch squeeze percentile + the raw
        # RVOL pillar from the SAME scanner row the arm-queue ranker reads (no new fetch).
        try:
            le["entry_squeeze_pct"] = _session_squeeze_rank_pct(sess, sess.symbol)
        except Exception:
            le.setdefault("entry_squeeze_pct", None)
        try:
            from .ross_momentum import _extract_pillars as _vex_pillars
            _vex = sess.execution_readiness_json if isinstance(sess.execution_readiness_json, dict) else {}
            _vex_extra = (_vex.get("extra") or {}) if isinstance(_vex, dict) else {}
            _vex_sigmap = _vex_extra.get("ross_signals") if isinstance(_vex_extra.get("ross_signals"), dict) else {}
            _vex_row = _vex_sigmap.get(str(sess.symbol or "").upper()) if isinstance(_vex_sigmap, dict) else None
            if isinstance(_vex_row, dict) and _vex_row:
                _vex_rvol, _, _, _ = _vex_pillars(_vex_row)
                le["entry_rvol"] = (float(_vex_rvol) if _vex_rvol is not None else None)
            else:
                le.setdefault("entry_rvol", None)
        except Exception:
            le.setdefault("entry_rvol", None)
        le["entry_client_order_id"] = res.get("client_order_id") or cid
        le["entry_order_id"] = res.get("order_id")
        # History: the ack-timeout may wipe the ACTIVE pointer later, but this id is
        # never forgotten — the late-fill sweep + pre-submit guard track it to a
        # terminal resolution (adopted | void). No fill can become untracked again.
        _record_entry_order_placed(le, res.get("order_id"))
        # ORDER CHUNKING (item 2): when the chunking wrapper split this entry into N child
        # blocks, fold EVERY child broker_order_id into the SAME entry-order history so the
        # existing late-fill sweep + pre-submit guard track each leg to a terminal resolution
        # (adopted | void) — no chunk leg can become an untracked stranded naked long. Absent
        # / single-order ⇒ chunk_order_ids is empty ⇒ this is a no-op (byte-identical).
        # Invariants proven in tests/test_momentum_order_path_dedupe.py (recorded-before-ok,
        # distinct cids, fail-closed-to-single, sweep adopts every leg, no double-count).
        for _chunk_oid in (res.get("chunk_order_ids") or []):
            _record_entry_order_placed(le, _chunk_oid)
        if res.get("chunk_order_ids"):
            le["entry_chunk_order_ids"] = [str(o) for o in res.get("chunk_order_ids")]
        le["entry_place_result"] = {"ok": res.get("ok"), "error": res.get("error")}
        _commit_le(sess, le)
        _emit(db, sess, "live_entry_submitted", {
            "client_order_id": le["entry_client_order_id"],
            "order_type": "limit",
            "limit_price": entry_limit_str,
            "result": res,
        })
        if not res.get("ok"):
            # ADAPTIVE ENTRY-REJECT COOLDOWN (2026-06-22): the broker REFUSED this entry
            # (place_equity_order isError — a leveraged/inverse ETF tripping
            # EQUITY_SUITABILITY like RKLZ/CORD, or a name untradable in the session).
            # It will reject again the instant it re-arms; tell auto-arm to sit it out so
            # the lane stops looping arm->break->reject->reap and a FILLABLE mover gets the
            # slot. Lazy import dodges a load-time cycle; best-effort (never block the
            # error transition). Equity OR crypto — any rail that refuses an entry.
            try:
                from .auto_arm import _write_entry_reject_cooldown

                # Pass the broker's rejection text so the 24h-eligibility negative cache can
                # self-heal on an "untradable for 24 hour trading" reject (TIER-2 backstop).
                _write_entry_reject_cooldown(
                    str(sess.symbol or "").upper(), reason=str(res.get("error") or "")
                )
            except Exception:
                pass
            # AGENTIC-TRADABILITY PRE-FILTER (learn-from-401): if THIS entry-place returned a
            # per-instrument 401 / not-available-for-agentic-trading on the agentic MCP rail
            # (CTNT 2026-06-29), record the symbol non-agentic-tradeable so auto-arm SKIPS it
            # at selection (it will never fill on this rail until RH re-enables it — stop
            # burning the single slot looping arm->break->401). Scoped to the agentic family;
            # the matcher excludes whole-rail token-revoked 401s (those are not per-symbol).
            # Best-effort + flag-gated inside the recorder (flag-off => no-op, byte-identical).
            try:
                if str(sess.execution_family or "") == "robinhood_agentic_mcp":
                    from .auto_arm import (
                        _record_agentic_non_tradeable,
                        is_agentic_unauthorized_reject,
                    )

                    if is_agentic_unauthorized_reject(str(res.get("error") or "")):
                        _record_agentic_non_tradeable(str(sess.symbol or "").upper())
            except Exception:
                pass
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": res.get("error") or "place_failed"}
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT):
        pos = le.get("position")
        if not isinstance(pos, dict):
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "position_missing"}

        qty = float(pos["quantity"])
        avg = float(pos["avg_entry_price"])
        stop_px = float(pos["stop_price"])
        target_px = float(pos["target_price"])
        # ── A2 RISK-ENVELOPE DISPLACEMENT — APPLY an enqueued stop-TIGHTEN ────────────
        # When the aggregate-open-risk cap blocked a TOP-RANKED candidate, the risk gate
        # enqueued a stop-tighten request onto THIS (largest at-risk) position, to its OWN
        # already-computed most-defensive trail candidate. Apply it HERE via INVARIANT-A
        # compose max(candidate, current) — the exit machinery owns the stop, so the
        # displacement expresses itself here, never as a force-liquidation. One-shot: the
        # request is consumed after applying. Flag OFF / no request / non-tightening => no-op
        # (byte-identical). Fail-safe: any error is swallowed and the tick proceeds.
        try:
            _disp_req = le.get("pending_risk_displacement_tighten") if isinstance(le, dict) else None
            if (
                isinstance(_disp_req, dict)
                and bool(getattr(settings, "chili_momentum_risk_envelope_displacement_enabled", True))
            ):
                _disp_cand = _float_or_none(_disp_req.get("candidate_stop"))
                # INVARIANT-A: only ever TIGHTEN (raise) the long stop; never loosen.
                if _disp_cand is not None and math.isfinite(_disp_cand) and _disp_cand > stop_px:
                    _old_disp_stop = stop_px
                    stop_px = _disp_cand
                    pos["stop_price"] = stop_px
                    le["position"] = pos
                    le.pop("pending_risk_displacement_tighten", None)
                    _commit_le(sess, le)
                    _emit(db, sess, "risk_envelope_displacement_tighten", {
                        "old_stop": _old_disp_stop,
                        "new_stop": stop_px,
                        "for_candidate": _disp_req.get("for_candidate"),
                        "freed_usd": _disp_req.get("freed_usd"),
                    })
                else:
                    # A stale / non-tightening request is consumed (one-shot) so it can't linger.
                    le.pop("pending_risk_displacement_tighten", None)
                    _commit_le(sess, le)
        except Exception:
            _log.debug("[momentum_live] risk-envelope displacement apply skipped", exc_info=True)
        # Ross runner: track the high-water mark (peak bid) each tick so the
        # trailing chandelier stop can ratchet up off it. Frozen in the position.
        _hwm_prev = _float_or_none(pos.get("high_water_mark"))
        _hwm = max(_hwm_prev if _hwm_prev is not None else avg, float(bid))
        if _hwm_prev is None or _hwm > _hwm_prev:
            pos["high_water_mark"] = _hwm
            le["position"] = pos
            _commit_le(sess, le)
        pending_exit_reason = le.get("pending_exit_reason")
        if pending_exit_reason:
            try:
                pending_qty = float(le.get("pending_exit_quantity") or qty)
            except (TypeError, ValueError):
                pending_qty = qty
            is_scale_out = bool(le.get("pending_exit_is_scale_out"))
            poll = _poll_live_exit_fill(
                db,
                sess,
                adapter,
                le=le,
                reason=str(pending_exit_reason),
                quantity=min(max(pending_qty, 0.0), qty),
            )
            if poll.get("filled"):
                slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
                if is_scale_out:
                    # Deliberate first-target scale-out confirmed on a later tick:
                    # bank the partial, move the balance to breakeven, hold the runner.
                    # E(1): route through the grid step when a multi-level ladder is in
                    # force (advances the rung); else the single scale-out (byte-identical).
                    _step = _scale_out_grid_step if _scale_grid_active(pos, sess.symbol) else _scale_out_to_runner
                    _step(
                        db,
                        sess,
                        le=le,
                        filled_quantity=min(max(pending_qty, 0.0), qty),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                    )
                else:
                    _complete_confirmed_live_exit(
                        db,
                        sess,
                        le=le,
                        quantity=min(max(pending_qty, 0.0), qty),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                        slip_bps=slip_live,
                    )
            elif poll.get("partial"):
                if is_scale_out:
                    _step = _scale_out_grid_step if _scale_grid_active(pos, sess.symbol) else _scale_out_to_runner
                    _step(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                    )
                else:
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                    )
            db.flush()
            return {
                "ok": bool(poll.get("filled") or poll.get("partial") or poll.get("pending")),
                "session_id": sess.id,
                "state": sess.state,
                "pending_exit": bool(poll.get("pending")),
                "partial_exit": bool(poll.get("partial")),
                "exit_failed": bool(poll.get("failed")),
            }
        opened_raw = pos.get("opened_at_utc")
        try:
            t0 = datetime.fromisoformat(str(opened_raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            t0 = _utcnow()
        held = (_utcnow() - t0).total_seconds()
        trail_activate_return = 1.0 + float(params["trail_activate_return_bps"]) / 10_000.0

        # GAP 4 CONSECUTIVE-HALT-DOWN LIQUIDATE (SS101-062 ZJYL/HKD halt-ladder trap):
        # a name that prints CONSECUTIVE down-halts (each halt RESUMES LOWER = a cascading
        # limit-down death-spiral) is a trap — stand aside rather than hold. Uses the lane's
        # existing halt lifecycle (suspected_halt_since_utc set at the stale-quote onset,
        # halt_resumed_at_utc stamped on the resume): we stamp the last FRESH bid before a
        # halt, and on each NEW resume compare the resume bid to that pre-halt ref. A resume
        # LOWER increments the consecutive-down counter; a flat/up resume RESETS it. At/above
        # the threshold we LIQUIDATE via the SAME bailout exit machinery. RISK-REDUCING ONLY:
        # it can ONLY force an EXIT of an already-held position (never opens/sizes/holds);
        # flag OFF (default) => the whole block is skipped => byte-identical.
        if (
            bool(getattr(settings, "chili_momentum_halt_down_cascade_liquidate_enabled", False))
            and st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING)
            and bid is not None
            and math.isfinite(float(bid))
            and float(bid) > 0
        ):
            _in_halt = bool(le.get("suspected_halt_since_utc"))
            if not _in_halt:
                # Fresh, non-halted tick: remember the last good bid as the pre-halt ref.
                le["halt_down_pre_ref_bid"] = float(bid)
            _resume_marker = le.get("halt_resumed_at_utc")
            _last_processed = le.get("halt_down_last_resume_utc")
            if _resume_marker and _resume_marker != _last_processed:
                # A halt has just RESUMED — classify it as down / flat-up exactly once.
                le["halt_down_last_resume_utc"] = _resume_marker
                _pre_ref = _float_or_none(le.get("halt_down_pre_ref_bid"))
                _cnt = int(le.get("halt_down_consecutive_count") or 0)
                if _pre_ref is not None and _pre_ref > 0 and float(bid) < _pre_ref * (1.0 - 1e-9):
                    _cnt += 1  # resumed LOWER -> a down-halt in the cascade
                    le["halt_down_consecutive_count"] = _cnt
                    _emit(db, sess, "halt_down_detected", {
                        "consecutive": _cnt, "pre_halt_bid": round(_pre_ref, 6),
                        "resume_bid": round(float(bid), 6),
                    })
                else:
                    _cnt = 0  # resumed flat/up -> the cascade broke, reset
                    le["halt_down_consecutive_count"] = 0
                # Reset the pre-ref so the NEXT halt measures from this resume's price.
                le["halt_down_pre_ref_bid"] = float(bid)
                _commit_le(sess, le)
                _threshold = int(getattr(settings, "chili_momentum_halt_down_cascade_threshold", 2) or 2)
                if _cnt >= max(2, _threshold) and st != STATE_LIVE_BAILOUT:
                    _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                    _emit(db, sess, "live_bailout", {
                        "reason": "halt_down_cascade_liquidate",
                        "consecutive_halt_downs": _cnt,
                        "threshold": max(2, _threshold),
                        "unrealized_pnl": (float(bid) - avg) * qty,
                    })
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state}

        # C1: Per-trade loss enforcement
        max_loss_usd = float(caps.get("max_loss_per_trade_usd") or 0)
        if max_loss_usd > 0 and st != STATE_LIVE_BAILOUT:
            unrealized_pnl = (bid - avg) * qty
            # FRESH-QUOTE GUARD (2026-06-30, PULLBACK-SCALP-ENABLE): the C1 1x max-loss
            # force-exit had NO fresh-quote guard (unlike the C1b #769 circuit just below),
            # so a torn/stale/zero bid (bid = float(tick.bid or mid) — falls back to a stale
            # mid) trips a SPURIOUS full liquidation on a phantom unrealized loss while the
            # real NBBO is fine (CELZ 9920: phantom unrealized=-$148 while the real bid was
            # >= $4.22 / +18%). Reuses the EXACT C1b fresh-quote predicate. When the guard is
            # ON and the quote is NOT fresh, SKIP C1 this pulse — the structural stop + the
            # fresh-guarded C1b still protect, and the next FRESH tick re-checks. A genuine
            # -max_loss on a FRESH bid still fires C1 immediately. Flag OFF => byte-identical
            # (C1 fires regardless of freshness).
            _c1_fresh_quote = (
                bid is not None
                and math.isfinite(float(bid))
                and float(bid) > 0
                and int(le.get("halt_stale_streak") or 0) == 0
                and not le.get("suspected_halt_since_utc")
            )
            _c1_guard_on = bool(
                getattr(settings, "chili_momentum_max_loss_fresh_quote_guard_enabled", True)
            )
            _c1_skip_stale = _c1_guard_on and not _c1_fresh_quote
            if unrealized_pnl <= -max_loss_usd and not _c1_skip_stale:
                # IQFeed TICK-LEVEL CROSS-CHECK (2026-06-30): on the C1-trigger path ONLY
                # (a breach is rare — a small indexed read is fine; NOT queried every tick),
                # cross-check the in-process bid against the freshest IQFeed NBBO mirrored
                # into momentum_nbbo_spread_tape. If the in-process bid is MATERIALLY below a
                # FRESH tape bid (adaptive tolerance = mult x the name's recent median spread;
                # documented-fallback bps when absent), the in-process tick is torn/stale and
                # the loss is PHANTOM — SKIP C1 this pulse (CELZ-9920: in-process −$148 while
                # IQFeed showed $4.22+). Complements the binary stale-flag guard above (both
                # gated by the SAME kill-switch). FAIL-CLOSED toward firing: missing/thin/
                # confirming tape => not phantom => C1 fires (a real loss is never suppressed).
                _c1_phantom = False
                if _c1_guard_on:
                    try:
                        _c1_phantom, _c1_iq_dbg = _c1_iqfeed_phantom_loss(
                            db, sess.symbol, in_process_bid=float(bid),
                        )
                    except Exception:
                        _c1_phantom, _c1_iq_dbg = False, {"checked": False}
                    if _c1_phantom:
                        _emit(db, sess, "max_loss_per_trade_phantom_skip", {
                            "reason": "iqfeed_nbbo_divergence",
                            "in_process_bid": bid,
                            "unrealized_pnl": unrealized_pnl,
                            "cross_check": _c1_iq_dbg,
                        })
                if not _c1_phantom:
                    _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                    _emit(db, sess, "live_bailout", {"reason": "max_loss_per_trade", "unrealized_pnl": unrealized_pnl})
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state}

        # C1b: HARD MAX-LOSS-PER-TRADE CIRCUIT (#1 profitability lever, 2026-06-17).
        # The 1x C1 check above transitions to BAILOUT, but the bid-relative ladder then
        # chases a falling/gapped book and fills 5-9% deep (the -$697.76 RH low-float
        # tail: MTEN/SDOT/CCTG/CAST gapped THROUGH their tight stops). This circuit caps
        # each trade's loss at K x the REALIZED STRUCTURAL RISK (stop_distance x qty — NOT
        # the frozen risk_usd budget, ~12x overstated) and flattens at an ABSOLUTE loss
        # anchor (avg - K*stop_distance) via a single capped limit (no repeg), so a deep
        # gap-through fill is mechanically impossible. Fires INSIDE the 1x window when the
        # structural threshold is the tighter cap. Guarded: flag, state (not BAILOUT),
        # double-fire, fresh-quote (this tick was not stale), and a usable basis.
        if (
            getattr(settings, "chili_momentum_max_loss_circuit_enabled", True)
            and st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING)
            and not le.get("max_loss_circuit_fired")
        ):
            # Fresh-quote gate: a finite positive bid AND this tick did NOT register a
            # stale-quote streak (the halt-stale counter is reset to 0 by a fresh tick
            # earlier this pulse; >=1 means staleness — never fire on a frozen/halted book).
            _fresh_quote = (
                bid is not None
                and math.isfinite(float(bid))
                and float(bid) > 0
                and int(le.get("halt_stale_streak") or 0) == 0
                and not le.get("suspected_halt_since_utc")
            )
            if _fresh_quote:
                # Basis = the REALIZED per-share structural stop distance frozen at entry.
                _stop_distance = None
                _es = le.get("entry_sizing")
                if isinstance(_es, dict):
                    try:
                        _sd = float(_es.get("stop_distance"))
                        if _sd > 0 and math.isfinite(_sd):
                            _stop_distance = _sd
                    except (TypeError, ValueError):
                        _stop_distance = None
                # Fallback: derive the structural distance from the live stop price.
                if _stop_distance is None:
                    try:
                        _sd2 = float(avg) - float(pos["stop_price"])
                        if _sd2 > 0 and math.isfinite(_sd2):
                            _stop_distance = _sd2
                    except (TypeError, ValueError, KeyError):
                        _stop_distance = None
                if _stop_distance is not None:
                    _k = float(getattr(settings, "chili_momentum_max_loss_risk_multiple", 2.0) or 2.0)
                    # GUARD #1 (risk-neutral pyramid): when this position has been
                    # pyramided, le["pyramid_risk_anchor_usd"] holds the STARTER's
                    # original structural risk R0. Passing it clamps the #769 circuit
                    # threshold to R0, so the ENLARGED qty cannot re-base the floor to
                    # k*sd*q1 (~3-4.5x R0) — the enlarged worst-case stays <= R0. None
                    # (no pyramid) => byte-identical legacy circuit (floor == avg-k*sd).
                    _circuit = max_loss_circuit_decision(
                        avg=avg, qty=qty, stop_distance=_stop_distance, bid=bid, k=_k,
                        risk_anchor_usd=le.get("pyramid_risk_anchor_usd"),
                    )
                    if _circuit.get("breach"):
                        le["max_loss_circuit_fired"] = True
                        le["max_loss_circuit_floor_price"] = _circuit["floor_price"]
                        # GAP 1 (DECOUPLED): a #769 max-loss-circuit fire is the bot's own
                        # mechanical per-trade stop doing its job on ONE position — NOT the
                        # PSY101 human-tilt signature. It does NOT arm the cross-day lockout.
                        # Same-day controls (per-broker daily cap / giveback / green-to-red /
                        # consecutive-loss halt) still bound the rest of the session.
                        # EQUITY-FIRST: the absolute floor + repeg-skip apply to the RH
                        # EQUITY paths only (where the gap-through tail lives) — BOTH the
                        # unofficial robin_stocks rail (robinhood_spot) AND the sanctioned
                        # Agentic Trading MCP rail (robinhood_agentic_mcp), which trade the
                        # SAME RH low-float names with the SAME gap-through risk (LULD halts,
                        # overnight gaps). Crypto (-USD) may fire but keeps the bid-relative
                        # ladder (dust, 24/7, no LULD).
                        if normalize_execution_family(sess.execution_family) in (
                            EXECUTION_FAMILY_ROBINHOOD_SPOT,
                            EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
                        ):
                            le["exit_floor_anchored"] = True
                        _commit_le(sess, le)
                        _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                        _emit(db, sess, "live_bailout", {
                            "reason": "max_loss_circuit",
                            "unrealized_pnl": _circuit["unrealized_pnl"],
                            "structural_risk_usd": _circuit["structural_risk_usd"],
                            "threshold_usd": _circuit["threshold_usd"],
                            "floor_price": _circuit["floor_price"],
                            "risk_multiple_used": _k,
                        })
                        db.flush()
                        return {"ok": True, "session_id": sess.id, "state": sess.state}

        # EARLY TRAIL-ARM (2026-06-30, PULLBACK-SCALP-ENABLE): a CONFIRMED front-side runner
        # must reach STATE_LIVE_TRAILING to open the ride+add / micro-reentry path (all 4
        # add/reload paths — pyramid_add, micropullback_reentry, pullback_add,
        # flag_breakout_add — gate on st == STATE_LIVE_TRAILING). Today the trail-activation
        # transition sits AFTER the ENTERED-only no-confirmation bailouts
        # (instant_bid_above_fill_unconfirmed, bail_on_no_confirmation), which run first each
        # tick and return early — so a normal entry goes ENTERED -> BAILOUT -> recycle and
        # NEVER reaches TRAILING => 0 adds (the add path is structurally unreachable). Arm
        # TRAILING on the SAME adaptive condition (bid >= avg * trail_activate_return) BEFORE
        # those bailouts can cut. ANTI-REGRESSION (must not hold a loser): arms ONLY when
        # bid >= avg*trail_activate_return — the position is already in profit ABOVE the
        # activation band; a position at/below entry is untouched and STILL gets the
        # no-confirmation cut downstream. The structural stop + the #769 C1b circuit are
        # evaluated ABOVE this block (every tick) and are NOT gated by it. The add predicates
        # downstream are fail-closed (paper_execution.pullback_add_decision knife-guard; a
        # missing front_side_strength/OFI/support => no add) and pyramid_blend re-bases the
        # #769 circuit to the starter R0, so an add cannot deepen a loss beyond R0. Uses the
        # adaptive params["trail_activate_return_bps"] (no magic numbers). The existing
        # post-bailout trail-arm is preserved + idempotent (it guards on st == ENTERED, so it
        # no-ops once armed here). Flag OFF (default-on kill-switch) => this block is skipped
        # => byte-identical (trail arms only at its current post-bailout site).
        if (
            bool(getattr(settings, "chili_momentum_early_trail_arm_enabled", True))
            and st == STATE_LIVE_ENTERED
            and bid is not None
            and math.isfinite(float(bid))
            and float(bid) >= avg * trail_activate_return
        ):
            _safe_transition(db, sess, STATE_LIVE_TRAILING)
            _emit(db, sess, "live_trailing_armed", {"bid": bid, "early_arm": True})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # #2 Breakout-or-bailout fast exit (Ross flat-top): within the early window
        # after a pullback_break entry, if the broken breakout level fails to HOLD on
        # the bid, cut NOW — well inside the structural stop — reusing the BAILOUT
        # machinery (the next tick flattens). Guarded so it never fights the normal
        # stop/target: only with a recorded breakout level (pullback_break entry, not
        # the momentum_volume fallback), only while plainly ENTERED (scaling/trailing
        # are already past target/in profit), and only inside the time window.
        # GATE 2 (explosive-mover recalibration): the fast-bail gets a LOCK-IN floor —
        # below it a momentary sub-level dip is NOT treated as a failed breakout (give
        # the breakout structural room for a normal retest; FCUV +21% after a 4.5s bail).
        # The lock-in is master-gated (0 = byte-identical) and WIDER for explosive names.
        # CRITICAL: the structural stop + the #769 max-loss circuit are evaluated ABOVE
        # this block (and re-run every tick), so a genuinely collapsing position still
        # exits inside the lock-in — only the level-retest fast-bail is deferred.
        # GAP-A — SMART POST-ENTRY HOLD. When the master flag is ON, replace the FIXED
        # 0.001 wick buffer with a VOL-ADAPTIVE band on the name's live realized vol scaled
        # to the holding horizon (dimensionally: band_frac = k*rv_live*sqrt(N), N =
        # expected_hold_s/grid_secs = GRID STEPS — the SAME √-time rule the vol-norm exit
        # uses, NOT a tick count), and gate the actual CUT on order-flow + a volume-confirmed
        # breach + an adaptive time-floor. INVARIANT-A: this governs ONLY the early
        # level-retest fast-bail and can only TIGHTEN/SUPPRESS it; the structural stop + the
        # #769 max-loss circuit are evaluated ABOVE this block (every tick) and are NOT gated
        # by it, so a genuinely collapsing position still exits. Flag OFF (default) ⇒ the
        # fixed-0.001-buffer path below runs BYTE-IDENTICAL.
        _bb_lock_in = _breakout_bailout_lock_in_seconds(explosive=_session_is_explosive(via))
        _smart_hold_on = bool(getattr(settings, "chili_momentum_smart_hold_enabled", False))
        if (
            _smart_hold_on
            and st == STATE_LIVE_ENTERED
            and bool(getattr(settings, "chili_momentum_breakout_bailout_enabled", True))
            and le.get("breakout_level_price") is not None
            and bid is not None
        ):
            _sh_fired = False
            try:
                _sh_window = _breakout_bailout_window_seconds()
                _bk_lvl = float(le.get("breakout_level_price"))
                _anchor = max(_bk_lvl, float(avg))
                # ── vol-adaptive band (the corrected √-time, N in GRID STEPS) ──
                from .pipeline import _live_realized_vol, _live_flow_slope

                _sh_rv = _live_realized_vol(sess.symbol, db=db)
                _bar_secs = float(
                    getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15
                )
                _k_atr = float(getattr(settings, "chili_momentum_smart_hold_k_atr", 1.2) or 1.2)
                _k = _k_atr * 1.2533  # mean-abs→stdev half-width (sqrt(pi/2))
                # P4(2) SQUEEZE-AWARE HOLD: an EXTREME-tail squeeze name WIDENS the candidate band
                # (k up by a bounded percentile factor) so the fueled runner extends. INVARIANT-A
                # SAFE — this widens the CANDIDATE band BEFORE smart_hold_decision derives the hold
                # floor; the decision never loosens a PLACED stop. Factor 1.0 ⇒ byte-identical.
                _sq_widen_sh = _squeeze_exit_band_widen_factor(via, sess.symbol)
                if _sq_widen_sh > 1.0:
                    _k = _k * _sq_widen_sh
                    le["squeeze_fuel_hold_widen"] = {"factor": round(_sq_widen_sh, 4), "k": round(_k, 5)}
                if _sh_rv is not None and _sh_rv.get("rv_step") is not None:
                    _sh_hold = _recent_scalp_median_hold_s(db, int(sess.user_id))
                    if _sh_hold is None or _sh_hold <= 0:
                        _sh_hold = max(2.0 * _bar_secs, float(held or 0.0))
                    _band_frac = smart_hold_band_frac(
                        rv_live=float(_sh_rv["rv_step"]),
                        expected_hold_s=float(_sh_hold),
                        grid_secs=float(_sh_rv.get("grid_secs") or 2.0),
                        k=_k,
                    )
                else:
                    # Thin tape ⇒ fall back to the fixed buffer width so the band is never
                    # tighter than the legacy 0.001 (no spurious early cut on missing data).
                    _band_frac = float(
                        getattr(settings, "chili_momentum_breakout_bailout_buffer_pct", 0.001) or 0.0
                    )
                # ── adaptive flow thresholds (lower-tail of the name's own OFI dist) ──
                _sh_fs = _live_flow_slope(sess.symbol, db=db)
                _t_floor = float(getattr(settings, "chili_momentum_smart_hold_t_flow_floor", 0.25) or 0.25)
                _s_floor = float(getattr(settings, "chili_momentum_smart_hold_s_flow_floor", 0.0) or 0.0)
                _ofi_level = _sh_fs.get("ofi_level") if _sh_fs else None
                _ofi_slope = _sh_fs.get("ofi_slope") if _sh_fs else None
                _tick_rate = _sh_fs.get("tick_rate") if _sh_fs else None
                # ── adaptive time-floor (q25 of recent break-resolution times) ──
                _tfloor_s = _smart_hold_time_floor_s(db, int(sess.user_id))
                if _tfloor_s is None or _tfloor_s <= 0:
                    _tfloor_s = 2.0 * _bar_secs
                # ── volume-confirmed breach (the name's own recent per-window dist) ──
                _bvol, _bvol_med = _smart_hold_breach_volume(
                    db, sess.symbol, window_s=max(1.0, _bar_secs)
                )
                _sh = smart_hold_decision(
                    anchor=_anchor,
                    bid=float(bid),
                    band_frac=_band_frac,
                    held_seconds=held,
                    window_seconds=_sh_window,
                    time_floor_s=_tfloor_s,
                    ofi_level=_ofi_level,
                    ofi_slope=_ofi_slope,
                    t_flow=_t_floor,
                    s_flow=_s_floor,
                    tick_rate=_tick_rate,
                    tick_rate_ref=_float_or_none(le.get("entry_tick_rate")),
                    rho=float(getattr(settings, "chili_momentum_smart_hold_rho", 0.6) or 0.6),
                    breach_volume=_bvol,
                    breach_volume_median=_bvol_med,
                )
                _emit(db, sess, "smart_hold_decision", {
                    "cut": bool(_sh.cut),
                    "hold": bool(_sh.hold),
                    "reason": _sh.reason,
                    "band_frac": _sh.band_frac,
                    "hold_floor_px": _sh.hold_floor_px,
                    "anchor": _anchor,
                    "bid": bid,
                    "held_seconds": held,
                    "window_seconds": _sh_window,
                    "time_floor_s": _tfloor_s,
                    "time_floor_suppressed": bool(_sh.time_floor_suppressed),
                    "ofi_level": _ofi_level,
                    "ofi_slope": _ofi_slope,
                    "tick_rate": _tick_rate,
                    "breach_volume": _bvol,
                    "breach_volume_median": _bvol_med,
                })
                if _sh.cut:
                    le["last_bailout_trigger"] = "smart_hold_cut"
                    _commit_le(sess, le)
                    _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                    _emit(db, sess, "live_bailout", {
                        "reason": "smart_hold_fast_bail",
                        "smart_hold_reason": _sh.reason,
                        "breakout_level": _bk_lvl,
                        "anchor": _anchor,
                        "bid": bid,
                        "held_seconds": held,
                        "band_frac": _sh.band_frac,
                        "hold_floor_px": _sh.hold_floor_px,
                        "window_seconds": _sh_window,
                    })
                    db.flush()
                    _sh_fired = True
                    return {"ok": True, "session_id": sess.id, "state": sess.state}
            except Exception:
                # On ANY error, fall through to the legacy fixed-buffer path below
                # (never strand a position because the adaptive read failed).
                _sh_fired = False
            if _sh_fired:
                return {"ok": True, "session_id": sess.id, "state": sess.state}
        elif (
            st == STATE_LIVE_ENTERED
            and bool(getattr(settings, "chili_momentum_breakout_bailout_enabled", True))
            and breakout_failed_to_hold(
                breakout_level=le.get("breakout_level_price"),
                bid=bid,
                held_seconds=held,
                window_seconds=_breakout_bailout_window_seconds(),
                buffer_pct=float(getattr(settings, "chili_momentum_breakout_bailout_buffer_pct", 0.001) or 0.0),
                lock_in_seconds=_bb_lock_in,
            )
        ):
            le["last_bailout_trigger"] = "breakout_failed_to_hold"
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_BAILOUT)
            _emit(db, sess, "live_bailout", {
                "reason": "breakout_failed_fast_bail",
                "breakout_level": le.get("breakout_level_price"),
                "bid": bid,
                "held_seconds": held,
                "window_seconds": _breakout_bailout_window_seconds(),
                "lock_in_seconds": _bb_lock_in,
            })
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # GAP2 — INSTANT BID-BELOW-FILL CUT (Warrior re-audit 2026-06-26). Right after
        # the fill, if the live bid has collapsed BELOW the fill by more than spread
        # noise (the move failed at the entry tick), cut FAST via the BAILOUT machinery
        # — well inside the structural stop. ENTERED-only (scaling/trailing are already
        # in profit, past their target partial). Fresh-quote gated (never on a stale/
        # halted book). Flag OFF (default) ⇒ this whole block is a no-op ⇒ byte-identical.
        # PROTECTIVE: only cuts a FAILED entry faster; it never widens any stop.
        if (
            st == STATE_LIVE_ENTERED
            and bool(getattr(settings, "chili_momentum_instant_bid_below_fill_cut_enabled", False))
            and bid is not None
            and math.isfinite(float(bid))
            and float(bid) > 0
            and int(le.get("halt_stale_streak") or 0) == 0
            and not le.get("suspected_halt_since_utc")
            and instant_bid_below_fill_cut(
                entry_price=avg,
                bid=float(bid),
                held_seconds=held,
                window_seconds=float(getattr(settings, "chili_momentum_instant_bid_cut_window_seconds", 6.0) or 0.0),
                margin_bps=float(getattr(settings, "chili_momentum_instant_bid_cut_margin_bps", 25.0) or 0.0),
            )
        ):
            le["last_bailout_trigger"] = "instant_bid_below_fill"
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_BAILOUT)
            _emit(db, sess, "live_bailout", {
                "reason": "instant_bid_below_fill_cut",
                "entry_price": avg,
                "bid": bid,
                "held_seconds": held,
                "window_seconds": float(getattr(settings, "chili_momentum_instant_bid_cut_window_seconds", 6.0) or 0.0),
                "margin_bps": float(getattr(settings, "chili_momentum_instant_bid_cut_margin_bps", 25.0) or 0.0),
            })
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # LOCATE #7 — INSTANT BID-ABOVE-FILL CONFIRM (positive mirror of the below-fill
        # cut). At the END of the confirm window, if the bid never held at/above the fill
        # AND the position never printed a confirming high, the entry did NOT confirm —
        # FEED the SAME bailout machinery (no new exit reason; the same protective cut).
        # ENTERED-only, fresh-quote gated. Reuses the instant-bid margin/window knobs.
        # Flag OFF (default) ⇒ no-op ⇒ byte-identical. PROTECTIVE: it can only cut a
        # non-confirming entry sooner; never widens a stop.
        if (
            st == STATE_LIVE_ENTERED
            and bool(getattr(settings, "chili_momentum_instant_bid_above_fill_confirm_enabled", False))
            and bid is not None
            and math.isfinite(float(bid))
            and float(bid) > 0
            and int(le.get("halt_stale_streak") or 0) == 0
            and not le.get("suspected_halt_since_utc")
            and instant_bid_above_fill_confirm_failed(
                entry_price=avg,
                bid=float(bid),
                high_water_mark=_float_or_none(pos.get("high_water_mark")),
                held_seconds=held,
                window_seconds=float(getattr(settings, "chili_momentum_instant_bid_confirm_window_seconds", 6.0) or 0.0),
                margin_bps=float(getattr(settings, "chili_momentum_instant_bid_cut_margin_bps", 25.0) or 0.0),
            )
        ):
            le["last_bailout_trigger"] = "instant_bid_above_fill_unconfirmed"
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_BAILOUT)
            _emit(db, sess, "live_bailout", {
                "reason": "instant_bid_above_fill_unconfirmed",
                "entry_price": avg,
                "bid": bid,
                "high_water_mark": _float_or_none(pos.get("high_water_mark")),
                "held_seconds": held,
                "window_seconds": float(getattr(settings, "chili_momentum_instant_bid_confirm_window_seconds", 6.0) or 0.0),
            })
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # LOCATE #1 — SUB-5MIN SCALP BAILOUT (scalp-family fast time-stop). Reads the SAME
        # deployed cadence classifier result the runner trail persists (le["cadence_cls"],
        # written every held tick with its FULL ema/rvol signals — ONE source of truth, no
        # duplicate classifier): only a SLOW_CHOPPER (a scalp that is NOT extending; a FAST
        # runner is NEVER time-stopped) that is held >= the scalp max-hold AND is NOT green
        # (bid <= entry) is time-stopped via the SAME bailout machinery. ENTERED-only (a
        # scaled/trailing position is already past its first target = in profit, never a
        # stalled scalp), fresh-quote gated. Flag OFF (default) ⇒ no-op ⇒ byte-identical.
        # PROTECTIVE: it can only exit a stalled scalp sooner; it never widens a stop or
        # admits risk. (The classifier is conservative: it falls to UNCERTAIN — no time-stop
        # — unless velocity is decisively slow AND the trail's trend/vol read agrees.)
        if (
            st == STATE_LIVE_ENTERED
            and bool(getattr(settings, "chili_momentum_sub5min_scalp_bailout_enabled", False))
            and bid is not None
            and math.isfinite(float(bid))
            and float(bid) > 0
            and int(le.get("halt_stale_streak") or 0) == 0
            and not le.get("suspected_halt_since_utc")
            and float(bid) <= float(avg)  # NOT green — a green scalp rides the trail, not a time-stop
            and str(le.get("cadence_cls") or "") == "SLOW_CHOPPER"
        ):
            _scalp_max_min = float(getattr(settings, "chili_momentum_sub5min_scalp_bailout_minutes", 5.0) or 0.0)
            if _scalp_max_min > 0.0 and held >= _scalp_max_min * 60.0:
                le["last_bailout_trigger"] = "sub5min_scalp_timestop"
                _commit_le(sess, le)
                _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                _emit(db, sess, "live_bailout", {
                    "reason": "sub5min_scalp_bailout",
                    "entry_price": avg,
                    "bid": bid,
                    "held_seconds": held,
                    "scalp_max_minutes": _scalp_max_min,
                    "cadence_cls": le.get("cadence_cls"),
                })
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}

        # GAP1 — BAIL ON ABSENCE-OF-STRENGTH (affirmative breakout-or-bailout, Warrior
        # re-audit 2026-06-26). Within [min_hold, window] seconds of the fill, if the
        # breakout showed NO confirming strength — no new high above the confirm buffer
        # AND the bid is at/below entry AND the tape is not accelerating up — the thesis
        # did not confirm, so BAIL before the stop via the BAILOUT machinery. A winner
        # that pops then consolidates (high-water mark above the buffer) is IMMUNE. The
        # OFI read is fail-open (None ⇒ governed by the price conditions). ENTERED-only,
        # fresh-quote gated. Flag OFF (default) ⇒ no-op ⇒ byte-identical. PROTECTIVE only.
        if (
            st == STATE_LIVE_ENTERED
            and bool(getattr(settings, "chili_momentum_bail_on_no_confirmation_enabled", False))
            and bid is not None
            and math.isfinite(float(bid))
            and float(bid) > 0
            and int(le.get("halt_stale_streak") or 0) == 0
            and not le.get("suspected_halt_since_utc")
        ):
            _nc_ofi = None
            try:
                from .pipeline import _live_ofi_microprice as _nc_ofi_fn

                _nc_ofi, _ = _nc_ofi_fn(sess.symbol, db=db)
            except Exception:
                _nc_ofi = None
            if bail_on_no_confirmation(
                entry_price=avg,
                bid=float(bid),
                high_water_mark=_float_or_none(pos.get("high_water_mark")),
                held_seconds=held,
                min_hold_seconds=float(getattr(settings, "chili_momentum_no_confirmation_min_hold_seconds", 8.0) or 0.0),
                window_seconds=float(getattr(settings, "chili_momentum_no_confirmation_window_seconds", 20.0) or 0.0),
                buffer_bps=float(getattr(settings, "chili_momentum_no_confirmation_buffer_bps", 10.0) or 0.0),
                ofi=_nc_ofi,
            ):
                le["last_bailout_trigger"] = "no_confirmation"
                _commit_le(sess, le)
                _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                _emit(db, sess, "live_bailout", {
                    "reason": "bail_on_no_confirmation",
                    "entry_price": avg,
                    "bid": bid,
                    "high_water_mark": _float_or_none(pos.get("high_water_mark")),
                    "held_seconds": held,
                    "window_seconds": float(getattr(settings, "chili_momentum_no_confirmation_window_seconds", 20.0) or 0.0),
                    "buffer_bps": float(getattr(settings, "chili_momentum_no_confirmation_buffer_bps", 10.0) or 0.0),
                    "ofi": _nc_ofi,
                })
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_BAILOUT:
            # MAX-LOSS-CIRCUIT HALT GATE (2026-06-17): a circuit-originated bailout must
            # NOT submit into a halted/frozen book — a marketable-but-capped floor cannot
            # fill while quotes are stale, and submitting risks a stranded order or a fill
            # the instant the halt lifts at a worse price. Hold in BAILOUT and re-attempt
            # on the first fresh-quote tick after resume (suspected_halt cleared by
            # _register_fresh_quote_tick). Keyed on max_loss_circuit_fired so legacy 1x /
            # stop / breakout bailouts are byte-identical (they still submit through halts).
            if le.get("max_loss_circuit_fired") and le.get("suspected_halt_since_utc"):
                _emit(db, sess, "max_loss_circuit_halt_deferred", {
                    "floor_price": le.get("max_loss_circuit_floor_price"),
                    "suspected_halt_since_utc": le.get("suspected_halt_since_utc"),
                })
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "halt_deferred": True}
            cid = f"chili_ml_b_{sess.id}_{uuid.uuid4().hex[:12]}"
            # Circuit bailouts on the RH (equity) path flatten at the ABSOLUTE loss-anchored
            # floor (no bid-relative ladder, no repeg). EQUITY-FIRST: keyed on
            # exit_floor_anchored, which the circuit sets ONLY for the RH equity rails
            # (robinhood_spot AND robinhood_agentic_mcp) — crypto circuit fires but
            # exit_floor_anchored is unset, so it falls through to None and keeps the legacy
            # bid-relative ladder (dust, 24/7, no LULD). All OTHER bailout reasons pass None
            # => byte-identical legacy ladder.
            _bailout_floor = (
                le.get("max_loss_circuit_floor_price")
                if le.get("exit_floor_anchored")
                else None
            )
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=qty,
                client_order_id=cid,
                reason="bailout",
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"unrealized_pnl_usd": (bid - avg) * qty},
                hard_floor_price=_bailout_floor,
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="bailout"):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason="bailout", quantity=qty)
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason="bailout",
                    )
                db.flush()
                return {
                    "ok": bool(poll.get("pending") or poll.get("partial")),
                    "session_id": sess.id,
                    "state": sess.state,
                    "pending_exit": bool(poll.get("pending")),
                    "partial_exit": bool(poll.get("partial")),
                    "exit_failed": bool(poll.get("failed")),
                }
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=qty,
                entry_price=avg,
                fill_price=float(poll["fill_price"]),
                reason="bailout",
                slip_bps=slip_live,
                sell_result=sr,
            )
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if float(via.viability_score or 0) < float(params["bailout_viability_floor"]):
            _safe_transition(db, sess, STATE_LIVE_BAILOUT)
            _emit(db, sess, "live_bailout", {"viability_score": via.viability_score})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # C4: Viability degradation — tighten stop if score drops >15% from admission
        admission_via = float(le.get("admission_viability_score") or 0)
        current_via = float(via.viability_score or 0)
        if admission_via > 0 and current_via < admission_via * 0.85:
            # WAVE-1 FIX-5 (B4): compose against the LIVE pos stop (not just the once-per-
            # tick cached `stop_px`) so a concurrent writer earlier in the tick can't be
            # undone here, then REFRESH `stop_px` after the write so the trailing chandelier
            # below composes off the tightened base — never re-loosens it (IREZ 36ms bug).
            _live_stop_c4 = (
                float(pos["stop_price"])
                if bool(getattr(settings, "chili_momentum_stop_ratchet_strict_enabled", True))
                else stop_px
            )
            tighter_stop = max(_live_stop_c4, avg * 0.995)
            if tighter_stop > _live_stop_c4:
                pos["stop_price"] = tighter_stop
                _commit_le(sess, le)
                _emit(db, sess, "viability_degraded_tighten", {
                    "admission_viability": admission_via,
                    "current_viability": current_via,
                    "old_stop": _live_stop_c4,
                    "new_stop": tighter_stop,
                })
                # INVARIANT-A: refresh the cached base so every later stop writer in this
                # tick (the trailing ratchet chain) composes off the tightened value.
                if bool(getattr(settings, "chili_momentum_stop_ratchet_strict_enabled", True)):
                    stop_px = tighter_stop

        if held >= max_hold:
            cid = f"chili_ml_t_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=qty,
                client_order_id=cid,
                reason="max_hold",
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"held_seconds": held, "max_hold_seconds": max_hold},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="max_hold"):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason="max_hold", quantity=qty)
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason="max_hold",
                    )
                db.flush()
                return {
                    "ok": bool(poll.get("pending") or poll.get("partial")),
                    "session_id": sess.id,
                    "state": sess.state,
                    "pending_exit": bool(poll.get("pending")),
                    "partial_exit": bool(poll.get("partial")),
                    "exit_failed": bool(poll.get("failed")),
                }
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=qty,
                entry_price=avg,
                fill_price=float(poll["fill_price"]),
                reason="max_hold",
                slip_bps=slip_live,
            )
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # ── ROSS GAP 1: LOST-VWAP → FLATTEN (held position) ──────────────────────
        # Ross's intraday line-in-the-sand: after entry, if price LOSES session VWAP in
        # a CONFIRMED way, he is OUT. None of the existing held-position exits (max-loss,
        # breakout-or-bailout, smart_hold, timestops, topping-tail, trail) compared the
        # live bid to session VWAP. This is that exit, and it runs for ALL held states
        # (ENTERED/SCALING_OUT/TRAILING) — Ross loses VWAP right after entry, not only on
        # a runner — placed AFTER max-loss/breakout-bailout/max-hold and BEFORE the
        # TRAILING-only chandelier ratchet + the entire add region below.
        #
        # ⭐ ANTI-WHIPSAW (the ONE documented confirmed-loss definition): ALL of
        #   (a) the last CLOSED bar closed BELOW session VWAP (no 1-tick intrabar undercut
        #       can fire it — a CLOSED bar is required), AND
        #   (b) the live bid is STILL below VWAP by an ADAPTIVE margin = the name's OWN
        #       close-vs-VWAP dispersion sigma * margin_sigma (NOT a fixed price), and not
        #       reclaiming, so a momentary kiss of VWAP does not flatten, AND
        #   (c) order-flow is NOT positive (the live signed tape / OFI confirms the break;
        #       None/absent ⇒ "not positive" only because (a)+(b) already prove a closed-
        #       bar structural loss — fail toward the EXIT).
        #
        # ⭐ COMPOSES WITH THE DIP-ADD (no conflict): it PRE-EMPTS the pullback-add block
        # below — this check runs FIRST and RETURNS on a confirmed loss, so the same tick
        # can never both add and flatten. A pullback that HOLDS/RECLAIMS VWAP (above_vwap
        # True / vwap_dist >= 0) is NOT a confirmed loss here, so the dip-add can still
        # fire on it. The two are mutually exclusive by construction (loss ⇒ flatten+
        # return; hold/reclaim ⇒ fall through to the dip-add).
        #
        # EXIT-only: routes through the BAILOUT machinery (set last_bailout_trigger,
        # transition to STATE_LIVE_BAILOUT, the next tick flattens) — VERBATIM with the
        # topping-tail runner exit. INVARIANT-A: an EXIT can flatten but never loosens the
        # ratchet floor (no stop is moved here). EQUITY + crypto (VWAP is computed lane-
        # wide). Flag OFF ⇒ byte-identical (no read, no emit). Fail-safe: any error is
        # swallowed so the exit path below ALWAYS runs.
        if (
            bool(getattr(settings, "chili_momentum_lost_vwap_flatten_enabled", True))
            and st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING)
            and bid is not None
            and math.isfinite(float(bid))
            and float(bid) > 0
        ):
            try:
                from .ross_momentum import front_side_state as _lv_state_fn
                from .entry_gates import _today_session_frame as _lv_today_frame

                _lv_iv = str(
                    getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m"
                )
                _lv_raw = _replay_aware_fetch_ohlcv_df(sess.symbol, interval=_lv_iv, period="5d")
                _lv_df = None
                if _lv_raw is not None and not getattr(_lv_raw, "empty", True):
                    _lv_df = _lv_today_frame(_lv_raw)
                if _lv_df is not None and not getattr(_lv_df, "empty", True):
                    _lv_state = _lv_state_fn(_lv_df)
                    _lv_vwap = _float_or_none(getattr(_lv_state, "session_vwap", None))
                    _lv_above = bool(getattr(_lv_state, "above_vwap", False))
                    _lv_dist = _float_or_none(getattr(_lv_state, "vwap_dist_sigma", None))
                    # (a) last CLOSED bar close below VWAP.
                    _lv_last_close = None
                    try:
                        _lv_last_close = float(_lv_df["Close"].astype(float).iloc[-1])
                    except Exception:
                        _lv_last_close = None
                    _lv_closed_below = (
                        _lv_vwap is not None
                        and _lv_last_close is not None
                        and _lv_last_close < _lv_vwap
                    )
                    # (b) live bid below VWAP by the ADAPTIVE dispersion-sigma margin.
                    # vwap_dist_sigma is the name's OWN (close-vwap)/sigma at the last bar;
                    # convert the requested margin (in sigma) into a PRICE margin via the
                    # implied per-sigma price = |last_close - vwap| / |dist|. When dist is
                    # absent/zero the price margin is 0 so the closed-bar loss alone still
                    # requires the bid below VWAP (no fixed-price magnitude introduced).
                    _lv_margin_sigma = float(
                        getattr(settings, "chili_momentum_lost_vwap_margin_sigma", 0.25) or 0.25
                    )
                    _lv_price_margin = 0.0
                    if (
                        _lv_vwap is not None
                        and _lv_last_close is not None
                        and _lv_dist is not None
                        and abs(_lv_dist) > 1e-9
                    ):
                        _lv_per_sigma = abs(_lv_last_close - _lv_vwap) / abs(_lv_dist)
                        _lv_price_margin = _lv_margin_sigma * _lv_per_sigma
                    _lv_bid_below_margin = (
                        _lv_vwap is not None
                        and float(bid) < (_lv_vwap - _lv_price_margin)
                        and not _lv_above
                        and not (_lv_dist is not None and _lv_dist >= 0.0)
                    )
                    # (c) order-flow NOT positive (live signed tape / OFI confirm).
                    _lv_flow_pos = False
                    try:
                        from .pipeline import (
                            _live_flow_slope as _lv_flow_fn,
                            _live_trade_flow as _lv_tape_fn,
                        )

                        _lv_tape = _lv_tape_fn(sess.symbol, db=db)
                        if _lv_tape is not None and float(_lv_tape) > 0.0:
                            _lv_flow_pos = True
                        _lv_flow = _lv_flow_fn(sess.symbol, db=db)
                        if isinstance(_lv_flow, dict):
                            _lv_ofi_lvl = _float_or_none(_lv_flow.get("ofi_level"))
                            if _lv_ofi_lvl is not None and _lv_ofi_lvl > 0.0:
                                _lv_flow_pos = True
                    except Exception:
                        _lv_flow_pos = False
                    if _lv_closed_below and _lv_bid_below_margin and not _lv_flow_pos:
                        le["last_bailout_trigger"] = "lost_vwap_flatten"
                        _commit_le(sess, le)
                        _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                        _emit(db, sess, "live_lost_vwap_flatten", {
                            "reason": "lost_vwap_confirmed",
                            "bid": float(bid),
                            "session_vwap": _lv_vwap,
                            "last_close": _lv_last_close,
                            "vwap_dist_sigma": _lv_dist,
                            "price_margin": round(_lv_price_margin, 6),
                            "margin_sigma": _lv_margin_sigma,
                            "above_vwap": _lv_above,
                            "high_water_mark": _float_or_none(pos.get("high_water_mark")),
                        })
                        db.flush()
                        return {"ok": True, "session_id": sess.id, "state": sess.state}
            except Exception:
                # Fail-safe: any lost-VWAP read error is swallowed so the exit path below
                # ALWAYS runs. The flatten NEVER blocks/delays a real stop/exit.
                _log.debug("[momentum_live] lost-VWAP flatten block error", exc_info=True)

        # ── ROSS GAP 2: LIVE CLOSE-BELOW-STRUCTURE (BOS) EXIT ────────────────────
        # Ross exits on a confirmed bar CLOSE below structure (the last confirmed swing
        # low), NOT an intrabar wick. The backtest/paper lane already has
        # bos_exit_triggered_long (entry_gates.py); the LIVE lane only had the
        # ATR/chandelier INTRABAR trail. This ports the SAME predicate onto a CLOSED-bar
        # read (the last bar's CLOSE vs the confirmed swing low), so it fires on a
        # confirmed close below structure — DISTINCT from the intrabar trail. The two
        # compose: this is an ADDITIONAL confirmed-close exit; whichever fires first wins
        # (a confirmed close-below-structure flattens HERE this tick; the intrabar trail
        # still owns the on-the-way-down chandelier).
        #
        # An intrabar WICK below the swing low whose bar CLOSES back above does NOT fire
        # (the predicate keys off the last CLOSE, not the low). EXIT-only: routes through
        # the BAILOUT machinery (VERBATIM with topping-tail / lost-VWAP) — no stop is
        # moved, so INVARIANT-A holds. EQUITY + crypto (the swing-low structure is price-
        # only). Flag OFF ⇒ byte-identical (no fetch, no emit). Fail-safe: any error is
        # swallowed so the exit path below ALWAYS runs. Held in ENTERED/TRAILING (not the
        # bailout/pending-exit states that already sell).
        if (
            bool(getattr(settings, "chili_momentum_bos_exit_live_enabled", True))
            and st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)
        ):
            try:
                from .entry_gates import bos_exit_triggered_long as _bos_fn

                _bos_iv = str(
                    getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m"
                )
                _bos_df = _replay_aware_fetch_ohlcv_df(sess.symbol, interval=_bos_iv, period="5d")
                if _bos_df is not None and not getattr(_bos_df, "empty", True):
                    _bos_close = float(_bos_df["Close"].astype(float).iloc[-1])
                    _bos_buf = float(
                        getattr(settings, "chili_momentum_bos_exit_buffer_pct", 0.003) or 0.003
                    )
                    if _bos_fn(_bos_df, current_close=_bos_close, buffer_pct=_bos_buf):
                        le["last_bailout_trigger"] = "bos_exit_live"
                        _commit_le(sess, le)
                        _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                        _emit(db, sess, "live_bos_exit", {
                            "reason": "close_below_structure",
                            "bid": float(bid),
                            "last_close": _bos_close,
                            "buffer_pct": _bos_buf,
                            "high_water_mark": _float_or_none(pos.get("high_water_mark")),
                        })
                        db.flush()
                        return {"ok": True, "session_id": sess.id, "state": sess.state}
            except Exception:
                # Fail-safe: any BOS read error is swallowed so the exit path below ALWAYS
                # runs. The BOS exit NEVER blocks/delays a real stop/exit.
                _log.debug("[momentum_live] live BOS exit block error", exc_info=True)

        # Ross runner trail: in TRAILING, ratchet the stop UP to a chandelier off
        # the high-water mark (the same ATR distance the initial stop used), floored
        # at breakeven once the first-target partial de-risked the runner. The stop
        # check below then enforces it SAME tick. Derived from the frozen entry ATR —
        # not a static floor. (docs/DESIGN/MOMENTUM_LANE.md)
        if st == STATE_LIVE_TRAILING:
            # Ross sell-into-strength: a topping-tail / shooting-star on the runner's
            # candles is momentum exhaustion — lock the tail NOW rather than waiting for
            # the chandelier trail to be hit on the way back down. Runner-only (post
            # first-target scale-out); reuses the bars already fetched for the adaptive-
            # spread check; fail-safe (no candle data -> no exit). docs/DESIGN/MOMENTUM_LANE.md
            if bool(getattr(settings, "chili_momentum_exit_topping_tail_enabled", True)):
                try:
                    from .candles import topping_tail_from_df

                    if topping_tail_from_df(_entry_df):
                        le["last_bailout_trigger"] = "topping_tail_runner"
                        _commit_le(sess, le)
                        _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                        _emit(db, sess, "live_bailout", {
                            "reason": "topping_tail_runner_exit", "bid": bid,
                            "high_water_mark": _float_or_none(pos.get("high_water_mark")),
                        })
                        db.flush()
                        return {"ok": True, "session_id": sess.id, "state": sess.state}
                except Exception:
                    pass
            _atr_pct_trail = _float_or_none(le.get("entry_stop_atr_pct")) or 0.0
            _hwm_trail = _float_or_none(pos.get("high_water_mark")) or avg
            _be_floor = avg if pos.get("partial_taken") else stop_px
            _sm = float(params.get("stop_atr_mult") or 0.60)
            _q0 = _float_or_none(pos.get("original_quantity")) or _float_or_none(pos.get("quantity")) or 0.0
            # 5m EMA9 structural anchor for the runner trail — refreshed at most
            # once per minute per session (cached in le), fail-open (None).
            _ema5 = None
            try:
                _min_key = _utcnow().strftime("%Y%m%d%H%M")
                if le.get("ema5m_min") == _min_key:
                    _ema5 = _float_or_none(le.get("ema5m_val"))
                else:
                    _e5_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)

                    _df5 = _e5_fetch(sess.symbol, interval="5m", period="1d")
                    if _df5 is not None and len(_df5) >= 9:
                        _ema5 = float(_df5["Close"].ewm(span=9, adjust=False).mean().iloc[-1])
                    le["ema5m_min"] = _min_key
                    le["ema5m_val"] = _ema5
                    _commit_le(sess, le)
            except Exception:
                _ema5 = None
            # GAP3: regime-conditioned hold-time — scale the give-back band by the
            # entry regime (HOT ⇒ wider/hold longer, COLD ⇒ tighter/cut quicker).
            # Default 1.0 (flag OFF) ⇒ byte-identical; ratchet-only ⇒ never weakens
            # the live stop. Reuses the deployed _session_is_explosive classifier.
            _regime_band_mult = _regime_holdtime_band_mult(
                explosive=_session_is_explosive(via)
            )
            _trailed = cushion_adaptive_trail_stop(
                high_water_mark=_hwm_trail,
                entry_price=avg,
                atr_pct=_atr_pct_trail,
                stop_atr_mult=_sm,
                day_realized_usd=_day_realized_usd_cached(db, int(sess.user_id)),
                position_risk_usd=(avg * max(0.003, _atr_pct_trail * _sm)) * _q0,
                breakeven_floor=_be_floor,
                current_stop=stop_px,
                side_long=True,
                ema_5m=_ema5,
                regime_band_mult=_regime_band_mult,
            )
            # LEVER 2A — MATH-VERIFIED adaptive vol-normalized trail. Re-derive the trail
            # WIDTH from LIVE tape realized vol (vs the frozen entry ATR width above) and
            # compose it through INVARIANT-A. This is an ADDITIONAL ratchet-only layer:
            # _trailed already passed through max(cs, be, ...) in the cushion helper, and
            # the vol-norm candidate is composed via volnorm_runner_trail_stop (also
            # max(cs, be, candidate)), so we take the MAX of the two ratchet candidates —
            # the result can only ever TIGHTEN the live stop, never loosen it. Flag-off or
            # a thin tape (vol read None) ⇒ _trailed is used unchanged (byte-identical).
            _vn_dist = None  # the 2A width, reused as the RIDE-LOCK base band (2B)
            if bool(getattr(settings, "chili_momentum_volnorm_trail_enabled", True)):
                try:
                    from .pipeline import _live_realized_vol
                    from .paper_execution import (
                        micro_price as _micro_price,
                        volnorm_runner_trail_stop,
                        volnorm_trail_dist_pct,
                    )

                    _rv = _live_realized_vol(sess.symbol, db=db)
                    if _rv is not None and _rv.get("rv_step") is not None:
                        _k = float(getattr(settings, "chili_momentum_volnorm_trail_k", 1.3) or 1.3)
                        # P4(2) SQUEEZE-AWARE HOLD: an EXTREME-tail squeeze name WIDENS the volnorm
                        # RIDE band (raise the trail k by a bounded percentile factor) so a fueled
                        # runner extends further before the vol-norm trail tightens. INVARIANT-A SAFE
                        # — a WIDER band lowers the trail CANDIDATE, which volnorm_runner_trail_stop
                        # composes through max(current_stop, be, candidate): it can only decline to
                        # ratchet as hard, NEVER loosen the placed stop. Factor 1.0 ⇒ byte-identical.
                        _sq_widen_vn = _squeeze_exit_band_widen_factor(via, sess.symbol)
                        if _sq_widen_vn > 1.0:
                            _k = _k * _sq_widen_vn
                        # DESIGN#2 ADAPTIVE TRAIL-WIDTH MATURITY WIDEN: a fresh, vol-rich runner
                        # trails toward the chandelier-literature optimum (PF peaks ~3x ATR; 2x
                        # over-tightens) by widening the 2A k; a maturing/exhausting runner (OFI
                        # slope rolling over) decays the factor to 1.0 so the existing RIDE-LOCK
                        # LOCK/HARD bands tighten unimpeded. INVARIANT-A SAFE — a wider band only
                        # LOWERS the candidate, composed through max(stop, be, candidate) below;
                        # never loosens a placed stop. Flag OFF / thin flow ⇒ factor 1.0 ⇒ byte-
                        # identical. The OFI read is the SAME _live_flow_slope LEVER 2B uses.
                        if bool(getattr(settings, "chili_momentum_volnorm_trail_maturity_widen_enabled", True)):
                            try:
                                from .pipeline import _live_flow_slope as _lfs_mat
                                from .paper_execution import trail_width_maturity_factor as _twmf
                                _fs_mat = _lfs_mat(sess.symbol, db=db) or {}
                                _mat_factor = _twmf(
                                    rv_live=float(_rv["rv_step"]),
                                    vol_floor_pct=max(0.0, _atr_pct_trail * _sm),
                                    ofi_level=_fs_mat.get("ofi_level"),
                                    ofi_slope=_fs_mat.get("ofi_slope"),
                                    max_widen=float(getattr(settings, "chili_momentum_volnorm_trail_maturity_max_widen", 2.0) or 2.0),
                                )
                                if _mat_factor > 1.0:
                                    _k = _k * _mat_factor
                            except Exception:
                                pass
                        # Live-derived holding horizon: median realized hold of recent scalps,
                        # falling back to this session's own elapsed hold (no magic horizon).
                        _hold_s = _recent_scalp_median_hold_s(db, int(sess.user_id))
                        if _hold_s is None or _hold_s <= 0:
                            _hold_s = max(30.0, float(held or 0.0))
                        # Vol floor: reuse the frozen entry vol-floored ATR width (the same
                        # vol-floor the entry stop used) so the trail can never sit tighter
                        # than the entry stop's documented noise floor.
                        _vol_floor_pct = max(0.0, _atr_pct_trail * _sm)
                        # HWM reference: the MICRO-PRICE high (size-weighted fair value),
                        # falling back to the existing HWM when L1 sizes are unavailable.
                        # Sizes come from the latest iqfeed_trade_ticks bid/ask sizes when
                        # present; absent sizes ⇒ raw HWM (the micro_price core is exercised
                        # the moment L1 sizes are captured on the tick path).
                        _bsz = _float_or_none(le.get("last_bid_size"))
                        _asz = _float_or_none(le.get("last_ask_size"))
                        _mp = None
                        if bid and ask and _bsz and _asz:
                            _mp = _micro_price(bid, _bsz, ask, _asz)
                        _hwm_vn = max(_hwm_trail, _mp) if _mp is not None else _hwm_trail
                        _vn_dist = volnorm_trail_dist_pct(
                            rv_live=float(_rv["rv_step"]),
                            expected_hold_s=float(_hold_s),
                            grid_secs=float(_rv.get("grid_secs") or 2.0),
                            k=_k,
                            vol_floor_pct=_vol_floor_pct,
                            effective_spread_pct=_rv.get("eff_spread_pct"),
                            max_dist_pct=float(getattr(settings, "chili_momentum_volnorm_trail_max_dist_pct", 0.15) or 0.15),
                        )
                        _vn_stop = volnorm_runner_trail_stop(
                            high_water_mark=_hwm_vn,
                            trail_dist_pct=_vn_dist,
                            breakeven_floor=_be_floor,
                            current_stop=stop_px,
                            side_long=True,
                        )
                        # INVARIANT-A: both candidates are ratchet-only over (cs, be); the
                        # max never loosens the live stop.
                        if _vn_stop > _trailed:
                            _trailed = _vn_stop
                        _emit(db, sess, "volnorm_trail_candidate", {
                            "vn_dist_pct": _vn_dist,
                            "vn_stop": _vn_stop,
                            "cushion_stop": _trailed,
                            "rv_step": _rv.get("rv_step"),
                            "tick_rate": _rv.get("tick_rate"),
                            "eff_spread_pct": _rv.get("eff_spread_pct"),
                            "expected_hold_s": _hold_s,
                            "micro_hwm": _hwm_vn,
                        })
                except Exception:
                    pass

            # LEVER 2B — VELOCITY/PERSISTENCE RIDE-LOCK on top of the 2A vol-norm trail.
            # Reads the DENOISED flow (OFI LEVEL + its EWMA SLOPE = the 1st derivative, NOT
            # raw signed_accel) + the live tick_rate, and modulates the 2A band by regime:
            # RIDE holds the band WIDE while flow persists (do not mechanically tighten);
            # LOCK collapses it to a tight giveback when flow rolls over near the HWM (sell
            # into strength before a full candle prints); HARD is a tighter climax-lock on
            # strong-negative flow + sellers lifting through the fair value. It is an
            # ADDITIONAL ratchet-only layer composed via max() exactly like the 2A trail and
            # the climax exits (INVARIANT-A): RIDE returns the 2A-width stop (never loosens —
            # it only declines to tighten FURTHER), LOCK/HARD only move the stop when their
            # tighter band lands ABOVE the live stop. The base band is the 2A width when the
            # 2A path produced one, else the realized band off the current trail. Flag-off /
            # thin-or-missing flow ⇒ no-op (the 2A trail alone, byte-identical).
            if bool(getattr(settings, "chili_momentum_velocity_persistence_exit_enabled", True)):
                try:
                    from .pipeline import _live_flow_slope
                    from .paper_execution import velocity_persistence_ride_lock

                    _fs = _live_flow_slope(sess.symbol, db=db)
                    if _fs is not None and _fs.get("ofi_slope") is not None:
                        # base band: prefer the 2A width; else the realized band off the
                        # current trail (so RIDE-LOCK always has a meaningful WIDE baseline).
                        if _vn_dist is not None and _vn_dist > 0:
                            _base_band = float(_vn_dist)
                        elif _hwm_trail > 0 and _trailed > 0:
                            _base_band = max(0.0, (_hwm_trail - _trailed) / _hwm_trail)
                        else:
                            _base_band = 0.0
                        _persist_frac = float(
                            getattr(settings, "chili_momentum_velocity_persist_frac", 0.6) or 0.6
                        )
                        _ofi_thr = abs(float(
                            getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25
                        ))
                        _vp = velocity_persistence_ride_lock(
                            high_water_mark=_hwm_trail,
                            entry_price=avg,
                            bid=bid,
                            base_trail_dist_pct=_base_band,
                            ofi_level=_fs.get("ofi_level"),
                            ofi_slope=_fs.get("ofi_slope"),
                            tick_rate_per_s=_fs.get("tick_rate"),
                            entry_tick_rate_per_s=_float_or_none(le.get("entry_tick_rate")),
                            persist_frac=_persist_frac,
                            breakeven_floor=_be_floor,
                            current_stop=stop_px,
                            micro_price_ref=_fs.get("mid"),
                            last_trade_px=_fs.get("last_price"),
                            ofi_threshold=_ofi_thr,
                            side_long=True,
                        )
                        # INVARIANT-A: RIDE-LOCK candidate is ratchet-only over (cs, be);
                        # take the MAX with the 2A/cushion trail — only a LOCK/HARD band that
                        # lands ABOVE the current _trailed actually tightens; RIDE never
                        # loosens (its candidate == the 2A-width stop ≤ _trailed already).
                        _vp_stop = _float_or_none(_vp.get("new_stop_floor"))
                        if _vp_stop is not None and _vp_stop > _trailed:
                            _trailed = _vp_stop
                        _emit(db, sess, "velocity_persistence_ride_lock", {
                            "regime": _vp.get("regime"),
                            "ride": bool(_vp.get("ride")),
                            "band_pct": _vp.get("band_pct"),
                            "base_band_pct": _base_band,
                            "vp_stop": _vp_stop,
                            "trailed_stop": _trailed,
                            "ofi_level": _fs.get("ofi_level"),
                            "ofi_slope": _fs.get("ofi_slope"),
                            "tick_rate": _fs.get("tick_rate"),
                            "entry_tick_rate": le.get("entry_tick_rate"),
                            "persist_ok": _vp.get("persist_ok"),
                            "last_price": _fs.get("last_price"),
                            "mid": _fs.get("mid"),
                            "high_water_mark": _hwm_trail,
                        })
                except Exception:
                    pass
            if _trailed > stop_px:
                pos["stop_price"] = _trailed
                stop_px = _trailed
                le["position"] = pos
                _commit_le(sess, le)
                _emit(db, sess, "live_trail_ratchet", {
                    "new_stop": _trailed,
                    "high_water_mark": _hwm_trail,
                    "partial_taken": bool(pos.get("partial_taken")),
                })

            # MEASURED-MOVE SCALE TARGET + DOUBLE-TOP EXHAUSTION (winner-management,
            # flag-gated, default OFF ⇒ this whole block is inert and the runner
            # trails byte-identical). Runs on the held RUNNER (partial_taken True) AFTER
            # the cushion ratchet, using the impulse leg frozen at the first-target
            # scale-out. WINNER-SAFE: both helpers are RATCHET-ONLY over the live stop
            # (Action A here) — they tighten the runner, never cut it. The measured-move
            # PARTIAL + the double-top partial arm route through the SAME audited
            # SCALING_OUT path (Action B, gated on its own one-tick marker, default OFF
            # / observe-first like the OFI lock partial) so a fraction is scaled and the
            # remainder keeps running. docs/DESIGN/MOMENTUM_LANE.md
            if measured_move_exit_enabled() and bool(pos.get("partial_taken")):
                try:
                    _imp_high = _float_or_none(pos.get("impulse_leg_high"))
                    _imp_entry = _float_or_none(pos.get("impulse_leg_entry")) or avg
                    if _imp_high is not None and _imp_high > 0:
                        _eq_shares = not str(sess.symbol or "").upper().endswith("-USD")
                        _mm_inc = prod.base_increment if prod else (1.0 if _eq_shares else None)
                        _mm_min = prod.base_min_size if prod else (1.0 if _eq_shares else None)
                        _mm = measured_move_scale_exit_decision(
                            flag_on=True,
                            current_qty=_float_or_none(pos.get("quantity")) or 0.0,
                            original_qty=_q0,
                            entry_price=_imp_entry,
                            impulse_leg_high=_imp_high,
                            bid=bid,
                            atr_pct=_atr_pct_trail,
                            stop_atr_mult=_sm,
                            current_stop=stop_px,
                            breakeven_floor=_be_floor,
                            already_fired=bool(pos.get("measured_move_taken")),
                            symbol=sess.symbol,
                            base_increment=_mm_inc,
                            base_min_size=_mm_min,
                            side_long=True,
                        )
                        # Double-top exhaustion off the SAME impulse high (flow optional).
                        _mm_ofi = None
                        _mm_micro = None
                        try:
                            from .pipeline import _live_ofi_microprice as _mm_flow

                            _mm_ofi, _mm_micro = _mm_flow(sess.symbol, db=db)
                        except Exception:
                            _mm_ofi, _mm_micro = None, None
                        _dt = double_top_tighten_decision(
                            flag_on=True,
                            impulse_leg_high=_imp_high,
                            current_high=_hwm_trail,
                            bid=bid,
                            entry_price=_imp_entry,
                            atr_pct=_atr_pct_trail,
                            stop_atr_mult=_sm,
                            current_stop=stop_px,
                            breakeven_floor=_be_floor,
                            ofi=_mm_ofi,
                            micro_edge=_mm_micro,
                            side_long=True,
                        )
                        # Action A — RATCHET-ONLY stop write (winner-safe core). The
                        # measured-move ratchet (to breakeven on the runner) and the
                        # double-top tighten both only ever RAISE the stop; the > stop_px
                        # guard is belt-and-suspenders.
                        _mm_floor = _float_or_none(_mm.get("new_stop_floor"))
                        _dt_floor = _float_or_none(_dt.get("new_stop_floor"))
                        _cand = max(
                            [v for v in (_mm_floor, _dt_floor) if v is not None] or [stop_px]
                        )
                        if _cand > stop_px:
                            pos["stop_price"] = _cand
                            stop_px = _cand
                            le["position"] = pos
                            _commit_le(sess, le)
                        if _mm.get("fire") or _dt.get("tighten") or _dt.get("exhausted"):
                            _emit(db, sess, "live_measured_move_exit", {
                                "mm_fire": bool(_mm.get("fire")),
                                "mm_reason": _mm.get("reason"),
                                "mm_target": _mm.get("target_price"),
                                "mm_leg_height": _mm.get("leg_height"),
                                "mm_scale_qty": _mm.get("scale_qty"),
                                "mm_scale_fraction": _mm.get("scale_fraction"),
                                "dt_exhausted": bool(_dt.get("exhausted")),
                                "dt_tighten": bool(_dt.get("tighten")),
                                "dt_flow_weak": bool(_dt.get("flow_weak")),
                                "dt_reason": _dt.get("reason"),
                                "new_stop": _cand,
                                "bid": bid,
                                "impulse_leg_high": _imp_high,
                                "high_water_mark": _hwm_trail,
                            })
                        # Action B — arm the measured-move / double-top PARTIAL (one-tick
                        # marker; observe-first, default OFF). It routes through the SAME
                        # audited SCALING_OUT path; a fraction is scaled and the runner
                        # keeps running (never a full cut). Promote after the A/B proves out.
                        if bool(getattr(settings, "chili_momentum_exit_ofi_lock_partial_enabled", False)):
                            if _mm.get("fire") or _dt.get("partial_arm"):
                                le["measured_move_partial_armed"] = True
                                _commit_le(sess, le)
                except Exception:
                    pass

            # Adaptive order-flow EXHAUSTION LOCK (crypto runner). Runs AFTER the
            # cushion ratchet so `current_band_bps` is the band's REALIZED output
            # this tick — the lock can then only ever tighten vs the actual trail
            # (never clamps to a looser theoretical band). Crypto-only: equity is
            # byte-identical (this entire block is gated on `-USD`). The lock is
            # ratchet-only over the structural stop and clamped no looser than the
            # band. The A/B counterfactual (fixed-R:R stop, lock OFF) is emitted on
            # EVERY armed tick so realized PnL is measured vs baseline LIVE before
            # the partial (Action B) ever moves size. (docs/DESIGN/ADAPTIVE_OFI_EXIT.md)
            if (
                sess.symbol.endswith("-USD")
                or bool(getattr(settings, "chili_momentum_exit_adaptive_equity_enabled", True))
            ) and bool(
                getattr(settings, "chili_momentum_exit_ofi_lock_enabled", True)
            ):
                try:
                    from .pipeline import _live_ofi_microprice

                    _ofi_x, _mpe_x = _live_ofi_microprice(sess.symbol, db=db)
                    # Hidden-seller absorption (accelerant) only when its flag is on;
                    # reads the in-process COINBASE book ring — CRYPTO-ONLY (an equity
                    # symbol has no ring entry → empty → _hs_x stays None anyway, but
                    # gate it explicitly so the crypto-only coupling is documented).
                    _hs_x = None
                    if sess.symbol.endswith("-USD") and bool(
                        getattr(settings, "chili_momentum_exit_ofi_hidden_seller_enabled", False)
                    ):
                        try:
                            from ..microstructure import get_book_buffer
                            from ..fast_path.microstructure_log import _hidden_seller

                            _hs_win = float(
                                getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0
                            )
                            _hs_snaps = get_book_buffer().recent(sess.symbol, window_secs=_hs_win)
                            if len(_hs_snaps) >= 2:
                                _hs_x = _hidden_seller(_hs_snaps)
                        except Exception:
                            _hs_x = None
                    # current_band_bps = the cushion band's REALIZED stop this tick.
                    _band_bps = ((_hwm_trail - stop_px) / _hwm_trail * 10_000.0) if _hwm_trail > 0 else 0.0
                    # 1m candle exhaustion confirmer: the entry trigger runs on 1m but
                    # the lock's only candle read upstream is the coarse 15m _entry_df.
                    # Fetch a 1m df at most once/min/session (mirrors the 5m-EMA cache
                    # above) and read a topping-tail (+ optional MACD-hist rollover) as
                    # ONE MORE AND-gated corroborant into the lock's FLOW confluence.
                    # Fail-open (None). The gate goes LIVE only under _confirm_live;
                    # default emits the candle_would_suppress A/B only. Class-agnostic
                    # (crypto + equity, same fetch). docs/DESIGN/ADAPTIVE_OFI_EXIT.md
                    _candle_exh = None
                    if bool(getattr(settings, "chili_momentum_exit_candle_confirm_enabled", True)):
                        try:
                            _cc_key = _utcnow().strftime("%Y%m%d%H%M")
                            if le.get("exit_candle1m_min") == _cc_key:
                                _candle_exh = le.get("exit_candle1m_exh")
                            else:
                                _c1_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)
                                from .candles import (
                                    topping_tail_from_df,
                                    macd_hist_rollover_from_df,
                                )

                                _df1 = _c1_fetch(sess.symbol, interval="1m", period="1d")
                                if _df1 is not None and len(_df1) >= 2:
                                    _tt1 = bool(topping_tail_from_df(_df1))
                                    _mh1 = (
                                        bool(macd_hist_rollover_from_df(_df1))
                                        if bool(getattr(settings, "chili_momentum_exit_candle_confirm_use_macd", True))
                                        else False
                                    )
                                    _candle_exh = bool(_tt1 or _mh1)
                                le["exit_candle1m_min"] = _cc_key
                                le["exit_candle1m_exh"] = _candle_exh
                                _commit_le(sess, le)
                        except Exception:
                            _candle_exh = None
                    _lock = ofi_exhaustion_lock(
                        high_water_mark=_hwm_trail,
                        entry_price=avg,
                        bid=bid,
                        atr_pct=_atr_pct_trail,
                        stop_atr_mult=_sm,
                        ofi=_ofi_x,
                        micro_edge=_mpe_x,
                        hidden_seller=_hs_x,
                        reward_risk=class_aware_reward_risk(sess.symbol),
                        current_stop=stop_px,
                        breakeven_floor=_be_floor,
                        current_band_bps=_band_bps,
                        candle_exhaustion=_candle_exh,
                        candle_gate_live=bool(
                            getattr(settings, "chili_momentum_exit_candle_confirm_live", False)
                        ),
                        side_long=True,
                    )
                    # A/B telemetry on every ARMED tick (winner past the profit-arm),
                    # whether or not the lock fired — this is the counterfactual that
                    # proves capture vs the fixed-R:R baseline before we trust it.
                    if _lock.get("armed"):
                        _emit(db, sess, "live_ofi_exhaustion_lock", {
                            "fired": bool(_lock.get("fired")),
                            "trigger": _lock.get("trigger"),
                            "peak_r": _lock.get("peak_r"),
                            "lock_bps": _lock.get("lock_bps"),
                            "band_bps": round(_band_bps, 2),
                            "ofi": _ofi_x,
                            "micro_edge": _mpe_x,
                            "hidden_seller": _hs_x,
                            "adaptive_stop": _lock.get("new_stop_floor"),
                            "counterfactual_fixed_stop": _lock.get("counterfactual_fixed_stop"),
                            "partial_arm": bool(_lock.get("partial_arm")),
                            "candle_exhaustion": _lock.get("candle_exhaustion"),
                            "candle_ok": _lock.get("candle_ok"),
                            "candle_gate_live": _lock.get("candle_gate_live"),
                            "candle_would_suppress": _lock.get("candle_would_suppress"),
                            "bid": bid,
                            "high_water_mark": _hwm_trail,
                        })
                    # Action A: ratchet-only stop write (belt-and-suspenders > guard).
                    _lock_stop = _float_or_none(_lock.get("new_stop_floor"))
                    if _lock.get("fired") and _lock_stop is not None and _lock_stop > stop_px:
                        pos["stop_price"] = _lock_stop
                        stop_px = _lock_stop
                        le["position"] = pos
                        _commit_le(sess, le)
                    # Action B: arm the early partial (one-tick flag read at 3778).
                    # Default OFF (log-would-fire-first); promote after A/B proves out.
                    if bool(getattr(settings, "chili_momentum_exit_ofi_lock_partial_enabled", False)):
                        if _lock.get("fired") and _lock.get("partial_arm"):
                            le["exhaustion_lock_partial_armed"] = True
                            _commit_le(sess, le)
                except Exception:
                    pass

            # TAPE-ACCELERATION REVERSAL EXIT (sibling of the OFI lock). The OFI lock is
            # L2-data-starved on equity (only ~88/684 names carry iqfeed_depth_snapshots);
            # this rides signed_tape_accel from the executed TRADE tape (iqfeed_trade_ticks,
            # broad equity coverage) so it covers the names the OFI lock misses. It SELLS
            # INTO STRENGTH at the spike's climax — the moment the executed-buy push ends /
            # turns while price is still near the high — BEFORE the giveback. COMPOSES with
            # the OFI lock: both run, whichever ratchets the stop HIGHER wins (Invariant A).
            # signed_tape_accel_features returns None for crypto / empty tape ⇒ the helper
            # no-ops ⇒ crypto byte-identical. Kill-switch gates the WHOLE block (OFF ⇒ no
            # fetch, byte-identical). RATCHET-ONLY: it can only ever exit a WINNER near its
            # top, never cut a loser early, never loosen the stop.
            if bool(
                getattr(settings, "chili_momentum_exit_tape_accel_reversal_enabled", True)
            ):
                try:
                    from .entry_gates import signed_tape_accel_features

                    _tape = signed_tape_accel_features(sess.symbol, db=db)
                    _accel = None
                    if _tape is not None:
                        _accel = _float_or_none(_tape.get("signed_tape_accel"))
                    _prev_accel = _float_or_none(le.get("prev_signed_tape_accel"))
                    _ar = tape_accel_reversal_exit(
                        high_water_mark=_hwm_trail,
                        entry_price=avg,
                        bid=bid,
                        atr_pct=_atr_pct_trail,
                        stop_atr_mult=_sm,
                        reward_risk=class_aware_reward_risk(sess.symbol),
                        current_stop=stop_px,
                        breakeven_floor=_be_floor,
                        signed_tape_accel=_accel,
                        prev_signed_tape_accel=_prev_accel,
                        side_long=True,
                    )
                    # A/B telemetry on EVERY tick (with the lock-OFF counterfactual) so
                    # realized PnL is measured vs the baseline before we trust it.
                    _emit(db, sess, "live_tape_accel_reversal_exit", {
                        "fired": bool(_ar.get("fired")),
                        "trigger": _ar.get("trigger"),
                        "peak_r": _ar.get("peak_r"),
                        "armed": bool(_ar.get("armed")),
                        "reason": _ar.get("reason"),
                        "signed_tape_accel": _accel,
                        "prev_signed_tape_accel": _prev_accel,
                        "adaptive_stop": _ar.get("new_stop_floor"),
                        "counterfactual_fixed_stop": _ar.get("counterfactual_fixed_stop"),
                        "bid": bid,
                        "high_water_mark": _hwm_trail,
                    })
                    # Store the current accel as the next tick's prev (genuine-TURN read).
                    if _accel is not None:
                        le["prev_signed_tape_accel"] = _accel
                        _commit_le(sess, le)
                    # RATCHET-ONLY stop write (belt-and-suspenders > stop_px guard).
                    _ar_stop = _float_or_none(_ar.get("new_stop_floor"))
                    if _ar.get("fired") and _ar_stop is not None and _ar_stop > stop_px:
                        pos["stop_price"] = _ar_stop
                        stop_px = _ar_stop
                        le["position"] = pos
                        _commit_le(sess, le)
                except Exception:
                    pass

            # v2 PROACTIVE sell-into-strength (Ross ladder read) — sibling to v1, runs
            # AFTER it so they compose: v1 DEFENDS (tightens the stop on exhaustion),
            # v2 HARVESTS the top (a small resting limit into genuine strength). The
            # counterfactual A/B + the INVARIANT-A stop-ratchet are LIVE on every armed
            # tick; the size-moving resting limit is gated by exit_ladder_live (2-step
            # ship). CLASS-AWARE: crypto always runs (byte-identical); equity runs when
            # chili_momentum_exit_adaptive_equity_enabled (default ON) — equity L2 from
            # iqfeed, same helpers. Fail-open: any error => no-op.
            if (
                sess.symbol.endswith("-USD")
                or bool(getattr(settings, "chili_momentum_exit_adaptive_equity_enabled", True))
            ) and bool(
                getattr(settings, "chili_momentum_exit_ladder_enabled", True)
            ):
                try:
                    from .paper_execution import sell_into_strength_ladder
                    from .pipeline import read_ladder_distribution

                    _cooldown = False
                    try:
                        _cd_raw = le.get("ladder_cooldown_until_utc")
                        if _cd_raw:
                            _cooldown = _utcnow() < datetime.fromisoformat(_cd_raw)
                    except Exception:
                        _cooldown = False
                    _ladder = read_ladder_distribution(sess.symbol, db=db)
                    # ── CADENCE CLASS (drives the SLOW_CHOPPER loosening below) ──
                    # Fully flag-gated + fail-open: any error / OFF flag ⇒ _cad_loosen
                    # stays False ⇒ the ladder is BYTE-IDENTICAL to today. All inputs
                    # are already present at this green tick (no new I/O for the class
                    # itself; the 5m-EMA + RVOL are the cached values fetched above).
                    _cad = None
                    _cad_loosen = False
                    if bool(getattr(settings, "chili_momentum_cadence_aware_exit_enabled", True)):
                        try:
                            # elapsed minutes since the fill (GUARD #1 cold-start input).
                            _cad_elapsed_min = None
                            _opened_raw = pos.get("opened_at_utc")
                            if _opened_raw:
                                try:
                                    _opened_dt = datetime.fromisoformat(str(_opened_raw))
                                    _now_dt = _utcnow()
                                    if _opened_dt.tzinfo is None and _now_dt.tzinfo is not None:
                                        _now_dt = _now_dt.replace(tzinfo=None)
                                    _cad_elapsed_min = max(
                                        0.0, (_now_dt - _opened_dt).total_seconds() / 60.0
                                    )
                                except (TypeError, ValueError):
                                    _cad_elapsed_min = None
                            # prior peak_r (GUARD #1 cold-start input): the trade's own
                            # risk unit (same formula the stop uses), HWM excursion.
                            _cad_peak_r = None
                            try:
                                _rd = avg * max(0.003, float(_atr_pct_trail or 0.0) * float(_sm or 0.0))
                                if _rd > 0:
                                    _cad_peak_r = max(0.0, (float(_hwm_trail) - float(avg)) / _rd)
                            except (TypeError, ValueError, ZeroDivisionError):
                                _cad_peak_r = None
                            # 5m-EMA rising vs the PREVIOUS cached EMA sample (structure
                            # anchor). None when we have no prior sample ⇒ unknown.
                            _cad_ema_rising = None
                            try:
                                _ema_prev = _float_or_none(le.get("cadence_prev_ema5m"))
                                if _ema5 is not None and _ema_prev is not None:
                                    _cad_ema_rising = bool(float(_ema5) > float(_ema_prev))
                                if _ema5 is not None:
                                    le["cadence_prev_ema5m"] = float(_ema5)
                            except (TypeError, ValueError):
                                _cad_ema_rising = None
                            # RVOL accelerating vs the previous cached sample. None when
                            # no prior sample / thin tape ⇒ unknown.
                            _cad_rvol_accel = None
                            try:
                                _rvol_now = _latest_rvol(db, sess.symbol)
                                _rvol_prev = _float_or_none(le.get("cadence_prev_rvol"))
                                if _rvol_now is not None and _rvol_prev is not None:
                                    _cad_rvol_accel = bool(float(_rvol_now) > float(_rvol_prev))
                                if _rvol_now is not None:
                                    le["cadence_prev_rvol"] = float(_rvol_now)
                            except (TypeError, ValueError):
                                _cad_rvol_accel = None
                            _cad = _classify_cadence(
                                high_water_mark=_hwm_trail,
                                entry_price=avg,
                                bid=bid,
                                atr_pct=_atr_pct_trail,
                                elapsed_minutes=_cad_elapsed_min,
                                peak_r_prior=_cad_peak_r,
                                ema_5m_rising=_cad_ema_rising,
                                rvol_accelerating=_cad_rvol_accel,
                                slow_atr_pct_threshold=float(
                                    getattr(settings, "chili_momentum_cadence_atr_pct_slow_threshold", 0.20) or 0.20
                                ),
                                trigger_bar_minutes=max(
                                    0.01,
                                    float(getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15) / 60.0,
                                ),
                            )
                            # ONLY a SLOW_CHOPPER loosens; FAST/UNCERTAIN never do.
                            _cad_loosen = bool(_cad.get("cls") == "SLOW_CHOPPER")
                            # LOCATE #1: persist the deployed classification (with its FULL
                            # ema/rvol signals) so the scalp time-stop in the exit handler
                            # reads the SAME classifier result — no duplicate classifier, one
                            # source of truth. Written every held tick; read next tick.
                            try:
                                le["cadence_cls"] = _cad.get("cls")
                            except Exception:
                                pass
                            _commit_le(sess, le)
                        except Exception:
                            _cad = None
                            _cad_loosen = False
                    _sis = sell_into_strength_ladder(
                        high_water_mark=_hwm_trail,
                        entry_price=avg,
                        bid=bid,
                        atr_pct=_atr_pct_trail,
                        stop_atr_mult=_sm,
                        reward_risk=class_aware_reward_risk(sess.symbol),
                        current_stop=stop_px,
                        breakeven_floor=_be_floor,
                        remaining_qty=_float_or_none(pos.get("quantity")) or 0.0,
                        ladder=_ladder,
                        prior_partial_taken=bool(pos.get("partial_taken")),
                        cooldown_active=_cooldown,
                        side_long=True,
                        cadence_loosen=_cad_loosen,
                    )
                    # GUARD #2 — RE-ENTRY DAMPER under SLOW_CHOPPER: a name just called a
                    # chopper must NOT be aggressively re-loaded (the 4.6%-spread scalp→
                    # reenter bleed). Widen/lengthen the micro-pullback re-entry cooldown
                    # for THIS session to ~3× the base bar frame from now. Monotonic: only
                    # ever pushes the cooldown LATER, never earlier (never enables a
                    # re-load that wasn't already allowed). No-op when not a chopper.
                    if _cad_loosen:
                        try:
                            _damp_s = 3.0 * float(
                                getattr(settings, "chili_momentum_micropullback_reentry_cooldown_seconds", 30.0) or 30.0
                            )
                            _damp_until = (_utcnow() + timedelta(seconds=_damp_s)).replace(tzinfo=None)
                            _dmp_raw = le.get("micropullback_reentry_cooldown_until_utc")
                            _dmp_cur = None
                            if _dmp_raw:
                                try:
                                    _dmp_cur = datetime.fromisoformat(str(_dmp_raw))
                                except (TypeError, ValueError):
                                    _dmp_cur = None
                            if _dmp_cur is None or _damp_until > _dmp_cur:
                                le["micropullback_reentry_cooldown_until_utc"] = _damp_until.isoformat()
                                _commit_le(sess, le)
                        except Exception:
                            pass
                    if _sis.get("armed"):
                        _emit(db, sess, "live_sell_into_strength", {
                            "state": _sis.get("state"),
                            "fired": bool(_sis.get("fired")),
                            "vetoed_by": _sis.get("vetoed_by"),
                            "reason": _sis.get("reason"),
                            "peak_r": _sis.get("peak_r"),
                            "dist_pctile": _sis.get("dist_pctile"),
                            "rung_bps": _sis.get("rung_bps"),
                            "first_increment_frac": _sis.get("first_increment_frac"),
                            "limit_px": _sis.get("limit_px"),
                            "sell_qty": _sis.get("sell_qty"),
                            "adaptive_stop": _sis.get("new_stop_floor"),
                            "counterfactual_hold_stop": _sis.get("counterfactual_hold_stop"),
                            "ofi": getattr(_ladder, "ofi", None),
                            "micro_edge": getattr(_ladder, "micro_edge", None),
                            "bid_refill": getattr(_ladder, "bid_refill", None),
                            "n_snaps": getattr(_ladder, "n_snaps", 0),
                            "live": bool(getattr(settings, "chili_momentum_exit_ladder_live", False)),
                            "bid": bid,
                            "high_water_mark": _hwm_trail,
                            # cadence-aware A/B telemetry (observe-first)
                            "cadence_cls": (_cad or {}).get("cls"),
                            "cadence_reason": (_cad or {}).get("reason"),
                            "cadence_velocity_score": (_cad or {}).get("velocity_score"),
                            "cadence_loosened": bool(_sis.get("cadence_loosened")),
                        })
                    # Action A: ratchet-only stop (INVARIANT A; live-on, can only help).
                    _sis_stop = _float_or_none(_sis.get("new_stop_floor"))
                    if _sis_stop is not None and _sis_stop > stop_px:
                        pos["stop_price"] = _sis_stop
                        stop_px = _sis_stop
                        le["position"] = pos
                        _commit_le(sess, le)
                    # Size-moving resting limit — GATED. Reuse the scale-out limit
                    # adoption machinery; one scale limit at a time (don't collide with
                    # v1's partial). On fill, the runner remainder ratchets to the fill.
                    if (
                        bool(getattr(settings, "chili_momentum_exit_ladder_live", False))
                        and _sis.get("fired")
                        and _sis.get("action") == "sell_limit"
                        and not le.get("scale_limit_order_id")
                    ):
                        _ll_px = _float_or_none(_sis.get("limit_px"))
                        _ll_qty = _float_or_none(_sis.get("sell_qty"))
                        if _ll_px and _ll_qty and _ll_qty > 0:
                            _ll_cid = f"chili_ml_sis_{sess.id}_{uuid.uuid4().hex[:12]}"
                            _ll_kwargs = dict(
                                product_id=product_id,
                                side="sell",
                                base_size=_fmt_base_size(_ll_qty),
                                limit_price=_fmt_limit_price_sell(_ll_px),
                                client_order_id=_ll_cid,
                            )
                            if not sess.symbol.endswith("-USD"):
                                # EQUITY: a DAY order (auto-cancels at the close — the
                                # free-option expires daily, never a stale resting GTC);
                                # extended_hours flagged when outside RTH. Crypto keeps the
                                # bare 24/7 GTC call (byte-identical).
                                try:
                                    from .market_profile import market_session_now

                                    _ll_ext = market_session_now(sess.symbol) != "regular"
                                except Exception:
                                    _ll_ext = False
                                _ll_kwargs["time_in_force"] = "gfd"
                                _ll_kwargs["extended_hours"] = _ll_ext
                            _ll_res = adapter.place_limit_order_gtc(**_ll_kwargs) or {}
                            if _ll_res.get("ok") and _ll_res.get("order_id"):
                                le["scale_limit_order_id"] = str(_ll_res["order_id"])
                                le["scale_limit_px"] = float(_ll_px)
                                le["scale_limit_qty"] = float(_ll_qty)
                                le["scale_limit_adopted_qty"] = 0.0
                                le["scale_limit_source"] = "sell_into_strength"
                                # cooldown so a second rung can't stack for ~15s
                                le["ladder_cooldown_until_utc"] = (
                                    _utcnow() + timedelta(seconds=15)
                                ).isoformat()
                                _commit_le(sess, le)
                                _emit(db, sess, "sell_into_strength_limit_placed", {
                                    "order_id": le["scale_limit_order_id"],
                                    "qty": float(_ll_qty), "limit_price": float(_ll_px),
                                    "peak_r": _sis.get("peak_r"), "rung_bps": _sis.get("rung_bps"),
                                })
                except Exception:
                    pass

            # ── ANTICIPATION STARTER REMAINDER (item 1, DEFAULT OFF ⇒ byte-identical) ──
            # The entry placed only the PROBE leg; once the probe CONFIRMS (a real held
            # position = the break is real) add the stashed remainder ONCE, reusing the
            # SAME in-flight broker-truth merge the pyramid uses (pyramid_blend_on_fill —
            # the shared/tested helper) on a SEPARATE in-flight slot so it is independent
            # of the pyramid flag. Two phases, both FALL THROUGH (no early return) so the
            # stop-breach check still runs this tick. Dedupe: one in-flight remainder order
            # at a time (idempotent); orphan-safe: the child order_id is folded into the
            # entry-order history so the late-fill sweep tracks it to terminal. Only fires
            # when armed (probe split happened) AND the position is GREEN (confirmation) —
            # it never bypasses a veto (a confirmed fill IS the trigger). Absent the arm
            # (flag OFF / no split) ⇒ this whole block is a no-op (byte-identical).
            if le.get("anticipation_armed") or le.get("anticipation_add_order_id"):
                try:
                    # PHASE 1 — resolve an in-flight remainder add (mirror the pyramid merge).
                    _ant_oid = le.get("anticipation_add_order_id")
                    if _ant_oid:
                        _ano, _ = adapter.get_order(str(_ant_oid))
                        if _ano is not None and not _order_open(_ano):
                            _ant_filled = float(getattr(_ano, "filled_size", 0) or 0)
                            if _ant_filled > 0:
                                _Pa_a = float(getattr(_ano, "average_filled_price", 0) or 0) or float(
                                    le.get("anticipation_add_limit_px") or ask
                                )
                                _blend_a = pyramid_blend_on_fill(
                                    q0=float(pos["quantity"]),
                                    a0=float(pos["avg_entry_price"]),
                                    qa_f=_ant_filled,
                                    Pa_f=_Pa_a,
                                    stop_px=float(pos["stop_price"]),
                                    original_quantity=_float_or_none(pos.get("original_quantity")),
                                )
                                pos["avg_entry_price"] = _blend_a["a1"]
                                pos["quantity"] = _blend_a["q1"]
                                pos["original_quantity"] = _blend_a["original_quantity"]
                                pos["notional_usd"] = _blend_a["q1"] * _blend_a["a1"]
                                pos["stop_price"] = _blend_a["s1"]
                                stop_px = _blend_a["s1"]
                                _ant_fee = _order_total_fees_usd(_ano) or 0.0
                                if _ant_fee > 0.0:
                                    le["entry_fee_usd_unbooked"] = (
                                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _ant_fee
                                    )
                                _mark_entry_order_resolved(le, str(_ant_oid), "adopted")
                                le["anticipation_completed"] = True
                                le.pop("anticipation_add_order_id", None)
                                le.pop("anticipation_add_limit_px", None)
                                le.pop("anticipation_armed", None)
                                le.pop("anticipation_remainder_qty", None)
                                le["position"] = pos
                                _commit_le(sess, le)
                                _emit(db, sess, "live_anticipation_remainder_filled", {
                                    "add_qty": _ant_filled, "add_price": _Pa_a,
                                    "q1": _blend_a["q1"], "a1": _blend_a["a1"],
                                })
                            else:
                                # Terminal with NO fill — clear the in-flight slot so a later
                                # tick may retry the remainder. No pos mutation.
                                le.pop("anticipation_add_order_id", None)
                                le.pop("anticipation_add_limit_px", None)
                                _commit_le(sess, le)
                    # PHASE 2 — submit the remainder ONCE when armed + confirmed green + idle.
                    _ant_rem = _float_or_none(le.get("anticipation_remainder_qty"))
                    if (
                        le.get("anticipation_armed")
                        and not le.get("anticipation_add_order_id")
                        and not le.get("anticipation_completed")
                        and _ant_rem is not None and _ant_rem > 0
                        and float(bid) > float(pos["avg_entry_price"])  # confirmation: position is GREEN
                    ):
                        _ant_ask = float(ask) if (ask and float(ask) > 0) else float(bid)
                        _ant_guard_ask = _ant_ask * _adaptive_notional_guard_multiplier(
                            expected_move_bps=_expected_move_bps
                        )
                        _ant_limit_str = _fmt_limit_price_buy(_ant_guard_ask)
                        _ant_place_n = int(le.get("anticipation_place_count", 0) or 0) + 1
                        le["anticipation_place_count"] = _ant_place_n
                        _ant_seed = (
                            f"{sess.id}|{sess.correlation_id or 'x'}|ant|{_ant_place_n}"
                        ).encode("utf-8")
                        _ant_suffix = hashlib.sha1(_ant_seed).hexdigest()[:10]
                        _ant_cid = (
                            f"chili_ml_ant_{sess.id}_{(sess.correlation_id or 'x')[:8]}_{_ant_suffix}"
                        )[:120]
                        try:
                            from .market_profile import market_session_now as _ant_sess_now
                            _ant_ext = _ant_sess_now(sess.symbol) != "regular"
                        except Exception:
                            _ant_ext = False
                        _ant_res = adapter.place_limit_order_gtc(
                            product_id=product_id,
                            side="buy",
                            base_size=_fmt_base_size(_ant_rem),
                            limit_price=_ant_limit_str,
                            client_order_id=_ant_cid,
                            extended_hours=_ant_ext,
                            time_in_force="gfd",
                        ) or {}
                        if _ant_res.get("ok") and _ant_res.get("order_id"):
                            le["anticipation_add_order_id"] = str(_ant_res["order_id"])
                            le["anticipation_add_limit_px"] = float(_ant_guard_ask)
                            # Fold the remainder leg into the entry-order history so the
                            # late-fill sweep + pre-submit guard track it to terminal (no
                            # stranded naked leg). SAME safety net as the primary entry.
                            _record_entry_order_placed(le, _ant_res.get("order_id"))
                            _commit_le(sess, le)
                            _emit(db, sess, "live_anticipation_remainder_submitted", {
                                "order_id": le["anticipation_add_order_id"],
                                "client_order_id": _ant_cid,
                                "remainder_qty": float(_ant_rem),
                                "limit_price": _ant_limit_str,
                            })
                except Exception:
                    # Fail-OPEN: any error leaves the position unchanged (the probe leg is
                    # already a complete, fully-managed position on its own). Never crash
                    # the tick; never mutate pos outside the confirmed-fill PHASE-1 merge.
                    logger.debug(
                        "[momentum_live] anticipation remainder pass skipped session=%s",
                        sess.id, exc_info=True,
                    )

            # ── RISK-NEUTRAL CONFIRMATION PYRAMID (single add to a winner) ───────
            # Placed AFTER the cushion-trail ratchet + OFI-lock + v2-ladder, so
            # `stop_px` here is the FRESHEST ratcheted value, and BEFORE the
            # stop-breach block below — the add is entry-side ONLY and physically
            # cannot precede or delay an exit. The whole block is a no-op when the
            # flag is OFF (byte-identical: no add, no pos mutation, no emit, no extra
            # broker call, #769 anchor stays None). Two phases, both FALL THROUGH
            # (never early-return) so the freshly-ratcheted stop-breach check still
            # runs this same tick. (docs/DESIGN/MOMENTUM_LANE.md)
            if bool(getattr(settings, "chili_momentum_pyramid_enabled", False)):
                try:
                    # PHASE 1 — resolve an IN-FLIGHT add order (mirror entry adopt).
                    # Mutate pos ONLY on a CONFIRMED fill; a partial blends ONLY the
                    # filled qty. While an order is in flight, PHASE 2 cannot submit a
                    # second (idempotency). No early return: on a confirmed/partial
                    # fill we fall through with the freshly-blended pos + ratcheted s1.
                    _pyr_oid = le.get("pyramid_order_id")
                    if _pyr_oid:
                        _pno, _ = adapter.get_order(str(_pyr_oid))
                        if _pno is not None and not _order_open(_pno):
                            _pyr_filled = float(getattr(_pno, "filled_size", 0) or 0)
                            if _pyr_filled > 0:
                                # CONFIRMED ADD FILL — blend via the SHARED pure helper
                                # (one source of truth with replay + tests). A partial add
                                # blends ONLY the filled qty.
                                _qa_f = _pyr_filled
                                _Pa_f = float(
                                    getattr(_pno, "average_filled_price", 0) or 0
                                ) or float(le.get("pyramid_limit_px") or ask)
                                _q0p = float(pos["quantity"])
                                _a0p = float(pos["avg_entry_price"])
                                _R0p = _float_or_none(le.get("pyramid_pending_R0"))
                                _prev_stop = float(pos["stop_price"])
                                _blend = pyramid_blend_on_fill(
                                    q0=_q0p, a0=_a0p, qa_f=_qa_f, Pa_f=_Pa_f,
                                    stop_px=_prev_stop,
                                    original_quantity=_float_or_none(pos.get("original_quantity")),
                                )
                                _q1 = _blend["q1"]
                                _a1 = _blend["a1"]
                                # INVARIANT-A: stop only TIGHTENS (asserted in the helper).
                                _s1 = _blend["s1"]
                                pos["avg_entry_price"] = _a1
                                pos["quantity"] = _q1
                                # GROW original_quantity so the Ross scale-out de-risks
                                # the ENLARGED position (scale_out_quantity bases its
                                # fraction on original_quantity; the can_split dust guard
                                # there is re-checked at scale time against the new size).
                                pos["original_quantity"] = _blend["original_quantity"]
                                pos["notional_usd"] = _q1 * _a1
                                pos["stop_price"] = _s1
                                stop_px = _s1
                                # Freeze R0 as the #769 risk anchor so the circuit
                                # re-bases to the STARTER's original risk (GUARD #1).
                                if _R0p is not None and _R0p > 0:
                                    le["pyramid_risk_anchor_usd"] = _R0p
                                le["pyramid_add_count"] = int(le.get("pyramid_add_count") or 0) + 1
                                # Book the add's entry fee (mirrors the entry adopt) so
                                # realized PnL nets it at the full exit.
                                _add_fee = _order_total_fees_usd(_pno) or 0.0
                                if _add_fee > 0.0:
                                    le["entry_fee_usd_unbooked"] = (
                                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _add_fee
                                    )
                                le.pop("pyramid_order_id", None)
                                le.pop("pyramid_limit_px", None)
                                le.pop("pyramid_pending_R0", None)
                                le["position"] = pos
                                _commit_le(sess, le)
                                _emit(db, sess, "live_pyramid_add", {
                                    "add_qty": _qa_f, "add_price": _Pa_f,
                                    "q0": _q0p, "a0": _a0p, "q1": _q1, "a1": _a1,
                                    "old_stop": float(le.get("pyramid_prev_stop") or _s1),
                                    "new_stop": _s1, "R0": _R0p,
                                    "rho": float(getattr(settings, "chili_momentum_pyramid_add_risk_fraction", 0.5) or 0.5),
                                    "cushion_r": ((bid - _a0p) * _q0p / _R0p) if (_R0p and _R0p > 0) else None,
                                    "ofi": le.get("pyramid_confirm_ofi"),
                                    "risk_anchor_usd": _R0p,
                                })
                                le.pop("pyramid_prev_stop", None)
                                le.pop("pyramid_confirm_ofi", None)
                            else:
                                # Order terminal with NO fill (rejected / cancelled /
                                # post-only-cross) — clear the in-flight marker so a
                                # future tick may try again. No pos mutation.
                                le.pop("pyramid_order_id", None)
                                le.pop("pyramid_limit_px", None)
                                le.pop("pyramid_pending_R0", None)
                                le.pop("pyramid_prev_stop", None)
                                le.pop("pyramid_confirm_ofi", None)
                                _commit_le(sess, le)
                        # else: still working — leave it in flight, do NOT submit again.

                    # PHASE 2 — TRIGGER a new add (only if none in flight + under cap).
                    # EQUITY-FIRST: gate to equity. Crypto is deferred — its L2/OFI
                    # ring is only partially populated in the scheduler process
                    # (_live_ofi_microprice returns None for many crypto names), so the
                    # confirmation can't be trusted to fire an extra BUY; revisit when
                    # crypto L2 coverage is complete. (project_l2_integration)
                    _is_equity_pyr = not str(sess.symbol or "").upper().endswith("-USD")
                    _max_adds = int(getattr(settings, "chili_momentum_pyramid_max_adds", 1) or 1)
                    if st == STATE_LIVE_TRAILING and not le.get("pyramid_order_id"):
                        # R0 = the STARTER's ORIGINAL structural risk = d0 * q0, where
                        # d0 is the frozen entry stop_distance (the C1b basis) and q0,a0
                        # are the STARTER size/avg. Use original_quantity as q0 so a
                        # post-partial runner still funds the add off the full starter R.
                        _es_p = le.get("entry_sizing") if isinstance(le.get("entry_sizing"), dict) else {}
                        _d0 = _float_or_none(_es_p.get("stop_distance"))
                        if _d0 is None or _d0 <= 0:
                            _d0 = max(0.0, float(avg) - float(pos["stop_price"])) or None
                        _q0_starter = (
                            _float_or_none(pos.get("original_quantity"))
                            or _float_or_none(pos.get("quantity"))
                            or 0.0
                        )
                        _a0_starter = float(pos["avg_entry_price"])
                        # OFI thrust (the confirmation; None for many crypto → fail-closed).
                        _pyr_ofi = None
                        try:
                            from .pipeline import _live_ofi_microprice as _pyr_ofi_fn
                            _pyr_ofi, _ = _pyr_ofi_fn(sess.symbol, db=db)
                        except Exception:
                            _pyr_ofi = None
                        # Anchor the entry-stop reference ONCE so "ratcheted since first
                        # considered" is monotone (the headroom test). Persist it.
                        _entry_stop0 = _float_or_none(le.get("pyramid_entry_stop_ref"))
                        if _entry_stop0 is None:
                            _entry_stop0 = float(pos["stop_price"])
                            le["pyramid_entry_stop_ref"] = _entry_stop0
                            _commit_le(sess, le)
                        # Anti-Ross midday: no add during the equity midday lull
                        # (entry-side parity with the #770 lull de-weight).
                        _lull = False
                        try:
                            from .market_profile import in_midday_lull as _pyr_lull
                            _lull = bool(_pyr_lull(sess.symbol))
                        except Exception:
                            _lull = False
                        # ICEBERG / HIDDEN-SELLER probe (Ross SS101 #038) — EQUITY-ONLY,
                        # ADD-PATH ONLY, fail-OPEN. Read the short-window top-of-book ASK
                        # series (price+size) from iqfeed_depth_snapshots (same source +
                        # window as the OFI/micro-price read) and score refill-vs-advance:
                        # a refilling displayed ask => an absorbing seller => block the add.
                        # None (flag off, crypto, or absent/stale L2) => the add is allowed.
                        _iceberg_score = None
                        _iceberg_thresh = None
                        if _is_equity_pyr and bool(
                            getattr(settings, "chili_momentum_iceberg_add_probe_enabled", True)
                        ):
                            try:
                                from sqlalchemy import text as _ice_sql

                                _ice_win = float(
                                    getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0
                                )
                                _ice_rows = db.execute(
                                    _ice_sql(
                                        "SELECT ask_top, ask_top_size "
                                        "FROM iqfeed_depth_snapshots "
                                        "WHERE symbol = :s AND observed_at > "
                                        "(now() at time zone 'utc') - make_interval(secs => :w) "
                                        "ORDER BY observed_at ASC"
                                    ),
                                    {"s": str(sess.symbol or "").strip().upper(), "w": _ice_win},
                                ).fetchall()
                                _ice_series = [
                                    (float(r[0]), float(r[1]))
                                    for r in _ice_rows
                                    if r[0] is not None and r[1] is not None
                                ]
                                _iceberg_score = iceberg_seller_score(_ice_series)
                                if _iceberg_score is not None:
                                    _iceberg_thresh = float(
                                        getattr(
                                            settings,
                                            "chili_momentum_iceberg_add_refill_ratio",
                                            1.0,
                                        )
                                        or 1.0
                                    )
                            except Exception:
                                # Fail-OPEN: any L2 read/parse error leaves the add unchanged.
                                _iceberg_score = None
                                _iceberg_thresh = None
                        # GAP 3 DISCRETE-ADD trigger (HVM101): require a FRESH discrete
                        # entry sub-pattern (a new higher-low bounce off the rising 9-EMA/
                        # VWAP) for the add, not merely CONTINUOUS cushion+HOD+OFI. Computed
                        # here from a fresh bar frame + the SAME indicator_core 9-EMA the
                        # entry triggers use; passed to pyramid_add_decision. None (flag OFF,
                        # crypto, or any read/parse error) => the add is UNCHANGED (the guard
                        # is inert / fail-OPEN). An explicit False (flag ON, evaluated, no
                        # fresh trigger) BLOCKS the add — it can ONLY tighten, never loosen.
                        _discrete_add = None
                        if _is_equity_pyr and bool(
                            getattr(settings, "chili_momentum_pyramid_discrete_add_enabled", False)
                        ):
                            try:
                                _da_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)
                                from ..indicator_core import compute_all_from_df as _da_compute
                                from .paper_execution import discrete_pullback_add_trigger as _da_trig

                                _da_iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                                _da_df = _da_fetch(sess.symbol, interval=_da_iv, period="5d")
                                if _da_df is not None and not getattr(_da_df, "empty", True) and len(_da_df) >= 6:
                                    _da_arr = _da_compute(_da_df, needed={"ema_9", "vwap"})
                                    _da_closes = [float(x) for x in _da_df["Close"].astype(float).tolist()[-12:]]
                                    _da_ema = _da_arr.get("ema_9") or []
                                    _da_ema = [float(e) for e in _da_ema[-12:]] if _da_ema else None
                                    _da_vwap_arr = _da_arr.get("vwap") or []
                                    _da_vwap = float(_da_vwap_arr[-1]) if _da_vwap_arr else None
                                    _da_fired, _da_dbg = _da_trig(
                                        _da_closes, ema=_da_ema, vwap=_da_vwap, live_price=float(bid),
                                    )
                                    _discrete_add = bool(_da_fired)
                                    le["pyramid_discrete_trigger"] = {"fired": _discrete_add, **_da_dbg}
                            except Exception:
                                # Fail-OPEN: any read/parse error leaves the add unchanged.
                                _discrete_add = None
                        # SHARED pure predicate (one source of truth w/ replay + tests).
                        _decn = pyramid_add_decision(
                            enabled=True,  # outer block already gated on the flag
                            is_equity=_is_equity_pyr,
                            add_count=int(le.get("pyramid_add_count") or 0),
                            max_adds=_max_adds,
                            in_flight=bool(le.get("pyramid_order_id")),
                            a0=_a0_starter,
                            q0=_q0_starter,
                            d0=_d0,
                            bid=float(bid),
                            stop_px=float(stop_px),
                            entry_stop_ref=_entry_stop0,
                            high_water_mark=_float_or_none(pos.get("high_water_mark")),
                            ofi=_pyr_ofi,
                            ofi_threshold=float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25),
                            min_cushion_r=float(getattr(settings, "chili_momentum_pyramid_min_cushion_r", 1.0) or 1.0),
                            midday_lull=_lull,
                            iceberg_score=_iceberg_score,
                            iceberg_threshold=_iceberg_thresh,
                            discrete_entry_trigger_fired=_discrete_add,
                        )
                        _R0 = _decn.get("R0")
                        # GAP 6 ADD-INTO-THE-HALT (Warrior re-audit, RISKIEST — default OFF).
                        # When the name has a SUSPECTED HALT in progress (le["suspected_halt_
                        # since_utc"], the lane's stale-quote halt onset) while the held
                        # position is GREEN, this is the "add into a limit-up halt" scenario.
                        # It is EXTRA-GUARDED + fail-CLOSED via add_into_halt_ok: ALREADY IN
                        # PROFIT (>= min R) + limit-UP (bullish; inferred from the green
                        # position) + structural stop intact + RTH-only. It can only ever
                        # TIGHTEN the add (turn a would-fire into a no-fire); it NEVER loosens
                        # an add or any veto. Flag OFF ⇒ this whole block is skipped ⇒ byte-
                        # identical to the existing pyramid behavior. Deploy recipe: KEEP OFF
                        # until soaked. docs/DESIGN/MOMENTUM_LANE.md
                        if (
                            _decn.get("fire")
                            and bool(getattr(settings, "chili_momentum_add_into_halt_enabled", False))
                            and le.get("suspected_halt_since_utc")
                        ):
                            try:
                                _orig_stop = _float_or_none(
                                    (le.get("entry_sizing") or {}).get("stop_price")
                                    if isinstance(le.get("entry_sizing"), dict) else None
                                )
                                if _orig_stop is None:
                                    _orig_stop = _float_or_none(le.get("pyramid_entry_stop_ref")) or float(pos["stop_price"])
                                # limit-UP direction = a GREEN held position during the halt
                                # (the name halted to the UPSIDE — we are in profit on it).
                                _is_limit_up = float(bid) > float(pos["avg_entry_price"])
                                # RTH-only (stops fire only in RTH; equity gate, crypto exempt).
                                _halt_in_rth, _ = _dip_buy_in_rth_window(
                                    now=_utcnow(), bar_ts=None, symbol=sess.symbol,
                                )
                                # ── CHASE-GUARD context for the loss-sensitive halt-add ──
                                # TAPE REQUIRED + fail-closed: read the live executed tape;
                                # the add fires ONLY when the tape is lifting (trade_flow > 0).
                                # None (no read) ⇒ tape_confirmed=None ⇒ the gate fails closed.
                                _ah_tape = None
                                try:
                                    from .pipeline import _live_trade_flow as _ah_tf_fn
                                    _ah_tf, _ = _ah_tf_fn(sess.symbol, db=db)
                                    if _ah_tf is not None:
                                        _ah_tape = bool(float(_ah_tf) > 0.0)
                                except Exception:
                                    _ah_tape = None  # fail-CLOSED in the gate
                                # EXTENSION + NOT-BACKSIDE structure: a fresh OHLCV frame +
                                # the breakout level (recent swing high) + ATR%. Missing ⇒
                                # the gate fails closed on its own. crypto/thin ⇒ None df.
                                _ah_df = None
                                _ah_level = None
                                _ah_atr_pct = None
                                try:
                                    _ah_fetch = _replay_aware_fetch_ohlcv_df  # replay-aware seam (prod byte-identical)
                                    from ..indicator_core import compute_all_from_df as _ah_compute
                                    _ah_iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                                    _ah_df = _ah_fetch(sess.symbol, interval=_ah_iv, period="5d")
                                    if _ah_df is not None and not getattr(_ah_df, "empty", True) and len(_ah_df) >= 6:
                                        _ah_arr = _ah_compute(_ah_df, needed={"atr"})
                                        _ah_atr = _ah_arr.get("atr") or []
                                        _ah_cur = len(_ah_df) - 1
                                        _ah_close = float(_ah_df["Close"].astype(float).iloc[_ah_cur])
                                        if _ah_atr and _ah_cur < len(_ah_atr) and _ah_atr[_ah_cur] is not None and _ah_close > 0:
                                            _ah_atr_pct = float(_ah_atr[_ah_cur]) / _ah_close
                                        # breakout level = the recent COMPLETED-bar swing high
                                        # (the level the add price is measured against for
                                        # extension), excluding the current forming bar.
                                        _ah_k = max(2, int(getattr(settings, "chili_momentum_add_into_halt_swing_lookback", 6) or 6))
                                        _ah_ws = max(0, _ah_cur - _ah_k)
                                        if _ah_ws < _ah_cur:
                                            _ah_level = float(_ah_df["High"].astype(float).iloc[_ah_ws:_ah_cur].max())
                                    else:
                                        _ah_df = None  # too thin for the structure read
                                except Exception:
                                    _ah_df = None
                                    _ah_level = None
                                    _ah_atr_pct = None
                                _ah_ok, _ah_reason, _ah_dbg = add_into_halt_ok(
                                    avg_entry=float(pos["avg_entry_price"]),
                                    original_stop=_orig_stop,
                                    current_stop=float(pos["stop_price"]),
                                    bid=float(bid),
                                    is_limit_up_halt=bool(_is_limit_up),
                                    in_rth=bool(_halt_in_rth),
                                    tape_confirmed=_ah_tape,
                                    breakout_level=_ah_level,
                                    atr_pct=_ah_atr_pct,
                                    df=_ah_df,
                                    consecutive_halt_up_count=int(le.get("halt_chain_up_count") or 0),
                                    halt_level=_float_or_none(le.get("halt_level")),
                                    resumption_open=_float_or_none(le.get("halt_resumption_open")),
                                )
                                if not _ah_ok:
                                    # EXTRA guard refused the halt-add: turn the decision into a
                                    # no-fire (NEVER an exit). The normal (non-halt) add path is
                                    # untouched (this only runs when a halt is suspected).
                                    _emit(db, sess, "live_pyramid_add_into_halt_refused", {
                                        "reason": _ah_reason, **_ah_dbg,
                                    })
                                    _decn = dict(_decn)
                                    _decn["fire"] = False
                                else:
                                    _emit(db, sess, "live_pyramid_add_into_halt_ok", {
                                        "reason": _ah_reason, **_ah_dbg,
                                    })
                            except Exception:
                                # fail-CLOSED: any error refuses the halt-add (never the exit).
                                _decn = dict(_decn)
                                _decn["fire"] = False
                        if _decn.get("fire"):
                            # GUARD #4 ADMISSION — the add is the FIRST post-entry BUY;
                            # it MUST be refused whenever a NEW entry would be refused.
                            # Route through the SAME risk_evaluator admission the
                            # decouple-watching entry path uses (kill-switch, per-broker
                            # + global daily-loss registry, governance inhibit, position
                            # cap, aggregate crypto risk). ABORT THE ADD on refusal —
                            # NEVER the exit (we fall through to the stop-breach below).
                            _adm_ok, _adm_ev = runner_boundary_risk_ok(
                                db, sess, expected_move_bps=_expected_move_bps
                            )
                            # GATE 5 (explosive-mover recalibration): the add is a BUY into
                            # an ALREADY-HELD winner that PASSED admission at entry — a
                            # neural-viability eligibility/freshness FLICKER (stale snapshot)
                            # must not refuse it (3x live risk_admission_refused on held RUN).
                            # Override ONLY when (a) master + sub-flag ON, (b) the position is
                            # held in STATE_LIVE_TRAILING, and (c) the ONLY failing checks are
                            # live_eligible / viability_freshness. Hard risk blocks (kill-switch,
                            # drawdown, daily-loss, position cap) are NEVER overridden — they
                            # fail other check ids, so _only_held_eligibility_flicker_block is
                            # False and the add is still refused. The cushion gate + #769
                            # max-loss circuit still bound the add. OFF ⇒ byte-identical.
                            if (
                                not _adm_ok
                                and bool(getattr(settings, "chili_momentum_explosive_recalibration_enabled", False))
                                and bool(getattr(settings, "chili_momentum_pyramid_skip_viability_recheck", False))
                                and st == STATE_LIVE_TRAILING
                                and _only_held_eligibility_flicker_block(_adm_ev)
                            ):
                                _emit(db, sess, "live_pyramid_add_eligibility_flicker_override", {
                                    "severity": _adm_ev.get("severity"),
                                    "errors": _adm_ev.get("errors"),
                                })
                                _adm_ok = True
                            if not _adm_ok:
                                _emit(db, sess, "live_pyramid_add_blocked", {
                                    "reason": "risk_admission_refused",
                                    "severity": _adm_ev.get("severity"),
                                    "errors": _adm_ev.get("errors"),
                                })
                            else:
                                # SIZE THE ADD via the SAME machinery (never a hardcoded
                                # share block): add_risk_budget = rho * R0, the SAME
                                # frozen ATR (entry_stop_atr_pct => the same d0), at the
                                # guarded ask; notional ceiling = the equity-relative
                                # per-trade notional cap, liquidity-capped on $-vol.
                                _rho = float(getattr(settings, "chili_momentum_pyramid_add_risk_fraction", 0.5) or 0.5)
                                _add_budget = _rho * _R0
                                _pyr_ask = float(ask) if (ask and float(ask) > 0) else float(bid)
                                _pyr_guard_ask = _pyr_ask * _adaptive_notional_guard_multiplier(
                                    expected_move_bps=_expected_move_bps
                                )
                                _eq_shares = not str(sess.symbol or "").upper().endswith("-USD")
                                _inc = prod.base_increment if prod else (1.0 if _eq_shares else None)
                                _mn = prod.base_min_size if prod else (1.0 if _eq_shares else None)
                                _add_atr_pct = _float_or_none(le.get("entry_stop_atr_pct")) or 0.0
                                _add_ceiling = equity_relative_notional_cap(
                                    policy_float_cap(
                                        caps, "max_notional_per_trade_usd",
                                        settings.chili_momentum_risk_max_notional_per_trade_usd,
                                    ),
                                    normalize_execution_family(sess.execution_family),
                                )
                                try:
                                    from .universe import snapshot_dollar_volumes as _pyr_dvol_fn
                                    _pyr_dvol = (_pyr_dvol_fn([sess.symbol]) or {}).get(
                                        str(sess.symbol or "").strip().upper()
                                    )
                                except Exception:
                                    _pyr_dvol = None
                                _add_ceiling = liquidity_capped_notional(_add_ceiling, _pyr_dvol)
                                _qa, _qa_meta = compute_risk_first_quantity(
                                    entry_price=_pyr_guard_ask,
                                    atr_pct=_add_atr_pct,
                                    max_loss_usd=_add_budget,
                                    max_notional_ceiling_usd=_add_ceiling,
                                    base_increment=_inc,
                                    base_min_size=_mn,
                                    stop_atr_mult=float(params.get("stop_atr_mult") or 0.60),
                                )
                                if not _qa or _qa <= 0:
                                    _emit(db, sess, "live_pyramid_add_blocked", {
                                        "reason": "add_size_zero",
                                        "detail": _qa_meta.get("reason"),
                                        "add_budget_usd": round(_add_budget, 2),
                                    })
                                else:
                                    # SUBMIT a marketable-limit BUY using the EXACT working
                                    # entry order shape (the agentic-rail isError on the add
                                    # path was the early shape bug — fixed for entries/exits,
                                    # never back-ported here). post_only only for crypto-maker
                                    # — this path is equity-only, so post_only is never set
                                    # (RH adapter has no such kwarg). DAY tif ("gfd") like the
                                    # entry; extended_hours from the SAME market_session_now
                                    # read so the venue routes pre/after-hours adds. The
                                    # client_order_id mirrors the ENTRY ref-id shape exactly
                                    # (sha1-seeded, 120-char-bounded, per-attempt-unique) so the
                                    # rail's "Reference ID must be unique" contract holds for a
                                    # retried add too.
                                    _pyr_limit_str = _fmt_limit_price_buy(_pyr_guard_ask)
                                    _pyr_place_n = int(le.get("pyramid_place_count", 0) or 0) + 1
                                    le["pyramid_place_count"] = _pyr_place_n
                                    _pyr_id_seed = (
                                        f"{sess.id}|{sess.correlation_id or 'x'}|pyr|{_pyr_place_n}"
                                    ).encode("utf-8")
                                    _pyr_suffix = hashlib.sha1(_pyr_id_seed).hexdigest()[:10]
                                    _pyr_cid = (
                                        f"chili_ml_pyr_{sess.id}_{(sess.correlation_id or 'x')[:8]}_{_pyr_suffix}"
                                    )[:120]
                                    try:
                                        from .market_profile import market_session_now as _pyr_sess_now
                                        _pyr_ext = _pyr_sess_now(sess.symbol) != "regular"
                                    except Exception:
                                        _pyr_ext = False
                                    _pyr_kwargs = dict(
                                        product_id=product_id,
                                        side="buy",
                                        base_size=_fmt_base_size(_qa),
                                        limit_price=_pyr_limit_str,
                                        client_order_id=_pyr_cid,
                                        extended_hours=_pyr_ext,
                                        time_in_force="gfd",
                                    )
                                    _pyr_res = adapter.place_limit_order_gtc(**_pyr_kwargs) or {}
                                    if _pyr_res.get("ok") and _pyr_res.get("order_id"):
                                        # Stash in-flight state. Mutate pos ONLY on the
                                        # confirmed poll (PHASE 1) — NEVER on submit.
                                        le["pyramid_order_id"] = str(_pyr_res["order_id"])
                                        le["pyramid_limit_px"] = float(_pyr_guard_ask)
                                        le["pyramid_pending_R0"] = float(_R0)
                                        le["pyramid_prev_stop"] = float(stop_px)
                                        le["pyramid_confirm_ofi"] = (
                                            None if _pyr_ofi is None else float(_pyr_ofi)
                                        )
                                        le.pop("pyramid_submit_retry_count", None)
                                        _commit_le(sess, le)
                                        _emit(db, sess, "live_pyramid_add_submitted", {
                                            "order_id": le["pyramid_order_id"],
                                            "client_order_id": _pyr_cid,
                                            "add_qty": float(_qa),
                                            "limit_price": _pyr_limit_str,
                                            "R0": float(_R0), "rho": _rho,
                                            "add_budget_usd": round(_add_budget, 2),
                                            "cushion_r": (
                                                round(_decn["cushion_r"], 3)
                                                if _decn.get("cushion_r") is not None else None
                                            ),
                                            "ofi": (None if _pyr_ofi is None else float(_pyr_ofi)),
                                            "iceberg_score": (
                                                None if _iceberg_score is None
                                                else round(float(_iceberg_score), 4)
                                            ),
                                            "stop_at_submit": float(stop_px),
                                        })
                                    else:
                                        # BOUNDED RETRY (explosive-mover recalibration): a
                                        # transient broker isError leaves NO order resting
                                        # (the submit failed before acceptance), so re-attempt
                                        # on a LATER tick (fresh per-attempt ref-id above) up to
                                        # the configured max before giving up. No in-flight
                                        # marker is set on failure, so the next tick re-evaluates
                                        # the add cleanly. MASTER-gated + retry_max default 0 ⇒
                                        # byte-identical (single block emit, as today).
                                        _pyr_retry_max = (
                                            int(getattr(settings, "chili_momentum_pyramid_add_submit_retry_max", 0) or 0)
                                            if bool(getattr(settings, "chili_momentum_explosive_recalibration_enabled", False))
                                            else 0
                                        )
                                        _pyr_retry_n = int(le.get("pyramid_submit_retry_count", 0) or 0)
                                        if _pyr_retry_max > 0 and _pyr_retry_n < _pyr_retry_max:
                                            le["pyramid_submit_retry_count"] = _pyr_retry_n + 1
                                            _commit_le(sess, le)
                                            _emit(db, sess, "live_pyramid_add_submit_retry", {
                                                "error": _pyr_res.get("error"),
                                                "retry_count": _pyr_retry_n + 1,
                                                "retry_max": _pyr_retry_max,
                                                "client_order_id": _pyr_cid,
                                            })
                                        else:
                                            le.pop("pyramid_submit_retry_count", None)
                                            _commit_le(sess, le)
                                            _emit(db, sess, "live_pyramid_add_blocked", {
                                                "reason": "submit_failed",
                                                "error": _pyr_res.get("error"),
                                                "retry_count": _pyr_retry_n,
                                                "retry_max": _pyr_retry_max,
                                            })
                except Exception:
                    # Fail-safe: any pyramid error is swallowed so the exit path below
                    # ALWAYS runs. The add never blocks/delays/loosens an exit.
                    _log.debug("[momentum_live] pyramid add block error", exc_info=True)

            # ── MICRO-PULLBACK RE-ENTRY (Ross "scale out into the pop, RE-LOAD on the
            # next micro-pullback dip"). A PARALLEL sub-branch to the #772 pyramid add:
            # OWN predicate, OWN counter (micropullback_reentry_count), OWN kill-switch,
            # OWN in-flight marker (micropullback_reentry_order_id). The #772 add above
            # is byte-identical when this flag is on/off — they share NOTHING but the
            # pyramid_blend_on_fill / pyramid_risk_anchor_usd rails (so the max-loss
            # circuit keeps re-basing to the STARTER R0). EQUITY-FIRST (crypto deferred).
            #
            # ADDITIVE: when chili_momentum_micropullback_reentry_enabled is False the
            # whole block is a no-op (no re-load, no pos mutation, no emit, no broker
            # call). Two phases (resolve-in-flight, trigger-new), both FALL THROUGH so
            # the stop-breach/exit block below ALWAYS runs this tick. The entire block is
            # in a try/except that swallows to the fall-through — a re-load NEVER blocks,
            # delays, or loosens an exit. SESSION-SCOPED 15s frame from _build_micro_bar_df
            # (the momentum_nbbo_spread_tape resampler), NEVER the 5d fetch_ohlcv_df.
            # (docs/DESIGN/MOMENTUM_LANE.md)
            if bool(getattr(settings, "chili_momentum_micropullback_reentry_enabled", True)):
                try:
                    # PHASE 1 — resolve an IN-FLIGHT re-load order (mirror the pyramid
                    # adopt). Blend ONLY on a CONFIRMED fill via the SHARED helper; the
                    # circuit re-bases to the STARTER R0 (pyramid_risk_anchor_usd). While
                    # an order is in flight PHASE 2 cannot submit a second (idempotency).
                    _mpr_oid = le.get("micropullback_reentry_order_id")
                    if _mpr_oid:
                        _mno, _ = adapter.get_order(str(_mpr_oid))
                        if _mno is not None and not _order_open(_mno):
                            _mpr_filled = float(getattr(_mno, "filled_size", 0) or 0)
                            if _mpr_filled > 0:
                                _qa_f = _mpr_filled
                                _Pa_f = float(
                                    getattr(_mno, "average_filled_price", 0) or 0
                                ) or float(le.get("micropullback_reentry_limit_px") or ask)
                                _q0m = float(pos["quantity"])
                                _a0m = float(pos["avg_entry_price"])
                                _R0m = _float_or_none(le.get("micropullback_reentry_pending_R0"))
                                _prev_stop_m = float(pos["stop_price"])
                                _blend_m = pyramid_blend_on_fill(
                                    q0=_q0m, a0=_a0m, qa_f=_qa_f, Pa_f=_Pa_f,
                                    stop_px=_prev_stop_m,
                                    original_quantity=_float_or_none(pos.get("original_quantity")),
                                )
                                _q1m = _blend_m["q1"]
                                _a1m = _blend_m["a1"]
                                _s1m = _blend_m["s1"]  # INVARIANT-A: tighten-only (asserted)
                                pos["avg_entry_price"] = _a1m
                                pos["quantity"] = _q1m
                                pos["original_quantity"] = _blend_m["original_quantity"]
                                pos["notional_usd"] = _q1m * _a1m
                                pos["stop_price"] = _s1m
                                stop_px = _s1m
                                # Re-base the max-loss circuit to the STARTER R0 (GUARD #1),
                                # VERBATIM with the pyramid add — re-loads NEVER inflate the
                                # per-trade loss budget.
                                if _R0m is not None and _R0m > 0:
                                    le["pyramid_risk_anchor_usd"] = _R0m
                                le["micropullback_reentry_count"] = (
                                    int(le.get("micropullback_reentry_count") or 0) + 1
                                )
                                _add_fee_m = _order_total_fees_usd(_mno) or 0.0
                                if _add_fee_m > 0.0:
                                    le["entry_fee_usd_unbooked"] = (
                                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _add_fee_m
                                    )
                                # RATCHET the shelf to THIS re-load's higher-low so the
                                # NEXT re-load must hold above this dip, not the stale
                                # original breakout (a refinement vs a fixed shelf).
                                _mpr_dip = _float_or_none(le.get("micropullback_pending_dip_low"))
                                if _mpr_dip is not None and _mpr_dip > 0:
                                    le["micropullback_last_shelf"] = _mpr_dip
                                from datetime import timezone as _tz_m
                                _cool_s = max(
                                    float(getattr(settings, "chili_momentum_micropullback_reentry_cooldown_seconds", 30.0) or 30.0),
                                    2.0 * float(getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15),
                                )
                                le["micropullback_reentry_cooldown_until_utc"] = (
                                    datetime.now(_tz_m.utc) + timedelta(seconds=_cool_s)
                                ).replace(tzinfo=None).isoformat()
                                le.pop("micropullback_reentry_order_id", None)
                                le.pop("micropullback_reentry_limit_px", None)
                                le.pop("micropullback_reentry_pending_R0", None)
                                le.pop("micropullback_pending_dip_low", None)
                                le["position"] = pos
                                _commit_le(sess, le)
                                _emit(db, sess, "live_micro_pullback_reentry_fill", {
                                    "add_qty": _qa_f, "add_price": _Pa_f,
                                    "q0": _q0m, "a0": _a0m, "q1": _q1m, "a1": _a1m,
                                    "old_stop": float(le.get("micropullback_prev_stop") or _s1m),
                                    "new_stop": _s1m, "R0": _R0m,
                                    "reentry_count": le["micropullback_reentry_count"],
                                    "shelf": le.get("micropullback_last_shelf"),
                                    "confirm_ofi": le.get("micropullback_confirm_ofi"),
                                    "confirm_trade_flow": le.get("micropullback_confirm_trade_flow"),
                                })
                                le.pop("micropullback_prev_stop", None)
                                le.pop("micropullback_confirm_ofi", None)
                                le.pop("micropullback_confirm_trade_flow", None)
                            else:
                                # Terminal with NO fill — clear the in-flight marker (a
                                # future tick may try again). No pos mutation.
                                le.pop("micropullback_reentry_order_id", None)
                                le.pop("micropullback_reentry_limit_px", None)
                                le.pop("micropullback_reentry_pending_R0", None)
                                le.pop("micropullback_pending_dip_low", None)
                                le.pop("micropullback_prev_stop", None)
                                le.pop("micropullback_confirm_ofi", None)
                                le.pop("micropullback_confirm_trade_flow", None)
                                _commit_le(sess, le)

                    # PHASE 2 — TRIGGER a new re-load (only if none in flight + under cap
                    # + cooldown elapsed). EQUITY-FIRST (crypto deferred per the pyramid
                    # _is_equity_pyr gate). Only on a winning runner (STATE_LIVE_TRAILING).
                    _is_equity_mpr = not str(sess.symbol or "").upper().endswith("-USD")
                    _max_reentries = int(
                        getattr(settings, "chili_momentum_micropullback_reentry_max", 3) or 3
                    )
                    if (
                        _is_equity_mpr
                        and st == STATE_LIVE_TRAILING
                        and not le.get("micropullback_reentry_order_id")
                        and not le.get("pyramid_order_id")  # never two adds in flight at once
                    ):
                        _mpr_count = int(le.get("micropullback_reentry_count") or 0)
                        if _mpr_count >= _max_reentries:
                            pass  # cap reached — no re-load (silently, not every tick noise)
                        else:
                            # COOLDOWN (pinned to >= 2*bar_seconds in the fill handler).
                            _cool_ok = True
                            _cool_raw = le.get("micropullback_reentry_cooldown_until_utc")
                            if _cool_raw:
                                try:
                                    _cool_ok = datetime.utcnow() >= datetime.fromisoformat(str(_cool_raw))
                                except (TypeError, ValueError):
                                    _cool_ok = True
                            if not _cool_ok:
                                _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                    "reason": "cooldown", "until": _cool_raw})
                            else:
                                # GUARD #2 cushion (knife defense) — only re-load when the
                                # runner has ALREADY banked >= min_cushion_r * R0 AND the
                                # stop is at/above the starter entry (breakeven+). A falling
                                # knife never banks cushion ⇒ structurally cannot re-load.
                                _es_m = le.get("entry_sizing") if isinstance(le.get("entry_sizing"), dict) else {}
                                _d0m = _float_or_none(_es_m.get("stop_distance"))
                                if _d0m is None or _d0m <= 0:
                                    _d0m = max(0.0, float(avg) - float(pos["stop_price"])) or None
                                _q0s = (
                                    _float_or_none(pos.get("original_quantity"))
                                    or _float_or_none(pos.get("quantity"))
                                    or 0.0
                                )
                                _a0s = float(pos["avg_entry_price"])
                                _R0_m = (float(_d0m) * float(_q0s)) if (_d0m and _q0s) else None
                                _min_cush = float(
                                    getattr(settings, "chili_momentum_pyramid_min_cushion_r", 1.0) or 1.0
                                )
                                _cushion_usd = (float(bid) - _a0s) * float(_q0s)
                                _cushion_banked = bool(
                                    _R0_m is not None and _R0_m > 0
                                    and _cushion_usd >= _min_cush * _R0_m
                                    and float(stop_px) >= _a0s
                                )
                                if not _cushion_banked:
                                    pass  # no cushion -> no re-load (silent; the common case)
                                else:
                                    # Anti-Ross midday lull (parity with the pyramid add).
                                    _lull_m = False
                                    try:
                                        from .market_profile import in_midday_lull as _mpr_lull
                                        _lull_m = bool(_mpr_lull(sess.symbol))
                                    except Exception:
                                        _lull_m = False
                                    # RATCHETING SHELF: max(starter entry, breakout level,
                                    # last re-load's higher-low). Re-load N must hold above
                                    # re-load N-1's dip, never the stale original breakout.
                                    _shelf = _a0s
                                    _bk = _float_or_none(le.get("breakout_level_price"))
                                    if _bk is not None and _bk > _shelf:
                                        _shelf = _bk
                                    _last_shelf = _float_or_none(le.get("micropullback_last_shelf"))
                                    if _last_shelf is not None and _last_shelf > _shelf:
                                        _shelf = _last_shelf
                                    # SESSION-SCOPED 15s micro-bar frame (NOT the 5d frame).
                                    _bar_s_m = int(
                                        getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15
                                    )
                                    _df_mpr = _build_micro_bar_df(db, sess.symbol, bar_seconds=_bar_s_m)
                                    from .entry_gates import micro_pullback_reentry_detect
                                    from .candles import bounce_curl_from_df
                                    _det = micro_pullback_reentry_detect(
                                        _df_mpr,
                                        shelf=_shelf,
                                        max_dip_pct=float(
                                            getattr(settings, "chili_momentum_micropullback_reentry_max_dip_pct", 0.04) or 0.04
                                        ),
                                    )
                                    # Per-bar curl-conviction confirm (fail-SAFE to False).
                                    _curl_ok = bounce_curl_from_df(_df_mpr)
                                    if not (_det.get("fire") and _curl_ok):
                                        pass  # no micro-pullback geometry this tick (silent)
                                    elif _lull_m:
                                        _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                            "reason": "midday_lull"})
                                    else:
                                        _emit(db, sess, "live_micro_pullback_detected", {
                                            "bounce_high": _det.get("bounce_high"),
                                            "dip_low": _det.get("dip_low"),
                                            "shelf": _shelf, "curl_ok": _curl_ok,
                                        })
                                        # FLOW GATE — route EVERY re-load through the SAME
                                        # _entry_flow_veto VERBATIM (hard negative-side
                                        # precondition: defer if True — never buy into
                                        # selling, the 06-24 fix), THEN require POSITIVE
                                        # confirmation (ofi & trade_flow turning up). The
                                        # veto fails-OPEN on None; the positive-confirm
                                        # fails-CLOSED on None (an extra BUY needs proof).
                                        _mpr_ofi = None
                                        _mpr_tf = None
                                        try:
                                            from .pipeline import _live_ofi_microprice as _mpr_ofi_fn
                                            from .pipeline import _live_trade_flow as _mpr_tf_fn
                                            _mpr_ofi, _ = _mpr_ofi_fn(sess.symbol, db=db)
                                            _mpr_tf = _mpr_tf_fn(sess.symbol, db=db)
                                            _mpr_ofi = None if _mpr_ofi is None else float(_mpr_ofi)
                                            _mpr_tf = None if _mpr_tf is None else float(_mpr_tf)
                                        except Exception:
                                            _mpr_ofi = _mpr_tf = None
                                        _ofi_floor = float(
                                            getattr(settings, "chili_momentum_micropullback_reentry_ofi_thr", 0.30) or 0.30
                                        )
                                        _tf_floor = float(
                                            getattr(settings, "chili_momentum_micropullback_reentry_trade_flow_thr", 0.20) or 0.20
                                        )
                                        _veto = _entry_flow_veto(_mpr_ofi, _mpr_tf, settings)
                                        _pos_confirm = (
                                            _mpr_ofi is not None and _mpr_tf is not None
                                            and _mpr_ofi >= _ofi_floor and _mpr_tf >= _tf_floor
                                        )
                                        if _veto or not _pos_confirm:
                                            _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                "reason": "flow",
                                                "veto": bool(_veto),
                                                "ofi": _mpr_ofi, "trade_flow": _mpr_tf,
                                                "ofi_floor": _ofi_floor, "tf_floor": _tf_floor,
                                            })
                                        else:
                                            # ADMISSION — route the re-load through the SAME
                                            # risk_evaluator gate a NEW entry uses (kill-
                                            # switch, per-broker + global daily-loss,
                                            # drawdown, position cap, aggregate crypto risk).
                                            # ABORT THE ADD on refusal — NEVER the exit.
                                            _adm_ok_m, _adm_ev_m = runner_boundary_risk_ok(
                                                db, sess, expected_move_bps=_expected_move_bps
                                            )
                                            if not _adm_ok_m:
                                                _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                    "reason": "risk_admission",
                                                    "severity": _adm_ev_m.get("severity"),
                                                    "errors": _adm_ev_m.get("errors"),
                                                })
                                            elif not (_R0_m and _R0_m > 0):
                                                _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                    "reason": "bad_R0"})
                                            else:
                                                # SIZE via the SAME machinery as the pyramid
                                                # add: re-load risk budget = rho_reload * R0,
                                                # at the guarded ask, liquidity-capped on
                                                # $-vol, equity-relative notional ceiling.
                                                _rho_m = float(
                                                    getattr(settings, "chili_momentum_micropullback_reentry_risk_fraction", 0.30) or 0.30
                                                )
                                                _budget_m = _rho_m * _R0_m
                                                _mpr_ask = float(ask) if (ask and float(ask) > 0) else float(bid)
                                                _mpr_guard_ask = _mpr_ask * _adaptive_notional_guard_multiplier(
                                                    expected_move_bps=_expected_move_bps
                                                )
                                                _inc_m = prod.base_increment if prod else 1.0
                                                _mn_m = prod.base_min_size if prod else 1.0
                                                _atr_m = _float_or_none(le.get("entry_stop_atr_pct")) or 0.0
                                                _ceil_m = equity_relative_notional_cap(
                                                    policy_float_cap(
                                                        caps, "max_notional_per_trade_usd",
                                                        settings.chili_momentum_risk_max_notional_per_trade_usd,
                                                    ),
                                                    normalize_execution_family(sess.execution_family),
                                                )
                                                try:
                                                    from .universe import snapshot_dollar_volumes as _mpr_dvol_fn
                                                    _mpr_dvol = (_mpr_dvol_fn([sess.symbol]) or {}).get(
                                                        str(sess.symbol or "").strip().upper()
                                                    )
                                                except Exception:
                                                    _mpr_dvol = None
                                                _ceil_m = liquidity_capped_notional(_ceil_m, _mpr_dvol)
                                                _qa_m, _qa_meta_m = compute_risk_first_quantity(
                                                    entry_price=_mpr_guard_ask,
                                                    atr_pct=_atr_m,
                                                    max_loss_usd=_budget_m,
                                                    max_notional_ceiling_usd=_ceil_m,
                                                    base_increment=_inc_m,
                                                    base_min_size=_mn_m,
                                                    stop_atr_mult=float(params.get("stop_atr_mult") or 0.60),
                                                )
                                                if not _qa_m or _qa_m <= 0:
                                                    _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                        "reason": "size_zero",
                                                        "detail": _qa_meta_m.get("reason"),
                                                        "budget_usd": round(_budget_m, 2),
                                                    })
                                                else:
                                                    _mpr_limit_str = _fmt_limit_price_buy(_mpr_guard_ask)
                                                    _mpr_cid = (
                                                        f"chili_ml_mpr_{sess.id}_{uuid.uuid4().hex[:12]}"
                                                    )
                                                    try:
                                                        from .market_profile import market_session_now as _mpr_sess_now
                                                        _mpr_ext = _mpr_sess_now(sess.symbol) != "regular"
                                                    except Exception:
                                                        _mpr_ext = False
                                                    _mpr_res = adapter.place_limit_order_gtc(
                                                        product_id=product_id,
                                                        side="buy",
                                                        base_size=_fmt_base_size(_qa_m),
                                                        limit_price=_mpr_limit_str,
                                                        client_order_id=_mpr_cid,
                                                        extended_hours=_mpr_ext,
                                                        time_in_force="gfd",
                                                    ) or {}
                                                    if _mpr_res.get("ok") and _mpr_res.get("order_id"):
                                                        le["micropullback_reentry_order_id"] = str(_mpr_res["order_id"])
                                                        le["micropullback_reentry_limit_px"] = float(_mpr_guard_ask)
                                                        le["micropullback_reentry_pending_R0"] = float(_R0_m)
                                                        le["micropullback_prev_stop"] = float(stop_px)
                                                        le["micropullback_pending_dip_low"] = _float_or_none(_det.get("dip_low"))
                                                        le["micropullback_confirm_ofi"] = _mpr_ofi
                                                        le["micropullback_confirm_trade_flow"] = _mpr_tf
                                                        _commit_le(sess, le)
                                                        _emit(db, sess, "live_micro_pullback_reentry_submitted", {
                                                            "order_id": le["micropullback_reentry_order_id"],
                                                            "client_order_id": _mpr_cid,
                                                            "add_qty": float(_qa_m),
                                                            "limit_price": _mpr_limit_str,
                                                            "R0": float(_R0_m), "rho": _rho_m,
                                                            "budget_usd": round(_budget_m, 2),
                                                            "reentry_count": _mpr_count,
                                                            "shelf": _shelf,
                                                            "bounce_high": _det.get("bounce_high"),
                                                            "dip_low": _det.get("dip_low"),
                                                            "ofi": _mpr_ofi, "trade_flow": _mpr_tf,
                                                            "stop_at_submit": float(stop_px),
                                                        })
                                                    else:
                                                        _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                            "reason": "submit_failed",
                                                            "error": _mpr_res.get("error"),
                                                        })
                except Exception:
                    # Fail-safe: any re-load error is swallowed so the exit path below
                    # ALWAYS runs. The re-load NEVER blocks/delays/loosens an exit.
                    _log.debug("[momentum_live] micro-pullback re-entry block error", exc_info=True)

            # ── ROSS BUY-THE-DIP / PULLBACK ADD (the operator ask) ───────────────
            # The #772 pyramid + the micro-pullback re-load both add on CONTINUATION
            # (UP/new-HOD, dip-and-curl). Ross ALSO buys the controlled PULLBACK to
            # SUPPORT (a higher-low / breakout shelf / VWAP) in an INTACT uptrend. This
            # is the THIRD, distinct add sub-branch: OWN predicate (pullback_add_decision),
            # OWN counter (pullback_add_count), OWN kill-switch, OWN in-flight marker
            # (pullback_add_order_id), OWN cooldown. The ⭐ FALLING-KNIFE GUARD is the
            # JUST-shipped front-side strength (front_side_strength_score >= an adaptive
            # floor) + OFI-not-collapsing + above-VWAP-or-reclaiming + higher-low — if ANY
            # fail it is a knife, not a dip, and NO add fires (bias toward not adding).
            #
            # COMPOSES with the other two: it REFUSES whenever the UP-pyramid OR the micro-
            # pullback has an add in flight (never two adds on one tick). Re-loads route
            # through pyramid_blend_on_fill + pyramid_risk_anchor_usd VERBATIM so the #769
            # max-loss circuit re-bases to the STARTER R0 (worst-case pullback-add risk =
            # max * fraction * R0 on top of the starter). EQUITY-FIRST (crypto deferred).
            # ADDITIVE: flag OFF ⇒ the whole block is a no-op (byte-identical: no add, no
            # pos mutation, no emit, no broker call). Two phases (resolve-in-flight, trigger-
            # new), both FALL THROUGH so the stop-breach/exit block below ALWAYS runs this
            # tick. The entire block is in a try/except that swallows to the fall-through —
            # a pullback-add NEVER blocks, delays, or loosens an exit. SESSION-SCOPED 15s
            # micro-bar frame from _build_micro_bar_df (NOT the 5d fetch_ohlcv_df) for the
            # structure read; the front-side strength reuses the SAME live OFI/tape reads.
            # This is an ADD lever, NEVER a veto. (docs/DESIGN/MOMENTUM_LANE.md)
            if bool(getattr(settings, "chili_momentum_pullback_add_enabled", True)):
                try:
                    # PHASE 1 — resolve an IN-FLIGHT pullback-add order (mirror the pyramid
                    # adopt). Blend ONLY on a CONFIRMED fill via the SHARED helper; the
                    # circuit re-bases to the STARTER R0 (pyramid_risk_anchor_usd). While an
                    # order is in flight PHASE 2 cannot submit a second (idempotency).
                    _pba_oid = le.get("pullback_add_order_id")
                    if _pba_oid:
                        _pbno, _ = adapter.get_order(str(_pba_oid))
                        if _pbno is not None and not _order_open(_pbno):
                            _pba_filled = float(getattr(_pbno, "filled_size", 0) or 0)
                            if _pba_filled > 0:
                                _qa_f = _pba_filled
                                _Pa_f = float(
                                    getattr(_pbno, "average_filled_price", 0) or 0
                                ) or float(le.get("pullback_add_limit_px") or ask)
                                _q0p = float(pos["quantity"])
                                _a0p = float(pos["avg_entry_price"])
                                _R0p = _float_or_none(le.get("pullback_add_pending_R0"))
                                _prev_stop_p = float(pos["stop_price"])
                                _blend_p = pyramid_blend_on_fill(
                                    q0=_q0p, a0=_a0p, qa_f=_qa_f, Pa_f=_Pa_f,
                                    stop_px=_prev_stop_p,
                                    original_quantity=_float_or_none(pos.get("original_quantity")),
                                )
                                _q1p = _blend_p["q1"]
                                _a1p = _blend_p["a1"]
                                _s1p = _blend_p["s1"]  # INVARIANT-A: tighten-only (asserted)
                                pos["avg_entry_price"] = _a1p
                                pos["quantity"] = _q1p
                                pos["original_quantity"] = _blend_p["original_quantity"]
                                pos["notional_usd"] = _q1p * _a1p
                                pos["stop_price"] = _s1p
                                stop_px = _s1p
                                # Re-base the max-loss circuit to the STARTER R0 (GUARD #1),
                                # VERBATIM with the pyramid add — adds NEVER inflate the
                                # per-trade loss budget.
                                if _R0p is not None and _R0p > 0:
                                    le["pyramid_risk_anchor_usd"] = _R0p
                                le["pullback_add_count"] = int(le.get("pullback_add_count") or 0) + 1
                                _add_fee_p = _order_total_fees_usd(_pbno) or 0.0
                                if _add_fee_p > 0.0:
                                    le["entry_fee_usd_unbooked"] = (
                                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _add_fee_p
                                    )
                                # RATCHET the higher-low reference to THIS pullback's low so
                                # the NEXT pullback-add must make a HIGHER low than this one.
                                _pba_low = _float_or_none(le.get("pullback_add_pending_low"))
                                if _pba_low is not None and _pba_low > 0:
                                    le["pullback_add_last_low"] = _pba_low
                                from datetime import timezone as _tz_p
                                _cool_s_p = max(
                                    float(getattr(settings, "chili_momentum_pullback_add_cooldown_seconds", 30.0) or 30.0),
                                    2.0 * float(getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15),
                                )
                                le["pullback_add_cooldown_until_utc"] = (
                                    datetime.now(_tz_p.utc) + timedelta(seconds=_cool_s_p)
                                ).replace(tzinfo=None).isoformat()
                                le.pop("pullback_add_order_id", None)
                                le.pop("pullback_add_limit_px", None)
                                le.pop("pullback_add_pending_R0", None)
                                le.pop("pullback_add_pending_low", None)
                                le["position"] = pos
                                _commit_le(sess, le)
                                _emit(db, sess, "live_pullback_add_fill", {
                                    "add_qty": _qa_f, "add_price": _Pa_f,
                                    "q0": _q0p, "a0": _a0p, "q1": _q1p, "a1": _a1p,
                                    "old_stop": float(le.get("pullback_add_prev_stop") or _s1p),
                                    "new_stop": _s1p, "R0": _R0p,
                                    "add_count": le["pullback_add_count"],
                                    "higher_low": le.get("pullback_add_last_low"),
                                    "strength": le.get("pullback_add_confirm_strength"),
                                    "ofi": le.get("pullback_add_confirm_ofi"),
                                })
                                le.pop("pullback_add_prev_stop", None)
                                le.pop("pullback_add_confirm_strength", None)
                                le.pop("pullback_add_confirm_ofi", None)
                            else:
                                # Terminal with NO fill — clear the in-flight marker (a
                                # future tick may try again). No pos mutation.
                                le.pop("pullback_add_order_id", None)
                                le.pop("pullback_add_limit_px", None)
                                le.pop("pullback_add_pending_R0", None)
                                le.pop("pullback_add_pending_low", None)
                                le.pop("pullback_add_prev_stop", None)
                                le.pop("pullback_add_confirm_strength", None)
                                le.pop("pullback_add_confirm_ofi", None)
                                _commit_le(sess, le)

                    # PHASE 2 — TRIGGER a new pullback-add (only if none in flight + under
                    # cap + cooldown elapsed + NO other add in flight). EQUITY-FIRST.
                    _is_equity_pba = not str(sess.symbol or "").upper().endswith("-USD")
                    _max_pba = int(getattr(settings, "chili_momentum_pullback_add_max", 2) or 2)
                    _other_add_in_flight = bool(
                        le.get("pyramid_order_id") or le.get("micropullback_reentry_order_id")
                    )
                    if (
                        st == STATE_LIVE_TRAILING
                        and not le.get("pullback_add_order_id")
                    ):
                        # STARTER structural risk R0 = d0 * q0 (the frozen entry stop_distance
                        # * the starter qty), funded off the full starter so a post-partial
                        # runner still sizes the add off the original R.
                        _es_p = le.get("entry_sizing") if isinstance(le.get("entry_sizing"), dict) else {}
                        _d0_p = _float_or_none(_es_p.get("stop_distance"))
                        if _d0_p is None or _d0_p <= 0:
                            _d0_p = max(0.0, float(avg) - float(pos["stop_price"])) or None
                        _q0_p = (
                            _float_or_none(pos.get("original_quantity"))
                            or _float_or_none(pos.get("quantity"))
                            or 0.0
                        )
                        _a0_p = float(pos["avg_entry_price"])
                        # COOLDOWN.
                        _cool_active_p = False
                        _cool_raw_p = le.get("pullback_add_cooldown_until_utc")
                        if _cool_raw_p:
                            try:
                                _cool_active_p = datetime.utcnow() < datetime.fromisoformat(str(_cool_raw_p))
                            except (TypeError, ValueError):
                                _cool_active_p = False
                        # Anti-Ross midday lull (parity with the pyramid add).
                        _lull_p = False
                        try:
                            from .market_profile import in_midday_lull as _pba_lull
                            _lull_p = bool(_pba_lull(sess.symbol))
                        except Exception:
                            _lull_p = False
                        # SUPPORT zone + higher-low structure on the SESSION-scoped 15s micro-
                        # bar frame (NOT the 5d frame). The ratcheting SUPPORT shelf = max(
                        # starter entry, breakout level, last pullback-add's higher-low); the
                        # detector finds the recent bounce-high → dip-low that holds the shelf.
                        _shelf_p = _a0_p
                        _bk_p = _float_or_none(le.get("breakout_level_price"))
                        if _bk_p is not None and _bk_p > _shelf_p:
                            _shelf_p = _bk_p
                        _last_low_p = _float_or_none(le.get("pullback_add_last_low"))
                        if _last_low_p is not None and _last_low_p > _shelf_p:
                            _shelf_p = _last_low_p
                        _bar_s_p = int(getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15)
                        _df_pba = _build_micro_bar_df(db, sess.symbol, bar_seconds=_bar_s_p)
                        # Structure read (reuse the micro-pullback detector for the higher-low
                        # dip-and-bounce geometry; the depth BAND + the front-side knife guard
                        # are applied by pullback_add_decision below). Use a generous dip cap
                        # here so the detector returns the geometry; the controlled-depth BAND
                        # (lo/hi frac of the move range) is the real depth gate in the predicate.
                        _pb_low = None
                        _bounce_high = None
                        _bounced_p = False
                        try:
                            from .entry_gates import micro_pullback_reentry_detect as _pba_detect
                            from .candles import bounce_curl_from_df as _pba_curl
                            _det_p = _pba_detect(
                                _df_pba, shelf=_shelf_p,
                                max_dip_pct=float(
                                    getattr(settings, "chili_momentum_pullback_add_depth_hi_frac", 0.62) or 0.62
                                ),
                            )
                            _pb_low = _float_or_none(_det_p.get("dip_low"))
                            _bounce_high = _float_or_none(_det_p.get("bounce_high"))
                            # BOUNCE confirm: the detector fired (higher-low dip held the shelf
                            # + the last bar curled up) AND the per-bar curl shape is green.
                            _bounced_p = bool(_det_p.get("fire")) and bool(_pba_curl(_df_pba))
                        except Exception:
                            _pb_low = _bounce_high = None
                            _bounced_p = False
                        # The HWM (move top) + the move range (HWM - the day/session low proxy)
                        # for the controlled-depth band. high_water_mark is the position HWM;
                        # the move base = the starter entry (a conservative range floor that
                        # makes depth_frac a fraction of the BANKED move, never inflated).
                        _hwm_p = _float_or_none(pos.get("high_water_mark")) or float(bid)
                        _move_base_p = min(_a0_p, _shelf_p)
                        _move_range_p = (_hwm_p - _move_base_p) if (_hwm_p and _move_base_p) else None
                        # The PRIOR higher-low = the ratcheting shelf (the level the new dip
                        # must print a HIGHER low than). The starter entry / breakout / last
                        # add's low — whichever is highest — is what "higher low" is measured
                        # against, so each pullback-add steps the structure UP.
                        _prior_low_p = _shelf_p
                        # ⭐ FALLING-KNIFE GUARD — front-side strength (the JUST-shipped score),
                        # OFI level+slope, and above-VWAP-or-reclaiming, computed on the SAME
                        # canonical today-session frame + live OFI/tape reads the ENTRY-side
                        # size-tilt uses (one source of truth). FAIL-CLOSED: any missing leg ⇒
                        # the strength/OFI stays None ⇒ pullback_add_decision refuses the add.
                        _fs_score_p = None
                        _fs_ofi_lvl_p = None
                        _fs_ofi_slp_p = None
                        _above_vwap_p = False
                        try:
                            from .ross_momentum import (
                                front_side_state as _pba_state_fn,
                                front_side_strength_score as _pba_strength_fn,
                            )
                            from .entry_gates import _today_session_frame as _pba_today_frame
                            from .pipeline import (
                                _live_flow_slope as _pba_flow_fn,
                                _live_trade_flow as _pba_tape_fn,
                            )
                            # Live order-flow (the SAME reads the entry-side tilt + the RIDE
                            # lock use). None ⇒ stale/crypto ⇒ the OFI guard fails closed.
                            _pba_flow = _pba_flow_fn(sess.symbol, db=db)
                            if isinstance(_pba_flow, dict):
                                _fs_ofi_lvl_p = _float_or_none(_pba_flow.get("ofi_level"))
                                _fs_ofi_slp_p = _float_or_none(_pba_flow.get("ofi_slope"))
                            try:
                                _pba_tape = _pba_tape_fn(sess.symbol, db=db)
                                _pba_tape = None if _pba_tape is None else float(_pba_tape)
                            except Exception:
                                _pba_tape = None
                            # The today-session frame for the ER spine + VWAP read. Prefer the
                            # 5d/15m fetch (the canonical session anchor) so the VWAP/range
                            # match the entry-gate read; fall back to the micro-bar frame.
                            _pba_sess_df = None
                            try:
                                _pba_fetch = _replay_aware_fetch_ohlcv_df
                                _pba_iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                                _pba_raw = _pba_fetch(sess.symbol, interval=_pba_iv, period="5d")
                                if _pba_raw is not None and not getattr(_pba_raw, "empty", True):
                                    _pba_sess_df = _pba_today_frame(_pba_raw)
                            except Exception:
                                _pba_sess_df = None
                            if _pba_sess_df is None or getattr(_pba_sess_df, "empty", True):
                                _pba_sess_df = _df_pba
                            _fs_closes_p = None
                            _fs_vwap_dist_p = None
                            _fs_range_pos_p = None
                            if _pba_sess_df is not None and not getattr(_pba_sess_df, "empty", True):
                                _pba_state = _pba_state_fn(_pba_sess_df)
                                _fs_vwap_dist_p = _float_or_none(getattr(_pba_state, "vwap_dist_sigma", None))
                                _fs_range_pos_p = _float_or_none(getattr(_pba_state, "day_range_pos", None))
                                # above-VWAP OR cleanly RECLAIMING (vwap_dist turning >= 0 / the
                                # state's above_vwap flag). below-VWAP-falling ⇒ knife ⇒ refuse.
                                _above_vwap_p = bool(getattr(_pba_state, "above_vwap", False)) or (
                                    _fs_vwap_dist_p is not None and _fs_vwap_dist_p >= 0.0
                                )
                                try:
                                    _fs_closes_p = [
                                        float(c)
                                        for c in _pba_sess_df["Close"].astype(float).tolist()[-12:]
                                    ]
                                except Exception:
                                    _fs_closes_p = None
                            _fs_score_p = _pba_strength_fn(
                                closes=_fs_closes_p,
                                vwap_dist_sigma=_fs_vwap_dist_p,
                                day_range_pos=_fs_range_pos_p,
                                ofi_level=_fs_ofi_lvl_p,
                                ofi_slope=_fs_ofi_slp_p,
                                signed_tape=_pba_tape,
                            )
                        except Exception:
                            _fs_score_p = None
                            _fs_ofi_lvl_p = _fs_ofi_slp_p = None
                            _above_vwap_p = False
                        # The STRENGTH FLOOR — the documented base, raised to the regime-
                        # adaptive p25 (the entry-side s_lo) when that distribution is WARM and
                        # higher (so the knife guard tracks the live regime, not a fixed magic).
                        _strength_floor_p = float(
                            getattr(settings, "chili_momentum_pullback_add_strength_floor", 0.50) or 0.50
                        )
                        try:
                            _fs_adapt_p = _frontside_adaptive_thresholds()
                            if _fs_adapt_p is not None:
                                _adapt_lo_p = float(_fs_adapt_p[0])
                                if math.isfinite(_adapt_lo_p) and _adapt_lo_p > _strength_floor_p:
                                    _strength_floor_p = _adapt_lo_p
                        except Exception:
                            pass
                        # SHARED pure predicate — the falling-knife-guarded BUY-THE-DIP gate.
                        _decn_p = pullback_add_decision(
                            enabled=True,  # outer block already gated on the flag
                            is_equity=_is_equity_pba,
                            add_count=int(le.get("pullback_add_count") or 0),
                            max_adds=_max_pba,
                            in_flight=bool(le.get("pullback_add_order_id")),
                            other_add_in_flight=_other_add_in_flight,
                            a0=_a0_p,
                            q0=_q0_p,
                            d0=_d0_p,
                            bid=float(bid),
                            stop_px=float(stop_px),
                            high_water_mark=_hwm_p,
                            support_level=_shelf_p,
                            pullback_low=_pb_low,
                            prior_pullback_low=_prior_low_p,
                            move_range=_move_range_p,
                            pullback_depth_lo_frac=float(
                                getattr(settings, "chili_momentum_pullback_add_depth_lo_frac", 0.20) or 0.20
                            ),
                            pullback_depth_hi_frac=float(
                                getattr(settings, "chili_momentum_pullback_add_depth_hi_frac", 0.62) or 0.62
                            ),
                            bounced=_bounced_p,
                            front_side_strength=_fs_score_p,
                            strength_floor=_strength_floor_p,
                            above_vwap_or_reclaiming=_above_vwap_p,
                            ofi_level=_fs_ofi_lvl_p,
                            ofi_slope=_fs_ofi_slp_p,
                            midday_lull=_lull_p,
                            cooldown_active=_cool_active_p,
                        )
                        _R0_p = _decn_p.get("R0")
                        if not _decn_p.get("fire"):
                            # Emit only on the INFORMATIVE near-misses (a fired-detector that a
                            # guard then refused), not the silent common "no geometry" case.
                            if _bounced_p and _decn_p.get("reason") not in (
                                "no_support_structure", "no_move_range", "no_bounce",
                            ):
                                _emit(db, sess, "live_pullback_add_vetoed", {
                                    "reason": _decn_p.get("reason"),
                                    "strength": (None if _fs_score_p is None else round(float(_fs_score_p), 4)),
                                    "strength_floor": round(float(_strength_floor_p), 4),
                                    "ofi_level": _fs_ofi_lvl_p,
                                    "ofi_slope": _fs_ofi_slp_p,
                                    "above_vwap": bool(_above_vwap_p),
                                    "pullback_low": _pb_low,
                                    "prior_low": _prior_low_p,
                                    "depth_frac": _decn_p.get("pullback_depth_frac"),
                                })
                        elif not (_R0_p and _R0_p > 0):
                            _emit(db, sess, "live_pullback_add_vetoed", {"reason": "bad_R0"})
                        else:
                            # GUARD #4 ADMISSION — the add is a post-entry BUY; refuse it
                            # whenever a NEW entry would be refused (kill-switch, per-broker +
                            # global daily-loss, drawdown, position cap, aggregate crypto risk).
                            # ABORT THE ADD on refusal — NEVER the exit.
                            _adm_ok_p, _adm_ev_p = runner_boundary_risk_ok(
                                db, sess, expected_move_bps=_expected_move_bps
                            )
                            if not _adm_ok_p:
                                _emit(db, sess, "live_pullback_add_vetoed", {
                                    "reason": "risk_admission",
                                    "severity": _adm_ev_p.get("severity"),
                                    "errors": _adm_ev_p.get("errors"),
                                })
                            else:
                                # SIZE via the SAME machinery as the pyramid add: the add's
                                # risk budget = rho * R0 (conservative — Ross sizes the
                                # pullback-add small), at the guarded ask, liquidity-capped on
                                # $-vol, equity-relative notional ceiling. The add's stop sits
                                # just below the pullback's higher-low (_decn_p["add_stop"]);
                                # the risk-first sizer keys off the frozen entry ATR so the
                                # combined position's worst-case stays bounded by R0 (the #769
                                # circuit re-bases to R0 on the blend).
                                _rho_p = float(
                                    getattr(settings, "chili_momentum_pullback_add_risk_fraction", 0.5) or 0.5
                                )
                                _budget_p = _rho_p * _R0_p
                                _pba_ask = float(ask) if (ask and float(ask) > 0) else float(bid)
                                _pba_guard_ask = _pba_ask * _adaptive_notional_guard_multiplier(
                                    expected_move_bps=_expected_move_bps
                                )
                                _inc_p = prod.base_increment if prod else 1.0
                                _mn_p = prod.base_min_size if prod else 1.0
                                _atr_p = _float_or_none(le.get("entry_stop_atr_pct")) or 0.0
                                _ceil_p = equity_relative_notional_cap(
                                    policy_float_cap(
                                        caps, "max_notional_per_trade_usd",
                                        settings.chili_momentum_risk_max_notional_per_trade_usd,
                                    ),
                                    normalize_execution_family(sess.execution_family),
                                )
                                try:
                                    from .universe import snapshot_dollar_volumes as _pba_dvol_fn
                                    _pba_dvol = (_pba_dvol_fn([sess.symbol]) or {}).get(
                                        str(sess.symbol or "").strip().upper()
                                    )
                                except Exception:
                                    _pba_dvol = None
                                _ceil_p = liquidity_capped_notional(_ceil_p, _pba_dvol)
                                # The add can never be LARGER than the starter (Ross sizes the
                                # pullback-add conservatively): cap the notional ceiling at the
                                # starter's notional so qty_add <= q0 even if the budget allowed
                                # more (rho<=1 already keeps the R bounded; this bounds the SHARE
                                # count too).
                                _starter_notional_p = _q0_p * _a0_p
                                if _starter_notional_p > 0:
                                    _ceil_p = min(_ceil_p, _starter_notional_p)
                                _qa_p, _qa_meta_p = compute_risk_first_quantity(
                                    entry_price=_pba_guard_ask,
                                    atr_pct=_atr_p,
                                    max_loss_usd=_budget_p,
                                    max_notional_ceiling_usd=_ceil_p,
                                    base_increment=_inc_p,
                                    base_min_size=_mn_p,
                                    stop_atr_mult=float(params.get("stop_atr_mult") or 0.60),
                                )
                                if not _qa_p or _qa_p <= 0:
                                    _emit(db, sess, "live_pullback_add_vetoed", {
                                        "reason": "size_zero",
                                        "detail": _qa_meta_p.get("reason"),
                                        "budget_usd": round(_budget_p, 2),
                                    })
                                else:
                                    _pba_limit_str = _fmt_limit_price_buy(_pba_guard_ask)
                                    _pba_cid = (
                                        f"chili_ml_pba_{sess.id}_{uuid.uuid4().hex[:12]}"
                                    )
                                    try:
                                        from .market_profile import market_session_now as _pba_sess_now
                                        _pba_ext = _pba_sess_now(sess.symbol) != "regular"
                                    except Exception:
                                        _pba_ext = False
                                    _pba_res = adapter.place_limit_order_gtc(
                                        product_id=product_id,
                                        side="buy",
                                        base_size=_fmt_base_size(_qa_p),
                                        limit_price=_pba_limit_str,
                                        client_order_id=_pba_cid,
                                        extended_hours=_pba_ext,
                                        time_in_force="gfd",
                                    ) or {}
                                    if _pba_res.get("ok") and _pba_res.get("order_id"):
                                        le["pullback_add_order_id"] = str(_pba_res["order_id"])
                                        le["pullback_add_limit_px"] = float(_pba_guard_ask)
                                        le["pullback_add_pending_R0"] = float(_R0_p)
                                        le["pullback_add_prev_stop"] = float(stop_px)
                                        le["pullback_add_pending_low"] = _float_or_none(_decn_p.get("add_stop"))
                                        le["pullback_add_confirm_strength"] = (
                                            None if _fs_score_p is None else round(float(_fs_score_p), 4)
                                        )
                                        le["pullback_add_confirm_ofi"] = _fs_ofi_lvl_p
                                        _commit_le(sess, le)
                                        _emit(db, sess, "live_pullback_add_fired", {
                                            "order_id": le["pullback_add_order_id"],
                                            "client_order_id": _pba_cid,
                                            "add_qty": float(_qa_p),
                                            "limit_price": _pba_limit_str,
                                            "R0": float(_R0_p), "rho": _rho_p,
                                            "budget_usd": round(_budget_p, 2),
                                            "add_count": int(le.get("pullback_add_count") or 0),
                                            "support": _shelf_p,
                                            "pullback_low": _pb_low,
                                            "bounce_high": _bounce_high,
                                            "depth_frac": (
                                                round(float(_decn_p["pullback_depth_frac"]), 4)
                                                if _decn_p.get("pullback_depth_frac") is not None else None
                                            ),
                                            "strength": (None if _fs_score_p is None else round(float(_fs_score_p), 4)),
                                            "strength_floor": round(float(_strength_floor_p), 4),
                                            "ofi_level": _fs_ofi_lvl_p,
                                            "ofi_slope": _fs_ofi_slp_p,
                                            "above_vwap": bool(_above_vwap_p),
                                            "stop_at_submit": float(stop_px),
                                        })
                                    else:
                                        _emit(db, sess, "live_pullback_add_vetoed", {
                                            "reason": "submit_failed",
                                            "error": _pba_res.get("error"),
                                        })
                except Exception:
                    # Fail-safe: any pullback-add error is swallowed so the exit path below
                    # ALWAYS runs. The pullback-add NEVER blocks/delays/loosens an exit.
                    _log.debug("[momentum_live] pullback-add block error", exc_info=True)

            # ── ROSS ADD-ON-FLAG-BREAKOUT (the FOURTH held-position add) ─────────
            # The UP-pyramid adds on a new-HOD + OFI thrust; the micro-pullback re-loads on a
            # shallow dip-and-curl; the BUY-THE-DIP pullback-add re-loads on a bounce off
            # support. Ross ALSO adds when a held winner consolidates into a tight BULL FLAG
            # (a base after the impulse) and then BREAKS the flag's swing high — a CONTINUATION
            # add at the breakout (which may be the FIRST new high after the base, NOT a fresh
            # day-HOD pyramid, and NOT a dip-bounce). This is the THIRD-and-a-half distinct add
            # sub-branch: OWN predicate (flag_breakout_add_decision), OWN counter
            # (flag_breakout_add_count), OWN kill-switch, OWN in-flight marker
            # (flag_breakout_add_order_id), OWN cooldown.
            #
            # FLAG DETECTION REUSE: bull_flag_confirmation (the SAME detector the fresh-ENTRY
            # lane uses, entry_gates.py) runs on the HELD position's recent bars. Its ok=True +
            # debug["pullback_high"] (= the flag swing-high / break level) + debug["pullback_low"]
            # (= the flag low / structural stop) + debug["above_vwap"] are the flag geometry +
            # confirmed break. The detector itself enforces the tight-consolidation depth band,
            # the EMA-9 hold, the light-pullback volume, the anti-chase / backside / L2-hidden-
            # seller vetoes, the NOT-PARABOLIC extension guard, and the genuine swing-high break
            # WITH tape — so a sloppy breakdown dressed as a flag never returns ok=True.
            #
            # ⭐ FALLING-KNIFE / QUALITY GUARD (identical discipline to the dip-add): the add
            # fires ONLY when front_side_strength >= an adaptive floor + OFI not collapsing +
            # above-VWAP-or-reclaiming + a HIGHER base than the prior flag + a GENUINE break
            # (bid clears the flag top by >= a margin-frac of the flag range, not a 1-tick wick).
            # FAIL-CLOSED on any missing input ⇒ NO add (bias toward not adding).
            #
            # COMPOSES with the other 3: it REFUSES whenever the UP-pyramid OR the micro-pullback
            # OR the buy-the-dip pullback-add has an add in flight (never two adds on one tick) +
            # its own cooldown. Re-loads route through pyramid_blend_on_fill +
            # pyramid_risk_anchor_usd VERBATIM so the #769 max-loss circuit re-bases to the
            # STARTER R0 (worst-case flag-add risk = max * fraction * R0 on top of the starter).
            # EQUITY-FIRST (crypto deferred). ADDITIVE: flag OFF ⇒ the whole block is a no-op
            # (byte-identical). Two phases (resolve-in-flight, trigger-new), both FALL THROUGH
            # so the stop-breach/exit block below ALWAYS runs this tick. The entire block is in a
            # try/except that swallows to the fall-through — a flag-add NEVER blocks, delays, or
            # loosens an exit. This is an ADD lever, NEVER a veto. (docs/DESIGN/MOMENTUM_LANE.md)
            if bool(getattr(settings, "chili_momentum_flag_breakout_add_enabled", True)):
                try:
                    # PHASE 1 — resolve an IN-FLIGHT flag-breakout-add order (mirror the pyramid
                    # adopt). Blend ONLY on a CONFIRMED fill via the SHARED helper; the circuit
                    # re-bases to the STARTER R0 (pyramid_risk_anchor_usd). While an order is in
                    # flight PHASE 2 cannot submit a second (idempotency).
                    _fba_oid = le.get("flag_breakout_add_order_id")
                    if _fba_oid:
                        _fbno, _ = adapter.get_order(str(_fba_oid))
                        if _fbno is not None and not _order_open(_fbno):
                            _fba_filled = float(getattr(_fbno, "filled_size", 0) or 0)
                            if _fba_filled > 0:
                                _qa_fb = _fba_filled
                                _Pa_fb = float(
                                    getattr(_fbno, "average_filled_price", 0) or 0
                                ) or float(le.get("flag_breakout_add_limit_px") or ask)
                                _q0fb = float(pos["quantity"])
                                _a0fb = float(pos["avg_entry_price"])
                                _R0fb = _float_or_none(le.get("flag_breakout_add_pending_R0"))
                                _prev_stop_fb = float(pos["stop_price"])
                                _blend_fb = pyramid_blend_on_fill(
                                    q0=_q0fb, a0=_a0fb, qa_f=_qa_fb, Pa_f=_Pa_fb,
                                    stop_px=_prev_stop_fb,
                                    original_quantity=_float_or_none(pos.get("original_quantity")),
                                )
                                _q1fb = _blend_fb["q1"]
                                _a1fb = _blend_fb["a1"]
                                _s1fb = _blend_fb["s1"]  # INVARIANT-A: tighten-only (asserted)
                                pos["avg_entry_price"] = _a1fb
                                pos["quantity"] = _q1fb
                                pos["original_quantity"] = _blend_fb["original_quantity"]
                                pos["notional_usd"] = _q1fb * _a1fb
                                pos["stop_price"] = _s1fb
                                stop_px = _s1fb
                                # Re-base the max-loss circuit to the STARTER R0 (GUARD #1),
                                # VERBATIM with the pyramid add — adds NEVER inflate the
                                # per-trade loss budget.
                                if _R0fb is not None and _R0fb > 0:
                                    le["pyramid_risk_anchor_usd"] = _R0fb
                                le["flag_breakout_add_count"] = int(le.get("flag_breakout_add_count") or 0) + 1
                                _add_fee_fb = _order_total_fees_usd(_fbno) or 0.0
                                if _add_fee_fb > 0.0:
                                    le["entry_fee_usd_unbooked"] = (
                                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _add_fee_fb
                                    )
                                # RATCHET the higher-base reference to THIS flag's high so the
                                # NEXT flag-add must build a HIGHER flag than this one.
                                _fba_high = _float_or_none(le.get("flag_breakout_add_pending_high"))
                                if _fba_high is not None and _fba_high > 0:
                                    le["flag_breakout_add_last_high"] = _fba_high
                                from datetime import timezone as _tz_fb
                                _cool_s_fb = max(
                                    float(getattr(settings, "chili_momentum_flag_breakout_add_cooldown_seconds", 30.0) or 30.0),
                                    2.0 * float(getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15),
                                )
                                le["flag_breakout_add_cooldown_until_utc"] = (
                                    datetime.now(_tz_fb.utc) + timedelta(seconds=_cool_s_fb)
                                ).replace(tzinfo=None).isoformat()
                                le.pop("flag_breakout_add_order_id", None)
                                le.pop("flag_breakout_add_limit_px", None)
                                le.pop("flag_breakout_add_pending_R0", None)
                                le.pop("flag_breakout_add_pending_high", None)
                                le["position"] = pos
                                _commit_le(sess, le)
                                _emit(db, sess, "live_flag_breakout_add_fill", {
                                    "add_qty": _qa_fb, "add_price": _Pa_fb,
                                    "q0": _q0fb, "a0": _a0fb, "q1": _q1fb, "a1": _a1fb,
                                    "old_stop": float(le.get("flag_breakout_add_prev_stop") or _s1fb),
                                    "new_stop": _s1fb, "R0": _R0fb,
                                    "add_count": le["flag_breakout_add_count"],
                                    "higher_base": le.get("flag_breakout_add_last_high"),
                                    "strength": le.get("flag_breakout_add_confirm_strength"),
                                    "ofi": le.get("flag_breakout_add_confirm_ofi"),
                                })
                                le.pop("flag_breakout_add_prev_stop", None)
                                le.pop("flag_breakout_add_confirm_strength", None)
                                le.pop("flag_breakout_add_confirm_ofi", None)
                            else:
                                # Terminal with NO fill — clear the in-flight marker (a future
                                # tick may try again). No pos mutation.
                                le.pop("flag_breakout_add_order_id", None)
                                le.pop("flag_breakout_add_limit_px", None)
                                le.pop("flag_breakout_add_pending_R0", None)
                                le.pop("flag_breakout_add_pending_high", None)
                                le.pop("flag_breakout_add_prev_stop", None)
                                le.pop("flag_breakout_add_confirm_strength", None)
                                le.pop("flag_breakout_add_confirm_ofi", None)
                                _commit_le(sess, le)

                    # PHASE 2 — TRIGGER a new flag-breakout add (only if none in flight + under
                    # cap + cooldown elapsed + NO other add in flight). EQUITY-FIRST.
                    _is_equity_fba = not str(sess.symbol or "").upper().endswith("-USD")
                    _max_fba = int(getattr(settings, "chili_momentum_flag_breakout_add_max", 2) or 2)
                    _other_add_in_flight_fb = bool(
                        le.get("pyramid_order_id")
                        or le.get("micropullback_reentry_order_id")
                        or le.get("pullback_add_order_id")
                    )
                    if (
                        st == STATE_LIVE_TRAILING
                        and not le.get("flag_breakout_add_order_id")
                    ):
                        # STARTER structural risk R0 = d0 * q0 (the frozen entry stop_distance *
                        # the starter qty), funded off the full starter so a post-partial runner
                        # still sizes the add off the original R.
                        _es_fb = le.get("entry_sizing") if isinstance(le.get("entry_sizing"), dict) else {}
                        _d0_fb = _float_or_none(_es_fb.get("stop_distance"))
                        if _d0_fb is None or _d0_fb <= 0:
                            _d0_fb = max(0.0, float(avg) - float(pos["stop_price"])) or None
                        _q0_fb = (
                            _float_or_none(pos.get("original_quantity"))
                            or _float_or_none(pos.get("quantity"))
                            or 0.0
                        )
                        _a0_fb = float(pos["avg_entry_price"])
                        # COOLDOWN.
                        _cool_active_fb = False
                        _cool_raw_fb = le.get("flag_breakout_add_cooldown_until_utc")
                        if _cool_raw_fb:
                            try:
                                _cool_active_fb = datetime.utcnow() < datetime.fromisoformat(str(_cool_raw_fb))
                            except (TypeError, ValueError):
                                _cool_active_fb = False
                        # Anti-Ross midday lull (parity with the pyramid / dip-add).
                        _lull_fb = False
                        try:
                            from .market_profile import in_midday_lull as _fba_lull
                            _lull_fb = bool(_fba_lull(sess.symbol))
                        except Exception:
                            _lull_fb = False
                        # FLAG DETECTION on the held position's recent bars — reuse the SAME
                        # bull_flag_confirmation the fresh-ENTRY lane uses. ok=True ⇒ a valid,
                        # confirmed-broken bull flag; debug["pullback_high"] = the flag swing-high
                        # (break level), debug["pullback_low"] = the flag low (structural stop),
                        # debug["above_vwap"] = the front-side VWAP read. Fetch the SAME canonical
                        # 5d frame on the bull-flag entry interval (the detector anchors on it).
                        _flag_ok = False
                        _flag_high = None
                        _flag_low = None
                        _flag_above_vwap = False
                        try:
                            from .entry_gates import bull_flag_confirmation as _fba_flag_fn
                            _fba_iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                            _fba_raw = _replay_aware_fetch_ohlcv_df(sess.symbol, interval=_fba_iv, period="5d")
                            _fba_df = _today_session_frame(_fba_raw) if (
                                _fba_raw is not None and not getattr(_fba_raw, "empty", True)
                            ) else None
                            if _fba_df is not None and not getattr(_fba_df, "empty", True):
                                _flag_ok, _flag_reason, _flag_dbg = _fba_flag_fn(
                                    _fba_df,
                                    entry_interval=_fba_iv,
                                    symbol=sess.symbol,
                                    live_price=float(ask) if (ask and float(ask) > 0) else float(bid),
                                    db=db,
                                )
                                _flag_high = _float_or_none(_flag_dbg.get("pullback_high"))
                                _flag_low = _float_or_none(_flag_dbg.get("pullback_low"))
                                _flag_above_vwap = bool(_flag_dbg.get("above_vwap"))
                        except Exception:
                            _flag_ok = False
                            _flag_high = _flag_low = None
                            _flag_above_vwap = False
                        # The PRIOR flag high = the ratcheting base (the new flag must build a
                        # HIGHER high than): the starter entry / breakout / last flag-add's high
                        # — whichever is highest — so each flag-add steps the structure UP.
                        _prior_high_fb = _a0_fb
                        _bk_fb = _float_or_none(le.get("breakout_level_price"))
                        if _bk_fb is not None and _bk_fb > _prior_high_fb:
                            _prior_high_fb = _bk_fb
                        _last_high_fb = _float_or_none(le.get("flag_breakout_add_last_high"))
                        if _last_high_fb is not None and _last_high_fb > _prior_high_fb:
                            _prior_high_fb = _last_high_fb
                        # ⭐ FALLING-KNIFE GUARD — front-side strength (the JUST-shipped score),
                        # OFI level+slope, above-VWAP-or-reclaiming, on the SAME canonical today-
                        # session frame + live OFI/tape reads the ENTRY-side size-tilt uses (one
                        # source of truth). FAIL-CLOSED: any missing leg ⇒ strength/OFI stays None
                        # ⇒ flag_breakout_add_decision refuses the add.
                        _fs_score_fb = None
                        _fs_ofi_lvl_fb = None
                        _fs_ofi_slp_fb = None
                        _above_vwap_fb = bool(_flag_above_vwap)
                        try:
                            from .ross_momentum import (
                                front_side_state as _fba_state_fn,
                                front_side_strength_score as _fba_strength_fn,
                            )
                            from .pipeline import (
                                _live_flow_slope as _fba_flow_fn,
                                _live_trade_flow as _fba_tape_fn,
                            )
                            _fba_flow = _fba_flow_fn(sess.symbol, db=db)
                            if isinstance(_fba_flow, dict):
                                _fs_ofi_lvl_fb = _float_or_none(_fba_flow.get("ofi_level"))
                                _fs_ofi_slp_fb = _float_or_none(_fba_flow.get("ofi_slope"))
                            try:
                                _fba_tape = _fba_tape_fn(sess.symbol, db=db)
                                _fba_tape = None if _fba_tape is None else float(_fba_tape)
                            except Exception:
                                _fba_tape = None
                            _fba_sess_df = _fba_df
                            _fs_closes_fb = None
                            _fs_vwap_dist_fb = None
                            _fs_range_pos_fb = None
                            if _fba_sess_df is not None and not getattr(_fba_sess_df, "empty", True):
                                _fba_state = _fba_state_fn(_fba_sess_df)
                                _fs_vwap_dist_fb = _float_or_none(getattr(_fba_state, "vwap_dist_sigma", None))
                                _fs_range_pos_fb = _float_or_none(getattr(_fba_state, "day_range_pos", None))
                                # above-VWAP OR cleanly RECLAIMING (the flag detector's read OR a
                                # non-negative vwap_dist). below-VWAP-falling ⇒ knife ⇒ refuse.
                                _above_vwap_fb = (
                                    bool(_flag_above_vwap)
                                    or bool(getattr(_fba_state, "above_vwap", False))
                                    or (_fs_vwap_dist_fb is not None and _fs_vwap_dist_fb >= 0.0)
                                )
                                try:
                                    _fs_closes_fb = [
                                        float(c)
                                        for c in _fba_sess_df["Close"].astype(float).tolist()[-12:]
                                    ]
                                except Exception:
                                    _fs_closes_fb = None
                            _fs_score_fb = _fba_strength_fn(
                                closes=_fs_closes_fb,
                                vwap_dist_sigma=_fs_vwap_dist_fb,
                                day_range_pos=_fs_range_pos_fb,
                                ofi_level=_fs_ofi_lvl_fb,
                                ofi_slope=_fs_ofi_slp_fb,
                                signed_tape=_fba_tape,
                            )
                        except Exception:
                            _fs_score_fb = None
                            _fs_ofi_lvl_fb = _fs_ofi_slp_fb = None
                            # keep the flag detector's own VWAP read as the fallback
                        # The STRENGTH FLOOR — the documented base, raised to the regime-adaptive
                        # p25 (the entry-side s_lo) when that distribution is WARM and higher.
                        _strength_floor_fb = float(
                            getattr(settings, "chili_momentum_flag_breakout_add_strength_floor", 0.50) or 0.50
                        )
                        try:
                            _fs_adapt_fb = _frontside_adaptive_thresholds()
                            if _fs_adapt_fb is not None:
                                _adapt_lo_fb = float(_fs_adapt_fb[0])
                                if math.isfinite(_adapt_lo_fb) and _adapt_lo_fb > _strength_floor_fb:
                                    _strength_floor_fb = _adapt_lo_fb
                        except Exception:
                            pass
                        # SHARED pure predicate — the falling-knife-guarded ADD-ON-FLAG-BREAKOUT.
                        _decn_fb = flag_breakout_add_decision(
                            enabled=True,  # outer block already gated on the flag
                            is_equity=_is_equity_fba,
                            add_count=int(le.get("flag_breakout_add_count") or 0),
                            max_adds=_max_fba,
                            in_flight=bool(le.get("flag_breakout_add_order_id")),
                            other_add_in_flight=_other_add_in_flight_fb,
                            a0=_a0_fb,
                            q0=_q0_fb,
                            d0=_d0_fb,
                            bid=float(bid),
                            stop_px=float(stop_px),
                            flag_confirmed=bool(_flag_ok),
                            flag_high=_flag_high,
                            flag_low=_flag_low,
                            prior_flag_high=_prior_high_fb,
                            breakout_margin_frac=float(
                                getattr(settings, "chili_momentum_flag_breakout_add_margin_frac", 0.10) or 0.10
                            ),
                            front_side_strength=_fs_score_fb,
                            strength_floor=_strength_floor_fb,
                            above_vwap_or_reclaiming=_above_vwap_fb,
                            ofi_level=_fs_ofi_lvl_fb,
                            ofi_slope=_fs_ofi_slp_fb,
                            midday_lull=_lull_fb,
                            cooldown_active=_cool_active_fb,
                        )
                        _R0_fb = _decn_fb.get("R0")
                        if not _decn_fb.get("fire"):
                            # Emit only on the INFORMATIVE near-misses (a confirmed flag that a
                            # guard then refused), not the silent common "no flag geometry" case.
                            if _flag_ok and _decn_fb.get("reason") not in (
                                "no_flag_break", "no_flag_structure", "bad_flag_levels",
                            ):
                                _emit(db, sess, "live_flag_breakout_add_vetoed", {
                                    "reason": _decn_fb.get("reason"),
                                    "strength": (None if _fs_score_fb is None else round(float(_fs_score_fb), 4)),
                                    "strength_floor": round(float(_strength_floor_fb), 4),
                                    "ofi_level": _fs_ofi_lvl_fb,
                                    "ofi_slope": _fs_ofi_slp_fb,
                                    "above_vwap": bool(_above_vwap_fb),
                                    "flag_high": _flag_high,
                                    "flag_low": _flag_low,
                                    "prior_high": _prior_high_fb,
                                    "breakout_frac": _decn_fb.get("breakout_frac"),
                                })
                        elif not (_R0_fb and _R0_fb > 0):
                            _emit(db, sess, "live_flag_breakout_add_vetoed", {"reason": "bad_R0"})
                        else:
                            # GUARD #4 ADMISSION — the add is a post-entry BUY; refuse it whenever
                            # a NEW entry would be refused (kill-switch, per-broker + global daily-
                            # loss, drawdown, position cap, aggregate crypto risk). ABORT THE ADD
                            # on refusal — NEVER the exit.
                            _adm_ok_fb, _adm_ev_fb = runner_boundary_risk_ok(
                                db, sess, expected_move_bps=_expected_move_bps
                            )
                            if not _adm_ok_fb:
                                _emit(db, sess, "live_flag_breakout_add_vetoed", {
                                    "reason": "risk_admission",
                                    "severity": _adm_ev_fb.get("severity"),
                                    "errors": _adm_ev_fb.get("errors"),
                                })
                            else:
                                # SIZE via the SAME machinery as the pyramid / dip-add: the add's
                                # risk budget = rho * R0 (conservative — a continuation add is
                                # sized small), at the guarded ask, liquidity-capped on $-vol,
                                # equity-relative notional ceiling. The add's stop sits just below
                                # the flag low (_decn_fb["add_stop"]); the risk-first sizer keys
                                # off the frozen entry ATR so the combined position's worst-case
                                # stays bounded by R0 (the #769 circuit re-bases to R0 on the blend).
                                _rho_fb = float(
                                    getattr(settings, "chili_momentum_flag_breakout_add_risk_fraction", 0.5) or 0.5
                                )
                                _budget_fb = _rho_fb * _R0_fb
                                _fba_ask = float(ask) if (ask and float(ask) > 0) else float(bid)
                                _fba_guard_ask = _fba_ask * _adaptive_notional_guard_multiplier(
                                    expected_move_bps=_expected_move_bps
                                )
                                _inc_fb = prod.base_increment if prod else 1.0
                                _mn_fb = prod.base_min_size if prod else 1.0
                                _atr_fb = _float_or_none(le.get("entry_stop_atr_pct")) or 0.0
                                _ceil_fb = equity_relative_notional_cap(
                                    policy_float_cap(
                                        caps, "max_notional_per_trade_usd",
                                        settings.chili_momentum_risk_max_notional_per_trade_usd,
                                    ),
                                    normalize_execution_family(sess.execution_family),
                                )
                                try:
                                    from .universe import snapshot_dollar_volumes as _fba_dvol_fn
                                    _fba_dvol = (_fba_dvol_fn([sess.symbol]) or {}).get(
                                        str(sess.symbol or "").strip().upper()
                                    )
                                except Exception:
                                    _fba_dvol = None
                                _ceil_fb = liquidity_capped_notional(_ceil_fb, _fba_dvol)
                                # The add can never be LARGER than the starter (a continuation add
                                # is sized conservatively): cap the notional ceiling at the
                                # starter's notional so qty_add <= q0 even if the budget allowed
                                # more (rho<=1 keeps R bounded; this bounds the SHARE count too).
                                _starter_notional_fb = _q0_fb * _a0_fb
                                if _starter_notional_fb > 0:
                                    _ceil_fb = min(_ceil_fb, _starter_notional_fb)
                                _qa_fb2, _qa_meta_fb = compute_risk_first_quantity(
                                    entry_price=_fba_guard_ask,
                                    atr_pct=_atr_fb,
                                    max_loss_usd=_budget_fb,
                                    max_notional_ceiling_usd=_ceil_fb,
                                    base_increment=_inc_fb,
                                    base_min_size=_mn_fb,
                                    stop_atr_mult=float(params.get("stop_atr_mult") or 0.60),
                                )
                                if not _qa_fb2 or _qa_fb2 <= 0:
                                    _emit(db, sess, "live_flag_breakout_add_vetoed", {
                                        "reason": "size_zero",
                                        "detail": _qa_meta_fb.get("reason"),
                                        "budget_usd": round(_budget_fb, 2),
                                    })
                                else:
                                    _fba_limit_str = _fmt_limit_price_buy(_fba_guard_ask)
                                    _fba_cid = (
                                        f"chili_ml_fba_{sess.id}_{uuid.uuid4().hex[:12]}"
                                    )
                                    try:
                                        from .market_profile import market_session_now as _fba_sess_now
                                        _fba_ext = _fba_sess_now(sess.symbol) != "regular"
                                    except Exception:
                                        _fba_ext = False
                                    _fba_res = adapter.place_limit_order_gtc(
                                        product_id=product_id,
                                        side="buy",
                                        base_size=_fmt_base_size(_qa_fb2),
                                        limit_price=_fba_limit_str,
                                        client_order_id=_fba_cid,
                                        extended_hours=_fba_ext,
                                        time_in_force="gfd",
                                    ) or {}
                                    if _fba_res.get("ok") and _fba_res.get("order_id"):
                                        le["flag_breakout_add_order_id"] = str(_fba_res["order_id"])
                                        le["flag_breakout_add_limit_px"] = float(_fba_guard_ask)
                                        le["flag_breakout_add_pending_R0"] = float(_R0_fb)
                                        le["flag_breakout_add_prev_stop"] = float(stop_px)
                                        le["flag_breakout_add_pending_high"] = _float_or_none(_flag_high)
                                        le["flag_breakout_add_confirm_strength"] = (
                                            None if _fs_score_fb is None else round(float(_fs_score_fb), 4)
                                        )
                                        le["flag_breakout_add_confirm_ofi"] = _fs_ofi_lvl_fb
                                        _commit_le(sess, le)
                                        _emit(db, sess, "live_flag_breakout_add_fired", {
                                            "order_id": le["flag_breakout_add_order_id"],
                                            "client_order_id": _fba_cid,
                                            "add_qty": float(_qa_fb2),
                                            "limit_price": _fba_limit_str,
                                            "R0": float(_R0_fb), "rho": _rho_fb,
                                            "budget_usd": round(_budget_fb, 2),
                                            "add_count": int(le.get("flag_breakout_add_count") or 0),
                                            "flag_high": _flag_high,
                                            "flag_low": _flag_low,
                                            "prior_high": _prior_high_fb,
                                            "breakout_frac": (
                                                round(float(_decn_fb["breakout_frac"]), 4)
                                                if _decn_fb.get("breakout_frac") is not None else None
                                            ),
                                            "strength": (None if _fs_score_fb is None else round(float(_fs_score_fb), 4)),
                                            "strength_floor": round(float(_strength_floor_fb), 4),
                                            "ofi_level": _fs_ofi_lvl_fb,
                                            "ofi_slope": _fs_ofi_slp_fb,
                                            "above_vwap": bool(_above_vwap_fb),
                                            "stop_at_submit": float(stop_px),
                                        })
                                    else:
                                        _emit(db, sess, "live_flag_breakout_add_vetoed", {
                                            "reason": "submit_failed",
                                            "error": _fba_res.get("error"),
                                        })
                except Exception:
                    # Fail-safe: any flag-breakout-add error is swallowed so the exit path below
                    # ALWAYS runs. The flag-add NEVER blocks/delays/loosens an exit.
                    _log.debug("[momentum_live] flag-breakout-add block error", exc_info=True)

        if bid > stop_px and le.pop("stop_breach_pending_utc", None) is not None:
            # breach -> recovery between reads = flicker dodged; clear the marker
            # AND the L2 chop-hold counter (the shake-out recovered — exactly the
            # OPG-USD case the anti-shake-out hold is meant to ride out).
            _holds_dodged = le.pop("stop_breach_chop_holds", None)
            _commit_le(sess, le)
            _emit(db, sess, "stop_breach_flicker_dodged", {
                "bid": bid, "stop_price": stop_px, "chop_holds": _holds_dodged})
        if bid <= stop_px:
            # SHAKE-OUT flicker guard (tick-speed exits): one bad bid print can show
            # a breach for a single cached quote; a REAL breakdown persists. Confirm
            # on a SECOND read >=1s apart before selling — the event loop redispatches
            # within ~2s while the breach holds, so a true stop pays at most ~2s of
            # delay; a transient flicker clears the marker on the recovery read.
            _pend_raw = le.get("stop_breach_pending_utc")
            if not _pend_raw:
                le["stop_breach_pending_utc"] = _utcnow().isoformat()
                _commit_le(sess, le)
                _emit(db, sess, "stop_breach_pending_confirm", {"bid": bid, "stop_price": stop_px})
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "stop_pending_confirm": True}
            try:
                _pend_t = datetime.fromisoformat(str(_pend_raw).replace("Z", "+00:00")).replace(tzinfo=None)
                if (_utcnow() - _pend_t).total_seconds() < 1.0:
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state, "stop_pending_confirm": True}
            except (TypeError, ValueError):
                _pend_t = None  # unparseable marker — treat as confirmed (protective default)

            # ── L2-aware anti-shake-out (LOSS side) ──────────────────────────
            # The >=1s flicker guard above has confirmed the breach PERSISTS. Before
            # paying the stop, read L2/OFI to separate a real BREAKDOWN (sell now)
            # from a CHOP dip with bids absorbing (the OPG-USD shake-out: stopped at
            # a dip valley, then recovered). BREAKDOWN is vetoed FIRST and stale/
            # missing L2 => BREAKDOWN, so a real breakdown's latency is <= today's;
            # only a CONFIRMED chop earns a hard-bounded hold. INVARIANT A untouched:
            # this delays the SELL execution only — it never moves/loosens the stop.
            # Default OFF = Stage-0 dark logging (classify + emit the A/B
            # counterfactual, always take today's sell path).
            try:
                _l2_thr = float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25)
                _l2_max_age = float(getattr(settings, "chili_momentum_stop_l2_confirm_max_age_s", 2.5) or 2.5)
                _l2_min_snaps = int(getattr(settings, "chili_momentum_stop_l2_confirm_min_snaps", 3) or 3)
                _l2_max_ticks = int(getattr(settings, "chili_momentum_stop_l2_confirm_max_ticks", 2) or 2)
                _l2_enabled = bool(getattr(settings, "chili_momentum_stop_l2_confirm_enabled", False))
                from .paper_execution import classify_stop_breach
                from .pipeline import read_ladder_distribution

                _bl = read_ladder_distribution(sess.symbol, db=db)
                _bc = classify_stop_breach(
                    ladder=_bl, ofi_threshold=_l2_thr,
                    max_age_s=_l2_max_age, min_snaps=_l2_min_snaps,
                )
                _holds = int(le.get("stop_breach_chop_holds") or 0)
                try:
                    _held_s = (_utcnow() - _pend_t).total_seconds() if _pend_t else 0.0
                except Exception:
                    _held_s = 0.0
                _within_bounds = (_holds < _l2_max_ticks) and (_held_s < _l2_max_age)
                _do_hold = bool(_l2_enabled and _bc.get("cls") == "CHOP" and _within_bounds)
                _emit(db, sess, "stop_breach_l2_classify", {
                    "bid": bid, "stop_price": stop_px, "cls": _bc.get("cls"),
                    "reason": _bc.get("reason"), "enabled": _l2_enabled,
                    "held_s": round(_held_s, 2), "holds": _holds,
                    "would_hold": bool(_bc.get("cls") == "CHOP" and _within_bounds),
                    "did_hold": _do_hold, "signals": _bc.get("signals"),
                })
                if _do_hold:
                    le["stop_breach_chop_holds"] = _holds + 1
                    # KEEP stop_breach_pending_utc so the wall-clock cap stays anchored
                    _commit_le(sess, le)
                    _emit(db, sess, "stop_breach_chop_hold", {
                        "bid": bid, "stop_price": stop_px, "hold_n": _holds + 1,
                        "max_ticks": _l2_max_ticks, "held_s": round(_held_s, 2),
                    })
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state, "stop_chop_hold": True}
            except Exception:
                pass  # any L2 failure => fall through to today's sell (protective)

            le.pop("stop_breach_chop_holds", None)
            le.pop("stop_breach_pending_utc", None)
            # A stop hit while TRAILING (or after the first-target partial) IS the
            # runner's trailing stop; before that it's the initial protective stop.
            _stop_reason = "trail_stop" if (st == STATE_LIVE_TRAILING or pos.get("partial_taken")) else "stop"
            cid = f"chili_ml_s_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=qty,
                client_order_id=cid,
                reason=_stop_reason,
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"stop_price": stop_px, "high_water_mark": _float_or_none(pos.get("high_water_mark"))},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason=_stop_reason):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason=_stop_reason, quantity=qty)
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=_stop_reason,
                    )
                db.flush()
                return {
                    "ok": bool(poll.get("pending") or poll.get("partial")),
                    "session_id": sess.id,
                    "state": sess.state,
                    "pending_exit": bool(poll.get("pending")),
                    "partial_exit": bool(poll.get("partial")),
                    "exit_failed": bool(poll.get("failed")),
                }
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=qty,
                entry_price=avg,
                fill_price=float(poll["fill_price"]),
                reason=_stop_reason,
                slip_bps=slip_live,
            )
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # Sell-into-strength: while a resting scale-out limit is working the
        # target, ADOPT its fill instead of firing the reactive market partial.
        # If price blew >2% through the target and the order is somehow still
        # open (stale book state), cancel-adopt and let the reactive path run.
        if (
            st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)
            and not pos.get("partial_taken")
            and le.get("scale_limit_order_id")
        ):
            _sl_oid = str(le["scale_limit_order_id"])
            _no_sl, _ = adapter.get_order(_sl_oid)
            _sl_filled = float(getattr(_no_sl, "filled_size", 0) or 0) if _no_sl is not None else 0.0
            if _no_sl is not None and not _order_open(_no_sl) and _sl_filled > 0:
                _px_f = float(getattr(_no_sl, "average_filled_price", 0) or 0) or float(
                    le.get("scale_limit_px") or target_px
                )
                _already = float(le.get("scale_limit_adopted_qty") or 0.0)
                le.pop("scale_limit_order_id", None)
                _commit_le(sess, le)
                _new_qty = max(0.0, _sl_filled - _already)
                if _new_qty > 0:
                    # E(1): a resting-limit fill advances the grid rung when a ladder is
                    # in force (the reactive market path takes the later rungs); else it
                    # finishes the single scale-out to the runner (byte-identical).
                    _step = _scale_out_grid_step if _scale_grid_active(pos, sess.symbol) else _scale_out_to_runner
                    _step(
                        db, sess, le=le, filled_quantity=_new_qty,
                        entry_price=avg, fill_price=_px_f, reason="scale_out_limit",
                    )
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            if bid is not None and bid >= target_px * 1.02 and _no_sl is not None and _order_open(_no_sl):
                _cancel_scale_limit_and_clamp(
                    db, sess, adapter, le=le, requested_qty=0.0, reason="stale_scale_limit"
                )
                # cleared — the reactive path below takes over this pulse

        # First-target (2:1) reached and not yet scaled — take the Ross partial.
        # Fires from ENTERED or from TRAILING (price drifted up past trail-activate
        # before reaching the target); the partial_taken guard ensures it fires once.
        # Skipped while a resting scale-out limit is working the level (above).
        #
        # OR-in the adaptive order-flow EXHAUSTION partial: a crypto runner whose
        # flow exhausted BELOW the fixed target arms `exhaustion_lock_partial_armed`
        # (primary hook). It routes through the SAME audited SCALING_OUT path (which
        # flips _be_floor to breakeven — the MEGA give-back fix). Gated directly on
        # `-USD` (NOT transitively via the flag) so equity is byte-identical, and on
        # the partial flag + the same `not scale_limit_order_id` contract so it never
        # races a resting limit. (docs/DESIGN/ADAPTIVE_OFI_EXIT.md)
        _ofi_partial_armed = bool(
            sess.symbol.endswith("-USD")
            and getattr(settings, "chili_momentum_exit_ofi_lock_partial_enabled", False)
            and le.get("exhaustion_lock_partial_armed")
        )
        if (
            st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)
            and not pos.get("partial_taken")
            and not le.get("scale_limit_order_id")
            and (bid >= target_px * 0.995 or _ofi_partial_armed)
        ):
            _exit_kind = "target" if bid >= target_px * 0.995 else "ofi_exhaustion"
            le.pop("exhaustion_lock_partial_armed", None)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_SCALING_OUT)
            _emit(db, sess, "live_partial_exit", {
                "bid": bid, "target_price": target_px, "trigger": _exit_kind,
            })
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_SCALING_OUT:
            # Ross asymmetric exit: sell `scale_out_fraction` of the ORIGINAL size
            # into the first (2:1) target, then move the balance stop to breakeven
            # and HOLD the runner (-> TRAILING). A position too small to leave a
            # sellable runner is flattened whole at target (the old flat exit) so we
            # never strand un-sellable dust. (docs/DESIGN/MOMENTUM_LANE.md)
            _eq_shares = not str(sess.symbol or "").upper().endswith("-USD")
            inc = prod.base_increment if prod else (1.0 if _eq_shares else None)
            mn = prod.base_min_size if prod else (1.0 if _eq_shares else None)
            orig_qty = _float_or_none(pos.get("original_quantity")) or qty
            # E(1) MULTI-LEVEL GRID: when active, this rung's fraction comes from the
            # frozen ladder (NOT the single scale_out_fraction); on fill the grid step
            # advances to the next rung instead of finishing. The grid is anchored on the
            # ORIGINAL risk — freeze the entry stop the FIRST time we scale so a later
            # breakeven ratchet can't re-scale the rung prices. Flag OFF => grid empty =>
            # this whole branch is byte-identical (frac = scale_out_fraction, single scale).
            _grid_active = False
            try:
                if pos.get("scale_grid_anchor_stop") is None and not pos.get("partial_taken"):
                    pos["scale_grid_anchor_stop"] = float(pos.get("stop_price") or 0.0)
                _grid_active = _scale_grid_active(pos, sess.symbol)
            except Exception:
                _grid_active = False
            if _grid_active:
                _grid = _resolve_scale_grid(pos, sess.symbol)
                _gidx = int(pos.get("scale_grid_idx") or 0)
                frac = float(_grid[_gidx][1]) if _gidx < len(_grid) else scale_out_fraction(symbol=sess.symbol, vol_pctl=_adaptive_scale_vol_pctl(le))
            else:
                frac = scale_out_fraction(symbol=sess.symbol, vol_pctl=_adaptive_scale_vol_pctl(le))
            scale_qty, runner_qty, can_split = scale_out_quantity(
                current_qty=qty,
                original_qty=orig_qty,
                fraction=frac,
                base_increment=inc,
                base_min_size=mn,
            )
            scaling = can_split and not pos.get("partial_taken")
            exit_qty = scale_qty if scaling else qty
            exit_reason = "scale_out_target" if scaling else "target"
            cid = f"chili_ml_{'so' if scaling else 'p'}_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=exit_qty,
                client_order_id=cid,
                reason=exit_reason,
                bid=bid,
                ask=ask,
                mid=mid,
                extra={
                    "target_price": target_px,
                    "scale_out_fraction": frac if scaling else None,
                    "runner_qty": runner_qty if scaling else 0.0,
                },
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason=exit_reason):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            if scaling:
                # Mark the pending exit as a deliberate scale-out so a later-tick
                # confirmation banks the partial + holds the runner (NOT a flatten).
                le["pending_exit_is_scale_out"] = True
                _commit_le(sess, le)
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason=exit_reason, quantity=exit_qty)
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            if poll.get("filled"):
                if scaling:
                    # E(1): a grid rung advances to the next target; the single scale
                    # finishes to the runner. Both route through the SAME chokepoint.
                    (_scale_out_grid_step if _grid_active else _scale_out_to_runner)(
                        db,
                        sess,
                        le=le,
                        filled_quantity=exit_qty,
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=exit_reason,
                    )
                else:
                    _complete_confirmed_live_exit(
                        db,
                        sess,
                        le=le,
                        quantity=qty,
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason="target",
                        slip_bps=slip_live,
                    )
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            if poll.get("partial"):
                if scaling:
                    # Any portion of the scale order filling establishes the runner
                    # + breakeven; never over-sell. Remaining intent is abandoned. A
                    # partial grid-rung fill still advances the rung (the remaining
                    # tranche intent is dropped — never re-sold).
                    (_scale_out_grid_step if _grid_active else _scale_out_to_runner)(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=exit_reason,
                    )
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state}
                _apply_confirmed_live_partial_exit(
                    db,
                    sess,
                    le=le,
                    filled_quantity=float(poll["filled_size"]),
                    entry_price=avg,
                    fill_price=float(poll["fill_price"]),
                    reason="target",
                )
            db.flush()
            return {
                "ok": bool(poll.get("pending") or poll.get("partial")),
                "session_id": sess.id,
                "state": sess.state,
                "pending_exit": bool(poll.get("pending")),
                "partial_exit": bool(poll.get("partial")),
                "exit_failed": bool(poll.get("failed")),
            }

        if st == STATE_LIVE_ENTERED and bid >= avg * trail_activate_return:
            _safe_transition(db, sess, STATE_LIVE_TRAILING)
            _emit(db, sess, "live_trailing_armed", {"bid": bid})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # TRAILING runs the chandelier ratchet above; the shared stop check enforces
        # the trailed stop. No dedicated static-floor trail exit remains.
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_EXITED:
        cd_sec = policy_int_cap(
            caps,
            "cooldown_after_stopout_seconds",
            settings.chili_momentum_risk_cooldown_after_stopout_seconds,
        )
        # ADAPTIVE RE-ENTRY COOLDOWN (kill the magic fixed 300s): scale the base by
        # the exit reason (a clean PROFIT/target exit => a SHORT re-scalp window so a
        # runner can be re-entered on the next micro-pullback — the TNMG case; a
        # STOP-OUT => the full base, sit out the chop) AND by the name's realized vol
        # (entry_stop_atr_pct, persisted across recycle). OFF => byte-identical (uses
        # cd_sec verbatim). The loss-side reason_mult is pinned 1.0 so an adaptive
        # cooldown is NEVER shorter than the base on a loss.
        _cd_dbg = None
        if bool(getattr(settings, "chili_momentum_adaptive_reentry_cooldown_enabled", True)):
            cd_sec, _cd_dbg = adaptive_reentry_cooldown_seconds(
                base_seconds=int(cd_sec),
                last_exit_reason=le.get("last_exit_reason"),
                last_exit_return_bps=_float_or_none(le.get("last_exit_return_bps")),
                entry_stop_atr_pct=_float_or_none(le.get("entry_stop_atr_pct")),
                profit_factor=float(getattr(settings, "chili_momentum_reentry_profit_cooldown_factor", 0.25) or 0.25),
                vol_ref_atr_pct=float(getattr(settings, "chili_momentum_reentry_cooldown_vol_ref_atr_pct", 0.03) or 0.03),
                vol_span=float(getattr(settings, "chili_momentum_reentry_cooldown_vol_span", 1.5) or 1.5),
            )
        # Track WHETHER this exit was a stop-out/loss (for the bounded re-entry cap
        # in COOLDOWN). A profit/target recycle is free; only loss recycles count.
        _rb = _float_or_none(le.get("last_exit_return_bps"))
        _was_loss = bool(_rb is not None and _rb <= 0)
        le["last_recycle_was_stopout"] = _was_loss
        until = _utcnow() + timedelta(seconds=max(0, int(cd_sec)))
        le["cooldown_until_utc"] = until.isoformat()
        _safe_transition(db, sess, STATE_LIVE_COOLDOWN)
        _commit_le(sess, le)
        _emit(db, sess, "live_cooldown_started", {
            "until_utc": le["cooldown_until_utc"],
            "cooldown_seconds": int(cd_sec),
            "adaptive": _cd_dbg,
            "was_stopout": _was_loss,
        })
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_COOLDOWN:
        until_raw = le.get("cooldown_until_utc")
        try:
            until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            until = _utcnow()
        if _utcnow() >= until:
            le.pop("cooldown_until_utc", None)
            le["trade_cycles"] = int(le.get("trade_cycles") or 0) + 1
            # Count LOSS recycles separately (a profit recycle is a free re-scalp).
            if bool(le.pop("last_recycle_was_stopout", False)):
                le["stopout_cycles"] = int(le.get("stopout_cycles") or 0) + 1
            # BOUNDED RE-ENTRY AFTER STOP-OUT: a chopper must not re-arm forever. When
            # the loss-recycle count hits the cap, TERMINALIZE (FINISHED) instead of
            # recycling to WATCHING. Flag OFF => unlimited (byte-identical legacy).
            _re_ok, _re_reason = reentry_after_stop_allowed(
                enabled=bool(getattr(settings, "chili_momentum_reentry_after_stop_bound_enabled", True)),
                stopout_cycles=int(le.get("stopout_cycles") or 0),
                max_stopout_reentries=int(getattr(settings, "chili_momentum_max_stopout_reentries", 3) or 3),
            )
            if not _re_ok:
                _commit_le(sess, le)
                _safe_transition(db, sess, STATE_LIVE_FINISHED)
                _emit(db, sess, "live_reentry_capped", {
                    "reason": _re_reason,
                    "stopout_cycles": int(le.get("stopout_cycles") or 0),
                    "trade_cycles": le["trade_cycles"],
                })
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            # RECYCLE ENTRY-STATE RESET (2026-06-27 duplicate-fill root cause): clear the
            # PRIOR trade's entry-order / position lifecycle state so the recycled watcher
            # starts CLEAN — without this it re-polls / re-adopts its OWN already-filled
            # entry order on the next WATCHING tick -> phantom 2x long + stuck bailout spin
            # (AREC sid 9331). OFF => byte-identical to the legacy recycle (state retained).
            _recycle_reset_keys: list[str] = []
            if bool(getattr(settings, "chili_momentum_recycle_entry_state_reset_enabled", True)):
                _recycle_reset_keys = _reset_entry_state_on_recycle(le)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            _emit(db, sess, "live_recycled", {
                "realized_pnl_usd": le.get("realized_pnl_usd"),
                "trade_cycles": le["trade_cycles"],
                "entry_state_reset_keys": _recycle_reset_keys,
            })
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    db.flush()
    return {"ok": True, "session_id": sess.id, "state": sess.state}
