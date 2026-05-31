from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5aa-market-data-anchor-parity-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5aa_market_data_anchor", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, rows=None, scalar_value=None):
        self._rows = rows or []
        self._scalar = scalar_value

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class _Db:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        rendered = str(sql)
        self.calls.append((rendered, params))
        if "pg_class" in rendered:
            return _Result(scalar_value="r")
        if "UNION" in rendered:
            return _Result(rows=[{"ticker": "ABC"}])
        if "trading_management_envelopes" in rendered:
            return _Result(
                rows=[
                    {
                        "id": 7,
                        "ticker": "ABC",
                        "entry_price": 12.5,
                        "entry_date": datetime(2026, 5, 31, 9, 30),
                    }
                ]
            )
        return _Result(
            rows=[
                {
                    "id": 7,
                    "ticker": "ABC",
                    "entry_price": 12.5,
                    "entry_date": datetime(2026, 5, 31, 9, 30),
                }
            ]
        )


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
        raise AssertionError("expected live database to require opt-in")


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


def test_anchor_row_from_relation_reads_management_envelopes_not_compat_view() -> None:
    module = _load_module()
    db = _Db()

    row = module._anchor_row_from_relation(
        db,
        relation_name=module.MANAGEMENT_ENVELOPES_RELATION,
        ticker="abc",
    )

    assert row.envelope_id == 7
    assert row.entry_price == 12.5
    sql, params = db.calls[-1]
    assert "FROM trading_management_envelopes" in sql
    assert "trading_trades" not in sql
    assert "status = 'open'" in sql
    assert params == {"ticker": "ABC"}


def test_run_probe_reports_complete_positive_when_anchors_match(monkeypatch) -> None:
    module = _load_module()
    db = _Db()

    monkeypatch.setattr(
        module,
        "_relation_kind",
        lambda _db, name: "r"
        if name == module.MANAGEMENT_ENVELOPES_RELATION
        else "v",
    )

    result = module.run_probe(db)

    assert result["status"] == "COMPLETE_POSITIVE"
    assert result["mismatches"] == 0
    assert result["tickers"] == 1
    assert result["comparisons"][0]["match"] is True


def test_run_probe_reports_alert_on_anchor_drift(monkeypatch) -> None:
    module = _load_module()

    class _DriftDb(_Db):
        def execute(self, sql, params=None):
            rendered = str(sql)
            if "trading_management_envelopes" in rendered and "UNION" not in rendered:
                return _Result(
                    rows=[
                        {
                            "id": 8,
                            "ticker": "ABC",
                            "entry_price": 13.0,
                            "entry_date": datetime(2026, 5, 31, 9, 30),
                        }
                    ]
                )
            return super().execute(sql, params)

    monkeypatch.setattr(
        module,
        "_relation_kind",
        lambda _db, name: "r"
        if name == module.MANAGEMENT_ENVELOPES_RELATION
        else "v",
    )

    result = module.run_probe(_DriftDb())

    assert result["status"] == "ALERT"
    assert result["mismatches"] == 1
    assert result["comparisons"][0]["match"] is False

