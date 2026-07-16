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

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationEvent, TradingAutomationSession
from .live_fsm import (
    LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY,
    LIVE_WATCHING_PREFILL_STATES,
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

_AGENTIC_NON_TRADEABLE_SYMBOLS: set[str] = set()
_ENTRY_REJECT_COOLDOWNS: dict[str, dict[str, Any]] = {}


def _auto_arm_user_id() -> int | None:
    return getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )


def _max_live_sessions() -> int:
    # Adaptive (equity-relative, risk-bounded) — scales the live-session cap with account
    # equity instead of a fixed 5; falls back to the fixed cap when equity is unavailable.
    from .risk_policy import adaptive_max_concurrent_live_sessions

    return adaptive_max_concurrent_live_sessions()


def _scan_limit() -> int:
    return max(1, int(getattr(settings, "chili_momentum_auto_arm_scan_limit", 40)))


def _probe_time_budget() -> float:
    """Wall-clock budget (seconds) for the concurrent entry-trigger probe wave. Auto-arm
    arms from whatever probes COMPLETE within it; un-probed candidates defer to the next
    tick. The adaptive control on probe breadth (breadth = as many as finish in the budget,
    not a magic candidate count) and the belt that keeps a wide net inside the cadence."""
    return max(
        1.0,
        float(getattr(settings, "chili_momentum_auto_arm_probe_time_budget_seconds", 18.0) or 18.0),
    )


def _auto_arm_crypto_only() -> bool:
    return bool(getattr(settings, "chili_momentum_auto_arm_crypto_only", True))


def _auto_arm_equity_only() -> bool:
    """Equity-only focus (Ross lane): exclude crypto ('-USD') so the lane trades stocks
    only. Operator-controlled; revisit crypto later. Crypto-only takes precedence if both."""
    return bool(getattr(settings, "chili_momentum_auto_arm_equity_only", False))


def _ross_equity_universe_required() -> bool:
    """Bare equity candidates must stay inside the Ross small-cap universe.

    This is the equity instrument-class boundary. It is intentionally separate
    from ``auto_arm_equity_only``, which is only a venue/lane focus toggle.
    """
    return bool(getattr(settings, "chili_momentum_ross_equity_universe_required", True))


def _lane_execution_family() -> str:
    """The venue whose ACCOUNT EQUITY the lane's equity-relative caps should scale against.
    crypto-only -> Coinbase; else (equity-only or mixed) -> Robinhood (the equity lane).
    Fixes the daily-loss / giveback breakers being computed against the SMALL crypto
    equity — which made them trip on tiny losses and never grow with the (much larger)
    equities account. docs/DESIGN/MOMENTUM_LANE.md [[feedback_adaptive_no_magic]]"""
    from ..execution_family_registry import (
        EXECUTION_FAMILY_COINBASE_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    )

    if _auto_arm_crypto_only():
        return EXECUTION_FAMILY_COINBASE_SPOT
    return EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP


def _is_coinbase_tradeable_symbol(symbol: str) -> bool:
    """The momentum live lane trades via coinbase_spot. Coinbase crypto pairs use
    the ``-USD`` / ``-USDC`` convention; equities (ARKK, CLSK) are bare tickers. So
    a ``-USD`` substring distinguishes a crypto pair the venue can actually trade
    from an equity that would fail at order time (esp. once US market opens)."""
    return "-USD" in str(symbol or "").upper()


def is_agentic_unauthorized_reject(reason: str | None) -> bool:
    """Detect RH Agentic per-symbol/auth rejects from broker text."""
    txt = str(reason or "").lower()
    if not txt:
        return False
    needles = (
        "unauthorized",
        "not authorized",
        "forbidden",
        "not available for agentic",
        "unavailable for agentic",
        "not tradeable",
        "non-tradeable",
        "not tradable",
    )
    return any(n in txt for n in needles)


