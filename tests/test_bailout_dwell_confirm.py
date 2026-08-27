"""Ang entry-price fast-bailout ang pinakamasamang panuntunan sa bawat table.

NASUKAT (2026-08-27, 1,206 labelled ignition sa 2 araw): 95% ng CONTINUED na
panalo ay nagre-retest sa/mababa sa entry sa loob ng 60s; ang fast-bail na
lumalabas sa unang breach ay pumapatalsik ng 86-96% ng panalo at ito lang ang
uniporme-negatibong panuntunan. Ang pinakamahusay na kombinasyon: 60s na
TULOY-TULOY na dwell sa ilalim ng entry AT lalim >=1% (panalo natatalsik 16.3%
vs pagkabigo 63.0%), may 2% hard backstop para sa rip-then-collapse.

Ang ``_bailout_dwell_confirm_holds`` ay kinokopya ang stop-side flicker-confirm
pattern. ⚠️ IPINADALANG OFF na may nakasulat na flip criterion (utos ng
adversarial audit: i-ship lang kasabay ng conditional admission gate; flip
kapag admitted-set winner rate >= ~25% sa nightly replay).

Runnable: pytest tests/test_bailout_dwell_confirm.py -v
"""
from __future__ import annotations

import ast
import pathlib
from datetime import timedelta

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


class _Sess:
    id = 991


def _le(entry=10.0, pending_at=None, extra=None):
    le = {"position": {"avg_entry_price": entry}}
    if pending_at is not None:
        le["bailout_breach_pending_utc"] = pending_at.isoformat()
        le["bailout_breach_trigger"] = "breakout_failed_to_hold"
    if extra:
        le.update(extra)
    return le


@pytest.fixture
def _wired(monkeypatch):
    """I-on ang flag at i-stub ang db side effects."""
    monkeypatch.setattr(
        settings, "chili_momentum_bailout_dwell_confirm_enabled", True, raising=False)
    emits = []
    monkeypatch.setattr(LR, "_commit_le", lambda sess, le: None, raising=True)
    monkeypatch.setattr(
        LR, "_emit", lambda db, sess, ev, payload: emits.append((ev, payload)),
        raising=True)
    monkeypatch.setattr(
        LR, "_schedule_stop_confirm_dispatch", lambda sid: None, raising=True)
    return emits


def _holds(le, bid):
    return LR._bailout_dwell_confirm_holds(
        object(), _Sess(), le, bid=bid, trigger="breakout_failed_to_hold")


# ── OFF = byte-identical ─────────────────────────────────────────────────────


def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_bailout_dwell_confirm_enabled", False, raising=False)
    assert LR._bailout_dwell_confirm_holds(
        object(), _Sess(), _le(), bid=5.0, trigger="x") is True


def test_the_flag_ships_OFF_with_the_flip_criterion_written():
    """⚠️ Hindi ito 'dark flag na naghihintay ng A/B na di darating' — ang
    nightly replay na sumusukat sa criterion ay GUMAGANA na. Ang criterion ay
    dapat nakasulat sa description."""
    assert settings.chili_momentum_bailout_dwell_confirm_enabled is False
    desc = str(type(settings).model_fields[
        "chili_momentum_bailout_dwell_confirm_enabled"].description or "")
    assert "25%" in desc, "dapat nakasulat ang flip criterion"
    assert "nightly replay" in desc, "dapat nakaturo sa gumaganang evidence machine"


# ── Ang dwell state machine ──────────────────────────────────────────────────


def test_first_breach_arms_a_stamp_and_holds(_wired):
    le = _le(entry=10.0)
    assert _holds(le, bid=9.95) is False, "unang breach = arm, hindi exit"
    assert "bailout_breach_pending_utc" in le
    assert _wired[-1][0] == "bailout_breach_pending_confirm"


def test_a_reclaim_clears_the_stamp(_wired):
    """ANG PANGUNAHING KASO. Ang retest na bumabalik — 95% ng panalo — ay
    naglilinis ng stamp at HINDI lumalabas."""
    le = _le(entry=10.0, pending_at=LR._utcnow() - timedelta(seconds=30))
    assert _holds(le, bid=10.01) is False
    assert "bailout_breach_pending_utc" not in le
    assert _wired[-1][0] == "bailout_breach_flicker_dodged"


