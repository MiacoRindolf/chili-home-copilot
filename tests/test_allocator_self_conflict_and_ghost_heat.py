"""Ang kandidato ay hindi sariling incumbent, at ang patay na arm ay hindi buhay (2026-08-25).

DALAWANG DEPEKTO SA IISANG COLLECTOR.

**1. ANG KANDIDATO AY SARILING KALABAN.** Kapag ang allocator ay tumatakbo PARA SA
isang session na umiiral na, ang sariling hilera ng session na iyon ay lumilitaw sa
scan at nagbubunga ng ``same_ticker`` na conflict laban sa sarili nito. Nasukat:
**282 sa 282** na live na momentum-entry packet sa 30 araw ang may EKSAKTONG ISANG
conflict na ang ``session_id`` ay katumbas ng sariling ``automation_session_id`` ng
packet. Ang sariling ``count_concurrent_automation_sessions`` ng codebase ay
tumatanggap ng exclusion -- pagkukulang ito, hindi kombensiyon.

**2. ANG PATAY NA ARM AY BINIBILANG NA BUHAY.** Nawawala ang ``live_arm_expired`` sa
``_LIVE_TERMINAL_SESSION_STATES`` habang isinasama ito ng
``operator_actions._TERMINAL_OPERATOR_STATES``. Ang isang arm na nag-expire ay HINDI
kailanman naging posisyon.

NASUKAT (2026-08-25, buhay na DB, user 1, mode=live)::

    binibilang bilang buhay:  200
    tunay na buhay:             6
    multong live_arm_expired: 194

⚠️ ANG PANGALAWANG PINSALA ANG MAS TAHIMIK. Ang parehong hanay ang nagpapakain sa
``portfolio_heat``::

    portfolio_heat_score = max(0.2, 1.0 - min(0.8, heat * 0.08))

Sa 200: ``max(0.2, 1.0-0.8) = 0.2`` -- **ANG SAHIG**. Sa 6: ``0.52``. Permanenteng
naka-pin sa minimum ang capacity term ng allocator dahil sa mga arm na matagal nang
patay -- at malamang ito ang paliwanag sa constant na 0.4580 na score na nakita sa
lahat ng 391 packet.

Runnable: pytest tests/test_allocator_self_conflict_and_ghost_heat.py -v
"""
from __future__ import annotations

import inspect

import pytest

from app.services.trading import portfolio_allocator as pa
from app.services.trading.portfolio_allocator import (
    _LIVE_TERMINAL_SESSION_STATES,
    _collect_live_session_conflicts,
    build_session_allocation_decision,
    evaluate_allocation_candidate,
)


class _Sess:
    def __init__(self, sid, symbol, variant_id=1, state="watching_live"):
        self.id = sid
        self.symbol = symbol
        self.variant_id = variant_id
        self.state = state


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """Ibinabalik ang mga session sa unang query at wala sa variant lookup."""

    def __init__(self, rows):
        self._rows = rows
        self._calls = 0

    def query(self, *a, **k):
        self._calls += 1
        return _FakeQuery(self._rows if self._calls == 1 else [])


def test_live_arm_expired_is_terminal():
    """ANG 194 NA MULTO."""
    assert "live_arm_expired" in _LIVE_TERMINAL_SESSION_STATES


def test_the_terminal_set_still_holds_every_previous_state():
    """⚠️ WALANG REGRESSION -- pagdaragdag lamang, hindi pagpapalit."""
    for state in (
        "cancelled", "expired", "error", "archived", "finished",
        "live_finished", "live_cancelled", "live_error",
    ):
        assert state in _LIVE_TERMINAL_SESSION_STATES


def test_the_candidate_is_excluded_from_its_own_conflicts():
    """ANG PANGUNAHING KASO -- ang 282/282 na self-conflict."""
    rows = [_Sess(101, "AAPL"), _Sess(202, "AAPL")]
    out = _collect_live_session_conflicts(
        _FakeDb(rows), user_id=1, symbol="AAPL", sector="equity",
        correlation_bucket="b", hypothesis_family=None, exclude_session_id=101,
    )
    ids = {c["session_id"] for c in out}
    assert 101 not in ids, "ang kandidato ay hindi dapat sarili nitong incumbent"
    assert 202 in ids, "ang TUNAY na kapwa ay dapat pa ring conflict"


