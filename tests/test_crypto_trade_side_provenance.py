"""The venue's reported side convention must not invert aggression."""
from app.services.trading import microstructure as micro
from app.services.trading.venue import coinbase_spot as coinbase


def test_coinbase_maker_side_becomes_opposite_taker_side(monkeypatch):
    captured = []
    class Buffer:
        def append(self, trade):
            captured.append(trade)
        def recent(self, *_, **__):
            return captured
    monkeypatch.setattr(micro, "get_trade_buffer", lambda: Buffer())
    seam = coinbase.CoinbaseWebSocketSeam()
    monkeypatch.setattr(seam, "_fire_tick", lambda *_: None)
    seam._handle_trades([{"type": "update", "trades": [
        {"product_id": "BTC-USD", "trade_id": "101", "price": "100", "size": "3", "side": "BUY"},
        {"product_id": "BTC-USD", "trade_id": "102", "price": "101", "size": "1", "side": "SELL"},
        {"product_id": "BTC-USD", "trade_id": "103", "price": "101", "size": "2"},
    ]}])
    assert [trade.side for trade in captured] == ["SELL", "BUY", "UNKNOWN"]
    assert [trade.reported_side for trade in captured] == ["BUY", "SELL", None]
    assert [trade.provider_trade_id for trade in captured] == ["101", "102", "103"]
    assert captured[0].side_basis == "coinbase_reported_maker_inverted"
    assert captured[2].side_basis == "unknown"
    class EmptyBook:
        def latest(self, _):
            return None
    features = micro.compute_features("BTC-USD", book_buf=EmptyBook(), trade_buf=Buffer())
    # Known-side mass: one taker-buy unit / (three taker-sell + one buy).
    # The two unknown-side units remain unresolved, not invented buys or sells.
    assert features.trade_aggression == 0.25
