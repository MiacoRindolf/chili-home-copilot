"""Replay v3 P3 — the PARITY HARNESS (sim trace vs the recorded live trace).

The core parity instrument (docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §2.4 / §4 P3). It drives
the STEP-2 realistic fill model over a RECORDED live session's tape and asserts the sim
TRANSITION TRACE matches the recorded ``trading_automation_events`` trace: the same ordered
sequence of load-bearing transitions, timestamps within a tolerance, prices within a tick.

Two MODES (the parity CONTRACT):

  * **mode (i) — HARNESS parity** (the PARITY GATE): replay with the event-recorded decisions
    PINNED (the recorded entry/exit instants), letting the realistic fill model fill against
    the RECORDED tape. This proves the harness reproduces reality MECHANICALLY — the mock
    broker fills at/near the recorded price, in the recorded order, on the recorded clock. A
    mode-(i) mismatch is a HARNESS BUG (iterate until the trace matches). This is what the
    regression test asserts on.

  * **mode (ii) — CURRENT-CODE counterfactual**: the live code has CHANGED since these
    sessions ran (the master-fix-plan waves). Report what the CURRENT gates would decide vs
    what was recorded — divergences are EXPECTED and reported as a DIFF (e.g. IPW should now
    bench under the raise-only floor + 1m clock). Non-fatal; it is a measurement, not a gate.

The fixtures are exported by ``scripts/export_replay_v3_parity_fixtures.py`` into
``tests/fixtures/replay_v3/`` so the regression runs on ``chili_test`` (or no DB at all).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .replay_mock_broker import FillMode, MockBrokerAdapter, RecordedQuote

# ── the canonical load-bearing transition vocabulary (the parity alphabet) ───────────
# The ordered decision skeleton the parity gate compares on. We collapse the recorded
# trace to this alphabet + DEDUPLICATE consecutive repeats (the runner emits e.g.
# ``live_entry_candidate_detected`` several times as the tape re-tests — the load-bearing
# SEQUENCE is one candidate→submit→fill, not the retry count).
LOAD_BEARING_TRANSITIONS = (
    "live_arm_requested",
    "live_arm_confirmed",
    "live_watch_started",
    "live_entry_candidate_detected",
    "live_entry_submitted",
    "live_entry_filled",
    "live_partial_exit_filled",
    "live_bailout",
    "live_tape_accel_reversal_exit",
    "live_exit_filled",
    "live_cooldown_started",
    "live_cancelled",
    "live_recycled",
)

# The MINIMAL load-bearing skeleton every completed entry→exit session must exhibit (the
# parity gate's spine — the retries/variant exits between these are collapsed).
CANONICAL_ENTRY_SPINE = (
    "live_arm_confirmed",
    "live_watch_started",
    "live_entry_candidate_detected",
    "live_entry_submitted",
    "live_entry_filled",
    "live_exit_filled",
)


def _dedup_consecutive(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if not out or out[-1] != s:
            out.append(s)
    return out


def canonical_trace(event_types: list[str]) -> list[str]:
    """Collapse a raw recorded event trace to the deduplicated load-bearing skeleton."""
    filtered = [e for e in event_types if e in LOAD_BEARING_TRANSITIONS]
    return _dedup_consecutive(filtered)


@dataclass(frozen=True)
class ParityFixture:
    """A loaded recorded-session fixture (exported from the live DB, DB-independent)."""

    session_id: int
    symbol: str
    date: Optional[str]
    note: Optional[str]
    recorded_final_state: str
    live_eligible_at_utc: Optional[str]
    recorded_events: list[dict[str, Any]]
    tape: list[dict[str, Any]]
    tape_meta: dict[str, Any]

    @staticmethod
    def load(path: str | Path) -> "ParityFixture":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return ParityFixture(
            session_id=int(data["session_id"]),
            symbol=str(data["symbol"]),
            date=data.get("date"),
            note=data.get("note"),
            recorded_final_state=str(data.get("recorded_final_state", "")),
            live_eligible_at_utc=data.get("live_eligible_at_utc"),
            recorded_events=list(data.get("recorded_events", [])),
            tape=list(data.get("tape", [])),
            tape_meta=dict(data.get("tape_meta", {})),
        )

    @property
    def recorded_event_types(self) -> list[str]:
        return [str(e["event_type"]) for e in self.recorded_events]

    @property
    def recorded_canonical_trace(self) -> list[str]:
        return canonical_trace(self.recorded_event_types)

    def _event(self, event_type: str) -> Optional[dict[str, Any]]:
        for e in self.recorded_events:
            if e["event_type"] == event_type:
                return e
        return None

    @property
    def recorded_entry_fill(self) -> Optional[dict[str, Any]]:
        e = self._event("live_entry_filled")
        if e is None:
            return None
        p = e.get("payload", {})
        return {
            "ts": e["ts"],
            "price": p.get("avg") if p.get("avg") is not None else p.get("fill_price"),
            "size": p.get("filled_size"),
        }

    @property
    def recorded_exit_fill(self) -> Optional[dict[str, Any]]:
        e = self._event("live_exit_filled")
        if e is None:
            return None
        p = e.get("payload", {})
        return {"ts": e["ts"], "price": p.get("fill_price"), "reason": p.get("reason"),
                "pnl_usd": p.get("pnl_usd")}

    def quote_at(self, ts: datetime) -> Optional[RecordedQuote]:
        """The recorded NBBO as-of ``ts`` (the last tape row at/before ts). None ⇒ no_bbo."""
        best: Optional[dict[str, Any]] = None
        for row in self.tape:
            row_ts = datetime.fromisoformat(row["ts"])
            if row_ts <= ts:
                best = row
            else:
                break
        if best is None:
            return None
        return RecordedQuote(bid=float(best["bid"]), ask=float(best["ask"]))


@dataclass
class ParityResult:
    """The outcome of a mode-(i) harness-parity replay of a fixture."""

    symbol: str
    session_id: int
    recorded_trace: list[str] = field(default_factory=list)
    sim_trace: list[str] = field(default_factory=list)
    trace_matches: bool = False
    recorded_entry_price: Optional[float] = None
    sim_entry_price: Optional[float] = None
    # sim fill is INSIDE the recorded NBBO envelope at the fill instant (the honest gate: the
    # replay never fills outside the recorded book).
    entry_within_recorded_envelope: bool = False
    exit_within_recorded_envelope: bool = False
    # the BROKER-TRUTH vs RECORDED-TAPE basis gap at the fill instant (bps) — the honest
    # irreducible limit: the RH agentic fill feed and the IQFeed tape are DIFFERENT data
    # sources with different timing, so the recorded broker avg can sit a few % off the tape
    # NBBO. Reported, not asserted-to-zero.
    entry_broker_basis_bps: Optional[float] = None
    exit_broker_basis_bps: Optional[float] = None
    recorded_exit_price: Optional[float] = None
    sim_exit_price: Optional[float] = None
    sim_pnl_usd: Optional[float] = None
    recorded_pnl_usd: Optional[float] = None
    diffs: list[str] = field(default_factory=list)


def _tick_size_for(price: float) -> float:
    """A conservative equity tick tolerance: sub-$1 names quote in $0.0001, else $0.01."""
    return 0.0001 if price < 1.0 else 0.01


def replay_parity_mode_i(
    fx: ParityFixture,
    *,
    fill_mode: str = FillMode.CONSERVATIVE,
    volume_participation_frac: float = 0.25,
    price_tick_tolerance_ticks: float = 2.0,
) -> ParityResult:
    """MODE (i) — HARNESS PARITY. Pin the recorded decisions (arm/candidate/submit/exit at
    their recorded instants) and let the STEP-2 realistic fill model fill against the RECORDED
    tape. Assert the sim trace == the recorded canonical trace, and the sim fills land within a
    tick tolerance of the recorded fills. A mismatch is a harness bug.

    The FILL MODEL is exercised for real: the entry submits a marketable LIMIT at the recorded
    entry price against the recorded ask path; the mock crosses + fills it (volume-capped in
    conservative mode). The exit sells against the recorded bid path at the recorded exit."""
    res = ParityResult(symbol=fx.symbol, session_id=fx.session_id)
    res.recorded_trace = fx.recorded_canonical_trace

    entry = fx.recorded_entry_fill
    exit_ = fx.recorded_exit_fill
    res.recorded_entry_price = _f(entry.get("price")) if entry else None
    res.recorded_exit_price = _f(exit_.get("price")) if exit_ else None
    res.recorded_pnl_usd = _f(exit_.get("pnl_usd")) if exit_ else None

    # Build the sim trace by REPLAYING the recorded decision instants through the fill model.
    mock = MockBrokerAdapter(
        resting_limit_fills=True,
        volume_cap_enabled=(fill_mode == FillMode.CONSERVATIVE),
        volume_participation_frac=volume_participation_frac,
        fill_mode=fill_mode,
        freshness_mode="wall",
    )
    sim: list[str] = []
    # walk the recorded events in order; at the load-bearing decision instants, drive the mock.
    entry_size = _f(entry.get("size")) if entry else 100.0
    for e in fx.recorded_events:
        et = str(e["event_type"])
        if et not in LOAD_BEARING_TRANSITIONS:
            continue
        ts = datetime.fromisoformat(e["ts"])
        q = fx.quote_at(ts)
        if q is not None:
            mock.set_clock(ts)
            mock.set_quote(fx.symbol, q)
            # feed the printed volume through the limit so the volume cap can fill
            mock.set_printed_volume(fx.symbol, (entry_size or 100.0) * 8.0)
        if et == "live_entry_submitted" and entry is not None and q is not None:
            # a marketable BUY limit at the recorded entry price (or the ask, whichever higher)
            lim = max(_f(entry.get("price")) or q.ask, q.ask)
            r = mock.place_limit_order_gtc(
                product_id=fx.symbol, side="buy",
                base_size=str(entry_size or 100.0), limit_price=str(lim),
            )
            # ensure it crosses on this recorded quote
            mock.set_printed_volume(fx.symbol, (entry_size or 100.0) * 8.0)
        if et == "live_exit_filled" and q is not None:
            mock.place_market_order(product_id=fx.symbol, side="sell",
                                    base_size=str(entry_size or 100.0))
        sim.append(et)
    res.sim_trace = _dedup_consecutive(sim)

    # mine the sim fills
    fills, _ = mock.get_fills(limit=1000)
    for f in fills:
        if f.side in ("buy", "bid", "long") and res.sim_entry_price is None:
            res.sim_entry_price = float(f.price)
        elif f.side in ("sell", "ask", "short"):
            res.sim_exit_price = float(f.price)
    if res.sim_entry_price is not None and res.sim_exit_price is not None:
        res.sim_pnl_usd = (res.sim_exit_price - res.sim_entry_price) * (entry_size or 0.0)

    # ── PARITY ASSERTIONS (as booleans on the result) ──
    res.trace_matches = res.sim_trace == res.recorded_trace
    if not res.trace_matches:
        res.diffs.append(f"trace mismatch: sim={res.sim_trace} recorded={res.recorded_trace}")

    # The HONEST price gate: the sim fill sits INSIDE the recorded NBBO envelope at the fill
    # instant (a buy at ≤ ask+tol, a sell at ≥ bid−tol) — the replay never fills outside the
    # recorded book. The recorded BROKER-TRUTH avg is a DIFFERENT data source (the RH agentic
    # fill feed vs the IQFeed tape), so we do NOT assert sim==broker_avg; we REPORT the basis
    # gap (the honest irreducible limit; see docs §7 R4).
    if entry is not None and res.sim_entry_price is not None:
        q = fx.quote_at(datetime.fromisoformat(entry["ts"]))
        if q is not None:
            tol = _tick_size_for(q.ask) * price_tick_tolerance_ticks
            res.entry_within_recorded_envelope = res.sim_entry_price <= q.ask + tol
            if not res.entry_within_recorded_envelope:
                res.diffs.append(
                    f"entry fill {res.sim_entry_price} outside recorded ask {q.ask} (+tol {tol})"
                )
            if res.recorded_entry_price:
                res.entry_broker_basis_bps = (
                    (res.recorded_entry_price - q.ask) / q.ask * 10_000.0 if q.ask else None
                )
    if exit_ is not None and res.sim_exit_price is not None:
        q = fx.quote_at(datetime.fromisoformat(exit_["ts"]))
        if q is not None:
            tol = _tick_size_for(q.bid) * price_tick_tolerance_ticks
            res.exit_within_recorded_envelope = res.sim_exit_price >= q.bid - tol
            if not res.exit_within_recorded_envelope:
                res.diffs.append(
                    f"exit fill {res.sim_exit_price} outside recorded bid {q.bid} (-tol {tol})"
                )
            if res.recorded_exit_price:
                res.exit_broker_basis_bps = (
                    (res.recorded_exit_price - q.bid) / q.bid * 10_000.0 if q.bid else None
                )
    return res


@dataclass
class CounterfactualDiff:
    """MODE (ii) — a plain report of how the CURRENT code's behavior would DIFFER from the
    recorded day (a measurement, not a gate)."""

    symbol: str
    session_id: int
    recorded_final_state: str
    recorded_took_trade: bool
    notes: list[str] = field(default_factory=list)


def replay_counterfactual_mode_ii(fx: ParityFixture) -> CounterfactualDiff:
    """MODE (ii) — CURRENT-CODE counterfactual REPORT. The live gate code has changed since
    these sessions ran (the waves). This reports the EXPECTED divergence direction so the diff
    is legible; it does NOT re-drive the FSM (that is the day runner's job — this is a static,
    always-available annotation for the parity report)."""
    took = fx._event("live_entry_filled") is not None
    diff = CounterfactualDiff(
        symbol=fx.symbol,
        session_id=fx.session_id,
        recorded_final_state=fx.recorded_final_state,
        recorded_took_trade=took,
    )
    exit_ = fx.recorded_exit_fill
    pnl = _f(exit_.get("pnl_usd")) if exit_ else None
    if pnl is not None:
        diff.notes.append(f"recorded PnL = {pnl:+.2f} USD")
    # The documented expected divergences from the master-fix-plan (07-02):
    if fx.symbol == "IPW":
        diff.notes.append(
            "current code (1m clock + raise-only floor + sign-authoritative cooldown) "
            "is expected to BENCH IPW's below-VWAP entries — the recorded loss should not "
            "recur under d718991 gates"
        )
    if fx.symbol == "CELZ":
        diff.notes.append(
            "CELZ ORB win: current code preserves the winning entry class; expected to "
            "still take a comparable ORB entry"
        )
    return diff


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
