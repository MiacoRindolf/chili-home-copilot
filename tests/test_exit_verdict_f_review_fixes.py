"""EXIT VERDICT F -- the review fixes of PR #1385 (2026-09-10), one test per confirmed finding.

DB-free: the state-machine fakes of tests/test_exit_verdict_f_state_machine.py, the `_FakeDB`
of tests/test_exit_verdict_f_no_lookahead.py, and a scripted ledger for the account certifier.

  major  a stale tick never drops the runner batch: the walk (deadman + ratchets) runs before
         the stale gate; stale withholds the DECISIONS (D, D2) only
  minor  the already-decided f sell and a forced whole are not withheld by a quiet tape
  minor  the f sibling (and the OCO tranche) are certified as CHILI-owned open orders
  minor  the runner-start receipt measures the shipped decision -> submit -> fill latency and
         the slippage against the decision bid the acceptance table priced at
  major  the five quote/flow stop-movers cannot lift `pos["stop_price"]` while the verdict
         machine holds the leg (telemetry unchanged)
  minor  the sell fraction: one shipped value, one named fallback, one harness of record in
         every receipt, and the shipped value EVALUATED
  minor  the rung multipliers are named constants shared by the chokepoint and the sibling
  minor  the ratchet read and the base read are delivery-bounded by the TICK, not the print

Runnable: pytest tests/test_exit_verdict_f_review_fixes.py -v
"""
from __future__ import annotations

import ast
import inspect
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.services.trading.momentum_neural import alpaca_orphan_claims as claims
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import exit_verdict as EV
from app.services.trading.momentum_neural import live_runner as lr

from tests.test_exit_verdict_f_state_machine import (
    _DB,
    T_ENTRY,
    Env,
    _le,
    _quiet_tape,
    _runner_leg,
    _sess,
    _shrunk_leg,
    _tick,
)

TICK = inspect.getsource(lr.tick_live_session)
MODULE = inspect.getsource(lr)
DOC = (Path(__file__).resolve().parents[1] / "docs" / "DESIGN" / "EXIT_VERDICT_F.md").read_text(
    encoding="utf-8"
)


# ── major: a stale tick walks the batch (the reviewer's two repros + the flight batch) ───

def test_a_crossing_print_inside_a_slow_tick_gap_exits_on_the_stale_tick_itself(monkeypatch):
    """level-0.05 lands 1 s into the runner; the next held tick is 18 s later on a quiet tape."""
    env, le, _ = _runner_leg(monkeypatch)
    level = le["exit_verdict"]["runner"]["level"]
    env.tape.add(42.0, level - 0.05, 200, aggressor=-1)
    out = _tick(env, le, seconds=60.0)
    assert out["stale"] is True and out["tape_frontier_age_s"] == pytest.approx(18.0)
    assert out["action"] == "runner_deadman" and out["n_batch"] >= 1
    assert out["exit_receipt"]["crossing_print"]["price"] == pytest.approx(level - 0.05)
    assert out["exit_receipt"]["stale"] is True
    u = env.events("live_exit_verdict_unreadable")
    assert u[-1]["why"] == "stale_tape" and u[-1]["walks_and_executions_continue"] is True


def test_the_partials_flight_batch_is_walked_when_the_first_runner_tick_is_stale(monkeypatch):
    """The frontier is rewound to the decision (+37 s) precisely so the first runner batch scans
    the partial's flight: a crossing print at +38 s, the f sell eats the bid, the thin tape goes
    quiet, the first runner tick lands at +50 s (12 s > 7.5 s). Shipped: dropped forever."""
    env, le, _ = _runner_leg(monkeypatch)
    level = le["exit_verdict"]["runner"]["level"]
    env.tape.add(38.0, level - 0.01, 200, aggressor=-1)
    out = _tick(env, le, seconds=50.0)
    assert out["stale"] is True and out["action"] == "runner_deadman"
    rc = out["exit_receipt"]
    assert rc["batch_window"]["frontier_at"] == le["exit_verdict"]["partial"]["decision_as_of"]
    assert rc["crossing_print"]["observed_at"] == (T_ENTRY + timedelta(seconds=38.0)).isoformat()