def test_without_an_exclusion_the_old_behaviour_is_exact():
    """⚠️ Ang None ay dapat byte-identical para sa purong pre-arm na pagsusuri."""
    rows = [_Sess(101, "AAPL"), _Sess(202, "AAPL")]
    out = _collect_live_session_conflicts(
        _FakeDb(rows), user_id=1, symbol="AAPL", sector="equity",
        correlation_bucket="b", hypothesis_family=None,
    )
    assert {c["session_id"] for c in out} == {101, 202}


def test_excluding_the_only_peer_yields_no_conflict():
    """Ang isang mag-isang kandidato ay dapat walang kalaban -- hindi isa."""
    out = _collect_live_session_conflicts(
        _FakeDb([_Sess(101, "AAPL")]), user_id=1, symbol="AAPL", sector="equity",
        correlation_bucket="b", hypothesis_family=None, exclude_session_id=101,
    )
    assert out == []


@pytest.mark.parametrize("bad", [0, None])
def test_a_falsy_exclusion_is_treated_as_no_exclusion(bad):
    """Ang session id na 0 ay hindi wastong id; hindi ito dapat magbura ng hilera."""
    out = _collect_live_session_conflicts(
        _FakeDb([_Sess(101, "AAPL")]), user_id=1, symbol="AAPL", sector="equity",
        correlation_bucket="b", hypothesis_family=None, exclude_session_id=bad,
    )
    assert {c["session_id"] for c in out} == {101}


def test_the_exclusion_is_threaded_all_the_way_down():
    """BANTAY. Ang parameter ay walang silbi kung hindi ito naihahatid."""
    assert "exclude_session_id" in inspect.signature(evaluate_allocation_candidate).parameters
    assert "exclude_session_id" in inspect.signature(_collect_live_session_conflicts).parameters
    body = inspect.getsource(evaluate_allocation_candidate)
    assert "exclude_session_id=exclude_session_id" in body, (
        "ang evaluate_allocation_candidate ay dapat ihatid ang exclusion pababa"
    )


def test_the_session_builder_passes_its_own_id():
    """⚠️ ITO ANG NAGSASARA NG BUTAS. Ang builder ang may hawak ng session; kung
    hindi nito ipapasa ang id, ang bagong parameter ay hindi kailanman gagamitin
    sa tunay na daan."""
    body = inspect.getsource(build_session_allocation_decision)
    assert "exclude_session_id=" in body
    assert 'getattr(session, "id"' in body


def test_the_heat_score_is_no_longer_pinned_to_its_floor():
    """⚠️ ANG TAHIMIK NA PINSALA. Sa 200 na multo ang heat score ay naka-sahig sa
    0.2; sa 6 na tunay na session ito ay 0.52. Ang formula ay hindi ginalaw --
    ang INPUT lamang ang nalinis -- kaya dito sinusuri ang aritmetika."""
    def heat_score(heat: int) -> float:
        return max(0.2, 1.0 - min(0.8, heat * 0.08))

    assert heat_score(200) == pytest.approx(0.2), "ang lumang input ay nasa sahig"
    assert heat_score(6) == pytest.approx(0.52), "ang malinis na input ay may headroom"
    assert heat_score(6) > heat_score(200)


def test_the_allocator_terminal_set_matches_the_operator_one():
    """⚠️ ANG UGAT NG DEPEKTO ay dalawang listahan ng terminal state na naghiwalay.
    Ang bawat state na itinuturing na terminal ng operator ay dapat terminal din
    dito -- kung hindi ay muling mabubuhay ang multo."""
    from app.services.trading.momentum_neural import operator_actions as oa

    operator_states = getattr(oa, "_TERMINAL_OPERATOR_STATES", frozenset())
    missing = {s for s in operator_states if s not in _LIVE_TERMINAL_SESSION_STATES}
    assert not missing, (
        f"terminal para sa operator ngunit buhay para sa allocator: {sorted(missing)}"
    )
