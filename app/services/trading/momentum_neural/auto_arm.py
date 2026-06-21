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
from .crypto_liquidity import crypto_liquidity_ok
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

# Rank-displacement reaps a STRICT SUBSET — only the two TRULY-INERT pre-entry states.
# NOT watching_live: live_fsm makes watching_live -> live_entry_candidate a single legal
# tick, so a watching_live name can fire (place a broker order) within one tick of a
# reap — exactly the cancel-races-fill window that manufactured the CRVO orphan. Rank-
# displacement only ever bumps names that are provably sitting still.
_RANK_DISPLACE_REAPABLE_STATES = frozenset(
    {STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE}
)


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


def _auto_arm_liquidity_bias() -> bool:
    """Prefer FILLABLE (high-dollar-volume -> tighter-spread) Ross small-caps at the
    selection gate so triggers convert to FILLS. ON by default (the live spread gate
    blocks wide-spread entries, so a trigger on an illiquid name never fills); set
    CHILI_MOMENTUM_AUTO_ARM_LIQUIDITY_BIAS=0 to rank by viability alone."""
    return bool(getattr(settings, "chili_momentum_auto_arm_liquidity_bias", True))


def _lane_execution_family() -> str:
    """The venue whose ACCOUNT EQUITY the lane's equity-relative caps should scale against.
    crypto-only -> Coinbase; else the EQUITY lane's configured execution rail — the Robinhood
    Agentic MCP cash account when that rail is active, otherwise legacy robinhood_spot.
    Fixes the daily-loss / giveback breakers being computed against the SMALL crypto
    equity — which made them trip on tiny losses and never grow with the (much larger)
    equities account. The agentic branch fixes the SAME class of bug for the cash-account
    migration: the legacy robinhood_spot account was drained to ~$950 when funds moved to
    the agentic account, so basing the cap on it froze the lane at ~$95/day (one trade)
    while the lane actually trades the $13,800 agentic account. docs/DESIGN/MOMENTUM_LANE.md
    [[feedback_adaptive_no_magic]] [[project_per_broker_daily_loss]]

    FOLLOW-UP (per-broker path, currently OFF via chili_per_broker_daily_loss_enabled): the
    per-broker breaker (governance.broker_daily_loss_breached) still treats only
    robinhood_spot / coinbase_spot as first-class — an agentic family normalizes to
    robinhood_spot there. BEFORE enabling that flag with the agentic rail, make
    robinhood_agentic_mcp first-class in REAL_DAILY_LOSS_FAMILIES + realized_pnl_today_by_broker
    (and re-tune the aggregate-backstop test), else the per-broker cap reverts to the drained
    legacy basis. The ACTIVE path (flag OFF) is already correct via THIS function +
    equity_relative_daily_loss_cap."""
    from ..execution_family_registry import (
        EXECUTION_FAMILY_COINBASE_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
    )

    if _auto_arm_crypto_only():
        return EXECUTION_FAMILY_COINBASE_SPOT
    rail = str(getattr(settings, "chili_equity_execution_rail", "") or "").strip().lower()
    if rail == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
        return EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
    return EXECUTION_FAMILY_ROBINHOOD_SPOT


def _is_coinbase_tradeable_symbol(symbol: str) -> bool:
    """The momentum live lane trades via coinbase_spot. Coinbase crypto pairs use
    the ``-USD`` / ``-USDC`` convention; equities (ARKK, CLSK) are bare tickers. So
    a ``-USD`` substring distinguishes a crypto pair the venue can actually trade
    from an equity that would fail at order time (esp. once US market opens)."""
    return "-USD" in str(symbol or "").upper()


def _crypto_paused_us_session() -> bool:
    """Crypto stands down while the US equity session is OPEN (premarket ->
    16:00 close): every live slot belongs to the equity tape during Ross
    hours, and crypto resumes AUTOMATICALLY after the close — no manual flag
    to remember to flip back (operator directive 2026-06-12)."""
    if not bool(getattr(settings, "chili_momentum_crypto_pause_during_us_session", True)):
        return False
    try:
        from .market_profile import market_session_now

        return market_session_now("SPY") in ("premarket", "regular")
    except Exception:
        return False


def _symbol_market_open(symbol: str) -> bool:
    """True if the symbol can be entered NOW. Crypto is 24/7; equities during the
    EXTENDED session (pre-market → after-hours, per config) so the lane catches Ross's
    pre-market gap-and-go — not just RTH. Outside-RTH orders are flagged extended_hours
    at placement so the venue routes them (Alpaca DAY+ext, RH override)."""
    try:
        from .market_profile import is_tradeable_now

        return bool(is_tradeable_now(symbol))
    except Exception:
        # Fail safe: crypto (-USD) is always tradeable; if unsure on an equity, skip.
        return "-USD" in str(symbol or "").upper()


def _venue_broker_ready_for(symbol: str, cache: dict[str, bool]) -> bool:
    """True if the broker for ``symbol``'s resolved venue can place a live order NOW.

    Memoised per-venue within a pass. The auto-arm picks ONE candidate per pass and
    arms it with NO fallthrough, so if the chosen name's venue is disconnected (e.g.
    the Robinhood token expired) ``confirm_live_arm`` fails ``broker_not_ready`` and the
    pass arms NOTHING — stalling the whole lane, including tradeable crypto/Alpaca names
    that a later candidate would have used. Dropping not-ready venues at SELECTION lets
    the pass fall through to a venue that can actually fill. Fail-OPEN on probe error
    (``confirm_live_arm`` still preflights broker readiness as the backstop)."""
    try:
        from ..execution_family_registry import (
            normalize_execution_family,
            resolve_execution_family_for_symbol,
        )

        ef = normalize_execution_family(resolve_execution_family_for_symbol(symbol))
    except Exception:
        return True
    if ef in cache:
        return cache[ef]
    try:
        from .operator_readiness import build_momentum_operator_readiness

        rd = build_momentum_operator_readiness(execution_family=ef, symbol=symbol)
        ready = bool(rd.get("broker_ready_for_live"))
    except Exception:
        ready = True  # fail-open; confirm_live_arm preflights broker readiness too
    cache[ef] = ready
    return ready


def _max_watch_seconds() -> int:
    return max(60, int(getattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 1800)))


# Per-symbol PRE-ENTRY REAP cooldown (in-process, scheduler-local). A name reaped
# here just held the live slot for the full watch window without firing; cooling it
# down briefly stops it from immediately re-arming and re-occupying the single slot,
# giving genuinely different fresh movers a turn. Diagnosed 2026-06-14: crypto arms
# 460:3 cancel:fill/24h, ~31% concentrated in RENDER(88x)/WLD(56x) looping arm->reap.
_REAP_COOLDOWN: dict[str, datetime] = {}


def _reap_cooldown_active(sym_u: str, now: datetime) -> bool:
    """True if ``sym_u`` (upper) was reaped pre-entry within
    ``chili_momentum_reap_cooldown_sec``. 0 disables (instant kill-switch)."""
    cd_sec = float(getattr(settings, "chili_momentum_reap_cooldown_sec", 300.0) or 0.0)
    if cd_sec <= 0:
        return False
    at = _REAP_COOLDOWN.get(sym_u)
    return at is not None and (now - at).total_seconds() < cd_sec


def _write_reap_cooldown(sym_u: str, now: datetime) -> None:
    """Record a pre-entry reap/displacement of ``sym_u`` (UPPER) so the name sits out
    ``chili_momentum_reap_cooldown_sec`` before it can re-arm — the oscillation damper.
    CLASS-AGNOSTIC (2026-06-17): generalized off the old '-USD'-only gate so EQUITIES are
    damped too (the rank-displacement motivating case, UTSI, is an equity). Bounded prune."""
    if not sym_u:
        return
    _REAP_COOLDOWN[sym_u] = now
    if len(_REAP_COOLDOWN) > 500:
        _stale = now - timedelta(hours=1)
        for _k in [k for k, v in _REAP_COOLDOWN.items() if v < _stale]:
            _REAP_COOLDOWN.pop(_k, None)


