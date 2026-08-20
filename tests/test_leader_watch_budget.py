"""LEADER WATCH BUDGET — ang tunay na dahilan ng YJ miss (2026-08-19).

Hindi ang volume gate at hindi ang `pullback_too_deep` ang nagpalampas sa +$3,000
na curl ni Ross. **Wala tayo doon.**

    08:43:33  nagsimulang bantayan ang YJ
    09:12:37  KINANSELA (matapos mag-fade mula 6.30 pababa sa ~5.7)
    ~09:15    binili ni Ross ang dip sa ~5.5
    09:22-24  bumenta siya sa 6.50 -> +$3,000
    09:21:32  saka pa lang tayo muling nag-arm
    09:26:25  saka pa lang umandar ang runner

Ang focus tilt ay gumagamit ng KAPAREHONG 600s extend cutoff na nakukuha ng bawat
watcher — masyadong maikli para sa stock of the day. Ang aktwal na nagpanatili sa
YJ nang 29 minuto ay ang event-based na "still high-conviction AND still
front-side" na keep, at MALI ang hugis niyon: hinahawakan ang pangalan habang
TUMATAKBO (kung kailan hindi tayo makakapasok dahil extended) at binibitawan sa
sandaling dumip — kung kailan NABUBUO ang entry.

⚠️ Ang watcher ay $0-risk: kumakain ito ng funnel cap, HINDI ng risk budget.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.config import settings


def _keep(started_at, now, *, is_leader, leader_sec, extend_sec=600.0):
    """Ang eksaktong lohika ng leader branch sa _reap_stale_watching_sessions."""
    extend_cutoff = now - timedelta(seconds=extend_sec)
    if not is_leader:
        return False
    cutoff = (
        now - timedelta(seconds=leader_sec) if leader_sec > 0 else extend_cutoff
    )
    return started_at >= cutoff


def test_yj_would_have_been_kept_through_the_curl():
    """ANG TUNAY NA KASO: sa 09:12:37, ang YJ ay 29.1 minuto na. Sa lumang 600s
    na extend ay BITAW; sa bagong 3600s na budget ay HAWAK pa rin — kaya nandoon
    tayo para sa ~09:15 na curl."""
    started = datetime(2026, 8, 19, 8, 43, 33)
    now = datetime(2026, 8, 19, 9, 12, 37)
    assert _keep(started, now, is_leader=True, leader_sec=0.0) is False, "luma"
    assert _keep(started, now, is_leader=True, leader_sec=3600.0) is True, "bago"
    # At buhay pa rin sa mismong sandali ng entry ni Ross at ng exit niya.
    for t in (datetime(2026, 8, 19, 9, 16, 0), datetime(2026, 8, 19, 9, 24, 0)):
        assert _keep(started, t, is_leader=True, leader_sec=3600.0) is True, t


def test_non_leader_is_unaffected():
    """PARITY: ang hindi-leader ay hindi nakikinabang — walang bagong slot leak."""
    started = datetime(2026, 8, 19, 8, 43, 33)
    now = datetime(2026, 8, 19, 9, 12, 37)
    assert _keep(started, now, is_leader=False, leader_sec=3600.0) is False


def test_dead_leader_still_reaps_at_the_ceiling():
    """ANG MAHALAGA: naka-bound pa rin ito. Ang leader na lampas sa budget ay
    kinakansela — hindi ito walang-hanggang slot."""
    started = datetime(2026, 8, 19, 8, 0, 0)
    now = started + timedelta(seconds=3601)
    assert _keep(started, now, is_leader=True, leader_sec=3600.0) is False


def test_zero_restores_legacy_extend_cutoff():
    """Kill-switch: 0 ⇒ ang lumang shared extend cutoff."""
    started = datetime(2026, 8, 19, 9, 0, 0)
    now = started + timedelta(seconds=500)   # loob ng 600s extend
    assert _keep(started, now, is_leader=True, leader_sec=0.0) is True
    now2 = started + timedelta(seconds=700)  # lampas sa 600s extend
    assert _keep(started, now2, is_leader=True, leader_sec=0.0) is False


def test_setting_is_bounded_and_generous_enough_for_the_real_case():
    v = getattr(settings, "chili_momentum_symbol_of_day_leader_watch_seconds", None)
    assert v is not None
    assert 0.0 <= float(v) <= 23400.0
    # Kailangang saklawin ang tunay na 32-minutong agwat ng YJ (1920s).
    assert float(v) >= 1920.0, float(v)