def test_a_print_dropped_by_the_old_gate_can_never_come_back_so_the_walk_must_run_first(monkeypatch):
    """The reviewer's third tick: after the fix the machine never reaches it in the runner phase
    (the crossing print exited it); pinned so the frontier-advance + walk ORDER cannot regress
    -- the frontier still moves on the stale tick (`frontier_at == as_of`), which is only safe
    because the batch was walked before it moved."""
    env, le, _ = _runner_leg(monkeypatch)
    level = le["exit_verdict"]["runner"]["level"]
    env.tape.add(42.0, level - 0.05, 200, aggressor=-1)
    out = _tick(env, le, seconds=60.0)
    assert out["action"] == "runner_deadman"
    assert le["exit_verdict"]["frontier_at"] == (T_ENTRY + timedelta(seconds=60.0)).isoformat()
    src = inspect.getsource(lr._exit_verdict_tick)
    walk = src.find("walk = _ev_walk_runner_prints(")
    d2_gate = src.rfind('result["withheld"] = "stale_tape"')       # the runner's D2 gate (last)
    frontier = src.find('ev["frontier_at"] = _exit_verdict_iso(as_of)')
    assert 0 < frontier < walk < d2_gate
    # no early return between the stale bookkeeping and the phase branches
    seg = src[src.find('STALE = "do not DECIDE"'): src.find('if phase == "armed":')]
    assert "return result" not in seg and '"why": "stale_tape"' in seg


def test_a_new_high_inside_a_stale_batch_still_ratchets_and_d2_waits_for_the_tape(monkeypatch):
    env, le, _ = _runner_leg(monkeypatch)
    level0 = le["exit_verdict"]["runner"]["level"]
    env.tape.add(46.0, 10.70, 400, aggressor=1)          # new runner high (> the 10.42 fill)
    env.tape.sellers_took_it(47.0, 10.65)                # D fires on the since-high prints
    out = _tick(env, le, seconds=60.0)                   # 11.5 s after the last print: stale
    assert out["stale"] is True and out["action"] is None
    assert out["verdict"]["fired"] is True and out["withheld"] == "stale_tape"
    runner = le["exit_verdict"]["runner"]
    assert runner["saw_new_high"] is True and runner["runner_high"] == 10.70
    assert runner["level"] >= level0                     # the walk ran (ratchet never lowers)
    assert le["exit_verdict"]["phase"] == "runner"
    # the tape speaks again, still weak (>= 4 prints AFTER the gap: the feature's own halt-gap
    # rule restarts the window at a > 7.5-s discontinuity): the fresh tick decides D2
    env.tape.add(61.0, 10.64, 50, aggressor=1)
    env.tape.add(61.2, 10.60, 200, aggressor=-1)
    env.tape.add(61.4, 10.58, 200, aggressor=-1)
    env.tape.add(61.6, 10.55, 300, aggressor=-1)
    out = _tick(env, le, seconds=62.0)
    assert out["stale"] is False and out["action"] == "runner_verdict"
    assert out["verdict"]["binding"] == "all_three" and out["verdict"]["n_since_high"] == 8
    assert out["exit_receipt"]["kind"] == "runner_second_verdict"


def test_the_first_verdict_is_still_withheld_on_a_quiet_tape(monkeypatch):
    """Unchanged doctrine: the FIRST verdict is a decision, not taken on a quiet tape."""
    tape = _quiet_tape()
    tape.sellers_took_it(35.0, 10.45)                    # last print at +36.5 s
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=50.0)                   # 13.5 s > 7.5 s
    assert out["stale"] is True and out["action"] is None
    assert out["verdict"]["fired"] is True and out["withheld"] == "stale_tape"
    assert le["exit_verdict"]["phase"] == "armed"


# ── minor: decided executions are not withheld by a quiet tape ─────────────────

def test_the_decided_f_sell_is_not_withheld_by_a_quiet_tape(monkeypatch):
    """partial_sell_pending is reached only after the deadman was shrunk to R and certified:
    the f shares have NO broker stop until the sibling is POSTed. A quiet tape (common right
    after a marketable sell on a thin name) must not defer that POST."""
    env, le = _shrunk_leg(monkeypatch)                   # last print at +36.5 s
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    le["deadman_stop"]["qty"] = 20.0
    out = _tick(env, le, seconds=50.0)                   # 13.5 s > 7.5 s
    assert out["stale"] is True and out["action"] == "partial_sell"
    assert env.events("live_exit_verdict_unreadable")[-1]["why"] == "stale_tape"


