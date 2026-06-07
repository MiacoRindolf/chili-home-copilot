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


def test_pass_skips_open_reentered_session(monkeypatch):
    # A session that re-entered after the exit that set the marker (state holding)
    # must be SKIPPED: the live runner owns its le and would clobber any label.
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import app.services.trading.momentum_neural.post_exit_excursion as pex

    now = datetime(2026, 6, 7, 3, 0, 0)
    pending = {
        "symbol": "KAIO-USD", "entry_price": 0.0415, "exit_price": 0.0408,
        "original_stop": 0.0411, "original_target": 0.0420, "side_long": True,
        "exit_reason": "stop", "realized_pnl": -4.0,
        "exit_time_utc": (now - timedelta(seconds=3600)).isoformat(),
        "horizon_seconds": 1800, "state": "pending",
    }
    sess = SimpleNamespace(
        id=9, state="live_trailing",  # re-entered, holding a position
        risk_snapshot_json={"momentum_live_execution": {"post_exit_excursion_pending": pending}},
    )

    class _DB:
        def query(self, *a, **k):
            class _Q:
                def filter(self, *a, **k): return self
                def order_by(self, *a, **k): return self
                def limit(self, *a, **k): return self
                def all(self_inner): return [sess]
            return _Q()
        def add(self, *a, **k): pass
        def commit(self): pass

    out = pex.run_post_exit_excursion_pass(_DB(), now=now)
    assert out["skipped_open"] == 1
    assert out["checked"] == 0
    assert out["labeled"] == 0
    # marker untouched (still pending) — resolves on the next real exit
    assert sess.risk_snapshot_json["momentum_live_execution"]["post_exit_excursion_pending"]["state"] == "pending"


def test_pass_expires_ancient_marker(monkeypatch):
    # A pending marker far older than the max-age bound is retired as 'expired'
    # (leaves the durable 'pending' set) rather than rescanned or labeled forever.
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import app.services.trading.momentum_neural.post_exit_excursion as pex

    now = datetime(2026, 6, 7, 3, 0, 0)
    pending = {
        "symbol": "KAIO-USD", "entry_price": 0.0415, "exit_price": 0.0408,
        "original_stop": 0.0411, "original_target": 0.0420, "side_long": True,
        "exit_reason": "stop", "realized_pnl": -4.0,
        "exit_time_utc": (now - timedelta(hours=60)).isoformat(),  # > 48h max-age
        "horizon_seconds": 1800, "state": "pending",
    }
    sess = SimpleNamespace(
        id=9, state="live_cancelled",
        risk_snapshot_json={"momentum_live_execution": {"post_exit_excursion_pending": pending}},
    )

    class _DB:
        def query(self, *a, **k):
            class _Q:
                def filter(self, *a, **k): return self
                def order_by(self, *a, **k): return self
                def limit(self, *a, **k): return self
                def all(self_inner): return [sess]
            return _Q()
        def add(self, *a, **k): pass
        def commit(self): pass

    out = pex.run_post_exit_excursion_pass(_DB(), now=now)
    assert out["expired"] == 1
    assert out["labeled"] == 0
    assert sess.risk_snapshot_json["momentum_live_execution"]["post_exit_excursion_pending"]["state"] == "expired"


def test_short_side_shakeout():
    # short: entry 100, stopped at 102, then price fell to 95 (past target 96)
    out = compute_post_exit_excursion(
        entry_price=100.0, exit_price=102.0, original_target=96.0, original_stop=102.5,
        side_long=False, future_high=102.5, future_low=95.0, exit_reason="stop",
        realized_pnl=-2.0,
    )
    assert out["counterfactual_target_hit"] is True
    assert out["outcome_class"] == "shakeout"


# --- consumer parity: the SELECTION aggregate must USE the shake-out label, not
# just store it. A shake-out (negative realized PnL) is a GOOD setup the stop gave
# back; the aggregate must credit it instead of scoring it as a thesis failure. ---

def _outcome_row(rb, pnl, label=None, oc="cancelled_in_trade", weight=1.0):
    from types import SimpleNamespace

    summary = {"post_exit_label": label} if label is not None else {}
    return SimpleNamespace(
        return_bps=rb, realized_pnl_usd=pnl, outcome_class=oc,
        evidence_weight=weight, extracted_summary_json=summary,
    )


def test_aggregate_credits_shakeout_setup_despite_negative_pnl():
    from app.services.trading.momentum_neural.evolution import (
        _aggregate_rows,
        _viability_delta_from_slices,
    )

    # Two real-shaped shake-outs: stopped for a loss, then ran +5.5% / +7.6% past target.
    shakeout_a = {
        "outcome_class": "shakeout", "setup_quality": 1.0, "stop_too_tight": True,
        "counterfactual_target_hit": True, "post_exit_mfe_pct": 5.5,
    }
    shakeout_b = {
        "outcome_class": "shakeout", "setup_quality": 1.0, "stop_too_tight": True,
        "counterfactual_target_hit": True, "post_exit_mfe_pct": 7.6,
    }
    rows = [_outcome_row(-32.0, -0.8, shakeout_a), _outcome_row(-258.0, -6.5, shakeout_b)]
    agg = _aggregate_rows(rows)

    assert agg["mean_return_bps"] < 0                  # raw realized P&L IS negative
    assert agg["mean_setup_quality"] == 1.0            # ...but the setup score is POSITIVE
    assert agg["mean_setup_adjusted_return_bps"] > 0   # ...and it is credited, not penalised
    assert agg["shakeout_count"] == 2
    assert agg["setup_credited_count"] == 2

    # A shake-out must NOT degrade viability (the delta uses the setup-adjusted mean).
    live = {
        "n": 3,
        "mean_return_bps": agg["mean_return_bps"],
        "mean_setup_adjusted_return_bps": agg["mean_setup_adjusted_return_bps"],
    }
    assert _viability_delta_from_slices({"n": 0}, live) >= 0.0


def test_aggregate_does_not_credit_thesis_invalidated():
    from app.services.trading.momentum_neural.evolution import _aggregate_rows

    bad = {
        "outcome_class": "thesis_invalidated", "setup_quality": 0.0, "stop_too_tight": False,
        "counterfactual_target_hit": False, "post_exit_mfe_pct": 0.1,
    }
    agg = _aggregate_rows([_outcome_row(-200.0, -5.0, bad)])
    # Genuinely wrong setup: stays a loss in BOTH channels, contributes no credit.
    assert agg["mean_return_bps"] < 0
    assert agg["mean_setup_adjusted_return_bps"] < 0
    assert agg["mean_setup_quality"] == 0.0
    assert agg["shakeout_count"] == 0
    assert agg["setup_credited_count"] == 0


def test_aggregate_unlabeled_rows_unchanged():
    # No post-exit label → setup-adjusted return must equal raw return (back-compat).
    from app.services.trading.momentum_neural.evolution import _aggregate_rows

    rows = [_outcome_row(120.0, 3.0, None, oc="target_win"), _outcome_row(-80.0, -2.0, None)]
    agg = _aggregate_rows(rows)
    assert agg["mean_setup_adjusted_return_bps"] == agg["mean_return_bps"]
    assert agg["setup_credited_count"] == 0
