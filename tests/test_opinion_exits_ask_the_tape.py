"""OPINION EXITS MUST ASK THE TAPE -- the tick exit is primary (2026-09-10, [21]).

FOUND on the running tree: 13 call sites of ``_transition_to_bailout`` in live_runner.py and
NONE reads a print. Four of the ON sites are OPINIONS -- a reading of a QUOTE, a BAR, a
WALL-CLOCK window or a candle shape -- and once any of them set STATE_LIVE_BAILOUT the tick
exit (``momentum_break_stop``, evaluated only in ENTERED/TRAILING) was never consulted again:

    breakout_failed_fast_bail   bid < level inside a clock window      (quote + wall clock)
    lost_vwap_confirmed         1m bar close + bid below VWAP margin   (bar + quote)
    close_below_structure       closed 1m/5m bar below the swing low   (bar)
    topping_tail_runner_exit    15-min candle shape                    (candle)

7-day live ledger: breakout_failed_fast_bail 10 fires, -$481.82, 0 wins, ALL 10 had a higher
print within 15 min (+0.75%..+20.57%). PCLA 09-10 13:41:05: bid 8.96 < level 9.07 after
33.8 s -> 542 sh market-sold; the 8.88 print was OUR OWN sweep, and price was back at 9.23
in 5 min. -$146.34 instead of ~+$43.

MEASURED BEFORE SHIPPING (scratchpad opinion_exit_counterfactual_v2.py, read-only, symbol-
scoped, own exit prints excluded, tick exit judged on the prod NBBO-mid 10-s frame), every
opinion exit on mode='live' in the 7 days to 2026-09-10, held to "deadman-or-tick-exit,
15-min cap". The table is pinned below as fixed expectations.

    15 armed-site legs      actual  -521.78
      deadman = entry - sizing.stop_distance  ->  -456.19   (+65.59;  tick exit 6/15,
                                                              deadman 10/15, 15-min 3/15)
      deadman = the stop actually resting     ->  -336.47   (+185.31; deadman 6/15)
    7 viability-floor legs  actual   -50.88   ->  -214.89 / -186.73   (WORSE, 6 of 7)

So the four opinion sites now ARM the tick exit and stay held (the tape or the deadman is
the exit), and the viability-floor site -- which the brief asked to delete -- is KEPT,
because the same measurement says deleting it loses money. A guard is judged by what it did
on the tape, not by what it reads.

[44]/[21]/[47] (2026-09-10, later the same day): what the sites ARM is now the PRINT-INDEXED
verdict F (`exit_verdict.py`, docs/DESIGN/EXIT_VERDICT_F.md), not `momentum_break_stop` --
which is a 10-s quote-mid bar, itself an opinion. The break elif became the FIFTH arming
site (`momentum_break_bars`); `momentum_break_stop` survives only as the named fallback for
crypto (-USD) and an unreadable entry-fill anchor. The tables below stand as measured.

Runnable: pytest tests/test_opinion_exits_ask_the_tape.py -v   (DB-free)
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as lr

ARMED_REASONS = {
    "breakout_failed_fast_bail",
    "lost_vwap_confirmed",
    "close_below_structure",
    "topping_tail_runner_exit",
    # [44] 2026-09-10: the 10-s quote-mid bar exit is an opinion too -- it arms now.
    "momentum_break_bars",
}
RETIRED_BAILOUT_EVENTS = {"live_lost_vwap_flatten", "live_bos_exit"}


# ── the AST view of live_runner.py ──────────────────────────────────────────────

def _module_tree() -> ast.Module:
    return ast.parse(inspect.getsource(lr))


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def _const(node: ast.AST):
    return node.value if isinstance(node, ast.Constant) else None


def _emit_reasons(tree: ast.AST, event: str) -> set[str]:
    """Every literal ``"reason"`` an ``_emit(db, sess, <event>, {...})`` call carries."""
    out: set[str] = set()
    for call in _calls_named(tree, "_emit"):
        if len(call.args) < 4 or _const(call.args[2]) != event:
            continue
        payload = call.args[3]
        if not isinstance(payload, ast.Dict):
            continue
        for k, v in zip(payload.keys, payload.values):
            if _const(k) == "reason" and _const(v) is not None:
                out.add(str(_const(v)))
    return out


def test_the_four_opinion_sites_no_longer_transition_to_bailout():
    """Positive on both sides: the four reasons are gone from every ``live_bailout`` emit
    AND present, exactly once each, as ``_arm_opinion_exit(reason=...)`` calls -- plus the
    fifth, the break elif's ``momentum_break_bars`` ([44])."""
    tree = _module_tree()
    bailout_reasons = _emit_reasons(tree, "live_bailout")
    assert not (bailout_reasons & ARMED_REASONS), bailout_reasons & ARMED_REASONS
    # the two dedicated bailout events of the lost-VWAP / BOS sites are retired
    for call in _calls_named(tree, "_emit"):
        if len(call.args) >= 3:
            assert _const(call.args[2]) not in RETIRED_BAILOUT_EVENTS, _const(call.args[2])
    armed: list[str] = []
    for call in _calls_named(tree, "_arm_opinion_exit"):
        kw = {k.arg: k.value for k in call.keywords}
        assert "reason" in kw and "prior_event" in kw and "inputs" in kw
        armed.append(str(_const(kw["reason"])))
    assert sorted(armed) == sorted(ARMED_REASONS), armed


