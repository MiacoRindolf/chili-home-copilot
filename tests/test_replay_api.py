"""Replay v2 API — run/status/list/result endpoints (engine mocked; it is minutes-heavy)."""
from __future__ import annotations

from unittest.mock import patch


def test_replay_status_idle(client):
    r = client.get("/api/trading/momentum/replay/status")
    assert r.status_code == 200
    assert r.json()["job"]["state"] in ("idle", "done", "error", "running")


def test_replay_run_rejects_bad_date(client):
    r = client.post("/api/trading/momentum/replay/run", json={"date": "garbage"})
    assert r.status_code == 400


def test_replay_run_starts_thread_and_single_flights(client):
    import app.routers.trading_sub.replay_api as ra
    with patch.object(ra.threading, "Thread") as th:
        with ra._lock:
            ra._job.clear(); ra._job.update({"state": "idle"})
        r = client.post("/api/trading/momentum/replay/run", json={"date": "2026-06-10"})
        assert r.status_code == 200 and r.json()["ok"] is True
        th.assert_called_once()
        # second run while "running" -> refused
        r2 = client.post("/api/trading/momentum/replay/run", json={"date": "2026-06-10"})
        assert r2.json()["ok"] is False and r2.json()["error"] == "replay_already_running"
        with ra._lock:
            ra._job.clear(); ra._job.update({"state": "idle"})


def test_replay_list_and_missing_result(client, tmp_path, monkeypatch):
    import app.services.trading.momentum_neural.replay_v2 as rv
    monkeypatch.setattr(rv, "REPLAY_RESULTS_DIR", str(tmp_path))
    r = client.get("/api/trading/momentum/replay/list")
    assert r.status_code == 200 and r.json()["results"] == []
    r2 = client.get("/api/trading/momentum/replay/result/2020-01-01")
    assert r2.status_code == 404
    # persist one + read it back through the API
    rv._persist({"date": "2026-06-10", "total_usd": -575, "wins": 0, "losses": 1,
                 "trades": [{"sym": "BATL"}], "tape_symbols": 694, "candidates": 272,
                 "halt_windows": 4740, "day_halted": None, "error": None,
                 "ran_at_utc": "2026-06-10T23:00:00+00:00"})
    r3 = client.get("/api/trading/momentum/replay/list")
    assert len(r3.json()["results"]) == 1
    r4 = client.get("/api/trading/momentum/replay/result/2026-06-10")
    assert r4.status_code == 200 and r4.json()["result"]["total_usd"] == -575


def test_replay_result_roundtrips_divergence_and_trace(client, tmp_path, monkeypatch):
    import app.services.trading.momentum_neural.replay_v2 as rv
    monkeypatch.setattr(rv, "REPLAY_RESULTS_DIR", str(tmp_path))
    rv._persist({"date": "2026-06-10", "armed_source": "live", "total_usd": 275,
                 "wins": 2, "losses": 3, "trades": [{"sym": "BATL"}],
                 "decision_trace": [{"t": "15:16", "sym": "BATL", "stage": "trigger_ok"}],
                 "divergence": [{"sym": "BATL", "live": "15:17 entry_filled 1.63",
                                 "replay": "15:16 fill@1.64", "cause": "aligned"}],
                 "tape_symbols": 694, "candidates": 272, "halt_windows": 592,
                 "day_halted": None, "error": None,
                 "ran_at_utc": "2026-06-10T23:00:00+00:00"})
    r = client.get("/api/trading/momentum/replay/result/2026-06-10?armed_source=live")
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["divergence"][0]["cause"] == "aligned"
    assert res["decision_trace"][0]["stage"] == "trigger_ok"