def _reap_stale_watching_sessions(db: Session, *, user_id: int | None, now: datetime) -> int:
    """Cancel PRE-ENTRY live sessions that have watched too long without entering,
    freeing the concurrency slot for a fresher surging candidate — Ross moves on
    when a setup never triggers. Never touches a session that holds a position.
    """
    cutoff = now - timedelta(seconds=_max_watch_seconds())
    extend_cutoff = now - timedelta(
        seconds=max(
            _max_watch_seconds(),
            int(getattr(settings, "chili_momentum_auto_arm_watch_extend_seconds", 600) or 600),
        )
    )
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
        # PROGRESSING setups earn the extended watch: a tick-armed session
        # (watch_break_level set = a reclaim/break is actually forming) keeps
        # its slot to the extend cutoff; a watch that never produced a level
        # is dead weight at the base cutoff (triggers fire in ~29s median).
        try:
            _snap = s.risk_snapshot_json or {}
            _le = _snap.get("momentum_live_execution") if isinstance(_snap, dict) else None
            if (
                isinstance(_le, dict)
                and _le.get("watch_break_level")
                and s.started_at >= extend_cutoff
            ):
                continue
        except Exception:
            pass
        try:
            cancel_automation_session(db, user_id=int(user_id), session_id=int(s.id))
            reaped += 1
            # Cool this name down so it doesn't immediately re-arm the slot it just
            # churned without firing (PROGRESSING tick-armed setups are already excluded
            # above via watch_break_level). CLASS-AGNOSTIC (2026-06-17): now damps
            # equities too (was '-USD'-only) so the rank-displacement loop can't
            # oscillate on an equity it just freed.
            try:
                _write_reap_cooldown(str(s.symbol or "").upper(), now)
            except Exception:
                pass
            logger.warning(
                "[auto_arm] reaped stale pre-entry session=%s %s state=%s "
                "(watched > %ss, never entered) — freeing slot for a fresher mover",
                s.id, s.symbol, s.state, _max_watch_seconds(),
            )
        except Exception:
            logger.debug("[auto_arm] reap failed session=%s", getattr(s, "id", None), exc_info=True)
    return reaped


def _finalize_stale_exited_sessions(db: Session, *, user_id: int | None, now: datetime) -> int:
    """BOOKING TRUTH (2026-06-12 waterfall c0 = $195 of unbooked exits): a live
    session parked in exited/cooldown that nobody advances never reaches a
    feedback-terminal state, so its realized PnL never books an outcome row —
    the day reported −$70 when broker truth was −$265. Sessions idle in
    exited/cooldown beyond the finalize window walk the LEGAL FSM chain
    (exited → cooldown → finished) via the live runner's _safe_transition,
    which fires the outcome writer exactly like a runner-driven finish."""
    try:
        idle_min = float(getattr(settings, "chili_momentum_exited_finalize_idle_min", 20.0) or 0.0)
    except (TypeError, ValueError):
        idle_min = 20.0
    if idle_min <= 0:
        return 0
    cutoff = now - timedelta(minutes=idle_min)
    try:
        q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(("live_exited", "live_cooldown")),
            TradingAutomationSession.updated_at < cutoff,
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        rows = q.all()
    except Exception:
        return 0
    if not rows:
        return 0
    from .live_runner import _safe_transition as _live_safe_transition

    done = 0
    for sess in rows:
        try:
            if sess.state == "live_exited":
                _live_safe_transition(db, sess, "live_cooldown")
            _live_safe_transition(db, sess, "live_finished")
            done += 1
            logger.info(
                "[auto_arm] finalized stale exited session=%s %s -> live_finished (outcome booked)",
                sess.id, sess.symbol,
            )
        except Exception:
            logger.debug("[auto_arm] finalize failed session=%s", getattr(sess, "id", None), exc_info=True)
    return done


def _active_live_session_count(db: Session, *, user_id: int | None) -> int:
    """Live sessions occupying a concurrency slot (any symbol) for the user.

    LEGACY single-cap path (decouple_watching OFF). Unchanged — counts every
    pre-fill-or-held state against one cap, which is exactly why the lane never
    fanned past ~5-15 watchers (a $0-risk watcher consumed a real slot)."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state.in_(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY),
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    return int(q.count())


def _count_watching_prefill(db: Session, *, user_id: int | None) -> int:
    """Decouple_watching: $0-risk pre-fill watchers (armed/queued/watching/candidate/
    pending_entry), twin-excluded. Governed by the watch-FANOUT cap, not the risk
    cap. Twins (alpaca paper-soak) never consume a real slot."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state.in_(LIVE_WATCHING_PREFILL_STATES),
        TradingAutomationSession.execution_family != "alpaca_spot",
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    return int(q.count())


def _paper_shadow_arm(
    db: Session, *, uid: int, candidates: list, exclude_symbol: str | None = None
) -> int:
    """PAPER SHADOW MASS (2026-06-11, operator: paper = free sample data): every
    probed ELIGIBLE candidate that did NOT win the single live slot becomes a
    PAPER session. The lane historically armed ~1 live/pass and ZERO paper
    (3 paper sessions EVER vs 718 live) — so exit/entry tuning ran on n=6
    anecdotes. Shadow-arming the rank losers multiplies outcome data ~10-20x
    per pass at zero dollar risk. Bounded by a concurrent-session cap;
    create_paper_draft_session dedupes per symbol/variant. Best-effort."""
    if not bool(getattr(settings, "chili_momentum_paper_shadow_arm_enabled", True)):
        return 0
    if not bool(getattr(settings, "chili_momentum_paper_runner_enabled", False)):
        return 0  # no runner to tick them — don't pile up dead drafts
    cap = int(getattr(settings, "chili_momentum_paper_shadow_max_sessions", 40) or 40)
    try:
        from .operator_actions import _TERMINAL_OPERATOR_STATES

        active = (
            db.query(TradingAutomationSession)
            .filter(
                TradingAutomationSession.mode == "paper",
                ~TradingAutomationSession.state.in_(tuple(_TERMINAL_OPERATOR_STATES)),
            )
            .count()
        )
    except Exception:
        return 0
    budget = max(0, cap - int(active or 0))
    if budget <= 0:
        return 0
    from ..execution_family_registry import resolve_execution_family_for_symbol
    from .operator_actions import create_paper_draft_session

    # A5 crypto clock applies to PAPER too so the weekend soak measures
    # productive-window behavior, not the 0/21 dead band that would pollute the
    # validation gate. Equity paper is unaffected. Resolve once per pass.
    _crypto_clock_blocks = False
    try:
        from .market_profile import crypto_schedule_enabled, crypto_session_active_now

        _crypto_clock_blocks = crypto_schedule_enabled() and not crypto_session_active_now()
    except Exception:
        _crypto_clock_blocks = False

    armed = 0
    _excl = str(exclude_symbol or "").upper()
    for c in candidates:
        if budget <= 0:
            break
        sym = str(getattr(c, "symbol", "") or "").upper()
        if not sym or sym == _excl:
            continue
        if _crypto_clock_blocks and sym.endswith("-USD"):
            continue  # crypto dead band — sit out, like the equity 'late' window
        try:
            res = create_paper_draft_session(
                db,
                user_id=int(uid),
                symbol=sym,
                variant_id=int(getattr(c, "variant_id", 0) or 0),
                # mode="paper": equities route to the Alpaca paper rail when
                # configured (the DMA fill-quality soak; docs/DESIGN/ALPACA_LANE.md)
                execution_family=resolve_execution_family_for_symbol(sym, mode="paper"),
            )
        except Exception:
            logger.debug("[auto_arm] paper shadow arm failed for %s", sym, exc_info=True)
            continue
        if res.get("ok") and not res.get("deduped"):
            armed += 1
            budget -= 1
    return armed