def test_they_emit_the_arming_receipt_with_the_inputs_the_bailout_used_to_carry():
    src = inspect.getsource(lr._arm_opinion_exit)
    assert '"live_opinion_exit_armed"' in src
    assert "**payload_inputs" in src and '"derivation"' in src
    # each site still hands over its decision inputs (the receipt = the decision INPUTS)
    tick = inspect.getsource(lr.tick_live_session)
    for anchor in ('"breakout_level": le.get("breakout_level_price")',
                   '"session_vwap": _lv_vwap', '"last_close": _bos_close',
                   '"high_water_mark": _float_or_none(pos.get("high_water_mark"))'):
        assert anchor in tick, anchor


def test_the_bailout_helper_has_nine_callers_left_and_none_of_them_is_an_opinion_site():
    tree = _module_tree()
    n = len(_calls_named(tree, "_transition_to_bailout"))
    assert n == 9, f"13 sites before [21], 4 re-routed to arming; found {n}"
    # the USD risk caps are untouched
    bailout_reasons = _emit_reasons(tree, "live_bailout")
    assert {"max_loss_per_trade", "max_loss_circuit"} <= bailout_reasons


def test_the_sites_stay_in_entered_or_trailing_so_the_tick_exit_stays_reachable():
    """The whole point: ``momentum_break_stop`` is evaluated in ENTERED/TRAILING only, so the
    arming must not move the state. Asserted on the helper, not on a site."""
    fn = ast.parse(inspect.getsource(lr._arm_opinion_exit))
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert not ({"_safe_transition", "_transition_to_bailout"} & called), called
    assert not any(isinstance(n, ast.Name) and n.id == "STATE_LIVE_BAILOUT" for n in ast.walk(fn))
    tick = inspect.getsource(lr.tick_live_session)
    i = tick.find('"chili_momentum_failed_pop_break_exit_enabled"')
    assert "st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)" in tick[i - 800:i + 300]


def test_the_tick_exit_receipt_carries_the_armed_marker():
    """[44]: the tick exit is the print verdict; EVERY verdict receipt (the partial decision
    included) carries the armed marker through `_exit_verdict_receipt_base`; the -USD /
    unreadable-anchor fallback (`live_momentum_break_exit`) still carries it directly."""
    verdict = inspect.getsource(lr._exit_verdict_tick)
    i = verdict.find('"live_exit_verdict_partial"')
    assert i > 0
    assert "**base" in verdict[i: i + 400]
    base = inspect.getsource(lr._exit_verdict_receipt_base)
    assert '"opinion_exit_armed": _opinion_exit_armed_receipt(le, now=as_of)' in base
    tick = inspect.getsource(lr.tick_live_session)
    j = tick.find('_emit(db, sess, "live_momentum_break_exit"')
    assert j > 0
    assert '"opinion_exit_armed": _opinion_exit_armed_receipt(le)' in tick[j: j + 700]
    src = inspect.getsource(lr._opinion_exit_armed_receipt)
    assert '"seconds_armed"' in src and '"reason"' in src


