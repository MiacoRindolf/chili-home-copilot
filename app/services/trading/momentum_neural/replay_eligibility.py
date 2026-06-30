"""Replay v3 P2 — the ELIGIBILITY REPLAYER (reproduce the ``live_eligible`` flicker as-of t).

``MomentumSymbolViability`` carries ``live_eligible`` as a SINGLE mutable column with a
UNIQUE(symbol, variant_id) constraint (``models/trading.py:1577``) — it is a current-state
SNAPSHOT, not a time-series. There is no ``momentum_viability_history`` table, so the
eligibility TIMELINE that produced the UPC TOCTOU flicker (eligible at confirm → NOT-eligible
at the entry instant) is NOT directly recorded as a column history (design R1).

This module RECONSTRUCTS that timeline and WRITES ``live_eligible`` + ``freshness_ts`` onto
the single viability row AS-OF each grid instant ``t``, immediately before the unchanged
``tick_live_session`` reads it (``live_runner.py:5168``) and the gate evaluates it
(``risk_evaluator.py:841``). The runner therefore sees the as-of-``t`` eligibility state —
the FLICKER — and the REAL recency-grace decides whether to tolerate it.

THREE-TIER reconstruction (the chosen tier is explicit + logged):

  * **TIER A — recompute-via-scorer** (highest fidelity): run the SAME viability scorer
    ``ross_momentum.score_universe`` the live pipeline uses, over the recorded inputs as-of
    ``t``, and write its ``live_eligible`` verdict. Regenerates the TRUE flicker because it
    uses the same scoring logic over the recorded tape. (Read-only import of the scorer; this
    module never modifies it.) Reserved for P3/P4 real-data replay — needs the recorded
    universe-snapshot inputs as-of t, which P2's synthetic test does not stand up.
  * **TIER B — event-derived**: stitch a step-function of eligibility over time from the
    recorded ``trading_automation_events`` — the runner logs ``live_eligible`` reads and the
    boundary-risk ``live_eligible`` check ``detail`` (``risk_evaluator.py:870``). Approximate
    but recorded-faithful; used when the scorer inputs are unavailable but the event trace is.
  * **TIER C — degenerate two-state / SCRIPTED** (the floor): eligible until a recorded (or
    SCRIPTED) block instant, then NOT-eligible. Enough to reproduce the
    "eligible-at-confirm, flicker-False-at-entry" TOCTOU even if intermediate transitions are
    coarse. For P2's MACHINERY test this is the right tier — a scripted flicker exercises the
    as-of-t WRITE path + the real gate without depending on prod data.

The FORWARD-MOMENTUM leg of the grace is ALREADY replay-native (the linchpin, design §2.3.1):
``live_runner._live_forward_momentum`` → ``pipeline._live_flow_slope(as_of=t)`` reads
``iqfeed_trade_ticks`` AS-OF a past instant. So this module also offers ``seed_forward_momentum_ticks``
to write a recorded/synthetic buyer-aggressed tape so the as-of-t OFI read shows the forward
momentum the grace keys on. (P3/P4 use the REAL recorded tape; P2 writes a synthetic one.)

See docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §2.3 / §4 (P2) / R1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text as _sql
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationEvent

_log = logging.getLogger(__name__)

# The reconstruction tiers (explicit + logged so a replay's eligibility provenance is auditable).
TIER_A_SCORER = "A_scorer"
TIER_B_EVENT = "B_event_derived"
TIER_C_DEGENERATE = "C_degenerate_scripted"


def _naive_utc(t: datetime) -> datetime:
    """Normalize any instant to naive-UTC (the codebase's dominant convention + the
    ``_utcnow()`` / ``freshness_ts`` shape). A tz-aware instant is converted; a naive one is
    assumed already-UTC and returned unchanged."""
    if t.tzinfo is not None:
        return t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


# ── the eligibility timeline (the step-function the replayer writes from) ──────────
@dataclass(frozen=True)
class EligibilityTransition:
    """``live_eligible`` becomes ``eligible`` at instant ``at`` (naive-UTC). The replayer
    holds the most-recent transition at/<= ``t`` and writes its ``eligible`` value."""

    at: datetime
    eligible: bool


@dataclass
class EligibilityTimeline:
    """An ordered step-function of ``live_eligible`` over time + the chosen reconstruction
    tier. ``value_as_of(t)`` returns the eligibility in force at ``t`` (the last transition
    whose ``at`` <= ``t``; before the first transition it holds ``initial``)."""

    initial: bool
    transitions: list[EligibilityTransition] = field(default_factory=list)
    tier: str = TIER_C_DEGENERATE

    def value_as_of(self, t: datetime) -> bool:
        tt = _naive_utc(t)
        val = bool(self.initial)
        for tr in self.transitions:  # transitions are kept sorted by ``at``
            if tr.at <= tt:
                val = bool(tr.eligible)
            else:
                break
        return val


def scripted_flicker_timeline(
    *,
    eligible_until: datetime,
    flicker_at: datetime,
    reeligible_at: Optional[datetime] = None,
) -> EligibilityTimeline:
    """TIER C — build the UPC-shape scripted flicker: live_eligible=True from the start,
    flips False at ``flicker_at`` (the entry-instant TOCTOU), optionally back True at
    ``reeligible_at`` (the post-flicker re-score). ``eligible_until`` documents the confirm
    span the name held eligibility through (informational; the initial state is True).

    This is the floor reconstruction the P2 MACHINERY test uses — it drives the as-of-t WRITE
    path + the real gate WITHOUT depending on recorded prod data."""
    trs = [EligibilityTransition(at=_naive_utc(flicker_at), eligible=False)]
    if reeligible_at is not None:
        trs.append(EligibilityTransition(at=_naive_utc(reeligible_at), eligible=True))
    trs.sort(key=lambda tr: tr.at)
    _log.info(
        "[replay_eligibility] built %s flicker timeline: eligible_until=%s flicker_at=%s reeligible_at=%s",
        TIER_C_DEGENERATE, eligible_until, flicker_at, reeligible_at,
    )
    return EligibilityTimeline(initial=True, transitions=trs, tier=TIER_C_DEGENERATE)


def event_derived_timeline(
    db: Session,
    *,
    symbol: str,
    variant_id: int,
    session_id: Optional[int] = None,
) -> Optional[EligibilityTimeline]:
    """TIER B — stitch the eligibility step-function from the recorded
    ``trading_automation_events``. The runner persists ``live_blocked_by_risk`` (with the
    not-eligible reason) and the boundary-risk ``live_eligible`` check ``detail``; a
    ``live_entry_*`` event implies eligibility held. We mine the event stream for the
    transitions and return a timeline, or ``None`` when no eligibility-bearing events exist
    (caller falls back to a lower tier). Best-effort + fail-safe.

    NOTE: P2 ships the MACHINERY; the precise event-payload mining for real sessions is
    exercised by the P3 parity harness against a recorded session. Here we provide the
    skeleton (the event read + the transition stitch) so the tier is real, not a stub."""
    try:
        q = db.query(TradingAutomationEvent).order_by(TradingAutomationEvent.id.asc())
        if session_id is not None:
            q = q.filter(TradingAutomationEvent.session_id == int(session_id))
        evs = q.all()
    except Exception:
        _log.debug("[replay_eligibility] event-derived read failed", exc_info=True)
        return None
    transitions: list[EligibilityTransition] = []
    for e in evs:
        et = str(getattr(e, "event_type", "") or "")
        at = getattr(e, "ts", None)
        if at is None:
            continue
        at = _naive_utc(at)
        payload = getattr(e, "payload_json", None)
        if not isinstance(payload, dict):
            payload = {}
        # A risk block whose error names live-eligibility ⇒ ineligible at this instant.
        if et == "live_blocked_by_risk":
            errs = " ".join(str(x) for x in (payload.get("errors") or []))
            if "live-eligible" in errs.lower() or "live_eligible" in errs.lower():
                transitions.append(EligibilityTransition(at=at, eligible=False))
        # An entry submission/fill implies eligibility was held at that instant.
        elif et in ("live_entry_submitted", "live_entry_filled"):
            transitions.append(EligibilityTransition(at=at, eligible=True))
    if not transitions:
        return None
    transitions.sort(key=lambda tr: tr.at)
    _log.info(
        "[replay_eligibility] built %s timeline for %s/%s: %d transitions",
        TIER_B_EVENT, symbol, variant_id, len(transitions),
    )
    # Initial eligibility = True (the name armed+confirmed live-eligible; the timeline then
    # flips it per the recorded events).
    return EligibilityTimeline(initial=True, transitions=transitions, tier=TIER_B_EVENT)


def scorer_recompute_eligible_as_of(
    db: Session,
    *,
    symbol: str,
    variant_id: int,
    t: datetime,
    universe_snapshot_provider: Optional[Callable[[datetime], Any]] = None,
) -> Optional[bool]:
    """TIER A — recompute ``live_eligible`` by running the SAME viability scorer
    ``ross_momentum.score_universe`` over the recorded inputs AS-OF ``t``.

    Read-only import of the scorer (this module NEVER modifies ``ross_momentum.py``). Returns
    the recomputed ``live_eligible`` or ``None`` when the recorded scorer inputs for ``t`` are
    not available (caller falls back to a lower tier). P2 does not stand up the recorded
    universe-snapshot inputs (that is the P3/P4 real-data path), so this returns ``None``
    unless a ``universe_snapshot_provider`` is supplied — the seam P3/P4 will wire."""
    if universe_snapshot_provider is None:
        return None
    try:
        from . import ross_momentum as _rm  # read-only import of the scorer

        snapshot = universe_snapshot_provider(_naive_utc(t))
        if not snapshot:
            return None
        scored = _rm.score_universe(snapshot)  # type: ignore[arg-type]
        for row in scored or []:
            sym = str(row.get("symbol") if isinstance(row, dict) else getattr(row, "symbol", "")).upper()
            if sym == symbol.strip().upper():
                le = row.get("live_eligible") if isinstance(row, dict) else getattr(row, "live_eligible", None)
                if le is not None:
                    _log.info(
                        "[replay_eligibility] %s recompute %s@%s -> live_eligible=%s",
                        TIER_A_SCORER, symbol, t, bool(le),
                    )
                    return bool(le)
        return None
    except Exception:
        _log.debug("[replay_eligibility] scorer recompute failed", exc_info=True)
        return None


# ── the replayer (writes live_eligible + freshness_ts as-of t before each tick) ────
class EligibilityReplayer:
    """Drive ``MomentumSymbolViability.live_eligible`` + ``freshness_ts`` as-of each grid
    instant ``t`` so a recorded session's eligibility TIME-SERIES (incl the flicker) is
    reproduced for the unchanged ``tick_live_session`` viability read + the real gate.

    Construct with a chosen ``EligibilityTimeline`` (any tier). Before each tick the driver
    calls ``apply(db, t)`` which writes the as-of-``t`` ``live_eligible`` and stamps
    ``freshness_ts`` so the viability row reads FRESH under the sim clock (the freshness gate
    uses ``_utcnow()`` — sim-governed — so a freshness_ts at/just-before ``t`` is within the
    policy window). The write is a targeted UPDATE on the single (symbol, variant_id) row.
    """

    def __init__(
        self,
        *,
        symbol: str,
        variant_id: int,
        timeline: EligibilityTimeline,
        freshness_lag_seconds: float = 1.0,
    ) -> None:
        self.symbol = symbol.strip().upper()
        self.variant_id = int(variant_id)
        self.timeline = timeline
        # freshness_ts is stamped this many seconds BEFORE t (a tiny positive lag so the read
        # is fresh-but-not-future under the sim clock). Default 1s << the 600s policy window.
        self.freshness_lag_seconds = max(0.0, float(freshness_lag_seconds))
        self.apply_log: list[tuple[datetime, bool]] = []

    @property
    def tier(self) -> str:
        return self.timeline.tier

    def eligible_as_of(self, t: datetime) -> bool:
        return self.timeline.value_as_of(t)

    def apply(self, db: Session, t: datetime) -> bool:
        """Write the as-of-``t`` ``live_eligible`` + a fresh ``freshness_ts`` onto the single
        viability row. Returns the eligibility value written. Idempotent per tick."""
        tt = _naive_utc(t)
        eligible = self.timeline.value_as_of(tt)
        fresh_ts = tt - timedelta(seconds=self.freshness_lag_seconds)
        row = (
            db.query(MomentumSymbolViability)
            .filter(
                MomentumSymbolViability.symbol == self.symbol,
                MomentumSymbolViability.variant_id == self.variant_id,
            )
            .one_or_none()
        )
        if row is None:
            _log.warning(
                "[replay_eligibility] no viability row for %s/%s — apply(t=%s) is a no-op",
                self.symbol, self.variant_id, t,
            )
            self.apply_log.append((tt, eligible))
            return eligible
        row.live_eligible = bool(eligible)
        row.freshness_ts = fresh_ts
        db.flush()
        self.apply_log.append((tt, eligible))
        return eligible


# ── forward-momentum tape (the as-of-t OFI evidence the grace keys on) ─────────────
def seed_forward_momentum_ticks(
    db: Session,
    *,
    symbol: str,
    as_of: datetime,
    start: Optional[datetime] = None,
    n: Optional[int] = None,
    cadence_seconds: float = 1.0,
    price: float = 10.05,
    size: float = 100.0,
    spread: float = 0.02,
) -> int:
    """Write a synthetic BUYER-AGGRESSED ``iqfeed_trade_ticks`` tape so the as-of-t order-flow
    read (``pipeline._live_flow_slope(as_of=t)`` via ``live_runner._live_forward_momentum``)
    shows FORWARD MOMENTUM (ofi_level>0 ∧ slope>=0) — the leg the recency-grace keys on. Each
    tick has ``price >= ask`` (Lee-Ready buy-aggressor ⇒ +1 signed flow), with a faint rising
    drift so the OFI slope stays non-negative.

    The tape spans ``[start, as_of]`` at ``cadence_seconds`` (default 1s) so EVERY as-of-t read
    inside that span has fresh buyer-aggressed ticks ≤ t (the freshness gate needs the newest
    print within one grid step of t). ``start`` defaults to one OFI window before ``as_of``;
    pass ``start=grid[0].ts - window`` to cover a whole grid's entry window. ``n`` overrides the
    cadence-derived tick count (back-compat: a small ``n`` packs near ``as_of``). Returns rows
    written.

    P2 uses this synthetic tape; P3/P4 read the REAL recorded ``iqfeed_trade_ticks`` (no seed).
    The read window is the live OFI knob (``chili_crypto_l2_ofi_window_s`` default 15s)."""
    sym = symbol.strip().upper()
    end = _naive_utc(as_of)
    try:
        window = float(getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0)
    except (TypeError, ValueError):
        window = 15.0
    cadence = max(0.25, float(cadence_seconds))
    if start is not None:
        begin = _naive_utc(start)
        span = max(cadence, (end - begin).total_seconds())
        count = int(span / cadence) + 1
    elif n is not None:
        # back-compat: pack n ticks ending at as_of, inside one window.
        count = max(2, int(n))
        cadence = max(cadence, min(cadence, (window - 1.0) / max(1, count)))
        begin = end - timedelta(seconds=cadence * (count - 1))
    else:
        # default: one window of ~1s ticks ending at as_of.
        count = int(window / cadence) + 1
        begin = end - timedelta(seconds=cadence * (count - 1))

    bid = price - spread / 2.0
    ask = price + spread / 2.0
    written = 0
    ins = _sql(
        "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
        "VALUES (:sym, :at, :px, :sz, :bid, :ask, 'replay_v3')"
    )
    for i in range(count):
        at = begin + timedelta(seconds=cadence * i)
        if at > end:
            break
        drift = 0.0005 * i  # monotone up ⇒ non-negative slope
        px = ask + drift  # at/above the ask ⇒ buy aggressor (+1)
        db.execute(
            ins,
            {"sym": sym, "at": at, "px": px, "sz": size, "bid": bid + drift, "ask": ask + drift},
        )
        written += 1
    db.flush()
    _log.info(
        "[replay_eligibility] seeded %d buyer-aggressed ticks for %s over [%s, %s] (window=%.0fs)",
        written, sym, begin, end, window,
    )
    return written


def clear_forward_momentum_ticks(db: Session, *, symbol: str) -> int:
    """Delete the replay-seeded ``iqfeed_trade_ticks`` rows for ``symbol`` (cleanup; only the
    rows this module wrote, keyed on source='replay_v3'). Returns rows deleted."""
    sym = symbol.strip().upper()
    res = db.execute(
        _sql("DELETE FROM iqfeed_trade_ticks WHERE symbol = :s AND source = 'replay_v3'"),
        {"s": sym},
    )
    db.flush()
    return int(res.rowcount or 0)