def _record_agentic_non_tradeable(symbol: str | None) -> None:
    sym = str(symbol or "").upper().strip()
    if sym:
        _AGENTIC_NON_TRADEABLE_SYMBOLS.add(sym)


def _write_entry_reject_cooldown(symbol: str | None, *, reason: str | None = None) -> None:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return
    _ENTRY_REJECT_COOLDOWNS[sym] = {
        "reason": str(reason or ""),
        "ts": datetime.utcnow().isoformat() + "Z",
    }
    if is_agentic_unauthorized_reject(reason):
        _record_agentic_non_tradeable(sym)


def known_24h_eligible_symbols() -> set[str]:
    """Best-effort positive 24h whitelist; empty means no explicit cache available."""
    raw = getattr(settings, "chili_momentum_24h_eligible_symbols", "") or ""
    if isinstance(raw, (list, tuple, set)):
        return {str(s).upper().strip() for s in raw if str(s).strip()}
    return {s.strip().upper() for s in str(raw).replace(";", ",").split(",") if s.strip()}


def _is_24h_eligible(symbol: str | None) -> bool:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    wl = known_24h_eligible_symbols()
    if wl:
        return sym in wl
    return "-USD" in sym


def win_cycle_yellow_size_multiplier(db: Session | None = None, *, execution_family: str | None = None) -> tuple[float, dict]:
    return 1.0, {"reason": "win_cycle_neutral", "execution_family": execution_family}


def per_symbol_fatigue_size_multiplier(
    db: Session | None,
    symbol: str | None,
    *,
    execution_family: str | None = None,
) -> tuple[float, dict]:
    sym = str(symbol or "").upper().strip()
    if sym and sym in _AGENTIC_NON_TRADEABLE_SYMBOLS:
        return 1.0, {"reason": "agentic_non_tradeable_recorded", "symbol": sym}
    return 1.0, {"reason": "per_symbol_fatigue_neutral", "symbol": sym, "execution_family": execution_family}


def hot_cold_tape_size_multiplier(*, atr_pct: float | None = None, rvol: float | None = None) -> tuple[float, dict]:
    return 1.0, {"reason": "hot_cold_neutral", "atr_pct": atr_pct, "rvol": rvol}


def prime_window_size_multiplier() -> tuple[float, dict]:
    return 1.0, {"reason": "prime_window_neutral"}


def _symbol_market_open(symbol: str) -> bool:
    """True if the symbol can be entered NOW. Crypto is 24/7; equities only during
    US regular hours (9:30-16:00 ET) — never arm a stock that can't fill."""
    try:
        from .market_profile import market_open_now

        return bool(
            market_open_now(
                symbol,
                allow_extended_hours=bool(getattr(settings, "chili_autotrader_allow_extended_hours", False)),
            )
        )
    except Exception:
        # Fail safe: crypto (-USD) is always tradeable; if unsure on an equity, skip.
        return "-USD" in str(symbol or "").upper()


def _max_watch_seconds() -> int:
    return max(60, int(getattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 1800)))


def _broker_failure_cooldown_seconds() -> float:
    try:
        return max(
            0.0,
            float(
                getattr(
                    settings,
                    "chili_momentum_auto_arm_broker_failure_cooldown_seconds",
                    600.0,
                )
                or 0.0
            ),
        )
    except (TypeError, ValueError):
        return 600.0


