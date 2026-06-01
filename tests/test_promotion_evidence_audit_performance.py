from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.promotion_evidence_audit import audit_promoted_pattern_evidence


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.execute_count = 0

    def execute(self, _stmt):
        self.execute_count += 1
        return _Rows(self.rows)


def test_audit_promoted_pattern_evidence_counts_from_single_audit_query() -> None:
    rows = [
        SimpleNamespace(
            id=1,
            name="Lifecycle complete",
            lifecycle_stage="promoted",
            promotion_status=None,
            oos_win_rate=55.0,
            oos_trade_count=12,
            promotion_gate_passed=True,
            deflated_sharpe=1.1,
            cpcv_median_sharpe=1.4,
        ),
        SimpleNamespace(
            id=2,
            name="Legacy incomplete",
            lifecycle_stage="candidate",
            promotion_status="promoted",
            oos_win_rate=None,
            oos_trade_count=0,
            promotion_gate_passed=False,
            deflated_sharpe=None,
            cpcv_median_sharpe=None,
        ),
        SimpleNamespace(
            id=3,
            name="Both conventions incomplete",
            lifecycle_stage="live",
            promotion_status="promoted",
            oos_win_rate=50.0,
            oos_trade_count=5,
            promotion_gate_passed=True,
            deflated_sharpe=1.0,
            cpcv_median_sharpe=None,
        ),
    ]
    db = _Db(rows)

    summary = audit_promoted_pattern_evidence(db)

    assert db.execute_count == 1
    assert summary["promoted_count_lifecycle"] == 2
    assert summary["promoted_count_legacy"] == 2
    assert summary["audit_universe_size"] == 3
    assert summary["evidence_complete"] == 1
    assert summary["evidence_incomplete"] == 2
    assert summary["by_missing_field"]["cpcv_median_sharpe_null"] == 2
    assert summary["incomplete_ids"] == [2, 3]