_ALPACA_LISTED_CACHE: dict[str, bool] = {}


def _alpaca_lists_symbol(symbol: str) -> bool:
    """True when Alpaca has a tradable asset for this lane symbol (equity ticker
    or crypto BASE-USD -> BASE/USD). Cached per process — listings change rarely.
    Fail-CLOSED (no twin) on probe errors: the twin is best-effort by design."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    if sym in _ALPACA_LISTED_CACHE:
        return _ALPACA_LISTED_CACHE[sym]
    listed = False
    try:
        from ..venue.alpaca_spot import AlpacaSpotAdapter

        prod, _ = AlpacaSpotAdapter().get_product(sym)
        listed = prod is not None and not bool(getattr(prod, "trading_disabled", True))
    except Exception:
        listed = False
    _ALPACA_LISTED_CACHE[sym] = listed
    return listed


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
    elif _auto_arm_equity_only():
        # Equity-only focus (Ross lane): exclude crypto ("-USD") pairs so the lane trades
        # stocks only — crypto pre-entry watchers were consuming concurrency + adding noise.
        q = q.filter(~MomentumSymbolViability.symbol.like("%-USD%"))
    rows = (
        q.order_by(MomentumSymbolViability.viability_score.desc())
        .limit(max(int(limit) * 25, 200))
        .all()
    )
    # MARKET-OPEN FILTER BEFORE THE LIMIT (2026-06-12 night-lane fix): the
    # top-N by score overnight is stale CLOSED equities (frozen at yesterday's
    # +200%, scoring above every crypto pair) — they filled the whole candidate
    # list, got dropped by the in-loop market-hours check, and the pass probed
    # NOTHING all night: zero crypto arms, zero paper shadow, an empty lane
    # while 320 live-eligible crypto candidates sat fresh. Filter untradeable
    # markets HERE so the limit is spent on names the pass can actually arm.
    rows = [r for r in rows if _symbol_market_open(r.symbol)]
    rows = _filter_fresh_tape(rows)
    # A0 (2026-06-12 selection-alpha study): the composite viability score has
    # ZERO winner discrimination (AUC 0.515, p=0.56) while its own buried Ross
    # sub-score DOES discriminate (AUC 0.58-0.63; ross>=0.8 hits 53% vs 25%
    # base) — a dozen small regime nudges average the working signal away.
    # Rank the arm queue by the ross score (already persisted in the same
    # row); viability stays the eligibility FLOOR and the tiebreak.
    def _ross_rank_key(r):
        try:
            extra = (r.execution_readiness_json or {}).get("extra") or {}
            rs = extra.get("ross_scores") or {}
            ross = float(rs.get("score", rs.get("ross_score", 0.0)) or 0.0)
        except Exception:
            ross = 0.0
        return (ross, float(r.viability_score or 0.0))

    rows = sorted(rows, key=_ross_rank_key, reverse=True)
    if rows:
        if _auto_arm_equity_only():
            rows = _enforce_ross_price_band(rows)
            rows = _liquidity_rerank(rows)
        else:
            # Crypto (crypto-only or mixed): the binary liquidity floor only gates
            # pass/block — among the passers, arm the most FILLABLE 24h-volume name
            # first (deepest book = tighter maker fill + cleaner exit, directly
            # attacking the crypto fill/exit toxicity). Equity rows in a mixed list
            # keep ross order (their re-rank is equity-only above).
            rows = _crypto_liquidity_rerank(rows)
    return _dedupe_by_symbol(rows, limit=int(limit))


def _filter_fresh_tape(rows: list, *, max_age_sec: float | None = None) -> list:
    """ARM only names with a LIVE tape (2026-06-12 IPO morning): the lane was
    arming quiet mid-caps (RYAM/BBD/BMA/ACAD) whose freshest NBBO was 8min-17h
    old — their stale bars probe as pretty pullbacks while the REAL movers
    probe as 'faded'. No fresh tape row = the name is not actually trading in
    this session = the runner will sit behind stale_bbo forever. Selection
    leads the trading window; data freshness leads selection."""
    if not rows:
        return rows
    try:
        age_cap = float(
            max_age_sec
            if max_age_sec is not None
            else getattr(settings, "chili_momentum_arm_tape_freshness_max_sec", 180.0) or 180.0
        )
    except (TypeError, ValueError):
        age_cap = 180.0
    if age_cap <= 0:
        return rows
    # CRYPTO EXEMPTION (2026-06-13): momentum_nbbo_spread_tape records EQUITY
    # only — crypto (-USD) has ZERO rows there, so this equity stale-quote gate
    # was silently dropping EVERY crypto candidate (flag on, but never arming).
    # Crypto majors are 24/7 liquid and already gated by the crypto liquidity
    # floor (C2) + the live-price trigger freshness, so this NBBO-tape gate is
    # equity-only by design. Apply it to equities; pass crypto through unchanged.
    equity_syms = sorted({
        str(r.symbol or "").upper()
        for r in rows
        if not str(r.symbol or "").upper().endswith("-USD")
    })
    if not equity_syms:
        return rows  # all-crypto candidate set — nothing for the equity gate to check
    fresh: set[str] = set()
    from ....db import SessionLocal

    db = SessionLocal()
    try:
        from sqlalchemy import text as _text

        res = db.execute(
            _text(
                "SELECT symbol FROM momentum_nbbo_spread_tape "
                "WHERE symbol = ANY(:syms) "
                "AND observed_at >= now() at time zone 'utc' - make_interval(secs => :cap) "
                "GROUP BY symbol"
            ),
            {"syms": equity_syms, "cap": age_cap},
        )
        fresh = {str(r[0]).upper() for r in res}
    except Exception:
        return rows  # fail-open: tape table unavailable must not kill arming
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()
    return [
        r
        for r in rows
        if str(r.symbol or "").upper().endswith("-USD")  # crypto: exempt (see above)
        or str(r.symbol or "").upper() in fresh
    ]


def _enforce_ross_price_band(
    rows: list[MomentumSymbolViability],
) -> list[MomentumSymbolViability]:
    """Equity-only LIVE-ARM instrument-class gate: keep only candidates whose CURRENT
    price sits in the Ross small-cap band ($1-$20 per ``EQUITY_ROSS_SMALLCAP``).

    Large-caps (MU/MRVL on an earnings breakout) go ``live_eligible`` in
    ``momentum_symbol_viability`` via the broad brain momentum scoring
    (``nm_momentum_crypto_intel``), which is NOT price-screened — and
    ``_fresh_live_eligible_candidates`` ranks by viability alone, so a $100 semi
    would out-rank a real $3 Ross gapper and get armed with real money. This
    enforces the lane's instrument CLASS at the selection gate, reusing the
    profile's existing price knobs (no new thresholds). docs/DESIGN/MOMENTUM_LANE.md

    Fail-SAFE: on a TOTAL snapshot outage, arm nothing we cannot confirm is in-class
    (a live-money gate must not arm on unknown data) and log it so the freeze is
    diagnosable; the lane resumes the instant the snapshot returns (~5min TTL, warm
    through RTH). A helper error also fails safe rather than leaking large-caps."""
    try:
        from .universe import EQUITY_ROSS_SMALLCAP, symbols_within_profile_price_band

        kept, snapshot_ok = symbols_within_profile_price_band(
            [r.symbol for r in rows], EQUITY_ROSS_SMALLCAP
        )
        if not snapshot_ok:
            logger.warning(
                "[auto_arm] ross price-band gate: full-market snapshot unavailable — "
                "holding %d equity candidate(s) (fail-safe; resumes when snapshot returns)",
                len(rows),
            )
            return []
        filtered = [r for r in rows if str(r.symbol or "").strip().upper() in kept]
        dropped = len(rows) - len(filtered)
        if dropped:
            logger.info(
                "[auto_arm] ross price-band gate: dropped %d non-small-cap equity "
                "candidate(s); kept %d in $%s-$%s band",
                dropped, len(filtered),
                EQUITY_ROSS_SMALLCAP.price_min, EQUITY_ROSS_SMALLCAP.price_max,
            )
        return filtered
    except Exception:
        logger.warning(
            "[auto_arm] ross price-band gate errored — failing safe (holding equity "
            "candidates this pass)", exc_info=True,
        )
        return []


def _liquidity_rerank(
    rows: list[MomentumSymbolViability],
) -> list[MomentumSymbolViability]:
    """Re-rank the (price-band-passed) equity candidates by a 50/50 blend of their
    VIABILITY rank and their DOLLAR-VOLUME rank, so the most FILLABLE high-quality
    Ross small-caps are armed first.

    The live spread gate blocks wide-spread entries, so a trigger on an illiquid name
    never fills (06-09: 13 clean triggers, 0 fills — all wide-spread-blocked). Dollar-
    volume is the cleanest selection-time liquidity proxy (the snapshot has no reliable
    ask); higher dollar-volume -> tighter, fillable spread. The spread sweep proved the
    payoff: 06-08 5m at liquid ~100bps = +$12,818 vs wide ~200bps = +$634. ADAPTIVE —
    a rank-blend WITHIN the batch, no fixed dollar-volume threshold (operator principle
    #1). FAIL-OPEN: any error / no liquidity data returns the rows unchanged (viability
    order), so a snapshot hiccup never blocks arming. docs/DESIGN/MOMENTUM_LANE.md"""
    if not _auto_arm_liquidity_bias() or len(rows) < 2:
        return rows
    try:
        from .universe import snapshot_dollar_volumes

        dvols = snapshot_dollar_volumes([r.symbol for r in rows])
        if not dvols:
            return rows  # no liquidity data -> keep viability order (fail-open)
        # rows already in viability order (desc) -> position = viability rank (0 best).
        vrank = {id(r): i for i, r in enumerate(rows)}
        by_dvol = sorted(
            rows, key=lambda r: dvols.get(str(r.symbol or "").strip().upper(), 0.0), reverse=True
        )
        drank = {id(r): i for i, r in enumerate(by_dvol)}
        reranked = sorted(rows, key=lambda r: vrank[id(r)] + drank[id(r)])
        if reranked and reranked[0] is not rows[0]:
            logger.info(
                "[auto_arm] liquidity-bias: armed-first now %s ($%.0fM dvol) over the "
                "viability-only top %s ($%.0fM) — preferring fillable",
                reranked[0].symbol,
                dvols.get(str(reranked[0].symbol or "").strip().upper(), 0.0) / 1e6,
                rows[0].symbol,
                dvols.get(str(rows[0].symbol or "").strip().upper(), 0.0) / 1e6,
            )
        return reranked
    except Exception:
        logger.debug("[auto_arm] liquidity-bias re-rank errored — viability order", exc_info=True)
        return rows


def _crypto_liquidity_rerank(
    rows: list[MomentumSymbolViability],
) -> list[MomentumSymbolViability]:
    """Crypto analog of :func:`_liquidity_rerank`: among the crypto (``-USD``)
    candidates that cleared the binary liquidity FLOOR (``crypto_liquidity_ok``),
    arm the most FILLABLE first by blending viability/ross rank with 24h
    quote-volume rank.

    The floor only gates pass/block — but among passers the thinnest can otherwise
    arm ahead of the deepest, and a trigger on a thin book pays the maker-fill /
    exit toxicity that drove the early crypto losses. Re-ranking by turnover lands
    the trigger on the deepest book. The 24h quote-$ datum already rides each
    viability row (``extra.ross_signals[sym].quote_volume_24h``) — zero new network
    call. ADAPTIVE rank-blend WITHIN the crypto batch (no fixed threshold, operator
    principle #1). FAIL-OPEN: missing data / <2 crypto names -> unchanged order.
    Non-crypto rows keep their ross-ranked positions (mixed-lane safe)."""
    if not _auto_arm_liquidity_bias():
        return rows
    crypto = [r for r in rows if str(r.symbol or "").strip().upper().endswith("-USD")]
    if len(crypto) < 2:
        return rows
    try:
        from .crypto_liquidity import _quote_volume_24h_for

        dvols: dict[int, float] = {}
        for r in crypto:
            qv = _quote_volume_24h_for(r, str(r.symbol or "").strip().upper())
            if qv is not None:
                dvols[id(r)] = qv
        if len(dvols) < 2:
            return rows  # not enough turnover data -> keep ross order (fail-open)
        # crypto is already in ross order -> position = ross/viability rank (0 best)
        vrank = {id(r): i for i, r in enumerate(crypto)}
        by_dvol = sorted(crypto, key=lambda r: dvols.get(id(r), 0.0), reverse=True)
        drank = {id(r): i for i, r in enumerate(by_dvol)}
        reranked = sorted(crypto, key=lambda r: vrank[id(r)] + drank[id(r)])
        if reranked[0] is not crypto[0]:
            logger.info(
                "[auto_arm] crypto liquidity-bias: armed-first now %s ($%.1fM 24h) over "
                "the ross-top %s ($%.1fM) — preferring fillable",
                reranked[0].symbol, dvols.get(id(reranked[0]), 0.0) / 1e6,
                crypto[0].symbol, dvols.get(id(crypto[0]), 0.0) / 1e6,
            )
        # splice the re-ranked crypto back into the crypto slots; equity untouched
        it = iter(reranked)
        return [
            next(it) if str(r.symbol or "").strip().upper().endswith("-USD") else r
            for r in rows
        ]
    except Exception:
        logger.debug("[auto_arm] crypto liquidity-bias errored — order unchanged", exc_info=True)
        return rows


_EPOCH = datetime(1970, 1, 1)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _adaptive_loss_cooldown_minutes(return_bps: float | None) -> float:
    """Post-loss cooldown minutes scaled by the LOSS MAGNITUDE the tape delivered
    (2026-06-16, Ross-discipline). A hard −892bps bailout must sit a name out far
    longer than a −50bps scratch — CCTG machine-gunned a 2nd −892bps loss 11min after
    a −159bps scratch, inside neither the fixed 5-min cooldown nor the 2-strike block.
    Base = the existing fixed knob; +1min per ``bps_per_min`` of realized loss; hard
    capped at ``max_base_mult``×base so a data glitch can never freeze a name for hours.
    Kill-switch off / missing magnitude / non-positive per_min → byte-identical fixed
    base (fail-open: thin data NEVER blocks longer)."""
    base = float(getattr(settings, "chili_momentum_symbol_loss_cooldown_min", 5.0) or 5.0)
    if not bool(getattr(settings, "chili_momentum_loss_cooldown_adaptive_enabled", True)):
        return base
    per_min = float(getattr(settings, "chili_momentum_loss_cooldown_bps_per_min", 500.0) or 0.0)
    if per_min <= 0.0 or return_bps is None:
        return base
    loss_bps = abs(float(return_bps))
    adaptive = base + loss_bps / per_min
    cap = base * float(getattr(settings, "chili_momentum_loss_cooldown_max_base_mult", 4.0) or 4.0)
    return min(adaptive, cap)


def _symbol_loss_guards(db: Session) -> tuple[set[str], dict[str, datetime]]:
    """Churn guards from TODAY's closed live outcomes (UTC day):

    - 2-STRIKE: symbols with >= ``chili_momentum_symbol_max_daily_stopouts``
      (default 2) losing live trades today are BLOCKED for the rest of the day —
      Ross walks away from a name that stopped him twice.
    - POST-LOSS COOLDOWN: after any losing live trade, the symbol cannot re-arm
      for ``chili_momentum_symbol_loss_cooldown_min`` (default 5) minutes — a
      tick-speed re-trigger into the same chop is how 1R losses machine-gun.

    Fail-open: any error returns no blocks (the daily-loss cap and drawdown
    breaker still bound the account)."""
    try:
        from ....models.trading import MomentumAutomationOutcome

        day_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = (
            db.query(
                MomentumAutomationOutcome.symbol,
                MomentumAutomationOutcome.terminal_at,
                MomentumAutomationOutcome.realized_pnl_usd,
                MomentumAutomationOutcome.return_bps,
                MomentumAutomationOutcome.execution_family,
            )
            .filter(
                MomentumAutomationOutcome.mode == "live",
                MomentumAutomationOutcome.terminal_at >= day_start,
                MomentumAutomationOutcome.realized_pnl_usd < 0,
            )
            .all()
        )
        max_stops = int(getattr(settings, "chili_momentum_symbol_max_daily_stopouts", 2) or 2)
        cd_min = float(getattr(settings, "chili_momentum_symbol_loss_cooldown_min", 5) or 5)
        counts: dict[str, int] = {}
        cooldown_until: dict[str, datetime] = {}
        for sym, t_at, _pnl, _bps, _ef in rows:
            s = str(sym).upper()
            counts[s] = counts.get(s, 0) + 1
            # EQUITY: the post-loss cooldown SCALES with the loss magnitude — a hard
            # bailout sits the name out far longer than a scratch (the CCTG re-entry).
            # CRYPTO: fixed base, BYTE-IDENTICAL — it re-arms fast by design and is
            # bounded by reap_cooldown below. The 2-strike day-block is unchanged for all.
            if str(_ef or "") in ("robinhood_spot", "alpaca_spot"):
                _mins = _adaptive_loss_cooldown_minutes(_bps)
            else:
                _mins = cd_min
            cd = (t_at if isinstance(t_at, datetime) else _utcnow()) + timedelta(minutes=_mins)
            if cd > cooldown_until.get(s, _EPOCH):
                cooldown_until[s] = cd
        blocked = {s for s, n in counts.items() if n >= max_stops}
        return blocked, cooldown_until
    except Exception:
        logger.debug("[auto_arm] loss-guard query failed (fail-open)", exc_info=True)
        return set(), {}


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
    """Replicate the live_runner WATCHING_LIVE hybrid trigger to find a name whose
    momentum is breaking NOW (pullback-break preferred, volume fallback).

    DUAL-PATH PARITY: the pullback-break branch evaluates the SAME settings-resolved
    Ross trigger the live + paper runners call (``momentum_pullback_trigger``), so the
    selection probe makes the IDENTICAL bar-level entry decision as the live runner —
    require_retest (deep_reclaim + dip-buy reachable), sustained-volume, candle, VWAP,
    MACD, runaway, verticality, and symbol-awareness (equity-only morning gate /
    premarket guards / crypto exemption). The probe is bar-only (no ``live_price``):
    that is BY DESIGN — it arms a WATCH and the live runner does the final tick-break
    confirmation before placing the order, so the probe should match the runner's
    BAR-level fire/wait reason exactly. ``CHILI_MOMENTUM_AUTO_ARM_TRIGGER_PARITY_ENABLED=0``
    reverts to the legacy library-defaults probe (require_retest=False → raw break,
    deep_reclaim unreachable). docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        from ..market_data import fetch_ohlcv_df
        from .entry_gates import (
            momentum_pullback_trigger,
            momentum_volume_confirmation,
            pullback_break_confirmation,
        )

        mode = str(getattr(settings, "chili_momentum_entry_trigger_mode", "hybrid") or "hybrid").lower()
        interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
        _parity = bool(getattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True))
        if mode in ("hybrid", "pullback_break"):
            df_pb = fetch_ohlcv_df(symbol, interval=interval, period="5d")
            if df_pb is not None and not getattr(df_pb, "empty", True):
                if _parity:
                    # The shared, settings-resolved trigger the live runner uses
                    # (symbol-aware, bar-level — no live_price, no halt-resume state).
                    ok, reason, _ = momentum_pullback_trigger(
                        df_pb, entry_interval=interval, symbol=symbol
                    )
                else:
                    # Legacy probe: raw library defaults (require_retest=False).
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


