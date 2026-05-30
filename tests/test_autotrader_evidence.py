from datetime import datetime
from types import SimpleNamespace

from app.services.trading.autotrader_evidence import (
    PHASE5K_D_PDT_RUNTIME_WRITE_QUARANTINE,
    classify_autotrader_run_evidence,
    clean_autotrader_runs,
    partition_autotrader_runs,
)
from app.services.trading.edge_reliability import _candidate_pattern_ids_from_recent_runs


def _run(**kwargs):
    defaults = {
        "decision": "placed",
        "reason": "normal_entry",
        "management_scope": None,
        "ticker": "SPY",
        "rule_snapshot": {},
        "llm_snapshot": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_classifies_plain_autotrader_rows_as_clean_evidence():
    row = _run(reason="non_positive_expected_edge")

    assert classify_autotrader_run_evidence(row).clean is True


def test_classifies_phase5k_runtime_churn_rows_as_quarantined():
    row = _run(
        rule_snapshot={
            "consumer_policy": {
                "required_bucket": PHASE5K_D_PDT_RUNTIME_WRITE_QUARANTINE,
            }
        }
    )

    classification = classify_autotrader_run_evidence(row)

    assert classification.clean is False
    assert classification.reason == PHASE5K_D_PDT_RUNTIME_WRITE_QUARANTINE


def test_classifies_pm_incident_markers_as_quarantined():
    row = _run(reason="PM-057 broker truth incident evidence")

    classification = classify_autotrader_run_evidence(row)

    assert classification.clean is False
    assert classification.reason == "pm057"


def test_partitions_autotrader_rows_and_reports_reason_counts():
    clean = _run(reason="missed_entry_slippage")
    pdt = _run(reason=PHASE5K_D_PDT_RUNTIME_WRITE_QUARANTINE)
    pm025 = _run(llm_snapshot={"governance": ["PM-025 DB/broker trust blocked"]})

    partition = partition_autotrader_runs([clean, pdt, pm025])

    assert partition.clean == [clean]
    assert partition.quarantined == [pdt, pm025]
    assert partition.to_summary() == {
        "total_count": 3,
        "clean_count": 1,
        "quarantined_count": 2,
        "reason_counts": {
            PHASE5K_D_PDT_RUNTIME_WRITE_QUARANTINE: 1,
            "pm025": 1,
        },
    }
    assert clean_autotrader_runs([clean, pdt, pm025]) == [clean]


def test_candidate_pattern_ranking_ignores_quarantined_autotrader_rows():
    clean = _run(
        scan_pattern_id=7,
        created_at=None,
        rule_snapshot={"entry_edge": {"expected_net_pct": 0.1}},
    )
    quarantined = _run(
        scan_pattern_id=99,
        created_at=None,
        reason=PHASE5K_D_PDT_RUNTIME_WRITE_QUARANTINE,
        rule_snapshot={"entry_edge": {"expected_net_pct": 99.0}},
    )

    class Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def all(self):
            return [quarantined, clean]

    class DB:
        def query(self, *args, **kwargs):
            return Query()

    assert _candidate_pattern_ids_from_recent_runs(
        DB(),
        cutoff=datetime(2026, 5, 30),
        limit=10,
    ) == [7]
