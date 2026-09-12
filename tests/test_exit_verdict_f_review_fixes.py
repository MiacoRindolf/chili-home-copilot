"""EXIT VERDICT G -- the adversarial findings on PR #1385 (2026-09-10), one test per finding
that survives Amendment 2 (the WHOLE position at the trigger; no partial, no runner).

  major  the stale-tick gate advanced `frontier_at` past UNWALKED prints -- now the frontier
         is the LAST WALKED print's tuple, an unreadable batch never moves it, and a crossing
         print inside a > 7.5-s gap exits on the stale tick itself (the tick always answers)
  major  quote/flow stop-movers could lift `pos["stop_price"]` on the held phase -- every
         write is under `not _ev_trail_bypass`, which is every equity leg with a readable
         anchor from the first held tick; nothing after the whole exit acts on the leg
  minor  the stale gate withheld the decided sell -- the decision is written ahead and
         submitted on the same tick; exit_pending never re-reads the tape
  minor  the certifier did not own the f sibling -- there is no sibling; the OCO-tranche
         whitelist (fixed in passing) stays and nothing under `exit_verdict` is whitelisted
  minor  the shipped fraction 0.75 was never evaluated -- exit_fraction = 1.0 is a reported
         constant with the 78-leg derivation; no knob; no receipt cites the partial-era numbers
  minor  4x / 8x rung literals duplicated in the f-sell ladder -- the ladder has ONE source
         and ONE caller (the chokepoint); the sibling ladder is gone
  minor  the ratchet read's delivery bound excluded the new-high print -- the base read is
         delivery-bounded by the TICK; the ratchet read IS the tick read (the last print is
         inside it, inclusive), as the measurement's `_tick_stop_at(sym, ts)`
  minor  a Massive snapshot bid could price the HELD decision -- every verdict receipt carries
         the [48] envelope (`bbo_source`, `bbo_age_s`, `bbo_fallback_engaged`; L1 first, never
         a snapshot) and the exit is priced by the chokepoint from the tick's bid
  minor  the acceptance table priced the exit at the decision tick -- the shipped latency is
         measured on the real seam in tests/test_exit_verdict_g_whole_exit_seam.py

Runnable: pytest tests/test_exit_verdict_f_review_fixes.py -v   (DB-free)
"""
from __future__ import annotations

import ast
import inspect
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.trading.momentum_neural import alpaca_orphan_claims as claims
from app.services.trading.momentum_neural import exit_verdict as EV
from app.services.trading.momentum_neural import live_runner as lr

from tests.test_exit_verdict_f_state_machine import (
    Env,
    T_ENTRY,
    _after_submit,
    _le,
    _quiet_tape,
    _spike_sells,
    _spike_tape,
    _tick,
)

TICK = inspect.getsource(lr.tick_live_session)
MODULE = inspect.getsource(lr)
VERDICT = inspect.getsource(lr._exit_verdict_tick)
DOC = (Path(__file__).resolve().parents[1] / "docs" / "DESIGN" / "EXIT_VERDICT_F.md").read_text(
    encoding="utf-8"
)


# ── major: the frontier never passes an unwalked print ─────────────────────────

def test_a_crossing_print_inside_a_slow_tick_gap_exits_on_the_stale_tick_itself(monkeypatch):
    """The reviewer's repro: the crossing print lands 1 s after the previous tick; the next
    held tick is 18 s later on a quiet tape (79.5% of live held ticks are > 7.5 s apart)."""
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=36.0)
    level = le["exit_verdict"]["deadman"]["level"]
    tape.add(37.0, level - 0.05, 200, aggressor=-1)
    out = _tick(env, le, seconds=55.0)
    assert out["stale"] is True and out["tape_frontier_age_s"] == pytest.approx(18.0)
    assert out["action"] == "tick_deadman" and out["n_batch"] == 1
    assert out["exit_receipt"]["crossing_print"]["price"] == pytest.approx(level - 0.05)
    assert out["exit_receipt"]["stale"] is True
    assert le["exit_verdict"]["frontier_at"] == (T_ENTRY + timedelta(seconds=37.0)).isoformat()


