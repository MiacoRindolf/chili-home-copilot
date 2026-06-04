import json

from app.config import Settings
from app.models.trading import TradingHypothesis
from app.services.trading import learning


def test_hypothesis_bootstrap_iterations_setting_reaches_validation_result(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAIN_HYPOTHESIS_BOOTSTRAP_ITERATIONS", "250")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.services.trading.market_data.ALL_SCAN_TICKERS", ["TEST"])
    monkeypatch.setattr(learning, "_use_massive", lambda: False)
    monkeypatch.setattr(learning, "_use_polygon", lambda: False)
    monkeypatch.setattr(learning, "io_workers_med", lambda _settings: 1)
    monkeypatch.setattr(learning, "io_workers_high", lambda _settings: 1)
    monkeypatch.setattr(learning, "_derive_hypotheses_from_patterns", lambda *_args: 0)
    monkeypatch.setattr(learning, "_migrate_legacy_hypotheses", lambda *_args: 0)
    monkeypatch.setattr(learning, "save_insight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(learning, "log_learning_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(learning, "_spawn_pattern_from_hypothesis", lambda *_args: None)
    monkeypatch.setattr(
        learning,
        "get_trade_stats_by_pattern",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "app.services.trading.scanner.evolve_strategy_weights",
        lambda *_args, **_kwargs: {"adjusted": 0},
    )

    def _rows(_ticker):
        group_a = [{"rsi": 70.0, "ret_5d": 2.0} for _ in range(20)]
        group_b = [{"rsi": 30.0, "ret_5d": -1.0} for _ in range(20)]
        return group_a + group_b

    monkeypatch.setattr(learning, "_mine_from_history", _rows)
    hyp = TradingHypothesis(
        description="bootstrap iterations env smoke",
        condition_a="rsi > 60",
        condition_b="rsi <= 40",
        expected_winner="a",
        origin="llm_generated",
        status="pending",
    )

    class _Query:
        def __init__(self, rows):
            self.rows = rows

        def count(self):
            return len(self.rows)

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return self.rows

    class _Db:
        def __init__(self, rows):
            self.rows = rows

        def query(self, model):
            if model is TradingHypothesis:
                return _Query(self.rows)
            return _Query([])

        def commit(self):
            return None

    result = learning.validate_and_evolve(_Db([hyp]), user_id=None)
    stored = hyp.last_result_json
    payload = json.loads(stored) if isinstance(stored, str) else stored

    assert settings.brain_hypothesis_bootstrap_iterations == 250
    assert result["hypotheses_tested"] == 1
    assert payload["bootstrap"]["iterations"] == 250
