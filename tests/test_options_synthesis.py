from __future__ import annotations

from datetime import datetime, timedelta

from app.config import (
    AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS,
    settings,
)
from app.services.trading.options.synthesis import (
    clear_synthesis_no_survivor_cache,
    _quality_sort_key,
    synthesize_option_meta,
)


class _FakeOptionsAdapter:
    def __init__(self, quotes: dict[float, dict[str, str]]):
        self.quotes = quotes

    def find_contract(self, _underlying: str, _expiration: str, strike: float, _option_type: str):
        if float(strike) not in self.quotes:
            return None
        return {"id": str(float(strike))}

    def get_quote(self, option_id: str):
        return self.quotes.get(float(option_id))


def _wire_synthesis_fakes(monkeypatch, quotes: dict[float, dict[str, str]]) -> None:
    from app.services import broker_service
    from app.services.trading import strategy_parameter
    from app.services.trading.options import synthesis
    from app.services.trading.venue import robinhood_options

    clear_synthesis_no_survivor_cache()
    expiration = (datetime.utcnow().date() + timedelta(days=21)).isoformat()
    monkeypatch.setattr(
        broker_service,
        "get_option_chains",
        lambda _underlying: {"expiration_dates": [expiration]},
    )
    monkeypatch.setattr(
        robinhood_options,
        "RobinhoodOptionsAdapter",
        lambda: _FakeOptionsAdapter(quotes),
    )
    monkeypatch.setattr(synthesis, "_register_synthesis_parameters", lambda _db: None)
    monkeypatch.setattr(
        strategy_parameter,
        "get_parameter",
        lambda *_args, default=None, **_kwargs: default,
    )
    monkeypatch.setattr(settings, "chili_autotrader_options_substitute_dte", 21)
    monkeypatch.setattr(settings, "chili_autotrader_options_max_contract_notional_usd", 300.0)
    monkeypatch.setattr(
        settings,
        "chili_autotrader_options_synthesis_no_survivor_cache_ttl_seconds",
        AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS,
    )


def test_synthesize_option_meta_selects_affordable_quality_contract(monkeypatch):
    _wire_synthesis_fakes(
        monkeypatch,
        {
            100.0: {"bid_price": "7.90", "ask_price": "8.00"},
            95.0: {"bid_price": "8.90", "ask_price": "9.00"},
            105.0: {"bid_price": "1.45", "ask_price": "1.50"},
        },
    )

    meta = synthesize_option_meta(
        db=None,
        underlying="XYZ",
        spot=100.0,
        notional_usd=300.0,
        underlying_target=112.0,
        underlying_stop=96.0,
        confidence=0.9,
    )

    assert meta is not None
    assert meta["strike"] == 105.0
    assert meta["quantity"] == 2
    assert meta["contract_key"] == f"XYZ:{meta['expiration']}:call:105.000"
    assert meta["price_domain"] == "option_premium"
    assert meta["quote_snapshot_recorded"] is False
    assert meta["synthesis_contract_notional_usd"] == 150.0
    assert meta["synthesis_reject_counts"]["contract_cost_above_budget"] == 2
    assert meta["entry_quality"]["option_reward_risk"] > 1.0


def test_option_synthesis_ranks_contracts_by_after_cost_edge():
    cheap_on_paper_but_wide = {
        "synthesis_spread_pct": 18.0,
        "synthesis_contract_notional_usd": 100.0,
        "entry_quality": {
            "expected_value_pct_of_premium": 240.0,
            "expected_value_after_cost_pct_of_premium": 12.0,
            "option_reward_risk": 3.5,
            "option_reward_risk_after_cost": 1.1,
        },
    }
    lower_raw_but_clean = {
        "synthesis_spread_pct": 2.0,
        "synthesis_contract_notional_usd": 120.0,
        "entry_quality": {
            "expected_value_pct_of_premium": 180.0,
            "expected_value_after_cost_pct_of_premium": 45.0,
            "option_reward_risk": 2.4,
            "option_reward_risk_after_cost": 1.8,
        },
    }

    assert _quality_sort_key(lower_raw_but_clean) > _quality_sort_key(
        cheap_on_paper_but_wide
    )


