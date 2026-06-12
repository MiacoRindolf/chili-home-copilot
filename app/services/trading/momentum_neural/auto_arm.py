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
    crypto-only -> Coinbase; else (equity-only or mixed) -> Robinhood (the equity lane).
    Fixes the daily-loss / giveback breakers being computed against the SMALL crypto
    equity — which made them trip on tiny losses and never grow with the (much larger)
    equities account. docs/DESIGN/MOMENTUM_LANE.md [[feedback_adaptive_no_magic]]"""
    from ..execution_family_registry import (
        EXECUTION_FAMILY_COINBASE_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
    )

    return EXECUTION_FAMILY_COINBASE_SPOT if _auto_arm_crypto_only() else EXECUTION_FAMILY_ROBINHOOD_SPOT


def _is_coinbase_tradeable_symbol(symbol: str) -> bool:
    """The momentum live lane trades via coinbase_spot. Coinbase crypto pairs use
    the ``-USD`` / ``-USDC`` convention; equities (ARKK, CLSK) are bare tickers. So
    a ``-USD`` substring distinguishes a crypto pair the venue can actually trade
    from an equity that would fail at order time (esp. once US market opens)."""
    return "-USD" in str(symbol or "").upper()


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

    armed = 0
    _excl = str(exclude_symbol or "").upper()
    for c in candidates:
        if budget <= 0:
            break
        sym = str(getattr(c, "symbol", "") or "").upper()
        if not sym or sym == _excl:
            continue
        try:
            res = create_paper_draft_session(
                db,
                user_id=int(uid),
                symbol=sym,
                variant_id=int(getattr(c, "variant_id", 0) or 0),
                execution_family=resolve_execution_family_for_symbol(sym),
            )
        except Exception:
            logger.debug("[auto_arm] paper shadow arm failed for %s", sym, exc_info=True)
            continue
        if res.get("ok") and not res.get("deduped"):
            armed += 1
            budget -= 1
    return armed


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
    if _auto_arm_equity_only() and rows:
        rows = _enforce_ross_price_band(rows)
        rows = _liquidity_rerank(rows)
    return _dedupe_by_symbol(rows, limit=int(limit))


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


_EPOCH = datetime(1970, 1, 1)


def _utcnow() -> datetime:
    return datetime.utcnow()


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
        for sym, t_at, _pnl in rows:
            s = str(sym).upper()
            counts[s] = counts.get(s, 0) + 1
            cd = (t_at if isinstance(t_at, datetime) else _utcnow()) + timedelta(minutes=cd_min)
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
    eligible: list[MomentumSymbolViability] = []
    for c in candidates:
        out["checked"] += 1
        if _auto_arm_crypto_only() and not _is_coinbase_tradeable_symbol(c.symbol):
            continue  # defensive: never arm an equity via the coinbase_spot lane
        if _auto_arm_equity_only() and _is_coinbase_tradeable_symbol(c.symbol):
            continue  # equity-only focus: never arm crypto in the Ross lane
        if c.symbol.upper() in busy_symbols:
            out["busy_skipped"] += 1
            continue  # already have a live session for this symbol — rotate to the next setup
        _sym_u = c.symbol.upper()
        if _sym_u in loss_blocked or _utcnow() < loss_cooldown_until.get(_sym_u, _EPOCH):
            out["loss_guard_skipped"] += 1
            continue  # 2-strike / post-loss cooldown — walk away like Ross does
        if not _symbol_market_open(c.symbol):
            continue  # equities only during their session; crypto always passes (24/7)
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
