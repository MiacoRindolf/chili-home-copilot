from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5aa-active-setup-runtime-adapter-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5aa_active_setup_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_active_setup_fields_pin_public_card_contract() -> None:
    module = _load_module()

    assert module.ACTIVE_SETUP_FIELDS == (
        "trade_id",
        "ticker",
        "direction",
        "pattern_name",
        "plan_label",
        "pattern_id",
        "timeframe",
        "entry_price",
        "quantity",
        "stop_loss",
        "take_profit",
        "entry_date",
        "current_price",
        "quote_source",
        "pnl_pct",
        "broker_truth_entry_price",
        "broker_truth_quantity",
        "broker_truth_position_id",
        "broker_truth_current_envelope_id",
        "broker_truth_metrics_source",
        "latest_decision",
        "decision_count",
        "recent_decisions",
        "execution_state",
        "execution_label",
        "execution_reason",
        "pending_exit_status",
        "pending_exit_order_id",
        "pending_exit_limit_price",
        "next_eligible_session_at",
    )


def test_normalize_stabilizes_dates_decimals_and_nested_values() -> None:
    module = _load_module()

    assert module._normalize(
        {
            "ts": datetime(2026, 5, 30, 21, 15),
            "px": Decimal("4.2500"),
            "nested": [{"b": 2, "a": 1}],
        }
    ) == {
        "nested": [{"a": 1, "b": 2}],
        "px": 4.25,
        "ts": "2026-05-30T21:15:00",
    }


def test_load_envelope_objects_reads_management_envelopes_not_compat_view(monkeypatch) -> None:
    module = _load_module()

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [{"id": 1, "ticker": "ABC", "entry_price": 10.0}]

    class _Db:
        sql = ""
        params = None

        def execute(self, sql, params=None):
            self.sql = str(sql)
            self.params = params
            return _Rows()

    monkeypatch.setattr(
        "app.services.trading.broker_position_truth.filter_broker_stale_open_trades",
        lambda _db, rows: (rows, []),
    )
    db = _Db()

    rows, suppressed = module.load_envelope_objects(db, user_id=7)

    assert suppressed == []
    assert rows[0].id == 1
    assert rows[0].ticker == "ABC"
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'open'" in db.sql
    assert "entry_price > 0" in db.sql
    assert db.params == {"uid": 7}


def test_fetch_quotes_for_tickers_uses_one_sorted_batch(monkeypatch) -> None:
    module = _load_module()
    calls: list[tuple[str, ...]] = []

    def fake_fetch(tickers, allow_provider_fallback=None):
        calls.append(tuple(tickers))
        return {ticker: {"price": 1.0, "source": "test"} for ticker in tickers}

    monkeypatch.setattr(module.monitor.ts, "fetch_quotes_batch", fake_fetch)
    cache: dict[tuple[str, ...], dict] = {}

    first = module._fetch_quotes_for_tickers(
        ["b", "A", "a"],
        batch_cache=cache,
        allow_external_quotes=True,
    )
    second = module._fetch_quotes_for_tickers(
        ["A", "B"],
        batch_cache=cache,
        allow_external_quotes=True,
    )

    assert first == second
    assert calls == [("A", "B")]


def test_fetch_quotes_for_tickers_is_stubbed_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()
    calls: list[tuple[str, ...]] = []

    def fake_fetch(tickers, allow_provider_fallback=None):
        calls.append(tuple(tickers))
        return {ticker: {"price": 1.0, "source": "test"} for ticker in tickers}

    monkeypatch.setattr(module.monitor.ts, "fetch_quotes_batch", fake_fetch)

    assert module._fetch_quotes_for_tickers(["A"], batch_cache={}) == {}
    assert calls == []


def test_probe_rejects_non_test_database_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)

    try:
        module._assert_probe_database_allowed(
            "postgresql://chili:chili@localhost:5433/chili"
        )
    except RuntimeError as exc:
        assert module.LIVE_PROBE_OPT_IN in str(exc)
    else:
        raise AssertionError("expected non-test database to require live opt-in")


def test_probe_allows_test_database_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)

    module._assert_probe_database_allowed(
        "postgresql://chili:chili@localhost:5433/chili_test"
    )


def test_probe_live_opt_in_allows_non_test_database(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.setenv(module.LIVE_PROBE_OPT_IN, "true")

    module._assert_probe_database_allowed(
        "postgresql://chili:chili@localhost:5433/chili"
    )
