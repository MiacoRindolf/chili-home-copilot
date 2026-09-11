"""[56] — ANG BACKSIDE BENCH AY RESIBO + SUKAT NA SIZE, HINDI VETO (2026-09-11).

Tinanong sa TAPE ang tanong ng bench ("hindi na ba gagawa ng bagong high?"): sa bawat sandaling
pumutok ang isang trigger habang benched, alin ang unang nangyari sa mga print — umabot sa
running high ng tape, o bumaba nang PAREHONG layo sa ilalim? Ang derivation ay
``scripts/backside_bench_side_test_56.py``; ang mga numero ay ``BACKSIDE_BENCH_MEASURED``.

REVIEW FIX (2026-09-11) — ang mga test dito ay nagpapatakbo ng bawat ayos:
  * ang binding ay HINDI na flat 1.0 na galing sa pagbubukod ng NONE: ito ay isang talaan kada
    retrace stratum na BINABASA ng sizing (product + ``risk_mults`` + post-paper-floor);
  * ang ``live_entry_backside_bench_conditioned`` ay inilalabas sa FIRE POINT ng bawat isa sa
    tatlong daan (break / tape-hold / continuation) — pagkatapos ng bawat gate — na may
    ``legacy_verdict`` + ``legacy_veto_site`` na tama para sa daang iyon;
  * ang basag na pagbasa ay may NAMED na resibo (``action: "error"``), hindi ``None``;
  * ang derivation ay may DEGEN na label, dalawang sukat, ang mga nahuhulog na cluster, at
    bumabasa ng populasyong inilalabas ng code NGAYON (hindi lamang ng lumang veto event);
  * ang per-trigger ``front_side_state`` ay tumitingin sa live na presyo (FTFT).

Runnable: pytest tests/test_backside_bench_conditions_not_vetoes_56.py -v
"""
from __future__ import annotations

import ast
import math
import re
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

import app.services.trading.momentum_neural.entry_gates as eg
import app.services.trading.momentum_neural.live_runner as lr
from app.services.trading.momentum_neural.entry_gates import (
    BACKSIDE_BENCH_MEASURED,
    backside_bench_size_multiplier,
)
from app.services.trading.momentum_neural.ross_bench_scoring import (
    BENCH_RECEIPT_EVENTS,
    BENCH_VETO_EVENTS,
)
from app.services.trading.momentum_neural.tape_cycles import PullbackCycleScanner

_LR_SRC = Path(lr.__file__).read_text(encoding="utf-8")
_LR_TREE = ast.parse(_LR_SRC)
_EG_SRC = Path(eg.__file__).read_text(encoding="utf-8")
_EG_TREE = ast.parse(_EG_SRC)
_REPO = Path(lr.__file__).resolve().parents[4]
_TABLE = BACKSIDE_BENCH_MEASURED["size_by_retrace"]
_T0 = datetime(2026, 9, 10, 13, 30, 0)


# ── fixtures ─────────────────────────────────────────────────────────────────────────
def _faded_frame() -> pd.DataFrame:
    """Isang pangalang tumakbo sa 10 at kumupas sa 5 — ang hugis na nagla-latch ng bench
    (kapareho ng fixture ng test_replay_causal_provenance)."""
    index = pd.date_range("2026-09-10T14:00:00Z", periods=20, freq="1min")
    closes = [10.0, 9.0, 8.0, 7.0, 6.0] + [5.0] * 15
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes,
         "Volume": [100.0] * len(closes)},
        index=index,
    )


def _ledger(prices, *, caught_up=True) -> dict[str, Any]:
    """Isang TUNAY na `tape_cycle_state` (gaya ng isinusulat ng `_feed_tape_cycle_state`)."""
    sc = PullbackCycleScanner(0.5)
    sc.feed([
        (_T0 + timedelta(milliseconds=250 * i), i + 1, float(px), 100.0, px - 0.01, px)
        for i, px in enumerate(prices)
    ])
    st = sc.to_dict()
    st["day"] = "2026-09-10"
    st["feed"] = {"fed": sc.n_prints, "reads": 1, "caught_up": bool(caught_up)}
    return st


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, db, sess, event_type, payload):  # noqa: ANN001 — mirrors lr._emit
        self.events.append((event_type, dict(payload)))
        return SimpleNamespace(id=len(self.events))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def last(self, event_type: str) -> dict[str, Any]:
        return [p for t, p in self.events if t == event_type][-1]


@pytest.fixture()
def bench_env(monkeypatch):
    rec = _Recorder()
    frame = {"df": _faded_frame()}
    monkeypatch.setattr(lr, "_emit", rec)
    monkeypatch.setattr(lr, "_replay_aware_fetch_ohlcv_df", lambda *_a, **_k: frame["df"])
    sess = SimpleNamespace(id=5601, symbol="BNCH", risk_snapshot_json={}, correlation_id=None)
    return SimpleNamespace(rec=rec, frame=frame, sess=sess)


def _tick(px: float) -> SimpleNamespace:
    return SimpleNamespace(ask=px, mid=px - 0.005, bid=px - 0.01)


def _pass(env, le, *, px, trigger_ok, trigger_reason):
    return lr._sticky_backside_bench_pass(
        None, env.sess, le, tick=_tick(px), trigger_ok=trigger_ok, trigger_reason=trigger_reason,
    )


def _fire(env, le, *, fire_path, trigger_reason, px=5.01):
    return lr._backside_bench_condition_fire(
        None, env.sess, le, fire_path=fire_path, trigger_reason=trigger_reason, tick=_tick(px),
    )


def _latched(env) -> dict[str, Any]:
    le: dict[str, Any] = {}
    _pass(env, le, px=5.01, trigger_ok=False, trigger_reason="waiting_for_break")
    assert le["benched_backside_hod"] == pytest.approx(10.0)
    return le


# ── (a) ANG PASS AY PHASE LAMANG — WALANG CONDITIONED EVENT DITO ─────────────────────
def test_the_pass_records_the_phase_and_never_emits_the_conditioned_event(bench_env):
    """Ang dating anyo ay naglabas ng conditioned event sa pass — BAGO ang opening-bell /
    burst / red-candle / G4 / chase gate sa parehong pass. Ngayon ang pass ay resibo ng PHASE."""
    le = _latched(bench_env)
    assert "live_entry_backside_benched" in bench_env.rec.types()
    r = _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="double_bottom_break_tick_ok")
    assert r["action"] == "benched"
    assert r["trigger"] is None and r["legacy_verdict"] is None and r["fire_path"] is None
    # ang trigger ng break ladder sa pass ay NAKATALA, pero hindi pa ito isang pasok
    assert r["trigger_at_pass"] == "double_bottom_break_tick_ok"
    assert r["benched_at_hod"] == pytest.approx(10.0)
    assert r["current_px"] == pytest.approx(5.01) and r["current_px_source"] == "quote_ask"
    assert r["retrace_pct"] == pytest.approx(49.9)
    assert "size_mult" not in r  # ang size ay desisyon ng FIRE, hindi ng phase
    assert "live_entry_backside_bench_conditioned" not in bench_env.rec.types()
    assert "live_entry_backside_bench_veto" not in bench_env.rec.types()


