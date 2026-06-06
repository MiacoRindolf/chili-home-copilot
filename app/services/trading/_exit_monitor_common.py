"""Shared helpers for the three exit-monitor lanes (equity / crypto / options).

Before the 2026-05-06 options fix the equity and crypto lanes each kept a
local copy of the ``latest_monitor_decisions_by_trade`` +
``fresh_monitor_exit_meta`` helpers. Subtle drift between the copies
(crypto used ``_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS``; equity used
``_MONITOR_EXIT_NOW_MAX_AGE_HOURS``) made the next consumer's job
ambiguous.

This module is the single source of truth. All three lanes (equity in
``auto_trader_monitor.py``, crypto in ``crypto/exit_monitor.py``,
options in ``options/exit_monitor.py``) import from here -- no local
copies, no per-lane fork of the freshness window.

Why not a class: each helper is a pure function with no shared state.
A module-level function plus a single constant is simpler than a
ExitMonitorCommon class with two methods.

f-exit-monitor-quote-guard-unification (2026-05-06): added the
``is_implausible_quote`` and ``should_consult_monitor_after_refusal``
helpers. The 0.1x / 10x bounds were previously inlined in crypto's and
options' ``_evaluate_exit_triggers``; they're relocated here as
documented module-level constants. Same values, one home, structural
(not strategy-tuning) constants.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from ...models.trading import PatternMonitorDecision


# ── Freshness window ───────────────────────────────────────────────────
#
# An ``exit_now`` recommendation older than this is treated as stale and
# does not trigger an exit. 96h is the value the equity lane has run with
# since 2026-04 and the crypto lane adopted on 2026-05-06; options lane
# inherits from this module on 2026-05-06.
#
# If a future asset class needs a tighter window (e.g., short-dated
# options where a 96h-old advisory is materially stale), introduce a
# per-asset override at the call site rather than splitting the module.
MONITOR_EXIT_NOW_MAX_AGE_HOURS = 96.0


# ── Implausibility bounds ──────────────────────────────────────────────
#
# Bounds for the ``observed_price / entry_price`` ratio. These are
# STRUCTURAL constants (data-feed-trust boundary), not strategy tuning
# parameters:
#
#   ratio < 0.1   -- quote is < 10% of entry. A stock dropping 90%
#                    intraday is almost certainly a data feed error;
#                    a real corporate action would carry a separate
#                    adjustment signal.
#   ratio > 10.0  -- quote is > 10x entry. Same reasoning, opposite
#                    direction. A 10x intraday move is essentially
#                    impossible without a stock split / decimal-place
#                    misread at the source.
#
# Per-ticker derivation from historical volatility is a future
# enhancement (see ``f-implausible-quote-per-ticker-vol`` open question
# in the unification CC report); not env-tunable today.
IMPLAUSIBLE_QUOTE_RATIO_LOW: float = 0.1
IMPLAUSIBLE_QUOTE_RATIO_HIGH: float = 10.0


def is_implausible_quote(px: float | None, entry: float | None) -> bool:
    """True iff the observed quote vs entry implies a data-feed error.

    Returns False (not refused) when ``entry`` is zero/negative/None or
    ``px`` is zero/negative/None -- the caller is responsible for
    handling the no-anchor / no-px cases before reaching this helper
    (each lane has its own no-quote / no-entry skip path with
    different semantics).
    """
    if not entry or entry <= 0:
        return False
    if not px or px <= 0:
        return False
    ratio = px / entry
    return (
        ratio < IMPLAUSIBLE_QUOTE_RATIO_LOW
        or ratio > IMPLAUSIBLE_QUOTE_RATIO_HIGH
    )


def should_consult_monitor_after_refusal(
    reason: str | None,
    abstained_implausible: bool = False,
) -> bool:
    """True iff the lane should consult the LLM advisory after a no-go.

    Returns ``False`` (do NOT consult) iff EITHER:

      * ``reason`` starts with ``no_trigger:implausible_quote``
        (crypto's prefix-match contract -- crypto's
        ``_evaluate_exit_triggers`` carries the refusal in the string
        portion of its ``(bool, str)`` return), OR
      * ``abstained_implausible`` is ``True`` (options' boolean flag --
        options' ``_evaluate_exit_triggers`` returns
        ``(reason, abstained_implausible)``).

    Both signals indicate the lane refused to trust its own price feed
    for this trade. Acting on a different (LLM/monitor) feed when our
    own is suspect is a foot-gun; abstain.

    Other "no exit" reasons (``no_trigger`` for "no stop/target hit",
    ``no_quote`` for "px=0") are NOT data-quality refusals -- the LLM
    is the secondary signal in those cases and consultation IS
    permitted. Returns ``True`` for those.
    """
    if abstained_implausible:
        return False
    if isinstance(reason, str) and reason.startswith("no_trigger:implausible_quote"):
        return False
    return True


def latest_monitor_decisions_by_trade(
    db: "Session",
    trade_ids: list[int],
) -> dict[int, "PatternMonitorDecision"]:
    """Latest ``PatternMonitorDecision`` per trade (most recent wins).

    Execution should follow the newest advisory state only. If a prior
    ``exit_now`` has since been superseded by ``hold``, the live monitor
    must not keep selling from the stale recommendation.

    Returns a dict keyed by trade_id. Missing trade_ids (no decision in
    the table) are absent from the dict; callers should treat absence
    as "no advisory."
    """
    from ...models.trading import PatternMonitorDecision

    if not trade_ids:
        return {}
    rows = (
        db.query(PatternMonitorDecision)
        .filter(PatternMonitorDecision.trade_id.in_(trade_ids))
        .order_by(PatternMonitorDecision.created_at.desc())
        .all()
    )
    latest: dict[int, PatternMonitorDecision] = {}
    for row in rows:
        latest.setdefault(int(row.trade_id), row)
    return latest


def fresh_monitor_exit_meta(
    decision: "PatternMonitorDecision | None",
) -> dict[str, Any] | None:
    """Audit metadata when the latest monitor decision still means exit.

    Returns ``None`` when:
      * ``decision`` is None (no advisory for this trade)
      * the decision's action isn't ``exit_now``
      * the decision is older than ``MONITOR_EXIT_NOW_MAX_AGE_HOURS``

    When the lane chooses to exit on this advisory, the returned dict
    becomes the audit log entry. Audit detail belongs in the log line,
    NOT in the 50-char ``pending_exit_reason`` column.
    """
    if decision is None or (decision.action or "").lower() != "exit_now":
        return None
    age_h = (datetime.utcnow() - decision.created_at).total_seconds() / 3600.0
    if age_h > MONITOR_EXIT_NOW_MAX_AGE_HOURS:
        return None
    return {
        "decision_id": int(decision.id),
        "decision_source": decision.decision_source,
        "decision_age_hours": round(age_h, 3),
        "decision_price": (
            float(decision.price_at_decision)
            if decision.price_at_decision is not None
            else None
        ),
    }


# ── Fix 5B: exit_now corroboration + reroute-to-tighten ────────────────
#
# The pattern monitor's ``exit_now`` advisory is beneficial only ~21% of the time
# (measured via PatternMonitorDecision.was_beneficial; the ``heuristic`` source is
# 0/296), whereas ``tighten_stop`` is ~69% beneficial. So a fresh but
# UNCORROBORATED exit_now (price has not yet deteriorated toward the stop) is
# rerouted to a stop-tighten instead of a hard market exit, and provably
# non-beneficial sources are dropped entirely. This NEVER loosens a stop, never
# moves it to/beyond the current price, and leaves the hard stop + drawdown
# breaker fully intact.


def _denylisted_exit_sources() -> "frozenset[str]":
    try:
        from ...config import settings

        raw = getattr(settings, "chili_monitor_exit_denylisted_sources", "") or ""
    except Exception:
        raw = ""
    return frozenset(s.strip().lower() for s in str(raw).split(",") if s.strip())


def _corroboration_floor() -> float:
    try:
        from ...config import settings

        return float(getattr(settings, "chili_monitor_exit_corroboration_floor", 0.5))
    except Exception:
        return 0.5


def resolve_monitor_exit_action(
    decision: "PatternMonitorDecision | None",
    *,
    entry: float | None,
    stop: float | None,
    current_px: float | None,
    is_long: bool,
    corroboration_floor: float | None = None,
    denylisted_sources: "frozenset[str] | None" = None,
) -> tuple[str, float | None, dict[str, Any] | None]:
    """Decide how an exit lane should act on a pattern-monitor exit_now advisory.

    Returns ``(verdict, new_stop, meta)``:
      * ``("exit", None, meta)``       -- honor exit_now as a hard market exit
                                          (price corroborates, or geometry is
                                          unavailable so prior behavior is
                                          preserved). ``meta`` is the audit dict.
      * ``("tighten_stop", px, None)`` -- reroute: tighten the protective stop to
                                          ``px`` (the corroboration level) instead
                                          of cutting. Keeps upside, caps downside.
      * ``("hold", None, None)``       -- do nothing (no fresh exit_now; a
                                          denylisted source; or the stop is already
                                          trailed into profit so the trailing stop
                                          governs).

    Invariants: any returned ``new_stop`` is strictly tighter than the current
    stop and strictly inside the current price. The hard stop + drawdown breaker
    are unaffected.
    """
    meta = fresh_monitor_exit_meta(decision)
    if meta is None:
        return ("hold", None, None)
    if denylisted_sources is None:
        denylisted_sources = _denylisted_exit_sources()
    src = (getattr(decision, "decision_source", "") or "").strip().lower()
    if src in denylisted_sources:
        return ("hold", None, None)
    if corroboration_floor is None:
        corroboration_floor = _corroboration_floor()
    # Geometry is needed for corroboration; if any is missing/degenerate, preserve
    # the prior behavior (honor the advisory) rather than silently swallow an exit.
    try:
        e = float(entry)  # type: ignore[arg-type]
        s = float(stop)  # type: ignore[arg-type]
        px = float(current_px)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ("exit", None, meta)
    if e <= 0 or s <= 0 or px <= 0:
        return ("exit", None, meta)
    risk = (e - s) if is_long else (s - e)
    if risk <= 0:
        # The stop is already on the profit side of entry (trailed into a gain):
        # the trailing stop already protects the position, so an uncorroborated
        # exit_now should not force a premature take-profit. Let the stop govern.
        return ("hold", None, None)
    adverse = (e - px) if is_long else (px - e)
    if (adverse / risk) >= corroboration_floor:
        return ("exit", None, meta)  # price corroborates the exit -> cut now
    # Not corroborated -> reroute to a tighter stop at the corroboration level.
    new_stop = (
        (e - corroboration_floor * risk)
        if is_long
        else (e + corroboration_floor * risk)
    )
    if is_long:
        if not (s < new_stop < px):
            return ("hold", None, None)
    else:
        if not (px < new_stop < s):
            return ("hold", None, None)
    return ("tighten_stop", new_stop, meta)


def apply_monitor_exit_reroute_tighten(
    db: "Session",
    trade: Any,
    *,
    new_stop: float,
    decision_meta: dict[str, Any] | None = None,
) -> bool:
    """Tighten ``trade.stop_loss`` to ``new_stop`` instead of cutting the position.

    Only ever tightens (long -> raise the stop; short -> lower it); refuses to
    loosen. Does NOT create a pending exit. Returns True if the stop moved.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        ns = float(new_stop)
    except (TypeError, ValueError):
        return False
    if ns <= 0:
        return False
    is_long = (getattr(trade, "direction", "long") or "long").lower() != "short"
    cur = getattr(trade, "stop_loss", None)
    if cur is not None:
        cur = float(cur)
        if is_long and ns <= cur:
            return False
        if (not is_long) and ns >= cur:
            return False
    trade.stop_loss = ns
    db.add(trade)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return False
    logger.info(
        "[monitor_exit_reroute] trade=%s ticker=%s exit_now rerouted to stop-tighten: "
        "stop %s -> %.8f (decision=%s)",
        getattr(trade, "id", None),
        getattr(trade, "ticker", None),
        (round(cur, 8) if cur is not None else None),
        ns,
        (decision_meta or {}).get("decision_id"),
    )
    return True


__all__ = [
    "MONITOR_EXIT_NOW_MAX_AGE_HOURS",
    "IMPLAUSIBLE_QUOTE_RATIO_LOW",
    "IMPLAUSIBLE_QUOTE_RATIO_HIGH",
    "latest_monitor_decisions_by_trade",
    "fresh_monitor_exit_meta",
    "is_implausible_quote",
    "should_consult_monitor_after_refusal",
    "resolve_monitor_exit_action",
    "apply_monitor_exit_reroute_tighten",
]