def test_the_viability_floor_bailout_is_kept_because_the_measurement_said_so():
    """The brief asked to delete it. Measured: -50.88 actual vs -214.89 / -186.73 held, worse
    on 6 of 7 legs. Per the brief's own rule ("if the counterfactual is NOT better in
    aggregate, STOP and report") the site stays -- now with a ``reason`` on its receipt."""
    tree = _module_tree()
    assert "viability_floor" in _emit_reasons(tree, "live_bailout")
    tick = inspect.getsource(lr.tick_live_session)
    i = tick.find('params["bailout_viability_floor"]')
    assert i > 0
    assert "-$214.89" in tick[i - 1500:i] and "6 of the 7" in tick[i - 1500:i]


def test_the_armed_marker_is_cleared_on_recycle():
    """A per-leg marker that survives a recycle mislabels the NEXT leg's tick exit as armed
    by the previous leg's opinion -- the burst-stamp shape, in the receipt. [44]: the verdict
    machine's whole marker goes with it (phase, leg high, frontier, pending partial)."""
    assert "opinion_exit_armed" in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "exit_verdict" in lr._RECYCLE_ENTRY_STATE_KEYS


def test_the_derivation_travels_with_the_change():
    text = lr._OPINION_EXIT_ARM_DERIVATION
    for token in ("2026-09-10", "15", "-521.78", "-456.19", "-336.47", "6/15"):
        assert token in text, token


# ── the arming helper, unit-tested with the receipt and the marker faked ────────

class _Le(dict):
    pass


def _fake_env(monkeypatch, *, now: datetime):
    emitted: list[tuple[str, dict]] = []
    committed: list[dict] = []
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload: emitted.append((ev, dict(payload))))
    monkeypatch.setattr(lr, "_commit_le", lambda sess, le: committed.append(dict(le)))
    monkeypatch.setattr(lr, "_utcnow", lambda: now)
    return emitted, committed


def test_arming_writes_the_marker_and_one_receipt_and_is_idempotent_per_reason(monkeypatch):
    now = datetime(2026, 9, 10, 13, 41, 5, 800540)
    emitted, committed = _fake_env(monkeypatch, now=now)
    sess = SimpleNamespace(id=21592, state="live_entered")
    le: dict = {"position": {"quantity": 542.0}}
    ok = lr._arm_opinion_exit(
        object(), sess, le, reason="breakout_failed_fast_bail", prior_event="live_bailout",
        inputs={"bid": 8.96, "breakout_level": 9.07, "held_seconds": 33.786},
    )
    assert ok is True
    armed = le["opinion_exit_armed"]
    assert armed["reason"] == "breakout_failed_fast_bail"
    assert armed["reasons"] == ["breakout_failed_fast_bail"]
    assert armed["at_utc"] == now.isoformat()
    assert armed["inputs"] == {"bid": 8.96, "breakout_level": 9.07, "held_seconds": 33.786}
    assert len(committed) == 1 and len(emitted) == 1
    ev, payload = emitted[0]
    assert ev == "live_opinion_exit_armed"
    assert payload["reason"] == "breakout_failed_fast_bail"
    assert payload["prior_event"] == "live_bailout"
    assert payload["bid"] == 8.96 and payload["breakout_level"] == 9.07
    assert payload["armed_at_utc"] == now.isoformat()
    assert payload["state"] == "live_entered"
    assert payload["derivation"] == lr._OPINION_EXIT_ARM_DERIVATION
    # the same opinion on the next pass: no second receipt, no rewrite (6,765-event lesson)
    again = lr._arm_opinion_exit(
        object(), sess, le, reason="breakout_failed_fast_bail", prior_event="live_bailout",
        inputs={"bid": 8.95},
    )
    assert again is False
    assert len(committed) == 1 and len(emitted) == 1
    assert le["opinion_exit_armed"]["inputs"]["bid"] == 8.96


