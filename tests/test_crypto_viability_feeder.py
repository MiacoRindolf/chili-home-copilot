"""Live crypto viability feeder: Coinbase 24h-stats -> Ross-pillar universe.

The legacy crypto breakout scanner was removed, leaving the momentum lane with no
live viability source (viability went stale past the 600s freshness gate, blocking
all guarded-live crypto entries). ``_build_crypto_momentum_universe`` rebuilds the
feed from the venue's own 24h stats. These tests pin the mapping + dedupe with a
stub adapter (no network).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.venue.protocol import FreshnessMeta, NormalizedProduct


def _prod(pid, base, quote, *, price, chg, volpct, qvol, tradable=True, status="online"):
    return NormalizedProduct(
        product_id=pid,
        base_currency=base,
        quote_currency=quote,
        status=status,
        trading_disabled=not tradable,
        cancel_only=False,
        limit_only=False,
        post_only=False,
        auction_mode=False,
        raw={
            "price": price,
            "price_percentage_change_24h": chg,
            "volume_percentage_change_24h": volpct,
            "approximate_quote_24h_volume": qvol,
            "volume_24h": "100",
        },
    )


class _StubAdapter:
    def __init__(self, products):
        self._p = products

    def get_products(self):
        return self._p, FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)


def _patch(monkeypatch, products):
    import app.services.trading.execution_family_registry as efr

    monkeypatch.setattr(efr, "resolve_live_spot_adapter_factory", lambda ef: (lambda: _StubAdapter(products)))


def test_feeder_maps_ross_pillars(monkeypatch) -> None:
    from app.services.trading_scheduler import _build_crypto_momentum_universe

    _patch(monkeypatch, [_prod("RSC-USD", "RSC", "USD", price="1.5", chg="58.3", volpct="2333.0", qvol="2525685")])
    out = _build_crypto_momentum_universe()
    assert len(out) == 1
    e = out[0]
    assert e["symbol"] == "RSC-USD"
    assert e["change_24h"] == pytest.approx(58.3)
    assert e["daily_change_pct"] == pytest.approx(58.3)
    assert e["rvol"] == pytest.approx(1.0 + 2333.0 / 100.0)  # +2333% volume -> ~24.33x
    assert e["quote_volume_24h"] == pytest.approx(2525685.0)


def test_feeder_filters_non_usd_untradable_and_zero(monkeypatch) -> None:
    from app.services.trading_scheduler import _build_crypto_momentum_universe

    _patch(
        monkeypatch,
        [
            _prod("BTC-EUR", "BTC", "EUR", price="60000", chg="1", volpct="0", qvol="1000"),  # non-USD
            _prod("DEAD-USD", "DEAD", "USD", price="1", chg="5", volpct="0", qvol="1000", tradable=False),
            _prod("NOPX-USD", "NOPX", "USD", price="0", chg="5", volpct="0", qvol="1000"),  # zero price
            _prod("NOVOL-USD", "NOVOL", "USD", price="1", chg="5", volpct="0", qvol="0"),  # zero turnover
            _prod("OK-USD", "OK", "USD", price="2", chg="9", volpct="100", qvol="5000"),  # keep
        ],
    )
    out = _build_crypto_momentum_universe()
    assert {e["symbol"] for e in out} == {"OK-USD"}


def test_feeder_dedupes_usd_over_usdc(monkeypatch) -> None:
    from app.services.trading_scheduler import _build_crypto_momentum_universe

    # USDC book appears first; the -USD book must win and collapse to one entry.
    _patch(
        monkeypatch,
        [
            _prod("RSC-USDC", "RSC", "USDC", price="1.5", chg="58", volpct="2333", qvol="2525685"),
            _prod("RSC-USD", "RSC", "USD", price="1.5", chg="58", volpct="2333", qvol="2525685"),
        ],
    )
    out = _build_crypto_momentum_universe()
    assert len(out) == 1
    assert out[0]["symbol"] == "RSC-USD"


def test_feeder_missing_volpct_yields_none_rvol(monkeypatch) -> None:
    from app.services.trading_scheduler import _build_crypto_momentum_universe

    _patch(monkeypatch, [_prod("ABC-USD", "ABC", "USD", price="3", chg="12", volpct=None, qvol="9000")])
    out = _build_crypto_momentum_universe()
    assert len(out) == 1
    assert out[0]["rvol"] is None  # scorer treats missing RVOL pillar gracefully
    assert out[0]["change_24h"] == pytest.approx(12.0)
