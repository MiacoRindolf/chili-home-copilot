import pytest

from app.services.trading import cash_deployment


def test_enqueue_cash_deployment_work_can_use_cached_snapshot_rows(monkeypatch):
    calls: list[dict] = []

    monkeypatch.setattr(
        cash_deployment,
        "enqueue_imminent_edge_snapshot_coverage_work",
        lambda *args, **kwargs: {
            "considered_slices": 0,
            "created": 0,
            "event_ids": [],
        },
    )
    monkeypatch.setattr(
        cash_deployment,
        "cash_deployment_rows",
        lambda *args, **kwargs: pytest.fail("producer should not run fresh edge compute"),
    )

    def _snapshot_rows(*args, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(cash_deployment, "cash_deployment_snapshot_rows", _snapshot_rows)
    monkeypatch.setattr(cash_deployment, "cash_deployment_null_lineage_candidates", lambda *args, **kwargs: [])

    out = cash_deployment.enqueue_cash_deployment_work(
        object(),
        window_days=7,
        limit=3,
        include_null_lineage=False,
        include_snapshot_coverage=False,
        use_snapshots=True,
    )

    assert out["row_source"] == "snapshot"
    assert out["created"] == 0
    assert calls == [{"user_id": None, "window_days": 7, "limit": 3}]
