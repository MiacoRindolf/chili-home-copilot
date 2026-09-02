"""Day-change BASIS INTEGRITY: prev close MUNA, saka ang open (#1284).

ANG BUG (nasukat 2026-09-01/02). Ang WS ignition tracker ay bumubuo ng day
baseline bilang `day.o or prev.c` mula sa Massive snapshot. Premarket ay ZERO
ang `day` aggregate (nbbo_tape.py:298-302) kaya prev close ang base — pero sa
13:30Z ay lumilipat ito sa OPENING PRINT, at ang move% na itina-stamp bilang
`todays_change_perc` (binabasa ng viability A-setup floor at ng Ross-universe
check bilang change-vs-PREV-CLOSE) ay bumabagsak sa ~0:

    BIAF 09-01: prev close 4.56, open 7.63-7.72, tape 7.73
      dating basis (open):        (7.73-7.72)/7.72 = +0.13%  -> "Below A-setup
                                   quality floor (change 0.1295 < 10%)" x15
      tamang basis (prev close):  (7.73-4.56)/4.56 = +69.5%

    Hinarang 13:31-13:37Z habang gumagawa ng bagong HOD 8.585 @13:38:04 (+12.3%
    mula sa block price). 09-01: BIAF 15 row, FLYE 9, GYGY 21 — 45/45 ay >=10%
    sa tamang basis. Ang screen at ang tape ay gumagamit ng vendor
    `todaysChangePerc` (prev-close-based); ang `day.o` ay fallback lang doon.
    Kaya ang tracker LANG ang open-anchored — iyon ang inaayos.

Hard Rule 3 (data-first): ang datum ang inaayos, hindi ang gate. Walang
threshold na nagbago; ang A-setup floor ay nagbabasa na ng tamang numero.

Runnable: pytest tests/test_day_change_basis_prev_close_first.py -v
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import ignition_loop as il
from app.services.trading.momentum_neural import nbbo_tape as nt
from app.services.trading.momentum_neural import universe as un

PREV_CLOSE = 4.56
OPEN_PRINT = 7.72
TAPE = 7.73


def _snap(sym="BIAF", price=TAPE, day_o=OPEN_PRINT, prev_c=PREV_CLOSE, vendor_chg=None,
          vol=50_000_000, bid=7.72, ask=7.74):
    return {
        "ticker": sym,
        "day": {"o": day_o, "c": price, "v": vol, "vw": price},
        "min": {"c": price, "av": vol},
        "prevDay": {"c": prev_c, "v": 5_000_000},
        "lastTrade": {"p": price},
        "lastQuote": {"p": bid, "P": ask},
        "todaysChangePerc": vendor_chg,
    }


def _refresh(tracker, snapshot, want):
    """Injected snapshot + injected universe (walang network), gaya ng
    tests/test_velocity_intake.py."""
    import app.services.massive_client as mc

    orig_build, orig_snap = il.build_equity_universe, mc.get_full_market_snapshot
    try:
        il.build_equity_universe = lambda profile, snapshot=None: list(want)
        mc.get_full_market_snapshot = lambda **kw: snapshot
        return tracker.refresh()
    finally:
        il.build_equity_universe = orig_build
        mc.get_full_market_snapshot = orig_snap


# ── 1. ang tracker: prev close ang baseline sa buong araw ────────────────────

def test_tracker_baseline_is_prev_close_after_the_open():
    """ANG PANGUNAHIN: kapag may `day.o` na (pagkatapos ng 13:30Z), prev close pa rin."""
    tr = il._UniverseTracker()
    _refresh(tr, [_snap()], {"BIAF"})
    assert tr.baseline_for("BIAF") == PREV_CLOSE, "dating 7.72 (open) — mali"


def test_tracker_baseline_premarket_is_unchanged():
    """Premarket (zero ang `day`): byte-identical — prev close pa rin."""
    tr = il._UniverseTracker()
    _refresh(tr, [_snap(day_o=None)], {"BIAF"})
    assert tr.baseline_for("BIAF") == PREV_CLOSE


def test_day_one_listing_without_prev_close_falls_back_to_the_open():
    """Ang `day.o` ay natitira bilang fallback para sa listing na walang prev close."""
    tr = il._UniverseTracker()
    _refresh(tr, [_snap(sym="NEWCO", prev_c=None)], {"NEWCO"})
    assert tr.baseline_for("NEWCO") == OPEN_PRINT


def test_move_pct_reads_the_biaf_gap_as_plus_69_not_plus_0_13():
    tr = il._UniverseTracker()
    _refresh(tr, [_snap()], {"BIAF"})
    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    loop._tracker = tr
    q = SimpleNamespace(bid=7.72, ask=7.74, mid=TAPE, last=None, price=None)
    move = loop._move_pct("BIAF", q)
    assert move == pytest.approx((TAPE - PREV_CLOSE) / PREV_CLOSE * 100.0, abs=0.01)
    assert move > 10.0, "papasa na sa A-setup 10% floor"


def test_the_open_is_kept_separately_for_the_since_open_move():
    """Review finding: ang RVOL-axis guard (_rvol_alone_may_fire) ay nangangailangan
    ng SINCE-OPEN na direksyon — ang gapper na bumabagsak mula sa open ay hindi
    dapat mag-ignite nang long kahit +69% pa vs prev close."""
    tr = il._UniverseTracker()
    _refresh(tr, [_snap()], {"BIAF"})
    assert tr.open_baseline_for("BIAF") == OPEN_PRINT
    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    loop._tracker = tr
    q = SimpleNamespace(bid=7.72, ask=7.74, mid=TAPE, last=None, price=None)
    assert loop._open_move_pct("BIAF", q) == pytest.approx(0.13, abs=0.01)
    assert loop._move_pct("BIAF", q) == pytest.approx(69.52, abs=0.05)
    # bumabagsak mula sa open: negatibo ang since-open, positibo pa ang day change
    dump = SimpleNamespace(bid=7.20, ask=7.22, mid=7.21, last=None, price=None)
    assert loop._open_move_pct("BIAF", dump) < 0 < loop._move_pct("BIAF", dump)


def test_premarket_has_no_open_so_the_since_open_move_falls_back_to_prev_close():
    tr = il._UniverseTracker()
    _refresh(tr, [_snap(day_o=None)], {"BIAF"})
    assert tr.open_baseline_for("BIAF") is None
    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    loop._tracker = tr
    q = SimpleNamespace(bid=7.72, ask=7.74, mid=TAPE, last=None, price=None)
    assert loop._open_move_pct("BIAF", q) == pytest.approx(69.52, abs=0.05)


def test_the_ross_predicate_gets_gap_vs_prev_close_and_move_since_open():
    """Source guard: dalawang magkaibang numero na sa tawag, hindi iisa."""
    src = inspect.getsource(il.IgnitionScoringLoop)
    assert "move_pct=self._open_move_pct(sym, quote)" in src
    assert "gap_pct=move_pct" in src


# ── 2. ang screen fallback at ang tape fallback: pareho ang kahulugan ────────

def test_universe_premarket_fallback_is_prev_close_first():
    chg = un._premarket_change_pct(_snap())
    assert chg == pytest.approx(69.52, abs=0.05)


def test_universe_fallback_uses_the_open_only_without_prev_close():
    chg = un._premarket_change_pct(_snap(sym="NEWCO", prev_c=None))
    assert chg == pytest.approx((TAPE - OPEN_PRINT) / OPEN_PRINT * 100.0, abs=0.01)


def test_tape_fallback_keeps_a_gapper_the_open_basis_would_drop():
    """`_ross_row` na walang vendor change: sa open basis ay +0.13% (itatapon ng
    min-change floor); sa prev-close basis ay +69.5% (nananatili)."""
    row = nt._ross_row(_snap(vendor_chg=None))
    assert row is not None and row["symbol"] == "BIAF"
    # counterfactual: walang prev close AT open lang ang base -> +0.13% -> itinatapon
    assert nt._ross_row(_snap(vendor_chg=None, prev_c=None)) is None
    # ang vendor field, kapag nandoon, ay nananatiling nangunguna
    assert nt._ross_row(_snap(vendor_chg=69.5)) is not None


# ── 3. source guard: prev.c MUNA sa LAHAT ng tatlong site ────────────────────

def _base_assignments(module):
    """(function name, first operand ng `base = A or B`) para sa bawat site."""
    tree = ast.parse(inspect.getsource(module))
    out = []

    def _leaf(call):
        # _f(prev.get("c")) -> ("prev", "c")
        if (
            isinstance(call, ast.Call) and call.args
            and isinstance(call.args[0], ast.Call)
            and isinstance(call.args[0].func, ast.Attribute)
            and isinstance(call.args[0].func.value, ast.Name)
            and call.args[0].args and isinstance(call.args[0].args[0], ast.Constant)
        ):
            return (call.args[0].func.value.id, call.args[0].args[0].value)
        return None

    def _walk(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
                and child.targets[0].id == "base"
                and isinstance(child.value, ast.BoolOp)
                and isinstance(child.value.op, ast.Or)
            ):
                out.append((fn, [_leaf(v) for v in child.value.values]))
            _walk(child, fn)

    _walk(tree, "<module>")
    return out


@pytest.mark.parametrize("module,fn", [
    (il, "refresh"),
    (un, "_premarket_change_pct"),
    (nt, "_ross_row"),
])
def test_every_day_change_base_puts_prev_close_first(module, fn):
    sites = [s for s in _base_assignments(module) if s[0] == fn]
    assert len(sites) == 1, f"inaasahan ang isang `base = ... or ...` sa {module.__name__}.{fn}"
    operands = sites[0][1]
    assert operands[0] == ("prev", "c"), f"prev close ay dapat MAUNA: {operands}"
    assert operands[1] == ("day", "o"), f"open ay fallback lamang: {operands}"