def _recent_broker_order_submit_failure(
    db: Session,
    *,
    user_id: int | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Fresh broker submit failures mean the rail cannot be trusted for new risk.

    Existing live sessions must keep retrying exits, but auto-arm should not add
    exposure while the same execution family is failing order submissions.
    """
    window = _broker_failure_cooldown_seconds()
    if window <= 0:
        return None
    try:
        q = (
            db.query(TradingAutomationEvent, TradingAutomationSession)
            .join(
                TradingAutomationSession,
                TradingAutomationSession.id == TradingAutomationEvent.session_id,
            )
            .filter(
                TradingAutomationSession.mode == "live",
                TradingAutomationSession.execution_family == _lane_execution_family(),
                TradingAutomationEvent.event_type.in_(
                    ("live_exit_submit_failed", "live_entry_submit_failed")
                ),
                TradingAutomationEvent.ts >= now - timedelta(seconds=window),
            )
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        row = q.order_by(TradingAutomationEvent.ts.desc()).first()
        if not row:
            return None
        ev, sess = row
        return {
            "event_type": ev.event_type,
            "ts": ev.ts.isoformat() if getattr(ev, "ts", None) else None,
            "session_id": getattr(sess, "id", None),
            "symbol": getattr(sess, "symbol", None),
            "window_seconds": window,
        }
    except Exception:
        logger.debug("[auto_arm] broker failure cooldown read failed", exc_info=True)
        return None


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
    """Live entries occupying real risk capacity for the user."""
    if bool(getattr(settings, "chili_momentum_decouple_watching_enabled", False)):
        try:
            from .risk_evaluator import count_inflight_entry_orders, count_open_positions

            uid_filter = int(user_id) if user_id is not None else None
            if uid_filter is None:
                return 0
            return int(
                count_open_positions(db, user_id=uid_filter, mode="live")
                + count_inflight_entry_orders(db, user_id=uid_filter)
            )
        except Exception:
            return 0

    # Legacy all-session concurrency slot.
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state.in_(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY),
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    return int(q.count())


def _active_prefill_watch_count(db: Session, *, user_id: int | None) -> int:
    """Non-expired $0-risk pre-fill watchers governed by watch fanout."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state.in_(LIVE_WATCHING_PREFILL_STATES),
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    try:
        from .risk_evaluator import _live_session_counts_for_concurrency, _naive_utc

        now = _naive_utc(datetime.utcnow())
        return int(
            sum(
                1
                for sess in q.all()
                if _live_session_counts_for_concurrency(sess, now=now)
            )
        )
    except Exception:
        return int(q.count())


def _watch_fanout_cap(field_size: int | None = None) -> int:
    try:
        from .risk_policy import adaptive_watch_fanout

        return int(adaptive_watch_fanout(field_size))
    except Exception:
        try:
            return max(1, int(getattr(settings, "chili_momentum_watch_fanout_max", 25) or 25))
        except Exception:
            return 25


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


def _fresh_live_eligible_candidates(
    db: Session,
    *,
    limit: int,
    ross_universe_symbols: set[str] | None = None,
) -> list[MomentumSymbolViability]:
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
    elif _auto_arm_equity_only() or _ross_equity_universe_required():
        # Equity-only focus (Ross lane): exclude crypto ("-USD") pairs so the lane trades
        # stocks only — crypto pre-entry watchers were consuming concurrency + adding noise.
        if ross_universe_symbols is None:
            ross_universe_symbols = _ross_universe_symbols_from_snapshot_rows(
                _ross_snapshot_rows_by_symbol()
            )
        if not ross_universe_symbols:
            logger.warning(
                "[auto_arm] Ross equity universe empty; refusing generic broad-equity fallback"
            )
            return []
        if _auto_arm_equity_only():
            q = q.filter(~MomentumSymbolViability.symbol.like("%-USD%"))
            q = q.filter(MomentumSymbolViability.symbol.in_(sorted(ross_universe_symbols)))
        else:
            q = q.filter(
                or_(
                    MomentumSymbolViability.symbol.like("%-USD%"),
                    MomentumSymbolViability.symbol.in_(sorted(ross_universe_symbols)),
                )
            )
    row_limit = max(int(limit) * 25, 200)
    if ross_universe_symbols:
        # The board stores multiple variants per symbol; fetch enough rows that
        # de-duping cannot starve valid Ross names behind one hot symbol's variants.
        row_limit = max(row_limit, min(len(ross_universe_symbols) * 12, 5000))
    rows = (
        q.order_by(MomentumSymbolViability.viability_score.desc())
        .limit(row_limit)
        .all()
    )
    return _dedupe_by_symbol(rows, limit=int(limit))


def _demote_non_ross_live_eligible_rows(
    db: Session,
    *,
    ross_universe_symbols: set[str],
) -> dict[str, Any]:
    """Keep the live-eligible board itself aligned with the Ross equity lane.

    Final live admission already has a hard Ross universe guard, but leaving
    generic broad-equity or crypto rows marked ``live_eligible=True`` lets the
    selector/audit surface drift away from Ross. In equity-only Ross mode, fresh
    live-eligible rows outside the current Ross universe are not live candidates.
    """
    equity_only = _auto_arm_equity_only()
    ross_required = _ross_equity_universe_required()
    if not equity_only and not ross_required:
        return {"demoted": 0}
    max_age = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
    cutoff = datetime.utcnow() - timedelta(seconds=max_age)
    rows = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.live_eligible.is_(True),
            or_(
                MomentumSymbolViability.scope == "symbol",
                MomentumSymbolViability.scope == "equity",
                MomentumSymbolViability.scope.is_(None),
            ),
            MomentumSymbolViability.freshness_ts >= cutoff,
        )
        .limit(5000)
        .all()
    )
    allowed = {str(s or "").strip().upper() for s in ross_universe_symbols or set()}
    demoted = 0
    by_reason: dict[str, int] = {}
    for row in rows:
        sym = str(getattr(row, "symbol", "") or "").strip().upper()
        if not sym:
            continue
        reason = None
        if _is_coinbase_tradeable_symbol(sym):
            if not equity_only:
                continue
            reason = "crypto_disabled_equity_lane"
        elif allowed and sym not in allowed:
            reason = "outside_ross_equity_universe"
        if reason is None:
            continue
        row.live_eligible = False
        try:
            ex = row.explain_json if isinstance(row.explain_json, dict) else {}
            ex = dict(ex)
            ex["live_eligible_demoted_by"] = "ross_equity_universe_selector"
            ex["live_eligible_demoted_reason"] = reason
            ex["live_eligible_demoted_at_utc"] = datetime.utcnow().isoformat() + "Z"
            row.explain_json = ex
        except Exception:
            pass
        demoted += 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
    if demoted:
        logger.warning(
            "[auto_arm] demoted stale/non-Ross live_eligible rows demoted=%s reasons=%s",
            demoted,
            by_reason,
        )
    return {"demoted": demoted, "reasons": by_reason}


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
                ok, reason, _ = pullback_break_confirmation(
                    df_pb,
                    entry_interval=interval,
                    symbol=symbol,
                )
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


