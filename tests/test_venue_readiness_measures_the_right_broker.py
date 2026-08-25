"""Ang venue readiness ay dapat sumukat sa venue na TATAKBUHAN (2026-08-25).

ANG DEPEKTO. Para sa equity ay tinatanong ng ``_venue_readiness_score`` ang
**Robinhood**::

    return 1.0 if brokers.get("robinhood", {}).get("connected") else 0.5

Pero ang buong momentum lane ay tumatakbo sa **Alpaca**. Napatunayan sa buhay na DB::

    SELECT execution_family, count(*) FROM trading_automation_sessions
    WHERE mode='live' AND created_at::date = current_date;
      alpaca_spot | 498       <- lahat sila

At ang ``get_all_broker_statuses()`` ay may alam lamang sa **coinbase / robinhood /
metamask** -- **WALANG alpaca**. Kaya ang termino ay hindi kailanman naging sukat ng
venue na aktwal nating pinagpapadalhan ng order.

ANG BUNGA ay tahimik. Ang venue term ay isa sa **limang** input ng allocator score::

    score = research*0.36 + drift*0.18 + exec*0.18 + venue*0.14 + heat*0.14

at ang pag-upo nito sa 0.5 kasama ang tatlong iba pang neutral at ang naka-sahig na
heat ang gumagawa ng eksaktong **0.4580** na constant sa lahat ng 391 packet.

⚠️ AT MAS MASAHOL PA ANG KABILANG PANIG: kung MAKONEKTA ang Robinhood ay tataas ito
sa 1.0 -- **maling impormasyon**, hindi tama. Isang neutral na 0.5 na sinasabing
"hindi alam" ay mas tapat kaysa sa 1.0 na sinasabing "handa" tungkol sa ibang
brokerage.

Runnable: pytest tests/test_venue_readiness_measures_the_right_broker.py -v
"""
from __future__ import annotations

import inspect

import pytest

from app.services.trading import portfolio_allocator as pa
from app.services.trading.portfolio_allocator import (
    _venue_readiness_score,
    build_session_allocation_decision,
    evaluate_allocation_candidate,
)


@pytest.fixture()
def robinhood_connected(monkeypatch):
    monkeypatch.setattr(
        pa, "get_all_broker_statuses",
        lambda: {"robinhood": {"connected": True}, "coinbase": {"connected": True}},
    )


@pytest.fixture()
def all_disconnected(monkeypatch):
    monkeypatch.setattr(
        pa, "get_all_broker_statuses",
        lambda: {"robinhood": {"connected": False}, "coinbase": {"connected": False}},
    )


def test_an_alpaca_candidate_is_NOT_scored_by_robinhood(robinhood_connected):
    """ANG PANGUNAHING KASO. Ang konektadong Robinhood ay hindi dapat magpataas ng
    score ng isang kandidatong ipapadala sa Alpaca."""
    assert _venue_readiness_score("AAPL", execution_family="alpaca_spot") == 0.5


def test_an_alpaca_candidate_is_the_same_when_robinhood_is_down(all_disconnected):
    """At hindi rin ito dapat bumaba -- walang sinasabi ang Robinhood tungkol sa atin."""
    assert _venue_readiness_score("AAPL", execution_family="alpaca_spot") == 0.5


@pytest.mark.parametrize("family", ["alpaca_spot", "alpaca_short", "ALPACA_SPOT"])
def test_every_alpaca_family_is_covered(robinhood_connected, family):
    assert _venue_readiness_score("AAPL", execution_family=family) == 0.5


def test_a_robinhood_candidate_still_reads_robinhood(robinhood_connected):
    """⚠️ WALANG REGRESSION. Ang mga pamilyang TALAGANG gumagamit ng Robinhood ay
    dapat sukatin pa rin nito."""
    assert _venue_readiness_score("AAPL", execution_family="robinhood_spot") == 1.0


def test_a_crypto_candidate_still_reads_coinbase(robinhood_connected):
    assert _venue_readiness_score("BTC-USD", execution_family="coinbase_spot") == 1.0


def test_a_disconnected_coinbase_still_penalises_crypto(all_disconnected):
    assert _venue_readiness_score("BTC-USD", execution_family="coinbase_spot") == 0.4


def test_no_family_keeps_the_legacy_behaviour(robinhood_connected):
    """Ang tumatawag na walang alam na family ay eksaktong gaya ng dati."""
    assert _venue_readiness_score("AAPL") == 1.0
    assert _venue_readiness_score("BTC-USD") == 1.0


def test_the_family_is_threaded_from_the_session():
    """⚠️ ANG PARAMETER AY WALANG SILBI KUNG HINDI IPINAPASA. At ang execution_MODE
    (live/paper) ay HINDI ang family -- iyon ang madaling maling ikabit."""
    assert "execution_family" in inspect.signature(evaluate_allocation_candidate).parameters
    body = inspect.getsource(evaluate_allocation_candidate)
    assert "execution_family=execution_family" in body, (
        "dapat ihatid ang FAMILY, hindi ang mode"
    )
    builder = inspect.getsource(build_session_allocation_decision)
    assert 'execution_family=getattr(session, "execution_family"' in builder


def test_the_alpaca_branch_never_consults_the_broker_map():
    """Ang tapat na 'hindi alam' ay hindi dapat umaasa sa isang mapa na walang
    alpaca -- kung tinatanong pa rin nito ang mapa, isa itong sagot na nagkataon."""
    src = inspect.getsource(_venue_readiness_score)
    alpaca_branch = src[src.index('if family.startswith("alpaca")'):]
    early = alpaca_branch[: alpaca_branch.index("return 0.5")]
    assert "get_all_broker_statuses" not in early
