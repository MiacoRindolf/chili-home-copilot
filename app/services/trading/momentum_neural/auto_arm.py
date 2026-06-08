"""Autonomous auto-arm-live for the momentum lane (Ross-style).

The live runner can ENTER/MANAGE a live session, but a session still had to be
armed by hand (the Phase-8 deliberate-arm guard). This pass closes that last gap:
each tick it ranks FRESH, LIVE-ELIGIBLE viability candidates and arms ONE whose
momentum entry trigger (pullback-break / volume) is firing NOW — exactly how Ross
picks "the one moving right now" rather than a stale leader.

It NEVER bypasses a guard. Arming goes through the same operator arm flow
(begin_live_arm -> confirm_live_arm), which re-checks kill-switch, drawdown,
concurrency, viability freshness, broker can_trade, and the equity-relative caps.
On top of that this pass pre-checks the cheap guards (kill-switch, global
concurrency=1, the portfolio drawdown breaker, per-symbol autopilot mutex) so it
fails fast and never spams pending arms. live + on, fully guarded.
docs/STRATEGY (auto-arm-live); see [[project_momentum_lane]].
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationSession
from .live_fsm import (
    LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY,
    STATE_ARMED_PENDING_RUNNER,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)

logger = logging.getLogger(__name__)

# Pre-entry live states safe to reap (no position yet). Never reap entered/
# holding/scaling/trailing/exited/cooldown — those own or just owned a position.
_REAPABLE_PRE_ENTRY_STATES = frozenset(
    {STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE, STATE_WATCHING_LIVE}
)


def _auto_arm_user_id() -> int | None:
    return getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )


def _max_live_sessions() -> int:
    return max(1, int(getattr(settings, "chili_momentum_risk_max_concurrent_live_sessions", 1)))


def _scan_limit() -> int:
    return max(1, int(getattr(settings, "chili_momentum_auto_arm_scan_limit", 10)))


def _auto_arm_crypto_only() -> bool:
    return bool(getattr(settings, "chili_momentum_auto_arm_crypto_only", True))


def _is_coinbase_tradeable_symbol(symbol: str) -> bool:
    """The momentum live lane trades via coinbase_spot. Coinbase crypto pairs use
    the ``-USD`` / ``-USDC`` convention; equities (ARKK, CLSK) are bare tickers. So
    a ``-USD`` substring distinguishes a crypto pair the venue can actually trade
    from an equity that would fail at order time (esp. once US market opens)."""
    return "-USD" in str(symbol or "").upper()


def _symbol_market_open(symbol: str) -> bool:
    """True if the symbol can be entered NOW. Crypto is 24/7; equities only during
    US regular hours (9:30-16:00 ET) — never arm a stock that can't fill."""
    try:
        from .market_profile import market_open_now

        return bool(market_open_now(symbol))
    except Exception:
        # Fail safe: crypto (-USD) is always tradeable; if unsure on an equity, skip.
        return "-USD" in str(symbol or "").upper()


def _max_watch_seconds() -> int:
    return max(60, int(getattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 1800)))


def _reap_stale_watching_sessions(db: Session, *, user_id: int | None, now: datetime) -> int:
    """Cancel PRE-ENTRY live sessions that have watched too long without entering,
    freeing the concurrency slot for a fresher surging candidate — Ross moves on
    when a setup never triggers. Never touches a session that holds a position.
    """
    cutoff = now - timedelta(seconds=_max_watch_seconds())
    try:
        q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(_REAPABLE_PRE_ENTRY_STATES),
            TradingAutomationSession.started_at < cutoff,
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        rows = q.all()
    except Exception:
        return 0
    if not rows:
        return 0
    from .automation_query import cancel_automation_session

    reaped = 0
    for s in rows:
        try:
            cancel_automation_session(db, user_id=int(user_id), session_id=int(s.id))
            reaped += 1
            logger.warning(
                "[auto_arm] reaped stale pre-entry session=%s %s state=%s "
                "(watched > %ss, never entered) — freeing slot for a fresher mover",
                s.id, s.symbol, s.state, _max_watch_seconds(),
            )
        except Exception:
            logger.debug("[auto_arm] reap failed session=%s", getattr(s, "id", None), exc_info=True)
    return reaped