def test_a_second_distinct_opinion_on_the_same_leg_is_recorded_not_dropped(monkeypatch):
    t0 = datetime(2026, 9, 10, 14, 43, 44)
    emitted, _ = _fake_env(monkeypatch, now=t0)
    sess = SimpleNamespace(id=21610, state="live_entered")
    le: dict = {}
    assert lr._arm_opinion_exit(object(), sess, le, reason="lost_vwap_confirmed",
                                prior_event="live_lost_vwap_flatten", inputs={"bid": 9.26})
    monkeypatch.setattr(lr, "_utcnow", lambda: t0 + timedelta(seconds=298))
    assert lr._arm_opinion_exit(object(), sess, le, reason="breakout_failed_fast_bail",
                                prior_event="live_bailout", inputs={"bid": 9.33})
    armed = le["opinion_exit_armed"]
    assert armed["reason"] == "lost_vwap_confirmed"           # the first opinion keeps the clock
    assert armed["at_utc"] == t0.isoformat()
    assert armed["reasons"] == ["lost_vwap_confirmed", "breakout_failed_fast_bail"]
    assert armed["inputs"]["breakout_failed_fast_bail"] == {"bid": 9.33}
    assert [e for e, _ in emitted] == ["live_opinion_exit_armed"] * 2
    assert emitted[1][1]["reasons"] == ["lost_vwap_confirmed", "breakout_failed_fast_bail"]
    assert emitted[1][1]["first_reason"] == "lost_vwap_confirmed"


def test_the_receipt_reports_seconds_armed_from_the_sim_aware_clock(monkeypatch):
    t0 = datetime(2026, 9, 10, 13, 41, 5, 800540)
    le = {"opinion_exit_armed": {"reason": "breakout_failed_fast_bail", "at_utc": t0.isoformat(),
                                 "reasons": ["breakout_failed_fast_bail"]}}
    monkeypatch.setattr(lr, "_utcnow", lambda: t0 + timedelta(seconds=899.5))
    r = lr._opinion_exit_armed_receipt(le)
    assert r["reason"] == "breakout_failed_fast_bail"
    assert r["seconds_armed"] == pytest.approx(899.5)
    assert r["armed_at_utc"] == t0.isoformat()
    # explicit `now` wins over the clock (the replay harness passes its own instant)
    assert lr._opinion_exit_armed_receipt(le, now=t0 + timedelta(seconds=10))["seconds_armed"] == 10.0


def test_the_receipt_fails_open_on_no_marker_or_a_broken_stamp():
    assert lr._opinion_exit_armed_receipt({}) is None
    assert lr._opinion_exit_armed_receipt({"opinion_exit_armed": "junk"}) is None
    r = lr._opinion_exit_armed_receipt({"opinion_exit_armed": {"reason": "x", "at_utc": "not-a-time"}})
    assert r["reason"] == "x" and r["seconds_armed"] is None


def test_the_helper_never_reads_the_wall_clock_directly():
    """Replay parity: the decision path goes through ``_utcnow`` (the sim-aware chokepoint),
    never ``datetime.now()`` / ``datetime.utcnow()``."""
    for fn in (lr._arm_opinion_exit, lr._opinion_exit_armed_receipt):
        src = inspect.getsource(fn)
        assert "datetime.now(" not in src and "datetime.utcnow(" not in src, fn.__name__
        assert "_utcnow()" in src