def test_synthesize_option_meta_rejects_when_contract_exceeds_budget(monkeypatch):
    _wire_synthesis_fakes(
        monkeypatch,
        {
            100.0: {"bid_price": "7.90", "ask_price": "8.00"},
            95.0: {"bid_price": "8.90", "ask_price": "9.00"},
            105.0: {"bid_price": "3.95", "ask_price": "4.00"},
        },
    )

    meta = synthesize_option_meta(
        db=None,
        underlying="XYZ",
        spot=100.0,
        notional_usd=300.0,
        underlying_target=112.0,
        underlying_stop=96.0,
        confidence=0.9,
    )

    assert meta is None


def test_synthesize_option_meta_rejects_crossed_option_quotes(monkeypatch):
    _wire_synthesis_fakes(
        monkeypatch,
        {
            100.0: {"bid_price": "8.10", "ask_price": "8.00"},
            95.0: {"bid_price": "9.10", "ask_price": "9.00"},
            105.0: {"bid_price": "1.55", "ask_price": "1.50"},
        },
    )

    meta = synthesize_option_meta(
        db=None,
        underlying="XYZ",
        spot=100.0,
        notional_usd=300.0,
        underlying_target=112.0,
        underlying_stop=96.0,
        confidence=0.9,
    )

    assert meta is None


def test_synthesize_option_meta_caches_recent_no_survivor_context(monkeypatch):
    from app.services import broker_service
    from app.services.trading import strategy_parameter
    from app.services.trading.options import synthesis
    from app.services.trading.venue import robinhood_options

    clear_synthesis_no_survivor_cache()
    expiration = (datetime.utcnow().date() + timedelta(days=21)).isoformat()
    chain_calls = {"count": 0}

    def _chains(_underlying: str):
        chain_calls["count"] += 1
        return {"expiration_dates": [expiration]}

    monkeypatch.setattr(broker_service, "get_option_chains", _chains)
    monkeypatch.setattr(
        robinhood_options,
        "RobinhoodOptionsAdapter",
        lambda: _FakeOptionsAdapter(
            {
                100.0: {"bid_price": "7.90", "ask_price": "8.00"},
                105.0: {"bid_price": "3.95", "ask_price": "4.00"},
            }
        ),
    )
    monkeypatch.setattr(synthesis, "_register_synthesis_parameters", lambda _db: None)
    monkeypatch.setattr(
        strategy_parameter,
        "get_parameter",
        lambda *_args, default=None, **_kwargs: default,
    )
    monkeypatch.setattr(settings, "chili_autotrader_options_substitute_dte", 21)
    monkeypatch.setattr(settings, "chili_autotrader_options_max_contract_notional_usd", 300.0)
    monkeypatch.setattr(
        settings,
        "chili_autotrader_options_synthesis_no_survivor_cache_ttl_seconds",
        AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS,
    )

    kwargs = dict(
        db=None,
        underlying="XYZ",
        spot=100.0,
        notional_usd=300.0,
        underlying_target=112.0,
        underlying_stop=96.0,
        confidence=0.9,
    )

    assert synthesize_option_meta(**kwargs) is None
    assert synthesize_option_meta(**kwargs) is None
    assert chain_calls["count"] == 1


def test_synthesize_option_meta_records_quote_snapshots_when_db_available(monkeypatch):
    from app.services.trading.options import quote_store

    recorded = {"chains": 0, "quotes": 0}

    _wire_synthesis_fakes(
        monkeypatch,
        {
            105.0: {
                "bid_price": "1.45",
                "ask_price": "1.50",
                "mark_price": "1.48",
                "delta": "0.40",
            },
        },
    )

    def _create_chain_snapshot(*_args, **kwargs):
        recorded["chains"] += 1
        assert kwargs["underlying"] == "XYZ"
        assert kwargs["venue"] == "robinhood"
        return 77

    def _record_quote_snapshot(*_args, **kwargs):
        recorded["quotes"] += 1
        assert kwargs["chain_id"] == 77
        assert kwargs["option_meta"]["contract_key"].endswith(":call:105.000")
        assert kwargs["quote"]["ask_price"] == "1.50"
        return True

    monkeypatch.setattr(quote_store, "create_chain_snapshot", _create_chain_snapshot)
    monkeypatch.setattr(quote_store, "record_quote_snapshot", _record_quote_snapshot)

    meta = synthesize_option_meta(
        db=object(),
        underlying="XYZ",
        spot=100.0,
        notional_usd=300.0,
        underlying_target=112.0,
        underlying_stop=96.0,
        confidence=0.9,
    )

    assert meta is not None
    assert recorded == {"chains": 1, "quotes": 1}
    assert meta["chain_snapshot_id"] == 77
    assert meta["quote_snapshot_recorded"] is True
