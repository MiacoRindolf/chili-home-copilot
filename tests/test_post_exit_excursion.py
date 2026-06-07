"""Post-exit excursion + shake-out classification (correct learning labels)."""
from __future__ import annotations

from app.services.trading.momentum_neural.post_exit_excursion import (
    compute_post_exit_excursion,
)


def test_kaio_real_case_is_shakeout_not_loss():
    # The actual KAIO trade: stopped out at 0.040768, then ran to 0.04237 — PAST
    # the original target 0.042057. The learner must see "shakeout", not a loss.
    out = compute_post_exit_excursion(
        entry_price=0.041460,
        exit_price=0.040768,
        original_target=0.042057,
        original_stop=0.041162,
        side_long=True,
        future_high=0.042370,
        future_low=0.040700,
        exit_reason="stop",
        realized_pnl=-4.13,
    )
    assert out["ok"] is True
    assert out["counterfactual_target_hit"] is True
    assert out["outcome_class"] == "shakeout"
    assert out["setup_quality"] == 1.0          # setup WAS right
    assert out["stop_too_tight"] is True         # the stop is the thing to fix
    assert out["post_exit_mfe_pct"] > 0


def test_premature_stop_reversed_but_no_target():
    out = compute_post_exit_excursion(
        entry_price=100.0, exit_price=98.0, original_target=104.0, original_stop=97.5,
        side_long=True, future_high=100.5, future_low=97.5, exit_reason="stop",
        realized_pnl=-2.0,
    )
    assert out["outcome_class"] == "premature_stop"
    assert out["stop_too_tight"] is True
    assert out["setup_quality"] == 0.6


def test_thesis_invalidated_no_recovery():
    out = compute_post_exit_excursion(
        entry_price=100.0, exit_price=98.0, original_target=104.0, original_stop=97.5,
        side_long=True, future_high=98.5, future_low=92.0, exit_reason="stop",
        realized_pnl=-2.0,
    )
    assert out["outcome_class"] == "thesis_invalidated"
    assert out["setup_quality"] == 0.0          # setup was genuinely wrong
    assert out["stop_too_tight"] is False


def test_target_win_is_setup_success():
    out = compute_post_exit_excursion(
        entry_price=100.0, exit_price=104.0, original_target=104.0, original_stop=98.0,
        side_long=True, future_high=105.0, future_low=103.0, exit_reason="target",
        realized_pnl=4.0,
    )
    assert out["outcome_class"] == "target_win"
    assert out["setup_quality"] == 1.0
    assert out["stop_too_tight"] is False


def test_invalid_prices_guard():
    out = compute_post_exit_excursion(
        entry_price=0.0, exit_price=1.0, original_target=2.0, original_stop=0.5,
        side_long=True, future_high=1.0, future_low=1.0, exit_reason="stop",
    )
    assert out["ok"] is False


def test_pass_labels_kaio_as_shakeout(monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import pandas as pd

    import app.services.trading.momentum_neural.post_exit_excursion as pex
    from app.services import coinbase_service  # noqa: F401 (ensure package import ok)

    now = datetime(2026, 6, 7, 3, 0, 0)
    pending = {
        "symbol": "KAIO-USD", "entry_price": 0.041460, "exit_price": 0.040768,
        "original_stop": 0.041162, "original_target": 0.042057, "side_long": True,
        "exit_reason": "stop", "realized_pnl": -4.13,
        "exit_time_utc": (now - timedelta(seconds=3600)).isoformat(),
        "horizon_seconds": 1800, "state": "pending",
    }
    sess = SimpleNamespace(
        id=9, symbol="KAIO-USD",
        risk_snapshot_json={"momentum_live_execution": {"post_exit_excursion_pending": pending}},
    )

    class _Q:
        def __init__(self, rows): self.rows = rows
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def all(self): return self.rows
        def first(self): return self.rows[0] if self.rows else None

    class _DB:
        def __init__(self, rows): self.rows = rows
        def query(self, *a, **k): return _Q(self.rows)
        def add(self, *a, **k): pass
        def commit(self): pass

    # post-exit bars: ran to 0.04237 (past target 0.042057)
    df = pd.DataFrame({"High": [0.0420, 0.04237, 0.0415], "Low": [0.0407, 0.0410, 0.0409]})
    from app.services.trading import market_data
    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *a, **k: df)
    monkeypatch.setattr(pex, "_patch_outcome_label", lambda db, sid, label: None)

    out = pex.run_post_exit_excursion_pass(_DB([sess]), now=now)
    assert out["labeled"] == 1
    assert out["shakeouts"] == 1
    label = sess.risk_snapshot_json["momentum_live_execution"]["post_exit_excursion"]
    assert label["outcome_class"] == "shakeout"
    assert sess.risk_snapshot_json["momentum_live_execution"]["post_exit_excursion_pending"]["state"] == "done"


def test_pass_waits_when_horizon_not_elapsed(monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    import app.services.trading.momentum_neural.post_exit_excursion as pex

    now = datetime(2026, 6, 7, 3, 0, 0)
    pending = {
        "symbol": "KAIO-USD", "entry_price": 0.0415, "exit_price": 0.0408,
        "original_stop": 0.0411, "original_target": 0.0420, "side_long": True,
        "exit_reason": "stop", "realized_pnl": -4.0,
        "exit_time_utc": (now - timedelta(seconds=300)).isoformat(),  # only 5min ago
        "horizon_seconds": 1800, "state": "pending",
    }
    sess = SimpleNamespace(id=9, risk_snapshot_json={"momentum_live_execution": {"post_exit_excursion_pending": pending}})

    class _Q:
        def __init__(self, rows): self.rows = rows
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def all(self): return self.rows

    class _DB:
        def query(self, *a, **k): return _Q([sess])
        def add(self, *a, **k): pass
        def commit(self): pass

    out = pex.run_post_exit_excursion_pass(_DB(), now=now)
    assert out["waiting"] == 1
    assert out["labeled"] == 0


def test_short_side_shakeout():
    # short: entry 100, stopped at 102, then price fell to 95 (past target 96)
    out = compute_post_exit_excursion(
        entry_price=100.0, exit_price=102.0, original_target=96.0, original_stop=102.5,
        side_long=False, future_high=102.5, future_low=95.0, exit_reason="stop",
        realized_pnl=-2.0,
    )
    assert out["counterfactual_target_hit"] is True
    assert out["outcome_class"] == "shakeout"
