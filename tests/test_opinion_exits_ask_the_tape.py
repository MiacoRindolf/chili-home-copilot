"""OPINION EXITS MUST ASK THE TAPE -- the tick exit is primary (2026-09-10, [21]).

FOUND on the running tree: 13 call sites of ``_transition_to_bailout`` in live_runner.py and
NONE reads a print. Four of the ON sites are OPINIONS -- a reading of a QUOTE, a BAR, a
WALL-CLOCK window or a candle shape -- and once any of them set STATE_LIVE_BAILOUT the tick
exit (``momentum_break_stop``, evaluated only in ENTERED/TRAILING) was never consulted again:

    breakout_failed_fast_bail   bid < level inside a clock window      (quote + wall clock)
    lost_vwap_confirmed         1m bar close + bid below VWAP margin   (bar + quote)
    close_below_structure       closed 1m bar below the swing low      (bar)
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

[44]/[21]/[47] (2026-09-10, later the same day, + Amendments 1-3): the tick exit is the
PRINT-INDEXED verdict G (`exit_verdict.py`, docs/DESIGN/EXIT_VERDICT_F.md), which judges
EVERY equity leg from the entry fill -- not `momentum_break_stop`, a 10-s quote-mid bar,
itself an opinion. What the sites "arm" is therefore a RECEIPT (the opinion also wanted
out); the break elif became the FOURTH such site (`momentum_break_bars`);
`momentum_break_stop` survives only as the named fallback for crypto (-USD) and an
unreadable entry-fill anchor. The tables below stand as measured.

2026-09-10 [57]: the close_below_structure site is GONE, not armed.

The deleted predicate read the LAST CLOSED 1m bar (the frame is
``chili_momentum_pullback_entry_interval``, default ``1m``, unpinned on this host) against
the last CONFIRMED swing low (``lookback=10`` bars each side, so ~10 minutes old by
construction), with a 30 bps buffer. It fired ONCE in 28 days (BIAF 09-04, -1.63 actual vs
-21.84 held: the deletion is -$20.21 on the only leg the site ever decided) and 0 times in
14 days of paper.

⚠️ The tail numbers below measure a DIFFERENT rule and are not attributed to the deleted
one. The evidence (memory project_shelf_break_is_a_stop_not_a_profit_taker_0909, full July
tape) is a PRINT-indexed pivot-low ratchet: 13 legs with peak >= 1 R, actual +47.03 R ->
-1.57 R, 11/13 cut at every k in 3..50 PRINTS; VRAX 07-09 +25.58 R -> -0.29 R; body: 6 of
the 10 best legs cut (+6.66 R -> +3.96 R); 86% of shelf breaks trap/noise (median depth
2.90%). It differs from the deleted site on four axes -- k counts prints, not bars; it
RATCHETS while ``_compute_confirmed_swing_low_last`` can step DOWN; it has no buffer; it
exits on the first PRINT below the shelf, not on a bar close -- and its harm SHRINKS as k
grows (+1.04 R at k=50), so it does not extrapolate onto a slower, buffered, bar-close rule.

What buys the deletion is therefore not the R gap: (a) a bar close is not a print, and on a
HELD tick the tick exit already owns the verdict; (b) the site is inert (1 fire / 28 d live,
0 / 14 d paper); (c) the faster analog of the same level destroys the tail, so the level has
no forward path on the REWARD side -- it keeps its place on the RISK side (the deadman /
pullback-low stop). Note also that "162 ticks held back by the 30-s floor" is NOT 162
near-misses: ``_opinion_exit_suppressed`` runs BEFORE the predicate, so that count is the
population of sub-30 s held ticks -- identical (162) to ``lost_vwap_flatten``.

THREE bailout-shaped sites arm (plus the bar elif's `momentum_break_bars`, measured
separately inside the verdict's 35 legs); the BIAF row moves to RETIRED_SITE_LEGS and
the 14-leg aggregate is re-stated below.

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
    "topping_tail_runner_exit",
    # [44] 2026-09-10: the 10-s quote-mid bar exit is an opinion too -- it arms now.
    "momentum_break_bars",
}
#: 2026-09-10 [57]: the close-below-structure site was DELETED, not armed (a bar shelf is a
#: stop, not a profit-taker -- see the module docstring). Its reason must not arm, must not
#: bail out, and its event must not be emitted anywhere in the live runner.
RETIRED_ARM_REASONS = {"close_below_structure"}
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


def test_the_three_opinion_sites_no_longer_transition_to_bailout():
    """Positive on both sides: the three reasons are gone from every ``live_bailout`` emit
    AND present, exactly once each, as ``_arm_opinion_exit(reason=...)`` calls -- plus the
    fourth, the break elif's ``momentum_break_bars`` ([44]); the retired reason ([57]) is
    in neither list."""
    tree = _module_tree()
    bailout_reasons = _emit_reasons(tree, "live_bailout")
    assert not (bailout_reasons & (ARMED_REASONS | RETIRED_ARM_REASONS)), bailout_reasons
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
    assert not (set(armed) & RETIRED_ARM_REASONS), armed


def test_they_emit_the_arming_receipt_with_the_inputs_the_bailout_used_to_carry():
    src = inspect.getsource(lr._arm_opinion_exit)
    assert '"live_opinion_exit_armed"' in src
    assert "**payload_inputs" in src and '"derivation"' in src
    # each site still hands over its decision inputs (the receipt = the decision INPUTS)
    tick = inspect.getsource(lr.tick_live_session)
    for anchor in ('"breakout_level": le.get("breakout_level_price")',
                   '"session_vwap": _lv_vwap',
                   '"high_water_mark": _float_or_none(pos.get("high_water_mark"))'):
        assert anchor in tick, anchor


def test_the_bailout_helper_has_nine_callers_left_and_none_of_them_is_an_opinion_site():
    tree = _module_tree()
    n = len(_calls_named(tree, "_transition_to_bailout"))
    assert n == 9, f"13 sites before [21], 4 re-routed to arming; found {n}"
    # the USD risk caps are untouched
    bailout_reasons = _emit_reasons(tree, "live_bailout")
    assert {"max_loss_per_trade", "max_loss_circuit"} <= bailout_reasons


def _call_names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _imported_names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
    return out


def test_the_bar_shelf_exit_is_gone_from_the_live_tick():
    """2026-09-10 [57]. The close-below-structure site does not ARM, does not BAIL, does not
    EMIT and does not FETCH: no ``_arm_opinion_exit`` carries its reason; ``tick_live_session``
    neither imports nor calls the old ``bos_exit_triggered_long`` helper (which is gone from
    ``entry_gates`` -- it had no caller left); the ``live_bos_exit`` event, the
    ``close_below_structure`` literal and the two settings that switched the site are absent
    from the module and from ``Settings`` (no dark flag). The swing-low reader itself stays:
    the G4 grind clamp and the micro-pullback ratchet read it on the ENTRY / risk side, where
    a shelf belongs (a better STOP, a worse profit-taker)."""
    from app.config import Settings
    from app.services.trading.momentum_neural import entry_gates

    tree = _module_tree()
    for call in _calls_named(tree, "_arm_opinion_exit"):
        kw = {k.arg: k.value for k in call.keywords}
        assert _const(kw["reason"]) not in RETIRED_ARM_REASONS
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for gone in ("live_bos_exit", "close_below_structure",
                 "chili_momentum_bos_exit_live_enabled", "chili_momentum_bos_exit_buffer_pct"):
        assert gone not in consts, gone
    tick = ast.parse(inspect.getsource(lr.tick_live_session))
    assert "bos_exit_triggered_long" not in _call_names(tick)
    assert "bos_exit_triggered_long" not in _imported_names(tick)
    assert 'trigger="bos_exit"' not in inspect.getsource(lr.tick_live_session)
    for key in ("chili_momentum_bos_exit_live_enabled", "chili_momentum_bos_exit_buffer_pct"):
        assert key not in Settings.model_fields, key
    assert not hasattr(entry_gates, "bos_exit_triggered_long")
    assert callable(getattr(entry_gates, "_compute_confirmed_swing_low_last", None))


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
    """[44]: the tick exit is the print verdict; EVERY verdict receipt (the whole-exit decision
    included) carries the armed marker through `_exit_verdict_receipt_base`; the -USD /
    unreadable-anchor fallback (`live_momentum_break_exit`) still carries it directly."""
    verdict = inspect.getsource(lr._exit_verdict_tick)
    i = verdict.find('_emit(db, sess, "live_exit_verdict_fired", receipt)')
    assert i > 0
    assert "**base," in verdict[i - 1600: i]          # the receipt dict is built right before its emit
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
    machine's whole marker goes with it (phase, entry anchor, leg high, frontier, deadman)."""
    assert "opinion_exit_armed" in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "exit_verdict" in lr._RECYCLE_ENTRY_STATE_KEYS