def _current_viability_scores(db: Session, symbols: set[str]) -> dict[str, float]:
    """Latest viability_score per symbol (keyed UPPER) from momentum_symbol_viability —
    the SAME source/freshness as the newcomer's board score, so rank-displacement compares
    like with like. A symbol absent from the table -> 0.0 (fell out of the universe =
    maximally displaceable). Read-only; fail-open to {}."""
    syms = {s for s in symbols if s}
    if not syms:
        return {}
    try:
        rows = (
            db.query(MomentumSymbolViability.symbol, MomentumSymbolViability.viability_score)
            .filter(MomentumSymbolViability.symbol.in_(tuple(syms)))
            .order_by(MomentumSymbolViability.symbol, MomentumSymbolViability.freshness_ts.desc())
            .all()
        )
    except Exception:
        return {}
    out: dict[str, float] = {}
    for sym, score in rows:
        su = str(sym or "").upper()
        if su and su not in out:  # ordered freshness desc -> first row per symbol is latest
            out[su] = float(score or 0.0)
    return out


def _symbols_with_inflight_entry(db: Session, *, user_id: int | None) -> set[str]:
    """Symbols (UPPER) with ANY live session past the inert pre-entry stage OR carrying a
    broker entry order — the PER-SYMBOL orphan veto for rank-displacement: never reap an
    inert twin of a symbol whose SIBLING session has an in-flight order (the CRVO/MTEN twin
    orphan). Fail-CLOSED: an unreadable snapshot vetoes that symbol."""
    out: set[str] = set()
    try:
        from .live_runner import _unresolved_entry_order_ids
    except Exception:
        _unresolved_entry_order_ids = None  # type: ignore
    try:
        q = db.query(
            TradingAutomationSession.symbol,
            TradingAutomationSession.state,
            TradingAutomationSession.risk_snapshot_json,
        ).filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(tuple(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY)),
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        rows = q.all()
    except Exception:
        return out
    for sym, state, snap in rows:
        su = str(sym or "").upper()
        if not su:
            continue
        if state not in _RANK_DISPLACE_REAPABLE_STATES:
            out.add(su)  # watching/candidate/pending/entered/... -> in-flight
            continue
        try:
            le = (snap or {}).get("momentum_live_execution") or {}
            _unres = _unresolved_entry_order_ids(le) if _unresolved_entry_order_ids else []
            if le.get("entry_submitted") or le.get("entry_order_id") or _unres:
                out.add(su)
        except Exception:
            out.add(su)  # cannot verify -> veto conservatively
    return out