def test_the_frontier_is_the_last_walked_print_and_an_unreadable_batch_leaves_it(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=36.0)
    last = tape.rows[-1]
    assert (le["exit_verdict"]["frontier_at"], le["exit_verdict"]["frontier_id"]) == (last[5].isoformat(), last[6])
    tape.add(37.0, 9.80, 200, aggressor=-1)                 # a crossing print the next read must see
    tape.fail_with = {"why": "timeout", "error": "OperationalError"}
    out = _tick(env, le, seconds=40.0)
    assert out == {"action": None, "unreadable": "timeout"}
    assert (le["exit_verdict"]["frontier_at"], le["exit_verdict"]["frontier_id"]) == (last[5].isoformat(), last[6])
    tape.fail_with = None
    out = _tick(env, le, seconds=43.0)
    assert out["action"] == "tick_deadman"                  # nothing was skipped
    # source: the frontier is written from the walk, never from the tick's as_of
    assert 'ev["frontier_at"] = _exit_verdict_iso(walk["frontier"][0])' in VERDICT
    assert 'ev["frontier_at"] = _exit_verdict_iso(as_of)' not in VERDICT
    walk = inspect.getsource(EV.walk_held_prints)
    assert "frontier = (_at(row), _id(row))" in walk and walk.find("frontier = (_at(row), _id(row))") < walk.find("break")


# ── major: nothing lifts the bid-stop while the verdict holds the leg ──────────

def test_every_quote_or_flow_stop_mover_write_is_guarded_by_the_verdict_phase():
    """The chandelier (`_trailed`), the measured-move composite (`_cand`), the OFI exhaustion
    lock, the tape-accel reversal exit, the sell-into-strength ladder and the ask-side pressure
    lock all write `pos["stop_price"]`; every write must sit under an `if` whose test carries
    `not _ev_trail_bypass`. The telemetry receipts of the five stay (A/B counterfactuals)."""
    fn = ast.parse(TICK).body[0]
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    movers = {"_trailed", "_cand", "_lock_stop", "_ar_stop", "_sis_stop", "_asp_stop"}
    seen: set[str] = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        if ast.unparse(node.targets[0]) != "pos['stop_price']":
            continue
        if not (isinstance(node.value, ast.Name) and node.value.id in movers):
            continue
        seen.add(node.value.id)
        guarded = False
        cur: ast.AST = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.If) and "not _ev_trail_bypass" in ast.unparse(cur.test):
                guarded = True
                break
        assert guarded, f"pos['stop_price'] = {node.value.id} is not under `not _ev_trail_bypass`"
    assert seen == movers, seen
    for receipt in ("live_measured_move_exit", "live_ofi_exhaustion_lock", "live_tape_accel_reversal_exit",
                    "live_sell_into_strength", "live_ask_side_pressure"):
        assert f'"{receipt}"' in TICK, receipt
    assert EV.TRAIL_BYPASS_PHASES == {"armed", "exit_pending"} and "runner" not in EV.PHASES


