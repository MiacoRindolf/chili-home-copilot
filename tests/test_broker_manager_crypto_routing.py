from app.services import broker_manager


def test_known_numeric_crypto_prefers_coinbase(monkeypatch):
    monkeypatch.setattr(broker_manager.coinbase_service, "is_connected", lambda: True)
    monkeypatch.setattr(broker_manager.broker_service, "is_connected", lambda: True)

    assert broker_manager.get_best_broker_for("00-USD") == broker_manager.BROKER_COINBASE

    available = broker_manager.get_available_brokers_for("00-USD")
    assert available[0]["broker"] == broker_manager.BROKER_COINBASE
    assert available[0]["preferred"] is True
    assert available[1]["broker"] == broker_manager.BROKER_ROBINHOOD