# ── (b) ANG FIRE POINT: TATLONG DAAN, BAWAT ISA MAY SARILING LEGACY VERDICT ─────────────
@pytest.mark.parametrize(
    "trigger",
    ["double_bottom_break_tick_ok",   # structural (may pullback_low)
     "momentum_ok_rel_vol"],          # hindi structural — dati ay LAGING kinakain
)
def test_a_break_fire_on_a_benched_name_is_conditioned_not_vetoed(bench_env, trigger):
    le = _latched(bench_env)
    _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason=trigger)
    receipt = _fire(bench_env, le, fire_path="break", trigger_reason=trigger)
    types = bench_env.rec.types()
    assert "live_entry_backside_bench_veto" not in types
    assert types.count("live_entry_backside_bench_conditioned") == 1
    ev = bench_env.rec.last("live_entry_backside_bench_conditioned")
    assert ev["preserved_trigger"] == trigger and ev["trigger"] == trigger
    assert ev["action"] == "conditioned" and ev["fire_path"] == "break"
    assert ev["reason"] == "benched_backside_sticky"
    # 49.9% na retrace ay LAMPAS sa #1274 window (12%) ⇒ ang lumang code ay nag-veto sana
    assert ev["legacy_verdict"] == "veto"
    assert ev["legacy_veto_site"] == "bench_block"
    # walang tape ledger sa le ⇒ NAMED fail-open, hindi hula
    assert ev["size_mult"] == 1.0
    assert ev["size_binding"]["reason"] == "no_tape_state"
    assert ev["binding"]["size_by_retrace"] == [dict(r) for r in _TABLE]
    assert ev["binding"]["rule"] == BACKSIDE_BENCH_MEASURED["rule"]
    assert "cycle_exhaustion_at_fire" in ev
    assert le[lr._BACKSIDE_BENCH_RECEIPT_KEY] == receipt


def test_the_legacy_structure_window_is_the_break_paths_verdict(bench_env):
    """Ang YJ-hugis: structural trigger, ~10% retrace off ang benched HOD ⇒ ang lumang code ay
    NAGPRESERBA sana (structure exception). Ang parehong sandali sa hindi-structural na trigger ay
    veto sana. Ngayon ay pinapanatili ang DALAWA; ang resibo ang nagsasabi kung alin."""
    from zoneinfo import ZoneInfo

    idx = pd.date_range("2026-09-10T14:00:00Z", periods=20, freq="1min")
    closes = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0] + [9.2] * 14
    bench_env.frame["df"] = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [100.0] * 20},
        index=idx,
    )
    today_et = lr._now_in_tz(ZoneInfo("America/New_York")).date().isoformat()
    le: dict[str, Any] = {"benched_backside_hod": 10.0, "benched_backside_session_date_et": today_et}
    r = _pass(bench_env, le, px=9.0, trigger_ok=True, trigger_reason="double_bottom_break_tick_ok")
    assert r["reason"] == "benched_backside_sticky"
    kept = _fire(bench_env, le, fire_path="break", trigger_reason="double_bottom_break_tick_ok", px=9.0)
    assert kept["legacy_verdict"] == "structure_exception"
    assert kept["legacy_veto_site"] is None
    assert kept["structure_window"]["retrace_pct"] == pytest.approx(10.0)
    _pass(bench_env, le, px=9.0, trigger_ok=True, trigger_reason="momentum_ok_rel_vol")
    other = _fire(bench_env, le, fire_path="break", trigger_reason="momentum_ok_rel_vol", px=9.0)
    assert other["legacy_verdict"] == "veto" and other["legacy_veto_site"] == "bench_block"
    assert other["structure_window"] == {"structural_trigger": False}
    assert bench_env.rec.types().count("live_entry_backside_bench_conditioned") == 2


@pytest.mark.parametrize(
    "fire_path,trigger,site",
    [("tape_hold", "tape_confirmed_hold", "tape_hold_gate"),
     ("continuation", "momentum_continuation", "continuation_gate")],
)
def test_tape_hold_and_continuation_fires_carry_the_silent_veto_they_replaced(
    bench_env, fire_path, trigger, site,
):
    """REVIEW FIX: dati ay `legacy_verdict='no_trigger'` ang dala ng dalawang ito, gayong ang
    lumang code ay TAHIMIK na tumanggi sa bawat benched na pangalan
    (`and le.get("benched_backside_hod") is None`, walang structure exception). Ang forward query
    na `legacy_verdict='veto'` ay makakaligtaan sana ang bawat pasok na bagong pinapapasok."""
    le = _latched(bench_env)
    # ang break ladder ay NAGHIHINTAY (walang trigger) — ganito lang umaabot ang dalawang daan
    r = _pass(bench_env, le, px=5.01, trigger_ok=False, trigger_reason="waiting_for_reclaim_high")
    assert r["trigger_at_pass"] is None
    rec = _fire(bench_env, le, fire_path=fire_path, trigger_reason=trigger)
    assert rec["action"] == "conditioned" and rec["fire_path"] == fire_path
    assert rec["trigger"] == trigger
    assert rec["legacy_verdict"] == "veto"
    assert rec["legacy_veto_site"] == site
    assert rec["structure_window"] == {"route": fire_path, "structure_exception_applied": False}
    ev = bench_env.rec.last("live_entry_backside_bench_conditioned")
    assert ev["legacy_verdict"] == "veto" and ev["preserved_trigger"] == trigger


def test_a_front_side_fire_is_not_conditioned(bench_env):
    """Walang resibo ng `benched` ⇒ walang ginagawa ang fire point (walang event, walang size)."""
    le: dict[str, Any] = {}
    assert _fire(bench_env, le, fire_path="break", trigger_reason="hod_break_tick_ok") is None
    le = {lr._BACKSIDE_BENCH_RECEIPT_KEY: {"action": "error", "error_type": "ValueError"}}
    assert _fire(bench_env, le, fire_path="break", trigger_reason="hod_break_tick_ok") is None
    assert le[lr._BACKSIDE_BENCH_RECEIPT_KEY]["action"] == "error"
    assert bench_env.rec.types() == []


