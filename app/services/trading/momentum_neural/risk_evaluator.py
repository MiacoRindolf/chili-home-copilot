"""Evaluate momentum automation sessions against config policy + governance (Phase 6)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, MomentumSymbolViability, TradingAutomationSession
from ..execution_family_registry import (
    asset_class_of_execution_family,
    is_documented_execution_family,
    is_momentum_automation_implemented,
    normalize_execution_family,
    resolve_execution_family_for_symbol,
)
from ..governance import get_kill_switch_status, is_kill_switch_active
from .market_profile import is_coinbase_spot_symbol
from .live_fsm import (
    LIVE_POSITION_HOLDING_STATES,
    LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY,
    LIVE_WATCHING_PREFILL_STATES,
    STATE_LIVE_PENDING_ENTRY,
)
from .paper_fsm import LIVE_INTENT_STATES, PAPER_CONCURRENT_STATES
from .risk_policy import (
    MomentumAutomationRiskPolicy,
    POLICY_VERSION,
    adaptive_max_spread_bps,
    effective_position_cap,
    equity_relative_daily_loss_cap,
    resolve_effective_risk_policy,
)

# Count toward concurrency limits (pre-runner + paper/live runner actives until terminal).
_CONCURRENT_STATES = (
    frozenset(PAPER_CONCURRENT_STATES) | frozenset(LIVE_INTENT_STATES) | frozenset(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY)
)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _check(
    cid: str,
    ok: bool,
    *,
    severity: str,
    message: str,
    detail: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "severity": severity, "message": message, "detail": detail or {}}


def aggregate_open_risk_usd(db: Session, *, user_id: int) -> tuple[float, list[dict[str, Any]]]:
    """Sum of entry-to-stop $ at-risk across OPEN live equity momentum positions.

    The 2026-06-11 lesson: three 'independent' losses (CPSH/SNDG/INDP) were ONE
    correlated regime trade trebled — per-trade risk caps don't see the pile-up.
    At-risk counts only what can still be LOST below entry (a breakeven/locked
    stop contributes 0), so winners being managed don't block new entries.
    Returns (total_usd, per-position breakdown)."""
    total = 0.0
    rows: list[dict[str, Any]] = []
    try:
        held = (
            db.query(TradingAutomationSession)
            .filter(
                TradingAutomationSession.user_id == int(user_id),
                TradingAutomationSession.mode == "live",
                TradingAutomationSession.state.in_(
                    ("live_entered", "live_scaling_out", "live_trailing", "live_bailout")
                ),
                ~TradingAutomationSession.symbol.like("%-USD"),
                # alpaca paper twin-soak = fake money; never consumes real risk budget
                TradingAutomationSession.execution_family != "alpaca_spot",
            )
            .all()
        )
    except Exception:
        return 0.0, rows
    for sess in held:
        try:
            snap = sess.risk_snapshot_json or {}
            le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
            pos = (le or {}).get("position") if isinstance(le, dict) else None
            if not isinstance(pos, dict):
                continue
            qty = float(pos.get("quantity") or 0.0)
            entry = float(pos.get("avg_entry_price") or 0.0)
            stop = float(pos.get("stop_price") or 0.0)
            if qty <= 0 or entry <= 0 or stop <= 0:
                continue
            at_risk = max(0.0, (entry - stop)) * qty
            if at_risk > 0:
                total += at_risk
                rows.append({"symbol": sess.symbol, "session_id": sess.id,
                             "at_risk_usd": round(at_risk, 2)})
        except (TypeError, ValueError):
            continue
    return total, rows


def count_concurrent_automation_sessions(
    db: Session,
    *,
    user_id: int,
    mode: Optional[str] = None,
    exclude_session_id: Optional[int] = None,
) -> int:
    """Active pre-runner sessions only (cancelled/archived/expired excluded by state set).

    Alpaca twin-soak sessions (execution_family=alpaca_spot, fake money against
    the paper endpoint) are EXCLUDED: every real arm spawns a twin, so counting
    them halves the lane's real capacity (2026-06-12 — 10 "live" slots were
    only ~5 real names on IPO morning). Twins are bounded 1:1 by the real arms.
    """
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == user_id,
        TradingAutomationSession.state.in_(_CONCURRENT_STATES),
        TradingAutomationSession.execution_family != "alpaca_spot",
    )
    if mode in ("paper", "live"):
        q = q.filter(TradingAutomationSession.mode == mode)
    if exclude_session_id is not None:
        q = q.filter(TradingAutomationSession.id != int(exclude_session_id))
    return int(q.count())


def count_open_positions(
    db: Session,
    *,
    user_id: int,
    mode: str = "live",
    crypto_only: Optional[bool] = None,
) -> int:
    """HELD positions only (``LIVE_POSITION_HOLDING_STATES`` = entered / scaling_out
    / trailing / bailout — the states that hold capital + a live stop). The
    decouple_watching position cap charges THESE; pre-fill watchers are $0-risk and
    are governed by the watch-fanout cap instead. Alpaca twins excluded (1:1 bounded
    by real arms; never consume a real position slot). ``crypto_only`` filters to /
    out ``-USD`` for the crypto super-bucket + per-lane checks."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == int(user_id),
        TradingAutomationSession.mode == mode,
        TradingAutomationSession.state.in_(LIVE_POSITION_HOLDING_STATES),
        TradingAutomationSession.execution_family != "alpaca_spot",
    )
    if crypto_only is True:
        q = q.filter(TradingAutomationSession.symbol.like("%-USD"))
    elif crypto_only is False:
        q = q.filter(~TradingAutomationSession.symbol.like("%-USD"))
    return int(q.count())