def _candidate_tick_scalp_watch_reason(candidate: MomentumSymbolViability) -> str | None:
    """Fast non-network auto-arm reason for Ross/5-Pillars tick-scalp candidates."""
    ok, reason, _ = _candidate_ross_tick_evidence(candidate)
    return reason if ok else None


def _candidate_ross_tick_evidence(candidate: MomentumSymbolViability) -> tuple[bool, str, dict[str, Any]]:
    """Ross/5-Pillars evidence check for an equity auto-arm candidate."""
    try:
        from .tick_scalp import ross_signal_for_symbol, ross_tick_scalp_evidence_ok

        signal = ross_signal_for_symbol(candidate.execution_readiness_json, candidate.symbol)
        ok, reason, debug = ross_tick_scalp_evidence_ok(signal)
        return bool(ok), str(reason or ""), dict(debug or {})
    except Exception as exc:
        return False, "ross_evidence_error", {"error": str(exc)[:160]}


def _ross_snapshot_rows_by_symbol() -> dict[str, dict]:
    """Current full-market snapshot keyed by ticker for Ross equity universe proof."""
    try:
        from ...massive_client import get_full_market_snapshot
        from .universe import EQUITY_ROSS_SMALLCAP

        snapshot = get_full_market_snapshot(
            max_age_seconds=EQUITY_ROSS_SMALLCAP.snapshot_max_age_seconds
        ) or []
    except Exception:
        logger.debug("[auto_arm] ross snapshot fetch failed", exc_info=True)
        return {}
    out: dict[str, dict] = {}
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker:
            out[ticker] = row
    return out


