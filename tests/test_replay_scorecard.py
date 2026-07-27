"""Pure tests for scripts/replay_scorecard.py (no DB, no network).

Covers the FIFO round-trip pairing, the Label-B window-capture edges (zero-entry,
hi < first_px), the copied Stage-0 expectancy math, the ①②③ verdict mapping via
the operator-tier grade path, and a markdown render smoke."""
from __future__ import annotations

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "replay_scorecard", os.path.join(_ROOT, "scripts", "replay_scorecard.py"))
rs = importlib.util.module_from_spec(_spec)
sys.modules["replay_scorecard"] = rs
_spec.loader.exec_module(rs)


def test_fifo_pairing_basic():
    fills = [{"side": "buy", "qty": 100, "px": 5.00},
             {"side": "buy", "qty": 100, "px": 6.00},
             {"side": "sell", "qty": 150, "px": 7.00},
             {"side": "sell", "qty": 50, "px": 6.50}]
    trades = rs.pair_round_trips(fills)
    assert len(trades) == 2
    # first sell: 100@5 + 50@6 -> entry 5.3333
    assert abs(trades[0]["entry_px"] - 5.3333) < 1e-3
    assert abs(trades[0]["pnl_usd"] - (7.00 - 5.33333) * 150) < 0.5
    # second sell closes the remaining 50@6
    assert trades[1]["qty"] == 50
    assert abs(trades[1]["pnl_usd"] - (6.50 - 6.00) * 50) < 1e-6


def test_fifo_pairing_ignores_unmatched_sell_and_bad_rows():
    trades = rs.pair_round_trips([{"side": "sell", "qty": 100, "px": 5.0},
                                  {"side": "buy", "qty": 0, "px": 5.0},
                                  {"side": "buy", "qty": 10, "px": 0}])
    assert trades == []


def test_window_capture_zero_entry_and_inverted_window():
    rec = {"pnl": 0.0, "market": {"first_px": 5.0, "hi": 7.5}}
    wc = rs.window_capture(rec, [])
    assert wc["entered"] is False
    assert wc["window_capture_ratio"] is None
    assert abs(wc["window_move_frac"] - 0.5) < 1e-9
    # hi < first_px (fade window) -> negative move -> ratio undefined, no crash
    rec2 = {"pnl": -10.0, "market": {"first_px": 5.0, "hi": 4.0}}
    wc2 = rs.window_capture(rec2, [{"qty": 100, "entry_px": 5.0, "exit_px": 4.9,
                                    "pnl_usd": -10.0}])
    assert wc2["entered"] is True
    assert wc2["window_capture_ratio"] is None


def test_window_capture_full_conversion_is_one():
    # buy the exact first px, sell the exact hi with all deployed capital
    rec = {"pnl": 250.0, "market": {"first_px": 5.0, "hi": 7.5}}
    trades = [{"qty": 100, "entry_px": 5.0, "exit_px": 7.5, "pnl_usd": 250.0}]
    wc = rs.window_capture(rec, trades)
    assert abs(wc["window_capture_ratio"] - 1.0) < 1e-9


def test_stage0_stats_parity_fixture():
    # 3 wins (300, 150, 90), 2 losses (-100, -60) -> r_unit = 80
    pnls = [300.0, 150.0, 90.0, -100.0, -60.0]
    s = rs.stage0_stats(pnls)
    assert s["n"] == 5 and s["wins"] == 3 and s["losses"] == 2
    assert abs(s["r_unit"] - 80.0) < 1e-9
    assert abs(s["profit_factor"] - (540.0 / 160.0)) < 1e-9
    # winners in R: 3.75, 1.875, 1.125 -> two >= 1.5R
    assert s["winners_ge_target_r"] == 2
    gates = rs.stage0_gates(s)
    assert gates[0][1] is False       # n < 30
    assert gates[1][1] is True        # PF > 1


def test_verdict_mapping_operator_tier():
    manifest = {"windows": [
        {"manifest_id": "a", "symbol": "AAA", "date": "2026-07-07",
         "expected_action": "trade", "ross_action": "trade", "ross_net_usd": 100.0,
         "pnl_confidence": "frame_verified"},
        {"manifest_id": "b", "symbol": "BBB", "date": "2026-07-07",
         "expected_action": "reject", "ross_action": "no_trade"},
        {"manifest_id": "c", "symbol": "CCC", "date": "2026-07-07",
         "expected_action": "trade", "ross_action": "trade"},
        {"manifest_id": "d", "symbol": "ZZZ", "date": "2026-01-01",
         "expected_action": "trade", "ross_action": "trade"},
    ]}
    records = [
        {"key": "AAA|2026-07-07|base", "symbol": "AAA", "day": "2026-07-07", "pnl": 50.0},
        {"key": "BBB|2026-07-07|base", "symbol": "BBB", "day": "2026-07-07", "pnl": 0.0},
        {"key": "CCC|2026-07-07|base", "symbol": "CCC", "day": "2026-07-07", "pnl": 0.0},
    ]
    trades_by_key = {
        "AAA|2026-07-07|base": [{"pnl_usd": 50.0}],   # traded + positive -> matched_trade
        "BBB|2026-07-07|base": [],                    # no entry on a reject -> matched_reject
        "CCC|2026-07-07|base": [],                    # missed a Ross winner -> missed
    }
    matched, manifest_only = rs.ross_crossref(manifest, records, trades_by_key)
    by_id = {m["manifest_id"]: m for m in matched}
    assert by_id["a"]["grade"] == "matched_trade" and by_id["a"]["verdict"] == "①✅"
    assert by_id["b"]["grade"] == "matched_reject" and by_id["b"]["verdict"] == "②✅"
    assert by_id["c"]["grade"] == "missed_profitable_setup" and by_id["c"]["verdict"] == "①❌"
    assert [w["manifest_id"] for w in manifest_only] == ["d"]


