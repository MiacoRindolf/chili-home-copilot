"""EXECUTION BBO TIER 2.75 — embedded bid/ask ng trade tick (2026-08-30).

SINUKAT: execution_bbo_unavailable ×12,979 sa 5 araw (78% ng risk blocks);
AREN 991 block habang RTH na may 66,080 buhay na trade ticks — bawat isa may
embedded bid/ask (1.19/1.20) at exact-print provider clock, pero trades-only
ang IQFeed watch kaya walang quote rows para sa quote-tape tier. Ang tier na
ito ang nagbabasa ng embedded top-of-book ng PINAKABAGONG trade tick, may
parehong dual-clock na disiplina (bumabagsak nang sarado kapag nahuhuli ang
bridge drain — received_at lag).

Runnable: pytest tests/test_trade_embedded_bbo_tier.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.venue.alpaca_spot import (
    AlpacaSpotAdapter,
    FreshnessMeta,
    NormalizedTicker,
)


def _emb_tick(age_s: float, sym: str = "AREN"):
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now - timedelta(seconds=max(0.0, age_s - 0.1)),
        provider_time_utc=now - timedelta(seconds=age_s),
        max_age_seconds=15.0,
    )
    return NormalizedTicker(
        product_id=sym, bid=1.19, ask=1.20, mid=1.195, spread_bps=83.7,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": "iqfeed_trade_embedded"},
    ), meta


def _silence_upper_tiers(monkeypatch, ad, calls):
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
    monkeypatch.setattr(
        ad, "_iqfeed_l1_own_clock_execution_bbo",
        lambda pid, ma: calls.append("l1") or None,
    )


def test_chain_order_l1_then_embedded_then_depth(monkeypatch):
    """Tier 2.75: pagkatapos ng L1 own-clock, bago ang depth."""
    ad = AlpacaSpotAdapter()
    calls: list[str] = []
    _silence_upper_tiers(monkeypatch, ad, calls)
    tick, meta = _emb_tick(1.0)
    monkeypatch.setattr(
        ad, "_iqfeed_trade_embedded_execution_bbo",
        lambda pid, ma: calls.append("embedded") or (tick, meta),
    )
    monkeypatch.setattr(
        ad, "_iqfeed_depth_execution_bbo",
        lambda pid, ma: calls.append("depth") or None,
    )
    got, _m = ad.get_execution_bbo(
        "AREN", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert calls == ["sip", "l1", "embedded"], "depth ay hindi na dapat naabot"
    assert got is not None and got.bid == 1.19 and got.ask == 1.20


def test_embedded_none_falls_to_depth(monkeypatch):
    ad = AlpacaSpotAdapter()
    calls: list[str] = []
    _silence_upper_tiers(monkeypatch, ad, calls)
    monkeypatch.setattr(
        ad, "_iqfeed_trade_embedded_execution_bbo",
        lambda pid, ma: calls.append("embedded") or None,
    )
    monkeypatch.setattr(
        ad, "_iqfeed_depth_execution_bbo",
        lambda pid, ma: calls.append("depth") or None,
    )
    got, _m = ad.get_execution_bbo(
        "AREN", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert calls == ["sip", "l1", "embedded", "depth"]


def test_flag_off_is_noop(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_trade_embedded_enabled", False,
        raising=False,
    )
    ad = AlpacaSpotAdapter()
    assert ad._iqfeed_trade_embedded_execution_bbo("AREN", 15.0) is None


def test_crypto_pid_skipped():
    ad = AlpacaSpotAdapter()
    assert ad._iqfeed_trade_embedded_execution_bbo("BTC-USD", 15.0) is None


def test_db_tier_fresh_row_passes(db):
    """Tunay na DB row: sariwa sa parehong orasan ⇒ ginagamit."""
    from sqlalchemy import text

    now = datetime.now(timezone.utc)
    db.execute(text(
        "INSERT INTO iqfeed_trade_ticks "
        "(symbol, observed_at, price, size, bid, ask, source, "
        " provider_event_at, received_at, timestamp_basis) VALUES "
        "(:s, :o, 1.20, 100, 1.19, 1.20, 'iqfeed_l1', :p, :r, "
        " 'iqfeed_selected_trade_date_timems_exact')"
    ), dict(s="EMBT", o=now.replace(tzinfo=None), p=now, r=now))
    db.commit()
    ad = AlpacaSpotAdapter()
    result = ad._iqfeed_trade_embedded_bbo("EMBT", max_age_seconds=15.0)
    assert result is not None
    tick, meta = result
    assert tick.bid == 1.19 and tick.ask == 1.20
    assert tick.raw["feed"] == "iqfeed_trade_embedded"


def test_db_tier_drain_lag_fails_closed(db):
    """Received_at na 15 minutong huli (bridge drain lag) ⇒ None — ang tier ay
    hindi nagpapanggap na sariwa ang lumang obserbasyon."""
    from sqlalchemy import text

    now = datetime.now(timezone.utc)
    db.execute(text(
        "INSERT INTO iqfeed_trade_ticks "
        "(symbol, observed_at, price, size, bid, ask, source, "
        " provider_event_at, received_at, timestamp_basis) VALUES "
        "(:s, :o, 1.20, 100, 1.19, 1.20, 'iqfeed_l1', :p, :r, "
        " 'iqfeed_selected_trade_date_timems_exact')"
    ), dict(
        s="EMBL",
        o=(now - timedelta(minutes=15)).replace(tzinfo=None),
        p=now - timedelta(minutes=15),
        r=now,
    ))
    db.commit()
    ad = AlpacaSpotAdapter()
    assert ad._iqfeed_trade_embedded_bbo("EMBL", max_age_seconds=15.0) is None


def test_db_tier_missing_quote_sides_fails_closed(db):
    from sqlalchemy import text

    now = datetime.now(timezone.utc)
    db.execute(text(
        "INSERT INTO iqfeed_trade_ticks "
        "(symbol, observed_at, price, size, bid, ask, source, "
        " provider_event_at, received_at, timestamp_basis) VALUES "
        "(:s, :o, 1.20, 100, NULL, NULL, 'iqfeed_l1', :p, :r, "
        " 'iqfeed_selected_trade_date_timems_exact')"
    ), dict(s="EMBN", o=now.replace(tzinfo=None), p=now, r=now))
    db.commit()
    ad = AlpacaSpotAdapter()
    assert ad._iqfeed_trade_embedded_bbo("EMBN", max_age_seconds=15.0) is None