def _guarded_reap_for_displacement(
    db: Session, *, user_id: int | None, session_id: int, expected_symbol: str
) -> bool:
    """Reap ONE inert pre-entry session to free a slot — SAFELY. Mirrors the live runner's
    row lock (with_for_update(nowait=True), live_runner.py:2283): if the runner holds the
    row (mid-tick, possibly submitting an order), the lock fails -> ABORT, never reap. Under
    the lock, re-verify the row is STILL inert + carries NO entry order (entry_submitted /
    entry_order_id / unresolved history) before cancelling, then writes the reap cooldown and
    COMMITS its own txn. Fail-CLOSED: any doubt -> rollback, no reap. True only on commit."""
    from .automation_query import cancel_automation_session
    from .live_runner import _unresolved_entry_order_ids

    try:
        q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.id == int(session_id)
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        locked = q.with_for_update(nowait=True).one_or_none()
    except Exception:
        # lock contention (runner mid-tick) or query error -> never reap on doubt
        try:
            db.rollback()
        except Exception:
            pass
        return False
    try:
        if locked is None:
            db.rollback()
            return False
        if locked.state not in _RANK_DISPLACE_REAPABLE_STATES:
            db.rollback()
            return False
        if str(locked.symbol or "").upper() != str(expected_symbol or "").upper():
            db.rollback()
            return False
        le: dict[str, Any] = {}
        try:
            _snap = locked.risk_snapshot_json or {}
            _le = _snap.get("momentum_live_execution") if isinstance(_snap, dict) else None
            le = _le if isinstance(_le, dict) else {}
        except Exception:
            le = {}
        if le.get("entry_submitted") or le.get("entry_order_id") or _unresolved_entry_order_ids(le):
            db.rollback()
            return False
        # Proven inert + orderless UNDER THE LOCK -> safe to cancel within this txn.
        res = cancel_automation_session(
            db, user_id=(int(user_id) if user_id is not None else None), session_id=int(session_id)
        )
        if not (isinstance(res, dict) and res.get("ok")):
            db.rollback()
            return False
        _write_reap_cooldown(str(expected_symbol or "").upper(), _utcnow())
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(
            "[auto_arm] guarded displacement reap failed session=%s", session_id, exc_info=True
        )
        return False


