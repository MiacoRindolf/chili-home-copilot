"""Tests for the Ross Parity Bench reporter (scripts/rossbench_report.py, STEP 12).

PURE: no database, no network, no subprocess. Every test builds a synthetic bench tree on
``tmp_path`` and drives the reporter's own functions, so this file is safe to run beside a
live lane.

What is being defended, and why each test exists:

* The three refusal stamps (``DIAGNOSTIC_ONLY`` / ``causal_use_allowed: false`` /
  ``admission_claim: false``) and the two limitations are STRUCTURAL properties of the Tier-1
  harness. If any of them can be made to disappear by the shape of a particular run, a reader
  will eventually see a report without them and read the numbers as an admission claim.
* ``0`` is a NULL SENTINEL on the Ross side of the ledger (measured: pnl_usd 30 zeros of 187
  rows) and a MEASUREMENT on the CHILI side. A report that renders a Ross 0 as ``$0.00``
  silently enters it into Avoidance ("CHILI >= 0") and Capture.
* The reporter must invent NOTHING: no parity metric of its own, no knob derivation, no Ross
  equity, no stage. Several tests assert the reporter *refuses* rather than that it computes.

Runnable: pytest tests/test_rossbench_report.py -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "rossbench_report", os.path.join(_REPO, "scripts", "rossbench_report.py")
)
rr = importlib.util.module_from_spec(_SPEC)
sys.modules["rossbench_report"] = rr
assert _SPEC.loader is not None
_SPEC.loader.exec_module(rr)


# ─────────────────────────────────────────────────────────────────────────────
# FIXTURE BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def make_receipt(
    symbol="SDOT",
    win_start="2026-06-26T13:30:00",
    win_end="2026-06-26T14:30:00",
    pnl=-11.37,
    *,
    depth_rows=0,
    entries=1,
    equity=13000.0,
    tree_head="abc123",
    nbbo_sources=None,
    mock=None,
    events=None,
    dirty=False,
):
    """A driver receipt shaped exactly like scripts/replay_v3_fsm_window.py:1140-1183.

    The key set here was read off that emission site; if the driver's receipt changes, this
    fixture is where the reporter's assumptions are re-checked.
    """
    return {
        "schema": rr.DRIVER_RECEIPT_SCHEMA,
        "label": symbol,
        "arm": "g4_on",
        "g4_on": True,
        "generated_at_utc": "2026-09-04T00:00:00+00:00",
        "tree": {"head": tree_head, "tree": "t" + tree_head,
                 "branch": "seam/rossbench-instrument-0904", "dirty": dirty},
        "env": {
            "SYMBOL": symbol, "WIN_START": win_start, "WIN_END": win_end,
            "OHLCV_START": win_start, "FRAME_WARMUP_MIN": 120, "FRAME_START": win_start,
            "TICK_STRIDE": 1, "GRID_STEP_S": 1.0, "EQUITY": equity, "RISK": 130.0,
            "SOURCE_FILTER": ["iqfeed_lookup_hist"], "EXEC_FAMILY": "alpaca_spot",
            "BENCH_QUESTION": "ross bench", "FULL_MIRROR": "1", "ARM": "on",
            "MAXLOSS_USD": None, "GRIND_FIX": None, "REPLAY_KEEP_SINK": None,
            "PROD_DB": "chili_hydrated", "SIM_DB": "chili_rossbench_test",
        },
        "tape_sources": {
            "iqfeed_trade_ticks": {"iqfeed_lookup_hist": 155059},
            "momentum_nbbo_spread_tape": (nbbo_sources if nbbo_sources is not None
                                          else {"iqfeed_lookup_bbo": 41000}),
        },
        "sink_reset": {"database": "chili_rossbench_test",
                       "cleaned": ["trading_automation_events"], "suspended": [["t", "g"]]},
        "mirrored": {"tick_rows": 155059, "nbbo_rows": 41000, "depth_rows": depth_rows},
        "density": {"mirror_span_seconds": 3600.0, "window_seconds": 3600.0,
                    "ticks_per_second": 43.0, "nbbo_rows_per_second": 11.4,
                    "depth_rows_per_second": 0.0, "grid_steps_per_second": 1.0},
        "grid_steps": 3600,
        "mock": mock if mock is not None else {
            "resting_limit_fills": True, "volume_cap_enabled": True,
            "fill_mode": "conservative", "freshness_mode": "wall"},
        "seed_session_id": 9198, "execution_family": "alpaca_spot", "venue": "alpaca",
        "economic_seed_mode": None, "certification_eligible": False,
        "certification_failures": ["entry_risk_gate_bypassed"],
        "final_state": "live_finished", "states_visited": ["armed"],
        "fills": [], "pnl_usd": pnl, "mtm_usd": 0.0, "net_open_shares": 0.0,
        "cost_usd": 0.0, "proceeds_usd": 0.0, "entries": entries, "exits": entries,
        "event_histogram": {},
        "events": events if events is not None else [
            {"ts": "2026-06-26T13:35:00", "event_type": "live_entry_filled", "payload": {}},
        ],
    }


def write_json(path, doc):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2)


def build_tree(tmp_path, *, cases=None, ross_pnl=1830.0, pin_confidence="tape_confirmed",
               account="big", arms=("base", "lever_off"), depth_rows=0):
    """A minimal but complete run tree + manifest + pins. Returns (run_dir, manifest, pins)."""
    run_dir = tmp_path / "run"
    for arm in arms:
        write_json(run_dir / "SDOT_2026-06-26" / arm / "run.json",
                   make_receipt(pnl=(-11.37 if arm == "base" else 42.10),
                                depth_rows=depth_rows))
        with open(str(run_dir / "SDOT_2026-06-26" / arm / "timeline.jsonl"),
                  "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"t": "13:35", "code_ref": "not_the_divergence.py:1"}) + "\n")
            fh.write(json.dumps({"t": "13:36", "first_divergence": True,
                                 "code_ref": "live_runner.py:33927"}) + "\n")
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {
        "schema": "chili.ross_ground_truth_manifest.v1",
        "generated_at": "2026-09-04T00:00:00+00:00",
        "windows": cases if cases is not None else [{
            "manifest_id": "v::SDOT::2026-06-26::t1", "symbol": "SDOT", "date": "2026-06-26",
            "account": account, "expected_action": "trade",
            "stated_entry_et": "09:35", "stated_exit_et": "09:52",
            "ross_net_usd": ross_pnl, "pnl_confidence": "stated_verbatim",
            "xref_verdict": "armed_no_entry",
        }],
    })
    pins = tmp_path / "pins.json"
    write_json(pins, {
        "schema": "chili.ross_event_pins.v1",
        "pins": [{"symbol": "SDOT", "date": "2026-06-26", "pin_method": "price_match",
                  "pin_confidence": pin_confidence, "entry_pin_et": "09:35:12"}],
    })
    return str(run_dir), str(manifest), str(pins)


class StubScorer:
    """Minimal stand-in for STEP 11 with the VERIFIED signatures.

    ``classify_first_divergence(case, recorded_events, replay_events, window) -> (rec, rep)``
    and ``ross_parity_index(cases, equity: Mapping) -> dict``. The real scorer raises
    ``TypeError`` on a scalar equity (ross_bench_scoring.py:900-906); the stub asserts the
    same thing, so a reporter regression that starts passing a scalar fails here too.
    """

    def __init__(self):
        self.calls = []
        self.equity_args = []

    @staticmethod
    def normalize_account(value):
        if value is None:
            return "unknown"
        text = str(value).strip().lower()
        return {"big": "main", "main": "main", "small": "small"}.get(text, "unknown")

    def classify_first_divergence(self, case, recorded_events, replay_events, window):
        self.calls.append((case, list(recorded_events), list(replay_events), window))
        return ("armed_no_candidate", "filled_exited_worse")

    def ross_parity_index(self, cases, equity, **kwargs):
        if not isinstance(equity, dict):
            raise TypeError("equity must be a Mapping of account -> USD")
        self.equity_args.append(dict(equity))
        return {
            "schema": "chili.ross_parity_index.v1", "tier": 1, "case_count": len(cases),
            "blended_score": None, "blended_score_note": "not combined",
            "capture": {"by_account": {"main": {
                "numerator_usd": -11.37, "denominator_usd": 1830.0, "ratio": -0.0062,
                "equity_normalized": None, "cases": [{"case_id": "SDOT_2026-06-26"}]}},
                "pooled": None, "rule": "capture rule"},
            "avoidance": {"numerator": 0, "denominator": 0, "ratio": None,
                          "cases": [], "rule": "avoidance rule"},
            "precision": {"numerator": 0, "denominator": 1, "ratio": 0.0,
                          "cases": [{"case_id": "SDOT_2026-06-26"}], "rule": "precision rule"},
            "liveness": {"value": None, "numerator": None, "denominator": None,
                         "cases": [], "reason": "tier2_required", "rule": "liveness rule"},
            "recorded_liveness": {"numerator": 1, "denominator": 1, "ratio": 1.0,
                                  "cases": [], "rule": "recorded liveness rule"},
            "excluded_cases": [],
        }


class EmptyScorer:
    """A scorer exposing neither entry point — the reporter must refuse, not improvise."""


# ─────────────────────────────────────────────────────────────────────────────
# 1. THE REFUSAL STAMPS ARE UNCONDITIONAL
# ─────────────────────────────────────────────────────────────────────────────

def test_refusal_stamps_are_on_every_document_and_every_report(tmp_path):
    """The Tier-1 harness fakes admission, so no run shape may drop the stamps.

    They are asserted on BOTH artefacts because a reader reaches for report.md and a tool
    reaches for rpi.json, and a stamp that survives in only one of them protects only one.

    ``evidence_grade`` is deliberately NOT asserted here: it is derived, not structural, and
    the tests for it live in section 1b.
    """
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base",
                            account_normalizer=StubScorer.normalize_account)
    doc, md = rr.build_report(bundle, scorer=StubScorer())

    assert doc["causal_use_allowed"] is False
    assert doc["admission_claim"] is False
    assert "`causal_use_allowed`: **false**" in md
    assert "`admission_claim`: **false**" in md
    # The stamps must be at the TOP: a refusal a reader meets after the numbers is late.
    assert md.index("admission_claim") < md.index("Per-case results")


def test_stamps_survive_a_scorer_that_produces_nothing(tmp_path):
    """A degraded run is exactly when a reader is most likely to over-read the output."""
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=EmptyScorer())
    assert doc["admission_claim"] is False
    assert "`admission_claim`: **false**" in md


# ─────────────────────────────────────────────────────────────────────────────
# 1b. THE EVIDENCE GRADE IS DERIVED, NOT DECLARED
# ─────────────────────────────────────────────────────────────────────────────
# It used to be the constant "DIAGNOSTIC_ONLY", asserted on every document by every run —
# a provenance stamp that no check could ever contradict. These tests exist to make that
# regression impossible: each one arranges a state in which the OLD constant would still
# have printed DIAGNOSTIC_ONLY and asserts the derived answer instead.

class StubGrader:
    """Minimal stand-in for ross_replay_benchmark with the VERIFIED signatures.

    ``diagnostic_coverage_from_replay_receipt(receipt, *, label_id, naive_clock_tz,
    allow_dirty_tree)`` and ``grade_recap_phase_window(*, label_id, symbol,
    expected_action, trades, phase_window, replay_coverage, diagnostic_coverage)``, both
    read off ross_replay_benchmark.py. ``tier`` is what a graded row comes back as.
    """

    class ReplayTradeObservation:
        def __init__(self, symbol, entry_ts, exit_ts, pnl_usd=None, pnl_r=None):
            self.symbol, self.entry_ts, self.exit_ts = symbol, entry_ts, exit_ts
            self.pnl_usd, self.pnl_r = pnl_usd, pnl_r

        def valid(self):
            return bool(self.symbol and self.entry_ts and self.exit_ts
                        and self.exit_ts >= self.entry_ts)

    class _Grade:
        def __init__(self, tier, reasons=()):
            self.label_id = "L"
            self.symbol = "SDOT"
            self.evidence_grade = tier
            self.matching_trade_count = 1
            self.aggregate_pnl_usd = -11.37
            self.coverage_reasons = tuple(reasons)
            self.diagnostic_provenance = {"tick_stride": 1}
            self.grade = type("G", (), {"status": "matched_trade", "credit": 1.0,
                                        "expected_action": "trade",
                                        "actual_action": "trade", "reason": "ok"})()

    def __init__(self, tier="diagnostic_only", coverage_raises=None):
        self.tier = tier
        self.coverage_raises = coverage_raises
        self.calls = []

    def diagnostic_coverage_from_replay_receipt(self, receipt, *, label_id,
                                                naive_clock_tz=None, allow_dirty_tree=False):
        if self.coverage_raises:
            raise ValueError(self.coverage_raises)
        if receipt.get("tree", {}).get("dirty") and not allow_dirty_tree:
            raise ValueError("replay receipt reports a dirty working tree")
        return {"label_id": label_id, "tz": naive_clock_tz}

    def grade_recap_phase_window(self, *, label_id, symbol, expected_action, trades,
                                 phase_window, replay_coverage=None,
                                 diagnostic_coverage=None, **kwargs):
        self.calls.append({"label_id": label_id, "symbol": symbol,
                           "expected_action": expected_action, "trades": list(trades),
                           "phase_window": phase_window,
                           "replay_coverage": replay_coverage,
                           "diagnostic_coverage": diagnostic_coverage})
        if phase_window is None or diagnostic_coverage is None:
            return StubGrader._Grade("unscorable", ("phase_window_missing_or_unverified",))
        return StubGrader._Grade(
            self.tier, ("diagnostic_only:sealed_decision_coverage_not_bound",))


class StubAdapter:
    """Stand-in for ross_manifest_adapter: one AdaptedCase per manifest window.

    Field names are the real dataclass's (``label_id`` / ``symbol`` / ``trade_date`` /
    ``window`` / ``unscorable_reasons`` / ``scorable``) — the reporter reads them by those
    names, so a rename in the adapter must break this stub too.
    """

    class _Window:
        def __init__(self, label_id, symbol):
            self.label_id, self.symbol = label_id, symbol
            self.start_ts = self.decision_ts = self.end_ts = None
            self.evidence_source = "tape"
            self.independently_verified = True

    class _Case:
        def __init__(self, row, scorable):
            self.label_id = row.get("manifest_id")
            self.symbol = str(row.get("symbol") or "").upper()
            self.trade_date = str(row.get("date") or "")
            self.scorable = scorable
            self.window = (StubAdapter._Window(self.label_id, self.symbol)
                           if scorable else None)
            self.unscorable_reasons = () if scorable else ("pin_unpinned",)

    def __init__(self, scorable=True):
        self.scorable = scorable

    def phase_windows_from_manifest(self, manifest, pins):
        return [StubAdapter._Case(r, self.scorable)
                for r in (manifest.get("windows") or [])]

    @staticmethod
    def adaptation_summary(cases):
        return {"case_count": len(cases),
                "scorable_count": sum(1 for c in cases if c.scorable),
                "unscorable_count": sum(1 for c in cases if not c.scorable),
                "unscorable_reason_counts": {"pin_unpinned":
                                             sum(1 for c in cases if not c.scorable)}}


def test_no_grader_means_unscorable_not_a_borrowed_diagnostic_stamp(tmp_path):
    """The exact regression: with nothing to check the grade, the grade is unscorable.

    The old constant printed DIAGNOSTIC_ONLY here, which asserted a tier that no coverage
    record and no phase window had ever been tested against.
    """
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=StubScorer(), grader=None)
    assert doc["evidence_grade"] == "UNSCORABLE"
    assert doc["evidence_grade_grader_enum"] == "unscorable"
    assert any("nothing checked it" in r for r in doc["evidence_grade_derivation"]["reasons"])
    assert "DIAGNOSTIC_ONLY" not in md


def test_a_graded_row_earns_the_stamp_and_the_counts_travel_with_it(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=StubAdapter())
    doc, md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader())
    assert doc["evidence_grade"] == "DIAGNOSTIC_ONLY"
    assert doc["evidence_grade_derivation"]["counts"] == {"diagnostic_only": 2}
    assert doc["evidence_grade_derivation"]["graded_rows"] == 2
    # A stamp without its sample size reads as "every row"; the counts must be printed.
    assert "`diagnostic_only` × 2" in md
    assert "DERIVED, not declared" in md


def test_a_window_the_adapter_refused_grades_unscorable(tmp_path):
    """No grading window -> no tier. The adapter owns that rule; the reporter obeys it."""
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base",
                            adapter=StubAdapter(scorable=False))
    doc, _md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader())
    assert doc["evidence_grade"] == "UNSCORABLE"
    assert doc["cases"][0]["phase_window"] is None
    assert doc["cases"][0]["phase_window_reasons"] == ["pin_unpinned"]
    assert doc["coverage_reason_counts"]["phase_window_missing_or_unverified"] == 2


def test_a_dirty_tree_is_refused_coverage_by_default_and_the_row_grades_unscorable(tmp_path):
    """tree.dirty means tree.tree does not describe the code that ran.

    Fails closed by default — the grader's own documented default — and the refusal is
    NAMED on the case rather than swallowed.
    """
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "base", "run.json"),
               make_receipt(pnl=-11.37, tree_head="dirty1", dirty=True))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=StubAdapter())
    doc, _md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader())
    grades = doc["cases"][0]["arms"]
    assert grades["base"]["evidence_grade"] == "unscorable"
    assert grades["lever_off"]["evidence_grade"] == "diagnostic_only"
    assert any("dirty" in p for p in doc["cases"][0]["coverage_problems"])


def test_allow_dirty_tree_is_recorded_in_the_document_not_just_honoured(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "base", "run.json"),
               make_receipt(pnl=-11.37, dirty=True))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=StubAdapter())
    doc, md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader(),
                              allow_dirty_tree=True)
    assert doc["allow_dirty_tree"] is True
    assert doc["cases"][0]["arms"]["base"]["evidence_grade"] == "diagnostic_only"
    assert "`allow_dirty_tree`: **true**" in md


def test_the_reporter_never_supplies_a_sealed_coverage_record(tmp_path):
    """``certified`` must be unreachable from this pipeline, and the document must say so.

    Passing a ValidatedReplayCoverage the bench cannot substantiate would let hydrated tape
    launder itself into a certified benchmark result.
    """
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=StubAdapter())
    grader = StubGrader()
    doc, md = rr.build_report(bundle, scorer=StubScorer(), grader=grader)
    assert all(c["replay_coverage"] is None for c in grader.calls)
    assert doc["evidence_grade_derivation"]["sealed_coverage_supplied"] is False
    assert "certified' is therefore unreachable" in md


def test_the_grading_window_is_joined_by_manifest_id_not_by_symbol_day(tmp_path):
    """The manifest fans one symbol-day out to several windows with different clocks.

    A symbol-day join would grade a run against another leg's window. The join key is the
    manifest_id of the SAME row the report's ground truth came from.
    """
    rows = [
        {"manifest_id": "v::SDOT::2026-06-26::t1", "symbol": "SDOT", "date": "2026-06-26",
         "account": "big", "expected_action": "trade", "stated_entry_et": "09:35",
         "ross_net_usd": 1830.0},
        {"manifest_id": "v::SDOT::2026-06-26::t2", "symbol": "SDOT", "date": "2026-06-26",
         "account": "big", "expected_action": "trade", "stated_entry_et": "10:35",
         "ross_net_usd": 42.0},
    ]
    run_dir, manifest, pins = build_tree(tmp_path, cases=rows)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=StubAdapter())
    grader = StubGrader()
    rr.build_report(bundle, scorer=StubScorer(), grader=grader)
    assert bundle.cases[0].manifest_id == "v::SDOT::2026-06-26::t1"
    assert {c["label_id"] for c in grader.calls} == {"v::SDOT::2026-06-26::t1"}


def test_a_sibling_window_that_would_grade_better_is_named_not_used(tmp_path):
    """Refusing to shop for a window is the point; saying so is what makes it actionable."""
    rows = [
        {"manifest_id": "v::SDOT::2026-06-26::t1", "symbol": "SDOT", "date": "2026-06-26",
         "account": "big", "expected_action": "trade", "ross_net_usd": 1830.0},
        {"manifest_id": "v::SDOT::2026-06-26::t2", "symbol": "SDOT", "date": "2026-06-26",
         "account": "big", "expected_action": "trade", "ross_net_usd": 42.0},
    ]

    class _Mixed(StubAdapter):
        def phase_windows_from_manifest(self, manifest, pins):
            out = []
            for r in manifest.get("windows") or []:
                out.append(StubAdapter._Case(r, r["manifest_id"].endswith("t2")))
            return out

    run_dir, manifest, pins = build_tree(tmp_path, cases=rows)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=_Mixed())
    doc, _md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader())
    assert doc["cases"][0]["phase_window"] is None
    assert any("t2" in n and "grade best" in n for n in doc["cases"][0]["notes"])


def test_fills_become_one_observation_carrying_the_drivers_own_pnl(tmp_path):
    """The reporter must not re-pair buys to sells — that is the driver's arithmetic."""
    receipt = make_receipt(pnl=-11.37)
    receipt["fills"] = [
        {"ts": "2026-06-26 13:35:00", "side": "BUY", "px": 3.1, "qty": 100},
        {"ts": "2026-06-26 13:41:00", "side": "SELL", "px": 3.0, "qty": 100},
        {"ts": "2026-06-26 13:52:00", "side": "BUY", "px": 3.4, "qty": 50},
        {"ts": "2026-06-26 14:01:00", "side": "SELL", "px": 3.5, "qty": 50},
    ]
    obs, note = rr.replay_trade_observations(receipt, StubGrader(),
                                            naive_clock_tz=rr.parse_clock_tz("utc"))
    assert len(obs) == 1
    assert obs[0].entry_ts.isoformat() == "2026-06-26T13:35:00+00:00"
    assert obs[0].exit_ts.isoformat() == "2026-06-26T14:01:00+00:00"
    assert obs[0].pnl_usd == -11.37       # the receipt's number, not a re-derived one
    assert "folded into one observation" in note


