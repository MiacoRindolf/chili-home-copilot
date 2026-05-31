from collections import OrderedDict
from types import SimpleNamespace

from app.services.trading import pattern_imminent_alerts as imminent


class _NoSnapshotOrderedDict(OrderedDict):
    def items(self):
        raise AssertionError("cache pruning should inspect only the oldest live entry")


def test_top_pending_shadow_near_misses_matches_full_sort_with_stable_ties(monkeypatch) -> None:
    monkeypatch.setattr(
        imminent,
        "_imminent_scan_priority_key",
        lambda pattern: pattern.priority,
    )
    rows = [
        {
            "ticker": "A",
            "pattern": SimpleNamespace(priority=(1, 1)),
            "readiness_gap_to_min": 0.05,
            "readiness": 0.71,
            "coverage_ratio": 0.8,
        },
        {
            "ticker": "B",
            "pattern": SimpleNamespace(priority=(1, 1)),
            "readiness_gap_to_min": 0.02,
            "readiness": 0.69,
            "coverage_ratio": 0.9,
        },
        {
            "ticker": "C",
            "pattern": SimpleNamespace(priority=(1, 1)),
            "readiness_gap_to_min": 0.02,
            "readiness": 0.69,
            "coverage_ratio": 0.9,
        },
        {
            "ticker": "D",
            "pattern": SimpleNamespace(priority=(0, 9)),
            "readiness_gap_to_min": 0.02,
            "readiness": 0.66,
            "coverage_ratio": 0.95,
        },
    ]

    expected = sorted(rows, key=imminent._pending_shadow_near_miss_priority)[:3]

    assert imminent._top_pending_shadow_near_misses(rows, 3) == expected


def test_top_readiness_band_near_misses_matches_full_sort_with_stable_ties() -> None:
    rows = [
        {"ticker": "A", "gap": 0.04, "readiness": 0.72},
        {"ticker": "B", "gap": 0.01, "readiness": 0.65},
        {"ticker": "C", "gap": 0.01, "readiness": 0.65},
        {"ticker": "D", "gap": 0.01, "readiness": 0.7},
    ]

    expected = sorted(rows, key=imminent._readiness_band_near_miss_priority)[:3]

    assert imminent._top_readiness_band_near_misses(rows, 3) == expected


def test_pattern_imminent_near_miss_helpers_empty_for_non_positive_limits() -> None:
    row = {"ticker": "A", "gap": 0.1, "readiness": 0.5}

    assert imminent._top_readiness_band_near_misses([row], 0) == []
    assert imminent._top_pending_shadow_near_misses([{"pattern": object()}], 0) == []


def test_score_failure_cooldown_hit_refreshes_recency(monkeypatch) -> None:
    monkeypatch.setattr(imminent, "_score_failure_cooldown_enabled", lambda: True)
    monkeypatch.setattr(imminent._time, "monotonic", lambda: 10.0)
    imminent._SCORE_FAILURE_CACHE.clear()
    imminent._SCORE_FAILURE_CACHE["A"] = {"failures": 1, "cooldown_until": 20.0}
    imminent._SCORE_FAILURE_CACHE["B"] = {"failures": 1, "cooldown_until": 20.0}

    assert imminent._score_failure_cooldown_active("a") is True

    assert list(imminent._SCORE_FAILURE_CACHE) == ["B", "A"]


def test_score_failure_cache_prunes_expired_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(imminent, "_score_failure_cooldown_enabled", lambda: True)
    monkeypatch.setattr(imminent, "_score_failure_cooldown_minutes", lambda: 1.0)
    monkeypatch.setattr(imminent, "_score_failure_min_failures", lambda: 1)
    monkeypatch.setattr(imminent._time, "monotonic", lambda: 100.0)
    cache = _NoSnapshotOrderedDict(
        [
            ("OLD", {"failures": 1, "cooldown_until": 99.0}),
            ("LIVE", {"failures": 1, "cooldown_until": 200.0}),
        ]
    )
    monkeypatch.setattr(imminent, "_SCORE_FAILURE_CACHE", cache)

    imminent._record_score_failure("NEW")

    assert list(cache) == ["LIVE", "NEW"]


def test_record_score_failure_caps_oldest_entries(monkeypatch) -> None:
    monkeypatch.setattr(imminent, "_score_failure_cooldown_enabled", lambda: True)
    monkeypatch.setattr(imminent, "_score_failure_cooldown_minutes", lambda: 1.0)
    monkeypatch.setattr(imminent, "_score_failure_min_failures", lambda: 1)
    monkeypatch.setattr(imminent, "_SCORE_FAILURE_CACHE_MAX", 2)
    monkeypatch.setattr(imminent._time, "monotonic", lambda: 100.0)
    imminent._SCORE_FAILURE_CACHE.clear()
    imminent._SCORE_FAILURE_CACHE["A"] = {"failures": 1, "cooldown_until": 200.0}
    imminent._SCORE_FAILURE_CACHE["B"] = {"failures": 1, "cooldown_until": 200.0}

    imminent._record_score_failure("C")

    assert list(imminent._SCORE_FAILURE_CACHE) == ["B", "C"]
