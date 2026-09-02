"""Ang SIP stand-in quote read ay BOUNDED sa oras (#1280).

ANG NASUKAT (2026-09-02 ~05:00Z, offline laban sa buhay na DB pagkatapos ng
index drop, malamig na cache, 10 simbolo):

    tier             median      max
    direct_alpaca      79ms    424ms
    massive_sip     2,430ms  20,745ms   (SSM)   <-- ITO ang ~8.9s ng place call
    l1_own_clock        4ms      5ms
    trade_embedded      3ms     36ms
    depth_l2            2ms     10ms

EXPLAIN (ANALYZE, BUFFERS) sa HIBS (walang SIP row): Bitmap Heap Scan sa
LAHAT ng 10,723 row ng simbolo sa 61GB na table, 8,127 buffer mula disk,
4,424ms. Ang parehong query na may 10-minutong bound sa observed_at (MUZ):
Index Scan, 4 buffer, 0.274ms.

BAKIT BYTE-IDENTICAL ANG RESULTA: bawat hilerang pumapasa sa freshness ay may
received_at sa loob ng max_age (<= 60s sa pinakamalawak na stand-in ceiling),
at ang observed_at (DB write) ay laging >= received_at. Kaya ang 10-minutong
bound ay superset ng bawat hilerang maaaring pumasa -- plano lang ang nagbago.

Runnable: pytest tests/test_massive_sip_quote_time_bound.py -v
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.services.trading.venue import alpaca_spot as mod
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeDb:
    def __init__(self, row, sink):
        self._row = row
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, stmt, params=None):
        self._sink.append((str(stmt), dict(params or {})))
        return _Result(self._row)


@pytest.fixture()
def adapter():
    return AlpacaSpotAdapter.__new__(AlpacaSpotAdapter)   # walang network


def _capture(monkeypatch, row=None):
    sink: list[tuple[str, dict]] = []
    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDb(row, sink))
    return sink


def _fresh_row(now):
    provider_at = now - timedelta(seconds=0.6)
    received_at = now - timedelta(seconds=0.3)
    return (
        123456, 1.03, 1.04, 1.035, None, "massive_ws_universe",
        provider_at, received_at, mod._MASSIVE_SIP_BASIS,
        mod._MASSIVE_SIP_BRIDGE_VERSION, "Q",
    )


def test_the_sip_read_is_time_bounded(adapter, monkeypatch):
    """ANG PANGUNAHIN: may bound sa observed_at ang SQL — hindi na buong kasaysayan."""
    sink = _capture(monkeypatch)
    out = adapter._massive_sip_quote("SSM", max_age_seconds=5.0)
    assert out is None
    (sql, params), = sink
    assert "momentum_nbbo_spread_tape" in sql
    assert re.search(r"observed_at\s*>\s*now\(\)\s*-\s*interval\s*'\d+ minutes'", sql), sql
    assert params["s"] == "SSM"


def test_the_bound_is_a_superset_of_every_passing_row(adapter, monkeypatch):
    """Ang bound ay dapat MAS malawak kaysa sa pinakamalawak na freshness ceiling.

    Kung mas makitid ang bound kaysa sa ceiling, may hilerang dating pumapasa
    na hindi na makikita — hindi na byte-identical. 10 min >> 60s stand-in.
    """
    sink = _capture(monkeypatch)
    adapter._massive_sip_quote("SSM", max_age_seconds=60.0)
    (sql, _), = sink
    minutes = int(re.search(r"interval '(\d+) minutes'", sql).group(1))
    widest_ceiling_s = 60.0
    assert minutes * 60 >= widest_ceiling_s * 5, minutes


def test_a_fresh_row_still_passes_through_the_bounded_read(adapter, monkeypatch):
    """Sanity: ang bound ay hindi nagpapalya ng tunay na sariwang hilera."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(mod, "_now", lambda: now)
    _capture(monkeypatch, row=_fresh_row(now))
    out = adapter._massive_sip_quote("SSM", max_age_seconds=5.0)
    assert isinstance(out, tuple) and len(out) == 2
    tick, meta = out
    assert (tick.bid, tick.ask) == (1.03, 1.04)
    assert meta.max_age_seconds == 5.0


def test_a_stale_row_is_still_rejected(adapter, monkeypatch):
    """Ang freshness contract ay hindi nagbago: 14.8-oras na quote = None."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(mod, "_now", lambda: now)
    stale = list(_fresh_row(now))
    stale[6] = now - timedelta(hours=14.8)
    stale[7] = now - timedelta(hours=14.8)
    _capture(monkeypatch, row=tuple(stale))
    assert adapter._massive_sip_quote("SSM", max_age_seconds=60.0) is None


def test_every_nbbo_tape_tier_read_is_time_bounded(adapter, monkeypatch):
    """Parity: LAHAT ng tier na nagbabasa ng momentum_nbbo_spread_tape (61GB) o
    iqfeed_depth_snapshots ay bounded — SIP, L1 own-clock, depth.

    SADYANG WALA RITO ang trade-embedded tier: ang iqfeed_trade_ticks (89GB) ay
    may BRIN sa observed_at na sira ang pagkakasunod (tingnan ang memory:
    probe timeouts), kaya ang isang range predicate doon ay maaaring hilahin
    ng planner sa BRIN at maging mas masahol. Nasukat itong 3ms nang walang
    bound (ang pinakabagong hilera ng simbolo ay agad tumutugma) — iniwan.
    """
    sink = _capture(monkeypatch)
    adapter._massive_sip_quote("SSM", max_age_seconds=5.0)
    adapter._iqfeed_l1_own_clock_quote("SSM", max_age_seconds=60.0)
    adapter._iqfeed_depth_quote("SSM", max_age_seconds=20.0)
    assert len(sink) == 3, "inaasahan ang isang SQL kada tier"
    for sql, _ in sink:
        assert re.search(r"observed_at\s*>\s*", sql) and "interval" in sql, (
            f"walang time bound: {sql[:160]}"
        )

