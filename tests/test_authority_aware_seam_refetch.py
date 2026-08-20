"""AUTHORITY-AWARE SEAMS — pito ang latency casualty ng 2026-08-20 hapon.

Iisang klase silang lahat: sariwang quote sa approval, tumanda sa loob ng
tick/reservation, namatay sa isang seam na (a) walang refetch (GYGY-1, TETH sa
REUSE branch) o (b) may refetch pero ipinasa ang direct na 2.0s cap sa stand-in
na hindi kayang lampasan ang 5s flush lag (GYGY-2, BRLS).

Ang lunas sa UGAT: ang `get_execution_bbo` ay tumatanggap na ng hiwalay na
`stand_in_max_age_seconds` — bawat source ay sinusukat sa SARILING kontrata
(direct 2.0s, stand-in 10s). Lahat ng seam ay tumatawag sa iisang daan:
1. REUSE branch: fall-through sa refetch sa halip na mag-defer
2-3. ang dalawang #1082 seam: pinapasa na ang stand-in bound
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.venue.alpaca_spot import (
    AlpacaSpotAdapter,
    FreshnessMeta,
    NormalizedTicker,
)


class _Recorder:
    """Adapter facade na nagre-record ng mga bound na ipinasa pababa."""

    def __init__(self):
        self.standin_bounds = []

    def install(self, monkeypatch, adapter, *, standin_result=None):
        def _no_direct(pid):
            return None, FreshnessMeta(
                retrieved_at_utc=datetime.now(timezone.utc),
                provider_time_utc=None,
                max_age_seconds=2.0,
            )

        def _standin(pid, max_age):
            self.standin_bounds.append(max_age)
            return standin_result

        monkeypatch.setattr(adapter, "_alpaca_latest_quote", _no_direct)
        monkeypatch.setattr(adapter, "_massive_sip_execution_bbo", _standin)


def _standin_tick(age_s=4.0):
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now - timedelta(seconds=age_s - 0.1),
        provider_time_utc=now - timedelta(seconds=age_s),
        max_age_seconds=10.0,
    )
    return NormalizedTicker(
        product_id="BRLS", bid=1.52, ask=1.53, mid=1.525, spread_bps=65.6,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": "massive_ws_universe",
             "timestamp_basis": "massive_sip_unix_ms"},
    ), meta


# ─────────────── ang adapter: bawat source, sariling kontrata ───────────────


def test_standin_bound_reaches_the_standin_path(monkeypatch):
    """max_age=2.0 sa direct, pero 10.0 ang umaabot sa stand-in."""
    ad = AlpacaSpotAdapter()
    rec = _Recorder()
    rec.install(monkeypatch, ad, standin_result=None)
    ad.get_execution_bbo(
        "BRLS", max_age_seconds=2.0, allow_stand_in=True,
        stand_in_max_age_seconds=10.0,
    )
    assert rec.standin_bounds == [10.0]


def test_without_the_bound_old_behavior_is_exact(monkeypatch):
    """PARITY: walang stand_in_max_age_seconds -> ang requested cap pa rin ang
    pumupunta sa stand-in (ang dating #1082 na ugali, byte-identical)."""
    ad = AlpacaSpotAdapter()
    rec = _Recorder()
    rec.install(monkeypatch, ad, standin_result=None)
    ad.get_execution_bbo("BRLS", max_age_seconds=2.0, allow_stand_in=True)
    assert rec.standin_bounds == [2.0]


def test_direct_only_callers_never_touch_the_standin(monkeypatch):
    ad = AlpacaSpotAdapter()
    rec = _Recorder()
    rec.install(monkeypatch, ad, standin_result=None)
    ad.get_execution_bbo("BRLS", max_age_seconds=2.0)
    assert rec.standin_bounds == []


# ─────────────── ang caller: validation sa tamang bound ───────────────


def test_brls_class_standin_now_survives_validation(monkeypatch):
    """Ang eksaktong BRLS kill: stand-in na 4s ang edad, direct cap 2.0s.
    Dati: stale (4 > 2). Ngayon: pasado (4 <= 10, ang sariling bound nito)."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    tick, meta = _standin_tick(age_s=4.0)

    class _Ad:
        def get_execution_bbo(self, pid, **kw):
            assert kw.get("stand_in_max_age_seconds") == 10.0
            return tick, meta

    got, ev = _final_entry_bbo(
        _Ad(), "BRLS", max_age_seconds=2.0, allow_stand_in=True,
        stand_in_max_age_seconds=10.0,
    )
    assert got is not None, ev
    assert ev["reason"] == "execution_bbo_ok"
    assert ev["quote_authority"] == "stand_in_massive_sip"
    assert ev["max_age_seconds"] == 10.0


def test_stale_standin_beyond_its_own_bound_still_dies(monkeypatch):
    """FAIL-CLOSED: lampas 10s ang stand-in -> stale pa rin."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    tick, meta = _standin_tick(age_s=12.0)

    class _Ad:
        def get_execution_bbo(self, pid, **kw):
            return tick, meta

    got, ev = _final_entry_bbo(
        _Ad(), "BRLS", max_age_seconds=2.0, allow_stand_in=True,
        stand_in_max_age_seconds=10.0,
    )
    assert got is None
    assert ev["reason"] == "execution_bbo_stale"


def test_direct_quote_never_inherits_the_wide_bound(monkeypatch):
    """Ang lumang DIRECT quote ay hindi puwedeng pumasok sa maluwag na bar —
    ang basis check ang bantay (hindi massive_sip_unix_ms ang direct)."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now,
        provider_time_utc=now - timedelta(seconds=8.0),
        max_age_seconds=10.0,
    )
    tick = NormalizedTicker(
        product_id="GYGY", bid=3.52, ask=3.53, mid=3.525, spread_bps=28.4,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": "alpaca", "timestamp_basis": "provider_event_at"},
    )

    class _Ad:
        def get_execution_bbo(self, pid, **kw):
            return tick, meta

    got, ev = _final_entry_bbo(
        _Ad(), "GYGY", max_age_seconds=2.0, allow_stand_in=True,
        stand_in_max_age_seconds=10.0,
    )
    assert got is None, "8s na direct quote sa 2.0s na kontrata = stale"
    assert ev["reason"] == "execution_bbo_stale"
    assert ev["max_age_seconds"] == 2.0


# ─────────────── ang tatlong seam ay naka-wire ───────────────


def test_all_three_seams_pass_the_standin_bound():
    import inspect

    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner._governed_place)
    # REUSE fall-through + 2 refetch seams = 3 na tawag na may bound.
    assert src.count("stand_in_max_age_seconds=") == 3
    assert "reuse-seam BBO refetch" in src