# ── (c) ANG SIZE: HINANGO SA RETRACE NG TAPE, BINABASA NG SIZING ──────────────────────
@pytest.mark.parametrize(
    "tail,stratum_idx",
    [([9.99, 9.9], 0),        # 1.0% sa ilalim ng run_hi  → hinahabol ang taas
     ([9.7, 9.5], 1),         # 5.0%                      → pullback
     ([9.2, 9.0], 2),         # 10.0%                     → pullback
     ([8.0, 7.0], 3),         # 30.0%                     → bumagsak na
     ([6.0, 4.0], 3)],        # 60% (DEGEN sa derivation) → ang bukas na itaas na stratum
)
def test_the_fire_is_sized_by_the_tape_retrace_stratum(bench_env, tail, stratum_idx):
    le = _latched(bench_env)
    le["tape_cycle_state"] = _ledger([5.0, 7.0, 9.0, 10.0] + tail)
    _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="hod_break_tick_ok")
    rec = _fire(bench_env, le, fire_path="break", trigger_reason="hod_break_tick_ok")
    row = _TABLE[stratum_idx]
    assert rec["size_mult"] == pytest.approx(row["mult"])
    sb = rec["size_binding"]
    assert sb["retrace_source"] == "tape_run_hi"
    assert sb["retrace_pct"] == pytest.approx((10.0 - tail[-1]) / 10.0 * 100.0, abs=0.01)
    assert sb["stratum"]["lo_pct"] == row["lo_pct"]
    assert rec["tape"]["run_hi"] == pytest.approx(10.0)
    assert rec["tape"]["last_print"] == pytest.approx(tail[-1])
    # ang [62] mult sa sandali ng fire — kasama sa CONDITIONED na populasyon (review fix)
    ce = rec["cycle_exhaustion_at_fire"]
    assert "mult" in ce and 0.0 < ce["mult"] <= 1.0


def test_a_ledger_still_backfilling_never_decides_the_size(bench_env):
    le = _latched(bench_env)
    le["tape_cycle_state"] = _ledger([5.0, 10.0, 7.0], caught_up=False)
    _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="hod_break_tick_ok")
    rec = _fire(bench_env, le, fire_path="break", trigger_reason="hod_break_tick_ok")
    assert rec["size_mult"] == 1.0
    assert rec["size_binding"]["reason"] == "tape_not_caught_up"


def test_the_size_multiplier_picks_lo_inclusive_hi_exclusive_and_fails_open_by_name():
    for row in _TABLE:
        m, dbg = backside_bench_size_multiplier({"below_run_hi_pct": row["lo_pct"], "caught_up": True})
        assert m == pytest.approx(row["mult"]) and dbg["stratum"]["lo_pct"] == row["lo_pct"]
        if row["hi_pct"] is not None:
            m2, dbg2 = backside_bench_size_multiplier(
                {"below_run_hi_pct": row["hi_pct"] - 1e-6, "caught_up": True})
            assert dbg2["stratum"]["lo_pct"] == row["lo_pct"]
    cases = [
        (None, "no_tape_state"),
        ({"below_run_hi_pct": 20.0, "caught_up": False}, "tape_not_caught_up"),
        ({"below_run_hi_pct": float("nan"), "caught_up": True}, "retrace_unreadable"),
        ({"below_run_hi_pct": None, "caught_up": True}, "retrace_unreadable"),
        ({"below_run_hi_pct": -1.0, "caught_up": True}, "retrace_unreadable"),
    ]
    for tape, reason in cases:
        m, dbg = backside_bench_size_multiplier(tape)
        assert (m, dbg["reason"]) == (1.0, reason), tape
    # ang isang sirang talaan ay hindi kailanman nagiging size-UP
    m, dbg = backside_bench_size_multiplier(
        {"below_run_hi_pct": 1.0, "caught_up": True},
        table=({"lo_pct": 0.0, "hi_pct": None, "mult": 1.7},),
    )
    assert m == 1.0 and dbg["reason"] == "stratum_mult_invalid"


def _sizing_block(start: str, end: str) -> str:
    src = inspect_source_tick()
    i = src.index(start)
    return textwrap.dedent(src[i:src.index(end, i)])


def inspect_source_tick() -> str:
    import inspect

    return inspect.getsource(lr.tick_live_session)


def test_sizing_reads_the_receipt_so_the_order_and_the_receipt_agree():
    """REVIEW FIX: ang `derived_mult` ay iniuulat pero WALANG sizing na bumabasa — ang resibo ay
    maaaring magsabi ng 0.8 habang ang shares ay hindi nagbabago. Ngayon ay ang IPINADALANG
    read block ang pinapatakbo: conditioned ⇒ ang `size_mult` ng resibo; iba ⇒ 1.0."""
    block = _sizing_block(
        "        # ── [56] BACKSIDE BENCH SIZE (review fix 2026-09-11)",
        "        # LOW-7: sanitize EACH per-factor multiplier",
    )
    cases = [
        ({"action": "conditioned", "size_mult": 0.685}, 0.685),
        ({"action": "conditioned", "size_mult": 1.0}, 1.0),
        ({"action": "benched"}, 1.0),                        # hindi pumutok ⇒ walang size
        ({"action": "error", "error_type": "ValueError"}, 1.0),
        ({"action": "conditioned", "size_mult": float("nan")}, 1.0),
        ({"action": "conditioned", "size_mult": 1.4}, 1.0),  # size-DOWN lamang
        (None, 1.0),
    ]
    for rec, want in cases:
        le = {} if rec is None else {lr._BACKSIDE_BENCH_RECEIPT_KEY: rec}
        ns: dict[str, Any] = {
            "le": le, "_float_or_none": lr._float_or_none,
            "_BACKSIDE_BENCH_RECEIPT_KEY": lr._BACKSIDE_BENCH_RECEIPT_KEY,
        }
        exec(compile(block, "<bench_read>", "exec"), ns, ns)  # noqa: S102 — shipped source
        assert ns["_backside_bench_mult"] == pytest.approx(want), rec


def _product_and_receipt() -> str:
    src = _LR_SRC
    start = src.index(
        "        _eff_max_loss = min(\n            float(_base_max_loss) * _safe_mult(_streak_mult)"
    )
    end = src.index("\n        )\n", start) + len("\n        )\n")
    r0 = src.index('            le["risk_mults"] = {\n', end)
    r1 = src.index("\n            }\n", r0) + len("\n            }\n")
    return textwrap.dedent(src[start:end]) + textwrap.dedent(src[r0:r1])


def test_the_bench_mult_is_in_the_executed_product_and_the_risk_mults_receipt():
    stmt = _product_and_receipt()
    names = set(re.findall(r"_safe_mult\((_[A-Za-z0-9_]+)\)", stmt))
    assert "_backside_bench_mult" in names
    for bench_mult in (1.0, 0.685):
        le: dict[str, Any] = {}
        ns: dict[str, Any] = {n: 1.0 for n in names}
        ns.update({"_safe_mult": lr._safe_mult, "_base_max_loss": 390.0, "le": le,
                   "_backside_bench_mult": bench_mult})
        exec(compile(stmt, "<sizing>", "exec"), ns, ns)  # noqa: S102 — shipped source
        assert ns["_eff_max_loss"] == pytest.approx(390.0 * bench_mult)
        assert le["risk_mults"]["backside_bench"] == pytest.approx(bench_mult)


