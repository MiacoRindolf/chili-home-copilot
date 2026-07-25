import pandas as pd
import pytest


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


@pytest.mark.parametrize(
    ("float_gate_enabled", "reason"),
    [
        (True, "historical_float_reference_coverage_unavailable"),
        (False, "historical_universe_reference_coverage_unavailable"),
    ],
)
def test_full_pipeline_reference_gap_is_terminal_before_current_lookups(
    monkeypatch,
    float_gate_enabled,
    reason,
):
    from app.services.trading.momentum_neural import replay_v2 as rv

    class RecordedTape:
        halts = {}

        def __init__(self, date):
            assert date == "2026-07-24"

        def symbols(self):
            return ["AAA"]

    monkeypatch.setattr(rv, "Tape", RecordedTape)
    monkeypatch.setattr(rv, "REPLAY_PRINTS_FILL", True)
    monkeypatch.setattr(
        rv.settings,
        "chili_momentum_universe_float_gate_enabled",
        float_gate_enabled,
    )
    monkeypatch.setattr(
        rv,
        "TradeTape",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("coverage-unavailable replay must not load prints")
        ),
    )
    monkeypatch.setattr(
        rv,
        "build_equity_universe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("coverage-unavailable replay must not rank candidates")
        ),
    )
    monkeypatch.setattr(
        rv,
        "fetch_ohlcv_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("coverage-unavailable replay must not call providers")
        ),
    )

    result = rv.run_replay(
        "2026-07-24",
        persist=False,
        armed_source="full_pipeline",
    )

    assert result["coverage_grade"] == "COVERAGE_UNAVAILABLE"
    assert result["coverage_unavailable_reasons"] == [reason]
    assert result["error"] == reason
    assert result["coverage_provenance"] == {
        "selection_builder": "build_equity_universe",
        "float_gate_setting": "chili_momentum_universe_float_gate_enabled",
        "float_gate_enabled": float_gate_enabled,
        "current_db_fallback_allowed": False,
        "current_provider_fallback_allowed": False,
    }
    assert result["trades"] == []
    assert result["total_usd"] == 0.0
