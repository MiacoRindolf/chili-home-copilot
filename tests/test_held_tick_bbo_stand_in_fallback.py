"""Ang HELD-tick BBO ay dapat may paningin sa premarket — pero mahigpit muna.

ANG PANGYAYARI (2026-08-25, buhay na trade). Ang `_live_tick_bbo` ay nagruruta ng
alpaca_spot + held-state na tick sa `_final_entry_bbo` NANG WALANG
`allow_stand_in`. Walang premarket book ang Alpaca, kaya:

    live_held_execution_bbo_blocked : 1,625 event, bawat tick pagkatapos ng fill
    age_seconds                     : 47,021 -> 51,737
    provider_event_at_utc           : nakapirmi sa close ng nakaraang araw

Ang `tick_live_session` ay bumabalik sa `live_runner.py:27918` -- BAGO ang HWM
ratchet, bago ang held-state branch, bago ang exit ladder. Bunga: nanatiling
1.51 ang `high_water_mark` habang umabot ang bid sa 1.71 (+4.0R), kaya
`peak_r = 0.0` magpakailanman at ZERO exit event sa buong 75-minutong hold.

⚠️ HINDI KAWALAN NG DATOS ITO. Sa mismong sandali ng unang harang, ang kaparehong
DB ay may hawak na row sa `iqfeed_depth_snapshots` na 7.1 SEGUNDO lamang ang
edad. Source-routing artifact.

⚠️ ANG DISENYO: MAHIGPIT MUNA. Kapag may buhay na direktang book -- ibig sabihin
RTH -- iyon ang mananalo at WALANG NAGBABAGO. Ang stand-in ay ginagamit LAMANG
kapag walang naibigay ang mahigpit na landas. Kaya hindi ito nagluluwag ng anuman
sa regular session; binibigyan lang nito ng paningin ang lane kapag bulag ito.

Runnable: pytest tests/test_held_tick_bbo_stand_in_fallback.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import live_runner as LR


class _Tick:
    def __init__(self, bid, freshness="fresh"):
        self.bid = bid
        self.freshness = freshness


@pytest.fixture
def calls(monkeypatch):
    """Kunin ang bawat tawag sa _final_entry_bbo kasama ang kwargs nito."""
    seen = []

    def _fake(adapter, product_id, **kw):
        seen.append(kw)
        # unang tawag = mahigpit; ibalik ang naka-program na resulta
        return _fake.results.pop(0)

    _fake.results = []
    monkeypatch.setattr(LR, "_final_entry_bbo", _fake)
    return seen, _fake


def test_strict_wins_and_stand_in_is_never_reached_when_the_book_is_alive(calls):
    """⚠️ ANG PANGUNAHING BANTAY SA KALIGTASAN. Sa RTH ay may direktang book, at
    kapag nagbalik iyon ng tick ay HINDI dapat humingi ng stand-in. Ito ang
    nagpapanatiling walang pagbabago sa regular session."""
    seen, fake = calls
    fake.results = [(_Tick(10.0), {"ok": True})]
    tick, freshness, snap = LR._live_tick_bbo(
        object(), "AAPL", execution_family="alpaca_spot", state="live_entered")
    assert tick is not None and tick.bid == 10.0
    assert len(seen) == 1, "isang tawag lamang -- hindi umabot sa stand-in"
    assert seen[0].get("allow_stand_in") in (None, False)


def test_stand_in_is_used_only_when_strict_returns_nothing(calls):
    """Ang premarket na kaso: patay ang direktang book, kaya kailangan ng
    pangalawang tanong -- kung hindi ay bulag ang lane sa sariling posisyon."""
    seen, fake = calls
    fake.results = [(None, {"reason": "execution_bbo_unavailable"}),
                    (_Tick(1.71), {"ok": True, "quote_authority": "stand_in"})]
    tick, freshness, snap = LR._live_tick_bbo(
        object(), "BDRX", execution_family="alpaca_spot", state="live_entered")
    assert tick is not None and tick.bid == 1.71, "dapat may paningin na sa premarket"
    assert len(seen) == 2, "mahigpit muna, tapos stand-in"
    assert seen[0].get("allow_stand_in") in (None, False)
    assert seen[1].get("allow_stand_in") is True
    assert seen[1].get("stand_in_max_age_seconds") == pytest.approx(15.0)


def test_a_dead_book_with_no_stand_in_still_returns_none(calls):
    """⚠️ FAIL-CLOSED pa rin. Kung wala kahit ang stand-in ay walang quote, at
    ang lane ay dapat pa ring tumanggi -- hindi mag-imbento."""
    seen, fake = calls
    fake.results = [(None, {}), (None, {})]
    tick, _f, _s = LR._live_tick_bbo(
        object(), "BDRX", execution_family="alpaca_spot", state="live_entered")
    assert tick is None
    assert len(seen) == 2


@pytest.mark.parametrize("family,state", [
    ("alpaca_spot", "watching_live"),
    ("coinbase_spot", "live_entered"),
])
def test_non_held_and_non_alpaca_paths_are_untouched(monkeypatch, family, state):
    """Ang pre-entry at ang crypto ay may sariling quote path. Hindi sila dapat
    dumaan sa execution-BBO contract kahit kailan."""
    called = []
    monkeypatch.setattr(LR, "_final_entry_bbo",
                        lambda *a, **k: called.append(k) or (None, {}))

    class _A:
        def get_best_bid_ask(self, pid):
            return _Tick(5.0), "ordinary"

    tick, freshness, snap = LR._live_tick_bbo(
        _A(), "X", execution_family=family, state=state)
    assert tick.bid == 5.0 and freshness == "ordinary" and snap is None
    assert called == [], "hindi dapat naabot ang execution-BBO contract"