def _ross_universe_symbols_from_snapshot_rows(rows_by_symbol: dict[str, dict]) -> set[str]:
    """Resolve the current Ross equity universe from snapshot rows.

    This is the selector-source guard: the Ross lane must query candidates from
    low-priced, liquid, up-moving equities first, not from the generic viability
    board and only filter later.
    """
    if not rows_by_symbol:
        return set()
    try:
        from .universe import ross_smallcap_profile_evidence
    except Exception:
        logger.debug("[auto_arm] ross universe helper unavailable", exc_info=True)
        return set()
    out: set[str] = set()
    for symbol, row in rows_by_symbol.items():
        sym = str(symbol or "").strip().upper()
        if not sym:
            continue
        ok, _reason, _debug = ross_smallcap_profile_evidence(sym, snapshot_row=row)
        if ok:
            out.add(sym)
    return out


def _candidate_ross_universe_evidence(
    candidate: MomentumSymbolViability,
    *,
    snapshot_row: dict | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Ross equity lane instrument-class check."""
    try:
        from .tick_scalp import ross_signal_for_symbol
        from .universe import ross_smallcap_profile_evidence

        signal = ross_signal_for_symbol(candidate.execution_readiness_json, candidate.symbol)
        ok, reason, debug = ross_smallcap_profile_evidence(
            candidate.symbol,
            signal=signal,
            snapshot_row=snapshot_row,
        )
        return bool(ok), str(reason or ""), dict(debug or {})
    except Exception as exc:
        return False, "ross_universe_evidence_error", {"error": str(exc)[:160]}


def _candidate_recent_pre_submit_terminal(
    db: Session,
    symbol: str,
    *,
    user_id: int | None,
) -> tuple[bool, dict[str, Any]]:
    """Mirror Ross event admission's no-order terminal cooldown for auto-arm."""
    try:
        from .ross_event_admission import _recent_pre_submit_terminal_block

        return _recent_pre_submit_terminal_block(
            db,
            str(symbol or "").strip().upper(),
            user_id=user_id,
        )
    except Exception:
        logger.debug("[auto_arm] recent terminal block check failed symbol=%s", symbol, exc_info=True)
        return False, {}


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

        _max_dl = equity_relative_daily_loss_cap(
            float(getattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
            _lane_execution_family(),
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

        _gb = evaluate_profit_giveback_halt(
            db, user_id=int(uid), execution_family=_lane_execution_family()
        )
        if _gb.get("halted"):
            out["skipped"] = "profit_giveback"
            out["daily_pnl_usd"] = _gb.get("daily_pnl_usd")
            out["peak_pnl_usd"] = _gb.get("peak_pnl_usd")
            out["giveback_fraction"] = _gb.get("giveback_fraction")
            return out
    except Exception:
        pass

    # Guard 6: broker order rail health. If exits/entries are currently failing
    # at submit time, keep the live runner focused on managing/retrying existing
    # positions and do not add new exposure until the rail has been quiet.
    _broker_fail = _recent_broker_order_submit_failure(
        db, user_id=int(uid), now=datetime.utcnow()
    )
    if _broker_fail:
        out["skipped"] = "broker_order_rail_unhealthy"
        out["broker_failure"] = _broker_fail
        return out

    # Clear expired pending arms so they do not pin a concurrency slot.
    try:
        from .automation_query import expire_stale_live_arm_sessions

        expire_stale_live_arm_sessions(db, user_id=int(uid))
    except Exception:
        pass

    _ross_snapshot_rows: dict[str, dict] = {}
    _ross_universe_symbols: set[str] = set()
    ross_required = _ross_equity_universe_required()
    if _auto_arm_equity_only() or ross_required:
        _ross_snapshot_rows = _ross_snapshot_rows_by_symbol()
        _ross_universe_symbols = _ross_universe_symbols_from_snapshot_rows(_ross_snapshot_rows)
        out["ross_snapshot_symbols"] = len(_ross_snapshot_rows)
        out["ross_universe_symbols"] = len(_ross_universe_symbols)
        if _ross_universe_symbols:
            demote_summary = _demote_non_ross_live_eligible_rows(
                db,
                ross_universe_symbols=_ross_universe_symbols,
            )
            out["live_eligible_demoted"] = demote_summary.get("demoted", 0)
            if demote_summary.get("reasons"):
                out["live_eligible_demoted_reasons"] = demote_summary.get("reasons")

    candidates = _fresh_live_eligible_candidates(
        db,
        limit=_scan_limit(),
        ross_universe_symbols=_ross_universe_symbols if (_auto_arm_equity_only() or ross_required) else None,
    )
    out["scanned"] = len(candidates)
    if not candidates:
        out["skipped"] = "no_fresh_live_eligible"
        return out

    if bool(getattr(settings, "chili_momentum_decouple_watching_enabled", False)):
        watching = _active_prefill_watch_count(db, user_id=uid)
        fanout_cap = _watch_fanout_cap(len(candidates))
        out["watching"] = watching
        out["watch_fanout_cap"] = fanout_cap
        if watching >= fanout_cap:
            out["skipped"] = "watch_fanout_active"
            return out

    # Cheap pre-filter (no network): venue, market hours, per-symbol mutex,
    # and self-collision (a symbol we already hold an active live session for).
    busy_symbols = _symbols_with_active_live_session(db, user_id=uid)
    out["busy_skipped"] = 0
    out["ross_universe_skipped"] = 0
    out["ross_evidence_skipped"] = 0
    out["recent_terminal_skipped"] = 0
    _ross_universe_skip_reasons: dict[str, int] = {}
    _ross_evidence_skip_reasons: dict[str, int] = {}
    eligible: list[MomentumSymbolViability] = []
    for c in candidates:
        out["checked"] += 1
        if _auto_arm_crypto_only() and not _is_coinbase_tradeable_symbol(c.symbol):
            continue  # defensive: never arm an equity via the coinbase_spot lane
        if _auto_arm_equity_only() and _is_coinbase_tradeable_symbol(c.symbol):
            continue  # equity-only focus: never arm crypto in the Ross lane
        if (_auto_arm_equity_only() or ross_required) and not _is_coinbase_tradeable_symbol(c.symbol):
            _ross_universe_ok, _ross_universe_reason, _ross_universe_debug = (
                _candidate_ross_universe_evidence(
                    c,
                    snapshot_row=_ross_snapshot_rows.get(str(c.symbol or "").strip().upper()),
                )
            )
            if not _ross_universe_ok:
                out["ross_universe_skipped"] += 1
                _ross_universe_skip_reasons[_ross_universe_reason] = (
                    _ross_universe_skip_reasons.get(_ross_universe_reason, 0) + 1
                )
                logger.info(
                    "[auto_arm] skip %s: ross universe rejected (%s) %s",
                    c.symbol, _ross_universe_reason, _ross_universe_debug,
                )
                continue
            _ross_ok, _ross_reason, _ross_debug = _candidate_ross_tick_evidence(c)
            if not _ross_ok:
                out["ross_evidence_skipped"] += 1
                _ross_evidence_skip_reasons[_ross_reason] = (
                    _ross_evidence_skip_reasons.get(_ross_reason, 0) + 1
                )
                logger.info(
                    "[auto_arm] skip %s: ross evidence rejected (%s) %s",
                    c.symbol, _ross_reason, _ross_debug,
                )
                continue
            _recent_terminal, _terminal_detail = _candidate_recent_pre_submit_terminal(
                db,
                c.symbol,
                user_id=int(uid),
            )
            if _recent_terminal:
                out["recent_terminal_skipped"] += 1
                logger.info(
                    "[auto_arm] skip %s: recent pre-submit terminal %s",
                    c.symbol,
                    _terminal_detail,
                )
                continue
        if c.symbol.upper() in busy_symbols:
            out["busy_skipped"] += 1
            continue  # already have a live session for this symbol — rotate to the next setup
        if not _symbol_market_open(c.symbol):
            continue  # equities follow RTH/extended-hours setting; crypto always passes
        if not _symbol_free(db, c.symbol, uid):
            continue
        eligible.append(c)
    if _ross_universe_skip_reasons:
        out["ross_universe_skip_reasons"] = _ross_universe_skip_reasons
    if _ross_evidence_skip_reasons:
        out["ross_evidence_skip_reasons"] = _ross_evidence_skip_reasons

    # Probe entry trigger + intraday-impulse freshness CONCURRENTLY. Each probe fetches
    # OHLCV (network-bound), so checking serially made a pass take ~40s — past the 30s
    # cadence, so the scheduler skipped overlapping runs and reacted slowly. Parallel
    # fetch -> a pass is ~the slowest single fetch (~5s).
    chosen: MomentumSymbolViability | None = None
    chosen_reason: str | None = None
    out["faded_skipped"] = 0
    if eligible:
        _results: dict[str, tuple[bool, str, Any]] = {}
        for c in eligible:
            fast_reason = _candidate_tick_scalp_watch_reason(c)
            if fast_reason:
                _results[c.symbol] = (True, fast_reason, None)
        _probe_candidates = [c for c in eligible if c.symbol not in _results]

        if _probe_candidates:
            import concurrent.futures

            _workers = min(
                len(_probe_candidates),
                max(1, int(getattr(settings, "chili_momentum_auto_arm_trigger_workers", 8))),
            )
            _budget = _probe_time_budget()
            _ex = concurrent.futures.ThreadPoolExecutor(max_workers=_workers)
            try:
                _futs = {_ex.submit(_probe_candidate, c.symbol): c.symbol for c in _probe_candidates}
                # Bound the whole wave by wall-clock so a WIDE candidate net never pushes a pass
                # past the scheduler cadence: arm from whatever COMPLETED within the budget;
                # un-probed names defer to the next tick. This is what lets a fresh #11+ name
                # (NPT) get probed at all without the old top-10 truncation, while the pass still
                # returns in time.
                try:
                    for _fut in concurrent.futures.as_completed(_futs, timeout=_budget):
                        _sym = _futs[_fut]
                        try:
                            _results[_sym] = _fut.result()
                        except Exception:
                            _results[_sym] = (False, "trigger_error", None)
                except concurrent.futures.TimeoutError:
                    out["probe_timed_out"] = True
            except Exception:
                # Pool failure -> serial fallback (also budget-bounded).
                import time as _time

                _deadline = _time.monotonic() + _budget
                for c in _probe_candidates:
                    if _time.monotonic() >= _deadline:
                        break
                    if c.symbol not in _results:
                        _results[c.symbol] = _probe_candidate(c.symbol)
            finally:
                # Never block the pass on stragglers: cancel queued probes and DON'T wait on the
                # running ones (they finish in background threads and are discarded). This is what
                # makes the budget a real wall-clock bound, not just a collection timeout.
                _ex.shutdown(wait=False, cancel_futures=True)
        out["probed"] = len(_results)
        out["eligible_probed_of"] = len(eligible)

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

    from ..execution_family_registry import (
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        resolve_execution_family_for_symbol,
    )
    from .operator_actions import begin_live_arm, confirm_live_arm

    if _auto_arm_equity_only() and not _is_coinbase_tradeable_symbol(chosen.symbol):
        _exec_family = EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
    else:
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