def _run_post_floor(*, paper_floor_fired: bool, mult: float, eff: float, le=None):
    block = _sizing_block(
        "        # [56] BACKSIDE BENCH SIZE BINDS ON PAPER TOO",
        "        # STARTER-SIZE BY TRIGGER CLASS",
    )
    recorded: list[tuple] = []
    le = {} if le is None else le
    ns: dict[str, Any] = {
        "_paper_floor_fired": paper_floor_fired, "_backside_bench_mult": mult,
        "_eff_max_loss": eff, "le": le,
        "_BACKSIDE_BENCH_RECEIPT_KEY": lr._BACKSIDE_BENCH_RECEIPT_KEY,
        "_record_post_floor": lambda *a: recorded.append(a),
    }
    exec(compile(block, "<bench_post_floor>", "exec"), ns, ns)  # noqa: S102 — shipped source
    return ns["_eff_max_loss"], le, recorded


def test_the_bench_size_binds_on_paper_after_the_full_size_floor():
    """Ang aral ng [62]: ang mult na nasa product lamang ay ibinabalik ng paper floor sa base."""
    le = {lr._BACKSIDE_BENCH_RECEIPT_KEY: {
        "action": "conditioned", "size_mult": 0.685,
        "size_binding": {"retrace_pct": 30.0, "stratum": dict(_TABLE[3])},
    }}
    eff, le, recorded = _run_post_floor(paper_floor_fired=True, mult=0.685, eff=390.0, le=le)
    assert eff == pytest.approx(390.0 * 0.685)
    pf = le["backside_bench_post_floor"]
    assert pf["mult"] == pytest.approx(0.685)
    assert pf["effective_usd"] == pytest.approx(round(390.0 * 0.685, 2))
    assert pf["retrace_pct"] == 30.0 and pf["stratum"]["lo_pct"] == _TABLE[3]["lo_pct"]
    assert recorded and recorded[0][:2] == ("backside_bench", "mult")
    # walang floor (real-money path, nasa product na) / full size ⇒ walang double-apply
    for fired, m in ((False, 0.685), (True, 1.0)):
        eff2, le2, rec2 = _run_post_floor(paper_floor_fired=fired, mult=m, eff=200.0)
        assert eff2 == pytest.approx(200.0) and "backside_bench_post_floor" not in le2 and not rec2
    # ang talaan ng nakaraang leg ay hindi kailanman nabubuhay sa isang full-size na leg
    stale = {"backside_bench_post_floor": {"mult": 0.685}}
    _eff3, le3, _r3 = _run_post_floor(paper_floor_fired=True, mult=1.0, eff=390.0, le=stale)
    assert "backside_bench_post_floor" not in le3


# ── (d) PER-PASS NA RESIBO; NABUBURA SA UN-BENCH; NAMED NA ERROR; PER-TRADE SA RECYCLE ─
def test_the_receipt_is_cleared_on_unbench_and_the_marker_drops(bench_env):
    le = _latched(bench_env)
    _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="double_bottom_break_tick_ok")
    assert lr._BACKSIDE_BENCH_RECEIPT_KEY in le
    out = _pass(bench_env, le, px=10.5, trigger_ok=True, trigger_reason="hod_break_tick_ok")
    assert out is None
    assert lr._BACKSIDE_BENCH_RECEIPT_KEY not in le
    assert "benched_backside_hod" not in le
    assert bench_env.rec.types()[-1] == "live_entry_backside_unbenched"


def test_a_front_side_name_never_carries_a_receipt(bench_env):
    idx = pd.date_range("2026-09-10T14:00:00Z", periods=20, freq="1min")
    closes = [5.0 + 0.1 * i for i in range(20)]  # malinis na takbo pataas
    bench_env.frame["df"] = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [100.0] * 20},
        index=idx,
    )
    le: dict[str, Any] = {lr._BACKSIDE_BENCH_RECEIPT_KEY: {"stale": True}}
    assert _pass(bench_env, le, px=6.95, trigger_ok=True, trigger_reason="hod_break_tick_ok") is None
    assert lr._BACKSIDE_BENCH_RECEIPT_KEY not in le
    assert "benched_backside_hod" not in le


def test_a_broken_bench_read_fails_open_with_a_named_receipt_not_none(bench_env, monkeypatch):
    """REVIEW FIX: ang ``None`` ay ang halaga rin ng pangalang hindi benched; ang fill payload ay
    dapat makapaghiwalay ng dalawa nang walang join sa error event."""
    def _boom(*_a, **_k):
        raise ValueError("frame exploded")

    monkeypatch.setattr(eg, "evaluate_sticky_backside_bench", _boom)
    le: dict[str, Any] = {lr._BACKSIDE_BENCH_RECEIPT_KEY: {"stale": True}}
    assert _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="hod_break_tick_ok") is None
    rec = le[lr._BACKSIDE_BENCH_RECEIPT_KEY]
    assert rec["action"] == "error" and rec["error_type"] == "ValueError" and rec["count"] == 1
    assert le["backside_bench_error_count"] == 1
    assert bench_env.rec.types() == ["live_entry_backside_bench_error"]
    # ...at ang fire point ay hindi ito ginagawang size
    assert _fire(bench_env, le, fire_path="break", trigger_reason="hod_break_tick_ok") is None


def test_the_receipt_is_jsonb_safe_even_when_the_bench_debug_is_not(bench_env, monkeypatch):
    """Ang resibo ay nakatira sa ``risk_snapshot_json`` (JSONB) — ang NaN ay tinatanggihan ng
    Postgres at sisira sa BUONG commit ng tick (ang CLRO 07-07 vol_ratio insidente)."""
    import json

    monkeypatch.setattr(eg, "evaluate_sticky_backside_bench", lambda *_a, **_k: (
        True, "benched_backside_sticky", 10.0,
        {"fs_session_vwap": float("nan"), "cur_hod": np.float64("inf"), "flag": np.bool_(True),
         "n": np.int64(3)},
    ))
    le: dict[str, Any] = {}
    r = _pass(bench_env, le, px=float("nan"), trigger_ok=True, trigger_reason="hod_break_tick_ok")
    json.dumps(le, allow_nan=False)
    assert r["bench_dbg"] == {"fs_session_vwap": None, "cur_hod": None, "flag": True, "n": 3}
    assert r["current_px"] is None and r["current_px_source"] is None and r["retrace_pct"] is None
    _fire(bench_env, le, fire_path="break", trigger_reason="hod_break_tick_ok", px=float("nan"))
    json.dumps(le, allow_nan=False)
    json.dumps(bench_env.rec.last("live_entry_backside_bench_conditioned"), allow_nan=False)


