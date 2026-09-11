"""[63] Ang mga GUARD ng sukat mismo — warm-up, hindi-trade, at ang pinangalanang fill.

Bakit may test ang isang off-line na script: ang UNANG bersyon ng sukat na ito ay nagbigay
ng ``-7.17 R`` na sagot sa operator, at ang 96% niyon ay galing sa ISANG setup na
nagdesisyon sa ika-25 print ng araw — isang scanner na mas bata pa sa sarili nitong
bintana. Sampu pang setup ang binilang na ang ``target`` ay nasa ITAAS ng sariling entry
(hindi trade). Ang mga bilang na nakarating sa planner row at sa design doc ay nagbago ng
TANDA nang idagdag ang mga guard na ito, kaya ang mga guard ay hindi puwedeng maging
komento lamang: sinusukat sila rito.
"""
from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta, timezone

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "short_side_exhaustion_measure_63",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "short_side_exhaustion_measure_63.py",
    ),
)
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)


def _tape(n_up: int = 1600, n_down: int = 1600) -> list[tuple]:
    """Isang spike pataas tapos buong pullback — sapat para magkaroon ng hod/spike_low."""
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    rows: list[tuple] = []
    for i in range(n_up):
        px = 1.00 + 1.00 * (i / max(1, n_up - 1))
        rows.append((t0 + timedelta(milliseconds=100 * len(rows)), px, 100, px - 0.01, px + 0.01))
    top = rows[-1][1]
    for i in range(n_down):
        px = top - 0.90 * (i / max(1, n_down - 1))
        rows.append((t0 + timedelta(milliseconds=100 * len(rows)), px, 100, px - 0.01, px + 0.01))
    return rows


@pytest.fixture
def always_exhausted(monkeypatch):
    """Pilitin ang T2 na pumutok sa BAWAT hakbang para masukat ang GUARD, hindi ang trigger."""
    monkeypatch.setattr(m, "cycle_exhaustion_score", lambda *_a, **_k: (0.99, {}))
    monkeypatch.setattr(
        m,
        "cycle_features_at",
        lambda sc, px: {
            "cycle_index": 1, "in_pullback": True, "pos_in_range": 0.2,
        },
    )
    return monkeypatch


def test_no_decision_before_the_window_the_decision_reads_is_full(always_exhausted):
    """WARM-UP: ang desisyon ay tumitingin sa WINDOW_PRINTS na bintana."""
    setups, _degen = m.walk_day("TEST", "2026-09-03", _tape())

    assert setups, "kailangang may pumutok para may masukat"
    assert min(r["idx"] for r in setups) >= m.WARMUP_PRINTS
    assert m.WARMUP_PRINTS == m.WINDOW_PRINTS


def test_a_setup_whose_target_sits_above_its_entry_is_not_a_trade(always_exhausted):
    """HINDI-TRADE: binibilang, hindi isinasama sa halagang nagdedesisyon.

    Sa ``RETRACE_X = 0`` ang target ay ang spike HIGH mismo, kaya ang BAWAT putok ay
    nasa itaas ng sariling entry — eksaktong hugis ng 10 sa 33 na setup ng unang sukat.
    """
    always_exhausted.setattr(m, "RETRACE_X", 0.0)

    setups, degen = m.walk_day("TEST", "2026-09-03", _tape())

    assert setups == []
    assert degen["exhausted"] > 0


def test_every_reported_setup_has_a_target_below_its_own_entry(always_exhausted):
    setups, _degen = m.walk_day("TEST", "2026-09-03", _tape())

    assert setups
    assert all(r["target"] < r["entry"] for r in setups)
    assert all(r["risk"] > 0 for r in setups)


def test_the_two_fill_conventions_are_named_and_never_worse_than_each_other(always_exhausted):
    """Ang cover sa ASK ay TAKER; ang resting limit ay napupuno sa mismong target.

    Ang limit ay hindi kailanman mas MASAMA kaysa taker sa isang target exit (iyon ang
    buong spread), at sa isang stop/horizon exit ay MAGKAPAREHO sila — ang stop ay isang
    market order, at walang resting limit doon na maipagmamalaki.
    """
    setups, _degen = m.walk_day("TEST", "2026-09-03", _tape())

    assert setups
    for r in setups:
        if r["howA"] == "target":
            assert r["RA_lim"] >= r["RA"]
            assert r["exA_lim"] == pytest.approx(r["target"])
        else:
            assert r["RA_lim"] == pytest.approx(r["RA"])


def test_every_setup_reports_its_bound_in_prints_not_only_in_minutes(always_exhausted):
    """Ang 60-min na horizon ay isang ORASAN; ang hangganan ay iniuulat din sa PRINT."""
    setups, _degen = m.walk_day("TEST", "2026-09-03", _tape())

    assert setups
    assert all(isinstance(r["prints_A"], int) and r["prints_A"] >= 0 for r in setups)
    assert all(isinstance(r["prints_B"], int) and r["prints_B"] >= 0 for r in setups)
    assert m.HORIZON_MIN == 60
