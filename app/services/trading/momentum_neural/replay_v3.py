"""Replay v3 P1 — the live-FSM SIMULATOR driver.

Step ONE recorded momentum session END-TO-END through the *real*
``live_runner.tick_live_session`` FSM, on a SIMULATED clock, against a deterministic
``replay_mock_broker.MockBrokerAdapter`` (no real broker, no network), serving 15m/5m/1m
bars from a RECORDED-OHLCV provider via the ``live_runner`` seam. The FSM is NEVER
re-implemented — the driver only supplies INPUTS (clock + quote + bars + a seeded session)
and calls the unchanged ``tick_live_session`` once per grid step, letting the runner's own
state machine drive ``queued_live → watching_live → live_entry_candidate → live_pending_entry
→ live_entered → … → live_exited``.

This is the instrument Replay v2 structurally cannot be (v2 forks the arm→enter decision
inline over the tape; v3 runs the live gate verbatim). P1 wires the machinery on SYNTHETIC
recorded data; the real-data / chili_staging replay + the UPC recency-grace A/B are P2–P4.

Reuse map (docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §6):
  * clock      — ``live_runner.replay_clock`` (P0 ContextVar on ``_utcnow``)
  * broker     — ``replay_mock_broker.MockBrokerAdapter`` + ``make_mock_broker_factory``
  * OHLCV seam — ``live_runner.replay_ohlcv_provider`` (P1 ContextVar on the in-tick fetch)
  * FSM        — ``live_runner.tick_live_session(db, sid, adapter_factory=…)`` (verbatim)

See docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §2.2 / §4 (P1).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd
from sqlalchemy.orm import Session

from ....models.core import User
from ....models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from . import live_runner as lr
from .live_fsm import STATE_QUEUED_LIVE
from .replay_mock_broker import MockBrokerAdapter, RecordedQuote, make_mock_broker_factory

_log = logging.getLogger(__name__)


# ── recorded inputs (the driver's data contract) ─────────────────────────────────
@dataclass(frozen=True)
class RecordedNbboTick:
    """One recorded NBBO snapshot at ``ts`` (naive-UTC). Mirrors ``momentum_nbbo_spread_tape``."""

    ts: datetime
    bid: float
    ask: float
    last: Optional[float] = None

    def as_quote(self) -> RecordedQuote:
        return RecordedQuote(bid=self.bid, ask=self.ask, last=self.last)


@dataclass
class RecordedArm:
    """A recorded live arm to seed: symbol + the confirm-time live-eligibility anchor.

    ``live_eligible_at_utc`` is the anchor ``confirm_live_arm`` stamps (the recency-grace
    keys off it) — seeded onto ``risk_snapshot_json['live_eligible_at_utc']`` so the grace is
    EXERCISABLE in P2 even though P1 enters via the happy (live_eligible=True) path."""

    symbol: str
    live_eligible_at_utc: str
    viability_score: float = 0.9
    atr_pct: float = 0.02
    user_id: Optional[int] = None
    variant_id: Optional[int] = None


@dataclass
class ReplaySeed:
    """The fully-seeded session handle the driver steps."""

    session_id: int
    symbol: str
    variant_id: int
    user_id: int


@dataclass
class TickTrace:
    """One grid step's outcome (the per-tick decision trace, the parity-harness input)."""

    ts: datetime
    state_before: str
    state_after: str
    result: dict[str, Any]


@dataclass
class ReplayResult:
    """The end-to-end run trace: the FSM state path + the mock's fills + the event log."""

    states_visited: list[str] = field(default_factory=list)
    ticks: list[TickTrace] = field(default_factory=list)
    final_state: str = ""
    entry_fill_price: Optional[float] = None
    exit_fill_prices: list[float] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


# ── the recorded-OHLCV provider (the fetch_ohlcv_df replacement) ─────────────────
class RecordedOhlcvProvider:
    """Serve OHLCV bars from RECORDED data (a per-interval frame) instead of the network.

    Installed on ``live_runner``'s ``_REPLAY_OHLCV_PROVIDER`` seam for the run. The runner
    calls it as ``provider(ticker, interval=…, period=…)`` — the exact ``fetch_ohlcv_df``
    signature. Bars are keyed by ``interval`` (15m/5m/1m); an unknown interval falls back to
    the nearest provided frame, or an empty frame (the runner's fetch sites all tolerate an
    empty/None df). P1 serves the WHOLE recorded frame each call (as-of slicing is P2/P3 —
    here the synthetic frame already ends at/just-before the entry instant)."""

    def __init__(self, frames_by_interval: dict[str, pd.DataFrame]) -> None:
        self._frames = {str(k): v for k, v in frames_by_interval.items()}
        self.call_log: list[tuple[str, str, str]] = []

    def __call__(
        self, ticker: str, *, interval: str = "1d", period: str = "6mo"
    ) -> pd.DataFrame:
        self.call_log.append((str(ticker), str(interval), str(period)))
        df = self._frames.get(str(interval))
        if df is None and self._frames:
            # Nearest available frame (the runner only needs a coherent OHLCV shape).
            df = next(iter(self._frames.values()))
        if df is None:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"]
            )
        return df.copy()