def test_a_receipt_with_no_fills_yields_no_observation(tmp_path):
    receipt = make_receipt(pnl=0.0)
    receipt["fills"] = []
    obs, note = rr.replay_trade_observations(receipt, StubGrader(),
                                            naive_clock_tz=rr.parse_clock_tz("utc"))
    assert obs == []
    assert note is None


def test_an_unparseable_fill_clock_is_counted_not_silently_dropped(tmp_path):
    receipt = make_receipt(pnl=1.0)
    receipt["fills"] = [{"ts": "not a time", "side": "BUY"},
                        {"ts": "2026-06-26 13:35:00", "side": "SELL"}]
    obs, note = rr.replay_trade_observations(receipt, StubGrader(),
                                            naive_clock_tz=rr.parse_clock_tz("utc"))
    assert len(obs) == 1
    assert "1 fill(s) had no parseable ts" in note


def test_the_receipt_clock_timezone_is_stated_and_never_guessed(tmp_path):
    """The grader refuses a naive clock rather than assuming one; so does this."""
    assert rr.parse_clock_tz("utc") is not None
    assert rr.parse_clock_tz(None) is not None
    with pytest.raises(SystemExit):
        rr.parse_clock_tz("Mars/Olympus_Mons")


def test_receipt_clock_tz_and_its_basis_reach_the_document(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=StubAdapter())
    doc, _md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader(),
                               naive_clock_tz=rr.parse_clock_tz("utc"))
    assert "UTC" in str(doc["receipt_clock_tz"])
    # Cited by SYMBOL, not by line: the runner is under active edit and et_clock_to_utc has
    # already moved once. A basis that rots is a basis a reader stops checking.
    assert "et_clock_to_utc" in doc["receipt_clock_tz_basis"]
    assert "replay_v3_fsm_window.py:154-156" in doc["receipt_clock_tz_basis"]