def test_crossref_window_not_covered():
    # Ross premarket window (08:05-08:20 ET = 12:05-12:20 UTC) vs replay 13:00-16:00 UTC
    manifest = {"windows": [
        {"manifest_id": "x", "symbol": "PLSM", "date": "2026-07-13",
         "expected_action": "trade", "ross_action": "trade",
         "window_et": "~08:05-08:20", "ross_net_usd": 2363.44,
         "pnl_confidence": "stated_verbatim"},
    ]}
    records = [{"key": "PLSM|2026-07-13|base", "symbol": "PLSM", "day": "2026-07-13",
                "pnl": 0.0, "win_start": "2026-07-13T13:00:00",
                "win_end": "2026-07-13T16:00:00"}]
    matched, _ = rs.ross_crossref(manifest, records, {"PLSM|2026-07-13|base": []})
    assert matched[0]["grade"] == "window_not_covered"
    assert matched[0]["credit"] is None
    # overlapping window keeps normal grading
    manifest["windows"][0]["window_et"] = "~09:30-10:00"  # 13:30-14:00 UTC — overlaps
    matched2, _ = rs.ross_crossref(manifest, records, {"PLSM|2026-07-13|base": []})
    assert matched2[0]["grade"] == "missed_profitable_setup"


def test_family_buckets():
    assert rs._family("micro_pullback_break_tick") == "micro_pullback"
    assert rs._family("orb_break_tick_ok") == "orb/raw_break"
    assert rs._family("vwap_reclaim") == "vwap/deep_reclaim"
    assert rs._family("flush_dip_reversal") == "flush_dip/wick"
    assert rs._family("inverse_head_shoulders_break") == "ihs/reversal"
    assert rs._family(None) == "unknown"
    assert rs._family("halt_resume_dip") == "halt"


def test_trigger_reasons_from_log(tmp_path):
    log = tmp_path / "w.log"
    log.write_text(
        "  --- ENTRY-DECISION TRACE (ts | event | reason | px) ---\n"
        "    02:29:19 | live_entry_backside_benched      | benched_backside_below_vwap        | None\n"
        "    02:30:31 | live_entry_candidate_detected    | hod_break_tick_ok                  | None\n"
        "    02:30:32 | live_entry_submitted             |                                    | None\n"
        "    02:30:32 | live_entry_filled                |                                    | None\n"
        "    02:31:24 | live_entry_candidate_detected    | abcd_break                         | None\n"
        "    02:31:24 | live_entry_filled                |                                    | None\n",
        encoding="utf-8")
    assert rs.trigger_reasons_from_log(str(log)) == ["hod_break_tick_ok", "abcd_break"]
    assert rs.trigger_reasons_from_log(str(tmp_path / "wala.log")) == []


def test_render_markdown_smoke():
    sc = {"generated_at": "2026-07-26T12:00:00",
          "meta": {"build_sha": "abc123def456", "arm": "base", "sink": "chili_replay2_test",
                   "equity_exec": "driver defaults"},
          "coverage": {"attempted": 2, "ok": 1, "failed": 1, "library": 222},
          "windows": [{"symbol": "AAA", "day": "2026-07-07",
                       "win_start": "2026-07-07T13:00:00", "win_end": "2026-07-07T15:00:00",
                       "class": "gold", "pnl": 12.34, "entries": 1, "exits": 1,
                       "market": {"up_pct": 42.0},
                       "window_capture": {"entered": True, "window_capture_ratio": 0.12},
                       "sink": {"setup_trace": {"trace_alias_counts": {"orb_break": 3}},
                                "top_rejects": [["spread:wide", 10]]}}],
          "errors": [{"key": "B|d|base", "symbol": "BBB", "day": "2026-07-08",
                      "status": "timeout", "exit_code": None, "log": "logs/x.log"}],
          "per_setup": {"orb/raw_break": {"n": 1, "net": 12.34, "wins": 1, "losses": 0}},
          "expectancy": rs.stage0_stats([12.34]),
          "expectancy_gates": rs.stage0_gates(rs.stage0_stats([12.34])),
          "label_a": {"n": 0, "clamped": 0, "mean_capture": 0.0, "mean_giveback": 0.0,
                      "outcome_classes": {}},
          "crossref": [], "manifest_only": []}
    md = rs.render_markdown(sc)
    assert "Golden-library replay baseline scorecard" in md
    assert "AAA" in md and "orb/raw_break" in md
    assert "deltas" in md.lower()
    assert "timeout" in md