def synthetic_uptrend_ohlcv(
    *, n: int = 48, start_close: float = 10.0, step: float = 0.05, surge_mult: float = 3.0
) -> pd.DataFrame:
    """A clean rising OHLCV frame whose LAST bar carries a volume surge — passes the shared
    momentum/volume entry confirmation (``entry_gates.momentum_volume_confirmation`` and the
    pullback-break fallback). Deterministic; no RNG. Used to seed a synthetic recorded day so
    the e2e test does not depend on prod ``chili`` data."""
    closes = [start_close + i * step for i in range(n)]
    base_vol = 1000.0
    vols = [base_vol for _ in range(n - 1)] + [base_vol * surge_mult]
    return pd.DataFrame(
        {
            "Open": [c - step * 0.4 for c in closes],
            "High": [c + step * 0.6 for c in closes],
            "Low": [c - step * 0.6 for c in closes],
            "Close": closes,
            "Volume": vols,
        }
    )


# ── seeding (the recorded arm → a queued_live session in the replay DB) ──────────
def _ensure_user(db: Session, *, name: Optional[str] = None) -> int:
    # ``users.name`` is UNIQUE — make the replay user name collision-proof across runs.
    uname = name or f"ReplayV3_{uuid.uuid4().hex[:10]}"
    u = User(name=uname)
    db.add(u)
    db.flush()
    return int(u.id)


def _ensure_variant(
    db: Session, *, execution_family: str = "robinhood_spot"
) -> MomentumStrategyVariant:
    """A minimal impulse_breakout variant (the family the params normalize against).

    ``(family, variant_key, version)`` is UNIQUE — use a per-call ``variant_key`` so repeated
    seeds (and a non-truncated DB) never collide."""
    v = MomentumStrategyVariant(
        family="impulse_breakout",
        variant_key=f"replay_v3_{uuid.uuid4().hex[:8]}",
        version=1,
        label="Replay v3 impulse_breakout",
        params_json={},
        is_active=True,
        execution_family=execution_family,
    )
    db.add(v)
    db.flush()
    return v


def seed_replay_session(
    db: Session,
    arm: RecordedArm,
    *,
    execution_family: str = "robinhood_spot",
    state: str = STATE_QUEUED_LIVE,
) -> ReplaySeed:
    """Seed ONE ``queued_live`` (or ``armed``) live momentum session from a recorded arm.

    Writes: a user, an impulse_breakout variant, a ``MomentumSymbolViability`` row
    (``live_eligible=True``, fresh ``freshness_ts``, ``regime_snapshot_json`` carrying the
    ATR so the stop sizes), and a ``TradingAutomationSession`` whose ``risk_snapshot_json``
    carries the frozen risk gate, the live-execution block, the policy caps, AND the
    ``live_eligible_at_utc`` recency-grace anchor. Self-contained — no prod data."""
    uid = arm.user_id or _ensure_user(db)
    variant = _ensure_variant(db, execution_family=execution_family)
    vid = arm.variant_id or int(variant.id)

    # Viability: live-eligible + fresh as-of the seed (the recency-grace happy path; the
    # P2 eligibility-replayer flips this per-tick to reproduce the flicker).
    via = MomentumSymbolViability(
        symbol=arm.symbol,
        scope="symbol",
        variant_id=vid,
        viability_score=float(arm.viability_score),
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=datetime.utcnow(),
        regime_snapshot_json={"atr_pct": float(arm.atr_pct), "meta": {"atr_pct": float(arm.atr_pct)}},
        execution_readiness_json={"spread_bps": 8.0},
        explain_json={},
        evidence_window_json={},
    )
    db.add(via)
    db.flush()

    risk_snapshot = {
        lr.RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": arm.live_eligible_at_utc},
        # The recency-grace anchor (confirm_live_arm stamps this top-level). REQUIRED so the
        # grace is exercisable later (P2); present-but-unused on the P1 happy path.
        "live_eligible_at_utc": arm.live_eligible_at_utc,
        "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        # A sane, documented risk budget so the risk-first sizing produces a small qty that
        # fits comfortably under a generous notional ceiling (else the notional-cap gate
        # bounces the entry back to watching without ever placing). These are SEED defaults
        # the caller can override on the RecordedArm in a later phase.
        "momentum_policy_caps": {
            "max_notional_per_trade_usd": 100000.0,
            "max_hold_seconds": 14400,
            "max_loss_per_trade_usd": 50.0,
        },
        lr.KEY_LIVE_EXEC: {"tick_count": 0},
    }
    sess = TradingAutomationSession(
        user_id=uid,
        venue="robinhood",
        execution_family=execution_family,
        mode="live",
        symbol=arm.symbol,
        variant_id=vid,
        state=state,
        risk_snapshot_json=risk_snapshot,
        correlation_id="replay-v3-p1",
    )
    db.add(sess)
    db.flush()
    return ReplaySeed(session_id=int(sess.id), symbol=arm.symbol, variant_id=vid, user_id=uid)


