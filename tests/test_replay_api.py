"""Replay v2 API — run/status/list/result endpoints (engine mocked; it is minutes-heavy)."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch


def test_replay_status_idle(client):
    r = client.get("/api/trading/momentum/replay/status")
    assert r.status_code == 200
    assert r.json()["job"]["state"] in ("idle", "done", "error", "running")


def test_replay_run_rejects_bad_date(client):
    r = client.post("/api/trading/momentum/replay/run", json={"date": "garbage"})
    assert r.status_code == 400


def test_replay_page_exposes_research_fsm_console(client):
    r = client.get("/trading/replay")
    assert r.status_code == 200
    assert b"Replay Research" in r.content
    assert b"Replay v3" in r.content
    assert b"Live FSM" in r.content
    assert b"/api/trading/momentum/replay/fsm" in r.content


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
    r2 = client.get("/api/trading/momentum/replay/result/2020-01-01?armed_source=asof")
    assert r2.status_code == 404
    # persist one + read it back through the API
    rv._persist({"date": "2026-06-10", "total_usd": -575, "wins": 0, "losses": 1,
                 "trades": [{"sym": "BATL"}], "tape_symbols": 694, "candidates": 272,
                 "halt_windows": 4740, "day_halted": None, "error": None,
                 "ran_at_utc": "2026-06-10T23:00:00+00:00"})
    r3 = client.get("/api/trading/momentum/replay/list")
    assert len(r3.json()["results"]) == 1
    r4 = client.get("/api/trading/momentum/replay/result/2026-06-10?armed_source=asof")
    assert r4.status_code == 200 and r4.json()["result"]["total_usd"] == -575


def test_replay_v3_day_result_loads_as_live_fsm(client, tmp_path, monkeypatch):
    import app.routers.trading_sub.replay_api as ra
    import app.services.trading.momentum_neural.replay_v2 as rv

    monkeypatch.setattr(rv, "REPLAY_RESULTS_DIR", str(tmp_path))
    ra._persist_v3_day({
        "date": "2026-07-09",
        "armed_symbol_count": 2,
        "traded_session_count": 1,
        "recorded_day_pnl_usd": 12.34,
        "replay_day_pnl_band_usd": {
            "low_conservative": 10.0,
            "point": 11.0,
            "high_optimistic": 12.0,
        },
        "per_trade": [{
            "session_id": 77,
            "symbol": "TEST",
            "trace_matches": True,
            "recorded_entry_ts": "2026-07-09T13:31:00",
            "recorded_entry": 1.1,
            "recorded_exit_ts": "2026-07-09T13:36:00",
            "recorded_exit": 1.2,
            "recorded_pnl_usd": 12.34,
        }],
    })
    rv._persist({
        "date": "2026-07-09",
        "armed_source": "live",
        "total_usd": -99,
        "wins": 0,
        "losses": 1,
        "trades": [{"sym": "OLD", "usd": -99}],
        "decision_trace": [],
        "series": {},
        "tape_symbols": 1,
        "candidates": 1,
        "halt_windows": 0,
        "day_halted": None,
        "error": None,
        "ran_at_utc": "2026-07-09T23:00:00+00:00",
    })

    r = client.get("/api/trading/momentum/replay/result/2026-07-09?armed_source=live_fsm")
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["engine"] == "v3_day"
    assert result["armed_source"] == "live_fsm"
    assert result["total_usd"] == 12.34
    assert result["trades"][0]["sym"] == "TEST"
    assert result["replay_v3_day"]["traded_session_count"] == 1
    assert any(row["stage"].startswith("v3 live entry") for row in result["decision_trace"])


def test_replay_day_truth_sums_full_day_live_outcomes(db):
    import app.routers.trading_sub.replay_api as ra
    from app.models.core import User
    from app.models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, TradingAutomationSession

    user = User(name="replay-truth")
    variant = MomentumStrategyVariant(family="ross", variant_key="truth_v", label="truth", params_json={})
    db.add_all([user, variant])
    db.flush()

    def session(symbol, *, family="robinhood_spot"):
        row = TradingAutomationSession(
            user_id=int(user.id),
            symbol=symbol,
            mode="live",
            state="live_exited",
            execution_family=family,
            variant_id=int(variant.id),
        )
        db.add(row)
        db.flush()
        return row

    vrax = session("VRAX")
    rkto = session("RKTO")
    alpaca = session("FAKE", family="alpaca_spot")
    db.add_all([
        MomentumAutomationOutcome(
            session_id=int(vrax.id), user_id=int(user.id), variant_id=int(variant.id),
            symbol="VRAX", mode="live", execution_family="robinhood_spot",
            terminal_state="live_exited", terminal_at=datetime(2026, 7, 9, 15, 0),
            outcome_class="target_hit", realized_pnl_usd=362.0,
        ),
        MomentumAutomationOutcome(
            session_id=int(rkto.id), user_id=int(user.id), variant_id=int(variant.id),
            symbol="RKTO", mode="live", execution_family="robinhood_spot",
            terminal_state="live_exited", terminal_at=datetime(2026, 7, 9, 20, 0),
            outcome_class="target_hit", realized_pnl_usd=100.0,
            broker_recon_status="reconciled", broker_realized_pnl_usd=845.0,
        ),
        MomentumAutomationOutcome(
            session_id=int(alpaca.id), user_id=int(user.id), variant_id=int(variant.id),
            symbol="FAKE", mode="live", execution_family="alpaca_spot",
            terminal_state="live_exited", terminal_at=datetime(2026, 7, 9, 16, 0),
            outcome_class="target_hit", realized_pnl_usd=999.0,
        ),
    ])
    db.flush()

    truth = ra._day_truth_for_date(db, date="2026-07-09", user_id=int(user.id))
    assert truth["available"] is True
    assert truth["total_usd"] == 2206.0
    assert truth["trades"] == 3
    assert truth["source_counts"] == {"automation_outcome": 2, "broker_reconciled": 1}
    assert {row["symbol"] for row in truth["symbols"]} == {"VRAX", "RKTO", "FAKE"}

    unpaired_truth = ra._day_truth_for_date(db, date="2026-07-09", user_id=None)
    assert unpaired_truth["available"] is True
    assert unpaired_truth["scope"] == "all_users"
    assert unpaired_truth["total_usd"] == truth["total_usd"]
    assert unpaired_truth["trades"] == truth["trades"]


def test_replay_day_truth_gap_marks_outside_shown_symbols():
    import app.routers.trading_sub.replay_api as ra

    result = {
        "engine": "v3_day",
        "trades": [{"sym": "VRAX", "usd": 362.0}],
    }
    day_truth = {
        "available": True,
        "total_usd": 1207.0,
        "trades": 2,
        "symbols": [
            {"symbol": "VRAX", "total_usd": 362.0, "trades": 1},
            {"symbol": "RKTO", "total_usd": 845.0, "trades": 1},
        ],
    }

    out = ra._attach_pnl_context(result, day_truth)
    assert out["shown_subset"]["total_usd"] == 362.0
    assert out["day_truth_gap"]["delta_usd"] == 845.0
    assert out["day_truth_gap"]["outside_symbols"][0]["symbol"] == "RKTO"
    assert out["day_truth_gap"]["shown_is_full_day"] is False


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