def test_an_unavailable_adapter_is_reported_and_grades_unscorable(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=None,
                            adapter_problem="adapter module unavailable (ImportError)")
    doc, md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader())
    assert doc["adaptation"]["available"] is False
    assert doc["evidence_grade"] == "UNSCORABLE"
    assert "The manifest adapter was not available" in md


def test_an_adapter_that_raises_does_not_lose_the_report(tmp_path):
    class _Exploding:
        @staticmethod
        def phase_windows_from_manifest(manifest, pins):
            raise RuntimeError("adapter boom")

    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=_Exploding())
    assert any("adapter boom" in w for w in bundle.load_warnings)
    doc, _md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader())
    assert doc["evidence_grade"] == "UNSCORABLE"


def test_the_adaptation_denominator_is_the_whole_manifest_not_the_run(tmp_path):
    """"n of m windows carry a grading window" is a corpus fact, not a run fact."""
    rows = [
        {"manifest_id": "v::SDOT::2026-06-26::t1", "symbol": "SDOT", "date": "2026-06-26",
         "account": "big", "expected_action": "trade", "ross_net_usd": 1830.0},
        {"manifest_id": "v::ZZZZ::2026-06-26::t1", "symbol": "ZZZZ", "date": "2026-06-26",
         "account": "big", "expected_action": "trade", "ross_net_usd": 10.0},
    ]
    run_dir, manifest, pins = build_tree(tmp_path, cases=rows)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=StubAdapter())
    doc, md = rr.build_report(bundle, scorer=StubScorer(), grader=StubGrader())
    assert doc["adaptation"]["summary"]["case_count"] == 2      # ZZZZ was never benched
    assert len(doc["cases"]) == 1
    assert "**2 of 2** manifest windows produced one" in md


def test_an_expected_action_outside_trade_reject_is_refused_not_graded(tmp_path):
    """``grade_recap_decision`` RAISES on anything else, so this is a refusal by contract."""
    rows = [{"manifest_id": "v::SDOT::2026-06-26::t1", "symbol": "SDOT",
             "date": "2026-06-26", "account": "big", "expected_action": None,
             "ross_net_usd": 1830.0}]
    run_dir, manifest, pins = build_tree(tmp_path, cases=rows)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base", adapter=StubAdapter())
    grader = StubGrader()
    doc, _md = rr.build_report(bundle, scorer=StubScorer(), grader=grader)
    assert grader.calls == []
    assert doc["cases"][0]["arms"]["base"]["evidence_grade"] == "unscorable"
    assert any("expected_action" in p for p in doc["cases"][0]["coverage_problems"])


# ─────────────────────────────────────────────────────────────────────────────
# 2. THE TWO LIMITATIONS PRINT ON EVERY REPORT
# ─────────────────────────────────────────────────────────────────────────────

