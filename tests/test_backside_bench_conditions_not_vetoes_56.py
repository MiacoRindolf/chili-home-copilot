"""[56] — ANG BACKSIDE BENCH AY RESIBO, HINDI VETO (2026-09-11).

Tinanong sa TAPE ang tanong ng bench ("hindi na ba gagawa ng bagong high?"): sa bawat sandaling
kinain ng bench ang isang trigger na PUMUTOK, alin ang unang nangyari sa mga print — umabot sa
running high ng tape, o bumaba nang PAREHONG layo sa ilalim? Ang mga tinanggihan nito ay HINDI
MAKILALA sa mga pasok na tinatanggap natin, at pagkatapos ng 86ed59aaf ay tinatanggihan pa nito
ang mga pangalang GUMAGAWA ng bagong high. Ang derivation ay
``scripts/backside_bench_side_test_56.py``; ang mga numero ay ``BACKSIDE_BENCH_MEASURED``.

Ang mga test dito ay nagpapatakbo ng MEKANISMO (``_sticky_backside_bench_pass`` na may tunay na
``evaluate_sticky_backside_bench`` at tunay na frame), hindi source offset. Ang tanging mga
istruktural na tseke ay AST — at iyon ay para sa mga pag-aari na hindi kayang ipakita ng isang
pure-function na test: na WALANG natitirang emit ng ``live_entry_backside_bench_veto`` sa
runner, na ang call site ay hindi nagsusulat sa ``_trigger_ok``, at na ang dalawang TAHIMIK na
kopya ng bench veto (tape-hold at continuation) ay wala na.

Runnable: pytest tests/test_backside_bench_conditions_not_vetoes_56.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

import app.services.trading.momentum_neural.entry_gates as eg
import app.services.trading.momentum_neural.live_runner as lr
from app.services.trading.momentum_neural.entry_gates import BACKSIDE_BENCH_MEASURED
from app.services.trading.momentum_neural.ross_bench_scoring import (
    BENCH_RECEIPT_EVENTS,
    BENCH_VETO_EVENTS,
)

_LR_SRC = Path(lr.__file__).read_text(encoding="utf-8")
_LR_TREE = ast.parse(_LR_SRC)


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


# ── (a) ANG TRIGGER NA PUMUTOK HABANG BENCHED AY HINDI KINAKAIN ───────────────────────
@pytest.mark.parametrize(
    "trigger",
    ["double_bottom_break_tick_ok",   # structural (may pullback_low)
     "momentum_ok_rel_vol"],          # hindi structural — dati ay LAGING kinakain
)
def test_a_fired_trigger_on_a_benched_name_is_conditioned_not_vetoed(bench_env, trigger):
    le: dict[str, Any] = {}
    # pass 1: nagla-latch ang bench sa kumupas na frame (walang trigger)
    first = _pass(bench_env, le, px=5.01, trigger_ok=False, trigger_reason="waiting_for_break")
    assert first is not None and first["trigger"] is None
    assert first["legacy_verdict"] == "no_trigger"
    assert le["benched_backside_hod"] == pytest.approx(10.0)
    assert "live_entry_backside_benched" in bench_env.rec.types()
    assert "live_entry_backside_bench_conditioned" not in bench_env.rec.types()

    # pass 2: benched pa rin (sticky) at PUMUTOK ang trigger
    receipt = _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason=trigger)
    types = bench_env.rec.types()
    assert "live_entry_backside_bench_veto" not in types
    assert types.count("live_entry_backside_bench_conditioned") == 1
    ev = bench_env.rec.last("live_entry_backside_bench_conditioned")
    assert ev["preserved_trigger"] == trigger
    assert ev["action"] == "conditioned"
    assert ev["reason"] == "benched_backside_sticky"
    assert ev["benched_at_hod"] == pytest.approx(10.0)
    assert ev["current_px"] == pytest.approx(5.01)
    assert ev["current_px_source"] == "quote_ask"
    assert ev["retrace_pct"] == pytest.approx(49.9)
    # 49.9% na retrace ay LAMPAS sa #1274 window (12%) ⇒ ang lumang code ay nag-veto sana
    assert ev["legacy_verdict"] == "veto"
    # binding: ang sinukat na hatol, iniuulat sa bawat resibo
    assert ev["binding"] == BACKSIDE_BENCH_MEASURED
    assert ev["size_mult"] == 1.0 == BACKSIDE_BENCH_MEASURED["derived_mult"]
    assert ev["sizing_owner"] == "cycle_exhaustion"
    # ang resibo ay nasa le para sa payload ng live_entry_filled
    assert le[lr._BACKSIDE_BENCH_RECEIPT_KEY] == receipt
    assert receipt["trigger"] == trigger


def test_the_legacy_structure_window_is_recorded_as_the_verdict_the_old_code_would_give(bench_env):
    """Ang YJ-hugis: structural trigger, ~10% retrace off ang benched HOD ⇒ ang lumang code ay
    NAGPRESERBA sana (structure exception). Ngayon ay pinapanatili LAHAT, pero ang resibo ay
    nagsasabi pa rin kung alin sa dalawa ang ginawa sana — ang NAMED na lumang gawi."""
    from zoneinfo import ZoneInfo

    # Tumakbo sa 10, humawak sa 9.2 (NASA IBABAW ng VWAP ~8.69 ⇒ walang VWAP-reclaim CROSS na
    # mag-a-un-bench). Ang marker ay naka-latch na sa 10.0 mula sa naunang pass.
    idx = pd.date_range("2026-09-10T14:00:00Z", periods=20, freq="1min")
    closes = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0] + [9.2] * 14
    bench_env.frame["df"] = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [100.0] * 20},
        index=idx,
    )
    today_et = lr._now_in_tz(ZoneInfo("America/New_York")).date().isoformat()
    le: dict[str, Any] = {"benched_backside_hod": 10.0, "benched_backside_session_date_et": today_et}
    # 9.0 = 10% sa ilalim ng anchor 10.0 — sa loob ng 3..12% window. Walang VWAP reading sa
    # sticky na daan ⇒ ang VWAP-hold leg ay hindi tumatakbo (dating gawi sa kulang na datos).
    receipt = _pass(bench_env, le, px=9.0, trigger_ok=True, trigger_reason="double_bottom_break_tick_ok")
    assert receipt["reason"] == "benched_backside_sticky"
    assert receipt["legacy_verdict"] == "structure_exception"
    assert receipt["structure_window"]["retrace_pct"] == pytest.approx(10.0)
    # ang PAREHONG sandali sa hindi-structural na trigger: veto sana ng lumang code
    other = _pass(bench_env, le, px=9.0, trigger_ok=True, trigger_reason="momentum_ok_rel_vol")
    assert other["legacy_verdict"] == "veto"
    assert other["structure_window"] == {"structural_trigger": False}
    assert "live_entry_backside_bench_veto" not in bench_env.rec.types()
    assert bench_env.rec.types().count("live_entry_backside_bench_conditioned") == 2


def test_a_trigger_that_did_not_fire_emits_no_conditioned_event_but_keeps_a_receipt(bench_env):
    """Ang tape-hold / continuation ay maaaring pumutok MAMAYA sa parehong pass — kaya ang resibo
    ay isinusulat kahit walang trigger; ang event ay para lamang sa trigger na pumutok."""
    le: dict[str, Any] = {}
    _pass(bench_env, le, px=5.01, trigger_ok=False, trigger_reason="waiting_for_break")
    r = _pass(bench_env, le, px=5.01, trigger_ok=False, trigger_reason="waiting_for_break")
    assert r is not None and r["trigger"] is None and r["legacy_verdict"] == "no_trigger"
    assert "live_entry_backside_bench_conditioned" not in bench_env.rec.types()


# ── (b) PER-PASS NA RESIBO; NABUBURA SA UN-BENCH; PER-TRADE SA RECYCLE ────────────────
def test_the_receipt_is_cleared_on_unbench_and_the_marker_drops(bench_env):
    le: dict[str, Any] = {}
    _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="double_bottom_break_tick_ok")
    _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="double_bottom_break_tick_ok")
    assert lr._BACKSIDE_BENCH_RECEIPT_KEY in le
    # tunay na bagong high sa ibabaw ng anchor ⇒ MANDATORY un-bench
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


def test_a_broken_bench_read_fails_open_with_a_counted_event_and_no_receipt(bench_env, monkeypatch):
    def _boom(*_a, **_k):
        raise ValueError("frame exploded")

    monkeypatch.setattr(eg, "evaluate_sticky_backside_bench", _boom)
    le: dict[str, Any] = {lr._BACKSIDE_BENCH_RECEIPT_KEY: {"stale": True}}
    assert _pass(bench_env, le, px=5.01, trigger_ok=True, trigger_reason="hod_break_tick_ok") is None
    assert lr._BACKSIDE_BENCH_RECEIPT_KEY not in le
    assert le["backside_bench_error_count"] == 1
    assert bench_env.rec.types() == ["live_entry_backside_bench_error"]


def test_the_receipt_is_jsonb_safe_even_when_the_bench_debug_is_not(bench_env, monkeypatch):
    """Ang resibo ay nakatira na sa ``risk_snapshot_json`` (JSONB) — ang NaN ay tinatanggihan ng
    Postgres at sisira sa BUONG commit ng tick (ang CLRO 07-07 vol_ratio insidente)."""
    import json

    import numpy as np

    monkeypatch.setattr(eg, "evaluate_sticky_backside_bench", lambda *_a, **_k: (
        True, "benched_backside_sticky", 10.0,
        {"fs_session_vwap": float("nan"), "cur_hod": np.float64("inf"), "flag": np.bool_(True),
         "n": np.int64(3)},
    ))
    le: dict[str, Any] = {}
    r = _pass(bench_env, le, px=float("nan"), trigger_ok=True, trigger_reason="hod_break_tick_ok")
    json.dumps(le, allow_nan=False)  # hindi nag-raise
    json.dumps(bench_env.rec.last("live_entry_backside_bench_conditioned"), allow_nan=False)
    assert r["bench_dbg"] == {"fs_session_vwap": None, "cur_hod": None, "flag": True, "n": 3}
    # NaN na ask ⇒ ang mid ang ginamit? hindi — NaN din ang mid ⇒ walang presyo, pinangalanan
    assert r["current_px"] is None and r["current_px_source"] is None and r["retrace_pct"] is None


def test_the_receipt_is_per_trade_but_the_latch_survives_recycle():
    assert "backside_bench_receipt" in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "benched_backside_hod" not in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "benched_backside_session_date_et" not in lr._RECYCLE_ENTRY_STATE_KEYS
    le = {
        "backside_bench_receipt": {"reason": "benched_backside_sticky"},
        "benched_backside_hod": 10.0,
        "benched_backside_session_date_et": "2026-09-10",
    }
    cleared = lr._reset_entry_state_on_recycle(le)
    assert "backside_bench_receipt" in cleared
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


# ── (c) ANG BINDING AY ANG MGA NUMERONG INIULAT SA PR ─────────────────────────────────
def test_the_measured_binding_is_the_reported_derivation():
    """Ang mga numerong ito ay ang output ng ``scripts/backside_bench_side_test_56.py
    --from 2026-09-01 --to 2026-09-11`` na sinipi sa PR body."""
    m = BACKSIDE_BENCH_MEASURED
    # buong window, anchor ng scout (planner row [56])
    assert (m["bench_clustered_up"], m["control_clustered_up"], m["ratio"]) == (0.422, 0.424, 0.994)
    assert (m["n_veto"], m["clusters"], m["control_n"], m["control_clusters"]) == (16672, 67, 127, 42)
    assert m["run_hi_anchor"] == {"bench_up": 0.413, "control_up": 0.409, "ratio": 1.011}
    assert m["prefix"]["ratio"] == 1.024
    assert m["postfix_0910"] == {"bench_up": 0.811, "control_up": 0.695, "ratio": 1.167,
                                 "clusters": 8, "control_clusters": 8}
    assert m["bar_anchor"] == {"clustered_up": 0.386, "right": 33, "wrong": 23, "binom_p": 0.229}
    assert m["derivation"].startswith("backside_bench_side_test_56")
    assert m["underived_literals"] == ["chili_momentum_backside_bench_min_fade_pct"]
    # ANG PANUNTUNAN: min(1, ratio ng populasyong ginagawa ng tumatakbong code). Size-DOWN lang.
    assert m["derived_mult"] == min(1.0, m["postfix_0910"]["ratio"]) == 1.0
    # at walang hiwa na nagbibigay ng tunay na size-down (lahat >= 0.99)
    for ratio in (m["ratio"], m["run_hi_anchor"]["ratio"], m["prefix"]["ratio"],
                  m["postfix_0910"]["ratio"]):
        assert ratio >= 0.99


def _derivation_module():
    import importlib.util

    p = Path(lr.__file__).resolve().parents[4] / "scripts" / "backside_bench_side_test_56.py"
    spec = importlib.util.spec_from_file_location("backside_bench_side_test_56", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_derivation_label_is_symmetric_first_passage_on_prints():
    import numpy as np

    d = _derivation_module()
    # H=10, px=8 ⇒ DOWN barrier 6. Unang tumama: 10 (UP) bago 6.
    assert d.first_passage(np.array([9.0, 9.5, 10.0, 5.0]), 10.0, 6.0) == "UP"
    assert d.first_passage(np.array([7.0, 6.0, 10.0]), 10.0, 6.0) == "DOWN"
    assert d.first_passage(np.array([7.0, 9.0]), 10.0, 6.0) == "NONE"
    # ang binomial na iniulat para sa bar-anchor scorecard (33 tama / 23 mali)
    assert round(d._binom_two_sided(33, 56), 3) == BACKSIDE_BENCH_MEASURED["bar_anchor"]["binom_p"]


# ── (d) ANG SCORING NG BENCH AY SUMUSUNOD ────────────────────────────────────────────
def test_scoring_keeps_the_historical_veto_and_treats_the_new_events_as_receipts():
    from app.services.trading.momentum_neural import ross_bench_scoring as rbs

    assert BENCH_VETO_EVENTS == frozenset({"live_entry_backside_bench_veto"})
    assert "live_entry_backside_bench_conditioned" in BENCH_RECEIPT_EVENTS
    assert "live_entry_backside_benched" in BENCH_RECEIPT_EVENTS
    for t in BENCH_RECEIPT_EVENTS:
        assert rbs._is_decision(t)
        assert not rbs._is_refusal(t)


def _sev(ts: str, t: str, **p) -> dict[str, Any]:
    return {"ts": f"2026-09-11 13:{ts}", "event_type": t, "payload": p}


_ARMED = [
    _sev("00:00", "live_arm_requested"), _sev("00:01", "live_arm_confirmed"),
    _sev("00:02", "live_runner_started"),
]


def test_a_latched_name_whose_break_never_came_is_a_trigger_wait_not_a_bench_veto():
    """Ang latch lamang ay HINDI ebidensya na may trigger na pumutok (ang kahulugan ng
    ``bench_veto`` sa rung 3). Pagkatapos ng [56] ang bawat latched na pangalang hindi pumutok
    ay magiging maling "bench_veto" sa scoreboard kung hindi ito itatama."""
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
    """Ang conditioned event ay sinusundan ng candidate sa live; kahit na walang candidate sa
    isang putol na trace, hindi ito refusal."""
    from app.services.trading.momentum_neural import ross_bench_scoring as rbs

    events = _ARMED + [
        _sev("00:03", "live_entry_backside_benched", reason="benched_backside_below_vwap"),
        _sev("00:04", "live_entry_backside_bench_conditioned", reason="benched_backside_sticky",
             preserved_trigger="abcd_break_tick_ok"),
    ]
    stage = rbs.classify_events(events, source="events")
    assert stage.qualifier != "bench_veto"


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
    assert len(_emit_calls(_fn("_sticky_backside_bench_pass"), "live_entry_backside_bench_conditioned")) == 1


def test_the_call_site_cannot_rewrite_the_trigger():
    """Ang If na tumatawag sa ``_sticky_backside_bench_pass`` sa ``tick_live_session`` ay walang
    assignment sa ``_trigger_ok`` / ``_trigger_reason`` (ang dating ``_trigger_ok = False;
    _trigger_reason = "backside_benched"``)."""
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
    site = min(sites, key=lambda n: len(list(ast.walk(n))))  # ang pinakamaliit na If na may tawag
    for n in ast.walk(site):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            names = {t.id for t in targets for t in ast.walk(t) if isinstance(t, ast.Name)}
            assert not names & {"_trigger_ok", "_trigger_reason"}, ast.unparse(n)
    assert "backside_benched" not in {
        n.value for n in ast.walk(tick) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_the_two_silent_bench_refusals_are_gone():
    """Ang tape-hold early fire at ang momentum-continuation fire ay parehong may
    ``and le.get("benched_backside_hod") is None`` — ang TAHIMIK na kopya ng bench veto (walang
    event, walang resibo). Wala nang If test sa runner na bumabasa ng marker na iyon."""
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


@pytest.mark.parametrize(
    "event_type",
    ["live_entry_filled", "live_entry_tape_hold_fire", "live_entry_momentum_continuation_fire"],
)
def test_entry_payloads_carry_the_bench_receipt(event_type):
    calls = _emit_calls(_fn("tick_live_session"), event_type)
    assert calls, event_type
    for call in calls:
        payload = call.args[3]
        assert isinstance(payload, ast.Dict)
        keys = {k.value for k in payload.keys if isinstance(k, ast.Constant)}
        assert "backside_bench" in keys, event_type
        val = payload.values[[getattr(k, "value", None) for k in payload.keys].index("backside_bench")]
        assert "_BACKSIDE_BENCH_RECEIPT_KEY" in ast.unparse(val)


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