def test_the_derivation_travels_with_the_change():
    text = lr._OPINION_EXIT_ARM_DERIVATION
    for token in ("2026-09-10", "14", "-520.15", "-434.35", "-314.63", "[57]",
                  "5 legs better, 9 a little worse"):
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
)
#: 2026-09-10 [57]: the one leg the bar-shelf site ended in 7 live days. Held, it lost the
#: SAME -21.84 under both deadman ladders, so retiring the site moves the aggregate by
#: exactly this row -- the three re-stated sums are arithmetic on these rows, not a re-run
#: (the per-exit-mode split is NOT re-derived; see _OPINION_EXIT_ARM_DERIVATION's block).
#: ⚠️ The identical -21.84 under both ladders says the hold left at a LADDER-INDEPENDENT
#: exit -- the tick exit or the 15-min cap, NOT necessarily the deadman (4 of the 14 retained
#: legs differ between the two ladders, which is the signature of a deadman exit). So the
#: retired row's exit MODE is unknown from this data and is never stated anywhere.
#: Kept as history, not erased: added back it reproduces the #1377 table exactly.
RETIRED_SITE_LEGS = (
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


def test_the_measured_aggregate_justifies_arming_the_three_sites():
    assert len(ARMED_SITE_LEGS) == 14
    # the fourth arming site ([44], the bar elif) was measured separately (7 break exits
    # inside the verdict's 35 legs); this table is the three bailout-shaped opinion sites.
    assert {r for _, _, r, *_ in ARMED_SITE_LEGS} == ARMED_REASONS - {"momentum_break_bars"}
    actual = sum(x[3] for x in ARMED_SITE_LEGS)
    sizing = sum(x[4] for x in ARMED_SITE_LEGS)
    resting = sum(x[5] for x in ARMED_SITE_LEGS)
    assert actual == pytest.approx(-520.15, abs=0.05)
    assert sizing == pytest.approx(-434.35, abs=0.05)
    assert resting == pytest.approx(-314.63, abs=0.05)
    assert sizing > actual and resting > actual
    # the shape: a few dollars given back on most legs, the continuations taken on a few
    better = [x for x in ARMED_SITE_LEGS if x[4] > x[3]]
    assert len(better) == 5, [x[:2] for x in better]
    assert {x[1] for x in better} >= {"BIAF", "PCLA", "WYHG"}


def test_the_retired_shelf_leg_is_kept_as_history_and_reproduces_the_first_table():
    """[57]: the BIAF row is retired with its site, not erased. Added back, the 15-leg table
    that justified #1377 (-521.78 -> -456.19 / -336.47) reproduces exactly -- so the earlier
    receipts stay auditable -- and the retired leg was NOT one of the 5 better legs (the hold
    lost it -21.84 vs -1.63 under both ladders: the one bar the shelf read right)."""
    assert len(RETIRED_SITE_LEGS) == 1
    assert {r for _, _, r, *_ in RETIRED_SITE_LEGS} == RETIRED_ARM_REASONS
    both = ARMED_SITE_LEGS + RETIRED_SITE_LEGS
    assert len(both) == 15
    assert sum(x[3] for x in both) == pytest.approx(-521.78, abs=0.05)
    assert sum(x[4] for x in both) == pytest.approx(-456.19, abs=0.05)
    assert sum(x[5] for x in both) == pytest.approx(-336.47, abs=0.05)
    (_, sym, _, actual, sizing, resting) = RETIRED_SITE_LEGS[0]
    assert sym == "BIAF" and sizing < actual and resting < actual


def test_the_direct_measurement_of_the_deleted_rule_points_the_other_way():
    """⚠️ [57] HONESTY PIN. The tail R-numbers in this module's docstring were measured on a
    PRINT-indexed ratchet, not on the bar predicate that was deleted. The ONLY direct
    measurement of the deleted rule is this one leg, and it is the OPPOSITE sign: the site
    was RIGHT, so over 28 days the deletion costs $20.21 on the only decision it ever made.
    Pinned so no later receipt can quote the tail gap as the measured effect of [57]."""
    (_, sym, reason, actual, sizing, resting) = RETIRED_SITE_LEGS[0]
    assert (sym, reason) == ("BIAF", "close_below_structure")
    assert actual == pytest.approx(-1.63, abs=0.005)
    assert sizing == resting == pytest.approx(-21.84, abs=0.005)
    # the cost of deleting the site, on the one leg it ever decided
    assert sizing - actual == pytest.approx(-20.21, abs=0.01)
    # and it is ladder-independent, so its exit MODE is not derivable from these rows
    assert sizing == resting
    ladder_dependent = [x[1] for x in ARMED_SITE_LEGS if x[4] != x[5]]
    assert len(ladder_dependent) == 4, ladder_dependent


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