def test_both_limitations_print_above_the_results(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    _doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert "Limitations that apply to every line below" in md
    assert "isolated_single_symbol" in md
    assert "depth_levers_unmeasurable" in md
    assert md.index("Limitations that apply") < md.index("Per-case results")


def test_leader_board_limitation_survives_an_empty_scorer(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    _doc, md = rr.build_report(bundle, scorer=EmptyScorer())
    assert "isolated_single_symbol" in md


def test_depth_limitation_is_measured_from_receipts_not_asserted():
    """``depth_levers_unmeasurable`` reads ``mirrored.depth_rows``; it is not a fixed sentence.

    Both directions matter: an all-zero run must say UNMEASURABLE (so nobody reads a 0.00
    delta as a measured lever result), and a run that DID mirror depth must not be mislabelled
    by a stale claim.
    """
    unmeasurable, text = rr.limitation_depth({"a/base": 0, "b/base": 0})
    assert unmeasurable is True
    assert "UNMEASURABLE" in text
    assert "0.00" in text  # names the exact confusion it is preventing

    measured, text2 = rr.limitation_depth({"a/base": 12, "b/base": 0})
    assert measured is False
    assert "12 depth row" in text2

    # No receipt at all: still unmeasurable, but the basis is ABSENCE, and the text must not
    # claim a measured zero it never took.
    none_at_all, text3 = rr.limitation_depth({})
    assert none_at_all is True
    assert "ABSENCE" in text3
    assert "Measured across" not in text3


def test_depth_flag_reaches_the_document_and_the_report(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, depth_rows=0)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert doc["depth_levers_unmeasurable"] is True
    assert "depth_levers_unmeasurable: true" in md

    run_dir2, manifest2, pins2 = build_tree(tmp_path / "b", depth_rows=500)
    bundle2 = rr.load_bundle(run_dir2, manifest2, pins2, base_arm="base")
    doc2, _md2 = rr.build_report(bundle2, scorer=StubScorer())
    assert doc2["depth_levers_unmeasurable"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 3. THE 0-SENTINEL RULE
# ─────────────────────────────────────────────────────────────────────────────

def test_ross_zero_pnl_is_absent_not_a_flat(tmp_path):
    """Measured on the ledger: 30 of 187 trade rows carry ``pnl_usd: 0`` as "not stated".

    Rendering that as ``$0.00`` would enter it into Avoidance (CHILI $ >= 0) and Capture as a
    real zero, which is a fabricated data point in both metrics.
    """
    run_dir, manifest, pins = build_tree(tmp_path, ross_pnl=0)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    case = bundle.cases[0]
    assert case.ross_pnl_usd is None
    assert rr.REASON_ROSS_PNL_ABSENT in case.unscorable_reasons

    _doc, md = rr.build_report(bundle, scorer=StubScorer())
    header, _sep, first_row = md.split("| symbol | date | acct")[1].split("\n")[:3]
    assert "+0.00" not in first_row
    assert "null sentinel" in md


def test_absent_ross_pnl_is_never_sent_to_the_scorer_as_zero(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, ross_pnl=0)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    payload = bundle.cases[0].scorer_payload("base")
    assert payload["ross_pnl_usd"] is None


def test_fmt_usd_distinguishes_absent_from_zero():
    assert rr._fmt_usd(None) == "—"
    # A CHILI 0.0 IS a measurement (the replay took nothing) and must print as a number.
    assert rr._fmt_usd(0.0) == "+0.00"
    assert rr._fmt_usd(-11.37) == "-11.37"
    assert rr._fmt_usd(1830.0) == "+1,830.00"


# ─────────────────────────────────────────────────────────────────────────────
# 4. THE REPORTER INVENTS NOTHING
# ─────────────────────────────────────────────────────────────────────────────

def test_no_rpi_is_fabricated_when_the_scorer_is_missing(tmp_path):
    """The four parity numbers belong to STEP 11. Absent it, the section says so."""
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=EmptyScorer())
    assert doc["rpi_by_arm"] == {}
    assert "**Unavailable.**" in md
    assert any("ross_parity_index" in p for p in doc["scorer_problems"])


def test_no_stage_is_fabricated_when_the_scorer_is_missing(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=EmptyScorer())
    assert bundle.cases[0].recorded_stage is None
    assert "unavailable" in md
    assert any("classify_first_divergence" in p for p in doc["scorer_problems"])


def test_ross_equity_defaults_to_unknown_and_is_never_the_sim_equity(tmp_path):
    """Ross's equity and the driver's ``env.EQUITY`` are different accounts.

    ``env.EQUITY`` is the SIM size CHILI was sized against (13000.0 in this fixture).
    Substituting it for Ross's account would produce a %-of-equity Capture figure that looks
    measured and is fiction, so the default must be the empty map.
    """
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    scorer = StubScorer()
    rr.build_report(bundle, scorer=scorer)  # no ross_equity supplied
    assert scorer.equity_args, "the scorer was never called"
    assert all(eq == {} for eq in scorer.equity_args)
    assert all(13000.0 not in eq.values() for eq in scorer.equity_args)


def test_ross_equity_is_passed_as_a_mapping_when_supplied(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    scorer = StubScorer()
    rr.build_report(bundle, scorer=scorer, ross_equity={"main": 60000.0})
    assert scorer.equity_args[0] == {"main": 60000.0}


def test_parse_ross_equity_rejects_junk_rather_than_defaulting():
    assert rr.parse_ross_equity(None) == {}
    assert rr.parse_ross_equity("") == {}
    assert rr.parse_ross_equity("main=60000,small=2000") == {"main": 60000.0, "small": 2000.0}
    with pytest.raises(SystemExit):
        rr.parse_ross_equity("60000")          # no account bucket
    with pytest.raises(SystemExit):
        rr.parse_ross_equity("main=notanumber")
    with pytest.raises(SystemExit):
        rr.parse_ross_equity("main=0")         # a zero equity is not a denominator


def test_unattributed_knobs_are_counted_not_explained(tmp_path):
    """A derivation must travel WITH the knob. The reporter ships no derivation table."""
    run_dir, manifest, pins = build_tree(tmp_path)
    # Wrap one arm's receipt the way the bench runner may, carrying derivations.
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "lever_off", "run.json"), {
        "schema": "chili.rossbench_run.v1",
        "knobs": {"GRID_STEP_S": {"value": 1.0, "derivation": "<= p10 first-decision latency"},
                  "TICK_STRIDE": "dense-stride invariant"},
        "replay_result": make_receipt(pnl=42.10),
    })
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=StubScorer())
    knobs = {k["knob"]: k["derivation"] for k in doc["provenance"]["knobs"]}
    assert knobs["GRID_STEP_S"] == "<= p10 first-decision latency"
    assert knobs["TICK_STRIDE"] == "dense-stride invariant"
    assert knobs["EQUITY"] == "UNATTRIBUTED"
    assert doc["provenance"]["knobs_unattributed"] >= 1
    assert "UNATTRIBUTED" in md
    assert "will not invent an explanation" in md


def test_no_receipt_means_no_replay_key_so_the_scorer_excludes_rather_than_scoring_zero(tmp_path):
    """An arm that did not run contributes nothing — it is NOT a $0 capture.

    "CHILI was there and took nothing" and "the bench has no receipt for this row" are
    different findings, and only the receipt can tell them apart.
    """
    run_dir, manifest, pins = build_tree(tmp_path, arms=("base",))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    payload = bundle.cases[0].scorer_payload("lever_off")
    assert "replay" not in payload


# ─────────────────────────────────────────────────────────────────────────────
# 5. RECEIPT AND TIMELINE READING (against the VERIFIED driver schema)
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_receipt_accepts_the_bare_receipt():
    doc = make_receipt()
    receipt, err = rr.extract_receipt(doc)
    assert err is None
    assert receipt["schema"] == rr.DRIVER_RECEIPT_SCHEMA


def test_extract_receipt_lifts_a_wrapped_receipt_and_its_knobs():
    wrapped = {"schema": "chili.rossbench_run.v1", "case_id": "SDOT_2026-06-26",
               "knobs": {"TICK_STRIDE": "invariant 1"}, "replay_result": make_receipt()}
    receipt, err = rr.extract_receipt(wrapped)
    assert err is None
    assert receipt["pnl_usd"] == -11.37
    assert receipt["knobs"] == {"TICK_STRIDE": "invariant 1"}


def test_extract_receipt_names_the_schema_it_got():
    receipt, err = rr.extract_receipt({"schema": "something.else.v1"})
    assert receipt is None
    assert "something.else.v1" in err
    assert rr.DRIVER_RECEIPT_SCHEMA in err


def test_code_ref_comes_only_from_the_flagged_divergence_row():
    """An unflagged code_ref would be a guess printed as a citation."""
    rows = [{"code_ref": "earlier.py:1"},
            {"first_divergence": True, "code_ref": "live_runner.py:33927"}]
    ref, row = rr.first_divergence_code_ref(rows)
    assert ref == "live_runner.py:33927"
    assert row["first_divergence"] is True

    ref2, row2 = rr.first_divergence_code_ref([{"code_ref": "a.py:1"}, {"code_ref": "b.py:2"}])
    assert ref2 is None and row2 is None
    assert rr.first_divergence_code_ref([]) == (None, None)


def test_code_ref_is_read_off_the_rows_chili_events():
    """The real timeline row has NO top-level code_ref.

    scripts/rossbench_timeline.py attaches ``code_ref`` to each CHILI event
    (:1077-1085) and the row document (:1374-1393) carries only ``chili``. A reporter that
    looked only at the row would print an empty column for every case.
    """
    rows = [{
        "t_et": "09:36:02", "first_divergence": True,
        "chili": [
            {"event_type": "live_entry_trigger_wait", "code_ref": "live_runner.py:33927"},
            {"event_type": "live_backside_bench", "code_ref": "live_runner.py:41002"},
            {"event_type": "live_noop", "code_ref": None},
        ],
    }]
    ref, _row = rr.first_divergence_code_ref(rows)
    # Both sites are reported: naming one would assert a primary cause the timeline did not.
    assert ref == "live_runner.py:33927; live_runner.py:41002"


def test_a_divergence_row_with_no_resolvable_code_ref_reports_none():
    rows = [{"first_divergence": True, "chili": [{"event_type": "x", "code_ref": None}]}]
    assert rr.first_divergence_code_ref(rows)[0] is None


def test_timeline_meta_is_preferred_over_the_jsonl(tmp_path):
    """Two producers write ``timeline.jsonl`` into the same directory.

    The bench runner writes a flat event log with no divergence flag
    (ross_replay_bench.py:1043-1050); the timeline writer writes the per-second document plus
    ``timeline.meta.json``, whose ``first_divergence`` block already resolved the code refs
    (rossbench_timeline.py:1134-1146). The meta file is the writer's own answer and wins.
    """
    run_dir, manifest, pins = build_tree(tmp_path, arms=("base",))
    arm_dir = os.path.join(run_dir, "SDOT_2026-06-26", "base")
    # The runner's flat log: no first_divergence, no code_ref anywhere.
    with open(os.path.join(arm_dir, "timeline.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": "13:35", "kind": "event", "what": "live_entry_filled"}) + "\n")
    write_json(os.path.join(arm_dir, "timeline.meta.json"), {
        "schema": "chili.rossbench_timeline.meta.v1",
        "first_divergence": {"t_et": "09:36:02", "ross_stage": "filled",
                             "chili_stage": "armed_no_candidate",
                             "code_refs": ["live_runner.py:33927", "live_runner.py:33927"]},
    })
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    run = bundle.cases[0].arms["base"]
    assert run.code_ref == "live_runner.py:33927"   # de-duplicated
    assert run.code_ref_source == "timeline.meta.json:first_divergence"


def test_an_explicit_null_divergence_is_an_answer_not_a_prompt_to_rescan(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, arms=("base",))
    arm_dir = os.path.join(run_dir, "SDOT_2026-06-26", "base")
    write_json(os.path.join(arm_dir, "timeline.meta.json"),
               {"schema": "chili.rossbench_timeline.meta.v1", "first_divergence": None})
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    run = bundle.cases[0].arms["base"]
    assert run.code_ref is None
    assert run.code_ref_source == "timeline.meta.json:no_divergence"


def test_the_jsonl_is_the_fallback_and_says_so(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, arms=("base",))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    run = bundle.cases[0].arms["base"]
    assert run.code_ref == "live_runner.py:33927"
    assert run.code_ref_source == "timeline.jsonl"


def test_nbbo_vendor_is_derived_from_tape_sources_and_flags_two_vendors():
    """The receipt has no scalar nbbo_vendor; it is the single key of the NBBO tape sources
    (ross_replay_benchmark.py:336-338). Two keys is a concatenated tape, not a vendor."""
    assert rr.nbbo_vendor({"momentum_nbbo_spread_tape": {"iqfeed_lookup_bbo": 41000}}) \
        == "iqfeed_lookup_bbo"
    assert rr.nbbo_vendor({"momentum_nbbo_spread_tape": {"a": 1, "b": 2}}) == "AMBIGUOUS:a,b"
    assert rr.nbbo_vendor({"momentum_nbbo_spread_tape": {"a": 0}}) is None
    assert rr.nbbo_vendor({}) is None
    assert rr.nbbo_vendor(None) is None


