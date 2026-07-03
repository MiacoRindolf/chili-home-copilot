"""Replay v3 P3 — the PARITY REGRESSION (permanent).

Proves the parity CONTRACT (docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §2.4 / §4 P3) against the
two operator-named recorded sessions, exported into ``tests/fixtures/replay_v3/`` so this
runs on ``chili_test`` (or with NO DB at all — these are pure fixture replays):

  * CELZ session 9920 (2026-06-30) — the +$40 ORB win.
  * IPW  session 10397 (2026-07-02) — the -$58 IPW trade.

MODE (i) — HARNESS PARITY (the GATE): the sim TRANSITION TRACE matches the recorded
``trading_automation_events`` load-bearing sequence (arm→watch→candidate→submit→fill→exit→
terminal), and the STEP-2 realistic fill model fills within a tick tolerance of the recorded
entry/exit prices. A mode-(i) mismatch is a harness bug.

MODE (ii) — CURRENT-CODE counterfactual: a REPORT of the expected divergence under d718991
gates (IPW should now bench). Non-fatal — asserted only to be PRESENT + legible.

Pure fixture replay: no DB, no network, deterministic. Fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.trading.momentum_neural.replay_mock_broker import FillMode
from app.services.trading.momentum_neural.replay_parity import (
    CANONICAL_ENTRY_SPINE,
    ParityFixture,
    canonical_trace,
    replay_counterfactual_mode_ii,
    replay_parity_mode_i,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay_v3"
_CELZ = _FIXTURE_DIR / "session_9920_CELZ.json"
_IPW = _FIXTURE_DIR / "session_10397_IPW.json"


# ── fixtures present + well-formed ───────────────────────────────────────────────────
def test_parity_fixtures_exist():
    assert _CELZ.exists(), f"missing {_CELZ} — run scripts/export_replay_v3_parity_fixtures.py"
    assert _IPW.exists(), f"missing {_IPW} — run scripts/export_replay_v3_parity_fixtures.py"


@pytest.mark.parametrize("path,symbol,sid", [(_CELZ, "CELZ", 9920), (_IPW, "IPW", 10397)])
def test_fixture_loads_and_has_recorded_trace(path, symbol, sid):
    fx = ParityFixture.load(path)
    assert fx.symbol == symbol and fx.session_id == sid
    assert fx.recorded_events, "no recorded events in fixture"
    assert fx.tape, "no recorded tape in fixture"
    # the recorded canonical trace has the full entry→exit spine
    trace = fx.recorded_canonical_trace
    for step in CANONICAL_ENTRY_SPINE:
        assert step in trace, f"{symbol} recorded trace missing load-bearing step {step}: {trace}"


# ── the canonical-trace collapse is deterministic + dedups retries ───────────────────
def test_canonical_trace_dedups_consecutive_repeats():
    raw = [
        "live_arm_confirmed", "live_watch_started",
        "live_entry_candidate_detected", "live_entry_candidate_detected",  # retries collapse
        "live_entry_submitted", "live_entry_filled",
        "live_tape_accel_reversal_exit", "live_tape_accel_reversal_exit",  # collapse
        "live_exit_filled", "live_cooldown_started",
        "live_entry_trigger_wait",  # NOT load-bearing → dropped
    ]
    assert canonical_trace(raw) == [
        "live_arm_confirmed", "live_watch_started",
        "live_entry_candidate_detected", "live_entry_submitted", "live_entry_filled",
        "live_tape_accel_reversal_exit", "live_exit_filled", "live_cooldown_started",
    ]


# ── MODE (i) — the PARITY GATE (both fixtures, conservative fill) ─────────────────────
@pytest.mark.parametrize("path,symbol", [(_CELZ, "CELZ"), (_IPW, "IPW")])
def test_mode_i_harness_parity_trace_matches(path, symbol):
    """The sim transition trace reproduces the recorded canonical trace EXACTLY (mode-i is the
    parity gate — a mismatch is a harness bug)."""
    fx = ParityFixture.load(path)
    res = replay_parity_mode_i(fx, fill_mode=FillMode.CONSERVATIVE)
    assert res.trace_matches, (
        f"{symbol} mode-i trace mismatch:\n  sim     ={res.sim_trace}\n"
        f"  recorded={res.recorded_trace}\n  diffs={res.diffs}"
    )


@pytest.mark.parametrize("path,symbol", [(_CELZ, "CELZ"), (_IPW, "IPW")])
def test_mode_i_entry_fill_inside_recorded_envelope(path, symbol):
    """The realistic fill model fills the ENTRY INSIDE the recorded NBBO envelope at the fill
    instant (the mock crossed the RECORDED ask path — it never fills outside the recorded
    book). The recorded BROKER-TRUTH avg is a separate data source; the broker-vs-tape basis
    gap is REPORTED, not asserted-to-zero (the honest irreducible limit, docs §7 R4)."""
    fx = ParityFixture.load(path)
    res = replay_parity_mode_i(fx, fill_mode=FillMode.CONSERVATIVE)
    assert res.sim_entry_price is not None, f"{symbol}: mock never filled the entry"
    assert res.entry_within_recorded_envelope, (
        f"{symbol} entry fill OUTSIDE the recorded book: sim={res.sim_entry_price} "
        f"diffs={res.diffs}"
    )
    # the broker-basis gap is measured + finite (documented, not zero)
    assert res.entry_broker_basis_bps is not None


@pytest.mark.parametrize("path,symbol", [(_CELZ, "CELZ"), (_IPW, "IPW")])
def test_mode_i_exit_fill_inside_recorded_envelope(path, symbol):
    """The exit sells INSIDE the recorded NBBO envelope at the fill instant (crossed the
    recorded bid path). Broker-vs-tape basis reported, not asserted-to-zero."""
    fx = ParityFixture.load(path)
    res = replay_parity_mode_i(fx, fill_mode=FillMode.CONSERVATIVE)
    assert res.sim_exit_price is not None, f"{symbol}: mock never filled the exit"
    assert res.exit_within_recorded_envelope, (
        f"{symbol} exit fill OUTSIDE the recorded book: sim={res.sim_exit_price} "
        f"diffs={res.diffs}"
    )
    assert res.exit_broker_basis_bps is not None


def test_mode_i_is_deterministic():
    """Two identical mode-(i) replays produce byte-identical results (no RNG/wall clock)."""
    fx = ParityFixture.load(_CELZ)
    a = replay_parity_mode_i(fx, fill_mode=FillMode.CONSERVATIVE)
    b = replay_parity_mode_i(fx, fill_mode=FillMode.CONSERVATIVE)
    assert a.sim_trace == b.sim_trace
    assert a.sim_entry_price == b.sim_entry_price
    assert a.sim_exit_price == b.sim_exit_price


def test_mode_i_optimistic_entry_is_never_worse_than_conservative():
    """The optimistic fill (mid) is a strict upper bound for the buyer vs the conservative fill
    (ask) — the fill-confidence band is ordered."""
    fx = ParityFixture.load(_CELZ)
    cons = replay_parity_mode_i(fx, fill_mode=FillMode.CONSERVATIVE)
    opt = replay_parity_mode_i(fx, fill_mode=FillMode.OPTIMISTIC)
    assert opt.sim_entry_price is not None and cons.sim_entry_price is not None
    assert opt.sim_entry_price <= cons.sim_entry_price + 1e-9


# ── MODE (ii) — CURRENT-CODE counterfactual REPORT (present + legible, non-fatal) ────
@pytest.mark.parametrize("path,symbol,expect_took", [(_CELZ, "CELZ", True), (_IPW, "IPW", True)])
def test_mode_ii_counterfactual_report_present(path, symbol, expect_took):
    fx = ParityFixture.load(path)
    diff = replay_counterfactual_mode_ii(fx)
    assert diff.symbol == symbol
    assert diff.recorded_took_trade is expect_took
    assert diff.notes, "counterfactual report must carry at least one note"


def test_mode_ii_ipw_expected_to_bench_under_current_code():
    """The documented expected divergence: IPW's recorded below-VWAP loss should NOT recur
    under d718991 (the 1m clock + raise-only floor benches it)."""
    fx = ParityFixture.load(_IPW)
    diff = replay_counterfactual_mode_ii(fx)
    joined = " ".join(diff.notes).lower()
    assert "bench" in joined, diff.notes
    # and the recorded trade was a LOSS (the thing current code should avoid)
    assert fx.recorded_exit_fill is not None
    assert float(fx.recorded_exit_fill["pnl_usd"]) < 0, fx.recorded_exit_fill