def test_the_receipts_are_per_trade_but_the_latch_survives_recycle():
    for key in ("backside_bench_receipt", "backside_bench_post_floor"):
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "benched_backside_hod" not in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "benched_backside_session_date_et" not in lr._RECYCLE_ENTRY_STATE_KEYS
    le = {
        "backside_bench_receipt": {"reason": "benched_backside_sticky"},
        "backside_bench_post_floor": {"mult": 0.685},
        "benched_backside_hod": 10.0,
        "benched_backside_session_date_et": "2026-09-10",
    }
    cleared = lr._reset_entry_state_on_recycle(le)
    assert {"backside_bench_receipt", "backside_bench_post_floor"} <= set(cleared)
    assert le == {"benched_backside_hod": 10.0, "benched_backside_session_date_et": "2026-09-10"}


def test_the_receipt_carries_the_tape_side_of_the_question_from_the_62_ledger(bench_env):
    le: dict[str, Any] = {"tape_cycle_state": {
        "n_prints": 4200, "run_hi": 10.02, "last_px": 5.03, "feed": {"caught_up": True},
    }}
    r = _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="hod_break_tick_ok")
    assert r["tape"] == {
        "run_hi": 10.02, "last_print": 5.03, "below_run_hi_pct": 49.8,
        "n_prints": 4200, "caught_up": True,
    }
    le2: dict[str, Any] = {}
    assert _pass(bench_env, le2, px=5.01, trigger_ok=True, trigger_reason="hod_break_tick_ok")["tape"] is None


# ── (e) ANG BINDING: ANG MGA NAHUHULOG AY INIULAT, AT ANG TALAAN AY MAGKAKAUGNAY ────────
def test_the_binding_reports_what_the_resolved_metric_drops():
    """REVIEW FIX: ang flat 1.0 ay galing sa isang pagbubukod na hindi iniulat (NONE + 11 sa 78
    na bench cluster). Ang binding ay nagdadala na ng pagbubukod at ng DALAWANG sukat."""
    m = BACKSIDE_BENCH_MEASURED
    ex = m["exclusions"]
    assert ex["bench_all_none_clusters"] > 0 and ex["bench_clusters"] > ex["bench_all_none_clusters"]
    assert ex["bench_none_share"] > ex["control_none_share"]
    assert ex["bench_degen_rows"] > 0
    for cut in ("whole", "prefix", "postfix_0910"):
        u, r = m["unconditional"][cut], m["resolved_only"][cut]
        assert u["ratio"] == pytest.approx(u["bench_p_up"] / u["control_p_up"], abs=2e-3)
        assert r["ratio"] == pytest.approx(r["bench_p_up"] / r["control_p_up"], abs=2e-3)
        # sa tanong ng bench mismo, ang bench ay mas madalang gumawa ng bagong high sa bawat hiwa
        assert u["ratio"] < 1.0
    assert "derived_mult" not in m  # walang flat na numero na hindi binabasa ng sizing


def test_the_size_table_is_contiguous_and_each_mult_is_the_rule_applied_to_its_inputs():
    """Ang talaan ay ang IISANG lugar kung saan nakatira ang halaga — at ang bawat `mult` ay ang
    panuntunan sa sarili nitong mga input (hindi nae-edit nang hiwalay sa ebidensya)."""
    assert _TABLE[0]["lo_pct"] == 0.0 and _TABLE[-1]["hi_pct"] is None
    for a, b in zip(_TABLE[:-1], _TABLE[1:]):
        assert a["hi_pct"] == b["lo_pct"] and a["lo_pct"] < a["hi_pct"]
    for row in _TABLE:
        ratio = row["bench_p_up"] / row["control_p_up"]
        assert row["ratio"] == pytest.approx(ratio, abs=2e-3)
        assert row["mult"] == pytest.approx(min(1.0, row["ratio"]), abs=1e-9)
        assert 0.0 < row["mult"] <= 1.0
        assert row["bench_clusters"] > 0 and row["control_clusters"] > 0


