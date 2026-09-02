"""DB-free tests for the stop-noise forensics helpers (2026-09-02).

House style: pure fakes + an AST guard.  No database, no settings, no
network.  Guards the semantics the CC report leans on:

* ``floor_stop`` is widen-only and bounded (never tightens, never widens
  past the structural / prior stop, identity on garbage);
* ``one_minute_true_ranges`` skips halt gaps (no fake zero bars);
* ``simulate_with_stop`` is bid-based, checks the stop first, honours a
  tighten's start time, arms the #1277 burst off the lookback low and prices
  a stop exit with the latency fill model;
* the CLI module never imports a DB / ORM layer (AST guard).
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MOD = _ROOT / "scripts" / "research" / "stop_noise_forensics_0902.py"


def _load():
    spec = importlib.util.spec_from_file_location("stop_noise_forensics_0902", _MOD)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses under `from __future__ import annotations` resolve the owning
    # module through sys.modules — register before exec.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


# ── AST guard: the research CLI must stay DB-free ─────────────────────────────


def test_cli_module_never_imports_db_layers():
    tree = ast.parse(_MOD.read_text(encoding="utf-8"))
    banned = {"sqlalchemy", "psycopg2", "psycopg", "app", "asyncpg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] not in banned, a.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned, node.module


# ── spread_units ──────────────────────────────────────────────────────────────


def test_spread_units_basic_and_unusable(m):
    assert m.spread_units(0.02, 4.30, 4.32) == pytest.approx(1.0)
    assert m.spread_units(0.03, 4.30, 4.32) == pytest.approx(1.5)
    assert m.spread_units(0.02, 4.32, 4.30) is None  # crossed
    assert m.spread_units(0.02, 4.30, 4.30) is None  # zero width
    assert m.spread_units(0.02, None, 4.30) is None
    assert m.spread_units("x", 4.30, 4.32) is None


# ── one_minute_true_ranges ────────────────────────────────────────────────────


def test_true_ranges_skip_halt_gap_and_use_prev_close(m):
    end = 1_000_000
    secs = []
    # bar -5 (start..start+59): 10.0-10.2
    for s in range(end - 300, end - 240, 10):
        secs.append((s, 10.0, 10.2))
    # bars -4, -3, -2 : halted (no prints)
    # bar -1: gap up to 10.8-10.9 -> TR must include the gap from prev close (~10.1)
    for s in range(end - 60, end, 10):
        secs.append((s, 10.8, 10.9))
    trs = m.one_minute_true_ranges(secs, end_epoch=end, minutes=5)
    assert len(trs) == 2  # halt bars are skipped, not zero
    assert trs[0] == pytest.approx(0.2)
    assert trs[1] == pytest.approx(10.9 - 10.1)  # |high - prev_close|


def test_true_ranges_garbage(m):
    assert m.one_minute_true_ranges([], end_epoch="nope") == []
    assert m.one_minute_true_ranges([("x", "y", "z")], end_epoch=100) == []


# ── floor_stop ────────────────────────────────────────────────────────────────


def test_floor_stop_widen_only_and_bounded(m):
    # already outside the floor -> identity
    assert m.floor_stop(entry=4.34, stop=4.2654, min_distance=0.03) == pytest.approx(4.2654)
    # inside the floor -> widened to entry - min_distance
    assert m.floor_stop(entry=4.34, stop=4.3183, min_distance=0.03) == pytest.approx(4.31)
    # bounded: never wider than the prior/structural stop
    assert m.floor_stop(entry=4.34, stop=4.3183, min_distance=0.28, bound=4.2654) == pytest.approx(4.2654)
    # bound above the stop never tightens the stop
    assert m.floor_stop(entry=4.34, stop=4.2654, min_distance=0.01, bound=4.30) == pytest.approx(4.2654)
    # garbage -> identity
    assert m.floor_stop(entry="e", stop=4.3183, min_distance=0.03) == 4.3183
    assert m.floor_stop(entry=4.34, stop=4.3183, min_distance=0.0) == 4.3183
    assert m.floor_stop(entry=4.34, stop=4.3183, min_distance=float("nan")) == 4.3183


# ── simulate_with_stop ────────────────────────────────────────────────────────


def _tape(rows):
    """rows: (epoch, bid_low, bid_high) -> Sec tuples with trade low/high = bid."""
    return [(s, blo, bhi, blo, bhi) for s, blo, bhi in rows]


def test_sim_stop_is_bid_based_checked_first_and_latency_filled(m):
    tape = _tape([
        (101, 4.33, 4.35),
        (102, 4.28, 4.32),   # bid_low <= stop 4.3183 -> breach; bid_high also >= target? no
        (103, 4.20, 4.25),
        (104, 4.12, 4.19),   # latency 2s -> fill on this second's bid_low
        (105, 4.14, 4.20),
    ])
    r = m.simulate_with_stop(tape, entry_epoch=100, entry=4.34, stop=4.3183, target=4.60,
                             end_epoch=200, stop_latency_s=2.0)
    assert r.outcome == "stop" and r.exit_epoch == 102
    assert r.exit_price == pytest.approx(4.12)


def test_sim_stop_wins_over_target_inside_one_second(m):
    tape = _tape([(101, 4.20, 4.70)])
    r = m.simulate_with_stop(tape, entry_epoch=100, entry=4.34, stop=4.2654, target=4.60,
                             end_epoch=200, stop_latency_s=0.0)
    assert r.outcome == "stop"


def test_sim_target_then_open_when_nothing_fires(m):
    tape = _tape([(101, 4.35, 4.40), (102, 4.50, 4.62)])
    r = m.simulate_with_stop(tape, entry_epoch=100, entry=4.34, stop=4.2654, target=4.60,
                             end_epoch=200, burst_enabled=False)
    assert r.outcome == "target" and r.exit_price == pytest.approx(4.60)
    r2 = m.simulate_with_stop(tape, entry_epoch=100, entry=4.34, stop=4.2654, target=None,
                              end_epoch=200, burst_enabled=False)
    assert r2.outcome == "open" and r2.exit_price == pytest.approx(4.50)


def test_sim_tighten_only_binds_from_its_own_time(m):
    tape = _tape([(101, 4.30, 4.31), (150, 4.30, 4.31), (160, 4.29, 4.31)])
    # tighten stop 4.3183 starting at 155: the 4.30 prints at 101/150 must NOT stop it
    r = m.simulate_with_stop(tape, entry_epoch=100, entry=4.34, stop=4.3183, target=None,
                             end_epoch=200, stop_start_epoch=155, burst_enabled=False, stop_latency_s=0.0)
    assert r.outcome == "stop" and r.exit_epoch == 160


def test_sim_burst_arms_off_lookback_low_and_exits_after_clock(m):
    # bid climbs 1.5% off the 60s lookback low at t=110, exit at 110 + 45 + 12 = 167
    rows = [(101, 4.00, 4.00), (105, 4.00, 4.01), (110, 4.06, 4.07)]
    rows += [(t, 4.10, 4.11) for t in range(120, 185, 5)]
    r = m.simulate_with_stop(_tape(rows), entry_epoch=100, entry=4.00, stop=3.90, target=9.0,
                             end_epoch=300)
    assert r.outcome == "burst"
    assert r.burst_armed_epoch == 110
    assert r.exit_epoch == 170  # first populated second >= 110 + 45 + 12
    assert r.exit_price == pytest.approx(4.10)  # bid-low of the exit second


def test_sim_no_tape(m):
    r = m.simulate_with_stop([], entry_epoch=100, entry=4.0, stop=3.9, target=4.2, end_epoch=200)
    assert r.outcome == "no_tape"


def test_latency_fill_fallback_when_tape_ends(m):
    tape = _tape([(101, 4.0, 4.0)])
    assert m.latency_fill(tape, breach_epoch=101, latency_s=30, fallback=3.95) == pytest.approx(3.95)
    assert m.latency_fill(tape, breach_epoch=100, latency_s=1, fallback=3.95) == pytest.approx(4.0)