def count_inflight_entry_orders(
    db: Session,
    *,
    user_id: int,
    crypto_only: Optional[bool] = None,
    exclude_session_id: Optional[int] = None,
) -> int:
    """In-flight LIVE entry orders: submitted to the broker but not yet filled
    (``state == live_pending_entry`` AND ``entry_submitted`` set in the live-exec
    snapshot, no ``position`` yet). These are positions *born-but-not-yet-held* —
    the resting order can fill into a held position at any instant.

    The decouple_watching fill-boundary cap MUST count these alongside held
    positions: a position only flips to a HOLDING state at fill (seconds after
    submit), so a burst of K simultaneous submits would each read the same held
    count and all fill → overshoot. The advisory lock serializes the
    count-and-submit so each submitter sees the prior one's committed
    ``entry_submitted=True`` here, making the cap exact (B1). ``entry_submitted``
    lives in ``risk_snapshot_json`` (not a column) so this is JSON-inspected over
    the small live-pending set. Alpaca twins excluded; ``exclude_session_id`` drops
    the submitter's own row (defensive — it has not set ``entry_submitted`` yet)."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == int(user_id),
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state == STATE_LIVE_PENDING_ENTRY,
        TradingAutomationSession.execution_family != "alpaca_spot",
    )
    if crypto_only is True:
        q = q.filter(TradingAutomationSession.symbol.like("%-USD"))
    elif crypto_only is False:
        q = q.filter(~TradingAutomationSession.symbol.like("%-USD"))
    if exclude_session_id is not None:
        q = q.filter(TradingAutomationSession.id != int(exclude_session_id))
    n = 0
    try:
        rows = q.all()
    except Exception:
        return 0
    for s in rows:
        try:
            snap = s.risk_snapshot_json or {}
            le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
            if isinstance(le, dict) and le.get("entry_submitted") and not le.get("position"):
                n += 1
        except (TypeError, ValueError, AttributeError):
            continue
    return n


def sum_inflight_entry_risk_usd(
    db: Session,
    *,
    user_id: int,
    per_trade_fallback_usd: float,
    crypto_only: Optional[bool] = None,
    exclude_session_id: Optional[int] = None,
) -> float:
    """In-flight (submitted-but-not-yet-held) entry $-at-risk for the dollar budget.

    Mirrors :func:`count_inflight_entry_orders`'s SAME born-but-not-held set
    (``state == live_pending_entry`` AND ``entry_submitted`` set AND no ``position``
    yet) but sums the ACTUAL per-order risk the live runner persists onto each
    session at submit time (``le['entry_inflight_risk_usd']`` = that order's real
    shape-aware ``(entry-stop)*qty``, which already reflects the per-trade
    multiplier). A flat ``count * per_trade_fallback`` under-charges a burst of
    HIGH-multiplier entries; reading the persisted per-order risk makes the
    in-flight charge multiplier-aware.

    CONSERVATIVE FALLBACK: when a sibling has no persisted (positive, finite)
    ``entry_inflight_risk_usd`` (a pre-submit race, or a session written by an
    older image), charge the positive flat ``per_trade_fallback_usd`` estimate
    instead — never $0 (an under-estimate would let a fill-burst slip dollars past
    the ceiling; an over-estimate is the safe side). Same advisory-lock atomicity
    contract as the count: the caller evaluates this INSIDE the per-(user,lane)
    lock so each serialized submitter sees the prior one's committed risk."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == int(user_id),
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state == STATE_LIVE_PENDING_ENTRY,
        TradingAutomationSession.execution_family != "alpaca_spot",
    )
    if crypto_only is True:
        q = q.filter(TradingAutomationSession.symbol.like("%-USD"))
    elif crypto_only is False:
        q = q.filter(~TradingAutomationSession.symbol.like("%-USD"))
    if exclude_session_id is not None:
        q = q.filter(TradingAutomationSession.id != int(exclude_session_id))
    try:
        fallback = float(per_trade_fallback_usd)
    except (TypeError, ValueError):
        fallback = 0.0
    if not (fallback > 0):
        fallback = 0.0
    total = 0.0
    try:
        rows = q.all()
    except Exception:
        return 0.0
    for s in rows:
        try:
            snap = s.risk_snapshot_json or {}
            le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
            if not (isinstance(le, dict) and le.get("entry_submitted") and not le.get("position")):
                continue
            try:
                persisted = float(le.get("entry_inflight_risk_usd") or 0.0)
            except (TypeError, ValueError):
                persisted = 0.0
            # Persisted real risk when present + sane; else the positive flat estimate.
            total += persisted if persisted > 0 else fallback
        except (TypeError, ValueError, AttributeError):
            # An un-inspectable sibling still carries real risk — charge the floor.
            total += fallback
            continue
    return total


def aggregate_open_crypto_risk_usd(db: Session, *, user_id: int) -> tuple[float, list[dict[str, Any]]]:
    """Crypto mirror of :func:`aggregate_open_risk_usd` (which is equity-only —
    it filters OUT ``-USD``). Sum of entry-to-stop $ at-risk across OPEN live
    CRYPTO (-USD) positions, so the crypto lane has a dollar-precise correlated-
    exposure backstop (decouple_watching B2: the count cap alone can't bound
    dollars once crypto gaps through). Breakeven/locked stops contribute 0."""
    total = 0.0
    rows: list[dict[str, Any]] = []
    try:
        held = (
            db.query(TradingAutomationSession)
            .filter(
                TradingAutomationSession.user_id == int(user_id),
                TradingAutomationSession.mode == "live",
                TradingAutomationSession.state.in_(LIVE_POSITION_HOLDING_STATES),
                TradingAutomationSession.symbol.like("%-USD"),
                TradingAutomationSession.execution_family != "alpaca_spot",
            )
            .all()
        )
    except Exception:
        return 0.0, rows
    for sess in held:
        try:
            snap = sess.risk_snapshot_json or {}
            le = snap.get("momentum_live_execution") if isinstance(snap, dict) else None
            pos = (le or {}).get("position") if isinstance(le, dict) else None
            if not isinstance(pos, dict):
                continue
            qty = float(pos.get("quantity") or 0.0)
            entry = float(pos.get("avg_entry_price") or 0.0)
            stop = float(pos.get("stop_price") or 0.0)
            if qty <= 0 or entry <= 0 or stop <= 0:
                continue
            at_risk = max(0.0, (entry - stop)) * qty
            if at_risk > 0:
                total += at_risk
                rows.append({"symbol": sess.symbol, "session_id": sess.id,
                             "at_risk_usd": round(at_risk, 2)})
        except (TypeError, ValueError):
            continue
    return total, rows


def _viability_age_seconds(via: MomentumSymbolViability) -> float:
    ts = via.freshness_ts
    if ts is None:
        return 1e9
    if ts.tzinfo:
        ts = ts.replace(tzinfo=None)
    return max(0.0, (_utcnow() - ts).total_seconds())