# ── the measurement, pinned ─────────────────────────────────────────────────────
# (event id, symbol, reason, actual P&L, hold-to-deadman-or-tick-exit with the sizing stop,
#  the same with the stop actually resting at the decision). USD, 2 dp.
ARMED_SITE_LEGS = (
    (1565244, "WYHG", "breakout_failed_fast_bail", -48.88, -54.73, -54.73),
    (1565724, "WYHG", "breakout_failed_fast_bail", -48.00, -28.12, -28.12),
    (1565852, "WYHG", "topping_tail_runner_exit", 19.60, 11.20, 11.20),
    (1565940, "WYHG", "topping_tail_runner_exit", 5.64, -39.40, -39.40),
    (1586274, "MOBX", "breakout_failed_fast_bail", -19.14, -48.69, -48.69),
    (1623778, "SUNE", "breakout_failed_fast_bail", -21.88, -26.47, -27.35),
    (1642773, "BIAF", "breakout_failed_fast_bail", -42.28, 39.26, 39.26),
    (1643768, "BIAF", "breakout_failed_fast_bail", -72.50, -104.40, -79.75),
    (1663535, "FTFT", "breakout_failed_fast_bail", -17.82, -18.81, -21.78),
    (1692039, "SUNE", "breakout_failed_fast_bail", -20.65, -24.78, -24.78),
    (1715902, "PCLA", "breakout_failed_fast_bail", -146.34, -138.10, -39.19),
    (1716197, "AHMA", "lost_vwap_confirmed", -33.98, -40.88, -40.88),
    (1723678, "PCLA", "lost_vwap_confirmed", -29.59, 14.80, 14.80),
    (1724029, "PCLA", "breakout_failed_fast_bail", -44.33, 24.78, 24.78),
    (1545696, "BIAF", "close_below_structure", -1.63, -21.84, -21.84),
)
VIABILITY_FLOOR_LEGS = (
    (1569247, "ISPC", -16.40, 7.95, 7.95),
    (1620382, "LABT", 53.88, -12.44, -12.44),
    (1643503, "BIAF", 1.02, -31.45, -31.45),
    (1647272, "TNON", -3.81, -26.56, -30.48),
    (1712081, "TNON", -25.73, -28.08, -28.08),
    (1712832, "TNON", -4.23, -56.16, -46.80),
    (1713340, "TNON", -55.61, -68.16, -45.44),
)


def test_the_measured_aggregate_justifies_arming_the_four_sites():
    assert len(ARMED_SITE_LEGS) == 15
    # the fifth arming site ([44], the bar elif) was measured separately (7 break exits
    # inside F's 35 legs); this table is the four bailout-shaped opinion sites as measured.
    assert {r for _, _, r, *_ in ARMED_SITE_LEGS} == ARMED_REASONS - {"momentum_break_bars"}
    actual = sum(x[3] for x in ARMED_SITE_LEGS)
    sizing = sum(x[4] for x in ARMED_SITE_LEGS)
    resting = sum(x[5] for x in ARMED_SITE_LEGS)
    assert actual == pytest.approx(-521.78, abs=0.05)
    assert sizing == pytest.approx(-456.19, abs=0.05)
    assert resting == pytest.approx(-336.47, abs=0.05)
    assert sizing > actual and resting > actual
    # the shape: a few dollars given back on most legs, the continuations taken on a few
    better = [x for x in ARMED_SITE_LEGS if x[4] > x[3]]
    assert len(better) == 5, [x[:2] for x in better]
    assert {x[1] for x in better} >= {"BIAF", "PCLA", "WYHG"}


def test_the_measured_aggregate_says_keep_the_viability_floor():
    assert len(VIABILITY_FLOOR_LEGS) == 7
    actual = sum(x[2] for x in VIABILITY_FLOOR_LEGS)
    sizing = sum(x[3] for x in VIABILITY_FLOOR_LEGS)
    resting = sum(x[4] for x in VIABILITY_FLOOR_LEGS)
    assert actual == pytest.approx(-50.88, abs=0.05)
    assert sizing == pytest.approx(-214.89, abs=0.05)
    assert resting == pytest.approx(-186.73, abs=0.05)
    assert sizing < actual and resting < actual
    assert sum(1 for x in VIABILITY_FLOOR_LEGS if x[3] < x[2]) == 6
