"""SUB-$1 PAPER LANE — utos ng operator, 2026-08-28.

Tatlong sub-dollar monster sa iisang araw (FNGR +242%, CHAI +29.5%, DUO +27.4%)
ang ganap na invisible dahil sa multi-layer na $1 floor. Ang lane na ito ay
nagbubukas ng SELECTION (ignite predicate, velocity intake) at ng PAPER shadow
arms para sa sub-dollar — habang ang LIVE slot ay nakakandado pa rin:
1. `auto_arm._live_armable` ay tumatanggi sa _subdollar_paper_only names
2. `ross_smallcap_profile_evidence` ay HINDI ginalaw — sub-$1 ay bagsak pa rin
   doon (`ross_universe_price_below_profile`), na siyang senyales ng
   paper-only pass-through sa eligible loop

Runnable: pytest tests/test_subdollar_paper_lane.py -v
"""
from __future__ import annotations


def _flag(monkeypatch, on: bool):
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_subdollar_paper_enabled", on, raising=False
    )


# ─────────────── ignite predicate: selection bumubukas, max hindi ───────────────


def test_subdollar_ignites_when_flag_on(monkeypatch):
    from app.services.trading.momentum_neural.nbbo_tape import (
        _ross_threshold_crossed,
    )

    _flag(monkeypatch, True)
    # CHAI-class: +29.5% move sa $0.48 — dating tahimik na tanggi sa band
    assert _ross_threshold_crossed(
        "CHAI", move_pct=29.5, gap_pct=29.5, price=0.48
    )


def test_subdollar_still_refused_when_flag_off(monkeypatch):
    from app.services.trading.momentum_neural.nbbo_tape import (
        _ross_threshold_crossed,
    )

    _flag(monkeypatch, False)
    assert not _ross_threshold_crossed(
        "CHAI", move_pct=29.5, gap_pct=29.5, price=0.48
    )


def test_above_band_max_never_opens(monkeypatch):
    """Ang paper flag ay ang MIN side lamang — ang >$20 ay sarado pa rin."""
    from app.services.trading.momentum_neural.nbbo_tape import (
        _ross_threshold_crossed,
    )

    _flag(monkeypatch, True)
    assert not _ross_threshold_crossed(
        "BIGG", move_pct=29.5, gap_pct=29.5, price=45.0
    )


def test_subdollar_still_needs_a_real_axis(monkeypatch):
    """Ang pagbubukas ng band ay HINDI pagluluwag ng explosiveness floors."""
    from app.services.trading.momentum_neural.nbbo_tape import (
        _ross_threshold_crossed,
    )

    _flag(monkeypatch, True)
    assert not _ross_threshold_crossed(
        "DEAD", move_pct=2.0, gap_pct=2.0, price=0.48
    )


# ─────────────── ang LIVE proteksyon: evidence gate hindi ginalaw ───────────────


def test_live_arm_evidence_gate_still_refuses_subdollar(monkeypatch):
    """KRITIKAL: ang ross_smallcap_profile_evidence ay dapat TUMANGGI pa rin sa
    sub-$1 (ross_universe_price_below_profile) — ito ang senyales na ginagamit
    ng auto_arm para sa paper-only pass-through, at ito rin ang huling pader
    ng LIVE slot."""
    from app.services.trading.momentum_neural.universe import (
        ross_smallcap_profile_evidence,
    )

    _flag(monkeypatch, True)
    sig = {"ticker": "CHAI", "price": 0.48, "volume": 5_000_000,
           "todays_change_perc": 29.5}
    ok, reason, _ = ross_smallcap_profile_evidence("CHAI", signal=sig)
    assert not ok
    assert reason == "ross_universe_price_below_profile"
