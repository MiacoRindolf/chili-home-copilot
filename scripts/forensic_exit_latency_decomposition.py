"""Phase-1 forensic: split every Alpaca LIVE realized loss into buckets that
reconcile exactly to the ledger P&L, and emit the per-trade exit timing series.

Corpus: every `momentum_automation_outcomes` row with `realized_pnl_usd < 0`
joined to `trading_automation_sessions` where `venue = 'alpaca'` and
`mode = 'live'`, created in the 21 days to 2026-09-02. That is FOUR rows —
CDTG (2026-08-26), and UPC / CANF / JLHL (2026-09-02). In the same window
3 691 Alpaca live outcomes exist, but 3 687 are `cancelled_pre_entry` with a
NULL `realized_pnl_usd`, so the priced corpus really is n=4.

BUCKETS (USD, signed). For every row:

    realized = A + C + B + D              and         B = B1 + B0 + B2

    A  = (S_struct  - entry_px)  * qty   ENTRY THESIS: what a perfect,
                                         instantaneous stop-out would have cost.
    C  = (S_breach  - S_struct)  * qty   TIGHTEN: the `viability_degraded_tighten`
                                         move, signed (positive = the tighten
                                         reduced the loss relative to the
                                         structural stop).
    B1 = (bid_cross - S_breach)  * qty   GAP AT FIRST OBSERVABLE CROSS. The first
                                         tape bid strictly below the stop is
                                         already below it; this term is the part
                                         of the slippage that no amount of speed
                                         can recover.
    B0 = (bid_event - bid_cross) * qty   DETECTION LAG: what the bid did between
                                         the tape crossing the stop and the runner
                                         emitting `stop_breach_pending_confirm`.
    B2 = (fill_px   - bid_event) * qty   CONFIRM -> SUBMIT -> FILL: the L2 classify
                                         hold, the stand-in pricing defers, and the
                                         broker round trip.
    D  = realized - (A + C + B1 + B0 + B2)   fees / residual. Measured 0.00 on all
                                         four rows (Alpaca is commission-free and
                                         `broker_recon_detail_json.fees_status` is
                                         `alpaca_commission_free_gross`).

INPUTS AND THEIR AUTHORITY
  entry_px, fill_px, and the fill timestamps come from BROKER TRUTH
    (`momentum_automation_outcomes.broker_recon_detail_json.broker_attribution.legs`),
    not from the ledger events. The ledger's `live_exit_filled.ts` lags the broker
    fill by 0.46 s (CANF), 2.20 s (JLHL) and 2.98 s (UPC).
  S_struct / S_breach / the breach and classify timestamps come from
    `trading_automation_events` payloads.
  bid_cross and bid_event come from `momentum_nbbo_spread_tape`, excluding
    `source = 'massive_snapshot'`. That source is a slow poll and is demonstrably
    wrong intraday: it printed CANF bid 4.33 at 11:11:05.840 while the WebSocket
    stream had 4.12 at 11:11:04.877 and 4.09 at 11:11:08.246.

The measured values are frozen in this module so the arithmetic is reproducible
and testable without a database. Re-derive them with the queries in the module
docstring of `_PROVENANCE` below.
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

_PROVENANCE = """
corpus:
  SELECT o.id, o.session_id, o.symbol, o.realized_pnl_usd, o.broker_realized_pnl_usd
    FROM momentum_automation_outcomes o
    JOIN trading_automation_sessions s ON s.id = o.session_id
   WHERE o.created_at >= now() - interval '21 days'
     AND o.realized_pnl_usd < 0 AND s.venue = 'alpaca' AND o.mode = 'live';
events:
  SELECT ts, event_type, payload_json FROM trading_automation_events
   WHERE session_id = :sid ORDER BY ts;
tape (bounded: one symbol, minutes):
  SELECT observed_at, source, bid, ask FROM momentum_nbbo_spread_tape
   WHERE symbol = :sym AND observed_at >= :lo AND observed_at < :hi
     AND source <> 'massive_snapshot' ORDER BY observed_at;
