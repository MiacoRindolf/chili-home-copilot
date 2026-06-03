import json

from fastapi import BackgroundTasks

from app.routers.trading_sub import scanning
from app.services.trading import scanner as scanner_mod


def _json_body(response):
    return json.loads(response.body.decode("utf-8"))


def test_momentum_scan_returns_fresh_cache_without_background_refresh(monkeypatch):
    monkeypatch.setattr(
        scanner_mod,
        "get_momentum_cache",
        lambda: {
            "age_seconds": 30,
            "results": [{"ticker": "OBAI", "score": 8.5}],
            "candidates_scanned": 4,
            "total_sourced": 9,
            "immaculate_count": 1,
            "scan_time": "2026-06-03T08:00:00",
        },
    )
    monkeypatch.setattr(scanner_mod, "_brain_meta", lambda: {"ok": True})
    monkeypatch.setattr(
        scanning.ts,
        "get_intraday_scan_progress",
        lambda: {"running": False, "scan_type": ""},
    )

    def _unexpected_scan(*args, **kwargs):
        raise AssertionError("fresh cache should not trigger a momentum scan")

    monkeypatch.setattr(scanner_mod, "run_momentum_scanner", _unexpected_scan)

    tasks = BackgroundTasks()
    body = _json_body(scanning.api_run_momentum_scan(tasks))

    assert body["ok"] is True
    assert body["cached"] is True
    assert body["refreshing"] is False
    assert body["matches"] == 1
    assert body["results"][0]["ticker"] == "OBAI"
    assert tasks.tasks == []


def test_momentum_scan_returns_stale_cache_and_schedules_refresh(monkeypatch):
    monkeypatch.setattr(
        scanner_mod,
        "get_momentum_cache",
        lambda: {
            "age_seconds": 180,
            "results": [{"ticker": "ABC", "score": 7.5}],
            "candidates_scanned": 10,
            "total_sourced": 30,
            "immaculate_count": 0,
        },
    )
    monkeypatch.setattr(scanner_mod, "_brain_meta", lambda: {"ok": True})
    monkeypatch.setattr(
        scanning.ts,
        "get_intraday_scan_progress",
        lambda: {"running": False, "scan_type": ""},
    )
    monkeypatch.setattr(scanner_mod, "run_momentum_scanner", lambda max_results=10: {"ok": True})

    tasks = BackgroundTasks()
    body = _json_body(scanning.api_run_momentum_scan(tasks))

    assert body["ok"] is True
    assert body["cached"] is True
    assert body["refreshing"] is True
    assert body["results"][0]["ticker"] == "ABC"
    assert len(tasks.tasks) == 1


def test_momentum_scan_without_cache_returns_warming_up_json(monkeypatch):
    monkeypatch.setattr(
        scanner_mod,
        "get_momentum_cache",
        lambda: {"age_seconds": None, "results": []},
    )
    monkeypatch.setattr(scanner_mod, "_brain_meta", lambda: {"ok": True})
    monkeypatch.setattr(
        scanning.ts,
        "get_intraday_scan_progress",
        lambda: {"running": False, "scan_type": ""},
    )
    monkeypatch.setattr(scanner_mod, "run_momentum_scanner", lambda max_results=10: {"ok": True})

    tasks = BackgroundTasks()
    body = _json_body(scanning.api_run_momentum_scan(tasks))

    assert body["ok"] is True
    assert body["warming_up"] is True
    assert body["results"] == []
    assert len(tasks.tasks) == 1
