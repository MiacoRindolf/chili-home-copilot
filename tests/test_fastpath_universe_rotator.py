"""Tests for f-fastpath-universe-rotation universe_rotator.

Covers the four admission gates + composite scoring + the run_rotation_pass
state machine (new entrant -> shadow -> active; demotion when dropped).

Helper-level tests use injectable list/snapshot functions so we never
touch the live Coinbase API. The DB-bound run_rotation_pass tests use
the chili_test ``db`` fixture.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# Admission gates
# ---------------------------------------------------------------------------

def _make_candidate(
    *,
    ticker: str = "TEST-USD",
    volume_24h_base: float = 1_000_000.0,
    last_price: float = 100.0,
    bid: float = 99.95,
    ask: float = 100.05,
    trades_24h: int = 10_000,
    bid_size_base: float = 100.0,
    ask_size_base: float = 100.0,
    high_24h: float = 105.0,
    low_24h: float = 95.0,
):
    from app.services.trading.fast_path.universe_rotator import _PairCandidate
    cand = _PairCandidate(
        ticker=ticker,
        volume_24h_base=volume_24h_base,
        last_price=last_price,
        bid=bid,
        ask=ask,
        trades_24h=trades_24h,
        high_24h=high_24h,
        low_24h=low_24h,
    )
    cand._bid_size_usd = bid_size_base * last_price
    cand._ask_size_usd = ask_size_base * last_price
    return cand


def test_passes_admission_gates_all_pass():
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    cand = _make_candidate()  # $100M volume, ~10 bps spread, $10k top-of-book, 10k trades
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is True
    assert reason is None


def test_passes_admission_gates_volume_below():
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    cand = _make_candidate(volume_24h_base=1_000.0)  # $100k volume
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is False
    assert reason == "volume_below_threshold"


def test_passes_admission_gates_spread_above():
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    cand = _make_candidate(bid=99.0, ask=101.0)  # ~200 bps spread
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is False
    assert reason == "spread_above_threshold"


def test_passes_admission_gates_top_of_book_below():
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    cand = _make_candidate(bid_size_base=10.0, ask_size_base=10.0)  # $1k each
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is False
    assert reason == "top_of_book_below_threshold"


def test_passes_admission_gates_trades_below():
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    cand = _make_candidate(trades_24h=100)
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is False
    assert reason == "trades_below_threshold"


def test_passes_admission_gates_missing_trade_count_is_not_zero_rejection():
    """Coinbase public stats omits trade_count; missing data is not 0 trades."""
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    cand = _make_candidate(trades_24h=0)
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is True
    assert reason is None


def test_passes_admission_gates_range_below():
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    cand = _make_candidate(high_24h=100.15, low_24h=99.85)
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
        min_range_24h_bps=150.0,
    )
    assert ok is False
    assert reason == "range_below_threshold"


# ---------------------------------------------------------------------------
# Opportunity score property
# ---------------------------------------------------------------------------

def test_composite_score_rewards_opportunity_data():
    """Composite rewards range, depth, and measured trade count."""
    a = _make_candidate(volume_24h_base=1_000_000.0, bid=99.95, ask=100.05)
    # range = 1000 bps, book = $10k, trades = 10k
    assert a.composite_score == pytest.approx(100_000_000_000.0, rel=0.01)


def test_composite_score_requires_opportunity_data():
    a = _make_candidate(high_24h=0.0, low_24h=0.0)
    assert a.range_24h_bps == 0.0
    assert a.composite_score == 0.0


def test_composite_score_allows_missing_trade_count():
    a = _make_candidate(trades_24h=0)
    assert a.has_valid_opportunity_data is True
    assert a.composite_score > 0.0


def test_more_volatile_pair_can_outrank_higher_volume_pair():
    quiet_high_volume = _make_candidate(
        ticker="QUIET-USD",
        volume_24h_base=10_000_000.0,
        high_24h=101.0,
        low_24h=99.0,
    )
    volatile_lower_volume = _make_candidate(
        ticker="VOL-USD",
        volume_24h_base=1_000_000.0,
        high_24h=120.0,
        low_24h=80.0,
    )
    assert volatile_lower_volume.volume_24h_usd < quiet_high_volume.volume_24h_usd
    assert volatile_lower_volume.composite_score > quiet_high_volume.composite_score


def test_candidate_rank_score_penalizes_execution_cost():
    from app.services.trading.fast_path.universe_rotator import _candidate_rank_score

    tight = _make_candidate(ticker="TIGHT-USD", bid=99.995, ask=100.005)
    wide = _make_candidate(ticker="WIDE-USD", bid=99.96, ask=100.04)

    assert tight.composite_score == pytest.approx(wide.composite_score)
    assert _candidate_rank_score(tight, fee_bps=40.0) > _candidate_rank_score(
        wide,
        fee_bps=40.0,
    )


def test_candidate_rank_score_caps_depth_at_fillability_gate():
    from app.services.trading.fast_path.universe_rotator import _candidate_rank_score

    quiet_deep = _make_candidate(
        ticker="QUIET-USD",
        bid_size_base=10_000.0,
        ask_size_base=10_000.0,
        high_24h=101.0,
        low_24h=99.0,
    )
    volatile_fillable = _make_candidate(
        ticker="VOL-USD",
        bid_size_base=50.0,
        ask_size_base=50.0,
        high_24h=120.0,
        low_24h=80.0,
    )

    assert quiet_deep.composite_score > volatile_fillable.composite_score
    assert _candidate_rank_score(
        volatile_fillable,
        fee_bps=40.0,
        top_of_book_cap_usd=5_000.0,
    ) > _candidate_rank_score(
        quiet_deep,
        fee_bps=40.0,
        top_of_book_cap_usd=5_000.0,
    )


def test_candidate_rank_score_treats_trade_count_as_admission_only():
    from app.services.trading.fast_path.universe_rotator import _candidate_rank_score

    busy_quiet = _make_candidate(
        ticker="BUSY-USD",
        trades_24h=1_000_000,
        high_24h=101.0,
        low_24h=99.0,
    )
    volatile_missing_trade_count = _make_candidate(
        ticker="VOL-USD",
        trades_24h=0,
        high_24h=120.0,
        low_24h=80.0,
    )

    assert busy_quiet.composite_score > volatile_missing_trade_count.composite_score
    assert _candidate_rank_score(
        volatile_missing_trade_count,
        fee_bps=40.0,
        top_of_book_cap_usd=5_000.0,
    ) > _candidate_rank_score(
        busy_quiet,
        fee_bps=40.0,
        top_of_book_cap_usd=5_000.0,
    )


# ---------------------------------------------------------------------------
# run_rotation_pass — disabled flag short-circuit
# ---------------------------------------------------------------------------

@dataclass
class _StubSettings:
    universe_rotation_enabled: bool = True
    universe_top_n: int = 5
    universe_hysteresis_ranks: int = 3
    universe_shadow_window_h: int = 24
    universe_min_volume_24h_usd: float = 10_000_000.0
    universe_max_spread_bps: float = 10.0
    universe_min_top_of_book_usd: float = 5_000.0
    universe_shadow_min_top_of_book_usd: float = 25.0
    universe_min_range_24h_bps: float = 150.0
    universe_adaptive_range_floor_enabled: bool = True
    universe_missing_grace_passes: int = 2
    universe_min_shadow_exploration_n: int = 0
    universe_market_velocity_cost_parity_ratio: float = 1.0
    universe_market_velocity_deadlock_probe_enabled: bool = True
    universe_learning_retention_horizon_s: int = 300
    universe_learning_retention_max_n: int = 3
    universe_min_trades_24h: int = 1_000
    execution_mode: str = "maker_only"
    cost_aware_maker_fee_bps: float = 0.0
    cost_aware_taker_fee_bps: float = 0.0
    live_alpha_min_samples: int = 50
    live_alpha_min_net_bps: float = 0.0
    maker_attempt_adverse_filter_enabled: bool = True
    maker_attempt_adverse_filter_window_h: int = 24
    emit_raw_imbalance_alerts: bool = True
    scanner_book_pressure_enabled: bool = True
    scanner_book_pressure_max_spread_bps: float = 10.0


class _FakeRotationDB:
    def __init__(
        self,
        *,
        previous: dict[str, tuple[str, int | None]] | None = None,
        completed_shadows: set[str] | None = None,
        edge_rows: dict[str, dict] | None = None,
        decay_rows: dict[str, list[dict]] | None = None,
        maker_attempt_rows: dict[str, list[dict]] | None = None,
        latest_rotation_at: datetime | None = None,
        observed_rows: list[dict] | None = None,
        prior_market_velocity_ratio: float | None = None,
        grace_history: dict[str, list[dict]] | None = None,
        learning_rows: list[dict] | None = None,
    ) -> None:
        self.inserted_rows: list[dict] = []
        self.inserted_run: dict | None = None
        self.committed = False
        self.previous = previous or {}
        self.completed_shadows = completed_shadows or set()
        self.edge_rows = edge_rows or {}
        self.decay_rows = decay_rows or {}
        self.maker_attempt_rows = maker_attempt_rows or {}
        self.latest_rotation_at = latest_rotation_at
        self.observed_rows = observed_rows or []
        self.prior_market_velocity_ratio = prior_market_velocity_ratio
        self.grace_history = grace_history or {}
        self.learning_rows = learning_rows or []

    def execute(self, statement, params=None):
        sql = str(statement)
        if "INSERT INTO fast_path_universe_runs" in sql:
            self.inserted_run = dict(params or {})
            return _FakeRows()
        if "INSERT INTO fast_path_universe" in sql:
            self.inserted_rows = list(params or [])
            return _FakeRows()
        if "SELECT ticker, status, rank" in sql:
            rows = [
                _FakeRow(ticker=t, status=status, rank=rank)
                for t, (status, rank) in self.previous.items()
            ]
            return _FakeRows(rows)
        if "MAX(rotation_at) AS rotation_at" in sql:
            if self.latest_rotation_at is None:
                return _FakeRows([])
            return _FakeRows([_FakeRow(rotation_at=self.latest_rotation_at)])
        if "observed alert/fill opportunity" in sql:
            return _FakeScalarRows(self.observed_rows)
        if "latest observed market velocity ratio" in sql:
            if self.prior_market_velocity_ratio is None:
                return _FakeRows([])
            return _FakeRows([
                _FakeRow(ratio=self.prior_market_velocity_ratio)
            ])
        if "recent learning retention events" in sql:
            return _FakeScalarRows(self.learning_rows)
        if "SELECT status, rank, composite_score" in sql:
            ticker = (params or {}).get("ticker")
            rows = [_FakeRow(**row) for row in self.grace_history.get(ticker, [])]
            return _FakeRows(rows)
        if "promoted_at IS NOT NULL" in sql and "shadow_status" in (params or {}):
            return _FakeRows([_FakeRow(ticker=t) for t in self.completed_shadows])
        if "SELECT ticker, alert_type, score_bucket" in sql and "FROM fast_signal_decay" in sql:
            ticker = (params or {}).get("ticker")
            return _FakeScalarRows(self.decay_rows.get(ticker, []))
        if "FROM fast_path_maker_attempts" in sql:
            ticker = (params or {}).get("ticker")
            return _FakeScalarRows(self.maker_attempt_rows.get(ticker, []))
        if "FROM fast_signal_decay" in sql:
            ticker = (params or {}).get("ticker")
            row = self.edge_rows.get(ticker)
            return _FakeScalarRows(row)
        return _FakeRows()

    def commit(self) -> None:
        self.committed = True


class _FakeRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeRows:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def fetchall(self):
        return self._rows


class _FakeScalarRows:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row

    def all(self):
        if self._row is None:
            return []
        if isinstance(self._row, list):
            return self._row
        return [self._row]


def test_run_rotation_pass_disabled_short_circuits():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(universe_rotation_enabled=False)
    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["BTC-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )
    assert out["skipped_reason"] == "universe_rotation_disabled"
    assert out["scanned"] == 0


def test_run_rotation_pass_first_pass_writes_shadow():
    """Brand-new entrants land in status='shadow' on first pass."""
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(universe_top_n=3, universe_hysteresis_ranks=0)

    candidates = ["BTC-USD", "ETH-USD", "SOL-USD"]
    snapshots = {
        # Decreasing composite so rank order is stable
        "BTC-USD": _make_candidate(ticker="BTC-USD", volume_24h_base=1_000_000.0),
        "ETH-USD": _make_candidate(ticker="ETH-USD", volume_24h_base=500_000.0),
        "SOL-USD": _make_candidate(ticker="SOL-USD", volume_24h_base=100_000.0),
    }
    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: candidates,
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["scanned"] == 3
    assert out["promoted_to_shadow"] == 3
    assert out["promoted_to_active"] == 0

    assert len(db.inserted_rows) == 3
    assert db.inserted_run is not None
    assert db.inserted_run["shadow_n"] == 3
    assert db.inserted_run["range_floor_effective_bps"] == pytest.approx(1000.0)
    assert all(r["status"] == "shadow" for r in db.inserted_rows)
    assert [r["ticker"] for r in db.inserted_rows] == [
        "BTC-USD", "ETH-USD", "SOL-USD",
    ]


def test_run_rotation_pass_demotes_shadow_when_learned_lanes_exhausted():
    from app.services.trading.fast_path.universe_status import (
        UNIVERSE_STATUS_INACTIVE,
        UNIVERSE_STATUS_SHADOW,
    )
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"HYPE-USD": (UNIVERSE_STATUS_SHADOW, 1)},
        decay_rows={
            "HYPE-USD": [
                {
                    "ticker": "HYPE-USD",
                    "alert_type": "imbalance_long",
                    "score_bucket": "low",
                    "horizon_s": 5,
                    "sample_count": 6,
                    "mean_return": -0.0012,
                    "m2_return": 0.0000001,
                },
            ],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)
    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["HYPE-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["edge_exhausted_demotions"] == 1
    assert out["edge_exhaustion_blocks"]["negative_edge"] == 1
    assert db.inserted_rows[0]["ticker"] == "HYPE-USD"
    assert db.inserted_rows[0]["status"] == UNIVERSE_STATUS_INACTIVE
    assert db.inserted_rows[0]["rank"] is None


def test_sparse_lane_does_not_veto_exhausted_ticker_demotion():
    from app.services.trading.fast_path.universe_status import (
        UNIVERSE_STATUS_INACTIVE,
        UNIVERSE_STATUS_SHADOW,
    )
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"SOL-USD": (UNIVERSE_STATUS_SHADOW, 1)},
        decay_rows={
            "SOL-USD": [
                {
                    "ticker": "SOL-USD",
                    "alert_type": "imbalance_long",
                    "score_bucket": "low",
                    "horizon_s": 5,
                    "sample_count": 6,
                    "mean_return": -0.0012,
                    "m2_return": 0.0000001,
                },
                {
                    "ticker": "SOL-USD",
                    "alert_type": "volume_breakout_long",
                    "score_bucket": "high",
                    "horizon_s": 5,
                    "sample_count": 1,
                    "mean_return": 0.001,
                    "m2_return": 0.0,
                },
            ],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["SOL-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["edge_exhausted_demotions"] == 1
    assert out["edge_exhaustion_blocks"]["negative_edge"] == 1
    assert out["edge_exhaustion_blocks"]["insufficient_statistical_evidence"] == 1
    assert db.inserted_rows[0]["status"] == UNIVERSE_STATUS_INACTIVE


def test_sparse_only_shadow_lane_still_collects_data():
    from app.services.trading.fast_path.universe_status import UNIVERSE_STATUS_SHADOW
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        decay_rows={
            "NEW-USD": [
                {
                    "ticker": "NEW-USD",
                    "alert_type": "volume_breakout_long",
                    "score_bucket": "high",
                    "horizon_s": 5,
                    "sample_count": 1,
                    "mean_return": 0.001,
                    "m2_return": 0.0,
                },
            ],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["NEW-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["edge_exhausted_demotions"] == 0
    assert db.inserted_rows[0]["status"] == UNIVERSE_STATUS_SHADOW


def test_shadow_exploration_floor_keeps_exhausted_candidate_learning():
    from app.services.trading.fast_path.universe_status import UNIVERSE_STATUS_SHADOW
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"HYPE-USD": (UNIVERSE_STATUS_SHADOW, 1)},
        decay_rows={
            "HYPE-USD": [
                {
                    "ticker": "HYPE-USD",
                    "alert_type": "imbalance_long",
                    "score_bucket": "low",
                    "horizon_s": 5,
                    "sample_count": 6,
                    "mean_return": -0.0012,
                    "m2_return": 0.0000001,
                },
            ],
        },
    )
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        universe_min_shadow_exploration_n=1,
    )

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: ["HYPE-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["shadow_exploration_forced"] == 1
    assert out["shadow_exploration_forced_reasons"] == {"edge_exhausted": 1}
    assert out["edge_exhausted_demotions"] == 0
    assert out["kept_shadow"] == 1
    assert db.inserted_rows[0]["ticker"] == "HYPE-USD"
    assert db.inserted_rows[0]["status"] == UNIVERSE_STATUS_SHADOW


def test_shadow_exploration_floor_respects_below_cost_market_velocity():
    from app.services.trading.fast_path.universe_status import (
        UNIVERSE_STATUS_INACTIVE,
        UNIVERSE_STATUS_SHADOW,
    )
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"HYPE-USD": (UNIVERSE_STATUS_SHADOW, 1)},
        prior_market_velocity_ratio=0.2,
        decay_rows={
            "HYPE-USD": [
                {
                    "ticker": "HYPE-USD",
                    "alert_type": "imbalance_long",
                    "score_bucket": "low",
                    "horizon_s": 5,
                    "sample_count": 6,
                    "mean_return": -0.0012,
                    "m2_return": 0.0000001,
                },
            ],
        },
    )
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        universe_min_shadow_exploration_n=1,
    )

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: ["HYPE-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["shadow_exploration_forced"] == 0
    assert out["shadow_exploration_force_velocity_blocked"] == 1
    assert out["shadow_exploration_force_velocity_ratio"] == pytest.approx(0.2)
    assert out["market_velocity_cost_parity_ratio"] == pytest.approx(1.0)
    assert out["edge_exhausted_demotions"] == 1
    assert db.inserted_rows[0]["ticker"] == "HYPE-USD"
    assert db.inserted_rows[0]["status"] == UNIVERSE_STATUS_INACTIVE
    assert not any(
        row["status"] == UNIVERSE_STATUS_SHADOW for row in db.inserted_rows
    )


def test_shadow_demotes_when_maker_attempts_prove_adverse_selection():
    from app.services.trading.fast_path.universe_status import (
        UNIVERSE_STATUS_INACTIVE,
        UNIVERSE_STATUS_SHADOW,
    )
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"AERO-USD": (UNIVERSE_STATUS_SHADOW, 1)},
        maker_attempt_rows={
            "AERO-USD": [
                {
                    "ticker": "AERO-USD",
                    "alert_type": "imbalance_long",
                    "signal_score": 0.75,
                    "side": "buy",
                    "fill_outcome": "cancelled",
                    "mid_drift_bps": 3.0,
                },
                {
                    "ticker": "AERO-USD",
                    "alert_type": "imbalance_long",
                    "signal_score": 0.75,
                    "side": "buy",
                    "fill_outcome": "cancelled",
                    "mid_drift_bps": 4.0,
                },
                {
                    "ticker": "AERO-USD",
                    "alert_type": "imbalance_long",
                    "signal_score": 0.75,
                    "side": "buy",
                    "fill_outcome": "replaced",
                    "mid_drift_bps": 5.0,
                },
            ],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["AERO-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["edge_exhausted_demotions"] == 1
    assert out["edge_exhaustion_blocks"] == {"maker_adverse_selection": 1}
    assert db.inserted_rows[0]["ticker"] == "AERO-USD"
    assert db.inserted_rows[0]["status"] == UNIVERSE_STATUS_INACTIVE
    assert db.inserted_rows[0]["rank"] is None


def test_sparse_maker_attempts_do_not_demote_new_shadow():
    from app.services.trading.fast_path.universe_status import UNIVERSE_STATUS_SHADOW
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        maker_attempt_rows={
            "NEW-USD": [
                {
                    "ticker": "NEW-USD",
                    "alert_type": "imbalance_long",
                    "signal_score": 0.75,
                    "side": "buy",
                    "fill_outcome": "cancelled",
                    "mid_drift_bps": 4.0,
                },
            ],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["NEW-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["edge_exhausted_demotions"] == 0
    assert out["edge_exhaustion_blocks"] == {}
    assert db.inserted_rows[0]["status"] == UNIVERSE_STATUS_SHADOW


def test_edge_exhausted_candidates_do_not_set_adaptive_range_floor():
    from app.services.trading.fast_path.universe_status import (
        UNIVERSE_STATUS_INACTIVE,
        UNIVERSE_STATUS_SHADOW,
    )
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        decay_rows={
            "BAD-USD": [
                {
                    "ticker": "BAD-USD",
                    "alert_type": "imbalance_long",
                    "score_bucket": "low",
                    "horizon_s": 5,
                    "sample_count": 6,
                    "mean_return": -0.0012,
                    "m2_return": 0.0000001,
                },
            ],
        },
    )
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=True,
    )
    snapshots = {
        "BAD-USD": _make_candidate(
            ticker="BAD-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
        "GOOD1-USD": _make_candidate(
            ticker="GOOD1-USD",
            high_24h=105.0,
            low_24h=95.0,
        ),
        "GOOD2-USD": _make_candidate(
            ticker="GOOD2-USD",
            high_24h=104.0,
            low_24h=96.0,
        ),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["BAD-USD", "GOOD1-USD", "GOOD2-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["range_floor_dynamic_bps"] == pytest.approx(800.0)
    assert out["edge_exhaustion_floor_excluded"] == 1
    assert out["edge_exhaustion_backfill_skips"] == 1
    assert out["edge_exhausted_demotions"] == 1
    assert out["ranked_n"] == 2
    rows_by_ticker = {r["ticker"]: r for r in db.inserted_rows}
    assert rows_by_ticker["BAD-USD"]["status"] == UNIVERSE_STATUS_INACTIVE
    assert rows_by_ticker["BAD-USD"]["rank"] is None
    assert rows_by_ticker["GOOD1-USD"]["status"] == UNIVERSE_STATUS_SHADOW
    assert rows_by_ticker["GOOD1-USD"]["rank"] == 1
    assert rows_by_ticker["GOOD2-USD"]["status"] == UNIVERSE_STATUS_SHADOW
    assert rows_by_ticker["GOOD2-USD"]["rank"] == 2


def test_rotation_prefers_lower_cost_when_raw_opportunity_ties():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        cost_aware_maker_fee_bps=40.0,
    )
    snapshots = {
        "WIDE-USD": _make_candidate(ticker="WIDE-USD", bid=99.96, ask=100.04),
        "TIGHT-USD": _make_candidate(ticker="TIGHT-USD", bid=99.995, ask=100.005),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["WIDE-USD", "TIGHT-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["ranked_n"] == 1
    assert db.inserted_rows[0]["ticker"] == "TIGHT-USD"


def test_rotation_honors_scanner_spread_cap_when_raw_imbalance_disabled():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_max_spread_bps=10.0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
        emit_raw_imbalance_alerts=False,
        scanner_book_pressure_enabled=True,
        scanner_book_pressure_max_spread_bps=3.0,
    )
    snapshots = {
        "WIDE-USD": _make_candidate(ticker="WIDE-USD", bid=99.98, ask=100.02),
        "TIGHT-USD": _make_candidate(ticker="TIGHT-USD", bid=99.995, ask=100.005),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["WIDE-USD", "TIGHT-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["universe_max_spread_bps"] == pytest.approx(10.0)
    assert out["signal_compatible_spread_cap_bps"] == pytest.approx(3.0)
    assert out["effective_universe_max_spread_bps"] == pytest.approx(3.0)
    assert out["gate_rejections"]["spread_above_threshold"] == 1
    assert out["ranked_n"] == 1
    assert [row["ticker"] for row in db.inserted_rows] == ["TIGHT-USD"]


def test_rotation_keeps_universe_spread_cap_when_raw_imbalance_enabled():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_max_spread_bps=10.0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
        emit_raw_imbalance_alerts=True,
        scanner_book_pressure_enabled=True,
        scanner_book_pressure_max_spread_bps=3.0,
    )
    snapshots = {
        "WIDE-USD": _make_candidate(ticker="WIDE-USD", bid=99.98, ask=100.02),
        "TIGHT-USD": _make_candidate(ticker="TIGHT-USD", bid=99.995, ask=100.005),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["WIDE-USD", "TIGHT-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["signal_compatible_spread_cap_bps"] is None
    assert out["effective_universe_max_spread_bps"] == pytest.approx(10.0)
    assert "spread_above_threshold" not in out["gate_rejections"]
    assert out["ranked_n"] == 2
    assert {row["ticker"] for row in db.inserted_rows} == {
        "TIGHT-USD",
        "WIDE-USD",
    }


def test_rotation_treats_depth_as_fillability_gate_for_ranking():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
    )
    snapshots = {
        "QUIET-USD": _make_candidate(
            ticker="QUIET-USD",
            bid_size_base=10_000.0,
            ask_size_base=10_000.0,
            high_24h=101.0,
            low_24h=99.0,
        ),
        "VOL-USD": _make_candidate(
            ticker="VOL-USD",
            bid_size_base=50.0,
            ask_size_base=50.0,
            high_24h=120.0,
            low_24h=80.0,
        ),
    }

    assert (
        snapshots["QUIET-USD"].composite_score
        > snapshots["VOL-USD"].composite_score
    )
    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: ["QUIET-USD", "VOL-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["rank_top_of_book_cap_usd"] == pytest.approx(25.0)
    assert out["rank_shadow_top_of_book_cap_usd"] == pytest.approx(25.0)
    from app.services.trading.fast_path.universe_rotator import (
        RANK_TRADE_COUNT_MULTIPLIER_MODE,
    )

    assert out["rank_trade_count_multiplier"] == RANK_TRADE_COUNT_MULTIPLIER_MODE
    assert out["ranked_n"] == 1
    assert db.inserted_rows[0]["ticker"] == "VOL-USD"


def test_rotation_uses_observed_signal_and_fill_rates_for_shadow_ranking():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        latest_rotation_at=datetime(2026, 5, 24, 15, 0, 0),
        observed_rows=[
            {
                "ticker": "NOFILL-USD",
                "bars": 20,
                "alerts": 20,
                "maker_attempts": 20,
                "maker_fills": 0,
            },
            {
                "ticker": "FILL-USD",
                "bars": 20,
                "alerts": 10,
                "maker_attempts": 10,
                "maker_fills": 5,
            },
        ],
    )
    s = _StubSettings(
        universe_top_n=3,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
    )
    snapshots = {
        "NOFILL-USD": _make_candidate(
            ticker="NOFILL-USD",
            high_24h=130.0,
            low_24h=70.0,
        ),
        "FILL-USD": _make_candidate(
            ticker="FILL-USD",
            high_24h=112.0,
            low_24h=88.0,
        ),
        "FRESH-USD": _make_candidate(
            ticker="FRESH-USD",
            high_24h=108.0,
            low_24h=92.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: ["NOFILL-USD", "FILL-USD", "FRESH-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["observed_opportunity_tickers"] == 2
    assert out["observed_opportunity_median_maker_fill_rate"] == pytest.approx(0.5)
    assert out["observed_opportunity_rank_skips"] == 1
    rows_by_ticker = {r["ticker"]: r for r in db.inserted_rows}
    assert rows_by_ticker["NOFILL-USD"]["status"] == "inactive"
    assert rows_by_ticker["NOFILL-USD"]["rank"] is None
    assert rows_by_ticker["FILL-USD"]["rank"] == 1
    assert rows_by_ticker["FRESH-USD"]["rank"] == 2


def test_rotation_uses_recent_realized_move_for_observed_ranking():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        latest_rotation_at=datetime(2026, 5, 24, 15, 0, 0),
        observed_rows=[
            {
                "ticker": "QUIET-USD",
                "bars": 20,
                "alerts": 10,
                "maker_attempts": 0,
                "maker_fills": 0,
                "realized_move_samples": 20,
                "mean_realized_bar_move_bps": 1.0,
            },
            {
                "ticker": "MOVING-USD",
                "bars": 20,
                "alerts": 10,
                "maker_attempts": 0,
                "maker_fills": 0,
                "realized_move_samples": 20,
                "mean_realized_bar_move_bps": 20.0,
            },
        ],
    )
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
    )
    snapshots = {
        "QUIET-USD": _make_candidate(
            ticker="QUIET-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
        "MOVING-USD": _make_candidate(
            ticker="MOVING-USD",
            high_24h=112.0,
            low_24h=88.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: ["QUIET-USD", "MOVING-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["observed_opportunity_median_realized_bar_move_bps"] == (
        pytest.approx(10.5)
    )
    assert out["observed_opportunity_median_round_trip_cost_bps"] == (
        pytest.approx(20.0)
    )
    assert out["observed_opportunity_median_realized_move_to_cost"] == (
        pytest.approx(0.525)
    )
    assert out["ranked_n"] == 1
    assert db.inserted_rows[0]["ticker"] == "MOVING-USD"


def test_rotation_penalizes_observed_move_below_round_trip_cost():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        latest_rotation_at=datetime(2026, 5, 24, 15, 0, 0),
        observed_rows=[
            {
                "ticker": "RANGEY-BUT-DEAD-USD",
                "bars": 20,
                "alerts": 10,
                "maker_attempts": 0,
                "maker_fills": 0,
                "realized_move_samples": 20,
                "mean_realized_bar_move_bps": 1.0,
            },
            {
                "ticker": "COST-FIT-USD",
                "bars": 20,
                "alerts": 10,
                "maker_attempts": 0,
                "maker_fills": 0,
                "realized_move_samples": 20,
                "mean_realized_bar_move_bps": 20.0,
            },
        ],
    )
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
    )
    snapshots = {
        "RANGEY-BUT-DEAD-USD": _make_candidate(
            ticker="RANGEY-BUT-DEAD-USD",
            high_24h=150.0,
            low_24h=50.0,
        ),
        "COST-FIT-USD": _make_candidate(
            ticker="COST-FIT-USD",
            high_24h=105.0,
            low_24h=95.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: ["RANGEY-BUT-DEAD-USD", "COST-FIT-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["observed_opportunity_median_realized_move_to_cost"] == (
        pytest.approx(0.525)
    )
    assert out["ranked_n"] == 1
    assert db.inserted_rows[0]["ticker"] == "COST-FIT-USD"


def test_rotation_does_not_backfill_unknown_symbols_when_velocity_below_cost():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(prior_market_velocity_ratio=0.2)
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
    )
    snapshots = {
        "UNKNOWN-A-USD": _make_candidate(
            ticker="UNKNOWN-A-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
        "UNKNOWN-B-USD": _make_candidate(
            ticker="UNKNOWN-B-USD",
            high_24h=118.0,
            low_24h=82.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: list(snapshots),
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["prior_observed_opportunity_median_realized_move_to_cost"] == (
        pytest.approx(0.2)
    )
    assert out["market_velocity_backfill_skips"] == 2
    assert out["ranked_n"] == 0
    assert {row["status"] for row in db.inserted_rows} == {"inactive"}


def test_velocity_deadlock_probe_keeps_configured_shadow_floor_alive():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass
    from app.services.trading.fast_path.universe_status import (
        UNIVERSE_STATUS_INACTIVE,
        UNIVERSE_STATUS_SHADOW,
    )

    shadow_floor = 1
    db = _FakeRotationDB(prior_market_velocity_ratio=0.2)
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
        universe_min_shadow_exploration_n=shadow_floor,
    )
    snapshots = {
        "REPROBE-A-USD": _make_candidate(
            ticker="REPROBE-A-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
        "REPROBE-B-USD": _make_candidate(
            ticker="REPROBE-B-USD",
            high_24h=118.0,
            low_24h=82.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: list(snapshots),
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    statuses = {row["ticker"]: row["status"] for row in db.inserted_rows}
    assert out["shadow_exploration_velocity_deadlock_probe_enabled"] is True
    assert out["shadow_exploration_velocity_deadlock_probe"] == shadow_floor
    assert out["shadow_exploration_forced_reasons"] == {
        "market_velocity_deadlock_probe": shadow_floor,
    }
    assert out["shadow_exploration_force_velocity_blocked"] == 0
    assert out["ranked_n"] == shadow_floor
    assert list(statuses.values()).count(UNIVERSE_STATUS_SHADOW) == shadow_floor
    assert list(statuses.values()).count(UNIVERSE_STATUS_INACTIVE) == (
        len(snapshots) - shadow_floor
    )


def test_velocity_deadlock_probe_fills_configured_floor_shortfall():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass
    from app.services.trading.fast_path.universe_status import UNIVERSE_STATUS_SHADOW

    shadow_floor = 2
    db = _FakeRotationDB(
        latest_rotation_at=datetime(2026, 5, 24, 15, 0, 0),
        observed_rows=[{
            "ticker": "OBSERVED-USD",
            "bars": 20,
            "alerts": 1,
            "maker_attempts": 0,
            "maker_fills": 0,
            "realized_move_samples": 20,
            "mean_realized_bar_move_bps": 1.0,
        }],
    )
    s = _StubSettings(
        universe_top_n=3,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
        universe_min_shadow_exploration_n=shadow_floor,
    )
    snapshots = {
        "OBSERVED-USD": _make_candidate(
            ticker="OBSERVED-USD",
            high_24h=110.0,
            low_24h=90.0,
        ),
        "BACKFILL-A-USD": _make_candidate(
            ticker="BACKFILL-A-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
        "BACKFILL-B-USD": _make_candidate(
            ticker="BACKFILL-B-USD",
            high_24h=118.0,
            low_24h=82.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: list(snapshots),
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    statuses = {row["ticker"]: row["status"] for row in db.inserted_rows}
    assert out["shadow_exploration_velocity_deadlock_probe"] == 1
    assert out["shadow_exploration_forced_reasons"] == {
        "market_velocity_deadlock_probe": 1,
    }
    assert out["shadow_exploration_force_velocity_blocked"] == 0
    assert out["ranked_n"] == shadow_floor
    assert list(statuses.values()).count(UNIVERSE_STATUS_SHADOW) == shadow_floor


def test_velocity_deadlock_probe_excludes_range_floor_boundary_candidate():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass
    from app.services.trading.fast_path.universe_status import (
        UNIVERSE_STATUS_INACTIVE,
        UNIVERSE_STATUS_SHADOW,
    )

    db = _FakeRotationDB(prior_market_velocity_ratio=0.2)
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=200.0,
        universe_adaptive_range_floor_enabled=False,
        universe_min_shadow_exploration_n=2,
    )
    snapshots = {
        "FLOOR-EDGE-USD": _make_candidate(
            ticker="FLOOR-EDGE-USD",
            high_24h=101.0,
            low_24h=99.0,
        ),
        "ABOVE-FLOOR-USD": _make_candidate(
            ticker="ABOVE-FLOOR-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: list(snapshots),
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    statuses = {row["ticker"]: row["status"] for row in db.inserted_rows}
    assert out["shadow_exploration_velocity_deadlock_probe"] == 1
    assert out["shadow_exploration_velocity_deadlock_floor_excluded"] == 1
    assert out["shadow_exploration_forced_reasons"] == {
        "market_velocity_deadlock_probe": 1,
    }
    assert out["shadow_exploration_force_velocity_blocked"] == 1
    assert out["market_velocity_backfill_skips"] == 1
    assert out["ranked_n"] == 1
    assert statuses["ABOVE-FLOOR-USD"] == UNIVERSE_STATUS_SHADOW
    assert statuses["FLOOR-EDGE-USD"] == UNIVERSE_STATUS_INACTIVE


def test_learning_retention_prioritizes_recent_alert_for_shadow_floor():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass
    from app.services.trading.fast_path.universe_status import (
        UNIVERSE_STATUS_INACTIVE,
        UNIVERSE_STATUS_SHADOW,
    )

    db = _FakeRotationDB(
        prior_market_velocity_ratio=0.2,
        learning_rows=[{
            "ticker": "LEARN-USD",
            "latest_event_at": datetime(2026, 5, 24, 15, 4, 0),
            "latest_alert_at": datetime(2026, 5, 24, 15, 4, 0),
            "latest_maker_attempt_at": None,
            "alert_count": 1,
            "maker_attempt_count": 0,
        }],
    )
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
        universe_min_shadow_exploration_n=1,
        universe_learning_retention_max_n=1,
    )
    snapshots = {
        "HIGHER-RANK-USD": _make_candidate(
            ticker="HIGHER-RANK-USD",
            high_24h=130.0,
            low_24h=70.0,
        ),
        "LEARN-USD": _make_candidate(
            ticker="LEARN-USD",
            high_24h=106.0,
            low_24h=94.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: list(snapshots),
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    statuses = {row["ticker"]: row["status"] for row in db.inserted_rows}
    assert out["learning_retention_event_tickers"] == 1
    assert out["learning_retention_candidates"] == 1
    assert out["learning_retained_n"] == 1
    assert out["shadow_exploration_forced_reasons"] == {"learning_retention": 1}
    assert statuses["LEARN-USD"] == UNIVERSE_STATUS_SHADOW
    assert statuses["HIGHER-RANK-USD"] == UNIVERSE_STATUS_INACTIVE


def test_rotation_ranks_shadow_candidates_against_active_depth_candidates():
    """Probe-fillable volatile names should compete for subscription rank."""
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass
    from app.services.trading.fast_path.universe_status import UNIVERSE_STATUS_SHADOW

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=False,
        universe_shadow_min_top_of_book_usd=25.0,
    )
    snapshots = {
        "ACTIVE-A-USD": _make_candidate(
            ticker="ACTIVE-A-USD",
            bid_size_base=100.0,
            ask_size_base=100.0,
            high_24h=102.0,
            low_24h=98.0,
        ),
        "ACTIVE-B-USD": _make_candidate(
            ticker="ACTIVE-B-USD",
            bid_size_base=100.0,
            ask_size_base=100.0,
            high_24h=101.75,
            low_24h=98.25,
        ),
        "VOLATILE-USD": _make_candidate(
            ticker="VOLATILE-USD",
            bid_size_base=1.0,
            ask_size_base=1.0,
            high_24h=125.0,
            low_24h=75.0,
        ),
    }

    out = run_rotation_pass(
        db,
        settings=s,
        list_usd_products_fn=lambda: [
            "ACTIVE-A-USD",
            "ACTIVE-B-USD",
            "VOLATILE-USD",
        ],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["hard_ranked_n"] == 2
    assert out["shadow_exploration_shortfall"] == 0
    assert out["shadow_exploration_candidates"] == 1
    assert out["gate_rejections"]["top_of_book_below_threshold"] == 1
    assert [r["ticker"] for r in db.inserted_rows] == ["VOLATILE-USD", "ACTIVE-A-USD"]
    volatile = db.inserted_rows[0]
    assert volatile["status"] == UNIVERSE_STATUS_SHADOW
    assert volatile["rank"] == 1


def test_run_rotation_pass_keeps_shadow_when_lane_is_still_uncertain():
    from app.services.trading.fast_path.universe_status import UNIVERSE_STATUS_SHADOW
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"BILL-USD": (UNIVERSE_STATUS_SHADOW, 1)},
        decay_rows={
            "BILL-USD": [
                {
                    "ticker": "BILL-USD",
                    "alert_type": "spread_squeeze",
                    "score_bucket": "med",
                    "horizon_s": 5,
                    "sample_count": 5,
                    "mean_return": 0.002,
                    "m2_return": 0.0001,
                },
            ],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)
    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["BILL-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["edge_exhausted_demotions"] == 0
    assert db.inserted_rows[0]["ticker"] == "BILL-USD"
    assert db.inserted_rows[0]["status"] == UNIVERSE_STATUS_SHADOW


def test_run_rotation_pass_adaptive_range_floor_filters_quiet_depth():
    """Depth alone should not let a quiet product outrank volatile symbols."""
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=True,
    )
    candidates = ["QUIET-USD", "MID-USD", "VOL-USD"]
    snapshots = {
        "QUIET-USD": _make_candidate(
            ticker="QUIET-USD",
            bid_size_base=10_000.0,
            ask_size_base=10_000.0,
            high_24h=101.0,
            low_24h=99.0,
        ),
        "MID-USD": _make_candidate(
            ticker="MID-USD",
            high_24h=105.0,
            low_24h=95.0,
        ),
        "VOL-USD": _make_candidate(
            ticker="VOL-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: candidates,
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["range_floor_dynamic_bps"] == pytest.approx(1000.0)
    assert out["range_floor_effective_bps"] == pytest.approx(1000.0)
    assert out["gate_rejections"]["range_below_threshold"] == 1
    assert db.inserted_run is not None
    assert db.inserted_run["gate_rejections"]
    assert {r["ticker"] for r in db.inserted_rows} == {"MID-USD", "VOL-USD"}
    assert "QUIET-USD" not in {r["ticker"] for r in db.inserted_rows}


def test_adaptive_range_floor_uses_median_when_target_cannot_fill():
    """A short candidate set should shrink, not backfill its quiet tail."""
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=5,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=0.0,
        universe_adaptive_range_floor_enabled=True,
    )
    candidates = ["QUIET-USD", "MID-USD", "VOL-USD"]
    snapshots = {
        "QUIET-USD": _make_candidate(
            ticker="QUIET-USD",
            high_24h=101.0,
            low_24h=99.0,
        ),
        "MID-USD": _make_candidate(
            ticker="MID-USD",
            high_24h=104.0,
            low_24h=96.0,
        ),
        "VOL-USD": _make_candidate(
            ticker="VOL-USD",
            high_24h=106.0,
            low_24h=94.0,
        ),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: candidates,
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["range_floor_dynamic_bps"] == pytest.approx(800.0)
    assert out["range_floor_effective_bps"] == pytest.approx(800.0)
    assert {r["ticker"] for r in db.inserted_rows} == {"MID-USD", "VOL-USD"}
    assert out["gate_rejections"]["range_below_threshold"] == 1


def test_run_rotation_pass_shadow_fills_depth_shortfall_without_active_promotion():
    """Depth-failed volatile symbols may learn in shadow but cannot promote."""
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"THIN-USD": ("shadow", 1)},
        completed_shadows={"THIN-USD"},
    )
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=150.0,
    )
    snapshots = {
        "GOOD-USD": _make_candidate(ticker="GOOD-USD"),
        "THIN-USD": _make_candidate(
            ticker="THIN-USD",
            bid_size_base=10.0,
            ask_size_base=10.0,
            high_24h=120.0,
            low_24h=80.0,
        ),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["GOOD-USD", "THIN-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["hard_ranked_n"] == 1
    assert out["shadow_exploration_shortfall"] == 1
    assert out["gate_rejections"]["top_of_book_below_threshold"] == 1
    assert out["promoted_to_active"] == 0
    thin = next(r for r in db.inserted_rows if r["ticker"] == "THIN-USD")
    assert thin["status"] == "shadow"


def test_run_rotation_pass_shadow_exploration_requires_probe_sized_depth():
    """A wild but unfillable book should not take a shadow slot."""
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=150.0,
        universe_shadow_min_top_of_book_usd=25.0,
    )
    snapshots = {
        "GOOD-USD": _make_candidate(ticker="GOOD-USD"),
        "MICRO-USD": _make_candidate(
            ticker="MICRO-USD",
            bid_size_base=0.10,
            ask_size_base=0.10,
            high_24h=250.0,
            low_24h=50.0,
        ),
        "THIN-USD": _make_candidate(
            ticker="THIN-USD",
            bid_size_base=1.0,
            ask_size_base=1.0,
            high_24h=120.0,
            low_24h=80.0,
        ),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["GOOD-USD", "MICRO-USD", "THIN-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["shadow_min_top_of_book_usd"] == pytest.approx(25.0)
    assert out["shadow_exploration_shortfall"] == 1
    assert out["gate_rejections"]["shadow_top_of_book_below_probe"] == 1
    tickers = {r["ticker"] for r in db.inserted_rows}
    assert "THIN-USD" in tickers
    assert "MICRO-USD" not in tickers


def test_run_rotation_pass_keeps_shadow_on_transient_depth_miss():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(previous={"THIN-USD": ("shadow", 1)})
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        universe_missing_grace_passes=2,
    )
    snapshots = {
        "GOOD-USD": _make_candidate(ticker="GOOD-USD"),
        "THIN-USD": _make_candidate(
            ticker="THIN-USD",
            bid_size_base=1.0,
            ask_size_base=1.0,
        ),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["GOOD-USD", "THIN-USD"],
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["kept_shadow_missing_grace"] == 1
    assert out["demoted_to_inactive"] == 0
    row = next(r for r in db.inserted_rows if r["ticker"] == "THIN-USD")
    assert row["ticker"] == "THIN-USD"
    assert row["status"] == "shadow"
    assert row["rank"] is None
    assert row["composite_score"] is None
    assert row["top_of_book_usd"] is None


def test_run_rotation_pass_probe_depth_failure_bypasses_missing_grace():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(previous={"MICRO-USD": ("shadow", 1)})
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        universe_missing_grace_passes=2,
        universe_shadow_min_top_of_book_usd=25.0,
    )
    snapshot = _make_candidate(
        ticker="MICRO-USD",
        bid_size_base=0.10,
        ask_size_base=0.10,
        high_24h=120.0,
        low_24h=80.0,
    )

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["MICRO-USD"],
        fetch_snapshot_fn=lambda t: snapshot,
    )

    assert out["kept_shadow_missing_grace"] == 0
    assert out["demoted_to_inactive"] == 1
    assert out["gate_rejections"]["shadow_top_of_book_below_probe"] == 1
    assert db.inserted_rows[0]["ticker"] == "MICRO-USD"
    assert db.inserted_rows[0]["status"] == "inactive"


def test_run_rotation_pass_hard_range_failure_bypasses_missing_grace():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(previous={"QUIET-USD": ("shadow", 1)})
    s = _StubSettings(
        universe_top_n=1,
        universe_hysteresis_ranks=0,
        universe_min_range_24h_bps=150.0,
        universe_missing_grace_passes=2,
    )
    snapshot = _make_candidate(
        ticker="QUIET-USD",
        high_24h=100.1,
        low_24h=99.9,
    )

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["QUIET-USD"],
        fetch_snapshot_fn=lambda t: snapshot,
    )

    assert out["kept_shadow_missing_grace"] == 0
    assert out["demoted_to_inactive"] == 1
    assert db.inserted_rows[0]["ticker"] == "QUIET-USD"
    assert db.inserted_rows[0]["status"] == "inactive"


def test_run_rotation_pass_uses_shadow_exploration_when_active_depth_rejects():
    """If active depth rejects everything, rank shadow-eligible snapshots.

    This keeps learning alive without letting threshold-failed pairs become
    active purely because the exchange-wide gate set was too strict.
    """
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_top_of_book_usd=1_000_000_000_000.0,
        universe_shadow_min_top_of_book_usd=25.0,
    )
    candidates = ["QUIET-USD", "VOL-USD"]
    snapshots = {
        "QUIET-USD": _make_candidate(
            ticker="QUIET-USD",
            high_24h=101.0,
            low_24h=99.0,
        ),
        "VOL-USD": _make_candidate(
            ticker="VOL-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: candidates,
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["exploration_fallback"] is True
    assert out["promoted_to_shadow"] == 2
    assert out["promoted_to_active"] == 0

    assert [(r["ticker"], r["status"]) for r in db.inserted_rows] == [
        ("VOL-USD", "shadow"),
        ("QUIET-USD", "shadow"),
    ]
    assert db.committed is True


def test_shadow_exploration_keeps_volume_and_spread_gates_binding():
    """Fallback must not subscribe low-volume or too-wide-spread products."""
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB()
    s = _StubSettings(
        universe_top_n=2,
        universe_hysteresis_ranks=0,
        universe_min_volume_24h_usd=1_000_000_000_000.0,
    )
    snapshots = {
        "VOL-USD": _make_candidate(
            ticker="VOL-USD",
            high_24h=120.0,
            low_24h=80.0,
        ),
        "WIDE-USD": _make_candidate(
            ticker="WIDE-USD",
            bid=95.0,
            ask=105.0,
            high_24h=120.0,
            low_24h=80.0,
        ),
    }

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: list(snapshots),
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["exploration_fallback"] is False
    assert out["ranked_n"] == 0
    assert out["promoted_to_shadow"] == 0
    assert db.inserted_rows == []
    assert db.committed is True


def test_completed_shadow_promotes_only_with_positive_maker_edge():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"VOL-USD": ("shadow", 1)},
        completed_shadows={"VOL-USD"},
        decay_rows={
            "VOL-USD": [{
                "ticker": "VOL-USD",
                "alert_type": "imbalance_long",
                "score_bucket": "high",
                "horizon_s": 60,
                "sample_count": 75,
                "mean_return": 0.003,
                "m2_return": 0.00000001,
            }],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["VOL-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["promoted_to_active"] == 1
    assert out["edge_promotion_blocks"] == {}
    assert out["promotion_decay_table"] == "fast_signal_decay_maker_filled"
    assert out["promotion_min_samples"] is None
    assert db.inserted_rows[0]["status"] == "active"


def test_completed_shadow_stays_shadow_without_positive_maker_edge():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"VOL-USD": ("shadow", 1)},
        completed_shadows={"VOL-USD"},
        decay_rows={
            "VOL-USD": [{
                "ticker": "VOL-USD",
                "alert_type": "imbalance_long",
                "score_bucket": "high",
                "horizon_s": 60,
                "sample_count": 5,
                "mean_return": 0.002,
                "m2_return": 0.0001,
            }],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["VOL-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["promoted_to_active"] == 0
    assert out["kept_shadow"] == 1
    assert out["edge_promotion_blocks"] == {"uncertain": 1}
    assert db.inserted_rows[0]["status"] == "shadow"


def test_completed_shadow_demotes_when_maker_edge_is_below_cost():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"VOL-USD": ("shadow", 1)},
        completed_shadows={"VOL-USD"},
        decay_rows={
            "VOL-USD": [{
                "ticker": "VOL-USD",
                "alert_type": "imbalance_long",
                "score_bucket": "high",
                "horizon_s": 60,
                "sample_count": 75,
                "mean_return": 0.001,
                "m2_return": 0.00000001,
            }],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["VOL-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["promoted_to_active"] == 0
    assert out["edge_exhausted_demotions"] == 1
    assert out["edge_exhaustion_blocks"] == {"below_cost": 1}
    assert db.inserted_rows[0]["status"] == "inactive"


def test_shadow_window_pending_is_counted_before_edge_promotion_check():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(
        previous={"VOL-USD": ("shadow", 1)},
        completed_shadows=set(),
        decay_rows={
            "VOL-USD": [{
                "ticker": "VOL-USD",
                "alert_type": "imbalance_long",
                "score_bucket": "high",
                "horizon_s": 60,
                "sample_count": 75,
                "mean_return": 0.003,
                "m2_return": 0.00000001,
            }],
        },
    )
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["VOL-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["promoted_to_active"] == 0
    assert out["kept_shadow"] == 1
    assert out["shadow_window_pending"] == 1
    assert out["edge_promotion_blocks"] == {}
    assert db.inserted_rows[0]["status"] == "shadow"


def test_active_pair_demotes_to_shadow_when_edge_evaporates():
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    db = _FakeRotationDB(previous={"VOL-USD": ("active", 1)})
    s = _StubSettings(universe_top_n=1, universe_hysteresis_ranks=0)

    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["VOL-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )

    assert out["kept_active"] == 0
    assert out["demoted_to_shadow"] == 1
    assert out["edge_promotion_blocks"] == {"no_decay_row": 1}
    assert db.inserted_rows[0]["status"] == "shadow"


def test_get_subscribed_pairs_excludes_unranked_shadow_grace():
    from sqlalchemy import create_engine, text

    from app.services.trading.fast_path.universe_rotator import get_subscribed_pairs

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as db:
        db.execute(text("""
            CREATE TABLE fast_path_universe (
                ticker TEXT NOT NULL,
                status TEXT NOT NULL,
                rank INTEGER NULL,
                rotation_at TEXT NOT NULL
            )
        """))
        db.execute(text("""
            INSERT INTO fast_path_universe (ticker, status, rank, rotation_at)
            VALUES
              ('OLD-USD', 'shadow', 1, '2026-05-23T00:00:00'),
              ('RANKED-USD', 'shadow', 1, '2026-05-23T01:00:00'),
              ('ACTIVE-BUFFER-USD', 'active', NULL, '2026-05-23T01:00:00'),
              ('GRACE-USD', 'shadow', NULL, '2026-05-23T01:00:00'),
              ('INACTIVE-USD', 'inactive', NULL, '2026-05-23T01:00:00')
        """))

        pairs = get_subscribed_pairs(db)

    assert pairs == ["RANKED-USD", "ACTIVE-BUFFER-USD"]


def test_get_subscribed_pairs_retains_open_paper_position_tickers():
    from sqlalchemy import create_engine, text

    from app.services.trading.fast_path.universe_rotator import get_subscribed_pairs

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as db:
        db.execute(text("""
            CREATE TABLE fast_path_universe (
                ticker TEXT NOT NULL,
                status TEXT NOT NULL,
                rank INTEGER NULL,
                rotation_at TEXT NOT NULL
            )
        """))
        db.execute(text("""
            CREATE TABLE fast_executions (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                decision TEXT NOT NULL,
                mode TEXT NOT NULL,
                decided_at TEXT NOT NULL
            )
        """))
        db.execute(text("""
            CREATE TABLE fast_exits (
                id INTEGER PRIMARY KEY,
                entry_execution_id INTEGER NOT NULL
            )
        """))
        db.execute(text("""
            INSERT INTO fast_path_universe (ticker, status, rank, rotation_at)
            VALUES
              ('RANKED-USD', 'shadow', 1, '2026-05-23T01:00:00'),
              ('ACTIVE-USD', 'active', 2, '2026-05-23T01:00:00'),
              ('GRACE-USD', 'shadow', NULL, '2026-05-23T01:00:00')
        """))
        db.execute(text("""
            INSERT INTO fast_executions (id, ticker, decision, mode, decided_at)
            VALUES
              (1, 'OPEN-OLD-USD', 'paper_fill', 'paper', '2026-05-23T00:10:00'),
              (2, 'RANKED-USD', 'paper_fill', 'paper', '2026-05-23T00:20:00'),
              (3, 'CLOSED-USD', 'paper_fill', 'paper', '2026-05-23T00:30:00'),
              (4, 'REJECTED-USD', 'rejected', 'paper', '2026-05-23T00:40:00'),
              (5, 'LIVE-USD', 'paper_fill', 'live', '2026-05-23T00:50:00')
        """))
        db.execute(text("""
            INSERT INTO fast_exits (id, entry_execution_id)
            VALUES (10, 3)
        """))

        pairs = get_subscribed_pairs(db)

    assert pairs == ["RANKED-USD", "ACTIVE-USD", "OPEN-OLD-USD"]


# ---------------------------------------------------------------------------
# Book-gate behaviour (f-fastpath-rotator-coinbase-fixes-bundle, 2026-05-08)
# ---------------------------------------------------------------------------
#
# The /book-derived top_of_book_usd gate fails when sizes are too thin.
# Three cases cover the surface:
#   - empty book -> _fetch_book returns None -> sizes stay 0 -> gate rejects
#   - thin book  -> _fetch_book returns small base sizes -> gate rejects
#   - deep book  -> _fetch_book returns large sizes -> gate passes


def test_passes_admission_gates_empty_book_rejected():
    """When _fetch_book returns no sizes (None), candidate carries 0
    bid/ask USD; the top-of-book gate rejects it."""
    from app.services.trading.fast_path.universe_rotator import (
        _PairCandidate,
        passes_admission_gates,
    )
    cand = _PairCandidate(
        ticker="EMPTY-USD",
        volume_24h_base=1_000_000.0,  # huge volume so volume gate passes
        last_price=100.0,
        bid=99.95,
        ask=100.05,
        trades_24h=10_000,
    )
    # _fetch_book returned None -> _bid_size_usd/_ask_size_usd stay at 0
    assert cand.top_of_book_usd == 0.0
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is False
    assert reason == "top_of_book_below_threshold"


def test_passes_admission_gates_thin_book_rejected():
    """Small but non-zero sizes still fail the top-of-book gate when
    USD value is below the threshold."""
    cand = _make_candidate(
        bid_size_base=10.0,  # 10 base * 100 = $1k each side
        ask_size_base=10.0,
    )
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is False
    assert reason == "top_of_book_below_threshold"


def test_passes_admission_gates_deep_book_passes():
    """Deep book sizes clear the top-of-book gate."""
    cand = _make_candidate(
        bid_size_base=500.0,  # 500 base * 100 = $50k each side
        ask_size_base=500.0,
    )
    from app.services.trading.fast_path.universe_rotator import (
        passes_admission_gates,
    )
    ok, reason = passes_admission_gates(
        cand,
        min_volume_24h_usd=10_000_000.0,
        max_spread_bps=10.0,
        min_top_of_book_usd=5_000.0,
        min_trades_24h=1_000,
    )
    assert ok is True
    assert reason is None


# ---------------------------------------------------------------------------
# _fetch_book parser (level=1 payload)
# ---------------------------------------------------------------------------

def test_fetch_book_parses_level1_payload():
    """Mock _http_get_json to return a synthetic level=1 book; assert
    _fetch_book returns ``(bid_size_base, ask_size_base)``."""
    from unittest.mock import patch
    from app.services.trading.fast_path.universe_rotator import _fetch_book

    fake_book = {
        "sequence": 12345,
        "bids": [["99.50", "1.5", 1]],
        "asks": [["100.00", "2.5", 1]],
    }
    with patch(
        "app.services.trading.fast_path.universe_rotator._http_get_json",
        return_value=fake_book,
    ):
        result = _fetch_book("BTC-USD")
    assert result == (1.5, 2.5)


def test_fetch_book_returns_none_on_empty_book():
    """Empty bids/asks -> None (the gate then sees 0 top_of_book_usd
    and rejects appropriately)."""
    from unittest.mock import patch
    from app.services.trading.fast_path.universe_rotator import _fetch_book

    with patch(
        "app.services.trading.fast_path.universe_rotator._http_get_json",
        return_value={"sequence": 1, "bids": [], "asks": []},
    ):
        result = _fetch_book("BTC-USD")
    assert result is None