def test_dwell_plus_depth_confirms_the_exit(_wired):
    """60s tuloy-tuloy sa ilalim + lalim >=1% = tunay na pagkabigo, labas."""
    le = _le(entry=10.0, pending_at=LR._utcnow() - timedelta(seconds=61))
    assert _holds(le, bid=9.89) is True  # -1.1%


def test_dwell_without_depth_keeps_holding(_wired):
    """Mababaw na hover (-0.5%) kahit matagal = hindi failure signature."""
    le = _le(entry=10.0, pending_at=LR._utcnow() - timedelta(seconds=300))
    assert _holds(le, bid=9.95) is False


def test_depth_without_dwell_keeps_holding(_wired):
    """-1.5% pero 10s pa lang = maaaring winner retest (p25 ng panalo ay
    -1.26%); hintay."""
    le = _le(entry=10.0, pending_at=LR._utcnow() - timedelta(seconds=10))
    assert _holds(le, bid=9.85) is False


def test_the_two_percent_backstop_exits_immediately(_wired):
    """⚠️ ANG RIP-THEN-COLLAPSE BOUND. -2% = labas AGAD kahit walang dwell —
    77 ganitong kaso sa corpus, mean -2.42% sa ilalim ng panuntunan."""
    le = _le(entry=10.0, pending_at=LR._utcnow() - timedelta(seconds=5))
    assert _holds(le, bid=9.79) is True


def test_backstop_fires_even_without_a_stamp(_wired):
    le = _le(entry=10.0)
    assert _holds(le, bid=9.75) is True


# ── Fail direction ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("le,bid", [
    ({"position": {}}, 9.5),                      # walang entry price
    ({"position": {"avg_entry_price": 0}}, 9.5),  # sirang entry
    ({"position": {"avg_entry_price": 10.0}}, None),  # walang bid
])
def test_missing_inputs_fall_back_to_todays_behaviour(_wired, le, bid):
    """⚠️ Ang exit ay HINDI kailanman naha-harang ng sirang datos."""
    assert LR._bailout_dwell_confirm_holds(
        object(), _Sess(), le, bid=bid, trigger="x") is True


def test_a_corrupt_stamp_falls_back_to_todays_behaviour(_wired):
    le = _le(entry=10.0, extra={"bailout_breach_pending_utc": "hindi-petsa"})
    assert _holds(le, bid=9.89) is True


# ── Guard sa mga kapatid na dark flag (audit item 2) ─────────────────────────


_DARK_BAILOUT_SIBLINGS = [
    "chili_momentum_bail_on_no_confirmation_enabled",
    "chili_momentum_instant_bid_below_fill_cut_enabled",
    "chili_momentum_sub5min_scalp_bailout_enabled",
    "chili_momentum_instant_bid_above_fill_confirm_enabled",
]


@pytest.mark.parametrize("flag", _DARK_BAILOUT_SIBLINGS)
def test_the_dark_bailout_siblings_stay_off(flag):
    """⚠️ Utos ng audit: ang apat na dark bailout flag ay dapat manatiling OFF
    habang umiiral ang dwell-confirm — ang kasaysayan ng flipped-default
    (#1024) ay ginagawang hindi mapagkakatiwalaan ang 'currently False' nang
    walang bantay. Kapag sinadyang i-ON ang isa, i-update ang test na ito NANG
    SABAY sa pagsusuri ng interaction nito sa dwell machine."""
    assert getattr(settings, flag) is False, (
        "%s ay naging ON — suriin muna ang interaction sa dwell-confirm" % flag)


# ── Bantay sa istruktura ─────────────────────────────────────────────────────


def test_both_trigger_sites_are_wrapped():
    """Ang dalawang fast-bail trigger (breakout_failed / lost_vwap) ay dapat
    parehong dumadaan sa dwell gate."""
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = 0
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_bailout_dwell_confirm_holds"):
            calls += 1
    assert calls == 2, (
        "inaasahang EKSAKTONG 2 tawag (trigger A at B), nakita: %d" % calls)


def test_the_exit_reason_token_is_unchanged():
    """⚠️ Ang #1199 stop-class semantics ay nakasalalay sa 'bailout' na token —
    ang helper ay hindi dapat gumagawa ng bagong exit reason."""
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_bailout_dwell_confirm_holds")
    body = ast.unparse(fn)
    assert "last_bailout_trigger" not in body, "hindi dapat hinahawakan ang exit token"
    assert "STATE_LIVE_BAILOUT" not in body, "hindi dapat nagta-transition mismo"
