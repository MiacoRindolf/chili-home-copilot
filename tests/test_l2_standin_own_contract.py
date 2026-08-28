"""L2 STAND-IN, SARILING KONTRATA — ang WHLR/XPON premarket kill ng 2026-08-28.

Ang `_final_entry_bbo` ay may "judge the stand-in by its own bound" na clause,
pero naka-gate ito sa `timestamp_basis == "massive_sip_unix_ms"` LAMANG. Ang
tier-3 IQFeed L2 stand-in (`iqfeed_l2_provider_at`, migration 371) ay
hinuhusgahan pa rin ng DIREKTANG cap.

SINUKAT (2026-08-28 premarket, 75 min): 13/13 na L2-stale block (8 session) ay
age 10-15s laban sa 10s na direct cap — ang sariling 15s na kontrata ng
stand-in, na pinili laban sa p50 7s na L2 bridge lag, ay hindi kailanman
nakaabot sa validation. WHLR: 1.81/1.82, 10.245s ang edad, patay sa 0.245s.

Kasama: ang L2 stand-in ay dating nagsusuot ng `quote_authority=alpaca_direct`
(provenance mislabel) — kaya sa final seam ay puwede nitong i-adjust ang limit,
na tahasang ipinagbabawal sa cross-source na quote. Ngayon ay
`stand_in_iqfeed_l2` na ang dala nito at ang limit ay naka-pin sa planned.

Runnable: pytest tests/test_l2_standin_own_contract.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.venue.alpaca_spot import (
    FreshnessMeta,
    NormalizedTicker,
)


def _l2_standin_tick(age_s: float, symbol: str = "WHLR"):
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now - timedelta(seconds=max(0.0, age_s - 0.1)),
        provider_time_utc=now - timedelta(seconds=age_s),
        max_age_seconds=10.0,
    )
    return NormalizedTicker(
        product_id=symbol, bid=1.81, ask=1.82, mid=1.815, spread_bps=55.1,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": "iqfeed_l2",
             "timestamp_basis": "iqfeed_l2_provider_at"},
    ), meta


class _Ad:
    def __init__(self, tick, meta):
        self._r = (tick, meta)

    def get_execution_bbo(self, pid, **kw):
        return self._r


def test_whlr_class_l2_standin_now_survives_validation():
    """ANG EKSAKTONG WHLR KILL: L2 stand-in na 10.2s, direct cap 10.0s.
    Dati: stale (10.2 > 10.0). Ngayon: pasado (10.2 <= 15, sariling bound)."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    tick, meta = _l2_standin_tick(age_s=10.2)
    got, ev = _final_entry_bbo(
        _Ad(tick, meta), "WHLR", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert got is not None, ev
    assert ev["reason"] == "execution_bbo_ok"
    assert ev["max_age_seconds"] == 15.0


def test_l2_standin_beyond_its_own_bound_still_dies():
    """FAIL-CLOSED: lampas 15s ang L2 stand-in -> stale pa rin."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    tick, meta = _l2_standin_tick(age_s=17.0)
    got, ev = _final_entry_bbo(
        _Ad(tick, meta), "WHLR", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert got is None
    assert ev["reason"] == "execution_bbo_stale"


def test_l2_standin_carries_its_true_provenance():
    """Ang L2 stand-in ay HINDI na nagsusuot ng alpaca_direct — dala na nito
    ang stand_in_iqfeed_l2, kaya ang final seam ay nagpi-pin ng limit sa
    planned sa halip na magpa-presyo sa cross-source na ask."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    tick, meta = _l2_standin_tick(age_s=3.0)
    got, ev = _final_entry_bbo(
        _Ad(tick, meta), "WHLR", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert got is not None, ev
    assert ev["quote_authority"] == "stand_in_iqfeed_l2"


def test_massive_sip_contract_is_byte_identical():
    """PARITY: ang massive_sip na landas ay hindi ginalaw."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now, provider_time_utc=now - timedelta(seconds=4.0),
        max_age_seconds=10.0,
    )
    tick = NormalizedTicker(
        product_id="BRLS", bid=1.52, ask=1.53, mid=1.525, spread_bps=65.6,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": "massive_ws_universe",
             "timestamp_basis": "massive_sip_unix_ms"},
    )
    got, ev = _final_entry_bbo(
        _Ad(tick, meta), "BRLS", max_age_seconds=2.0, allow_stand_in=True,
        stand_in_max_age_seconds=10.0,
    )
    assert got is not None, ev
    assert ev["quote_authority"] == "stand_in_massive_sip"
    assert ev["max_age_seconds"] == 10.0


def test_direct_quote_never_inherits_the_wide_bound():
    """Ang lumang DIRECT quote ay hindi nakakapasok sa 15s na bar — basis
    check ang bantay (alpaca provider basis, hindi L2/SIP)."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now, provider_time_utc=now - timedelta(seconds=12.0),
        max_age_seconds=10.0,
    )
    tick = NormalizedTicker(
        product_id="GYGY", bid=3.52, ask=3.53, mid=3.525, spread_bps=28.4,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": "alpaca", "timestamp_basis": "provider_event_at"},
    )
    got, ev = _final_entry_bbo(
        _Ad(tick, meta), "GYGY", max_age_seconds=10.0, allow_stand_in=True,
        stand_in_max_age_seconds=15.0,
    )
    assert got is None
    assert ev["reason"] == "execution_bbo_stale"