def test_symbol_comes_from_the_receipt_and_the_date_source_is_recorded(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    case = bundle.cases[0]
    assert case.symbol == "SDOT"
    assert case.date == "2026-06-26"
    # Resolved from the directory name, so no fallback note is added.
    assert not any(n.startswith("date_source=") for n in case.notes)


def test_a_case_dir_without_a_date_falls_back_and_says_so(tmp_path):
    run_dir = tmp_path / "run"
    write_json(run_dir / "SDOT" / "base" / "run.json", make_receipt())
    manifest = tmp_path / "m.json"
    write_json(manifest, {"schema": "chili.ross_ground_truth_manifest.v1", "windows": []})
    pins = tmp_path / "p.json"
    write_json(pins, {"schema": "chili.ross_event_pins.v1", "pins": []})
    bundle = rr.load_bundle(str(run_dir), str(manifest), str(pins), base_arm="base")
    case = bundle.cases[0]
    assert case.date == "2026-06-26"
    assert "date_source=receipt_env.WIN_START_utc_date" in case.notes


# ─────────────────────────────────────────────────────────────────────────────
# 6. THE DELTA AND THE TABLE
# ─────────────────────────────────────────────────────────────────────────────

def test_delta_is_arm_minus_base_and_blank_without_both(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    case = bundle.cases[0]
    assert case.pnl("base") == pytest.approx(-11.37)
    assert case.pnl("lever_off") == pytest.approx(42.10)
    assert case.delta("base", "lever_off") == pytest.approx(53.47)
    assert case.delta("base", "no_such_arm") is None


def test_table_has_the_twelve_scorecard_columns(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    table = rr.render_case_table(bundle.cases, base_arm="base", arm="lever_off")
    header = table.splitlines()[0]
    assert header.count("|") == 13  # 12 columns
    for column in ("symbol", "date", "acct", "Ross window ET", "Ross $",
                   "recorded stage", "code_ref"):
        assert column in header
    assert "live_runner.py:33927" in table


def test_stage_cells_always_carry_their_source():
    """A ledger verdict must never read as an observed lifecycle."""
    class _Stage(str):
        source = "xref_verdict"

    assert rr._stage_cell(_Stage("not_alive")) == "not_alive [xref_verdict]"
    assert rr._stage_cell(None) == "unavailable"
    assert rr._stage_cell("plain") == "plain"


def test_pipes_in_a_stage_cannot_shift_the_table():
    """An unescaped pipe would split the row and silently move every value after it."""
    assert rr._md_cell("armed_no_candidate(trigger_wait:a|b)") \
        == "armed_no_candidate(trigger_wait:a\\|b)"
    assert rr._md_cell(None) == "—"


def test_a_single_arm_run_still_renders_a_table(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, arms=("base",))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    _doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert "| symbol | date | acct" in md


# ─────────────────────────────────────────────────────────────────────────────
# 7. THE UNSCORABLE LIST CARRIES A REASON FOR EVERY CASE
# ─────────────────────────────────────────────────────────────────────────────

def test_ambiguous_pin_leaves_the_scored_population_with_a_named_reason(tmp_path):
    """Ambiguity is EXPECTED in volume (STEP 3) and is reported, not treated as a failure —
    but it cannot anchor a grading window, so the case is not scored."""
    run_dir, manifest, pins = build_tree(tmp_path, pin_confidence="tape_ambiguous")
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert rr.REASON_PIN_AMBIGUOUS in bundle.cases[0].unscorable_reasons
    doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert doc["unscorable"][0]["reasons"] == bundle.cases[0].unscorable_reasons
    assert rr.REASON_PIN_AMBIGUOUS in md


def test_unpinned_case_is_named(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, pin_confidence="unpinned")
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert rr.REASON_UNPINNED in bundle.cases[0].unscorable_reasons


def test_missing_arm_is_named_with_the_arm(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    # A second case that only ran the base arm.
    write_json(os.path.join(run_dir, "ZDAI_2026-06-26", "base", "run.json"),
               make_receipt(symbol="ZDAI"))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    zdai = [c for c in bundle.cases if c.symbol == "ZDAI"][0]
    assert f"{rr.REASON_ARM_MISSING}:lever_off" in zdai.unscorable_reasons


def test_case_without_ground_truth_is_named(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, cases=[])
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert rr.REASON_NO_MANIFEST_ROW in bundle.cases[0].unscorable_reasons


def test_arms_priced_at_different_equities_are_not_an_ab(tmp_path):
    """Two arms sized differently produce dollars that are not comparable to each other."""
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "lever_off", "run.json"),
               make_receipt(pnl=42.10, equity=400000.0))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    reasons = bundle.cases[0].unscorable_reasons
    assert any(r.startswith(rr.REASON_EQUITY_MISMATCH) for r in reasons)


def test_a_case_id_that_disagrees_with_its_directory_is_flagged(tmp_path):
    """A renamed or hand-assembled directory would be graded against the wrong ground truth."""
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "lever_off", "run.json"),
               {"schema": "chili.rossbench_run.v1", "case_id": "ZDAI_2026-06-26",
                "replay_result": make_receipt(pnl=42.10)})
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    reasons = bundle.cases[0].unscorable_reasons
    assert any(r.startswith(rr.REASON_CASE_ID_MISMATCH) for r in reasons)
    assert any("ZDAI_2026-06-26" in r for r in reasons)


def test_a_matching_declared_case_id_is_not_flagged(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "lever_off", "run.json"),
               {"schema": "chili.rossbench_run.v1", "case_id": "SDOT_2026-06-26",
                "replay_result": make_receipt(pnl=42.10)})
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert not any(r.startswith(rr.REASON_CASE_ID_MISMATCH)
                   for r in bundle.cases[0].unscorable_reasons)


def test_unreadable_receipt_is_named_not_dropped(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    with open(os.path.join(run_dir, "SDOT_2026-06-26", "base", "run.json"),
              "w", encoding="utf-8") as fh:
        fh.write("{not json")
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    reasons = bundle.cases[0].unscorable_reasons
    assert any(r.startswith(rr.REASON_RECEIPT_UNREADABLE) for r in reasons)


def test_every_unscorable_case_reaches_the_report_with_its_reason(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, ross_pnl=0, pin_confidence="tape_ambiguous")
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    _doc, md = rr.build_report(bundle, scorer=StubScorer())
    section = md.split("## Unscorable cases")[1]
    assert rr.REASON_ROSS_PNL_ABSENT in section
    assert rr.REASON_PIN_AMBIGUOUS in section


# ─────────────────────────────────────────────────────────────────────────────
# 8. PROVENANCE
# ─────────────────────────────────────────────────────────────────────────────

def test_provenance_carries_every_required_element(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    _doc, md = rr.build_report(bundle, scorer=StubScorer())
    for required in ("Tree that ran", "Sink", "Tape sources", "Mock fill model",
                     "nbbo_vendor", "depth rows", "grid steps", "Knobs and their derivations",
                     "Ground-truth inputs"):
        assert required in md, required
    assert "chili_rossbench_test" in md      # sink
    assert "conservative" in md              # mock fill model
    assert "iqfeed_lookup_bbo" in md         # nbbo vendor
    assert "abc123" in md                    # tree sha


def _write_bench_plan(run_dir, overrides=None):
    """The runner's own plan document (scripts/ross_replay_bench.py:1282-1300, :1384)."""
    write_json(os.path.join(run_dir, "bench.json"), {
        "schema": "chili.ross_replay_bench.v1",
        "generated_at_utc": "2026-09-04T00:00:00+00:00",
        "driver_result_schema": rr.DRIVER_RECEIPT_SCHEMA,
        "args": {"equity": 13000.0, "risk": 130.0, "tick_stride": 1,
                 "source": "***REDACTED***", "sink": "***REDACTED***"},
        "tree": {"build": "E:/dev/wt-bench", "ref": "origin/main", "head": "abc123",
                 "sentinel_file": "scripts/replay_v3_fsm_window.py", "sentinel": "SOURCE_FILTER"},
        "inputs": {"manifest": {}, "pins": {}, "corpus": {}},
        "env_fence": {"forbidden_prefixes": ["ROSS_"], "forbidden_keys": ["REPLAY_KEEP_SINK"]},
        "arms": [{"name": "base", "source": None, "overrides": {}},
                 {"name": "lever_off", "source": "arms/lever_off.json",
                  "overrides": overrides or {"CHILI_MOMENTUM_STICKY_BACKSIDE_BENCH_ENABLED": "0"}}],
        "cases": [],
    })


def test_the_arm_overrides_are_reported_so_a_delta_names_its_treatment(tmp_path):
    """A delta whose treatment is unnamed is not a finding.

    The runner records each arm's env overrides in bench.json; the report must show them next
    to the numbers those overrides produced.
    """
    run_dir, manifest, pins = build_tree(tmp_path)
    _write_bench_plan(run_dir)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=StubScorer())
    plan = doc["provenance"]["bench_plan"]
    assert plan["present"] is True
    assert plan["arm_overrides"]["lever_off"] == {
        "CHILI_MOMENTUM_STICKY_BACKSIDE_BENCH_ENABLED": "0"}
    assert "What each arm actually changed" in md
    assert "CHILI_MOMENTUM_STICKY_BACKSIDE_BENCH_ENABLED" in md
    assert "origin/main" in md and "SOURCE_FILTER" in md


def test_a_missing_bench_plan_is_stated_not_reconstructed(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert any("bench.json is absent" in w for w in bundle.load_warnings)
    doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert doc["provenance"]["bench_plan"]["present"] is False
    assert "was not found in the run dir" in md
    assert "A delta whose treatment is unnamed is not a finding." in md


def test_supplied_knob_derivations_are_used_and_the_rest_stay_unattributed(tmp_path):
    """Derivations arrive as DATA. The reporter still refuses to invent the missing ones."""
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, _md = rr.build_report(bundle, scorer=StubScorer(), knob_derivations={
        "TICK_STRIDE": "dense-stride invariant 1",
        "GRID_STEP_S": {"value": 1.0, "derivation": "<= p10 first-decision latency"},
    })
    knobs = {k["knob"]: k["derivation"] for k in doc["provenance"]["knobs"]}
    assert knobs["TICK_STRIDE"] == "dense-stride invariant 1"
    assert knobs["GRID_STEP_S"] == "<= p10 first-decision latency"
    assert knobs["EQUITY"] == "UNATTRIBUTED"
    assert doc["provenance"]["knobs_unattributed"] >= 1


def test_cli_rejects_a_knob_derivations_file_that_is_not_an_object(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bad = tmp_path / "knobs.json"
    write_json(bad, ["not", "an", "object"])
    with pytest.raises(SystemExit):
        rr.main(["--run-dir", run_dir, "--manifest", manifest, "--pins", pins,
                 "--knob-derivations", str(bad), "--out-dir", str(tmp_path / "o"),
                 "--scorer-module", "no.such.module"])


def test_a_split_tree_across_arms_is_flagged_not_folded(tmp_path):
    """Arms that ran on different trees are not an A/B, and the report must say so."""
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "lever_off", "run.json"),
               make_receipt(pnl=42.10, tree_head="deadbeef"))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert len(doc["provenance"]["tree"]) == 2
    assert "did not run on one tree" in md


def test_a_split_mock_config_across_arms_is_flagged(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "lever_off", "run.json"),
               make_receipt(pnl=42.10, mock={"resting_limit_fills": False,
                                             "fill_mode": "aggressive"}))
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    _doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert "filled against different mock configs" in md


