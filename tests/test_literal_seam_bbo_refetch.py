"""LITERAL-SEAM BBO REFETCH — ang reservation ang tumanda, hindi ang market.

XRPI 2026-08-20 15:47: ang aprubadong quote ay 0.123s ang edad — tapos ang
adaptive reservation path ay gumugol ng 10.4s bago ang literal seam, kaya ang
muling pag-edad ng LUMANG freshness ay sumagot sa maling tanong at nag-defer
(`alpaca_final_bbo_stale_after_reservation`). Pangatlong sariling-bagal kill ng
araw. Ang tamang tanong sa huling hakbang: "may sariwa at maayos bang merkado
NGAYON?" — kaya nagre-refetch na tayo sa mismong seam. Naka-pin na ang limit
(hindi kailanman nagre-reprice ang sariwang tick), entry-only ang seam, at ang
bigong refetch ay bumabagsak sa eksaktong defer na dating ginagawa.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.venue.alpaca_spot import FreshnessMeta, NormalizedTicker


def _tick(age_ms=100, authority_basis="provider_event_at", feed="alpaca"):
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now,
        provider_time_utc=now - timedelta(milliseconds=age_ms),
        max_age_seconds=2.0,
    )
    return NormalizedTicker(
        product_id="XRPI", bid=6.82, ask=6.84, mid=6.83, spread_bps=29.28,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": feed, "timestamp_basis": authority_basis},
    ), meta


class _RefetchAdapter:
    """Adapter na nagbibilang ng refetch at kayang mag-fail nang deterministiko."""

    def __init__(self, tick=None, meta=None):
        self.calls = []
        self._tick, self._meta = tick, meta

    def get_execution_bbo(self, product_id, **kwargs):
        self.calls.append(kwargs)
        return self._tick, self._meta


def test_refetch_call_is_entry_only_and_bounded():
    """Ang refetch ay dapat mag-opt-in sa stand-in (entry seam ito) at gamitin
    ang literal ceiling, hindi isang pribadong mas maluwag."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    tick, meta = _tick()
    ad = _RefetchAdapter(tick, meta)
    got_tick, ev = _final_entry_bbo(
        ad, "XRPI", max_age_seconds=2.0, allow_stand_in=True
    )
    assert got_tick is not None
    assert ad.calls[0]["max_age_seconds"] == 2.0
    assert ad.calls[0]["allow_stand_in"] is True
    assert ev["quote_authority"] == "alpaca_direct"


def test_failed_refetch_reports_reason():
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    now = datetime.now(timezone.utc)
    stale_meta = FreshnessMeta(
        retrieved_at_utc=now, provider_time_utc=None, max_age_seconds=2.0
    )
    ad = _RefetchAdapter(None, stale_meta)
    got_tick, ev = _final_entry_bbo(
        ad, "XRPI", max_age_seconds=2.0, allow_stand_in=True
    )
    assert got_tick is None
    assert ev["reason"] == "execution_bbo_unavailable"


def test_both_literal_seams_carry_the_refetch():
    """Ang DALAWANG literal seam (after-reservation at at-literal-post) ay
    parehong nagre-refetch bago mag-defer — hindi na muling ine-edad ang lumang
    freshness bilang huling salita."""
    import inspect

    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner._governed_place)
    # Dalawang refetch site, may allow_stand_in pareho, at pareho pa ring
    # nagde-defer sa parehong error string kapag bigo.
    assert src.count("literal_refetch_attempted") == 2
    assert src.count("allow_stand_in=True") >= 2
    assert src.count("alpaca_final_bbo_stale_after_reservation") >= 2
