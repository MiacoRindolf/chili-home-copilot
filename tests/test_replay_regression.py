"""Nightly replay regression tripwire — the Replay Lab as a CI gate."""

from __future__ import annotations

from sqlalchemy.orm import Session

import app.services.trading.momentum_neural.replay_regression as rr


def test_regression_flags_replay_blind(db: Session, monkeypatch, tmp_path) -> None:
    # live filled today but the replay took zero trades -> the tripwire must fire
    monkeypatch.setattr(rr, "_live_actuals", lambda *_a, **_k: {
        "fills": 2, "symbols": ["INDP"], "realized_usd": -239.0, "top_blocks": [],
    })
    import app.services.trading.momentum_neural.replay_v2 as rv

    monkeypatch.setattr(rv, "run_replay", lambda *a, **k: {
        "trades": [], "total_usd": 0.0, "tape_symbols": 40, "live_sessions": 10,
    })
    monkeypatch.setattr(rv, "REPLAY_RESULTS_DIR", str(tmp_path))
    report = rr.run_nightly_replay_regression(db)
    assert any(f.startswith("replay_blind") for f in report["flags"]), report


def test_regression_clean_day_no_flags(db: Session, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(rr, "_live_actuals", lambda *_a, **_k: {
        "fills": 1, "symbols": ["BATL"], "realized_usd": 600.0, "top_blocks": [],
    })
    import app.services.trading.momentum_neural.replay_v2 as rv

    monkeypatch.setattr(rv, "run_replay", lambda *a, **k: {
        "trades": [{"sym": "BATL"}], "total_usd": 640.0, "tape_symbols": 40, "live_sessions": 10,
    })
    monkeypatch.setattr(rv, "REPLAY_RESULTS_DIR", str(tmp_path))
    report = rr.run_nightly_replay_regression(db)
    assert report["flags"] == [], report
    assert report["replay"]["trades"] == 1


def test_regression_survives_replay_crash(db: Session, monkeypatch) -> None:
    import app.services.trading.momentum_neural.replay_v2 as rv

    def _boom(*a, **k):
        raise RuntimeError("tape exploded")
    monkeypatch.setattr(rv, "run_replay", _boom)
    report = rr.run_nightly_replay_regression(db)
    assert "error" in report  # best-effort: reports the failure, never raises