def _maybe_rank_displace(
    db: Session, *, user_id: int | None, newcomer: MomentumSymbolViability, busy_symbols: set[str]
) -> tuple[bool, dict[str, Any]]:
    """When arm slots are full, evict the worst-ranked INERT pre-entry watcher so a higher-
    ranked NEWCOMER can arm. Ranks victims by CURRENT viability score (same source as the
    newcomer). Guards: strict score margin, min-dwell (off updated_at), reap-cooldown,
    per-symbol in-flight veto, and a row-locked guarded reap. PARITY: returns (False, ...)
    without mutating any row when nothing qualifies."""
    margin_floor = float(getattr(settings, "chili_momentum_rank_displacement_margin", 0.02) or 0.0)
    min_dwell = float(getattr(settings, "chili_momentum_rank_displacement_min_dwell_sec", 45.0) or 0.0)
    now = _utcnow()
    nsym = str(newcomer.symbol or "").upper()
    nscore = float(newcomer.viability_score or 0.0)
    try:
        q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(tuple(_RANK_DISPLACE_REAPABLE_STATES)),
            TradingAutomationSession.execution_family != "alpaca_spot",  # never reap a paper twin
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        victims = q.all()  # .all() — NEVER .one_or_none() (would crash on the dupe-symbol rows)
    except Exception:
        return False, {"reason": "victim_query_failed"}
    if not victims:
        return False, {"reason": "no_reapable"}
    score_map = _current_viability_scores(db, {str(v.symbol or "") for v in victims})

    def _vscore(v) -> float:
        return float(score_map.get(str(v.symbol or "").upper(), 0.0))

    victims.sort(key=lambda v: (_vscore(v), v.updated_at or now))  # worst (lowest score) first
    inflight = _symbols_with_inflight_entry(db, user_id=user_id)
    for v in victims:
        vsym = str(v.symbol or "").upper()
        if not vsym or vsym == nsym:
            continue
        vscore = _vscore(v)
        if nscore - vscore < margin_floor:
            continue  # newcomer must STRICTLY beat by the margin (parity no-op otherwise)
        try:
            dwell = (now - (v.updated_at or v.started_at or now)).total_seconds()
        except Exception:
            dwell = 1e9
        if dwell < min_dwell:
            continue  # freshly-armed watcher — let it settle/fire before it can be bumped
        if _reap_cooldown_active(vsym, now):
            continue  # just churned/displaced — don't re-bump
        if vsym in inflight:
            continue  # PER-SYMBOL orphan veto: a sibling session holds an in-flight order
        if _guarded_reap_for_displacement(
            db, user_id=user_id, session_id=int(v.id), expected_symbol=v.symbol
        ):
            return True, {
                "reaped_session": int(v.id),
                "reaped_symbol": v.symbol,
                "reaped_score": round(vscore, 4),
                "newcomer": nsym,
                "newcomer_score": round(nscore, 4),
                "margin": round(nscore - vscore, 4),
            }
        # reap aborted (lock race / became non-inert) -> try the next-worst victim
    return False, {"reason": "no_displaceable"}


