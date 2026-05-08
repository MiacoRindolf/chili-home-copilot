"""Tests for f-fastpath-universe-rotation universe_rotator.

Covers the four admission gates + composite scoring + the run_rotation_pass
state machine (new entrant -> shadow -> active; demotion when dropped).

Helper-level tests use injectable list/snapshot functions so we never
touch the live Coinbase API. The DB-bound run_rotation_pass tests use
the chili_test ``db`` fixture.
"""
from __future__ import annotations

from dataclasses import dataclass
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
):
    from app.services.trading.fast_path.universe_rotator import _PairCandidate
    cand = _PairCandidate(
        ticker=ticker,
        volume_24h_base=volume_24h_base,
        last_price=last_price,
        bid=bid,
        ask=ask,
        trades_24h=trades_24h,
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


# ---------------------------------------------------------------------------
# Composite score property
# ---------------------------------------------------------------------------

def test_composite_score_volume_over_spread():
    """Composite is volume_24h_usd / max(spread_bps, 0.5)."""
    a = _make_candidate(volume_24h_base=1_000_000.0, bid=99.95, ask=100.05)
    # vol = 1M * 100 = 100M; spread = 10 bps; composite = 100M / 10 = 1e7
    assert abs(a.composite_score - 1e7) < 1.0


def test_composite_score_spread_floor():
    """Tiny spread floors at 0.5 bps to avoid division-by-near-zero."""
    a = _make_candidate(volume_24h_base=1_000.0, bid=100.0, ask=100.0001)
    # actual spread is ~0.01 bps -> floored to 0.5; composite = 100k / 0.5 = 200k
    assert a.composite_score == pytest.approx(200_000.0, rel=0.01)


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
    universe_min_trades_24h: int = 1_000


def test_run_rotation_pass_disabled_short_circuits(db):
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    s = _StubSettings(universe_rotation_enabled=False)
    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: ["BTC-USD"],
        fetch_snapshot_fn=lambda t: _make_candidate(ticker=t),
    )
    assert out["skipped_reason"] == "universe_rotation_disabled"
    assert out["scanned"] == 0


def test_run_rotation_pass_first_pass_writes_shadow(db):
    """Brand-new entrants land in status='shadow' on first pass."""
    from sqlalchemy import text
    from app.services.trading.fast_path.universe_rotator import run_rotation_pass

    s = _StubSettings(universe_top_n=3, universe_hysteresis_ranks=0)

    candidates = ["BTC-USD", "ETH-USD", "SOL-USD"]
    snapshots = {
        # Decreasing composite so rank order is stable
        "BTC-USD": _make_candidate(ticker="BTC-USD", volume_24h_base=10_000.0),
        "ETH-USD": _make_candidate(ticker="ETH-USD", volume_24h_base=5_000.0),
        "SOL-USD": _make_candidate(ticker="SOL-USD", volume_24h_base=1_000.0),
    }
    out = run_rotation_pass(
        db, settings=s,
        list_usd_products_fn=lambda: candidates,
        fetch_snapshot_fn=lambda t: snapshots[t],
    )

    assert out["scanned"] == 3
    assert out["promoted_to_shadow"] == 3
    assert out["promoted_to_active"] == 0

    rows = db.execute(text(
        "SELECT ticker, status, rank FROM fast_path_universe ORDER BY rank"
    )).mappings().all()
    assert len(rows) == 3
    assert all(r["status"] == "shadow" for r in rows)
    assert [r["ticker"] for r in rows] == ["BTC-USD", "ETH-USD", "SOL-USD"]


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