def test_a_forced_whole_after_a_failed_partial_is_not_withheld_by_a_quiet_tape(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    le["deadman_stop"]["qty"] = 20.0
    lr._exit_verdict_partial_failed(
        _DB, _sess(), le, stage="sell", why="sell_attempts_exhausted", fallback="whole_partial_failed",
        as_of=env.now, bid=10.1, to_phase="armed", clear_pending=True, force_whole="whole_partial_failed",
    )
    out = _tick(env, le, seconds=50.0)
    assert out["stale"] is True and out["action"] == "whole_partial_failed"
    assert out["kind"] == "whole_partial_failed"


# ── minor: the f sibling and the OCO tranche are CHILI-owned to the certifier ──

class _LedgerDB:
    """`_certify_alpaca_owned_entry_posture` runs exactly two reads: claims, then the scan."""

    def __init__(self, claim_rows, session_rows):
        self._answers = [claim_rows, session_rows]
        self.statements: list[str] = []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        rows = self._answers.pop(0)
        return SimpleNamespace(fetchall=lambda: rows)


def _live(*, sibling=None, tranche=None, position=True):
    live: dict = {
        "deadman_stop": {"order_id": "dm-oid-2", "client_order_id": "chili_dm_21605_2_def", "qty": 20.0},
    }
    if position:
        live["position"] = {"quantity": 20.0}
    if tranche:
        live["scale_limit_order_id"] = tranche
    if sibling:
        live["exit_verdict"] = {"phase": "partial_sell_pending", "partial": {"f": 11.0, "sell": sibling}}
    return live


def _certify(live, broker_orders):
    snap = {"alpaca_account_scope": "alpaca:paper", "alpaca_account_id": "ACC-1",
            "momentum_live_execution": live}
    db = _LedgerDB([], [(21605, "SKYQ", "alpaca_spot", "live_entered", snap)])
    return claims._certify_alpaca_owned_entry_posture(
        db, broker_positions=[{"product_id": "SKYQ", "qty": 20.0}], broker_orders=broker_orders,
        account_scope="alpaca:paper", alpaca_account_id="ACC-1",
    )


def test_the_resting_f_sibling_is_an_owned_open_order_not_a_deferral_of_every_entry():
    deadman = {"order_id": "dm-oid-2", "client_order_id": "chili_dm_21605_2_def"}
    sibling = {"order_id": "sib-oid-1", "client_order_id": "chili_ml_tv_21605_abc"}
    # the sibling resting beside the deadman: fully owned
    out = _certify(_live(sibling={"order_id": "sib-oid-1", "client_order_id": "chili_ml_tv_21605_abc"}),
                   [deadman, sibling])
    assert out["ok"] is True and out["reason"] == "broker_exposure_fully_owned"
    # an ack-lost sibling (cid durable, no order id yet) is owned by its cid
    out = _certify(_live(sibling={"order_id": None, "client_order_id": "chili_ml_tv_21605_abc",
                                  "phase": "indeterminate"}),
                   [deadman, {"order_id": "sib-oid-x", "client_order_id": "chili_ml_tv_21605_abc"}])
    assert out["ok"] is True
    # the SAME broker order with no sibling recorded is still unowned (the guard is not weakened)
    out = _certify(_live(), [deadman, sibling])
    assert out["ok"] is False and out["reason"] == "alpaca_unowned_open_order_present"
    assert out["broker_order_id"] == "sib-oid-1"


def test_the_oco_tranche_shares_the_fix_and_neither_exists_without_a_position():
    deadman = {"order_id": "dm-oid-2", "client_order_id": "chili_dm_21605_2_def"}
    out = _certify(_live(tranche="oco-1"), [deadman, {"order_id": "oco-1", "client_order_id": "c"}])
    assert out["ok"] is True
    oids, cids = claims.alpaca_ledger_position_sibling_order_ids(
        _live(sibling={"order_id": "s", "client_order_id": "c"}, tranche="oco-1"))
    assert oids == {"s", "oco-1"} and cids == {"c"}
    # no position => no sibling can exist => nothing is whitelisted (the scan cannot miss a row)
    assert claims.alpaca_ledger_position_sibling_order_ids(
        _live(sibling={"order_id": "s", "client_order_id": "c"}, tranche="oco-1", position=False)
    ) == (set(), set())
    assert claims.alpaca_ledger_position_sibling_order_ids(None) == (set(), set())
    # the two nested keys are NOT new exposure markers (the mig-374 index expression is untouched)
    assert "exit_verdict" not in claims.ALPACA_LEDGER_EXPOSURE_MARKERS
    assert "scale_limit_order_id" not in claims.ALPACA_LEDGER_EXPOSURE_MARKERS
    certify = inspect.getsource(claims._certify_alpaca_owned_entry_posture)
    assert "alpaca_ledger_position_sibling_order_ids(live)" in certify


# ── minor: the shipped latency is measured on every partial ────────────────────

def test_the_runner_start_receipt_measures_the_shipped_latency_and_the_slippage(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)                   # decision at +37 s, decision_bid 10.1
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    le["deadman_stop"]["qty"] = 20.0
    le["exit_verdict"]["partial"]["attempts"] = {"shrink": 1, "sell": 1}
    le["exit_verdict"]["partial"]["sell_done"] = {
        "submitted_at_utc": (T_ENTRY + timedelta(seconds=39.5)).isoformat(), "attempt": 1,
    }
    le["pending_exit_reason"] = "tape_sellers_took_it"
    le["pending_exit_quantity"] = 11.0
    le["pending_exit_is_scale_out"] = True
    env.now = T_ENTRY + timedelta(seconds=41.0)
    lr._verdict_partial_to_runner(_DB, _sess(), le=le, filled_quantity=11.0, entry_price=10.0,
                                  fill_price=10.02, reason="tape_sellers_took_it", as_of=env.now)
    r = env.events("live_exit_verdict_runner_started")[-1]
    assert r["decision_bid"] == 10.1
    assert r["decision_as_of"] == (T_ENTRY + timedelta(seconds=37.0)).isoformat()
    assert r["decision_to_submit_s"] == pytest.approx(2.5)
    assert r["decision_to_fill_s"] == pytest.approx(4.0)
    assert r["slippage_vs_decision_bid_usd"] == pytest.approx((10.1 - 10.02) * 11.0)
    assert r["sell_attempts"] == 1 and r["sell_rung"] == 1
    p = le["exit_verdict"]["partial"]
    assert p["decision_to_fill_s"] == pytest.approx(4.0)
    assert p["slippage_vs_decision_bid_usd"] == pytest.approx(0.88)
    assert p["decision_to_submit_s"] == pytest.approx(2.5)


# ── major: the runner is under the tick deadman ONLY ───────────────────────────

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
    assert "armed" in EV.TRAIL_BYPASS_PHASES and "runner" in EV.TRAIL_BYPASS_PHASES


# ── minor: one fraction, one fallback, one harness of record ───────────────────

def test_the_fraction_has_one_shipped_value_one_named_fallback_and_is_evaluated(monkeypatch):
    field = Settings.model_fields["chili_momentum_exit_verdict_sell_fraction"]
    assert field.default == 24 / 32
    assert "F(0.75,255) = -495.7" in field.description and "never a third number" in field.description
    # the getattr fallback is the doctrine 0.5 -- not 11/31, not 0.75
    src = inspect.getsource(lr._exit_verdict_settings)
    assert "11 / 31" not in src and "11/31" not in src and "_EV_SELL_FRACTION_FALLBACK" in src
    assert lr._EV_SELL_FRACTION_FALLBACK == EV.SELL_FRACTION_FALLBACK == 0.5
    assert lr._exit_verdict_settings()["sell_fraction"] == pytest.approx(
        settings.chili_momentum_exit_verdict_sell_fraction)
    monkeypatch.setattr(lr, "settings", SimpleNamespace())
    assert lr._exit_verdict_settings()["sell_fraction"] == 0.5
    # the receipt derivation cites the harness of record and the shipped value's own P&L
    for tok in ("-502.18", "F(0.75, shipped) -495.7", "tick-by-tick"):
        assert tok in EV._EXIT_VERDICT_DERIVATION, tok
    assert "F(0.75) = -495.7" in EV._SELL_FRACTION_DERIVATION
    # no comment in the machine still names 11/31 as the fraction
    assert "fraction = 11/31" not in MODULE and "F(11/31) -129.60" not in MODULE
    assert "-232.62" not in MODULE
    # the design doc's binding row is the shipped value, evaluated
    assert "11/31 = 0.3548 of the CURRENT position" not in DOC
    assert "24/32 = 0.75 of the CURRENT position" in DOC and "F(0.75) −495.7" in DOC


# ── minor: the rung ladder is ONE named shape ──────────────────────────────────

def test_the_chokepoint_and_the_sibling_price_one_named_ladder():
    assert (lr._EXIT_LADDER_GUARD_MULT_RUNG1, lr._EXIT_LADDER_GUARD_MULT_RUNG2,
            lr._EXIT_LADDER_GUARD_MULT_EXTENDED) == (1.0, 4.0, 8.0)
    g = lr._notional_guard_multiplier() - 1.0
    assert lr._exit_ladder_guard_fraction(attempt=1, extended=False) == pytest.approx(g)
    assert lr._exit_ladder_guard_fraction(attempt=2, extended=False) == pytest.approx(4.0 * g)
    assert lr._exit_ladder_guard_fraction(attempt=3, extended=False) == pytest.approx(4.0 * g)
    assert lr._exit_ladder_guard_fraction(attempt=1, extended=True) == pytest.approx(8.0 * g)
    impl = inspect.getsource(lr._submit_live_market_exit_impl)
    rung = inspect.getsource(lr._exit_verdict_sell_rung)
    for src in (impl, rung):
        assert "_exit_ladder_guard_fraction(" in src
        for literal in ("* 8.0", "else 4.0", "4.0 * g", "8.0 * g"):
            assert literal not in src, literal
    for attempt in (1, 2):
        t, px = lr._exit_verdict_sell_rung(attempt=attempt, bid=9.95, mid=9.96, extended=False)
        assert t == "limit"
        assert px == pytest.approx(9.95 * (1.0 - lr._exit_ladder_guard_fraction(attempt=attempt, extended=False)))
    assert lr._exit_verdict_sell_rung(attempt=3, bid=9.95, mid=9.96, extended=False) == ("market", None)
    t, px = lr._exit_verdict_sell_rung(attempt=5, bid=9.95, mid=9.96, extended=True)
    assert t == "limit" and px == pytest.approx(9.95 * (1.0 - 8.0 * g))


# ── minor: the ratchet read and the base read are delivery-bounded by the TICK ─

def _recording(monkeypatch, env):
    calls: list[dict] = []
    real = env.tape.signed_tape_accel_features

    def _rec(symbol, **kw):
        calls.append(dict(kw))
        return real(symbol, **kw)

    monkeypatch.setattr(EG, "signed_tape_accel_features", _rec)
    return calls


def test_the_ratchet_read_is_delivery_bounded_by_the_tick_not_the_new_high_print(monkeypatch):
    env, le, _ = _runner_leg(monkeypatch)
    calls = _recording(monkeypatch, env)
    env.tape.add(46.0, 10.70, 400, aggressor=1)          # a new runner high => one ratchet read
    out = _tick(env, le, seconds=48.0)
    assert out["action"] is None and le["exit_verdict"]["runner"]["saw_new_high"] is True
    reads = [c for c in calls if c.get("window_prints")]
    assert len(reads) == 1
    assert reads[0]["as_of"] == T_ENTRY + timedelta(seconds=46.0)          # observed up to the print
    assert reads[0]["available_by"] == T_ENTRY + timedelta(seconds=48.0)   # delivered by the TICK


def test_the_base_read_at_the_fill_is_delivery_bounded_by_the_tick(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    le["deadman_stop"]["qty"] = 20.0
    le["pending_exit_reason"] = "tape_sellers_took_it"
    le["pending_exit_quantity"] = 11.0
    le["pending_exit_is_scale_out"] = True
    calls = _recording(monkeypatch, env)
    env.now = T_ENTRY + timedelta(seconds=41.0)
    lr._verdict_partial_to_runner(_DB, _sess(), le=le, filled_quantity=11.0, entry_price=10.0,
                                  fill_price=10.42, reason="tape_sellers_took_it", as_of=env.now)
    assert calls[-1]["as_of"] == T_ENTRY and calls[-1]["available_by"] == env.now
    assert le["exit_verdict"]["runner"]["level"] == 9.85          # the measured base, unchanged