def test_field_aliases_actually_used_are_reported(tmp_path):
    """The sibling schemas are authored in parallel; which key carried each field is evidence."""
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert bundle.alias_hits["ross_pnl_usd"] == "ross_net_usd"
    assert bundle.alias_hits["ross_window_et"] == "stated_entry_et"
    doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert doc["field_aliases_used"]["ross_pnl_usd"] == "ross_net_usd"
    assert "Cross-component field names actually used" in md


def test_the_entry_leg_pin_wins_over_the_exit_leg(tmp_path):
    """The pin builder writes one row per leg (rossbench_pin_ross_events.py:763,829).

    The grading window is anchored on the ENTRY pin, so an exit-leg row that happens to be
    ``tape_confirmed`` must not mark a case scorable when the entry leg is ambiguous — that
    would be evidence about the wrong instant.
    """
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(pins, {"schema": "chili.ross_event_pins.v1", "pins": [
        {"symbol": "SDOT", "date": "2026-06-26", "leg": "exit",
         "pin_method": "price_match", "pin_confidence": "tape_confirmed",
         "pin_second_et": "09:52:00"},
        {"symbol": "SDOT", "date": "2026-06-26", "leg": "entry",
         "pin_method": "level_cross", "pin_confidence": "tape_ambiguous",
         "pin_second_et": "09:35:12"},
    ]})
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    case = bundle.cases[0]
    assert case.pin_method == "level_cross"
    assert case.pin_confidence == "tape_ambiguous"
    assert rr.REASON_PIN_AMBIGUOUS in case.unscorable_reasons


def test_an_exit_only_pin_is_used_rather_than_nothing(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(pins, {"schema": "chili.ross_event_pins.v1", "pins": [
        {"symbol": "SDOT", "date": "2026-06-26", "leg": "exit",
         "pin_method": "price_match", "pin_confidence": "tape_confirmed"},
    ]})
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert bundle.cases[0].pin_method == "price_match"


def test_alias_pin_keys_are_accepted_and_recorded(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(pins, {"schema": "chili.ross_event_pins.v1",
                      "pins": [{"symbol": "SDOT", "date": "2026-06-26",
                                "method": "level_cross", "confidence": "tape_confirmed"}]})
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert bundle.cases[0].pin_method == "level_cross"
    assert bundle.alias_hits["pin_method"] == "method"


# ─────────────────────────────────────────────────────────────────────────────
# 9. ACCOUNTS ARE NEVER GUESSED OR POOLED
# ─────────────────────────────────────────────────────────────────────────────

def test_big_collapses_to_main_and_absent_stays_unknown(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path, account="big")
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base",
                            account_normalizer=StubScorer.normalize_account)
    assert bundle.cases[0].account == "main"

    run_dir2, manifest2, pins2 = build_tree(tmp_path / "b", account=None)
    bundle2 = rr.load_bundle(run_dir2, manifest2, pins2, base_arm="base",
                             account_normalizer=StubScorer.normalize_account)
    # Never defaulted to "main": that would pool small- and main-account dollars.
    assert bundle2.cases[0].account == "unknown"


def test_the_raw_account_is_what_reaches_the_scorer(tmp_path):
    """The bucket vocabulary lives in ONE place; the reporter hands over the raw value."""
    run_dir, manifest, pins = build_tree(tmp_path, account="big")
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base",
                            account_normalizer=StubScorer.normalize_account)
    assert bundle.cases[0].scorer_payload("base")["account"] == "big"