def _recent_eligible_age_seconds(recent_live_eligible_at_utc: Optional[str]) -> Optional[float]:
    """Age (seconds) of the arm/confirm-time live-eligibility anchor, or None if it is
    absent / unparseable. Pure + side-effect-free. FAIL-SAFE: any parse error returns
    None so the caller keeps its conservative BLOCK (the recency grace only relaxes on a
    positively-parsed, in-window anchor — never on a missing or garbage timestamp)."""
    raw = (recent_live_eligible_at_utc or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    age = (_utcnow() - ts).total_seconds()
    # A future-dated anchor (clock skew) is treated as age 0 (still "recent"); a sane
    # positive age flows through to the window comparison.
    return max(0.0, age)


def _live_eligible_recency_grace_active(
    *,
    policy: MomentumAutomationRiskPolicy,
    recent_live_eligible_at_utc: Optional[str],
    live_forward_momentum: Optional[bool],
) -> tuple[bool, dict[str, Any]]:
    """Decide whether a live_eligible=False FLICKER at the entry instant qualifies for the
    adaptive recency grace. Returns ``(active, detail)``. ``active`` is True ONLY when ALL of:
      * the grace flag is ON (flag OFF => byte-identical: never active);
      * the session was live-eligible at ARM/CONFIRM within ``live_eligible_recency_grace_seconds``
        (the anchor parses AND its age <= the window — ONE documented base);
      * there is live FORWARD MOMENTUM (``live_forward_momentum`` is True — signed-tape accel>0
        / OFI / price rising, computed by the runner).
    FAIL-SAFE: a missing/unparseable anchor, an out-of-window anchor, or absent/false momentum
    => ``active=False`` (keep today's BLOCK). Pure + side-effect-free."""
    detail: dict[str, Any] = {
        "grace_enabled": bool(policy.live_eligible_recency_grace_enabled),
        "grace_window_s": float(policy.live_eligible_recency_grace_seconds),
        "recent_eligible_age_s": None,
        "recent_eligible_within_window": False,
        "live_forward_momentum": (None if live_forward_momentum is None else bool(live_forward_momentum)),
    }
    if not policy.live_eligible_recency_grace_enabled:
        return False, detail
    age = _recent_eligible_age_seconds(recent_live_eligible_at_utc)
    if age is None:
        return False, detail
    detail["recent_eligible_age_s"] = round(age, 3)
    within = age <= float(policy.live_eligible_recency_grace_seconds)
    detail["recent_eligible_within_window"] = bool(within)
    if not within:
        return False, detail
    if not bool(live_forward_momentum):
        return False, detail
    return True, detail


def _readiness_numbers(exec_json: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not exec_json:
        return out
    for k in (
        "spread_bps",
        "slippage_estimate_bps",
        "fee_to_target_ratio",
        "product_tradable",
        "extra",
    ):
        if k in exec_json:
            out[k] = exec_json.get(k)
    ex = exec_json.get("extra")
    if isinstance(ex, dict):
        for k2 in ("spread_bps", "market_data_retrieved_at_utc", "market_data_max_age_seconds"):
            if k2 in ex and k2 not in out:
                out[k2] = ex[k2]
    return out


def _daily_realized_pnl(db: Session, user_id: int) -> float:
    """Sum realized PnL from all sessions that terminated today for this user.

    Routes through ``authoritative_label_for_outcome``: flag-OFF this is the legacy
    ``realized_pnl_usd`` sum byte-for-byte (accessor returns legacy pnl,
    is_reconciled=True). Flag-ON, the broker-true pnl is summed for reconciled rows
    and unreconciled rows are EXCLUDED (not summed as $0) — ⚠️ this changes the
    daily-loss-cap GATE input, a trading-behavior change to soak deploy-when-flat.
    """
    from datetime import date

    from .outcome_reconcile import authoritative_label_for_outcome

    today_start = datetime.combine(date.today(), datetime.min.time())
    rows = (
        db.query(MomentumAutomationOutcome)
        .filter(
            MomentumAutomationOutcome.user_id == user_id,
            MomentumAutomationOutcome.terminal_at >= today_start,
        )
        .all()
    )
    total = 0.0
    for o in rows:
        pnl, _bps, _win, is_rec = authoritative_label_for_outcome(o)
        if not is_rec:
            continue
        if pnl is not None:
            total += float(pnl)
    return total


def _running_peak_and_total(pnls: Iterable[float]) -> tuple[float, float]:
    """Pure: ``(high-water mark, final total)`` of a running cumulative sum.

    The peak is floored at 0.0 — you start the day flat, so a day that was never
    green has no PEAK PROFIT to give back. Walking close-events in time order, the
    running cumulative sum's max is exactly the peak accumulated realized profit
    Ross's 50%-giveback rule protects. Separated out (no I/O) so the arithmetic is
    unit-testable without a DB.
    """
    peak = 0.0
    running = 0.0
    for p in pnls:
        try:
            running += float(p or 0.0)
        except (TypeError, ValueError):
            continue
        if running > peak:
            peak = running
    return peak, running


def _daily_realized_pnl_peak_and_current(db: Session, user_id: int) -> tuple[float, float]:
    """``(peak high-water mark, current cumulative)`` of today's realized PnL — one query.

    Walks today's terminated-session outcomes in ``terminal_at`` order accumulating
    ``realized_pnl_usd``; ``current`` is the final cumulative sum (identical to
    ``_daily_realized_pnl``) and ``peak`` is its running max floored at 0.0. Same
    ``date.today()`` window as the daily-loss cap, so both reset together at the
    daily boundary (00:00 UTC in production containers).
    """
    from datetime import date

    from .outcome_reconcile import authoritative_label_for_outcome

    today_start = datetime.combine(date.today(), datetime.min.time())
    rows = (
        db.query(MomentumAutomationOutcome)
        .filter(
            MomentumAutomationOutcome.user_id == user_id,
            MomentumAutomationOutcome.terminal_at >= today_start,
        )
        .order_by(
            MomentumAutomationOutcome.terminal_at.asc(),
            MomentumAutomationOutcome.id.asc(),
        )
        .all()
    )

    # Flag-OFF: legacy realized_pnl_usd in terminal_at order, byte-identical.
    # Flag-ON: broker-true pnl for reconciled rows; unreconciled EXCLUDED from the
    # high-water walk (a $0 fill-in would distort the giveback peak).
    def _ordered_pnls():
        for o in rows:
            pnl, _bps, _win, is_rec = authoritative_label_for_outcome(o)
            if not is_rec:
                continue
            yield pnl

    return _running_peak_and_total(_ordered_pnls())


def evaluate_profit_giveback_halt(
    db: Session, *, user_id: int, execution_family: str = "coinbase_spot"
) -> dict[str, Any]:
    """Ross-style profit-giveback session halt for the momentum LIVE lane.

    Ross's rule (warriortrading.com/7-day-trading-rules, confirmed in the 2026-06-07
    research): once he gives back 50% of his PEAK accumulated daily profit he STOPS
    trading for the day ("easier to remember half than 40%"). This mirrors it — the
    UPSIDE counterpart of the daily-loss cap: once today's high-water mark of realized
    PnL has reached an equity-relative ACTIVATION threshold (a meaningful green day)
    AND current realized PnL has fallen to ``peak * (1 - giveback_fraction)`` or below,
    new arming is blocked for the rest of the daily window (lock in the green day).
    Resets with the SAME ``date.today()`` window as the daily-loss cap.

    The giveback FRACTION is the single documented knob
    (``chili_momentum_profit_giveback_fraction``, default 0.5). The activation
    threshold is equity-relative — it reuses the equity-relative daily-loss-cap
    magnitude so there is no second fixed-$ magic number (a green day worth protecting
    is, by symmetry, one that exceeds the day's max tolerable red). 0 disables.
    Read-only; mirror of the daily_loss_cap two-layer pattern.
    docs/DESIGN/MOMENTUM_LANE.md [[project_momentum_lane]] [[feedback_adaptive_no_magic]]
    """
    try:
        frac = float(getattr(settings, "chili_momentum_profit_giveback_fraction", 0.5))
    except (TypeError, ValueError):
        frac = 0.5
    # Clamp to [0, 1]: <=0 disables the rule; >1 is nonsensical (cap at full giveback).
    if frac < 0.0 or not (frac == frac):  # NaN-safe
        frac = 0.0
    elif frac > 1.0:
        frac = 1.0
    # Activation threshold is equity-relative (no second fixed-$ knob): reuse the
    # daily-loss-cap magnitude. [[feedback_adaptive_no_magic]]
    activation = equity_relative_daily_loss_cap(
        float(getattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
        execution_family,
    )
    peak, current = _daily_realized_pnl_peak_and_current(db, int(user_id))
    giveback_floor = peak * (1.0 - frac)
    armed = bool(frac > 0.0 and activation > 0.0 and peak >= activation)
    halted = bool(armed and current <= giveback_floor)
    return {
        "halted": halted,
        "armed": armed,
        "peak_pnl_usd": round(float(peak), 2),
        "daily_pnl_usd": round(float(current), 2),
        "activation_threshold_usd": round(float(activation), 2),
        "giveback_fraction": round(float(frac), 4),
        "giveback_floor_usd": round(float(giveback_floor), 2),
    }


# A green day worth protecting from a FULL round-trip into the red: at least half the
# day's max-tolerable RED (the equity-relative daily-loss cap). Deliberately SMALLER than
# the profit-giveback activation (the full cap) so this catches the small green day the
# giveback — whose floor sits ABOVE $0 — cannot. One documented base, equity-relative.
_GREEN_TO_RED_ACTIVATION_FRAC = 0.5


def evaluate_green_to_red_halt(
    db: Session, *, user_id: int, execution_family: str = "coinbase_spot"
) -> dict[str, Any]:
    """Ross green-to-red session breaker (gap #8, videos 37/38): going from green on the
    day back to <= $0 is the emotional-hijack trigger — walk away. The profit-giveback
    halt's floor (``peak * (1 - frac)``) sits ABOVE $0, so a TRUE round-trip into the red
    on a smaller green day is not caught. Once today's realized PnL has PEAKED above a
    small equity-relative activation (half the daily-loss-cap magnitude — no second
    fixed-$ knob) AND current realized PnL is <= 0, new live arming is blocked for the
    rest of the daily window. Read-only; same ``date.today()`` window + two-layer pattern
    as the giveback halt. [[feedback_adaptive_no_magic]]
    """
    activation = _GREEN_TO_RED_ACTIVATION_FRAC * equity_relative_daily_loss_cap(
        float(getattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
        execution_family,
    )
    peak, current = _daily_realized_pnl_peak_and_current(db, int(user_id))
    armed = bool(activation > 0.0 and peak >= activation)
    halted = bool(armed and current <= 0.0)
    return {
        "halted": halted,
        "armed": armed,
        "peak_pnl_usd": round(float(peak), 2),
        "daily_pnl_usd": round(float(current), 2),
        "activation_threshold_usd": round(float(activation), 2),
    }


def evaluate_proposed_momentum_automation(
    db: Session,
    *,
    user_id: int,
    symbol: str,
    variant_id: int,
    mode: str,
    execution_family: str = "coinbase_spot",
    exclude_session_id: Optional[int] = None,
    expected_move_bps: Optional[float] = None,
    recent_live_eligible_at_utc: Optional[str] = None,
    live_forward_momentum: Optional[bool] = None,
) -> dict[str, Any]:
    """
    Server-side risk gate for operator flows (paper draft, live arm, confirm).

    Returns stable dict: allowed, severity, checks, warnings, errors, governance_state, ...
    Archived/expired/cancelled sessions do not count toward concurrency (query filter).

    ``recent_live_eligible_at_utc`` / ``live_forward_momentum`` carry the live-eligibility
    RECENCY-GRACE evidence (2026-06-29 UPC +500% miss). The runner passes the session's
    arm/confirm-time eligibility anchor (an ISO-8601 UTC string proving the name WAS
    live-eligible at arm/confirm) and a positive forward-momentum read (signed-tape accel > 0
    / OFI / price rising). When ``via.live_eligible`` flickers False at the entry instant but
    BOTH (a) the anchor is within the grace window AND (b) forward momentum is present, the
    eligibility block is DOWNGRADED to a warn — a transient re-scoring flicker cannot terminally
    veto a just-confirmed active mover. FAIL-SAFE: a missing/unparseable anchor or absent
    momentum keeps today's BLOCK (the grace only relaxes on positive evidence, never widens
    risk blindly). Both default ``None`` so non-runner callers are byte-identical.
    """
    policy = MomentumAutomationRiskPolicy.from_settings()
    sym = symbol.strip().upper()
    m = mode.lower().strip()
    ef = normalize_execution_family(execution_family)
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    gov = get_kill_switch_status()
    governance_state = {"kill_switch_active": bool(gov.get("active")), "kill_switch_reason": gov.get("reason")}

    # ── Governance / kill switch ──────────────────────────────────────
    # A LEGACY single-global daily-loss breach is handled PER BROKER below
    # (global_daily_loss_cap check) when per-broker is enabled, so it does NOT
    # block here — only true-global halts (manual/emergency/price-monitor/backstop) do.
    _ks_reason = str(gov.get("reason") or "")
    _defer_daily_loss = (
        bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True))
        and _ks_reason.startswith("global_daily_loss_breach")
        and "backstop" not in _ks_reason
    )
    if is_kill_switch_active() and not _defer_daily_loss:
        if m == "live" and policy.disable_live_if_governance_inhibit:
            checks.append(
                _check(
                    "governance_kill_switch",
                    False,
                    severity="block",
                    message="Kill switch active — live automation progression blocked.",
                    detail=governance_state,
                )
            )
        elif m == "paper" and policy.block_paper_when_kill_switch:
            checks.append(
                _check(
                    "governance_kill_switch_paper",
                    False,
                    severity="block",
                    message="Kill switch active — paper automation blocked by policy.",
                    detail=governance_state,
                )
            )
        else:
            checks.append(
                _check(
                    "governance_kill_switch",
                    True,
                    severity="ok",
                    message="Kill switch active but mode not blocked by policy.",
                    detail=governance_state,
                )
            )
    else:
        checks.append(
            _check("governance_kill_switch", True, severity="ok", message="Kill switch inactive.", detail=gov)
        )

    # ── Execution family (strategy logic vs routing seam — Phase 11) ─────
    if not is_documented_execution_family(ef):
        checks.append(
            _check(
                "execution_family",
                False,
                severity="block",
                message=f"Unknown execution_family {ef!r} (not in documented registry).",
                detail={"execution_family": ef},
            )
        )
    elif not is_momentum_automation_implemented(ef):
        checks.append(
            _check(
                "execution_family",
                False,
                severity="block",
                message=f"execution_family {ef!r} is documented but not implemented yet.",
                detail={"execution_family": ef},
            )
        )
    else:
        checks.append(
            _check(
                "execution_family",
                True,
                severity="ok",
                message="execution_family supported for momentum automation.",
                detail={"execution_family": ef},
            )
        )

    # The authoritative venue is symbol-routed (E1 per-symbol routing,
    # ``resolve_execution_family_for_symbol``): crypto BASE-USD -> coinbase_spot,
    # equities -> robinhood_spot (the DEFAULT). Validate the REQUEST against the symbol's
    # ASSET CLASS, not the single default-resolved venue: an EQUITY may legitimately route to
    # ANY equity venue — robinhood_spot OR alpaca_spot (the same-name A/B), or the sanctioned
    # MCP rail — while the dangerous CROSS-CLASS case (an equity requested via the crypto
    # venue coinbase_spot, or a crypto pair via an equity venue) is still BLOCKED. This is the
    # bug a pre-flight caught: the old exact `ef != symbol_ef` blocked alpaca_spot for equities
    # because the default resolves to robinhood_spot. (docs/DESIGN/ALPACA_LANE.md)
    symbol_ef = normalize_execution_family(resolve_execution_family_for_symbol(sym))
    v_row = (
        db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(variant_id)).one_or_none()
    )
    vef = normalize_execution_family(v_row.execution_family) if v_row is not None else None
    _symbol_class = asset_class_of_execution_family(symbol_ef)
    from ..execution_family_registry import execution_family_supports_asset_class

    if not execution_family_supports_asset_class(ef, _symbol_class):
        checks.append(
            _check(
                "execution_family_variant_alignment",
                False,
                severity="block",
                message="Requested execution_family is for a different ASSET CLASS than the symbol's venue.",
                detail={
                    "request": ef,
                    "request_asset_class": asset_class_of_execution_family(ef),
                    "symbol_resolved": symbol_ef,
                    "symbol_asset_class": asset_class_of_execution_family(symbol_ef),
                    "variant_execution_family": vef,
                    "variant_id": int(variant_id),
                },
            )
        )
    else:
        checks.append(
            _check(
                "execution_family_variant_alignment",
                True,
                severity="ok",
                message="execution_family matches symbol-resolved venue.",
                detail={
                    "execution_family": ef,
                    "symbol_resolved": symbol_ef,
                    "variant_execution_family": vef,
                },
            )
        )

    # ── Viability row ───────────────────────────────────────────────────
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == int(variant_id))
        .one_or_none()
    )
    viability_state: dict[str, Any] = {"row_present": via is not None}
    freshness_state: dict[str, Any] = {"viability_age_sec": None, "fresh": False}
    if not via:
        checks.append(
            _check(
                "viability_present",
                False,
                severity="block",
                message="No durability viability row for symbol/variant.",
            )
        )
    else:
        viability_state.update(
            {
                "viability_score": via.viability_score,
                "paper_eligible": via.paper_eligible,
                "live_eligible": via.live_eligible,
                "freshness_ts": via.freshness_ts.isoformat() if via.freshness_ts else None,
            }
        )
        age = _viability_age_seconds(via)
        fresh = not policy.require_fresh_viability or age <= policy.viability_max_age_seconds
        freshness_state = {"viability_age_sec": round(age, 3), "fresh": fresh}
        checks.append(
            _check(
                "viability_present",
                True,
                severity="ok",
                message="Viability row present.",
            )
        )
        if policy.require_fresh_viability and not fresh:
            sev = "block" if m == "live" else "warn"
            checks.append(
                _check(
                    "viability_freshness",
                    False,
                    severity=sev,
                    message=f"Viability snapshot stale (age {age:.0f}s > max {policy.viability_max_age_seconds}s).",
                    detail=freshness_state,
                )
            )
        else:
            checks.append(
                _check(
                    "viability_freshness",
                    True,
                    severity="ok",
                    message="Viability freshness within policy.",
                    detail=freshness_state,
                )
            )

        if m == "paper":
            ok_pe = bool(via.paper_eligible)
            checks.append(
                _check(
                    "paper_eligible",
                    ok_pe,
                    severity="block" if not ok_pe else "ok",
                    message="Paper eligible" if ok_pe else "Not paper-eligible per neural viability.",
                )
            )
        if m == "live":
            if ef == "coinbase_spot" and not is_coinbase_spot_symbol(sym):
                checks.append(
                    _check(
                        "symbol_live_compatibility",
                        False,
                        severity="block",
                        message="Symbol is not a Coinbase spot product id for live execution.",
                        detail={"symbol": sym, "execution_family": ef},
                    )
                )
            ok_le = bool(via.live_eligible)
            if policy.require_live_eligible_for_live:
                if ok_le:
                    checks.append(
                        _check("live_eligible", True, severity="ok", message="Live eligible")
                    )
                else:
                    # TOCTOU recency grace: a fast/thin premarket vertical can FLICKER
                    # live_eligible False at the exact entry instant even though the name
                    # armed+confirmed live-eligible seconds earlier (UPC +500%, 2026-06-29).
                    # If the session was live-eligible at arm/confirm within the grace window
                    # AND live forward momentum is present, DOWNGRADE the terminal block to a
                    # warn so a transient flicker can't veto a just-confirmed active mover.
                    # FAIL-SAFE: no recent-eligible evidence / no momentum => keep the block.
                    grace_active, grace_detail = _live_eligible_recency_grace_active(
                        policy=policy,
                        recent_live_eligible_at_utc=recent_live_eligible_at_utc,
                        live_forward_momentum=live_forward_momentum,
                    )
                    if grace_active:
                        checks.append(
                            _check(
                                "live_eligible",
                                True,
                                severity="warn",
                                message=(
                                    "Live-eligibility FLICKER tolerated by recency grace "
                                    "(recent-eligible at arm/confirm + live forward momentum)."
                                ),
                                detail=grace_detail,
                            )
                        )
                    else:
                        checks.append(
                            _check(
                                "live_eligible",
                                False,
                                severity="block",
                                message="Not live-eligible per neural viability.",
                                detail=grace_detail,
                            )
                        )
            else:
                checks.append(
                    _check(
                        "live_eligible",
                        ok_le,
                        severity="warn" if not ok_le else "ok",
                        message="Live eligibility optional by policy.",
                    )
                )

        # ── Execution readiness (spread / slip / fee) ──────────────────
        ex = via.execution_readiness_json if isinstance(via.execution_readiness_json, dict) else {}
        nums = _readiness_numbers(ex)
        # Live spread cap is volatility-relative (adaptive) when the caller passes
        # the instrument's expected move — the live runner does; other callers fall
        # back to the documented base floor. Paper keeps its fixed cap.
        if m == "live":
            max_spread = adaptive_max_spread_bps(
                policy.max_spread_bps_live, expected_move_bps, policy.spread_to_expected_move_ratio,
                abs_cap_bps=policy.max_spread_bps_abs_cap,
            )
        else:
            max_spread = policy.max_spread_bps_paper
        spread = nums.get("spread_bps")
        if spread is not None:
            try:
                sb = float(spread)
                ok_sp = sb <= max_spread
                checks.append(
                    _check(
                        "spread_bps",
                        ok_sp,
                        severity="block" if not ok_sp and m == "live" else ("warn" if not ok_sp else "ok"),
                        message=f"Spread {sb} bps vs max {max_spread} ({m}).",
                        detail={"spread_bps": sb, "max": max_spread},
                    )
                )
            except (TypeError, ValueError):
                checks.append(
                    _check(
                        "spread_bps",
                        False,
                        severity="warn",
                        message="Spread bps missing or invalid in readiness JSON.",
                    )
                )
        else:
            checks.append(
                _check(
                    "spread_bps",
                    False,
                    severity="warn" if m == "live" else "ok",
                    message="No spread_bps in viability execution readiness (cannot enforce cap).",
                )
            )

        slip = nums.get("slippage_estimate_bps")
        if slip is not None:
            try:
                sl = float(slip)
                ok_sl = sl <= policy.max_estimated_slippage_bps
                checks.append(
                    _check(
                        "slippage_estimate_bps",
                        ok_sl,
                        severity="block" if not ok_sl and m == "live" else ("warn" if not ok_sl else "ok"),
                        message=f"Slippage est {sl} bps vs max {policy.max_estimated_slippage_bps}.",
                    )
                )
            except (TypeError, ValueError):
                pass
        else:
            warnings.append("slippage_estimate_bps not present — cap not enforced.")

        fee = nums.get("fee_to_target_ratio")
        if fee is not None:
            try:
                fr = float(fee)
                ok_f = fr <= policy.max_fee_to_target_ratio
                checks.append(
                    _check(
                        "fee_to_target_ratio",
                        ok_f,
                        severity="block" if not ok_f and m == "live" else ("warn" if not ok_f else "ok"),
                        message=f"Fee/target {fr:.3f} vs max {policy.max_fee_to_target_ratio:.3f}.",
                    )
                )
            except (TypeError, ValueError):
                pass

        pt = nums.get("product_tradable")
        if pt is False and m == "live":
            checks.append(
                _check(
                    "product_tradable",
                    False,
                    severity="block",
                    message="Product marked not tradable in readiness metadata.",
                )
            )
        elif m == "live" and ef == "coinbase_spot" and not is_coinbase_spot_symbol(sym):
            checks.append(
                _check(
                    "product_tradable_symbol",
                    False,
                    severity="block",
                    message="Live readiness requires a Coinbase spot symbol like BTC-USD.",
                    detail={"symbol": sym},
                )
            )

        # Strict Coinbase freshness (optional)
        if policy.require_strict_coinbase_freshness and settings.chili_coinbase_strict_freshness:
            max_age = float(
                min(policy.stale_market_data_max_age_sec, settings.chili_coinbase_market_data_max_age_sec)
            )
            md_age = nums.get("market_data_max_age_seconds")
            if md_age is not None:
                try:
                    mda = float(md_age)
                    ok_md = mda <= max_age
                    checks.append(
                        _check(
                            "market_data_freshness",
                            ok_md,
                            severity="block" if not ok_md and m == "live" else ("warn" if not ok_md else "ok"),
                            message=f"Market data age {mda}s vs max {max_age}s.",
                        )
                    )
                except (TypeError, ValueError):
                    pass
            else:
                checks.append(
                    _check(
                        "market_data_freshness",
                        False,
                        severity="warn",
                        message="Strict freshness requested but market_data_max_age_seconds missing.",
                    )
                )

    # ── Concurrency ─────────────────────────────────────────────────────
    # MODE-SCOPED count (2026-06-12 SpaceX-morning incident): the paper shadow
    # mass (10 overnight crypto paper sessions) filled a mode-blind total cap
    # and starved EVERY live arm through the premarket window. Paper sessions
    # are free simulations — they must never consume the real-money budget.
    # Live proposals are additionally bounded by the adaptive live cap below.
    _decouple = bool(getattr(settings, "chili_momentum_decouple_watching_enabled", False))
    total_ct = count_concurrent_automation_sessions(
        db, user_id=user_id, mode=m, exclude_session_id=exclude_session_id
    )
    _max_total = policy.max_concurrent_sessions
    if _decouple and m == "live":
        # Decoupled: watchers fan out to watch_fanout_max, so the coarse all-states
        # cap must clear (fanout + position cap + slack) or it would silently re-cap
        # the funnel at the legacy 10. It remains a leak-catching backstop (a stuck
        # live_cooldown pile-up still trips it), not the active constraint.
        _fanout = int(getattr(settings, "chili_momentum_watch_fanout_max", 15) or 15)
        _max_total = max(_max_total, _fanout + effective_position_cap(crypto=False) + 5)
    ok_tot = total_ct < _max_total
    checks.append(
        _check(
            "max_concurrent_sessions",
            ok_tot,
            severity="block" if not ok_tot else "ok",
            message=f"Concurrent {m} sessions {total_ct} / max {_max_total}.",
            detail={"count": total_ct, "mode": m},
        )
    )
    if m == "live":
        if _decouple:
            # Charge the risk-budget cap against HELD positions only (watchers are
            # $0-risk). This mirrors the authoritative advisory-locked fill-boundary
            # cap in live_runner; here it is a coarse secondary check at arm time.
            live_ct = count_open_positions(db, user_id=user_id, mode="live")
            _live_cap = effective_position_cap(crypto=False)
        else:
            live_ct = count_concurrent_automation_sessions(
                db, user_id=user_id, mode="live", exclude_session_id=exclude_session_id
            )
            _live_cap = policy.max_concurrent_live_sessions
        ok_lv = live_ct < _live_cap
        checks.append(
            _check(
                "max_concurrent_live_sessions",
                ok_lv,
                severity="block" if not ok_lv else "ok",
                message=f"Concurrent live sessions {live_ct} / max {_live_cap}.",
                detail={"count": live_ct},
            )
        )

    # ── Daily loss cap (momentum-local) ───────────────────────────────────
    daily_pnl = _daily_realized_pnl(db, user_id)
    # Equity-relative daily-loss circuit-breaker (no fixed-$ magic); falls back to
    # the fixed cap when equity is unavailable. [[feedback_adaptive_no_magic]]
    max_daily_loss = equity_relative_daily_loss_cap(policy.max_daily_loss_usd, ef)
    ok_dloss = daily_pnl > -max_daily_loss
    checks.append(
        _check(
            "daily_loss_cap",
            ok_dloss,
            severity="block" if not ok_dloss and m == "live" else ("warn" if not ok_dloss else "ok"),
            message=f"Daily realized PnL ${daily_pnl:+.2f} vs max loss -${max_daily_loss:.2f}.",
            detail={"daily_pnl_usd": daily_pnl, "max_daily_loss_usd": max_daily_loss},
        )
    )

    # ── Profit-giveback session halt (Ross 50%-giveback rule) ─────────────
    # The UPSIDE mirror of the daily-loss cap: once today's realized PnL has PEAKED at
    # a meaningful equity-relative green AND has since given back >= giveback_fraction
    # of that peak, block new live arming for the rest of the daily window (lock in the
    # green day instead of round-tripping it back to flat/red). The single documented
    # knob is the giveback fraction; the activation threshold is equity-relative (reuses
    # the daily-loss-cap magnitude — no second fixed-$ number). [[feedback_adaptive_no_magic]]
    gb = evaluate_profit_giveback_halt(db, user_id=user_id, execution_family=ef)
    checks.append(
        _check(
            "profit_giveback",
            not gb["halted"],
            severity="block" if gb["halted"] and m == "live" else ("warn" if gb["halted"] else "ok"),
            message=(
                f"Profit giveback halt: realized PnL ${gb['daily_pnl_usd']:+.2f} gave back "
                f">= {int(round(gb['giveback_fraction'] * 100))}% of ${gb['peak_pnl_usd']:+.2f} peak "
                f"(halts at <= ${gb['giveback_floor_usd']:+.2f})."
                if gb["halted"]
                else (
                    f"Profit giveback within band (peak ${gb['peak_pnl_usd']:+.2f}, "
                    f"now ${gb['daily_pnl_usd']:+.2f})."
                )
            ),
            detail=gb,
        )
    )

    # ── Green-to-red session breaker (Ross gap #8) ────────────────────────
    # Stricter complement of the giveback halt: once the day PEAKED green above a small
    # equity-relative activation and current realized PnL has round-tripped to <= $0,
    # block new live arming (the green-to-red emotional-hijack walk-away the giveback's
    # above-$0 floor misses). [[feedback_adaptive_no_magic]]
    g2r = evaluate_green_to_red_halt(db, user_id=user_id, execution_family=ef)
    checks.append(
        _check(
            "green_to_red",
            not g2r["halted"],
            severity="block" if g2r["halted"] and m == "live" else ("warn" if g2r["halted"] else "ok"),
            message=(
                f"Green-to-red halt: peaked ${g2r['peak_pnl_usd']:+.2f} (>= "
                f"${g2r['activation_threshold_usd']:+.2f}) then round-tripped to "
                f"${g2r['daily_pnl_usd']:+.2f} — walk away for the session."
                if g2r["halted"]
                else (
                    f"Green-to-red ok (peak ${g2r['peak_pnl_usd']:+.2f}, "
                    f"now ${g2r['daily_pnl_usd']:+.2f})."
                )
            ),
            detail=g2r,
        )
    )

    # ── Global daily loss cap (P0.2 — spans autotrader + momentum) ────────
    # Read-only here: we block new entries if already breached, but do NOT
    # activate the kill switch from a pre-entry "what if" evaluation. The
    # post-close hooks (feedback_emit / auto_trader_monitor) do the actual
    # activation when a realized-loss event lands.
    try:
        if bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True)):
            # PER-BROKER: block this candidate only if ITS OWN broker breached its
            # own real-equity cap — a Coinbase-sized breach can't block an RH arm
            # (the literal 2026-06-15 incident). Read-only (activate=False).
            from ..governance import _peek_broker_breach

            _pb_breached, _pb = _peek_broker_breach(db, ef, user_id=user_id)
            ok_gdl = not _pb_breached
            checks.append(
                _check(
                    "global_daily_loss_cap",
                    ok_gdl,
                    severity="block" if not ok_gdl and m == "live" else ("warn" if not ok_gdl else "ok"),
                    message=(
                        f"Broker[{_pb.get('family')}] realized PnL "
                        f"${float(_pb.get('realized', 0.0) or 0.0):+.2f} "
                        f"vs cap -${float(_pb.get('limit', _pb.get('cap', 0.0)) or 0.0):.2f}."
                    ),
                    detail=_pb,
                )
            )
        else:
            from ..governance import check_daily_loss_breach
            gdl = check_daily_loss_breach(db, user_id=user_id, activate=False)
            ok_gdl = not bool(gdl.get("breached"))
            if gdl.get("source") != "none":
                checks.append(
                    _check(
                        "global_daily_loss_cap",
                        ok_gdl,
                        severity="block" if not ok_gdl and m == "live" else ("warn" if not ok_gdl else "ok"),
                        message=(
                            f"Global realized PnL ${float(gdl.get('realized_usd', 0.0)):+.2f} "
                            f"vs cap -${float(gdl.get('limit_usd', 0.0)):.2f} "
                            f"(src={gdl.get('source')})."
                        ),
                        detail={
                            "realized_usd": gdl.get("realized_usd"),
                            "limit_usd": gdl.get("limit_usd"),
                            "source": gdl.get("source"),
                            "breakdown": gdl.get("breakdown"),
                        },
                    )
                )
    except Exception:
        # Non-fatal: the post-close hook is the real enforcement; this check
        # is additive / informational at pre-entry.
        pass

    # ── Aggregate open at-risk cap (correlation guard, 2026-06-11) ─────────
    # Low-float momentum positions are REGIME-correlated: they fade together.
    # Cap the SUM of entry-to-stop risk across open equity positions at an
    # equity-relative ceiling; a new entry may not push the pile-up past it.
    try:
        _agg_pct = float(getattr(
            settings, "chili_momentum_max_aggregate_risk_pct_of_equity", 0.03) or 0.0)
        if _agg_pct > 0 and m == "live":
            from .risk_policy import _account_equity_usd

            _eq = _account_equity_usd()
            if _eq and float(_eq) > 0:
                _agg_cap = _agg_pct * float(_eq)
                _open_risk, _open_rows = aggregate_open_risk_usd(db, user_id=user_id)
                # the candidate entry's planned risk = the lane's per-trade loss cap
                try:
                    from .risk_policy import equity_relative_loss_cap

                    _planned = float(equity_relative_loss_cap(0.0) or 0.0)
                except Exception:
                    _planned = 0.0
                _ok_agg = (_open_risk + _planned) <= _agg_cap
                checks.append(
                    _check(
                        "aggregate_open_risk_cap",
                        _ok_agg,
                        severity="block" if not _ok_agg else "ok",
                        message=(
                            f"Open at-risk ${_open_risk:,.0f} + planned ${_planned:,.0f} "
                            f"vs cap ${_agg_cap:,.0f} ({_agg_pct:.1%} of equity)."
                        ),
                        detail={
                            "open_risk_usd": round(_open_risk, 2),
                            "planned_risk_usd": round(_planned, 2),
                            "cap_usd": round(_agg_cap, 2),
                            "positions": _open_rows,
                        },
                    )
                )
    except Exception:
        # Additive guard: never brick entries on its own failure.
        pass

    # ── Portfolio drawdown breaker (Hard Rule 2 — spans every entry path) ──
    # The portfolio tier samples ALL closed trades (attributed + no_pattern
    # + manual + reconcile-inferred), independent of the momentum-local
    # daily-loss cap above. Wired here so the AUTHORITATIVE momentum arm
    # path enforces Hard Rule 2 as a hard block — not only at the
    # venue-adapter BUY gate (_assert_portfolio_breaker_ok) + auto_arm
    # Guard 3 (both fail-open pre-checks). check_portfolio_drawdown_breaker
    # returns (False, None) when disabled / shadow / insufficient history /
    # not tripped, and (True, reason) ONLY when enabled AND live AND the
    # trip condition is met (it is fail-CLOSED on its own DB/threshold
    # errors in live mode). Shadow-mode "would_have_tripped" logging is
    # emitted inside the helper. (2026-06-07 momentum-lane audit.)
    try:
        from ..portfolio_risk import check_portfolio_drawdown_breaker

        pdd_tripped, pdd_reason = check_portfolio_drawdown_breaker(db, user_id)
    except Exception:
        # A setup/import failure (NOT a breaker trip — a genuine trip
        # returns normally above and never raises). Fail-open with a warn so
        # an unwired environment is not bricked; the venue-adapter gate is
        # the live-money backstop.
        checks.append(
            _check(
                "portfolio_dd_breaker",
                True,
                severity="warn",
                message=(
                    "Portfolio drawdown breaker check unavailable (setup error); "
                    "venue-adapter gate is the backstop."
                ),
            )
        )
    else:
        checks.append(
            _check(
                "portfolio_dd_breaker",
                not pdd_tripped,
                severity="block" if pdd_tripped and m == "live" else ("warn" if pdd_tripped else "ok"),
                message=(
                    str(pdd_reason)
                    if pdd_tripped
                    else "Portfolio drawdown breaker not tripped."
                ),
                detail={"tripped": bool(pdd_tripped)},
            )
        )

    checks.append(
        _check(
            "notional_cap",
            True,
            severity="ok",
            message="Max notional per trade is enforced at the runner order boundary before adapter submission.",
            detail={
                "max_notional_per_trade_usd": policy.max_notional_per_trade_usd,
                "enforcement_boundary": "momentum_live_runner_pre_adapter",
            },
        )
    )

    # ── Aggregate severity ────────────────────────────────────────────────
    has_block = any(c.get("severity") == "block" and not c.get("ok") for c in checks)
    has_warn = any(c.get("severity") == "warn" and not c.get("ok") for c in checks)
    allowed = not has_block
    if has_block:
        severity = "block"
    elif has_warn:
        severity = "warn"
    else:
        severity = "ok"

    for c in checks:
        if not c.get("ok") and c.get("severity") == "warn":
            warnings.append(str(c.get("message", "")))
        if not c.get("ok") and c.get("severity") == "block":
            errors.append(str(c.get("message", "")))

    evaluated_at = datetime.now(timezone.utc).isoformat()
    return {
        "allowed": allowed,
        "severity": severity,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "effective_policy_summary": {
            "policy_version": POLICY_VERSION,
            "mode": m,
            "execution_family": ef,
            "max_spread_bps": policy.max_spread_bps_live if m == "live" else policy.max_spread_bps_paper,
            "max_concurrent_sessions": policy.max_concurrent_sessions,
            "max_concurrent_live_sessions": policy.max_concurrent_live_sessions,
        },
        "governance_state": governance_state,
        "freshness_state": freshness_state,
        "viability_state": viability_state,
        "evaluated_at_utc": evaluated_at,
    }