"""


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


@dataclass(frozen=True)
class Step:
    """One observed step of the exit path."""

    ts: str
    event: str
    bid_reported: Optional[float]
    bid_tape: Optional[float]
    note: str = ""


@dataclass(frozen=True)
class Trade:
    key: str
    symbol: str
    outcome_id: int
    session_id: int
    day: str
    qty: float
    entry_px: float
    s_struct: float
    s_breach: float
    bid_cross: float
    bid_event: float
    fill_px: float
    realized: float
    t_entry_fill: str
    t_first_cross: str
    t_breach_event: str
    t_fill: str
    quote_sources: str
    exit_path: str
    steps: tuple[Step, ...] = field(default=())

    # ---- buckets -------------------------------------------------------
    @property
    def A(self) -> float:
        return (self.s_struct - self.entry_px) * self.qty

    @property
    def C(self) -> float:
        return (self.s_breach - self.s_struct) * self.qty

    @property
    def B1(self) -> float:
        return (self.bid_cross - self.s_breach) * self.qty

    @property
    def B0(self) -> float:
        return (self.bid_event - self.bid_cross) * self.qty

    @property
    def B2(self) -> float:
        return (self.fill_px - self.bid_event) * self.qty

    @property
    def B(self) -> float:
        return self.B1 + self.B0 + self.B2

    @property
    def D(self) -> float:
        return self.realized - (self.A + self.C + self.B)

    def secs(self, a: str, b: str) -> float:
        return (_t(getattr(self, b)) - _t(getattr(self, a))).total_seconds()


TRADES: tuple[Trade, ...] = (
    Trade(
        key="CDTG", symbol="CDTG", outcome_id=201720, session_id=16534,
        day="2026-08-26", qty=191.0, entry_px=1.369948,
        s_struct=1.3184669738029555, s_breach=1.36309826,
        bid_cross=1.31, bid_event=1.31, fill_px=1.16, realized=-40.100068,
        t_entry_fill="2026-08-26T11:41:22",
        t_first_cross="2026-08-26T11:42:30.890069",
        t_breach_event="2026-08-26T11:42:37.458530",
        t_fill="2026-08-26T12:00:28",
        quote_sources="iqfeed_l1 dense (1 973-5 027 rows/min) + massive_snapshot",
        exit_path=(
            "NEVER PRICED. 19 x live_exit_deferred_final_bbo, every one "
            "execution_bbo_unavailable with age_seconds ~53 000 (14.7 h) while "
            "iqfeed_l1 was writing thousands of rows per minute for the same "
            "symbol. Runner process exited 11:45:05 for the PR #1179 deploy; "
            "operator sold manually at 12:00:28. Pre-dates the #1254 stop-class "
            "fail-open (live 2026-08-31)."
        ),
        steps=(
            Step("2026-08-26T11:41:22", "broker entry fill", None, None, "1.369948 x 191"),
            Step("2026-08-26T11:41:24.693652", "live_deadman_stop_placed", None, None,
                 "stop 1.31, INERT premarket (Alpaca rejects stops in ext hours)"),
            Step("2026-08-26T11:42:30.890069", "TAPE first cross of 1.31847", None, 1.31, ""),
            Step("2026-08-26T11:42:37.458530", "stop_breach_pending_confirm", 1.31, 1.31, ""),
            Step("2026-08-26T11:42:39.898788", "stop_breach_l2_classify", 1.31, None,
                 "held_s 2.44 INCONCLUSIVE/mixed"),
            Step("2026-08-26T11:42:40.063511", "live_exit_deferred_final_bbo", None, None,
                 "execution_bbo_unavailable age 53 032 s -- 1 of 19"),
            Step("2026-08-26T11:43:12.672409", "stop_breach_chop_hold", 1.27, None, "hold_n 1"),
            Step("2026-08-26T11:43:35.826866", "stop_breach_chop_hold", 1.30, None, "hold_n 1"),
            Step("2026-08-26T11:43:43.932311", "position_halted", None, None,
                 "suspected_halt_detected, stale_tick_streak 3"),
            Step("2026-08-26T11:43:45.357463", "halt_resumed", None, None, ""),
            Step("2026-08-26T11:43:47.752879", "viability_degraded_tighten", None, None,
                 "1.31847 -> 1.36310"),
            Step("2026-08-26T11:45:03.187365", "stop_breach_pending_confirm (last)", 1.31, 1.32, ""),
            Step("2026-08-26T11:45:05.059453", "live_cancelled", None, 1.32,
                 "orphaned_runner_process_exited (PR #1179 deploy)"),
            Step("2026-08-26T12:00:28", "operator manual sell", None, 1.16, "1.16 x 191"),
        ),
    ),
    Trade(
        key="UPC", symbol="UPC", outcome_id=203719, session_id=19457,
        day="2026-09-02", qty=314.0, entry_px=5.395955,
        s_struct=5.309003607274401, s_breach=5.309003607274401,
        bid_cross=5.29, bid_event=5.24, fill_px=5.24, realized=-48.96986999999988,
        t_entry_fill="2026-09-02T08:40:22.680801",
        t_first_cross="2026-09-02T08:48:06.404063",
        t_breach_event="2026-09-02T08:48:13.753884",
        t_fill="2026-09-02T08:48:20.000358",
        quote_sources="iqfeed_l1 dense + massive_ws_universe ~1/s",
        exit_path="breach -> l2_classify 2.17 s (INCONCLUSIVE/mixed) -> stand-in x2 -> fill",
        steps=(
            Step("2026-09-02T08:40:22.680801", "broker entry fill", None, None, "5.395955 x 314"),
            Step("2026-09-02T08:40:23.370806", "live_deadman_stop_inert_until_rth", None, None,
                 "stop 5.29 inert; runner software stop is sole protection"),
            Step("2026-09-02T08:40:45.881782", "bailout_breach_pending_confirm", 5.35, None,
                 "breakout_failed_to_hold; did NOT fire -- held 7.5 more minutes"),
            Step("2026-09-02T08:48:06.404063", "TAPE first cross of 5.30900", None, 5.29, ""),
            Step("2026-09-02T08:48:13.753884", "stop_breach_pending_confirm", 5.24, 5.24, ""),
            Step("2026-09-02T08:48:15.922754", "stop_breach_l2_classify", 5.24, None,
                 "held_s 2.17 INCONCLUSIVE/mixed, would_hold false"),
            Step("2026-09-02T08:48:16.214604", "live_exit_stand_in_pricing", 5.23, None,
                 "defer_count 1, quote age 1.41 s, stand_in_massive_sip"),
            Step("2026-09-02T08:48:16.252560", "live_deadman_stop_release_blocked", None, None,
                 "deadman_successor_intent_frozen_for_next_pulse"),
            Step("2026-09-02T08:48:17.554579", "live_exit_stand_in_pricing", 5.23, None,
                 "defer_count 1, SAME quote row, age now 2.75 s"),
            Step("2026-09-02T08:48:20.000358", "BROKER exit fill", None, 5.24, "5.24 x 314"),
            Step("2026-09-02T08:48:22.985055", "live_exit_filled (ledger)", None, None,
                 "ledger saw the fill 2.98 s after the broker booked it"),
        ),
    ),
    Trade(
        key="CANF_c1", symbol="CANF", outcome_id=203734, session_id=19471,
        day="2026-09-02", qty=355.0, entry_px=4.34,
        s_struct=4.265389592760181, s_breach=4.3183,
        bid_cross=4.28, bid_event=4.23, fill_px=4.119915,
        realized=-78.13017500000004,
        t_entry_fill="2026-09-02T11:10:19.535053",
        t_first_cross="2026-09-02T11:10:55.220293",
        t_breach_event="2026-09-02T11:10:58.107551",
        t_fill="2026-09-02T11:11:05.383316",
        quote_sources="massive_ws_universe ONLY (727 rows / 20 min ~ 0.6/s). ZERO iqfeed_l1.",
        exit_path=(
            "tighten set the stop ABOVE the last known bid -> instant breach -> "
            "l2_classify 2.07 s on stale_or_missing_l2 -> stand-in x2 pinned to one "
            "quote row -> fill"
        ),
        steps=(
            Step("2026-09-02T11:10:19.535053", "broker entry fill", None, None, "4.34 x 355"),
            Step("2026-09-02T11:10:20.855684", "live_deadman_stop_inert_until_rth", None, None,
                 "stop 4.25 inert premarket"),
            Step("2026-09-02T11:10:54.470721", "tape row (last before tighten)", None, 4.28, ""),
            Step("2026-09-02T11:10:55.220293", "viability_degraded_tighten", None, 4.28,
                 "4.26539 -> 4.31830, i.e. 3.8 c ABOVE the last known bid: "
                 "the stop was breached the instant it was written"),
            Step("2026-09-02T11:10:55.824304", "tape row", None, 4.25, ""),
            Step("2026-09-02T11:10:56.969111", "tape row", None, 4.23, ""),
            Step("2026-09-02T11:10:58.107551", "stop_breach_pending_confirm", 4.28, 4.23,
                 "event reported the 11:10:54.47 bid -- 3.64 s stale"),
            Step("2026-09-02T11:10:58.118887", "tape row", None, 4.12, "arrived 11 ms after the event"),
            Step("2026-09-02T11:11:00.181140", "stop_breach_l2_classify", 4.28, 4.14,
                 "held_s 2.07, reason stale_or_missing_l2, signals n=0, cls BREAKDOWN"),
            Step("2026-09-02T11:11:00.632739", "live_exit_stand_in_pricing", 4.14, 4.14,
                 "defer_count 1, quote age 1.98 s, tape_row_id 188601373"),
            Step("2026-09-02T11:11:00.678258", "live_deadman_stop_release_blocked", None, 4.19,
                 "deadman_successor_intent_frozen_for_next_pulse"),
            Step("2026-09-02T11:11:03.362625", "live_exit_stand_in_pricing", 4.14, 4.15,
                 "defer_count 1, SAME tape_row_id 188601373, age now 4.71 s"),
            Step("2026-09-02T11:11:05.383316", "BROKER exit fill", None, 4.12, "4.119915 x 355"),
            Step("2026-09-02T11:11:05.845232", "live_exit_filled (ledger)", None, None,
                 "ledger saw the fill 0.46 s after the broker booked it"),
        ),
    ),
    Trade(
        key="JLHL", symbol="JLHL", outcome_id=203735, session_id=19463,
        day="2026-09-02", qty=149.0, entry_px=7.396376,
        s_struct=7.21806860125335, s_breach=7.21806860125335,
        bid_cross=7.21, bid_event=7.20, fill_px=7.27,
        realized=-18.830024000000073,
        t_entry_fill="2026-09-02T10:47:33.507131",
        t_first_cross="2026-09-02T10:48:21.181146",
        t_breach_event="2026-09-02T10:48:51.710665",
        t_fill="2026-09-02T10:49:03.476086",
        quote_sources="iqfeed_l1 dense (up to 53 rows/s) + massive_ws_universe ~1/s",
        exit_path=(
            "breach -> chop_hold(1) -> l2_classify x2 -> stand-in x2 -> fill. "
            "THE COUNTER-EXAMPLE: the bid ROSE from 7.20 to 7.27 during the wait, "
            "so the 11.8 s of latency EARNED +10.43 USD."
        ),
        steps=(
            Step("2026-09-02T10:47:33.507131", "broker entry fill", None, None, "7.396376 x 149"),
            Step("2026-09-02T10:48:21.181146", "TAPE first cross of 7.21807", None, 7.21, ""),
            Step("2026-09-02T10:48:51.710665", "stop_breach_pending_confirm", 7.20, 7.20,
                 "30.53 s after the tape first crossed"),
            Step("2026-09-02T10:48:53.547432", "stop_breach_l2_classify", 7.20, None,
                 "held_s 1.84 CHOP/bids_absorbing, did_hold TRUE"),
            Step("2026-09-02T10:48:53.550435", "stop_breach_chop_hold", 7.20, None, "hold_n 1"),
            Step("2026-09-02T10:48:54.633440", "stop_breach_l2_classify", 7.20, None,
                 "held_s 2.92 INCONCLUSIVE/mixed, hold released"),
            Step("2026-09-02T10:48:54.960018", "live_exit_stand_in_pricing", 7.22, 7.22,
                 "defer_count 1, quote age 1.10 s"),
            Step("2026-09-02T10:48:55.003415", "live_deadman_stop_release_blocked", None, None,
                 "deadman_successor_intent_frozen_for_next_pulse"),
            Step("2026-09-02T10:48:59.943501", "live_exit_stand_in_pricing", 7.22, 7.25,
                 "defer_count 1, SAME tape_row_id 188528252, age now 6.08 s"),
            Step("2026-09-02T10:49:03.476086", "BROKER exit fill", None, 7.27, "7.27 x 149"),
            Step("2026-09-02T10:49:05.677374", "live_exit_filled (ledger)", None, None,
                 "ledger saw the fill 2.20 s after the broker booked it"),
        ),
    ),
)

# Legs the ledger never priced, so they cannot be bucketed. Kept here so the
# aggregate can be stated honestly against the broker-truth day total.
UNDECOMPOSABLE = (
    dict(
        key="CANF_c2", symbol="CANF", outcome_id=203734, session_id=19471,
        day="2026-09-02", qty=165.0, entry_px=4.62, fill_px=3.960303,
        broker_pnl=-108.850005,
        t_entry_fill="2026-09-02T11:19:12.223684",
        t_fill="2026-09-02T11:34:31.991961",
        why=(
            "Second entry cycle of session 19471. No live_entry_filled, no "
            "live_deadman_stop_placed, no stop_breach_* and no live_exit_filled "
            "were ever written for it, so there is no stop_price_at_breach and "
            "the A/C/B split is undefined. The runner emitted "
            "live_emergency_exit_unpriced (fill_price null) at 13:20:19 -- one "
            "hour and 46 minutes AFTER the broker had already filled the closing "
            "leg at 11:34:31.99. Surfaced only by the 20:18 "
            "broker_truth_attribution_divergence sweep, which named both legs in "
            "legs_missing_from_ledger. Worth -108.85 USD = 42.7 percent of the "
            "day's -254.78 broker total."
        ),
    ),
)

CSV_FIELDS = [
    "trade", "day", "outcome_id", "session_id", "qty", "entry_px",
    "structural_stop", "breach_stop", "bid_first_cross", "bid_at_breach_event",
    "fill_px", "A_entry_thesis", "C_tighten", "B1_gap_at_cross",
    "B0_detection_lag", "B2_confirm_to_fill", "B_total_breach_to_fill",
    "D_fees_residual", "sum_buckets", "realized_reported", "reconciles",
    "t_entry_fill", "t_first_cross", "t_breach_event", "t_fill",
    "entry_to_cross_s", "cross_to_breach_event_s", "breach_event_to_fill_s",
    "cross_to_fill_s", "B_pct_of_realized", "quote_sources", "exit_path",
]

TIMING_FIELDS = ["trade", "symbol", "session_id", "ts", "t_plus_s_from_cross",
                 "event", "bid_reported_by_runner", "bid_on_tape", "note"]


def rows() -> list[dict]:
    out = []
    for t in TRADES:
        out.append({
            "trade": t.key, "day": t.day, "outcome_id": t.outcome_id,
            "session_id": t.session_id, "qty": t.qty, "entry_px": t.entry_px,
            "structural_stop": round(t.s_struct, 8),
            "breach_stop": round(t.s_breach, 8),
            "bid_first_cross": t.bid_cross, "bid_at_breach_event": t.bid_event,
            "fill_px": t.fill_px,
            "A_entry_thesis": round(t.A, 4), "C_tighten": round(t.C, 4),
            "B1_gap_at_cross": round(t.B1, 4),
            "B0_detection_lag": round(t.B0, 4),
            "B2_confirm_to_fill": round(t.B2, 4),
            "B_total_breach_to_fill": round(t.B, 4),
            "D_fees_residual": round(t.D, 6),
            "sum_buckets": round(t.A + t.C + t.B + t.D, 6),
            "realized_reported": round(t.realized, 6),
            "reconciles": abs((t.A + t.C + t.B + t.D) - t.realized) < 1e-6,
            "t_entry_fill": t.t_entry_fill, "t_first_cross": t.t_first_cross,
            "t_breach_event": t.t_breach_event, "t_fill": t.t_fill,
            "entry_to_cross_s": round(t.secs("t_entry_fill", "t_first_cross"), 3),
            "cross_to_breach_event_s": round(
                t.secs("t_first_cross", "t_breach_event"), 3),
            "breach_event_to_fill_s": round(t.secs("t_breach_event", "t_fill"), 3),
            "cross_to_fill_s": round(t.secs("t_first_cross", "t_fill"), 3),
            "B_pct_of_realized": round(100.0 * t.B / t.realized, 1),
            "quote_sources": t.quote_sources, "exit_path": t.exit_path,
        })
    return out


def timing_rows() -> list[dict]:
    out = []
    for t in TRADES:
        base = _t(t.t_first_cross)
        for s in t.steps:
            out.append({
                "trade": t.key, "symbol": t.symbol, "session_id": t.session_id,
                "ts": s.ts,
                "t_plus_s_from_cross": round((_t(s.ts) - base).total_seconds(), 3),
                "event": s.event,
                "bid_reported_by_runner": s.bid_reported,
                "bid_on_tape": s.bid_tape,
                "note": s.note,
            })
    return out


def assert_reconciles() -> None:
    for t in TRADES:
        got = t.A + t.C + t.B + t.D
        assert abs(got - t.realized) < 1e-6, (t.key, got, t.realized)
        assert abs((t.B1 + t.B0 + t.B2) - t.B) < 1e-9, t.key
        assert abs(t.D) < 1e-6, (t.key, t.D)
    tot = sum(t.A + t.C + t.B + t.D for t in TRADES)
    exp = sum(t.realized for t in TRADES)
    assert abs(tot - exp) < 1e-6, (tot, exp)


def main(argv: list[str]) -> int:
    assert_reconciles()
    out_dir = argv[1] if len(argv) > 1 else "."
    recon_path = f"{out_dir}/2026-09-02_exit-latency-decomposition.csv"
    timing_path = f"{out_dir}/2026-09-02_exit-latency-timing.csv"
    with open(recon_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows())
    with open(timing_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TIMING_FIELDS)
        w.writeheader()
        w.writerows(timing_rows())

    tot = {k: sum(getattr(t, k) for t in TRADES)
           for k in ("A", "C", "B1", "B0", "B2", "B", "D")}
    tot["realized"] = sum(t.realized for t in TRADES)
    print(f"wrote {recon_path}")
    print(f"wrote {timing_path}")
    print("\nAGGREGATE, 4 ledger-priced legs (USD)")
    for k in ("A", "C", "B1", "B0", "B2", "B", "D", "realized"):
        print(f"  {k:9s} {tot[k]:10.4f}")
    print(f"  A+C+B+D = {tot['A'] + tot['C'] + tot['B'] + tot['D']:.6f}"
          f"   realized = {tot['realized']:.6f}")
    print("\nUNDECOMPOSABLE LEGS")
    for u in UNDECOMPOSABLE:
        print(f"  {u['key']}: broker P&L {u['broker_pnl']:.2f} -- {u['why'][:70]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
