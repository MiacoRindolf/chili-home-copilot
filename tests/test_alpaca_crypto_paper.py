"""Archived Alpaca-crypto helpers remain readable, but execution is quarantined.

The active recertification lane is paper/equity/long-only. Broker listing support
must never be interpreted as CHILI order authority.
"""

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
def test_alpaca_recertification_supports_equity_only() -> None:
    from app.services.trading.execution_family_registry import (
        execution_family_supports_asset_class,
    )

    assert not execution_family_supports_asset_class("alpaca_spot", "crypto")
    assert execution_family_supports_asset_class("alpaca_spot", "equity")
    assert not execution_family_supports_asset_class("robinhood_spot", "crypto")
    assert not execution_family_supports_asset_class("coinbase_spot", "equity")
    assert not execution_family_supports_asset_class("unknown_venue", "equity")


# ── crypto orders are rejected before transport ─────────────────────────────
def test_crypto_submit_is_quarantined_before_transport(monkeypatch) -> None:
    import app.services.trading.venue.alpaca_spot as ap

    calls = []

    def _forbidden_client():
        calls.append("transport")
        raise AssertionError("crypto instruction reached Alpaca transport")

    monkeypatch.setattr(ap, "_trading_client", _forbidden_client)
    a = ap.AlpacaSpotAdapter()
    for index, product_id in enumerate(("BTC-USD", "BTC/USD")):
        out = a.place_limit_order_gtc(
            product_id=product_id,
            side="buy",
            base_size="0.001",
            limit_price="50000",
            client_order_id=f"c{index}",
            position_intent="buy_to_open",
            time_in_force="day",
        )
        assert out["ok"] is False
        assert out["pre_submit_blocked"] is True
    assert calls == []


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