# ── the event grid ───────────────────────────────────────────────────────────────
def build_event_grid(
    nbbo: list[RecordedNbboTick], *, step_seconds: float = 0.0
) -> list[RecordedNbboTick]:
    """Build the ordered time/event grid the driver steps. With ``step_seconds<=0`` the grid
    is the recorded NBBO ticks themselves (true tick granularity — a sub-minute flicker is
    hit). With a coarse step it down-samples to one tick per ``step_seconds`` bucket (the
    existing tick-cadence option). Always sorted by ``ts``."""
    ticks = sorted(nbbo, key=lambda t: t.ts)
    if step_seconds <= 0 or not ticks:
        return ticks
    out: list[RecordedNbboTick] = []
    next_at: Optional[datetime] = None
    for t in ticks:
        if next_at is None or t.ts >= next_at:
            out.append(t)
            next_at = t.ts + timedelta(seconds=float(step_seconds))
    return out


# ── the driver ───────────────────────────────────────────────────────────────────
class ReplayV3Driver:
    """Step the REAL ``tick_live_session`` across an event grid with a mock broker + sim clock.

    Per grid step (in order, mirroring docs §2.2):
      1. ``mock.set_clock(t)`` + ``mock.set_quote(symbol, quote@t)`` — broker BBO/fill as-of t.
      2. ``replay_clock(t)`` — freeze the runner's ``_utcnow()`` chokepoint at t.
      3. ``replay_ohlcv_provider(provider)`` — serve recorded bars for the in-tick fetches.
      4. ``tick_live_session(db, sid, adapter_factory=make_mock_broker_factory(mock))``.

    The FSM advances itself; the driver only records the per-tick state transition + result.
    """

    def __init__(
        self,
        db: Session,
        seed: ReplaySeed,
        *,
        mock: MockBrokerAdapter,
        ohlcv_provider: Callable[..., Any],
        grid: list[RecordedNbboTick],
        risk_gate_allows: bool = True,
    ) -> None:
        self.db = db
        self.seed = seed
        self.mock = mock
        self.ohlcv_provider = ohlcv_provider
        self.grid = grid
        self.risk_gate_allows = bool(risk_gate_allows)
        self._factory = make_mock_broker_factory(mock)

    def _session(self) -> Optional[TradingAutomationSession]:
        return (
            self.db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.id == self.seed.session_id)
            .one_or_none()
        )

    def _state(self) -> str:
        s = self._session()
        return str(s.state) if s is not None else "<gone>"

    def step(self, t: datetime, quote: Optional[RecordedQuote]) -> TickTrace:
        """Run ONE tick at instant ``t`` with the recorded ``quote`` (None ⇒ no_bbo)."""
        # 1) broker clock + quote
        self.mock.set_clock(t)
        if quote is None:
            self.mock.clear_quote(self.seed.symbol)
        else:
            self.mock.set_quote(self.seed.symbol, quote)
        state_before = self._state()
        # 2) sim clock + 3) recorded OHLCV provider, both around the unchanged FSM tick.
        with lr.replay_clock(t), lr.replay_ohlcv_provider(self.ohlcv_provider):
            result = lr.tick_live_session(
                self.db, self.seed.session_id, adapter_factory=self._factory
            )
        self.db.flush()
        state_after = self._state()
        return TickTrace(
            ts=t, state_before=state_before, state_after=state_after, result=dict(result)
        )

    def run(self) -> ReplayResult:
        """Step the whole grid and return the end-to-end trace."""
        res = ReplayResult()
        # Optionally neutralize the full risk gate (its full DB-seeded eval is out of P1
        # scope; it has its own dedicated parity tests). The driver wraps the REAL FSM either
        # way — only this ONE pre-entry gate is short-circuited, never the FSM transitions.
        _orig_gate = lr.runner_boundary_risk_ok
        if self.risk_gate_allows:
            lr.runner_boundary_risk_ok = lambda *a, **k: (True, {"allowed": True, "replay": True})  # type: ignore[assignment]
        try:
            res.states_visited.append(self._state())
            for tk in self.grid:
                trace = self.step(tk.ts, tk.as_quote())
                res.ticks.append(trace)
                if trace.state_after != res.states_visited[-1]:
                    res.states_visited.append(trace.state_after)
        finally:
            lr.runner_boundary_risk_ok = _orig_gate  # type: ignore[assignment]

        res.final_state = self._state()
        # Mine fills off the mock + the event log off the DB (the runner persisted them).
        fills, _ = self.mock.get_fills(limit=1000)
        for f in fills:
            if f.side in ("buy", "bid", "long") and res.entry_fill_price is None:
                res.entry_fill_price = float(f.price)
            elif f.side in ("sell", "ask", "short"):
                res.exit_fill_prices.append(float(f.price))
        evs = (
            self.db.query(TradingAutomationEvent)
            .filter(TradingAutomationEvent.session_id == self.seed.session_id)
            .order_by(TradingAutomationEvent.id.asc())
            .all()
        )
        res.events = [str(e.event_type) for e in evs]
        return res
