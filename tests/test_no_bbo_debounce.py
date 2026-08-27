"""Ang pre-entry no_bbo decline ay debounced na (2026-08-27).

ANG SINUKAT: sa tape freeze ngayong hapon, 28 TUNAY na pangalan ang
na-terminal nang INSTANT sa kanilang unang quoteless tick (ang komento mismo
ay nagsasabing para sa "persistently quoteless" ang check), tapos muling
in-arm — puro churn na nagpabagal pa sa drain. Ang "persistent" ay
nangangailangan ng pagpapatuloy: N sunud-sunod na quoteless tick na ngayon.

Runnable: pytest tests/test_no_bbo_debounce.py -v
"""
from __future__ import annotations

import pathlib

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


def test_the_flag_ships_at_3_with_the_incident_recorded():
    assert int(settings.chili_momentum_no_bbo_decline_consecutive_ticks) == 3
    desc = str(type(settings).model_fields[
        "chili_momentum_no_bbo_decline_consecutive_ticks"].description or "")
    assert "28" in desc and "2026-08-27" in desc
    assert "reset" in desc.lower()


def test_the_terminal_needs_consecutive_ticks_not_the_first():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('_decline_terminal(db, sess, reason="no_bbo")')
    region = src[max(0, i - 2000):i]
    assert "no_bbo_consecutive_ticks" in region, (
        "ang terminal ay dapat nasa likod ng counter"
    )
    assert "chili_momentum_no_bbo_decline_consecutive_ticks" in region
    assert "_nb_seen >= max(1, _nb_need)" in region, (
        "ang decline ay dapat kondisyonal sa threshold"
    )


def test_the_counter_is_persisted_before_the_decline():
    """⚠️ Ang counter ay dapat naka-commit sa le BAGO ang posibleng terminal —
    kung hindi, ang restart sa pagitan ay magre-reset ng bilang nang tahimik."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('le["no_bbo_consecutive_ticks"] = _nb_seen')
    region = src[i:i + 300]
    assert "_commit_le(sess, le)" in region


def test_a_good_quote_resets_the_counter():
    """Ang "persistent" ay SUNUD-SUNOD — isang magandang quote ay nagre-reset."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('if le.get("no_bbo_consecutive_ticks"):')
    region = src[i:i + 400]
    assert 'le.pop("no_bbo_consecutive_ticks", None)' in region
    # ang reset ay dapat PAGKATAPOS ng quoteless early-return (may quote na)
    i_return = src.index('_quote_reason if _held_execution_bbo is not None else "no_quote"')
    assert i > i_return, "ang reset ay dapat sa may-quote na landas"


def test_one_restores_legacy_instant_decline():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("chili_momentum_no_bbo_decline_consecutive_ticks")
    region = src[i:i + 300]
    assert "3" in region, "default 3 sa code fallback"
    # 1 => _nb_seen(1) >= max(1,1) => instant — walang hiwalay na branch na kailangan
