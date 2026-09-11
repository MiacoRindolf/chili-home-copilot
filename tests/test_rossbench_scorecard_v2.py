"""scripts/rossbench_scorecard_v2.py -- the [E] A/B scorer, DB-free (a stub tape).

Pins: FIFO legs keep the WHOLE leg's realised P&L (partials included -- the scratchpad v2
kept only the closing sale's); each leg is attributed to the ``live_exit_filled`` that closed
it, raw reason; EXIT / MOVE capture are dollar-weighted ratios of sums; a ``--same-window``
group counts once; the verdict histogram and sizing binding are lifted verbatim.

Runnable: pytest tests/test_rossbench_scorecard_v2.py -v
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

import rossbench_scorecard_v2 as S  # noqa: E402


class _Tape:
    """hi/lo = fixed per (a, b) span: leg highs from a table, window from the receipt env."""

    def __init__(self, legs, window):
        self._legs = legs
        self._window = window

    def hilo(self, sym, a, b):
        key = (str(a), str(b))
        if key in self._legs:
            return self._legs[key], None
        return self._window


def _receipt(fills, exits, *, pnl, hist=None, sizing=None):
    events = [{"ts": ts, "event_type": "live_exit_filled", "payload": {"reason": r, "fill_price": px}}
              for ts, r, px in exits]
    for sz in sizing or []:
        events.append({"ts": "2026-07-13 12:00:00", "event_type": "live_entry_submitted",
                       "payload": {"sizing": sz[0], "risk_mults": sz[1]}})
    return {
        "env": {"WIN_START": "2026-07-13T12:00:00", "WIN_END": "2026-07-13T13:00:00"},
        "fills": [{"ts": ts, "side": side, "px": px, "qty": q} for ts, side, px, q in fills],
        "events": events,
        "event_histogram": hist or {},
        "pnl_usd": pnl,
        "tree": {"head": "abc"},
    }


def test_legs_keep_partials_and_adds_and_the_closing_reason():
    r = _receipt(
        fills=[("2026-07-13 12:01:00", "buy", 10.0, 100), ("2026-07-13 12:02:00", "buy", 10.5, 100),
               ("2026-07-13 12:03:00", "sell", 11.0, 50), ("2026-07-13 12:04:00", "sell", 10.8, 150),
               ("2026-07-13 12:10:00", "buy", 12.0, 100), ("2026-07-13 12:11:00", "sell", 11.9, 100)],
        exits=[("2026-07-13 12:04:00.5", "tape_accel_rollover", 10.8),
               ("2026-07-13 12:11:00.2", "tick_deadman_stop", 11.9)],
        pnl=0.0,
    )
    L = S.legs(r)
    S.attach_exit_reasons(r, L)
    assert [x["adds"] for x in L] == [1, 0] and [x["parts"] for x in L] == [1, 0]
    # avg 10.25: 50 x 0.75 + 150 x 0.55 = 37.5 + 82.5 = 120.0 (the partial is NOT dropped)
    assert L[0]["pnl"] == pytest.approx(120.0)
    assert L[1]["pnl"] == pytest.approx(-10.0)
    assert [x["reason"] for x in L] == ["tape_accel_rollover", "tick_deadman_stop"]


def test_capture_is_a_ratio_of_sums_and_the_split_is_by_raw_reason():
    r = _receipt(
        fills=[("2026-07-13 12:01:00", "buy", 10.0, 100), ("2026-07-13 12:05:00", "sell", 11.0, 100),
               ("2026-07-13 12:10:00", "buy", 10.0, 100), ("2026-07-13 12:12:00", "sell", 9.5, 100)],
        exits=[("2026-07-13 12:05:00", "trail_stop", 11.0), ("2026-07-13 12:12:00", "stop", 9.5)],
        pnl=50.0,
        hist={"live_exit_verdict_armed": 2, "live_exit_verdict_fired": 1},
        sizing=[({"notional_ceiling_source": "replay_equity_seam:broker_multiplier_pinned",
                  "notional_ceiling_binding": "allocation", "risk_usd": 48.57},
                 {"base_max_loss": 390.0, "realized_over_base": 0.1245})],
    )
    tape = _Tape({("2026-07-13 12:01:00", "2026-07-13 12:05:00"): 12.0,
                  ("2026-07-13 12:10:00", "2026-07-13 12:12:00"): 10.5}, (12.0, 9.0))
    c = S.score_case("VEEE@x-ml1_2026-07-13", r, tape)
    agg = S.aggregate([c])
    # EXIT: (100 - 50) / (200 + 50) ; MOVE: 50 / ((12 - 9) x 100)
    assert agg["exit_capture_pct"] == pytest.approx(20.0)
    assert agg["move_capture_pct"] == pytest.approx(16.7)
    assert agg["exit_reasons"]["trail_stop"] == {"n": 1, "pnl_usd": 100.0, "exit_capture_pct": 50.0}
    assert agg["exit_reasons"]["stop"]["pnl_usd"] == -50.0
    assert list(agg["exit_reasons"]) == ["trail_stop", "stop"]      # REASON_ORDER
    assert agg["verdict_hist"]["live_exit_verdict_fired"] == 1
    assert agg["ceiling_sources"] == {"replay_equity_seam:broker_multiplier_pinned": 1}
    assert agg["risk_over_base_mean"] == pytest.approx(0.1245)


def test_a_same_window_group_counts_once_and_says_if_it_was_fill_identical():
    base = dict(fills=[("t1", "buy", 1.0, 1), ("t2", "sell", 2.0, 1)], exits=[], pnl=10.0)
    cases = {
        "VEEE@x-ml1": {"pnl_usd": 10.0, "fills_fingerprint": "f"},
        "VEEE@x-ml2": {"pnl_usd": 10.0, "fills_fingerprint": "f"},
        "VEEE@x-ml3": {"pnl_usd": 40.0, "fills_fingerprint": "g"},
        "CLRO@y-ml1": {"pnl_usd": 5.0, "fills_fingerprint": "h"},
    }
    total, notes = S.dedupe(cases, [["VEEE@x-ml1", "VEEE@x-ml2", "VEEE@x-ml3"]])
    assert total == pytest.approx(20.0 + 5.0)
    assert notes[0]["fill_identical"] is False and notes[0]["counted_as"] == 20.0
    assert base["pnl"] == 10.0


def test_load_runs_carries_the_bench_s_own_scoreable_verdict(tmp_path):
    """An unscoreable run (the bench's post-run invariants, e.g. cold start) is flagged, so the
    A/B can compare only the cases every arm scored -- beside, never instead of, the raw total."""
    import json as _json

    bench = tmp_path / "E_ab_A_x13_INLF"
    run_dir = bench / "INLF@1ml6zHikpsE-t1_2026-07-28" / "canon"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(_json.dumps({"pnl_usd": -43.61, "fills": []}), encoding="utf-8")
    (bench / "bench.json").write_text(_json.dumps({"runs": [{
        "out_dir": str(run_dir), "scoreable": False,
        "invariant_problems": ["cold_start:live_entry_candidate_detected at runner tick 1 < 6"]}]}),
        encoding="utf-8")
    runs = S.load_runs(str(bench))
    doc = runs["INLF@1ml6zHikpsE-t1_2026-07-28"]
    assert doc["_scoreable"] is False and "cold_start" in doc["_problems"][0]