def _derivation_module():
    import importlib.util

    p = _REPO / "scripts" / "backside_bench_side_test_56.py"
    spec = importlib.util.spec_from_file_location("backside_bench_side_test_56", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_derivation_label_is_symmetric_first_passage_on_prints_with_a_degen_class():
    d = _derivation_module()
    # H=10, px=8 ⇒ DOWN barrier 6. Unang tumama: 10 (UP) bago 6.
    assert d.first_passage(np.array([9.0, 9.5, 10.0, 5.0]), 10.0, 6.0) == "UP"
    assert d.first_passage(np.array([7.0, 6.0, 10.0]), 10.0, 6.0) == "DOWN"
    assert d.first_passage(np.array([7.0, 9.0]), 10.0, 6.0) == "NONE"
    # REVIEW FIX: px <= H/2 ⇒ ang DOWN barrier ay <= 0 — isang panig lang ang kayang lumapag
    assert d.label_instant(np.array([4.0, 0.5, 0.01]), 10.0, 5.0) == "DEGEN"
    assert d.label_instant(np.array([4.0, 11.0]), 10.0, 4.0) == "DEGEN"
    assert d.label_instant(np.array([11.0]), 10.0, 10.0) == "ATHIGH"
    assert d.label_instant(np.array([8.0, 10.5]), 10.0, 6.0) == "UP"
    # ang binomial na iniulat para sa bar-anchor scorecard (33 tama / 23 mali)
    assert round(d._binom_two_sided(33, 56), 3) == BACKSIDE_BENCH_MEASURED["bar_anchor"]["binom_p"]


def _row(cl, lab, retr):
    return {"cl": cl, "lab": lab, "retr": retr, "mult": 1.0}


def test_the_derivation_counts_none_as_no_new_high_and_reports_the_dropped_clusters():
    d = _derivation_module()
    rows = (
        [_row(("A", 1), "UP", 0.05), _row(("A", 1), "DOWN", 0.05)]       # A: 1/2 up
        + [_row(("B", 1), "NONE", 0.2), _row(("B", 1), "NONE", 0.2)]     # B: puro NONE
        + [_row(("C", 1), "UP", 0.2), _row(("C", 1), "NONE", 0.2)]       # C: 1 up, 1 none
        + [_row(("D", 1), "DEGEN", 0.6), _row(("D", 1), "ATHIGH", 0.0)]  # D: walang sukat
    )
    cr = d.cluster_rates(rows)
    assert cr["clusters"] == 3                 # D ay walang nasusukat na hilera
    assert cr["dropped_all_none_clusters"] == 1  # B
    assert cr["clusters_resolved"] == 2
    assert cr["p_up"] == pytest.approx((0.5 + 0.0 + 0.5) / 3)
    assert cr["p_up_resolved"] == pytest.approx((0.5 + 1.0) / 2)
    assert cr["p_none"] == pytest.approx((0.0 + 1.0 + 0.5) / 3)


def test_the_derivation_strata_are_the_control_quartiles_and_the_mult_is_size_down_only():
    d = _derivation_module()
    ctrl = [_row(("C", i), "UP" if i % 2 else "DOWN", r)
            for i, r in enumerate([0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09])]
    edges = d.stratum_edges(ctrl)
    assert edges[0] == 0.0 and math.isinf(edges[-1]) and len(edges) == 5
    assert edges[1:-1] == pytest.approx([0.03, 0.05, 0.07])
    bench = (
        [_row(("B", 1), "UP", 0.02)] * 3                                  # stratum 0: all up
        + [_row(("B", 2), "NONE", 0.08)] * 3 + [_row(("B", 2), "UP", 0.08)]  # stratum 3
    )
    strata = d.stratified_binding(bench, ctrl, edges)
    assert [s["lo_pct"] for s in strata] == [0.0, 3.0, 5.0, 7.0]
    assert strata[-1]["hi_pct"] is None
    # stratum 0: bench 1.0 vs control 0.5 ⇒ ratio 2 ⇒ walang size-UP
    assert strata[0]["ratio"] == pytest.approx(2.0) and strata[0]["mult"] == 1.0
    # stratum 1: walang bench ⇒ mult 1.0 na PINANGALANAN
    assert strata[1]["mult"] == 1.0 and strata[1]["mult_reason"] == "no_bench_in_stratum"
    # stratum 3: control 1/3; bench P(UP)=1/4 (NONE = hindi-UP) ⇒ 0.75 — kahit ang
    # resolved-only na sukat ay 1.0/0.333 = 3.0 (ang NONE ang nagpapasya, at iniuulat pareho)
    assert strata[3]["control_p_up"] == pytest.approx(1 / 3, abs=1e-3)
    assert strata[3]["bench_p_up"] == pytest.approx(0.25)
    assert strata[3]["mult"] == pytest.approx(0.75, abs=1e-3)
    assert strata[3]["ratio_resolved"] == pytest.approx(3.0, abs=1e-2)


def test_the_derivation_reads_the_population_the_code_emits_now(db):
    """REVIEW FIX: ang tanging bench population ng script ay `live_entry_backside_bench_veto` —
    na hindi na kailanman inilalabas ng runner. Ang re-measure sa 09-12..09-19 ay magbabalik ng
    zero at TAHIMIK na babalik sa lumang halo. Ngayon ay binabasa nito ang conditioned fire, at
    ang benched na pasok ay hindi na napapabilang sa control."""
    from app.db import engine
    from app.models.trading import (
        MomentumStrategyVariant,
        TradingAutomationEvent,
        TradingAutomationSession,
    )

    d = _derivation_module()
    v = MomentumStrategyVariant(family="test_family", variant_key="bench56_pop",
                                label="bench56", params_json={})
    db.add(v)
    db.flush()
    s = TradingAutomationSession(venue="test", execution_family="alpaca_spot", mode="live",
                                 symbol="BNCH", variant_id=v.id, state="watching_live",
                                 risk_snapshot_json={})
    db.add(s)
    db.flush()
    t = datetime(2026, 9, 12, 14, 0, 0)
    rows = [
        ("live_entry_backside_bench_veto", {"blocked_trigger": "abcd_break_tick_ok",
                                            "benched_at_hod": 10.0, "current_px": 8.0,
                                            "reason": "benched_backside_sticky"}),
        ("live_entry_backside_bench_conditioned", {"preserved_trigger": "hod_break_tick_ok",
                                                   "benched_at_hod": 10.0, "current_px": 9.0,
                                                   "reason": "benched_backside_sticky",
                                                   "action": "conditioned"}),
        ("live_entry_submitted", {"place_profile_ms": {"total": 1000.0},
                                  "backside_bench": {"action": "conditioned"}}),
        ("live_entry_submitted", {"place_profile_ms": {"total": 500.0},
                                  "backside_bench": None}),
        ("live_entry_submitted", {"place_profile_ms": {"total": 0.0}}),
        ("live_entry_backside_benched", {"reason": "benched_backside_below_vwap"}),
    ]
    for i, (et, p) in enumerate(rows):
        db.add(TradingAutomationEvent(session_id=s.id, ts=t + timedelta(seconds=i),
                                      event_type=et, payload_json=p))
    db.commit()
    V = d.bench_vetoes(engine, t - timedelta(minutes=1), t + timedelta(minutes=1))
    assert sorted(r.source for r in V) == sorted(d.BENCH_EVENT_TYPES)
    conditioned = [r for r in V if r.source == "live_entry_backside_bench_conditioned"][0]
    assert (conditioned.px, conditioned.hod, conditioned.trig) == (9.0, 10.0, "hod_break_tick_ok")
    vetoed = [r for r in V if r.source == "live_entry_backside_bench_veto"][0]
    assert vetoed.trig == "abcd_break_tick_ok"
    C = d.control_decisions(engine, t - timedelta(minutes=1), t + timedelta(minutes=1))
    assert len(C) == 2  # ang benched na submit ay BENCH, hindi control


# ── (f) ANG SCORING NG BENCH AY SUMUSUNOD — AT ANG TIMELINE AY KAPAREHO NITO ─────────────
def test_scoring_keeps_the_historical_veto_and_treats_the_new_events_as_receipts():
    from app.services.trading.momentum_neural import ross_bench_scoring as rbs

    assert BENCH_VETO_EVENTS == frozenset({"live_entry_backside_bench_veto"})
    assert "live_entry_backside_bench_conditioned" in BENCH_RECEIPT_EVENTS
    assert "live_entry_backside_benched" in BENCH_RECEIPT_EVENTS
    for t in BENCH_RECEIPT_EVENTS:
        assert rbs._is_decision(t)
        assert not rbs._is_refusal(t)


def test_the_timeline_agrees_with_the_scorer_on_the_bench_receipts():
    """REVIEW FIX: `rossbench_timeline` ay nagmamapa ng BAWAT event na may "benched" sa
    `blocked` — kaya ang latch (at ang un-bench!) ay "gate na tumanggi" doon, habang resibo sa
    `ross_bench_scoring`. Ang dalawang scorer ay nagkakasalungat sa parehong event."""
    import importlib.util

    p = _REPO / "scripts" / "rossbench_timeline.py"
    spec = importlib.util.spec_from_file_location("rossbench_timeline_56", p)
    tlm = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = tlm
    spec.loader.exec_module(tlm)
    for et in BENCH_RECEIPT_EVENTS | {"live_entry_backside_unbenched"}:
        assert tlm.stage_for_event(et) != tlm.STAGE_BLOCKED, et
    assert tlm.stage_for_event("live_entry_backside_benched") == tlm.STAGE_WATCHING
    assert tlm.stage_for_event("live_entry_backside_bench_conditioned") == tlm.STAGE_CANDIDATE
    assert tlm.stage_for_event("live_entry_backside_bench_veto") == tlm.STAGE_BLOCKED


def _sev(ts: str, t: str, **p) -> dict[str, Any]:
    return {"ts": f"2026-09-11 13:{ts}", "event_type": t, "payload": p}


_ARMED = [
    _sev("00:00", "live_arm_requested"), _sev("00:01", "live_arm_confirmed"),
    _sev("00:02", "live_runner_started"),
]


def test_a_latched_name_whose_break_never_came_is_a_trigger_wait_not_a_bench_veto():
    from app.services.trading.momentum_neural import ross_bench_scoring as rbs

    events = _ARMED + [
        _sev("00:03", "live_entry_backside_benched", reason="benched_backside_below_vwap"),
        _sev("00:04", "live_entry_trigger_wait", reason="waiting_for_break"),
        _sev("00:05", "live_entry_trigger_wait", reason="waiting_for_break"),
    ]
    stage = rbs.classify_events(events, source="events")
    assert stage.qualifier == "trigger_wait:waiting_for_break"
    assert stage.detail["bench_veto_count"] == 0


def test_a_historical_bench_veto_keeps_its_label():
    from app.services.trading.momentum_neural import ross_bench_scoring as rbs

    events = _ARMED + [
        _sev("00:03", "live_entry_backside_benched", reason="benched_backside_below_vwap"),
        _sev("00:06", "live_entry_backside_bench_veto", reason="benched_backside_sticky",
             blocked_trigger="abcd_break_tick_ok"),
    ]
    stage = rbs.classify_events(events, source="events")
    assert stage.qualifier == "bench_veto"
    assert stage.detail["blocked_triggers"] == {"abcd_break_tick_ok": 1}


def test_a_conditioned_trigger_is_never_scored_as_a_bench_veto():
    from app.services.trading.momentum_neural import ross_bench_scoring as rbs

    events = _ARMED + [
        _sev("00:03", "live_entry_backside_benched", reason="benched_backside_below_vwap"),
        _sev("00:04", "live_entry_backside_bench_conditioned", reason="benched_backside_sticky",
             preserved_trigger="abcd_break_tick_ok"),
    ]
    stage = rbs.classify_events(events, source="events")
    assert stage.qualifier != "bench_veto"


# ── (g) ANG PER-TRIGGER NA BACKSIDE VETO AY TUMITINGIN SA LIVE NA PRESYO (FTFT) ─────────
def _ftft_frame() -> pd.DataFrame:
    """Ang FTFT na hugis (test_backside_bench_sees_the_live_tick): ang huling completed close
    (2.65) ay isang buhok SA ILALIM ng frame VWAP habang ang live ay ~13% sa itaas nito."""
    idx = pd.to_datetime([f"2026-09-09 14:{m:02d}:00+00:00" for m in range(20)], utc=True)
    close = [2.60, 2.62, 2.66, 2.72, 2.80, 2.95, 3.20, 3.45, 3.55, 3.40,
             3.20, 3.05, 2.95, 2.85, 2.80, 2.75, 2.72, 2.70, 2.68, 2.65]
    return pd.DataFrame(
        {"Open": close, "High": [c + 0.03 for c in close], "Low": [c - 0.03 for c in close],
         "Close": close, "Volume": [40000] * 20},
        index=idx,
    )


def test_a_trigger_backside_read_no_longer_vetoes_on_the_stale_close(monkeypatch):
    """REVIEW FIX: 17 per-trigger `front_side_state(_today_session_frame(df))` na walang
    live_price — ang depekto ng FTFT na inayos ng 86ed59aaf SA BENCH lamang. Pinapatakbo ang
    tunay na `tape_confirmed_hold_trigger`; ang EMA/MACD rollover leg ay nilampasan para ang
    front_side_state leg lamang ang sinusukat."""
    from app.services.trading.momentum_neural import ross_momentum as rm

    real = rm.front_side_state
    monkeypatch.setattr(eg, "_detect_back_side", lambda *a, **k: (False, None))
    df = _ftft_frame()
    blind = real(df)
    # fixture guard: ang BLIND na pagbasa (ang lumang tawag, walang live) ay below_vwap
    assert blind.reason == "below_vwap" and blind.is_backside is True
    vwap = float(blind.session_vwap)
    live = vwap * 1.13                       # FTFT: ~13% sa itaas ng frame VWAP
    kwargs = dict(pullback_high=3.55, pullback_low=2.50, entry_interval="1m")
    # ang live ay 13% SA ITAAS ng VWAP ⇒ hindi na backside (bago ang ayos: tape_hold_backside)
    ok1, reason1, dbg1 = eg.tape_confirmed_hold_trigger(df, live_price=live, **kwargs)
    assert ok1 is True and reason1 == "tape_hold_ok", dbg1
    assert dbg1["above_vwap"] is True and dbg1["front_side_reason"] == "front_side"
    # ...at ang binasa ng backside read ay ang LIVE na presyo mismo
    seen: list[Any] = []

    def _spy(frame, **kw):
        seen.append(kw.get("live_price"))
        return real(frame, **kw)

    monkeypatch.setattr(rm, "front_side_state", _spy)
    eg.tape_confirmed_hold_trigger(df, live_price=live, **kwargs)
    assert seen == [pytest.approx(live)]
    # ang veto mismo ay buo: ang TUNAY na backside na hatol ay tumatanggi pa rin
    monkeypatch.setattr(rm, "front_side_state", lambda frame, **kw: real(frame, live_price=vwap * 0.90))
    ok2, reason2, dbg2 = eg.tape_confirmed_hold_trigger(df, live_price=live, **kwargs)
    assert (ok2, reason2) == (False, "tape_hold_backside")
    assert dbg2["front_side_reason"] == "below_vwap"


def test_every_trigger_front_side_read_passes_the_live_price():
    """Ang bawat `front_side_state(...)` sa loob ng isang function na may `live_price` ay dapat
    ipasa ito. Dalawang pinangalanang eksepsyon: ang VWAP-reclaim read ng bench (`session_vwap`
    lamang ang binabasa — hindi apektado ng live_price) at `add_into_halt_ok` (walang live tick
    habang halted; ang presyo nito ay ang bid)."""
    missing = []
    for fn in ast.walk(_EG_TREE):
        if not isinstance(fn, ast.FunctionDef):
            continue
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "front_side_state":
                kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
                if "live_price" in params and kw.get("live_price") != "live_price":
                    missing.append((fn.name, call.lineno))
                if "live_price" not in params:
                    assert fn.name == "add_into_halt_ok", (fn.name, call.lineno)
    assert [m[0] for m in missing] == ["evaluate_sticky_backside_bench"], missing


# ── AST: mga pag-aaring hindi kayang ipakita ng isang pure-function na test ──────────
def _emit_calls(tree: ast.AST, event_type: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_emit"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Constant)
            and node.args[2].value == event_type
        ):
            out.append(node)
    return out


def _fn(name: str) -> ast.FunctionDef:
    return next(
        n for n in ast.walk(_LR_TREE) if isinstance(n, ast.FunctionDef) and n.name == name
    )


def test_the_runner_never_emits_the_bench_veto_again():
    assert _emit_calls(_LR_TREE, "live_entry_backside_bench_veto") == []
    assert _emit_calls(_fn("_sticky_backside_bench_pass"), "live_entry_backside_bench_conditioned") == []
    assert len(_emit_calls(_fn("_backside_bench_condition_fire"), "live_entry_backside_bench_conditioned")) == 1


def _condition_fire_calls() -> list[ast.Call]:
    return [
        n for n in ast.walk(_fn("tick_live_session"))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_backside_bench_condition_fire"
    ]


def test_each_of_the_three_entry_routes_conditions_at_its_candidate_transition():
    """Ang tatlong fire point: bawat isa ay nasa PAREHONG statement list ng `_commit_le` at ng
    `_safe_transition(..., STATE_LIVE_ENTRY_CANDIDATE)` na sumusunod dito."""
    calls = _condition_fire_calls()
    paths = sorted(
        next(k.value.value for k in c.keywords if k.arg == "fire_path") for c in calls
    )
    assert paths == ["break", "continuation", "tape_hold"]
    tick = _fn("tick_live_session")
    for node in ast.walk(tick):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            if (
                isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and getattr(stmt.value.func, "id", None) == "_backside_bench_condition_fire"
            ):
                nxt = [ast.unparse(s) for s in body[i + 1:i + 3]]
                assert nxt[0].startswith("_commit_le(sess, le)"), nxt
                assert "_safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)" in nxt[1], nxt


def test_the_break_fire_point_runs_after_every_gate_that_can_still_refuse_the_trigger():
    """REVIEW FIX: ang conditioned event ay dating inilalabas BAGO ang opening-bell / burst /
    red-candle / G4 / chase gate, kaya ang `preserved_trigger` ay maaaring pangalanan ang trigger
    na hinarang pa rin sa parehong pass."""
    tick = _fn("tick_live_session")
    brk = next(
        c for c in _condition_fire_calls()
        if next(k.value.value for k in c.keywords if k.arg == "fire_path") == "break"
    )
    for et in ("live_entry_opening_bell_suppressed", "live_entry_order_burst_deferred",
               "live_entry_red_candle_blocked", "g4_reentry_escalation_blocked",
               "live_entry_halt_chain_blocked", "live_entry_postopen_quality_bar"):
        sites = [c.lineno for c in _emit_calls(tick, et)]
        assert sites and min(sites) < brk.lineno, et
    bench_pass = next(
        n for n in ast.walk(tick)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_sticky_backside_bench_pass"
    )
    assert bench_pass.lineno < brk.lineno


def test_the_call_site_cannot_rewrite_the_trigger():
    tick = _fn("tick_live_session")
    sites = [
        n for n in ast.walk(tick)
        if isinstance(n, ast.If) and any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "_sticky_backside_bench_pass"
            for c in ast.walk(n)
        )
    ]
    assert sites, "the bench pass is no longer called from tick_live_session"
    site = min(sites, key=lambda n: len(list(ast.walk(n))))
    for n in ast.walk(site):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            names = {t.id for t in targets for t in ast.walk(t) if isinstance(t, ast.Name)}
            assert not names & {"_trigger_ok", "_trigger_reason"}, ast.unparse(n)
    assert "backside_benched" not in {
        n.value for n in ast.walk(tick) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_the_two_silent_bench_refusals_are_gone():
    tick = _fn("tick_live_session")
    for n in ast.walk(tick):
        if isinstance(n, ast.If):
            for c in ast.walk(n.test):
                if (
                    isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "get"
                    and c.args
                    and isinstance(c.args[0], ast.Constant)
                    and c.args[0].value == "benched_backside_hod"
                ):
                    pytest.fail(f"bench marker still gates an entry: {ast.unparse(n.test)[:200]}")


def _payload_value(call: ast.Call, key: str) -> str | None:
    payload = call.args[3]
    if not isinstance(payload, ast.Dict):
        return None
    keys = [getattr(k, "value", None) for k in payload.keys]
    return ast.unparse(payload.values[keys.index(key)]) if key in keys else None


@pytest.mark.parametrize(
    "event_type",
    ["live_entry_filled", "live_entry_tape_hold_fire", "live_entry_momentum_continuation_fire"],
)
def test_entry_payloads_carry_the_bench_receipt(event_type):
    calls = _emit_calls(_fn("tick_live_session"), event_type)
    assert calls, event_type
    for call in calls:
        val = _payload_value(call, "backside_bench")
        assert val is not None and "_BACKSIDE_BENCH_RECEIPT_KEY" in val, event_type


def test_the_submit_and_fill_carry_the_receipt_the_derivation_splits_on():
    """Ang control ng derivation ay nagbubukod ng submit na `backside_bench.action =
    'conditioned'` — kaya ang submit payload ay DAPAT may resibo; ang fill ay may post-floor din."""
    subs = [c for c in _emit_calls(_LR_TREE, "live_entry_submitted")
            if _payload_value(c, "backside_bench") is not None]
    assert subs, "no live_entry_submitted payload carries the bench receipt"
    for c in subs:
        assert "_BACKSIDE_BENCH_RECEIPT_KEY" in _payload_value(c, "backside_bench")
        assert "backside_bench_post_floor" in _payload_value(c, "backside_bench_post_floor")
    for c in _emit_calls(_fn("tick_live_session"), "live_entry_filled"):
        assert "backside_bench_post_floor" in (_payload_value(c, "backside_bench_post_floor") or "")


# ── counterfactual replay parity ──────────────────────────────────────────────────────
def test_counterfactual_replay_keeps_the_benched_candidate(monkeypatch):
    from app.services.trading.momentum_neural.counterfactual_replay import (
        ReplayTapeTick,
        _iter_bar_candidates,
    )

    bars = _faded_frame()
    ticks = [ReplayTapeTick(ts=bars.index[0].to_pydatetime(), bid=4.99, ask=5.01, mid=5.0)]
    last = bars.index[-1].to_pydatetime()

    def _late_fire(family, *, now, **_kwargs):
        if family == "momentum_pullback" and now == last:
            return True, "synthetic_late_curl", {"pullback_low": 4.90}
        return False, "test_decline", {}

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay._call_bar_gate", _late_fire,
    )
    candidates, reasons = _iter_bar_candidates(symbol="BNCH", ticks=ticks, bars=bars, bar_seconds=60)
    assert len(candidates) == 1
    dbg = candidates[0].trigger_debug
    assert dbg["phase_benched"] is True
    assert dbg["phase_bench_reason"] == "benched_backside_sticky"
    assert isinstance(dbg.get("phase_bench"), dict)
    assert reasons.get("backside_bench_conditioned:benched_backside_sticky") == 1
    assert not any(k.startswith("backside_bench_veto:") for k in reasons)
