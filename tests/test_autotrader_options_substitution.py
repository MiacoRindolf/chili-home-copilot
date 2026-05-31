from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config import AUTOTRADER_OPTIONS_SUBSTITUTE_DEFAULT_REQUIRES_UNDERLYING_POSITIVE_EDGE
from app.services.trading import auto_trader as at_mod


REPO = Path(__file__).resolve().parent.parent


def _stock_alert() -> SimpleNamespace:
    return SimpleNamespace(
        id=101,
        ticker="XYZ",
        asset_type="stock",
        entry_price=100.0,
        target_price=112.0,
        stop_loss=96.0,
        score_at_alert=0.9,
        scan_pattern_id=501,
        indicator_snapshot={},
    )


def test_options_substitute_skips_synthesis_when_underlying_edge_is_negative(monkeypatch):
    alert = _stock_alert()
    synth_calls = {"count": 0}

    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_options_substitute_enabled",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_options_substitute_requires_underlying_positive_edge",
        AUTOTRADER_OPTIONS_SUBSTITUTE_DEFAULT_REQUIRES_UNDERLYING_POSITIVE_EDGE,
    )
    monkeypatch.setattr(at_mod, "resolve_pattern_signal_context", lambda *_a, **_k: {})
    monkeypatch.setattr(
        at_mod,
        "evaluate_entry_edge",
        lambda *_a, **_k: SimpleNamespace(
            allowed=False,
            reason="non_positive_expected_edge",
            snapshot={"expected_net_pct": -0.15},
        ),
    )

    def _synthesize_option_meta(**_kwargs):
        synth_calls["count"] += 1
        return {"limit_price": 1.23}

    from app.services.trading.options import synthesis

    monkeypatch.setattr(synthesis, "synthesize_option_meta", _synthesize_option_meta)

    at_mod._maybe_substitute_with_options(None, alert, 100.0, uid=1)

    assert synth_calls["count"] == 0
    assert alert.asset_type == "stock"
    assert (
        alert.indicator_snapshot["options_substitution_underlying_edge_reason"]
        == "non_positive_expected_edge"
    )


def test_options_substitute_skips_shadow_observation_alerts(monkeypatch):
    alert = _stock_alert()
    alert._chili_shadow_observation_only = True
    synth_calls = {"count": 0}

    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_options_substitute_enabled",
        True,
    )
    monkeypatch.setattr(
        at_mod,
        "evaluate_entry_edge",
        lambda *_a, **_k: SimpleNamespace(
            allowed=True,
            reason="positive_expected_edge",
            snapshot={"expected_net_pct": 0.25},
        ),
    )

    def _synthesize_option_meta(**_kwargs):
        synth_calls["count"] += 1
        return {"limit_price": 1.23}

    from app.services.trading.options import synthesis

    monkeypatch.setattr(synthesis, "synthesize_option_meta", _synthesize_option_meta)

    at_mod._maybe_substitute_with_options(None, alert, 100.0, uid=1)

    assert synth_calls["count"] == 0
    assert alert.asset_type == "stock"


def test_options_substitute_runs_after_underlying_positive_edge(monkeypatch):
    alert = _stock_alert()
    synth_calls = {"count": 0}

    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_options_substitute_enabled",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_options_substitute_requires_underlying_positive_edge",
        AUTOTRADER_OPTIONS_SUBSTITUTE_DEFAULT_REQUIRES_UNDERLYING_POSITIVE_EDGE,
    )
    monkeypatch.setattr(at_mod, "resolve_pattern_signal_context", lambda *_a, **_k: {})
    monkeypatch.setattr(
        at_mod,
        "evaluate_entry_edge",
        lambda *_a, **_k: SimpleNamespace(
            allowed=True,
            reason="positive_expected_edge",
            snapshot={"expected_net_pct": 0.25},
        ),
    )
    monkeypatch.setattr(
        at_mod,
        "_resolve_entry_risk_notional",
        lambda *_a, **_k: (300.0, {"notional_source": "test"}),
    )

    def _synthesize_option_meta(**_kwargs):
        synth_calls["count"] += 1
        return {
            "expiration": "2026-06-18",
            "strike": 105.0,
            "option_type": "call",
            "quantity": 1,
            "limit_price": 1.23,
        }

    from app.services.trading.options import synthesis

    monkeypatch.setattr(synthesis, "synthesize_option_meta", _synthesize_option_meta)

    at_mod._maybe_substitute_with_options(None, alert, 100.0, uid=1)

    assert synth_calls["count"] == 1
    assert alert.asset_type == "options"
    assert alert.entry_price == 1.23
    assert alert.indicator_snapshot["options_path"] is True
    assert alert.indicator_snapshot["asset_kind"] == "option"
    assert alert.indicator_snapshot["option_contract_key"] == (
        "XYZ:2026-06-18:call:105.000"
    )
    assert alert.indicator_snapshot["option_meta"]["strike"] == 105.0
    assert alert.indicator_snapshot["option_meta"]["price_domain"] == "option_premium"


def test_options_alert_skips_equity_llm_revalidation(monkeypatch):
    alert = _stock_alert()
    alert.asset_type = "options"

    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_llm_revalidation_enabled",
        True,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_llm_revalidation_skip_options_path",
        True,
    )

    should_run, reason = at_mod._should_run_llm_revalidation(alert)

    assert should_run is False
    assert reason == at_mod.LLM_REVALIDATION_SKIP_REASON_OPTIONS_PATH


