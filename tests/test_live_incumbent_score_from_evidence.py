"""Ang lakas ng incumbent ay galing sa ebidensya, hindi sa isang constant (2026-08-25).

ANG ASIMETRIYA. Sa IISANG file, ang open-trade na conflict ay kumukwenta ng lakas ng
incumbent nito mula sa ebidensya::

    incumbent_score = confidence*0.6 + _pattern_win_rate_score(incumbent)*0.4

habang ang live-session na conflict ay nag-hardcode::

    "incumbent_score": 0.55

ANG BUNGA, AT ITO ANG PUNTO. Ang gate ay::

    threshold = best_incumbent (0.55) + margin (0.08) = 0.63
    kandidato = research*0.36 + drift*0.18 + exec*0.18 + venue*0.14 + heat*0.14

Sa pang-araw-araw na kaso ay **neutral ang apat sa limang input** ng kandidato at
naka-sahig ang panlima::

    research_quality  0.5   (ang `if research_quality > 0 else 0.5` na fallback)
    live_drift_score  0.5   (_tier_score: walang tier => return 0.5)
    execution_score   0.5   (pareho)
    venue_score       0.5   (_venue_readiness_score: hindi konektado ang Robinhood)
    portfolio_heat    0.2   (ANG SAHIG -- 194 na multong live_arm_expired)

    0.5*0.36 + 0.5*0.18 + 0.5*0.18 + 0.5*0.14 + 0.2*0.14 = 0.4580

Iyon ang EKSAKTONG constant na lumitaw sa lahat ng **391** packet, live at paper.
Laban sa 0.63 na hindi maaabot, ang gate ay **HINDI MAPAPANALUNAN SA PAGKAKABUO**:
bawat same-ticker na conflict ay humaharang, at ang paghahambing sa margin ay
walang bisa.

⚠️ WALANG EBIDENSYA => 0.55, ang dating halaga. Ang pagbabago ay PAGPAPALIT LAMANG
ng constant kung saan may TUNAY na ebidensya.

⚠️ NAKA-OFF ANG GUARD ngayon (``brain_allocator_live_hard_block_enabled=False``),
kaya walang perang nawawala -- landmine ito, hindi bumbero. Ang tinatanggal nito ay
ang landmine.

Runnable: pytest tests/test_live_incumbent_score_from_evidence.py -v
"""
from __future__ import annotations

import inspect

import pytest

from app.services.trading.portfolio_allocator import (
    _LIVE_SESSION_INCUMBENT_SCORE_WITHOUT_EVIDENCE,
    _candidate_score,
    _collect_live_session_conflicts,
    _live_session_incumbent_score,
)


class _Pattern:
    def __init__(self, confidence=None, oos_win_rate=None, win_rate=None):
        self.confidence = confidence
        self.oos_win_rate = oos_win_rate
        self.win_rate = win_rate


def test_no_evidence_keeps_the_previous_constant():
    """⚠️ WALANG REGRESSION. Ang session na walang pattern ay eksaktong gaya ng dati."""
    assert _live_session_incumbent_score(None) == 0.55
    assert _LIVE_SESSION_INCUMBENT_SCORE_WITHOUT_EVIDENCE == 0.55


def test_a_WEAK_incumbent_now_scores_below_the_old_constant():
    """ANG PUNTO. Ang mahinang incumbent ay hindi dapat magtaglay ng parehong
    lakas ng malakas -- iyon ang ginagawa ng isang constant."""
    weak = _live_session_incumbent_score(_Pattern(confidence=0.1, oos_win_rate=0.2))
    assert weak < 0.55, f"ang mahinang incumbent ay dapat mas mababa sa base, nakuha {weak}"


def test_a_STRONG_incumbent_scores_above_the_old_constant():
    strong = _live_session_incumbent_score(_Pattern(confidence=0.95, oos_win_rate=0.8))
    assert strong > 0.55, f"ang malakas na incumbent ay dapat mas mataas, nakuha {strong}"


def test_it_uses_the_SAME_formula_as_the_open_trade_path():
    """⚠️ ANG UGAT NG DEPEKTO ay dalawang daan na naghiwalay. Ang pagpapanatili
    sa kanilang magkatugma ang pumipigil sa muling paghihiwalay."""
    src = inspect.getsource(_live_session_incumbent_score)
    assert "* 0.6" in src and "* 0.4" in src, "dapat kaparehong timbang ng open-trade"
    assert "_pattern_win_rate_score" in src


def test_a_weak_incumbent_becomes_beatable():
    """ANG TUNAY NA PAKINABANG. Sa isang mahinang incumbent, ang isang kandidatong
    may TUNAY na ebidensya ay dapat na ngayong makalampas sa margin -- imposible
    iyon noon laban sa isang nakapirming 0.55."""
    weak = _live_session_incumbent_score(_Pattern(confidence=0.05, oos_win_rate=0.1))
    good_candidate = _candidate_score(
        research_quality=0.8, live_drift_score=0.8, execution_score=0.8,
        venue_score=0.8, portfolio_heat_score=0.52,
    )
    assert good_candidate > weak + 0.08, (
        f"ang mahusay na kandidato ({good_candidate}) ay dapat matalo ang mahinang "
        f"incumbent ({weak}) na may 0.08 na margin"
    )


def test_the_measured_constant_reproduces_exactly():
    """⚠️ ANG BUONG DIAGNOSIS SA ISANG ASERSIYON. Kung magbago ang aritmetika ay
    dapat ding magbago ang paliwanag sa itaas."""
    assert _candidate_score(
        research_quality=0.5, live_drift_score=0.5, execution_score=0.5,
        venue_score=0.5, portfolio_heat_score=0.2,
    ) == pytest.approx(0.4580)


def test_the_old_pairing_was_unwinnable():
    """At ito ang dahilan kung bakit ito mahalaga: ang lumang score laban sa lumang
    threshold ay hindi kailanman makakadaan."""
    old_score = _candidate_score(
        research_quality=0.5, live_drift_score=0.5, execution_score=0.5,
        venue_score=0.5, portfolio_heat_score=0.2,
    )
    assert old_score <= 0.55 + 0.08, "napatunayan: ang lumang pares ay hindi mapapanalunan"


def test_the_collector_still_returns_a_float_score():
    """Ang hugis ng kontrata ay hindi nagbabago -- ang `max()` sa itaas nito ay
    umaasa sa isang numero."""
    class _S:
        def __init__(self):
            self.id = 1
            self.symbol = "AAPL"
            self.variant_id = None
            self.state = "watching_live"

    class _Q:
        def __init__(self, rows):
            self._rows = rows
        def filter(self, *a, **k):
            return self
        def all(self):
            return self._rows

    class _Db:
        def __init__(self, rows):
            self._rows = rows
            self._n = 0
        def query(self, *a, **k):
            self._n += 1
            return _Q(self._rows if self._n == 1 else [])

    out = _collect_live_session_conflicts(
        _Db([_S()]), user_id=1, symbol="AAPL", sector="equity",
        correlation_bucket="b", hypothesis_family=None,
    )
    assert out and isinstance(out[0]["incumbent_score"], float)
    assert out[0]["incumbent_score"] == 0.55, "walang variant => no-evidence na base"
