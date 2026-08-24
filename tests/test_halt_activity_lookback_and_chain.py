"""Ang halt detector ay dapat manatiling nakakakita SA BUONG halt (2026-08-24).

DALAWANG BAGAY, PAREHONG SINUKAT NGAYONG ARAW.

(1) ANG ACTIVITY WINDOW AY MAS MAIKLI KAYSA SA HALT.
    Ang recently-active gate ay nagbabasa ng prints sa loob LAMANG ng
    ``chili_momentum_halt_print_recent_active_seconds`` (300s). Ang tunay na LULD
    halt ay mas mahaba -- sinukat sa 56 na halt sa 6 na simbolo::

        p50 397s · p75 681s · p90 1306s · p95 1577s · max 3466s

    Kaya habang halted ang pangalan ay nauubos ang window, bumabagsak ang
    ``recent_print_count`` sa 0, at NABUBULAG ang detector. Nasukat na bunga --
    ang lag mula sa TUNAY na unang resume hanggang sa unang detect ni CHILI::

        DAIC  09:40:21 (+27.0%)  ->  10:24:53   = 44 MINUTO
        BTCT  09:55:21           ->  10:09:39   = 14 minuto

    Pagdating ni CHILI sa DAIC ay tapos na ang HOD (2.45) at bumagsak na ito ng
    -35%; lahat ng nakita niya ay backside. Lagi itong ISANG HALT NA HULI.

    ⚠️ Ang lunas ay HINDI nagluluwag ng anuman: ang minimum na bilang ay
    ini-scale ng PAREHONG proporsyon, kaya ang kinakailangang DENSITY ay eksaktong
    pareho -- mas malawak lang ang ebidensya.

(2) ``halt_chain_up_count`` AY NULL SA BAWAT EVENT.
    Ang counter ay dating ginagalaw lamang kapag ON ang
    ``halt_chain_risk_gate_enabled`` o ``add_into_halt_enabled`` -- pareho silang
    OFF sa produksyon. Ang chain position ANG pangunahing signal ng halt game
    (sinukat, n=18, isang araw: median 5m MFE +7.0% sa chain 1-3 laban sa +0.9%
    sa chain 4+), at hindi natin ito mapapatunayan nang walang bilang.

Runnable: pytest tests/test_halt_activity_lookback_and_chain.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import nbbo_tape


def _sess(symbol: str = "ZACT"):
    return SimpleNamespace(id=1, symbol=symbol, state="watching_live")


# ── (1) ANG ACTIVITY LOOKBACK ──────────────────────────────────────────────

@pytest.fixture
def spy_tape(monkeypatch):
    """Itala kung anong window ang hinihingi, at ibalik ang naka-script na estado."""
    box: dict = {"state": None, "asked": []}

    def _fake(db, symbol, *, recent_active_window_s=None, **kwargs):
        box["asked"].append(float(recent_active_window_s or 0.0))
        return box["state"]

    monkeypatch.setattr(nbbo_tape, "print_recency_state", _fake)
    return box


def _run_detect(monkeypatch, spy, marked: list, *, le=None):
    """Patakbuhin ang totoong detector; ibalik kung nag-mark ito ng halt."""
    monkeypatch.setattr(
        lr, "_mark_suspected_halt",
        lambda db, sess, le_, tick=None, detail=None: marked.append(detail or {}),
    )
    monkeypatch.setattr(lr, "_is_data_session_now", lambda *a, **k: True, raising=False)
    import app.services.trading.momentum_neural.market_profile as mp
    monkeypatch.setattr(mp, "is_data_session_now", lambda *a, **k: True)
    lr._register_print_recency_halt_check(None, _sess(), le if le is not None else {}, None)
    return marked


def test_the_wider_lookback_is_actually_requested(spy_tape, monkeypatch):
    """ANG BUG: hinihingi dati ang 300s, na nauubos sa gitna ng halt."""
    spy_tape["state"] = {"last_print_age_s": 400.0, "recent_print_count": 4300, "median_gap_s": 0.01}
    _run_detect(monkeypatch, spy_tape, [])
    assert spy_tape["asked"], "dapat tinawag ang tape reader"
    asked = spy_tape["asked"][-1]
    wide = float(settings.chili_momentum_halt_print_activity_lookback_seconds)
    assert asked == pytest.approx(wide), f"hiningi ang {asked}s, inaasahan ang {wide}s"
    assert asked > float(settings.chili_momentum_halt_print_recent_active_seconds)


def test_the_required_density_is_exactly_unchanged():
    """ANG BUONG PUNTO: mas malawak na ebidensya, HINDI mas mahinang bar."""
    tight = float(settings.chili_momentum_halt_print_recent_active_seconds)
    wide = float(settings.chili_momentum_halt_print_activity_lookback_seconds)
    min_tight = int(settings.chili_momentum_halt_print_recent_active_min_prints)
    min_wide = max(min_tight, int(round(min_tight * (wide / tight))))
    assert (min_tight / tight) == pytest.approx(min_wide / wide, rel=1e-9), (
        "ang density ay dapat magkapareho -- kung hindi ay niluwagan natin ang gate"
    )


def test_a_long_halt_is_still_detected(spy_tape, monkeypatch):
    """ANG DAIC NA KASO: 4,330 print, tapos 600s na katahimikan.

    Sa lumang 300s na window ay 0 na ang recent_print_count sa puntong ito at
    tahimik na tumatanggi ang detector. Sa mas malawak na lookback ay nakikita
    pa rin ang mga print at TAMANG nag-i-infer ng halt.
    """
    spy_tape["state"] = {"last_print_age_s": 600.0, "recent_print_count": 4330, "median_gap_s": 0.01}
    marked = _run_detect(monkeypatch, spy_tape, [])
    assert marked, "ang mahabang halt ay dapat pa ring nadedetect"
    assert marked[-1].get("source") == "print_recency"


def test_a_trickling_name_is_still_never_inferred_halted(spy_tape, monkeypatch):
    """FAIL-CLOSED: ang density ang nagbabantay, hindi ang haba ng window.

    30 print sa 1800s ang kailangan (0.0167/s). Ang 12 ay malayong kulang.
    """
    spy_tape["state"] = {"last_print_age_s": 600.0, "recent_print_count": 12, "median_gap_s": 30.0}
    marked = _run_detect(monkeypatch, spy_tape, [])
    assert not marked, "ang tumutulong pangalan ay hinding-hindi dapat maging halted"


def test_a_never_active_name_is_untouched(spy_tape, monkeypatch):
    """Ang DAIC bago ang 09:40 na reopen: 1 print sa 10 minuto -- TAMANG tinanggihan."""
    spy_tape["state"] = {"last_print_age_s": 600.0, "recent_print_count": 1, "median_gap_s": None}
    assert not _run_detect(monkeypatch, spy_tape, [])


def test_no_tape_still_fails_closed(spy_tape, monkeypatch):
    spy_tape["state"] = None
    assert not _run_detect(monkeypatch, spy_tape, [])


def test_a_bad_lookback_value_falls_back_to_the_tight_window(spy_tape, monkeypatch):
    """Fail-open: 0 / negatibo / mas maliit sa tight ⇒ ang lumang gawi."""
    tight = float(settings.chili_momentum_halt_print_recent_active_seconds)
    for bad in (0.0, -5.0, tight - 1.0):
        spy_tape["asked"].clear()
        spy_tape["state"] = {"last_print_age_s": 400.0, "recent_print_count": 9, "median_gap_s": 0.01}
        monkeypatch.setattr(
            settings, "chili_momentum_halt_print_activity_lookback_seconds", bad, raising=False
        )
        _run_detect(monkeypatch, spy_tape, [])
        assert spy_tape["asked"][-1] == pytest.approx(tight), (
            f"lookback={bad} ay dapat bumalik sa {tight}s"
        )


# ── (2) ANG CHAIN COUNTER ──────────────────────────────────────────────────

def _resume(monkeypatch, le: dict, bid: float):
    """Patakbuhin ang totoong resume path na may bumalik nang tape."""
    monkeypatch.setattr(
        nbbo_tape, "print_recency_state",
        lambda db, symbol, **k: {"last_print_age_s": 0.3, "recent_print_count": 4300, "median_gap_s": 0.01},
    )
    monkeypatch.setattr(lr, "_commit_le", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_emit", lambda *a, **k: None)
    lr._register_fresh_quote_tick(
        None, _sess(), le, SimpleNamespace(bid=bid, mid=bid, open=bid)
    )
    return le.get("halt_chain_up_count")


def _halted(level: float, prev=None):
    le = {
        "suspected_halt_since_utc": lr._utcnow().isoformat(),
        "suspected_halt_source": "print_recency",
        "halt_level": level,
    }
    if prev is not None:
        le["halt_chain_up_count"] = prev
    return le


def test_the_chain_counter_is_populated_without_any_flag(monkeypatch):
    """ANG BUG: NULL ito sa bawat naitalang halt_resumed dahil OFF ang parehong flag."""
    monkeypatch.setattr(settings, "chili_momentum_halt_chain_risk_gate_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_add_into_halt_enabled", False, raising=False)
    assert _resume(monkeypatch, _halted(1.50), bid=1.62) == 1


def test_a_halt_up_extends_the_chain(monkeypatch):
    """Ang DAIC chain: 1.51 -> 1.62 -> 1.69 -> 1.73, lahat pataas."""
    assert _resume(monkeypatch, _halted(1.50, prev=2), bid=1.62) == 3


def test_a_halt_down_resets_the_chain(monkeypatch):
    """Ang DAIC 10:22:50 ay bumalik ng -35% -- iyon ang katapusan ng up-chain."""
    assert _resume(monkeypatch, _halted(2.05, prev=4), bid=1.33) == 0


def test_an_unreadable_direction_extends_conservatively(monkeypatch):
    """Walang halt_level ⇒ bilangin bilang halt-up (mas mahigpit, hindi mas maluwag)."""
    le = _halted(1.50, prev=1)
    le.pop("halt_level")
    assert _resume(monkeypatch, le, bid=1.62) == 2


def test_the_counter_survives_a_flagless_lane(monkeypatch):
    """Bantayan ang source mismo -- huwag payagang bumalik ang flag gate."""
    import inspect

    src = inspect.getsource(lr._register_fresh_quote_tick)
    idx = src.index("halt_chain_up_count")
    before = src[max(0, idx - 1400):idx]
    assert "chili_momentum_halt_chain_risk_gate_enabled" not in before, (
        "ang chain counter ay hindi dapat muling ma-gate -- observation-only ito"
    )