def test_nothing_after_the_whole_exit_acts_on_the_leg(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    _spike_sells(tape)
    out = _tick(env, le, seconds=12.5)
    assert out["action"] == "accel_rollover"
    _after_submit(le, out)
    assert lr._exit_verdict_phase(le) in EV.TRAIL_BYPASS_PHASES          # the movers stay bypassed
    assert lr._exit_verdict_phase(le) in EV.FIRST_TARGET_BYPASS_PHASES   # the first-target stays out
    n_reads, n_emits = len(tape.reads), len(env.emitted)
    calls_before = len(env.calls)

    def no_broker_submit(*args, **kwargs):
        raise AssertionError("an already-pending verdict must not submit another exit")

    monkeypatch.setattr(lr, "_submit_live_market_exit", no_broker_submit)
    tape.add(13.0, 9.50, 900, aggressor=-1)
    assert _tick(env, le, seconds=14.0) == {"action": None, "phase": "exit_pending"}
    assert len(tape.reads) == n_reads
    event, payload = env.emitted[n_emits:][0]
    assert len(env.emitted[n_emits:]) == 1 and event == "live_exit_evaluation"
    assert payload["result"] == {"action": None, "phase": "exit_pending"}
    assert payload["reads"] == [] and payload["feature_pointer_advanced"] is False
    assert payload["pre"] == payload["post"]  # decision, position, protection and pending identity unchanged
    assert set(env.calls[calls_before:]) <= {"commit"}  # optional audit bookkeeping only; no wake


# ── minor: a decided exit is never withheld ────────────────────────────────────

def test_the_decision_is_durable_before_the_submit_and_exit_pending_never_reads_the_tape(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    _spike_sells(tape)
    out = _tick(env, le, seconds=12.5)
    assert out["action"] == "accel_rollover"
    # the LAST commit before the caller's submit already carries the decision (write-ahead)
    assert env.commits[-1]["exit_verdict"]["phase"] == "exit_pending"
    assert env.commits[-1]["exit_verdict"]["exit"]["trigger"] == "accel_rollover"
    _after_submit(le, out)
    n_reads = len(tape.reads)
    for s in (13.0, 30.0, 60.0):                             # quiet, stale, whatever: no read, no gate
        assert _tick(env, le, seconds=s) == {"action": None, "phase": "exit_pending"}
    assert len(tape.reads) == n_reads
    # source: between the action and the submit in the tick there is no stale check
    j = TICK.find('_ev_action = str(_ev.get("action") or "")')
    k = TICK.find("sr = _submit_live_market_exit(", j)
    assert 0 < j < k and "stale" not in TICK[j: k] and "withheld" not in TICK[j: k]


# ── minor: the certifier -- the tranche stays whitelisted, nothing else ────────

class _LedgerDB:
    """`_certify_alpaca_owned_entry_posture` runs exactly two reads: claims, then the scan."""

    def __init__(self, claim_rows, session_rows):
        self._answers = [claim_rows, session_rows]
        self.statements: list[str] = []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        rows = self._answers.pop(0)
        return SimpleNamespace(fetchall=lambda: rows)


def _live(*, tranche=None, position=True, marker=None):
    live: dict = {
        "deadman_stop": {"order_id": "dm-oid-2", "client_order_id": "chili_dm_21605_2_def", "qty": 20.0},
    }
    if position:
        live["position"] = {"quantity": 20.0}
    if tranche:
        live["scale_limit_order_id"] = tranche
    if marker:
        live["exit_verdict"] = marker
    return live


def _certify(live, broker_orders):
    snap = {"alpaca_account_scope": "alpaca:paper", "alpaca_account_id": "ACC-1",
            "momentum_live_execution": live}
    db = _LedgerDB([], [(21605, "SKYQ", "alpaca_spot", "live_entered", snap)])
    return claims._certify_alpaca_owned_entry_posture(
        db, broker_positions=[{"product_id": "SKYQ", "qty": 20.0, "side": "long"}], broker_orders=broker_orders,
        account_scope="alpaca:paper", alpaca_account_id="ACC-1",
    )


def test_the_oco_tranche_is_owned_and_nothing_under_the_verdict_marker_is_whitelisted():
    deadman = {"order_id": "dm-oid-2", "client_order_id": "chili_dm_21605_2_def"}
    out = _certify(_live(tranche="oco-1"), [deadman, {"order_id": "oco-1", "client_order_id": "c"}])
    assert out["ok"] is True and out["reason"] == "broker_exposure_fully_owned"
    # a stray open order is still unowned -- the guard is not weakened by the verdict marker
    stray = {"order_id": "sib-oid-1", "client_order_id": "chili_ml_tv_21605_abc"}
    marker = {"phase": "exit_pending", "exit": {"trigger": "accel_rollover"},
              "partial": {"sell": {"order_id": "sib-oid-1", "client_order_id": "chili_ml_tv_21605_abc"}}}
    out = _certify(_live(marker=marker), [deadman, stray])
    assert out["ok"] is False and out["reason"] == "alpaca_unowned_open_order_present"
    assert out["broker_order_id"] == "sib-oid-1"
    assert claims.alpaca_ledger_position_sibling_order_ids(_live(marker=marker, tranche="oco-1")) == ({"oco-1"}, set())
    assert claims.alpaca_ledger_position_sibling_order_ids(_live(tranche="oco-1", position=False)) == (set(), set())
    assert claims.alpaca_ledger_position_sibling_order_ids(None) == (set(), set())
    assert "exit_verdict" not in claims.ALPACA_LEDGER_EXPOSURE_MARKERS
    assert "scale_limit_order_id" not in claims.ALPACA_LEDGER_EXPOSURE_MARKERS
    assert "exit_verdict" not in inspect.getsource(claims.alpaca_ledger_position_sibling_order_ids).split('"""')[2]


# ── minor: the fraction is 1.0, reported, evaluated on 78 legs; no knob ────────

def test_the_exit_fraction_is_reported_with_the_78_leg_derivation_and_no_partial_era_number_survives(monkeypatch):
    assert "chili_momentum_exit_verdict_sell_fraction" not in Settings.model_fields
    assert EV.EXIT_FRACTION == lr._EV_EXIT_FRACTION == 1.0
    for tok in ("+157.52", "-59.25", "-1,216.28", "78 live Alpaca legs", "+217", "unmeasured, not refuted"):
        assert tok in EV._EXIT_FRACTION_DERIVATION, tok
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    _spike_sells(tape)
    _tick(env, le, seconds=12.5)
    for name in ("live_exit_verdict_armed", "live_exit_verdict_fired"):
        for r in env.events(name):
            assert r["exit_fraction"] == 1.0 and r["exit_fraction_derivation"] == EV._EXIT_FRACTION_DERIVATION
    for tok in ("0.75", "24/32", "11/31", "8/32", "-495.7", "-502.18", "sell_fraction"):
        assert tok not in inspect.getsource(EV), tok
        assert tok not in VERDICT, tok
    assert "exit_fraction | 1.0" in DOC or "| exit_fraction | **1.0**" in DOC
    assert "24/32 = 0.75 of the CURRENT position" not in DOC


# ── minor: ONE ladder, ONE caller ──────────────────────────────────────────────

def test_the_chokepoint_prices_one_named_ladder_and_the_sibling_ladder_is_gone():
    assert (lr._EXIT_LADDER_GUARD_MULT_RUNG1, lr._EXIT_LADDER_GUARD_MULT_RUNG2,
            lr._EXIT_LADDER_GUARD_MULT_EXTENDED) == (1.0, 4.0, 8.0)
    g = lr._notional_guard_multiplier() - 1.0
    assert lr._exit_ladder_guard_fraction(attempt=1, extended=False) == pytest.approx(g)
    assert lr._exit_ladder_guard_fraction(attempt=2, extended=False) == pytest.approx(4.0 * g)
    assert lr._exit_ladder_guard_fraction(attempt=3, extended=False) == pytest.approx(4.0 * g)
    assert lr._exit_ladder_guard_fraction(attempt=1, extended=True) == pytest.approx(8.0 * g)
    impl = inspect.getsource(lr._submit_live_market_exit_impl)
    assert "_exit_ladder_guard_fraction(" in impl
    for literal in ("* 8.0", "else 4.0", "4.0 * g", "8.0 * g"):
        assert literal not in impl, literal
    assert not hasattr(lr, "_exit_verdict_sell_rung")
    assert MODULE.count("_exit_ladder_guard_fraction(") == 3        # the def + the chokepoint's two rungs


# ── minor: the reads' delivery bounds ──────────────────────────────────────────

def test_the_base_read_is_delivery_bounded_by_the_tick_and_the_ratchet_read_is_the_tick_read(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=36.0)
    feats = [p for k, p in tape.reads if k == "feats"]
    assert len(feats) == 2
    base, tick = feats
    assert base["as_of"] == T_ENTRY and base["available_by"] == T_ENTRY + timedelta(seconds=36.0)
    assert tick["as_of"] == T_ENTRY + timedelta(seconds=36.0) and "available_by" not in tick
    # the tick read includes the last print (inclusive), as the measurement's `_tick_stop_at`
    f = tape.signed_tape_accel_features("SKYQ", as_of=env.now, window_prints=255)
    assert f["last_print"] == tape.rows[-1][0] == le["exit_verdict"]["last_print"]
    assert out["accel_now"] == f["signed_tape_accel"]
    assert VERDICT.count("_tape_feats(") == 2                    # the base read and the tick read, nothing else


# ── minor: the bid's provenance is on every receipt; the exit is priced at the tick's bid ─

def test_every_verdict_receipt_carries_the_48_envelope_and_the_exit_is_priced_at_the_ticks_bid(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    le["last_held_execution_bbo"] = {"bbo_selector_version": "test", "bbo_source": "iqfeed_l1",
                                     "bbo_age_s": 0.31, "bbo_fallback_engaged": False}
    _tick(env, le, seconds=4.0, bid=10.33)
    _spike_sells(tape)
    _tick(env, le, seconds=12.5, bid=10.24)
    for name, payload in env.emitted:
        if name.startswith("live_exit_verdict") or name.startswith("live_tick_deadman"):
            assert payload["bbo_source"] == "iqfeed_l1" and payload["bbo_age_s"] == 0.31, name
            assert payload["bbo_fallback_engaged"] is False, name
    fired = env.events("live_exit_verdict_fired")[0]
    assert fired["bid"] == 10.24 and le["exit_verdict"]["exit"]["bid"] == 10.24
    # no envelope => the receipt says so instead of inventing a source
    le2 = _le(qty=31.0)
    le2.pop("last_held_execution_bbo")
    r = lr._exit_verdict_receipt_base(SimpleNamespace(state="live_entered"), le2, as_of=env.now, bid=10.0)
    assert r["bbo_source"] is None and r["bbo_receipt"] == "no_held_bbo_envelope"
    # the elif hands the tick's bid / ask / mid to the chokepoint, which prices the rungs
    j = TICK.find("sr = _submit_live_market_exit(", TICK.find('_ev_action = str(_ev.get("action") or "")'))
    assert "bid=bid, ask=ask, mid=mid" in TICK[j: j + 500]