def test_option_entry_fill_price_never_falls_back_to_underlying_spot():
    alert = _stock_alert()
    alert.asset_type = "options"
    alert.entry_price = 4.01
    snap = {
        "options_path": True,
        "option_meta": {
            "underlying": "SPY",
            "strike": 729.0,
            "expiration": "2026-05-15",
            "option_type": "call",
            "limit_price": 4.01,
        },
    }

    fill = at_mod._entry_fill_price_from_response(
        {"ok": True, "order_id": "oid", "raw": {"state": "queued"}},
        alert,
        px=715.37,
        snap=snap,
    )

    assert fill == 4.01


def test_entry_broker_fill_price_ignores_option_limit_fallback_for_tca():
    fill = at_mod._entry_broker_fill_price_from_response(
        {
            "ok": True,
            "order_id": "oid",
            "_chili_options_path": True,
            "limit_price": 4.01,
            "raw": {"state": "queued"},
        }
    )

    assert fill is None


def test_entry_broker_fill_price_reads_average_fill_for_tca():
    fill = at_mod._entry_broker_fill_price_from_response(
        {
            "ok": True,
            "order_id": "oid",
            "raw": {"state": "filled", "average_price": "4.05"},
        }
    )

    assert fill == 4.05


def test_option_tca_reference_uses_option_limit_not_underlying_or_fill():
    alert = _stock_alert()
    alert.asset_type = "options"
    alert.entry_price = 1.26
    snap = {
        "options_path": True,
        "option_meta": {
            "underlying": "SPY",
            "strike": 729.0,
            "expiration": "2026-05-15",
            "option_type": "call",
            "limit_price": 1.25,
        },
    }

    ref = at_mod._entry_tca_reference_price(
        {
            "ok": True,
            "_chili_options_path": True,
            "price": 1.30,
            "limit_price": 1.25,
        },
        alert,
        px=715.37,
        snap=snap,
        fill=1.30,
    )

    assert ref == 1.25


def test_stock_tca_reference_uses_underlying_decision_price():
    alert = _stock_alert()

    ref = at_mod._entry_tca_reference_price(
        {"ok": True, "limit_price": 1.25},
        alert,
        px=100.50,
        snap={},
        fill=100.55,
    )

    assert ref == 100.50


def test_option_queued_order_stays_working_until_position_truth():
    status, broker_status, filled_qty, remaining_qty = (
        at_mod._entry_lifecycle_from_response(
            broker_source="robinhood",
            res={"ok": True, "order_id": "oid", "state": "queued"},
            snap={"options_path": True},
            qty=1.0,
        )
    )

    assert status == "working"
    assert broker_status == "queued"
    assert filled_qty == 0.0
    assert remaining_qty == 1.0


def test_option_partial_entry_uses_processed_quantity():
    status, broker_status, filled_qty, remaining_qty = (
        at_mod._entry_lifecycle_from_response(
            broker_source="robinhood",
            res={
                "ok": True,
                "order_id": "oid",
                "state": "partially_filled",
                "processed_quantity": "1",
            },
            snap={"options_path": True},
            qty=2.0,
        )
    )

    assert status == "working"
    assert broker_status == "partially_filled"
    assert filled_qty == 1.0
    assert remaining_qty == 1.0


def test_option_terminal_partial_entry_preserves_filled_contract_only():
    status, broker_status, filled_qty, remaining_qty = (
        at_mod._entry_lifecycle_from_response(
            broker_source="robinhood",
            res={
                "ok": True,
                "order_id": "oid",
                "state": "cancelled",
                "processed_quantity": "1",
            },
            snap={"options_path": True},
            qty=2.0,
        )
    )
    trade_qty = at_mod._entry_quantity_for_trade(
        is_option_entry=True,
        requested_qty=2.0,
        entry_broker_status=broker_status,
        entry_filled_qty=filled_qty,
    )

    assert status == "open"
    assert broker_status == "partially_filled_cancelled"
    assert filled_qty == 1.0
    assert remaining_qty == 0.0
    assert trade_qty == 1.0


def test_option_terminal_zero_fill_is_cancelled_not_open_zero_quantity():
    status, broker_status, filled_qty, remaining_qty = (
        at_mod._entry_lifecycle_from_response(
            broker_source="robinhood",
            res={
                "ok": True,
                "order_id": "oid",
                "state": "cancelled",
                "processed_quantity": "0",
            },
            snap={"options_path": True},
            qty=2.0,
        )
    )

    assert status == "cancelled"
    assert broker_status == "cancelled"
    assert filled_qty == 0.0
    assert remaining_qty == 0.0


def test_option_entry_rejects_invalid_contract_quantity_instead_of_defaulting_to_one():
    src = (REPO / "app/services/trading/auto_trader.py").read_text()

    assert "parse_contract_quantity(_opt_meta.get(\"quantity\"))" in src
    assert "options_meta_invalid_quantity" in src
    assert "int(_opt_meta.get(\"quantity\") or 1)" not in src


def test_option_terminal_complete_entry_fill_opens_requested_contracts():
    status, broker_status, filled_qty, remaining_qty = (
        at_mod._entry_lifecycle_from_response(
            broker_source="robinhood",
            res={
                "ok": True,
                "order_id": "oid",
                "state": "cancelled",
                "processed_quantity": "2",
            },
            snap={"options_path": True},
            qty=2.0,
        )
    )
    trade_qty = at_mod._entry_quantity_for_trade(
        is_option_entry=True,
        requested_qty=2.0,
        entry_broker_status=broker_status,
        entry_filled_qty=filled_qty,
    )

    assert status == "open"
    assert broker_status == "filled"
    assert filled_qty == 2.0
    assert remaining_qty == 0.0
    assert trade_qty == 2.0
