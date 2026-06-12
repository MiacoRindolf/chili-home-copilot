"""Alpaca crypto paper rail (operator 2026-06-12: 'magpaper trade din ang crypto
sa alpaca'): symbol mapping, GTC-only crypto TIF, multi-class family gate, and
the twin-arm listing probe — the soak that measures whether crypto can go live
on Alpaca (fees 0.15/0.25% vs Coinbase retail ~0.6%)."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.venue.alpaca_spot import (
    _from_alpaca_symbol,
    _is_crypto_pid,
    _to_symbol,
)


# ── symbol mapping ───────────────────────────────────────────────────────────
def test_symbol_mapping_round_trips() -> None:
    assert _to_symbol("BTC-USD") == "BTC/USD"
    assert _to_symbol("AAPL") == "AAPL"
    assert _from_alpaca_symbol("BTC/USD") == "BTC-USD"
    assert _from_alpaca_symbol("AAPL") == "AAPL"
    assert _is_crypto_pid("KAIO-USD") and not _is_crypto_pid("INDP")


# ── multi-class family gate ──────────────────────────────────────────────────
def test_alpaca_supports_both_asset_classes() -> None:
    from app.services.trading.execution_family_registry import (
        execution_family_supports_asset_class,
    )

    assert execution_family_supports_asset_class("alpaca_spot", "crypto")
    assert execution_family_supports_asset_class("alpaca_spot", "equity")
    assert not execution_family_supports_asset_class("robinhood_spot", "crypto")
    assert not execution_family_supports_asset_class("coinbase_spot", "equity")
    assert not execution_family_supports_asset_class("unknown_venue", "equity")


# ── crypto orders use GTC (Alpaca rejects DAY for crypto) ────────────────────
def test_crypto_submit_uses_gtc(monkeypatch) -> None:
    import app.services.trading.venue.alpaca_spot as ap

    captured = {}

    class _FakeTC:
        def submit_order(self, order_data):
            captured["symbol"] = order_data.symbol
            captured["tif"] = str(order_data.time_in_force)
            return SimpleNamespace(id="o1", client_order_id="c1", status="accepted")

    monkeypatch.setattr(ap, "_trading_client", lambda: _FakeTC())
    a = ap.AlpacaSpotAdapter()
    out = a.place_limit_order_gtc(
        product_id="BTC-USD", side="buy", base_size="0.001", limit_price="50000",
        client_order_id="c1",
    )
    assert out["ok"], out
    assert captured["symbol"] == "BTC/USD"
    assert "GTC" in captured["tif"].upper()


def test_twin_listing_probe_caches_and_fails_closed(monkeypatch) -> None:
    import app.services.trading.momentum_neural.auto_arm as aa

    aa._ALPACA_LISTED_CACHE.clear()
    calls = {"n": 0}

    class _FakeAdapter:
        def get_product(self, sym):
            calls["n"] += 1
            if sym == "BTC-USD":
                return SimpleNamespace(trading_disabled=False), None
            raise RuntimeError("not found")

    import app.services.trading.venue.alpaca_spot as ap

    monkeypatch.setattr(ap, "AlpacaSpotAdapter", _FakeAdapter)
    assert aa._alpaca_lists_symbol("BTC-USD") is True
    assert aa._alpaca_lists_symbol("BTC-USD") is True  # cached
    assert calls["n"] == 1
    assert aa._alpaca_lists_symbol("KAIO-USD") is False  # probe error -> no twin
    aa._ALPACA_LISTED_CACHE.clear()