def _active_live_session_count(db: Session, *, user_id: int | None) -> int:
    """Live sessions occupying a concurrency slot (any symbol) for the user."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state.in_(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY),
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    return int(q.count())


def _symbols_with_active_live_session(db: Session, *, user_id: int | None) -> set[str]:
    """Symbols that already hold a non-terminal live momentum session.

    Mirrors begin_live_arm's dedup (the SAME _TERMINAL_OPERATOR_STATES — single
    source of truth) so the auto-arm never re-picks a symbol the operator flow
    would simply dedup. Without this guard the top-viability name (e.g. one hot
    crypto) is chosen every pass; begin_live_arm then returns that session's
    already-watching token, confirm_live_arm fails `invalid_token` (the token's
    session is no longer arm-pending), and the rest of the explosive board is
    starved. Skipping busy symbols rotates the lane to the next fresh setup —
    Ross-style — and removes the confirm noise entirely.
    """
    try:
        from .operator_actions import _TERMINAL_OPERATOR_STATES

        q = db.query(TradingAutomationSession.symbol).filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.notin_(tuple(_TERMINAL_OPERATOR_STATES)),
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        return {str(s).upper() for (s,) in q.all() if s}
    except Exception:
        # Fail-open: no exclusions. begin_live_arm's own dedup still prevents a
        # true double-arm; this guard is a selection-quality filter, not a safety
        # gate, so a DB hiccup must not block the pass.
        return set()


def _dedupe_by_symbol(rows: list[Any], *, limit: int) -> list[Any]:
    """Keep the highest-viability variant per SYMBOL (rows must be pre-sorted by
    viability desc), so the scan covers `limit` DISTINCT symbols — not `limit`
    variants of the same hot name (each symbol carries ~10 variants)."""
    seen: set[str] = set()
    out: list[Any] = []
    for r in rows:
        sym = getattr(r, "symbol", None)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(r)
        if len(out) >= int(limit):
            break
    return out


def _fresh_live_eligible_candidates(db: Session, *, limit: int) -> list[MomentumSymbolViability]:
    """Top live-eligible candidates (distinct symbols) fresh within the LIVE risk
    gate (600s).

    The viability board keeps ~1h of rows, but the arm's risk evaluator requires
    freshness <= viability_max_age, so we filter to that here to never pick a
    candidate the arm would reject. Each symbol has many variants; we fetch a
    generous slice then dedupe to the best variant per distinct symbol.
    """
    max_age = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
    cutoff = datetime.utcnow() - timedelta(seconds=max_age)
    q = db.query(MomentumSymbolViability).filter(
        MomentumSymbolViability.scope == "symbol",
        MomentumSymbolViability.live_eligible.is_(True),
        MomentumSymbolViability.freshness_ts >= cutoff,
    )
    if _auto_arm_crypto_only():
        # Exclude equities (ARKK, CLSK...) that go live-eligible at US market open —
        # the coinbase_spot lane cannot trade them. Crypto pairs carry "-USD".
        q = q.filter(MomentumSymbolViability.symbol.like("%-USD%"))
    rows = (
        q.order_by(MomentumSymbolViability.viability_score.desc())
        .limit(max(int(limit) * 25, 200))
        .all()
    )
    return _dedupe_by_symbol(rows, limit=int(limit))


def _symbol_free(db: Session, symbol: str, user_id: int | None) -> bool:
    """Per-symbol autopilot mutex vs AutoTrader v1 (fail open on helper error)."""
    try:
        from ..autopilot_scope import check_autopilot_entry_gate

        gate = check_autopilot_entry_gate(
            db, candidate="momentum_neural", symbol=symbol, user_id=user_id
        )
        return bool(gate.get("allowed", True))
    except Exception:
        return True


def _entry_trigger_fires(symbol: str) -> tuple[bool, str]:
    """Replicate the live_runner WATCHING_LIVE hybrid trigger to find a name
    whose momentum is breaking NOW (pullback-break preferred, volume fallback)."""
    try:
        from ..market_data import fetch_ohlcv_df
        from .entry_gates import momentum_volume_confirmation, pullback_break_confirmation

        mode = str(getattr(settings, "chili_momentum_entry_trigger_mode", "hybrid") or "hybrid").lower()
        interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
        if mode in ("hybrid", "pullback_break"):
            df_pb = fetch_ohlcv_df(symbol, interval=interval, period="5d")
            if df_pb is not None and not getattr(df_pb, "empty", True):
                ok, reason, _ = pullback_break_confirmation(df_pb, entry_interval=interval)
                if ok:
                    return True, reason
                if mode == "pullback_break":
                    return False, reason
        if mode != "pullback_break":
            df = fetch_ohlcv_df(symbol, interval="15m", period="5d")
            if df is None or getattr(df, "empty", True):
                return False, "no_data"
            return momentum_volume_confirmation(df)
    except Exception:
        return False, "trigger_error"
    return False, "trigger_wait"


def _require_fresh_impulse() -> bool:
    """Selection->entry alignment, ON by default: drop FADED 24h movers from the live
    slot and watch the FRESHEST in-impulse name instead. One documented knob — set
    ``CHILI_MOMENTUM_AUTO_ARM_REQUIRE_FRESH_IMPULSE=0`` to restore the prior
    arm-only-on-an-active-break behaviour. docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md ME-4."""
    return bool(getattr(settings, "chili_momentum_auto_arm_require_fresh_impulse", True))


def _freshness_retracement_threshold() -> float:
    """The 'near recent high' bar reuses the entry gate's OWN shallow/deep boundary
    (``pullback_break_confirmation``'s ``retracement_threshold``, 0.50) so the freshness
    filter and the gate share one self-consistent definition of 'shallow' — no separate
    magic cutoff. Tracks the gate's setting if/when it is wired to one."""
    try:
        return float(getattr(settings, "chili_momentum_pullback_retracement_threshold", 0.50) or 0.50)
    except (TypeError, ValueError):
        return 0.50


def _candidate_freshness(symbol: str):
    """``ross_momentum.intraday_impulse_freshness`` for a candidate, on the SAME intraday
    interval the entry trigger uses (a cache-hit OHLCV fetch). Returns the result, or
    ``None`` on missing data / error — FAIL-OPEN, because the freshness filter is a
    selection-quality filter, not a safety gate (the entry gate + risk belts still
    control the actual entry); a market-data hiccup must never block arming."""
    try:
        from ..market_data import fetch_ohlcv_df
        from .ross_momentum import intraday_impulse_freshness

        interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
        df = fetch_ohlcv_df(symbol, interval=interval, period="5d")
        if df is None or getattr(df, "empty", True):
            return None
        return intraday_impulse_freshness(df, retracement_threshold=_freshness_retracement_threshold())
    except Exception:
        return None


def _probe_candidate(symbol: str) -> tuple[bool, str, Any]:
    """One network-bound pass per candidate: (trigger fires?, reason, freshness)."""
    fires, reason = _entry_trigger_fires(symbol)
    return fires, reason, _candidate_freshness(symbol)


def _known_fresh(fresh: Any) -> bool:
    """True only when we POSITIVELY know the name is in a fresh up-impulse (so it is a
    worthwhile name to WATCH). Unknown freshness (None) is NOT watched proactively —
    only an actively-firing break arms an unknown (fail-open on the firing path)."""
    return bool(getattr(fresh, "is_fresh", False)) if fresh is not None else False


def _freshness_rank(fresh: Any) -> float:
    """Ranking key: current price's position in the recent intraday range (higher =
    closer to / above the recent high = fresher). Unknown ranks last among knowns."""
    return float(getattr(fresh, "position_in_range", 0.0) or 0.0) if fresh is not None else 0.0


def run_auto_arm_pass(db: Session) -> dict[str, Any]:
    """Single auto-arm pass. Returns a summary dict (armed 0/1)."""
    out: dict[str, Any] = {"checked": 0, "scanned": 0, "armed": 0, "skipped": None}

    if not bool(getattr(settings, "chili_momentum_auto_arm_live_enabled", True)):
        out["skipped"] = "flag_off"
        return out
    # Only meaningful when the live runner is on to process an armed session.
    if not bool(getattr(settings, "chili_momentum_live_runner_enabled", False)):
        out["skipped"] = "live_runner_off"
        return out

    uid = _auto_arm_user_id()
    if uid is None:
        out["skipped"] = "no_user"
        return out

    # Guard 1: kill switch.
    try:
        from ..governance import is_kill_switch_active

        if is_kill_switch_active():
            out["skipped"] = "kill_switch"
            return out
    except Exception:
        pass

    # Reap stale pre-entry sessions FIRST so a faded leftover (e.g. a name armed
    # long ago whose intraday move never triggered) does not pin the only slot.
    reaped = _reap_stale_watching_sessions(db, user_id=uid, now=datetime.utcnow())
    if reaped:
        out["reaped"] = reaped
        db.commit()

    # Guard 2: global concurrency (one live position at a time by default).
    active = _active_live_session_count(db, user_id=uid)
    if active >= _max_live_sessions():
        out["skipped"] = "live_session_active"
        out["active"] = active
        return out

    # Guard 3: portfolio drawdown breaker (Hard Rule 2 — not enforced in the
    # arm path; shadow mode returns not-tripped).
    try:
        from ..portfolio_risk import check_portfolio_drawdown_breaker

        tripped, reason = check_portfolio_drawdown_breaker(db, int(uid))
        if tripped:
            out["skipped"] = "drawdown_breaker"
            out["dd_reason"] = reason
            return out
    except Exception:
        pass

    # Guard 4: daily-loss circuit breaker. If today's realized PnL already breached
    # the equity-relative daily cap, EVERY begin_live_arm returns risk_blocked — so
    # skip the whole scan (OHLCV fetches + arm attempts) and report it CLEARLY rather
    # than churning the top candidate every 30s with a misleading begin_blocked. The
    # cap is authoritatively re-enforced in begin_live_arm; this is a cheap early-out
    # that mirrors risk_evaluator's daily_loss_cap check. Fail-open. MOMENTUM_LANE.md
    try:
        from .risk_evaluator import _daily_realized_pnl
        from .risk_policy import equity_relative_daily_loss_cap
        from ..execution_family_registry import EXECUTION_FAMILY_COINBASE_SPOT

        _max_dl = equity_relative_daily_loss_cap(
            float(getattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
            EXECUTION_FAMILY_COINBASE_SPOT,
        )
        _daily_pnl = _daily_realized_pnl(db, int(uid))
        if _daily_pnl <= -_max_dl:
            out["skipped"] = "daily_loss_cap"
            out["daily_pnl_usd"] = round(float(_daily_pnl), 2)
            out["max_daily_loss_usd"] = round(float(_max_dl), 2)
            return out
    except Exception:
        pass

    # Guard 5: profit-giveback session halt (Ross 50%-giveback rule). The UPSIDE mirror
    # of Guard 4: once today's realized PnL peaked at a meaningful (equity-relative)
    # green and has since given back >= the giveback fraction of that peak, STOP arming
    # for the rest of the daily window — lock in the green day instead of round-tripping
    # it back to flat/red. Authoritatively re-enforced in begin_live_arm (risk_evaluator
    # profit_giveback check); this is the cheap early-out that mirrors Guard 4 so the
    # pass reports it clearly instead of churning every candidate into begin_blocked.
    # Fail-open. MOMENTUM_LANE.md [[project_momentum_lane]] [[feedback_adaptive_no_magic]]
    try:
        from .risk_evaluator import evaluate_profit_giveback_halt
        from ..execution_family_registry import EXECUTION_FAMILY_COINBASE_SPOT

        _gb = evaluate_profit_giveback_halt(
            db, user_id=int(uid), execution_family=EXECUTION_FAMILY_COINBASE_SPOT
        )
        if _gb.get("halted"):
            out["skipped"] = "profit_giveback"
            out["daily_pnl_usd"] = _gb.get("daily_pnl_usd")
            out["peak_pnl_usd"] = _gb.get("peak_pnl_usd")
            out["giveback_fraction"] = _gb.get("giveback_fraction")
            return out
    except Exception:
        pass

    # Clear expired pending arms so they do not pin a concurrency slot.
    try:
        from .automation_query import expire_stale_live_arm_sessions

        expire_stale_live_arm_sessions(db, user_id=int(uid))
    except Exception:
        pass

    candidates = _fresh_live_eligible_candidates(db, limit=_scan_limit())
    out["scanned"] = len(candidates)
    if not candidates:
        out["skipped"] = "no_fresh_live_eligible"
        return out

    # Cheap pre-filter (no network): venue, market hours, per-symbol mutex,
    # and self-collision (a symbol we already hold an active live session for).
    busy_symbols = _symbols_with_active_live_session(db, user_id=uid)
    out["busy_skipped"] = 0
    eligible: list[MomentumSymbolViability] = []
    for c in candidates:
        out["checked"] += 1
        if _auto_arm_crypto_only() and not _is_coinbase_tradeable_symbol(c.symbol):
            continue  # defensive: never arm an equity via the coinbase_spot lane
        if c.symbol.upper() in busy_symbols:
            out["busy_skipped"] += 1
            continue  # already have a live session for this symbol — rotate to the next setup
        if not _symbol_market_open(c.symbol):
            continue  # equities only during RTH; crypto always passes (24/7)
        if not _symbol_free(db, c.symbol, uid):
            continue
        eligible.append(c)

    # Probe entry trigger + intraday-impulse freshness CONCURRENTLY. Each probe fetches
    # OHLCV (network-bound), so checking serially made a pass take ~40s — past the 30s
    # cadence, so the scheduler skipped overlapping runs and reacted slowly. Parallel
    # fetch -> a pass is ~the slowest single fetch (~5s).
    chosen: MomentumSymbolViability | None = None
    chosen_reason: str | None = None
    out["faded_skipped"] = 0
    if eligible:
        import concurrent.futures

        _workers = min(
            len(eligible),
            max(1, int(getattr(settings, "chili_momentum_auto_arm_trigger_workers", 8))),
        )
        _results: dict[str, tuple[bool, str, Any]] = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_workers) as _ex:
                _futs = {_ex.submit(_probe_candidate, c.symbol): c.symbol for c in eligible}
                for _fut in concurrent.futures.as_completed(_futs):
                    _sym = _futs[_fut]
                    try:
                        _results[_sym] = _fut.result()
                    except Exception:
                        _results[_sym] = (False, "trigger_error", None)
        except Exception:
            # Fall back to serial if the pool fails for any reason.
            for c in eligible:
                _results[c.symbol] = _probe_candidate(c.symbol)

        # SELECTION->ENTRY ALIGNMENT (M4 keystone). The viability board ranks the day's
        # 24h-cumulative movers, but many have FADED into a deep intraday retrace by the
        # time the pullback gate sees them — over recent bars faded names returned a 0.00%
        # break fire-rate while every fire came from a still-fresh name (dry-run 2026-06-07).
        # So: (1) a name whose break is FIRING now is always a valid entry — arm the
        # freshest of those; (2) otherwise WATCH the freshest name we POSITIVELY know is in
        # a fresh up-impulse (Ross's "the one moving right now") rather than pinning the
        # single live slot on the stale 24h leader. The live runner still confirms the
        # actual break (+ viability + market-open + belts) before any order is placed.
        # docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md ME-4.
        def _r(_c) -> tuple[bool, str, Any]:
            return _results.get(_c.symbol, (False, "no_result", None))

        _firing = sorted(
            (c for c in eligible if _r(c)[0]),
            key=lambda c: _freshness_rank(_r(c)[2]),
            reverse=True,
        )
        if _firing:
            chosen = _firing[0]
            chosen_reason = _r(chosen)[1]
        elif _require_fresh_impulse():
            _watch = sorted(
                (c for c in eligible if _known_fresh(_r(c)[2])),
                key=lambda c: _freshness_rank(_r(c)[2]),
                reverse=True,
            )
            out["faded_skipped"] = len(eligible) - len(_watch)
            if _watch:
                chosen = _watch[0]
                chosen_reason = "fresh_watch:" + str(_r(chosen)[1])
        if chosen is not None:
            _cf = _r(chosen)[2]
            out["chosen_fresh_score"] = (
                round(float(getattr(_cf, "score", 0.0) or 0.0), 4) if _cf is not None else None
            )
            out["chosen_firing"] = bool(_firing)

    if chosen is None:
        out["skipped"] = "no_active_trigger"
        return out

    # Ensure the live client is connected (full-scope cred) before arming.
    try:
        from ...coinbase_service import connect as _cb_connect

        _cb_connect()
    except Exception:
        pass

    from ..execution_family_registry import resolve_execution_family_for_symbol
    from .operator_actions import begin_live_arm, confirm_live_arm

    _exec_family = resolve_execution_family_for_symbol(chosen.symbol)
    out["symbol"] = chosen.symbol
    out["execution_family"] = _exec_family
    out["viability_score"] = round(float(chosen.viability_score or 0.0), 4)
    out["trigger"] = chosen_reason

    begin = begin_live_arm(
        db,
        user_id=int(uid),
        symbol=chosen.symbol,
        variant_id=int(chosen.variant_id),
        execution_family=_exec_family,
    )
    if not begin.get("ok"):
        out["skipped"] = "begin_blocked"
        out["begin_error"] = begin.get("error")
        logger.info(
            "[auto_arm] begin_live_arm blocked %s: %s",
            chosen.symbol, begin.get("error"),
        )
        return out

    if begin.get("deduped"):
        # A race created an active session for this symbol after the busy-set
        # snapshot. begin_live_arm returned the existing session's token, whose
        # session is no longer arm-pending — confirming it would fail
        # invalid_token. Treat as already-active and skip; the live runner owns
        # that session now.
        out["skipped"] = "already_active"
        out["session_id"] = begin.get("session_id")
        logger.info(
            "[auto_arm] %s already has an active live session (state=%s) — skip confirm",
            chosen.symbol, begin.get("state"),
        )
        return out

    confirm = confirm_live_arm(
        db, user_id=int(uid), arm_token=begin.get("arm_token"), confirm=True
    )
    if confirm.get("ok"):
        out["armed"] = 1
        out["session_id"] = begin.get("session_id")
        out["state"] = confirm.get("state")
        logger.warning(
            "[auto_arm] ARMED live %s session=%s state=%s trigger=%s viability=%.3f",
            chosen.symbol, begin.get("session_id"), confirm.get("state"),
            chosen_reason, float(chosen.viability_score or 0.0),
        )
    else:
        out["skipped"] = "confirm_blocked"
        out["confirm_error"] = confirm.get("error")
        logger.info(
            "[auto_arm] confirm_live_arm blocked %s: %s",
            chosen.symbol, confirm.get("error"),
        )
    return out