def evaluate_existing_automation_session(
    db: Session,
    *,
    user_id: int,
    session_id: int,
) -> dict[str, Any]:
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {
            "allowed": False,
            "severity": "block",
            "checks": [_check("session", False, severity="block", message="Session not found.")],
            "warnings": [],
            "errors": ["Session not found."],
            "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    return evaluate_proposed_momentum_automation(
        db,
        user_id=user_id,
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode=sess.mode,
        execution_family=sess.execution_family,
        exclude_session_id=int(sess.id),
    )


def summarize_risk_from_snapshot(snap: Any) -> dict[str, Any]:
    """Light read-model for list views (persisted evaluation only)."""
    if not isinstance(snap, dict):
        return {"severity": "unknown", "allowed": True, "reasons": []}
    mr = snap.get("momentum_risk")
    if not isinstance(mr, dict):
        return {"severity": "unknown", "allowed": True, "reasons": ["no_risk_evaluation_stored"]}
    reasons = list(mr.get("errors") or [])[:4]
    reasons.extend(list(mr.get("warnings") or [])[:2])
    return {
        "severity": mr.get("severity", "unknown"),
        "allowed": bool(mr.get("allowed", True)),
        "evaluated_at_utc": mr.get("evaluated_at_utc"),
        "reasons": reasons[:6],
        "governance_inhibit": bool((mr.get("governance_state") or {}).get("kill_switch_active")),
    }
