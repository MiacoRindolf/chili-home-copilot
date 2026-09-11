"""[23] WALANG ORASAN SA LOOB NG BAR — ang G4 tape read ay ``count_v1``.

Pinanatili ng [29] ang re-entry ramp sa ``feature_contract="legacy_time_split"`` nang
sadya: ang 255 print ay pinipili sa BILANG, pero ang ``signed_tape_accel`` ay hinahati sa
GITNA NG ORAS at ang discontinuity trim ay ``window_s/2`` = 7.5 s — isang orasan sa loob
ng bar — habang ang ``buy_share_delta`` ay count-split na. Ang dalawang kalahati ng iisang
bar ay hinahati sa dalawang magkaibang axis.

SINUKAT (read-only, bounded, 2026-09-11):
  * hindi magkasundo kung tape+ sa 465/2,175 = 21.4% ng G4 instant (1 araw, 9 cluster);
    walang tape ang legacy sa 67/2,244 (3.0% — ang mabagal na pangalan, pinutol ng 7.5-s
    trim) laban sa 2/2,244 sa count_v1;
  * walang edge ang alinman sa 8 araw (first touch +/-2% sa 15 min, <= 4 kada
    symbol-15min): TT 40/84 = 0.476, FF 60/133 = 0.451, count-lang-tape+ 32/58 = 0.552,
    legacy-lang-tape+ 10/16 = 0.625; admitted/refused count_v1 0.507/0.470, legacy
    0.500/0.482 ⇒ ang DOKTRINA ang nagpapasya (print-indexed, walang segundo);
  * ang [46] chase gate (kumakain ng PAREHONG tape) ay muling sinukat sa 177-hilerang
    populasyon nito sa dalawang kontrata — ang legacy ay nag-reproduce ng bawat [46] numero
    (47/177, 31/177, 32 & 24, 0.8667/0.8824/1.0000, unang-admit na ext −0.62..4.32);
  * ang [7] door 1 (0.81, walang reference) ay BUMABASA ng tanda ng tape (pasa sa tape+
    lamang) — kaya muling sinukat: class-A tape+ sa count_v1 26/59 = 0.441 ⇒ 0.441/0.548 =
    0.804 (legacy 24/51 = 0.471 ⇒ 0.859); ang 0.81 ay nananatili.

[23] REVIEW FIX (2026-09-11) — ANG MGA TEST AY NAGPAPATAKBO NG GAWI, HINDI NG SARILING
KONSTANTE. Ang unang anyo ay nag-assert ng aritmetika sa mga dict na tinukoy DITO
(REDERIVED_46 / DOOR1_REMEASURE) at ng nilalaman ng derivation string, at pinakain ang
purong chase decision ng hard-coded na accel/bsd (hindi nakasalalay sa kontrata) — kaya
kung binago ng count_v1 ang [46] o ang [7], papasa pa rin sila. Ngayon: ang PAREHONG
synthetic na print ay dumadaan sa TUNAY na tape function sa loob ng shipped na helper,
tapos sa shipped na chase gate / door 1, sa ilalim ng dalawang kontrata. Ang mga numero ng
derivation ay nasa PR at design doc (§13.3), hindi rito.

AT ANG EDAD NG PRINT (review fix, major): sa count_v1 ang trim ay p90 x 7.82, kaya ang
mga gap na > 14.69 s ay nakalulusot at ang [59] na ``max(14.69, gap_p99 ng bintana)`` ay
itinataas ng bintana mismo. Ang bound ay ang SELYO ng helper (independiyenteng sahig).

Runnable: pytest tests/test_g4_bar_count_contract.py -v
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural import optional_db_read as ODR
from app.services.trading.momentum_neural import risk_policy as RP

from tests.test_reentry_bar_level0_prior_leg_high import GREEN_PRIOR, _FakeDB, _harness
from tests.test_reentry_chase_is_the_tape import _Tick, _Via

G4_SOURCE = inspect.getsource(LR._g4_reentry_escalation_check)
#: The REAL tape function, captured before any test monkeypatches the module attribute.
_ORIGINAL_TAPE_FN = EG.signed_tape_accel_features


# ── the wiring ───────────────────────────────────────────────────────────────


def _tape_calls() -> list[ast.Call]:
    tree = ast.parse(textwrap.dedent(G4_SOURCE))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_g4e_tape_fn"
    ]


def test_the_g4_read_passes_count_v1():
    """AST PIN: the ONE tape read of the bar names ``count_v1`` as a literal keyword."""
    calls = _tape_calls()
    assert len(calls) == 1, len(calls)
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw["feature_contract"], ast.Constant)
    assert kw["feature_contract"].value == "count_v1"
    assert isinstance(kw["window_prints"], ast.Name) and kw["window_prints"].id == "_g4e_window_prints"
    assert "legacy_time_split" not in ast.unparse(calls[0])


def test_window_prints_is_unchanged():
    assert int(settings.chili_momentum_g4_reentry_tape_window_prints) == 255


# ── the synthetic tape where the two splits disagree ─────────────────────────

_NOW = datetime(2026, 9, 10, 13, 55, 34)
_NOW_EPOCH = (_NOW - datetime(1970, 1, 1)).total_seconds()


def _disagreeing_rows() -> list[tuple]:
    """20 prints over 9 s. Prints 0-9 (t 0.0-0.9 s) are SELLS at the bid, prints 10-15
    (t 1.0-1.5 s) BUYS at the ask, prints 16-19 (t 6, 7, 8, 9 s) SELLS.

    TIME midpoint = 4.5 s ⇒ front = prints 0-15 (buy 600), back = 16-19 (buy 0)
    ⇒ accel −600. COUNT midpoint = print 10 ⇒ front = 0-9 (buy 0), back = 10-19
    (buy 600) ⇒ accel +600. ``buy_share_delta`` is count-split in BOTH (+0.6).
    No gap exceeds either trim (legacy 7.5 s; count p90 1.0 s x 7.82)."""
    t0 = _NOW_EPOCH - 10.0
    bid, ask = 0.99, 1.01
    rows = []
    for i in range(10):
        rows.append((bid, 100, bid, ask, t0 + 0.1 * i))
    for i in range(10, 16):
        rows.append((ask, 100, bid, ask, t0 + 0.1 * i))
    for t in (6.0, 7.0, 8.0, 9.0):
        rows.append((bid, 100, bid, ask, t0 + t))
    return rows


def _fake_fetch(monkeypatch):
    monkeypatch.setattr(ODR, "optional_fetchall",
                        lambda db, stmt, params=None, **kw: db.execute(stmt, params).fetchall())


def _read(rows, contract, monkeypatch):
    _fake_fetch(monkeypatch)
    return _ORIGINAL_TAPE_FN("SYNC", db=_FakeDB(rows), window_prints=255,
                             as_of=_NOW, feature_contract=contract)


def test_the_two_contracts_disagree_on_the_same_prints(monkeypatch):
    rows = _disagreeing_rows()
    legacy = _read(rows, "legacy_time_split", monkeypatch)
    count = _read(rows, "count_v1", monkeypatch)
    assert legacy is not None and count is not None
    assert legacy["split"] == "time" and count["split"] == "count"
    assert legacy["signed_tape_accel"] == pytest.approx(-600.0)
    assert count["signed_tape_accel"] == pytest.approx(600.0)
    # the share delta was already count-split under both
    assert legacy["buy_share_delta"] == pytest.approx(0.6)
    assert count["buy_share_delta"] == pytest.approx(0.6)
    assert legacy["last_print"] == count["last_print"] == pytest.approx(0.99)


def test_the_bar_resolves_by_count(monkeypatch):
    """The SHIPPED helper, reading the SAME prints through the REAL tape function: the
    count split says buyers are lifting, and the bar passes; the time split on the same
    prints would have refused (``tape_not_confirming``)."""
    rows = _disagreeing_rows()
    le, sess, via, emitted = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=None,
                                      high_print=0.98)
    _fake_fetch(monkeypatch)
    calls: list[dict] = []

    def _spy(symbol, **kw):
        calls.append(dict(kw))
        kw = dict(kw)
        kw["db"] = _FakeDB(rows)
        return _ORIGINAL_TAPE_FN(symbol, **kw)

    monkeypatch.setattr(EG, "signed_tape_accel_features", _spy)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.01)
    assert len(calls) == 1
    assert calls[0]["feature_contract"] == "count_v1"
    assert calls[0]["window_prints"] == 255
    assert (ok, lvl) == (True, 0), dbg
    assert dbg["reason"] == "reclaim_met_level0"
    assert dbg["tape_feature_contract"] == "count_v1"
    assert dbg["binding"]["tape_split"] == "count"
    assert dbg["signed_tape_accel"] == pytest.approx(600.0)
    # the legacy read of the same prints, through the same pure decision, refuses
    legacy = _ORIGINAL_TAPE_FN("SYNC", db=_FakeDB(rows), window_prints=255, as_of=_NOW,
                               feature_contract="legacy_time_split")
    ok_l, dbg_l = RP.reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=False,
        live_price=legacy["last_print"], prior_hwm=GREEN_PRIOR["high_water_mark"],
        prior_exit_price=GREEN_PRIOR["exit_price"], prior_risk_dist=GREEN_PRIOR["risk_dist"],
        tape_accel=legacy["signed_tape_accel"], prior_high_print=0.98,
        tape_buy_share_delta=legacy["buy_share_delta"], tape_stale=False,
    )
    assert ok_l is False and dbg_l["reason"] == "tape_not_confirming"


def test_the_receipt_names_the_contract_even_on_a_refusal(monkeypatch):
    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR,
                                tape={"signed_tape_accel": -5.0, "buy_share_delta": -0.1,
                                      "back_buy_share": 0.4, "n_ticks": 255,
                                      "last_print": 3.81, "last_bid": 3.80, "last_ask": 3.82,
                                      "feature_contract": "count_v1", "split": "count"},
                                high_print=3.80)
    ok, dbg, _ = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.82)
    assert ok is False and dbg["reason"] == "tape_not_confirming"
    assert dbg["tape_feature_contract"] == "count_v1"
    assert dbg["binding"]["tape_split"] == "count"


# ── [46] the chase gate eats the SAME tape: behaviour through the shipped seams ─


RED_PRIOR = dict(GREEN_PRIOR, was_loss=True, exit_reason="tape_accel_rollover")


def _g4_on_rows(monkeypatch, rows, *, prior, high_print, level=0):
    """The SHIPPED G4 helper reading ``rows`` through the REAL tape function (count_v1)."""
    le, sess, via, emitted = _harness(monkeypatch, level=level, prior=prior, tape=None,
                                      high_print=high_print)
    _fake_fetch(monkeypatch)

    def _spy(symbol, **kw):
        kw = dict(kw)
        kw["db"] = _FakeDB(rows)
        return _ORIGINAL_TAPE_FN(symbol, **kw)

    monkeypatch.setattr(EG, "signed_tape_accel_features", _spy)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.01)
    return ok, dbg, lvl, le, sess, emitted


def test_the_chase_gate_decides_on_the_count_v1_tape_through_the_shipped_seams(monkeypatch):
    """The red prior leg's high print is 0.95; the band is 0.95 x 1.0225 = 0.9714; the
    newest print 0.99 is ABOVE it, so the TAPE decides. Through the shipped helper the
    count split says buyers are lifting => the chase gate ADMITS and names count_v1. The
    SAME prints read under ``legacy_time_split`` (time midpoint) say the opposite => the
    same gate WAITS. The contract switch moves the [46] decision, measured end to end."""
    rows = _disagreeing_rows()
    ok, dbg, lvl, le, sess, _ = _g4_on_rows(monkeypatch, rows, prior=RED_PRIOR, high_print=0.95)
    assert ok is True and dbg["tape_feature_contract"] == "count_v1", dbg
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(LR, "_emit", lambda db, s, ev, payload: seen.append((ev, payload)))
    admit, cdbg = LR._reentry_chase_gate(
        None, sess, le, _Via(), tick=_Tick(ask=1.01), g4e_dbg=dbg,
        trigger_reason="momentum_ok_rel_vol")
    assert admit is True and cdbg["reason"] == "reentry_chase_tape_admit", cdbg
    assert cdbg["tape_feature_contract"] == "count_v1"
    # the legacy read of the same prints, through the same gate
    legacy = _ORIGINAL_TAPE_FN("SYNC", db=_FakeDB(rows), window_prints=255, as_of=_NOW,
                               feature_contract="legacy_time_split")
    legacy_dbg = dict(dbg, tape_accel=legacy["signed_tape_accel"],
                      buy_share_delta=legacy["buy_share_delta"],
                      tape_feature_contract="legacy_time_split")
    admit_l, cdbg_l = LR._reentry_chase_gate(
        None, sess, le, _Via(), tick=_Tick(ask=1.01), g4e_dbg=legacy_dbg,
        trigger_reason="momentum_ok_rel_vol")
    assert admit_l is False and cdbg_l["reason"] == "reentry_chase_tape_wait", cdbg_l


def test_no_size_band_machinery_is_revived():
    """R3 withdrew the q50/q90 size band; [23] re-measured under count_v1 and did not
    revive it (the numbers are in the PR and 15.5, not asserted here)."""
    for dead in ("REENTRY_CHASE_EXT_Q50", "REENTRY_CHASE_EXT_Q90", "REENTRY_CHASE_SIZE_FLOOR"):
        assert not hasattr(RP, dead), dead


# ── [7] door 1 reads the SIGN of the tape: the switch moves which instants pass ───


def _rows_at(now: datetime) -> list[tuple]:
    """``_disagreeing_rows`` re-anchored so the newest print is 1 s old at ``now``."""
    shift = (now - _NOW).total_seconds()
    return [(px, sz, b, a, ts + shift) for (px, sz, b, a, ts) in _disagreeing_rows()]


def test_door1_reads_the_count_v1_sign_through_the_shipped_helper(monkeypatch):
    """Level 1, NO reference (the class-A population: a cross-day seed with no leg
    today), a non-structural trigger. Door 1 opens on tape+ alone at x0.81. The SAME
    prints: count_v1 reads tape+ => the door opens at the shipped multiplier; the
    legacy time split reads tape- => it stays shut. So the contract switch changes which
    no-reference instants pass, which is why 0.81 was re-measured under count_v1."""
    from tests.test_g4_substitute_fails_open_on_missing_data import _lr_harness

    le, sess, via, emitted, _ = _lr_harness(monkeypatch, tape=None)
    now = LR._utcnow()
    rows = _rows_at(now)
    _fake_fetch(monkeypatch)

    def _spy(symbol, **kw):
        kw = dict(kw)
        kw["db"] = _FakeDB(rows)
        return _ORIGINAL_TAPE_FN(symbol, **kw)

    monkeypatch.setattr(EG, "signed_tape_accel_features", _spy)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.01)
    assert (ok, lvl) == (True, 1), dbg
    assert dbg["reason"] == "non_structural_substitute_no_reference"
    assert dbg["tape_feature_contract"] == "count_v1"
    assert le["g4_reentry_size_mult"] == pytest.approx(
        float(settings.chili_momentum_g4_substitute_no_reference_size_mult))
    # the same prints under the legacy time split: tape-, the door stays shut
    legacy = _ORIGINAL_TAPE_FN("TNON", db=_FakeDB(rows), window_prints=255, as_of=now,
                               feature_contract="legacy_time_split")
    assert legacy["signed_tape_accel"] < 0 < legacy["buy_share_delta"]
    ok_l, dbg_l = RP.reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=False,
        live_price=legacy["last_print"], prior_hwm=None, prior_exit_price=None,
        prior_risk_dist=None, tape_accel=legacy["signed_tape_accel"],
        tape_buy_share_delta=legacy["buy_share_delta"], tape_stale=False,
        substitute_no_reference_size_mult=0.81, substitute_unreadable_tape_size_mult=0.48,
        substitute_size_floor=0.25,
    )
    assert ok_l is False, dbg_l


# ── [23] review fix (major): the window cannot raise its own age bound ───────


def _slow_rows(now_epoch: float, *, age: float = 16.0) -> list[tuple]:
    """A slow name: 60 prints 2.5 s apart with three 16/17/18-s gaps inside the window.
    Under count_v1 the trim is p90 2.5 x 7.82 = 19.55 s, so all three gaps SURVIVE and
    the window's own gap p99 is 18.0 s; the newest print is ``age`` s old. Front half
    sells at the bid, back half buys at the ask => tape+ under the count split."""
    gaps = [2.5] * 59
    gaps[5], gaps[12], gaps[20] = 16.0, 17.0, 18.0
    ts = [now_epoch - age]
    for g in reversed(gaps):
        ts.append(ts[-1] - g)
    ts.reverse()
    bid, ask = 0.99, 1.01
    return [((bid if i < 30 else ask), 100, bid, ask, t) for i, t in enumerate(ts)]


def test_a_slow_window_cannot_raise_its_own_age_bound(monkeypatch):
    """DPU/WYHG/DLTH-shaped: the deciding print is 16 s old, the window's own gap p99 is
    18 s. The [59] form max(14.69, 18.0) = 18.0 called it FRESH and the bar would pass
    on a lifting tape; the helper's independent stamp (14.69) says STALE. The bar WAITs
    and the receipt names the bound and its source."""
    rows = _slow_rows(_NOW_EPOCH, age=16.0)
    ok, dbg, lvl, le, sess, emitted = _g4_on_rows(
        monkeypatch, rows, prior=GREEN_PRIOR, high_print=0.98)
    raw = _ORIGINAL_TAPE_FN("SYNC", db=_FakeDB(rows), window_prints=255, as_of=_NOW,
                            feature_contract="count_v1")
    floor = float(settings.chili_momentum_g4_reentry_max_print_age_seconds)
    # the scenario: the window's own p99 would have covered the age
    assert raw["gap_p99_s"] == pytest.approx(18.0)
    assert floor < raw["print_age_s"] <= max(floor, raw["gap_p99_s"])
    assert raw["signed_tape_accel"] > 0 and raw["buy_share_delta"] > 0
    # the shipped bar: stale on the independent bound
    assert (ok, lvl) == (False, 0), dbg
    assert dbg["reason"] == "reentry_tape_source_stale"
    assert dbg["tape_age_s"] == pytest.approx(16.0, abs=0.01)
    assert dbg["tape_age_bound_s"] == pytest.approx(floor)
    assert dbg["binding"]["price_age_bound_s"] == pytest.approx(floor)
    assert dbg["binding"]["price_age_basis"] == "helper_stamp"
    # ...and the [46] chase gate inherits the SAME verdict (tape_source_stale)
    red_le = dict(le, g4_prior_trade=RED_PRIOR)
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(LR, "_emit", lambda db, s, ev, payload: seen.append((ev, payload)))
    admit, cdbg = LR._reentry_chase_gate(
        None, sess, red_le, _Via(), tick=_Tick(ask=1.01),
        g4e_dbg=dict(dbg, prior_high_print=0.95), trigger_reason="momentum_ok_rel_vol")
    assert admit is False and cdbg["reason"] == "reentry_chase_tape_unreadable_wait", cdbg


def test_a_fresh_slow_window_still_passes(monkeypatch):
    """The fix is a bound, not a new refusal: the same slow tape with its newest print
    3 s old passes the level-0 bar on the lifting count split."""
    rows = _slow_rows(_NOW_EPOCH, age=3.0)
    ok, dbg, lvl, *_ = _g4_on_rows(monkeypatch, rows, prior=GREEN_PRIOR, high_print=0.98)
    assert (ok, lvl) == (True, 0), dbg
    assert dbg["reason"] == "reclaim_met_level0"
    assert dbg["binding"]["price_age_basis"] == "helper_stamp"


def test_the_receipt_carries_the_trim_that_decided(monkeypatch):
    """Under count_v1 the basis string no longer fixes the value: the binding (on every
    row, blocked or passed) carries the trim that ran, its varying input (the window's
    own gap p90) and the span actually measured; the CONSTANT multiplier rides the
    deduped pass receipt, not the 1,141-2,061 row/day blocked event."""
    rows = _slow_rows(_NOW_EPOCH, age=3.0)
    ok, dbg, lvl, le, sess, emitted = _g4_on_rows(
        monkeypatch, rows, prior=GREEN_PRIOR, high_print=0.98)
    assert ok is True
    mult = float(settings.chili_momentum_tape_gap_discontinuity_p90_mult)
    b = dbg["binding"]
    assert b["gap_trim_basis"] == "window_gap_p90 x measured_p99_over_p90"
    assert b["gap_trim_window_p90_s"] == pytest.approx(2.5)
    assert b["gap_trim_s"] == pytest.approx(2.5 * mult, abs=1e-3)
    assert b["span_s"] == pytest.approx(191.0)
    assert b["gap_restricted"] is False
    assert "gap_trim_mult" not in b
    passed = [p for et, p in emitted if et in ("g4_reentry_reclaim_proven", "g4_reentry_pass_unproven")]
    assert len(passed) == 1
    assert passed[0]["gap_trim_mult"] == pytest.approx(mult)
    assert passed[0]["binding"]["gap_trim_s"] == pytest.approx(2.5 * mult, abs=1e-3)


def test_the_local_fallback_bound_is_the_floor_alone(monkeypatch):
    """No helper stamp (a pure/legacy read or a fixture): the local age is judged
    against the floor, never against the window's gap p99."""
    tape = {"signed_tape_accel": 5000.0, "back_buy_share": 0.6, "buy_share_delta": 0.12,
            "prints_since_high": 0, "n_ticks": 60, "last_print": 1.0, "last_bid": 0.99,
            "last_ask": 1.01, "last_ts": _NOW_EPOCH - 60.0, "gap_p99_s": 90.0}
    le, sess, via, _ = _harness(monkeypatch, level=0, prior=GREEN_PRIOR, tape=tape,
                                high_print=0.98)
    ok, dbg, _ = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.01)
    assert ok is False and dbg["reason"] == "reentry_tape_source_stale"
    assert dbg["tape_age_bound_s"] == pytest.approx(
        float(settings.chili_momentum_g4_reentry_max_print_age_seconds))
    assert dbg["binding"]["price_age_basis"] == "local_fallback_floor"
