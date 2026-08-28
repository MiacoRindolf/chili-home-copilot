"""L1 OWN-CLOCK EXECUTION BBO TIER (#1236 follow-through, 2026-08-28).

Ang bridge ay naghahatid na ng quote rows na may totoong Bid/Ask Time event
clock — ang tier na ito ang nagdadala nito sa execution BBO stand-in chain
(pagkatapos ng SIP, bago ang depth) na may parehong dalawang-orasan na
disiplina. Kasama: ang #1233 own-contract validation at ang provenance label
(stand_in_iqfeed_l1 — pin-to-planned sa final seam).

Runnable: pytest tests/test_l1_own_clock_tier.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.venue.alpaca_spot import (
    AlpacaSpotAdapter,
    FreshnessMeta,
    NormalizedTicker,
)


def _l1_tick(age_s: float, sym: str = "AREN"):
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now - timedelta(seconds=max(0.0, age_s - 0.1)),
        provider_time_utc=now - timedelta(seconds=age_s),
        max_age_seconds=15.0,
    )
    return NormalizedTicker(
        product_id=sym, bid=2.10, ask=2.12, mid=2.11, spread_bps=94.8,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": "iqfeed_l1",
             "timestamp_basis": "iqfeed_q_bid_ask_time_clock"},
    ), meta


def test_chain_order_sip_then_l1_then_depth(monkeypatch):
    """Ang L1 own-clock ay tier 2.5: pagkatapos ng SIP, bago ang depth."""
    ad = AlpacaSpotAdapter()
    calls: list[str] = []

    def _no_direct(pid):
        return None, FreshnessMeta(
            retrieved_at_utc=datetime.now(timezone.utc),
            provider_time_utc=None, max_age_seconds=2.0,
        )

    monkeypatch.setattr(ad, "_alpaca_latest_quote", _no_direct)
    monkeypatch.setattr(
        ad, "_massive_sip_execution_bbo",
        lambda pid, ma: calls.append("sip") or None,
    )
    tick, meta = _l1_tick(3.0)
    monkeypatch.setattr(
        ad, "_iqfeed_l1_own_clock_execution_bbo",
        lambda pid, ma: calls.append("l1") or (tick, meta),
    )
    monkeypatch.setattr(
        ad, "_iqfeed_depth_execution_bbo",
        lambda pid, ma: calls.append("depth") or None,
    )
    got, _m = ad.get_execution_bbo(
        "AREN", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert calls == ["sip", "l1"], "depth ay hindi na dapat naabot"
    assert got is not None and got.bid == 2.10


def test_flag_off_skips_to_depth(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_iqfeed_l1_own_clock_enabled",
        False, raising=False,
    )
    ad = AlpacaSpotAdapter()
    assert ad._iqfeed_l1_own_clock_execution_bbo("AREN", 15.0) is None


def test_own_contract_validation_accepts_l1_basis():
    """#1233 pattern: ang L1 own-clock stand-in na 12s ang edad ay pasado sa
    sarili nitong 15s bound kahit 10s ang direct cap."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    tick, meta = _l1_tick(12.0)

    class _Ad:
        def get_execution_bbo(self, pid, **kw):
            return tick, meta

    got, ev = _final_entry_bbo(
        _Ad(), "AREN", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert got is not None, ev
    assert ev["reason"] == "execution_bbo_ok"
    assert ev["max_age_seconds"] == 15.0
    assert ev["quote_authority"] == "stand_in_iqfeed_l1"


def test_l1_beyond_its_bound_still_dies():
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    tick, meta = _l1_tick(17.0)

    class _Ad:
        def get_execution_bbo(self, pid, **kw):
            return tick, meta

    got, ev = _final_entry_bbo(
        _Ad(), "AREN", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert got is None
    assert ev["reason"] == "execution_bbo_stale"
