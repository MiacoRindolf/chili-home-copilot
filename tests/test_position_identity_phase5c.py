"""Position-identity Phase 5C reporting-reader adoption."""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from app import migrations
from app.routers.trading_sub import trades as trades_router
from app.services.trading.attribution_service import _phase5b_pattern_attribution_compare


class _FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


def test_phase5c_migration_registered_after_coinbase_repair():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]
    assert "273_coinbase_envelope_position_backfill" in ids
    assert "274_position_identity_phase5c_attribution_columns" in ids
    assert ids.index("274_position_identity_phase5c_attribution_columns") == (
        ids.index("273_coinbase_envelope_position_backfill") + 1
    )


def test_phase5c_view_adds_decision_and_envelope_pattern_columns():
    src = inspect.getsource(
        migrations._migration_274_position_identity_phase5c_attribution_columns
    )
    assert "CREATE OR REPLACE VIEW trading_phase5b_decision_envelope_position" in src
    assert "d.scan_pattern_id AS decision_scan_pattern_id" in src
    assert "e.scan_pattern_id AS envelope_scan_pattern_id" in src
    assert "pattern_attribution_mismatches" in src
    assert "ALTER TABLE trading_trades RENAME" not in src


def test_live_vs_research_endpoint_keeps_phase5c_compare_opt_in():
    src = inspect.getsource(trades_router.api_attribution_live_vs_research)
    assert "phase5b_compare: bool = Query(False)" in src
    assert "include_phase5b_compare=phase5b_compare" in src


def test_phase5c_compare_reports_decision_vs_envelope_drift():
    grouped = [
        {
            "attribution_source": "envelope",
            "scan_pattern_id": 585,
            "closed_envelopes": 3,
            "total_pnl": 42.5,
            "avg_pnl": 14.1667,
        },
        {
            "attribution_source": "decision",
            "scan_pattern_id": None,
            "closed_envelopes": 1,
            "total_pnl": 10.0,
            "avg_pnl": 10.0,
        },
        {
            "attribution_source": "decision",
            "scan_pattern_id": 585,
            "closed_envelopes": 2,
            "total_pnl": 32.5,
            "avg_pnl": 16.25,
        },
    ]
    mismatches = [
        {
            "decision_scan_pattern_id": None,
            "envelope_scan_pattern_id": 585,
            "closed_envelopes": 1,
            "total_pnl": 10.0,
        }
    ]
    db = MagicMock()
    db.execute.side_effect = [_FakeResult(grouped), _FakeResult(mismatches)]

    out = _phase5b_pattern_attribution_compare(db, user_id=1, days=30, limit=10)

    assert out["enabled"] is True
    assert out["legacy_attribution"] == "envelope_scan_pattern_id"
    assert out["phase5b_attribution"] == "decision_scan_pattern_id"
    assert out["summary"]["mismatched_pattern_groups"] == 1
    assert out["summary"]["mismatched_closed_envelopes"] == 1
    assert out["summary"]["null_decision_pattern_envelopes"] == 1
    assert out["summary"]["absolute_group_pnl_delta"] == 20.0
    assert out["by_envelope_pattern"][0]["scan_pattern_id"] == 585
    assert out["by_decision_pattern"][0]["scan_pattern_id"] is None
    assert out["attribution_mismatches"][0] == {
        "decision_scan_pattern_id": None,
        "envelope_scan_pattern_id": 585,
        "closed_envelopes": 1,
        "total_pnl": 10.0,
    }

    sql = "\n".join(str(call.args[0]) for call in db.execute.call_args_list)
    assert "trading_phase5b_decision_envelope_position" in sql
    assert "decision_scan_pattern_id" in sql
    assert "envelope_scan_pattern_id" in sql