def _try_displacement_for_full_slots(db: Session, *, uid: int | None, out: dict[str, Any]) -> bool:
    """Slot-full hook: if rank-displacement is ON and no per-pass cancel has fired yet,
    pick the best fresh non-busy eligible NEWCOMER and try to displace the worst inert
    watcher to free a slot. Returns True iff a slot was freed. PARITY: flag OFF -> returns
    False immediately, touching nothing (byte-identical to skip-on-full)."""
    if not bool(getattr(settings, "chili_momentum_rank_displacement_enabled", True)):
        return False
    # Per-pass cancel budget shared with the stale-reaper (out['reaped']) — at most 1/pass.
    if out.get("reaped") or out.get("displaced"):
        return False
    try:
        cands = _fresh_live_eligible_candidates(db, limit=_scan_limit())
    except Exception:
        return False
    if not cands:
        return False
    try:
        busy = _symbols_with_active_live_session(db, user_id=uid)
    except Exception:
        return False
    newcomer = None
    for c in cands:
        su = str(c.symbol or "").upper()
        if not su or su in busy:
            continue
        if _auto_arm_crypto_only() and not _is_coinbase_tradeable_symbol(c.symbol):
            continue
        if _auto_arm_equity_only() and _is_coinbase_tradeable_symbol(c.symbol):
            continue
        if not _symbol_market_open(c.symbol):
            continue
        if _reap_cooldown_active(su, _utcnow()):
            continue
        newcomer = c
        break
    if newcomer is None:
        return False
    displaced, info = _maybe_rank_displace(db, user_id=uid, newcomer=newcomer, busy_symbols=busy)
    if displaced:
        out["displaced"] = info
        logger.warning(
            "[auto_arm] rank_displaced session=%s %s (score %.4f) for newcomer %s (score %.4f, "
            "margin %.4f) — freed a full slot for a higher-ranked mover",
            info.get("reaped_session"), info.get("reaped_symbol"), info.get("reaped_score", 0.0),
            info.get("newcomer"), info.get("newcomer_score", 0.0), info.get("margin", 0.0),
        )
        return True
    return False


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
        from ..governance import kill_switch_halts_new_entries

        # True-global halts (manual/emergency/price-monitor/aggregate-backstop) still
        # freeze the whole lane. A LEGACY single-global daily-loss breach is handled
        # PER BROKER in Guard 4 below (so a Coinbase-sized cap can't freeze Robinhood).
        if kill_switch_halts_new_entries():
            out["skipped"] = "kill_switch"
            return out
    except Exception:
        pass

    # Reap stale pre-entry sessions FIRST so a faded leftover (e.g. a name armed
    # long ago whose intraday move never triggered) does not pin the only slot.
    reaped = _reap_stale_watching_sessions(db, user_id=uid, now=datetime.utcnow())
    try:
        _finalized = _finalize_stale_exited_sessions(db, user_id=uid, now=datetime.utcnow())
        if _finalized:
            out["finalized_exited"] = _finalized
    except Exception:
        logger.debug("[auto_arm] finalize sweep failed", exc_info=True)
    if reaped:
        out["reaped"] = reaped
        db.commit()

    # Guard 2: concurrency. Two regimes, selected by the master flag.
    if getattr(settings, "chili_momentum_decouple_watching_enabled", False):
        # DECOUPLED: watchers fan out to the top-N funnel cap (a $0-risk watcher no
        # longer eats a real slot); only HELD positions charge the risk-budget cap.
        # Both checks here are SOFT pre-checks (don't bother arming an 11th watcher
        # into a full book) — the AUTHORITATIVE position cap is the advisory-locked
        # fill boundary in live_runner (a soft re-count cannot be atomic at arm time).
        from .risk_evaluator import count_open_positions as _count_open_positions
        from .risk_policy import effective_position_cap as _effective_position_cap

        _fanout = int(getattr(settings, "chili_momentum_watch_fanout_max", 15) or 15)
        _watch_ct = _count_watching_prefill(db, user_id=uid)
        if _watch_ct >= _fanout:
            # RANK-DISPLACEMENT: rather than skip, try to evict the worst inert watcher so
            # a higher-ranked newcomer can take the slot. Parity: flag-off -> byte-identical.
            if not _try_displacement_for_full_slots(db, uid=uid, out=out):
                out["skipped"] = "watch_fanout_full"
                out["watching"] = _watch_ct
                return out
            _watch_ct = _count_watching_prefill(db, user_id=uid)
            if _watch_ct >= _fanout:
                out["skipped"] = "watch_fanout_full"
                out["watching"] = _watch_ct
                return out
        try:
            _pos_ct = _count_open_positions(db, user_id=int(uid), mode="live")
            if _pos_ct >= _effective_position_cap(crypto=False):
                out["skipped"] = "position_cap"
                out["open_positions"] = _pos_ct
                return out
        except Exception:
            logger.debug("[auto_arm] decoupled position pre-check failed", exc_info=True)
    else:
        # LEGACY single-cap path — byte-identical to pre-decouple behaviour.
        active = _active_live_session_count(db, user_id=uid)
        if active >= _max_live_sessions():
            # RANK-DISPLACEMENT (legacy single-cap path): same as the decoupled path above.
            if not _try_displacement_for_full_slots(db, uid=uid, out=out):
                out["skipped"] = "live_session_active"
                out["active"] = active
                return out
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
        if bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True)):
            # PER-BROKER: the lane's daily-loss cap is THIS broker's own budget
            # (off its real equity), not an all-families sum vs a single cap. A
            # breach blocks only this broker's arming; the other broker keeps trading.
            from ..governance import broker_daily_loss_breached

            _fam = _lane_execution_family()
            _breached, _info = broker_daily_loss_breached(db, _fam, user_id=int(uid))
            if _breached:
                out["skipped"] = "daily_loss_cap_broker"
                out["blocked_broker"] = _info.get("family")
                out["daily_pnl_usd"] = round(float(_info.get("realized", 0.0) or 0.0), 2)
                out["max_daily_loss_usd"] = round(float(_info.get("cap", 0.0) or 0.0), 2)
                return out
        else:
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

    # Guard 5b: green-to-red session breaker (Ross gap #8). Stricter complement of the
    # giveback halt — once the day PEAKED green above a small equity-relative activation
    # and current realized PnL has round-tripped to <= $0, STOP arming for the session
    # (the giveback's above-$0 floor misses a true round-trip into the red). Fail-open.
    try:
        from .risk_evaluator import evaluate_green_to_red_halt

        _g2r = evaluate_green_to_red_halt(
            db, user_id=int(uid), execution_family=_lane_execution_family()
        )
        if _g2r.get("halted"):
            out["skipped"] = "green_to_red"
            out["daily_pnl_usd"] = _g2r.get("daily_pnl_usd")
            out["peak_pnl_usd"] = _g2r.get("peak_pnl_usd")
            return out
    except Exception:
        pass

    # Clear expired pending arms so they do not pin a concurrency slot.
    try:
        from .automation_query import expire_stale_live_arm_sessions

        expire_stale_live_arm_sessions(db, user_id=int(uid))
    except Exception:
        pass

    # Coinbase connect at PASS START (2026-06-12): the venue-readiness filter
    # at selection ran BEFORE the lazy _cb_connect() at the arm phase, so a
    # fresh scheduler process dropped every crypto candidate as
    # broker_not_ready and never reached the code that would have connected —
    # the chicken-and-egg that kept the night lane empty. connect() is cached/
    # idempotent; failures fall through to the readiness filter as before.
    try:
        from ...coinbase_service import connect as _cb_connect_early

        _cb_connect_early()
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
    # SHAKE-OUT churn guards (tick-speed entries can re-trigger within seconds of a
    # stop-out): (a) 2-strike rule — a symbol that stopped us out twice TODAY is
    # done for the day (Ross's own discipline); (b) post-loss cooldown — after any
    # loss on a symbol, sit out a few minutes before re-arming it so a chop doesn't
    # machine-gun 1R losses on one name. Both fail-open on query errors.
    loss_blocked, loss_cooldown_until = _symbol_loss_guards(db)
    out["busy_skipped"] = 0
    out["broker_not_ready_skipped"] = 0
    out["loss_guard_skipped"] = 0
    _broker_ready_cache: dict[str, bool] = {}
    out["crypto_illiquid_skipped"] = 0
    out["reap_cooldown_skipped"] = 0
    eligible: list[MomentumSymbolViability] = []
    for c in candidates:
        out["checked"] += 1
        if _auto_arm_crypto_only() and not _is_coinbase_tradeable_symbol(c.symbol):
            continue  # defensive: never arm an equity via the coinbase_spot lane
        if _auto_arm_equity_only() and _is_coinbase_tradeable_symbol(c.symbol):
            continue  # equity-only focus: never arm crypto in the Ross lane
        # (crypto live-arm gates apply at the LIVE pick stage below, NOT here —
        # filtering the eligible list would also starve the PAPER shadow arms,
        # which must keep learning crypto 24/7.)
        if c.symbol.upper() in busy_symbols:
            out["busy_skipped"] += 1
            continue  # already have a live session for this symbol — rotate to the next setup
        _sym_u = c.symbol.upper()
        if _sym_u in loss_blocked or _utcnow() < loss_cooldown_until.get(_sym_u, _EPOCH):
            out["loss_guard_skipped"] += 1
            continue  # 2-strike / post-loss cooldown — walk away like Ross does
        if _reap_cooldown_active(_sym_u, _utcnow()):
            out["reap_cooldown_skipped"] += 1
            continue  # just churned/displaced the slot without firing — let a different mover watch
        if not _symbol_market_open(c.symbol):
            continue  # equities only during their session; crypto always passes (24/7)
        # Crypto liquidity floor (A1): the Ross scorer is blind to executability,
        # so it ranks $24k/24h names alongside DOGE. Block crypto pairs whose
        # turnover can't absorb a trade — applies to PAPER too, since the whole
        # point of the soak is to learn on EXECUTABLE names. Cheap ($-volume
        # from the already-loaded viability row; no network). The live spread
        # probe runs later at the arm stage. Stash the per-name notional cap.
        if _is_coinbase_tradeable_symbol(c.symbol):
            _liq_ok, _liq_detail, _liq_cap = crypto_liquidity_ok(c.symbol, c, adapter=None)
            if not _liq_ok:
                out["crypto_illiquid_skipped"] = out.get("crypto_illiquid_skipped", 0) + 1
                continue
        if not _venue_broker_ready_for(c.symbol, _broker_ready_cache):
            out["broker_not_ready_skipped"] += 1
            continue  # venue disconnected (e.g. RH token expired) — don't burn the single
            # per-pass arm on a name whose confirm will fail; fall through to a fillable venue
        if not _symbol_free(db, c.symbol, uid):
            continue
        eligible.append(c)

    # Release the read transaction BEFORE the network-bound probe phase below. The probes
    # (OHLCV fetches) don't touch the DB, but the still-open read txn — which includes the
    # trading_automation_sessions SELECT from _symbols_with_active_live_session above —
    # would otherwise sit idle-in-transaction across the multi-second probe wave, long
    # enough for the per-connection idle-in-transaction timeout to kill the connection
    # (the server-closed-connection bursts seen during high-candidate pre-market passes).
    # Detach the loaded candidate rows first so their already-loaded symbol/variant_id/
    # viability_score stay usable without a lazy reload (expire_on_commit defaults True);
    # begin/confirm_live_arm re-open their own txns. (#561/#563 read-release pattern.)
    try:
        db.expunge_all()
        db.rollback()
    except Exception:
        logger.debug("[auto_arm] read-txn release before probe failed", exc_info=True)

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
        _budget = _probe_time_budget()
        _results: dict[str, tuple[bool, str, Any]] = {}
        _ex = concurrent.futures.ThreadPoolExecutor(max_workers=_workers)
        try:
            _futs = {_ex.submit(_probe_candidate, c.symbol): c.symbol for c in eligible}
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
            for c in eligible:
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
        def _live_armable(_c) -> bool:
            """Live-pick gates that must NOT starve the paper shadow list:
            crypto pauses during the US equity session, and live crypto arming
            stays off entirely while the realized record is 0/17 (A4)."""
            if not _is_coinbase_tradeable_symbol(_c.symbol):
                # A2 schedule (quant pass v2): no NEW equity arms in the late
                # window (>=14:30 ET) — freed-slot signals there lose money
                # (−$169/−$322 buckets); exits/management unaffected.
                try:
                    from .market_profile import schedule_window_now

                    if schedule_window_now() == "late":
                        out["late_window_skipped"] = out.get("late_window_skipped", 0) + 1
                        return False
                except Exception:
                    pass
                return True
            if not bool(getattr(settings, "chili_momentum_crypto_live_arm_enabled", False)):
                out["crypto_live_disabled_skipped"] = out.get("crypto_live_disabled_skipped", 0) + 1
                return False
            if _crypto_paused_us_session():
                out["crypto_us_session_skipped"] = out.get("crypto_us_session_skipped", 0) + 1
                return False
            # A5 crypto clock: no NEW crypto entries in the 21:00–05:00 UTC dead
            # band (0/21 earned there). Exits/management unaffected.
            try:
                from .market_profile import crypto_schedule_enabled, crypto_session_active_now

                if crypto_schedule_enabled() and not crypto_session_active_now():
                    out["crypto_clock_skipped"] = out.get("crypto_clock_skipped", 0) + 1
                    return False
            except Exception:
                pass
            return True

        _watch = []
        if _firing:
            chosen = next((c for c in _firing if _live_armable(c)), None)
            if chosen is not None:
                chosen_reason = _r(chosen)[1]
        if chosen is None and _require_fresh_impulse():
            _watch = sorted(
                (c for c in eligible if _known_fresh(_r(c)[2])),
                key=lambda c: _freshness_rank(_r(c)[2]),
                reverse=True,
            )
            out["faded_skipped"] = len(eligible) - len(_watch)
            _w_ok = next((c for c in _watch if _live_armable(c)), None)
            if _w_ok is not None:
                chosen = _w_ok
                chosen_reason = "fresh_watch:" + str(_r(chosen)[1])
        # A6: the freshest distinct armable candidates after the primary — the
        # arm loop below spends up to max_arms_per_pass on them (open-burst
        # bandwidth; each still passes begin/confirm risk gates individually).
        _more_picks = []
        if chosen is not None:
            _seen_syms = {chosen.symbol}
            for _c in list(_firing) + list(_watch):
                if _c.symbol in _seen_syms or not _live_armable(_c):
                    continue
                _seen_syms.add(_c.symbol)
                _is_fire = any(_c is _f for _f in _firing)
                _more_picks.append((_c, _r(_c)[1] if _is_fire else "fresh_watch:" + str(_r(_c)[1])))
        if chosen is not None:
            _cf = _r(chosen)[2]
            out["chosen_fresh_score"] = (
                round(float(getattr(_cf, "score", 0.0) or 0.0), 4) if _cf is not None else None
            )
            out["chosen_firing"] = bool(_firing)

    # Paper shadow mass: the probed eligibles that lose the live rank race still
    # carry information — run them all in paper (free outcome data, zero risk).
    try:
        out["paper_shadow_armed"] = _paper_shadow_arm(
            db, uid=int(uid), candidates=list(eligible or []),
            exclude_symbol=(chosen.symbol if chosen is not None else None),
        )
    except Exception:
        logger.debug("[auto_arm] paper shadow pass failed", exc_info=True)

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

    # A6: spend up to max_arms_per_pass on distinct fresh candidates — the
    # open burst offers far more simultaneous setups than one arm per 30s pass
    # can take (74 fresh vs 6 armed in the 13:30-13:50Z window). Every pick
    # still passes begin/confirm risk gates individually.
    _max_arms = max(1, int(getattr(settings, "chili_momentum_auto_arm_max_arms_per_pass", 3) or 1))
    _picks = [(chosen, chosen_reason)] + list(_more_picks)
    out["armed"] = 0
    _armed_syms: list[str] = []
    for chosen, chosen_reason in _picks:
        if out["armed"] >= _max_arms:
            break
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
            continue

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
            continue

        confirm = confirm_live_arm(
            db, user_id=int(uid), arm_token=begin.get("arm_token"), confirm=True
        )
        if confirm.get("ok"):
            out["armed"] += 1
            _armed_syms.append(chosen.symbol)
            out["session_id"] = begin.get("session_id")
            out["state"] = confirm.get("state")
            logger.warning(
                "[auto_arm] ARMED live %s session=%s state=%s trigger=%s viability=%.3f",
                chosen.symbol, begin.get("session_id"), confirm.get("state"),
                chosen_reason, float(chosen.viability_score or 0.0),
            )
            # ALPACA TWIN SOAK (2026-06-12, docs/DESIGN/ALPACA_LANE.md "same-name
            # A/B"): every EQUITY name armed live on Robinhood also arms a TWIN
            # session on alpaca_spot — the live runner drives a REAL order
            # lifecycle against Alpaca's PAPER endpoint (fake money) on the same
            # symbol, same triggers, same session. The fill-quality diff between
            # the twins is the evidence that decides the venue migration. The
            # venue-aware arm dedupe was built for exactly this; fake-money
            # outcomes/risk are excluded from real accounting (governance +
            # aggregate-risk filters). Best-effort: a twin failure never affects
            # the primary arm.
            try:
                if (
                    bool(getattr(settings, "chili_momentum_alpaca_twin_arm_enabled", True))
                    and _exec_family in ("robinhood_spot", "coinbase_spot")
                    and bool(getattr(settings, "chili_alpaca_enabled", False))
                    and bool(getattr(settings, "chili_alpaca_paper", True))
                    and str(getattr(settings, "chili_alpaca_api_key", "") or "")
                    # crypto twin only for pairs Alpaca actually lists (majors —
                    # the lane's exotic low-cap alts mostly aren't there); equities
                    # are probed too (cheap, cached) so delisted names skip cleanly
                    and _alpaca_lists_symbol(chosen.symbol)
                ):
                    _tb = begin_live_arm(
                        db, user_id=int(uid), symbol=chosen.symbol,
                        variant_id=int(chosen.variant_id), execution_family="alpaca_spot",
                    )
                    if _tb.get("ok") and not _tb.get("deduped"):
                        _tc = confirm_live_arm(
                            db, user_id=int(uid), arm_token=_tb.get("arm_token"), confirm=True
                        )
                        if _tc.get("ok"):
                            out["alpaca_twin_session_id"] = _tb.get("session_id")
                            logger.info(
                                "[auto_arm] alpaca twin armed %s session=%s (paper endpoint)",
                                chosen.symbol, _tb.get("session_id"),
                            )
            except Exception:
                logger.debug("[auto_arm] alpaca twin arm failed", exc_info=True)
        else:
            out["skipped"] = "confirm_blocked"
            out["confirm_error"] = confirm.get("error")
            logger.info(
                "[auto_arm] confirm_live_arm blocked %s: %s",
                chosen.symbol, confirm.get("error"),
            )
    if _armed_syms:
        out["armed_symbols"] = _armed_syms
        out.pop("skipped", None)
        out.pop("begin_error", None)
        out.pop("confirm_error", None)
    return out