def test_the_fallback_normalizer_announces_itself(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")  # no normalizer
    assert any("FALLBACK" in w for w in bundle.load_warnings)


def test_fallback_normalizer_matches_the_scorer_vocabulary():
    assert rr._fallback_normalize_account("big") == "main"
    assert rr._fallback_normalize_account("Main") == "main"
    assert rr._fallback_normalize_account("small") == "small"
    assert rr._fallback_normalize_account(None) == "unknown"
    assert rr._fallback_normalize_account("small+main") == "mixed"


# ─────────────────────────────────────────────────────────────────────────────
# 10. SCORER PLUMBING
# ─────────────────────────────────────────────────────────────────────────────

def test_the_grading_window_is_the_window_the_run_executed(tmp_path):
    """Widening the window would credit or blame decisions the run never made."""
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    scorer = StubScorer()
    rr.build_report(bundle, scorer=scorer)
    _case, _rec, _rep, window = scorer.calls[0]
    assert window == {"win_start": "2026-06-26T13:30:00", "win_end": "2026-06-26T14:30:00"}


def test_replay_events_come_from_the_receipt(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    scorer = StubScorer()
    rr.build_report(bundle, scorer=scorer)
    _case, _rec, replay_events, _window = scorer.calls[0]
    assert replay_events[0]["event_type"] == "live_entry_filled"


def test_recorded_events_are_loaded_from_the_case_directory(tmp_path):
    """Recorded events are case-level; when absent the scorer falls back to xref_verdict."""
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(os.path.join(run_dir, "SDOT_2026-06-26", "recorded_events.json"),
               {"events": [{"ts": "2026-06-26T13:31:00", "event_type": "live_arm_confirmed"}]})
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert len(bundle.cases[0].recorded_events) == 1
    scorer = StubScorer()
    rr.build_report(bundle, scorer=scorer)
    _case, recorded_events, _rep, _w = scorer.calls[0]
    assert recorded_events[0]["event_type"] == "live_arm_confirmed"


def test_no_recorded_events_means_an_empty_list_not_a_crash(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert bundle.cases[0].recorded_events == []
    scorer = StubScorer()
    doc, _md = rr.build_report(bundle, scorer=scorer)
    assert doc["cases"][0]["recorded_events_loaded"] == 0


def test_scorer_exceptions_become_reported_problems_not_a_lost_report(tmp_path):
    class _Exploding(StubScorer):
        def ross_parity_index(self, cases, equity, **kwargs):
            raise RuntimeError("boom")

    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=_Exploding())
    assert any("boom" in p for p in doc["scorer_problems"])
    assert "Scorer problems" in md or "**Unavailable.**" in md


def test_rpi_renders_numerator_and_denominator_for_every_rate(tmp_path):
    """A bare rate hides its sample size; this bench runs on a handful of symbol-days."""
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    _doc, md = rr.build_report(bundle, scorer=StubScorer())
    rpi = md.split("## Ross Parity Index")[1]
    assert "| metric | ratio | numerator | denominator | cases |" in rpi
    assert "numerator (CHILI $) | denominator (Ross $)" in rpi
    assert "tier2_required" in rpi
    assert "blended_score" in rpi


def test_unknown_scorer_keys_are_surfaced_not_dropped(tmp_path):
    class _Extra(StubScorer):
        def ross_parity_index(self, cases, equity, **kwargs):
            out = StubScorer.ross_parity_index(self, cases, equity, **kwargs)
            out["capture_plus"] = {"numerator": 1, "denominator": 4}
            return out

    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    _doc, md = rr.build_report(bundle, scorer=_Extra())
    assert "Additional scorer keys not rendered above" in md
    assert "capture_plus" in md


# ─────────────────────────────────────────────────────────────────────────────
# 11. CLI
# ─────────────────────────────────────────────────────────────────────────────

def test_cli_writes_both_artefacts_with_lf_newlines(tmp_path):
    """CRLF would change the bytes of an otherwise identical report and break the no-op
    A/B byte comparison (reference_python_write_text_crlf_windows)."""
    run_dir, manifest, pins = build_tree(tmp_path)
    out = tmp_path / "out"
    rc = rr.main(["--run-dir", run_dir, "--manifest", manifest, "--pins", pins,
                  "--base-arm", "base", "--out-dir", str(out),
                  "--scorer-module", "no.such.module"])
    assert rc == 0
    report = (out / "report.md").read_bytes()
    payload = (out / "rpi.json").read_bytes()
    assert b"\r\n" not in report
    assert b"\r\n" not in payload
    doc = json.loads(payload.decode("utf-8"))
    assert doc["schema"] == rr.RPI_SCHEMA
    assert doc["admission_claim"] is False
    # The CLI ran with the REAL adapter and grader, so the grade is whatever they returned.
    # It must be one of the enum's members and it must carry its derivation — a stamp with
    # an empty derivation block would be the old constant wearing a new name.
    assert doc["evidence_grade"] in rr.EVIDENCE_GRADE_STAMPS.values()
    assert doc["evidence_grade_derivation"]["rule"]


def test_cli_strict_exits_nonzero_on_unattributed_knobs(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    rc = rr.main(["--run-dir", run_dir, "--manifest", manifest, "--pins", pins,
                  "--out-dir", str(tmp_path / "o2"), "--strict",
                  "--scorer-module", "no.such.module"])
    assert rc == 2


def test_cli_refuses_a_base_arm_that_is_not_in_the_tree(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    with pytest.raises(SystemExit) as exc:
        rr.load_bundle(run_dir, manifest, pins, base_arm="not_an_arm")
    # It must NAME the arms it found rather than electing one silently.
    assert "base" in str(exc.value) and "lever_off" in str(exc.value)


def test_an_empty_run_dir_is_a_hard_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    manifest = tmp_path / "m.json"
    write_json(manifest, {"schema": "chili.ross_ground_truth_manifest.v1", "windows": []})
    pins = tmp_path / "p.json"
    write_json(pins, {"schema": "chili.ross_event_pins.v1", "pins": []})
    with pytest.raises(SystemExit):
        rr.load_bundle(str(empty), str(manifest), str(pins))
    with pytest.raises(SystemExit):
        rr.load_bundle(str(tmp_path / "does_not_exist"), str(manifest), str(pins))


def test_unexpected_input_schemas_are_warned_about_not_rejected(tmp_path):
    """A schema rename upstream should degrade the report, not delete it."""
    run_dir, manifest, pins = build_tree(tmp_path)
    write_json(manifest, {"schema": "chili.ross_ground_truth_manifest.v2", "windows": []})
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert any("manifest schema" in w for w in bundle.load_warnings)
    _doc, md = rr.build_report(bundle, scorer=StubScorer())
    assert "Load warnings" in md


def test_the_envelope_and_the_metric_block_no_longer_share_a_schema_id():
    """The collision is FIXED, not merely annotated.

    Both documents used to declare ``chili.ross_parity_index.v1`` with different shapes, and
    this envelope nests the metric block at ``rpi_by_arm[<arm>].index`` — so one file
    carried two objects under one id and a schema-dispatching reader mis-parsed one. The
    scorer's block was renamed; this test reads the real scorer so a re-collision fails
    here.
    """
    import importlib

    scoring = importlib.import_module(
        "app.services.trading.momentum_neural.ross_bench_scoring")
    assert rr.RPI_SCHEMA == "chili.ross_parity_index.v1"
    assert scoring.PARITY_INDEX_SCHEMA == "chili.ross_parity_index_metrics.v1"
    assert scoring.PARITY_INDEX_SCHEMA != rr.RPI_SCHEMA
    # The scorer also records the envelope's id, so the pair is documented on both sides.
    assert scoring.PARITY_INDEX_ENVELOPE_SCHEMA == rr.RPI_SCHEMA


def test_the_schema_note_describes_the_nesting_without_claiming_a_collision(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, _md = rr.build_report(bundle, scorer=StubScorer())
    assert doc["schema"] == "chili.ross_parity_index.v1"
    assert "chili.ross_parity_index_metrics.v1" in doc["schema_note"]
    assert "SAME schema id" not in doc["schema_note"]


# ─────────────────────────────────────────────────────────────────────────────
# 13. THE RECORDED SIDE: MEASURED OR INFERRED, AND WHICH VERDICTS CANNOT BE PLACED
# ─────────────────────────────────────────────────────────────────────────────
# Until scripts/rossbench_export_recorded_events.py existed, nothing wrote the files
# ``load_recorded_events`` reads, so the recorded stage ALWAYS came from the ledger's
# xref_verdict — and two of the four tokens the manifest emits cannot be placed on the
# ladder from prose. The report must say so, per run, with counts.

class _PlacementScorer(StubScorer):
    """StubScorer plus the scorer's real verdict-placement surface.

    The reporter reads ``XREF_VERDICT_PLACEMENT`` / ``XREF_VERDICTS_PLACEABLE`` /
    ``XREF_VERDICTS_UNPLACEABLE`` by those names; a rename in the scorer breaks this.
    """

    XREF_VERDICT_PLACEMENT = {"armed_no_entry": {"basis": "refused"}}
    XREF_VERDICTS_PLACEABLE = ("not_in_universe", "never_armed", "entered_wrong_leg")
    XREF_VERDICTS_UNPLACEABLE = ("armed_no_entry", "unknown_no_data")

    def classify_first_divergence(self, case, recorded_events, replay_events, window):
        self.calls.append((case, list(recorded_events), list(replay_events), window))
        stage = _stage("unknown(armed_no_entry)", "xref_verdict", {
            "xref_verdict": "armed_no_entry",
            "unplaceable_reason": "one token, three rungs",
            "rung_bounds": ["no_arm_attempt", "submit_no_fill"],
        })
        return (stage, _stage("filled_exited_worse", "events", {}))


def _stage(label, source, detail):
    """A ``Stage``-shaped object: a str carrying ``.source`` and ``.detail``."""
    class _S(str):
        pass
    s = _S(label)
    s.source = source
    s.detail = dict(detail)
    return s


def test_an_unplaceable_verdict_is_named_in_the_report_never_printed_bare(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=_PlacementScorer())
    block = doc["recorded_side"]["unplaceable_in_this_run"]["armed_no_entry"]
    assert block["count"] == 1
    assert block["cases"] == ["SDOT_2026-06-26"]
    assert block["rung_bounds"] == ["no_arm_attempt", "submit_no_fill"]
    assert "Verdicts this run could not place on the ladder" in md
    assert "one token, three rungs" in md
    # The bound must be printed too: "we could not place it" is weaker than "we could not
    # place it, but it IS somewhere between these two rungs".
    assert "`no_arm_attempt` .. `submit_no_fill`" in md


def test_the_report_names_the_exporter_that_would_make_the_side_measured(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=_PlacementScorer())
    assert doc["recorded_side"]["exporter"] == "scripts/rossbench_export_recorded_events.py"
    assert "scripts/rossbench_export_recorded_events.py" in md
    assert doc["recorded_side"]["cases_with_exported_events"] == 0


def test_an_exported_case_is_counted_as_measured_and_its_file_is_named(tmp_path):
    """An empty export and a missing export look identical in a list; they are not."""
    run_dir, manifest, pins = build_tree(tmp_path)
    path = os.path.join(run_dir, "SDOT_2026-06-26", "recorded_events.jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": "2026-06-26T13:31:00",
                             "event_type": "live_arm_confirmed", "payload": {},
                             "session_id": 9198, "mode": "live"}) + "\n")
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    assert bundle.cases[0].recorded_events_source == "recorded_events.jsonl"
    doc, md = rr.build_report(bundle, scorer=_PlacementScorer())
    assert doc["recorded_side"]["cases_with_exported_events"] == 1
    assert doc["cases"][0]["recorded_events_source"] == "recorded_events.jsonl"
    assert "**1 of 1** case(s) carried exported live-lane events" in md


def test_an_empty_export_is_distinguishable_from_no_export(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    path = os.path.join(run_dir, "SDOT_2026-06-26", "recorded_events.jsonl")
    open(path, "w", encoding="utf-8", newline="\n").close()
    events, source = rr.load_recorded_events(os.path.join(run_dir, "SDOT_2026-06-26"))
    assert events == []
    assert source == "recorded_events.jsonl"   # exported, and genuinely empty
    events, source = rr.load_recorded_events(os.path.join(run_dir, "nope"))
    assert (events, source) == ([], None)      # nobody ever exported anything


def test_recorded_stage_sources_are_counted_so_the_mix_is_visible(tmp_path):
    run_dir, manifest, pins = build_tree(tmp_path)
    bundle = rr.load_bundle(run_dir, manifest, pins, base_arm="base")
    doc, md = rr.build_report(bundle, scorer=_PlacementScorer())
    assert doc["recorded_side"]["recorded_stage_sources"] == {"xref_verdict": 1}
    assert "| recorded stage source | cases |" in md


# ─────────────────────────────────────────────────────────────────────────────
# 14. THE RECORDED-EVENTS EXPORTER (scripts/rossbench_export_recorded_events.py)
# ─────────────────────────────────────────────────────────────────────────────
# PURE functions only — no DSN is resolved and no connection is opened anywhere below.
# The exporter's DB path is exercised by running it, not by mocking a driver here.

_EXPORTER_SPEC = importlib.util.spec_from_file_location(
    "rossbench_export_recorded_events",
    os.path.join(_REPO, "scripts", "rossbench_export_recorded_events.py"),
)
rex = importlib.util.module_from_spec(_EXPORTER_SPEC)
sys.modules["rossbench_export_recorded_events"] = rex
assert _EXPORTER_SPEC.loader is not None
_EXPORTER_SPEC.loader.exec_module(rex)


def test_the_exporter_writes_the_filename_the_reporter_reads():
    """The whole defect was a reader with no writer. This is the seam, asserted."""
    assert rex.EVENTS_FILENAME in rr._RECORDED_EVENT_FILES
    assert rr.RECORDED_EVENTS_EXPORTER.endswith("rossbench_export_recorded_events.py")


def test_the_exported_row_carries_the_three_keys_the_scorer_reads():
    row = rex.event_row("2026-06-26 13:35:00", "live_entry_filled",
                        {"reason": "target", "noise": 1}, 9198, "live",
                        keys=("reason",), load_bearing=lambda t, p: {})
    assert set(("ts", "event_type", "payload")) <= set(row)
    assert row["payload"] == {"reason": "target"}
    assert row["session_id"] == 9198 and row["mode"] == "live"


def test_a_json_string_payload_is_parsed_not_stringified():
    row = rex.event_row("2026-06-26 13:35:00", "live_exit_filled",
                        json.dumps({"reason": "stop"}), 1, "live",
                        keys=("reason",), load_bearing=lambda t, p: {})
    assert row["payload"] == {"reason": "stop"}


def test_full_payload_keeps_everything_and_the_allow_list_does_not():
    payload = {"reason": "x", "detector_rejects": {"a": 1}}
    filtered = rex.event_row("t", "e", payload, 1, "live", keys=("reason",),
                             load_bearing=lambda t, p: {})
    kept = rex.event_row("t", "e", payload, 1, "live", keys=("reason",),
                         full_payload=True, load_bearing=lambda t, p: {})
    assert "detector_rejects" not in filtered["payload"]
    assert "detector_rejects" in kept["payload"]


def test_the_payload_allow_list_is_read_from_the_driver_source_not_copied():
    """A second copy of the driver's allow-list would drift and grade the two sides
    of the bench on different payload shapes."""
    keys = rex.bench_payload_keys()
    assert "reason" in keys and "blocked_trigger" in keys
    # A Python file that parses but does not define it: a rename must be LOUD.
    with pytest.raises(SystemExit):
        rex.bench_payload_keys(os.path.join(_REPO, "scripts", "rossbench_report.py"))
    # A file that does not parse at all is equally loud, not a traceback.
    with pytest.raises(SystemExit):
        rex.bench_payload_keys(os.path.join(_REPO, "README.md"))


def test_et_day_bounds_are_et_midnights_and_survive_a_dst_switch():
    """A fixed -4h/-5h offset would put the day boundary in the wrong place twice a year."""
    lo, hi = rex.et_day_bounds_utc("2026-06-26")          # EDT, UTC-4
    assert (lo.isoformat(), hi.isoformat()) == ("2026-06-26T04:00:00", "2026-06-27T04:00:00")
    lo, hi = rex.et_day_bounds_utc("2026-01-15")          # EST, UTC-5
    assert (lo.isoformat(), hi.isoformat()) == ("2026-01-15T05:00:00", "2026-01-16T05:00:00")
    lo, hi = rex.et_day_bounds_utc("2026-03-08")          # spring forward: a 23h day
    assert (hi - lo).total_seconds() == 23 * 3600
    assert lo.tzinfo is None and hi.tzinfo is None        # the ts column is naive


def test_et_day_bounds_refuse_a_malformed_date():
    with pytest.raises(ValueError):
        rex.et_day_bounds_utc("26/06/2026")


def test_case_discovery_matches_the_runners_directory_naming(tmp_path):
    """A case is ``(symbol, date, dirname)`` — the REAL directory name, not a rebuilt one.

    The runner names a disambiguated case ``SYMBOL@<selector-slug>_<DATE>`` because 62 of
    217 symbol-days carry more than one manifest row. Rebuilding the name as
    ``f"{symbol}_{date}"`` sent the export into a sibling directory the reporter never
    opens, so 5 of the 8 lane-alive known answers exported nothing and recorded_stage fell
    back to xref_verdict — the exact defect this exporter exists to close.
    """
    for name in ("SDOT_2026-06-26", "IPST_2026-08-17", "not_a_case", "bench.json"):
        os.makedirs(str(tmp_path / name), exist_ok=True)
    assert rex.cases_from_run_dir(str(tmp_path)) == [
        ("IPST", "2026-08-17", "IPST_2026-08-17"),
        ("SDOT", "2026-06-26", "SDOT_2026-06-26")]


def test_case_discovery_admits_the_runners_selector_infix(tmp_path):
    """The ``@selector`` form must be discovered AND must keep its own directory name."""
    os.makedirs(str(tmp_path / "ILLR@A7Gnw1CMExI-ml3_2026-06-25"), exist_ok=True)
    os.makedirs(str(tmp_path / "SDOT_2026-06-26"), exist_ok=True)
    got = rex.cases_from_run_dir(str(tmp_path))
    assert ("ILLR", "2026-06-25", "ILLR@A7Gnw1CMExI-ml3_2026-06-25") in got
    assert ("SDOT", "2026-06-26", "SDOT_2026-06-26") in got
    assert len(got) == 2


def test_case_specs_are_refused_by_name_not_silently_skipped():
    assert rex.parse_cases("SDOT:2026-06-26, IPST:2026-08-17") == [
        ("SDOT", "2026-06-26", "SDOT_2026-06-26"),
        ("IPST", "2026-08-17", "IPST_2026-08-17")]
    with pytest.raises(SystemExit):
        rex.parse_cases("SDOT")
    with pytest.raises(SystemExit):
        rex.parse_cases("SDOT:26-06-2026")


def test_manifest_case_discovery_dedupes_the_leg_fan_out():
    """The manifest emits one window per ledger leg; the recorded side is per symbol-day."""
    manifest = {"windows": [
        {"manifest_id": "a", "symbol": "SDOT", "date": "2026-06-26"},
        {"manifest_id": "b", "symbol": "SDOT", "date": "2026-06-26"},
        {"manifest_id": "c", "symbol": "IPST", "date": "2026-08-17"},
        {"manifest_id": "d", "symbol": "", "date": "2026-08-17"},
    ]}
    assert rex.cases_from_manifest(manifest) == [
        ("SDOT", "2026-06-26", "SDOT_2026-06-26"),
        ("IPST", "2026-08-17", "IPST_2026-08-17")]


def test_the_exporter_never_guesses_a_dsn(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        rex.resolve_dsn(None)
    assert "will not guess" in str(exc.value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5433/chili")
    assert rex.resolve_dsn(None).endswith("/chili")
    assert rex.resolve_dsn("postgresql://u:p@h:5433/other") .endswith("/other")


def test_the_database_name_is_parsed_not_substring_matched():
    """``.../chili?application_name=chili_hydrated`` connects to PROD."""
    assert rex.dsn_database("postgresql://u:p@h:5433/chili?application_name=chili_hydrated") \
        == "chili"


def test_the_meta_distinguishes_an_absent_lane_from_an_absent_day(tmp_path):
    empty_but_paper = rex.case_meta(
        "SHPH", "2026-06-26", database="chili",
        lo=rex.et_day_bounds_utc("2026-06-26")[0], hi=rex.et_day_bounds_utc("2026-06-26")[1],
        rows=[], modes_requested=("live",), modes_seen={"paper": 412},
        payload_filter="allow_list", payload_keys=("reason",))
    empty_entirely = rex.case_meta(
        "SHPH", "2026-06-26", database="chili",
        lo=rex.et_day_bounds_utc("2026-06-26")[0], hi=rex.et_day_bounds_utc("2026-06-26")[1],
        rows=[], modes_requested=("live",), modes_seen={},
        payload_filter="allow_list", payload_keys=("reason",))
    assert empty_but_paper["modes_seen_in_window"] == {"paper": 412}
    assert empty_entirely["modes_seen_in_window"] == {}
    assert empty_but_paper["event_count"] == empty_entirely["event_count"] == 0


def test_the_meta_records_every_session_merged_into_the_stream(tmp_path):
    rows = [{"ts": "t", "event_type": "live_arm_requested", "payload": {}, "session_id": s,
             "mode": "live"} for s in (9173, 9180, 9183, 9173)]
    meta = rex.case_meta("SHPH", "2026-06-26", database="chili",
                         lo=rex.et_day_bounds_utc("2026-06-26")[0],
                         hi=rex.et_day_bounds_utc("2026-06-26")[1],
                         rows=rows, modes_requested=("live",), modes_seen={"live": 4},
                         payload_filter="allow_list", payload_keys=("reason",))
    assert meta["session_ids"] == [9173, 9180, 9183]
    assert meta["event_type_counts"] == {"live_arm_requested": 4}
    assert "ONE chronological stream" in meta["sessions_merged_note"]


def test_the_exporter_writes_lf_only_jsonl(tmp_path):
    path = str(tmp_path / "case" / rex.EVENTS_FILENAME)
    n = rex.write_jsonl(path, [{"ts": "t", "event_type": "e", "payload": {}}])
    assert n == 1
    with open(path, "rb") as fh:
        raw = fh.read()
    assert b"\r\n" not in raw
    # And the reporter can read exactly what it wrote.
    events, source = rr.load_recorded_events(os.path.dirname(path))
    assert source == rex.EVENTS_FILENAME
    assert events[0]["event_type"] == "e"


def test_case_dir_resolver_requires_the_symbol_and_date_segment():
    """One video covers several symbols: CA8i4Rc2bUY on 2026-07-23 has EHGO ml1 and JEM t1.
    A slug-only match was ambiguous for EHGO and resolved to None (2026-09-05)."""
    rows = [{"manifest_id": "CA8i4Rc2bUY::EHGO::2026-07-23::ml1"},
            {"manifest_id": "CA8i4Rc2bUY::EHGO::2026-07-23::t4"},
            {"manifest_id": "CA8i4Rc2bUY::JEM::2026-07-23::ml1"},
            {"manifest_id": "Ts7C0flv-1g::EHGO::2026-07-23::ml1"}]
    assert rr._manifest_id_from_case_dir("EHGO@CA8i4Rc2bUY-ml1_2026-07-23", rows) == "CA8i4Rc2bUY::EHGO::2026-07-23::ml1"
    assert rr._manifest_id_from_case_dir("JEM@CA8i4Rc2bUY-ml1_2026-07-23", rows) == "CA8i4Rc2bUY::JEM::2026-07-23::ml1"
    assert rr._manifest_id_from_case_dir("EHGO@Ts7C0flv-1g-ml1_2026-07-23", rows) == "Ts7C0flv-1g::EHGO::2026-07-23::ml1"
    assert rr._manifest_id_from_case_dir("NXTC@ChLgwLS9eJY-t1-big-acct_2026-07-14",
                                         [{"manifest_id": "ChLgwLS9eJY::NXTC::2026-07-14::t1-big-acct"},
                                          {"manifest_id": "ChLgwLS9eJY::NXTC::2026-07-14::t1"}]) == "ChLgwLS9eJY::NXTC::2026-07-14::t1-big-acct"
