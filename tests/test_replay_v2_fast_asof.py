import pandas as pd


def test_replay_v2_prefetch_ohlcv_frames_normalizes_hits_and_misses(monkeypatch):
    from app.services.trading.momentum_neural import replay_v2 as rv

    idx = pd.date_range("2026-07-09 13:30", periods=2, freq="5min", tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [1.0, 1.1],
            "High": [1.2, 1.3],
            "Low": [0.9, 1.0],
            "Close": [1.1, 1.2],
            "Volume": [1000, 1200],
        },
        index=idx,
    )
    calls = []

    def fake_batch(symbols, *, interval, period):
        calls.append((symbols, interval, period))
        return {"AAA": frame}

    monkeypatch.setattr(rv, "fetch_ohlcv_batch", fake_batch)

    out = rv._prefetch_ohlcv_frames(["aaa", "AAA", "bbb"], interval="5m", period="1mo")

    assert calls == [(["AAA", "BBB"], "5m", "1mo")]
    assert out["AAA"] is frame
    assert out["BBB"] is None


def test_replay_v2_prefetch_ohlcv_frames_falls_back_on_batch_error(monkeypatch):
    from app.services.trading.momentum_neural import replay_v2 as rv

    def fake_batch(symbols, *, interval, period):
        raise RuntimeError("provider down")

    monkeypatch.setattr(rv, "fetch_ohlcv_batch", fake_batch)

    assert rv._prefetch_ohlcv_frames(["AAA"], interval="5m", period="1mo") == {}
