"""Phase 1a tests: log-only L2 signal layer (compute + drain + migration +
no-decision-wiring). The forward-return backfill is Phase 1b (separate)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import engine
from app.migrations import _migration_306_trading_microstructure_log
from app.services.trading.microstructure import BookLevel, BookSnapshot, get_book_buffer
from app.services.trading.fast_path import microstructure_log as ml


def _snap(pid, bids, asks, ts, event_ts=None):
    return BookSnapshot(
        product_id=pid,
        bids=[BookLevel(price=p, size=s, side="bid") for p, s in bids],
        asks=[BookLevel(price=p, size=s, side="offer") for p, s in asks],
        ts=ts,
        event_ts=event_ts if event_ts is not None else ts,
    )


@pytest.fixture
def clean_ring():
    buf = get_book_buffer()
    before = set(buf.product_ids())
    yield buf
    with buf._lock:  # noqa: SLF001
        for pid in list(buf._books.keys()):
            if pid not in before:
                buf._books.pop(pid, None)


# ── signal math (pure) ─────────────────────────────────────────────────

def test_ofi_bid_lift():
    prev = _snap("X-USD", [(100.0, 5.0)], [(101.0, 4.0)], 1.0)
    cur = _snap("X-USD", [(100.5, 7.0)], [(101.0, 4.0)], 2.0)
    # bid price rose -> e_b = cur bid size (7); ask unchanged -> e_a = 0
    assert ml._ofi([prev, cur]) == pytest.approx(7.0)


def test_ofi_ask_lift_negative_contribution():
    prev = _snap("X-USD", [(100.0, 5.0)], [(101.0, 4.0)], 1.0)
    cur = _snap("X-USD", [(100.0, 5.0)], [(102.0, 3.0)], 2.0)
    # bid unchanged -> e_b=0; ask price ROSE -> e_a = -prev ask size (-4); ofi = 0 - (-4) = +4
    assert ml._ofi([prev, cur]) == pytest.approx(4.0)


def test_ask_eaten_counts_and_notional():
    prev = _snap("X-USD", [(100.0, 5.0)], [(101.0, 4.0)], 1.0)
    cur = _snap("X-USD", [(100.0, 5.0)], [(102.0, 3.0)], 2.0)
    events, notional = ml._ask_eaten([prev, cur])
    assert events == 1
    assert notional == pytest.approx(4.0 * 101.0)


def test_micro_price_pulls_toward_heavy_side(clean_ring):
    # bid-heavy book -> micro-price above mid. Use near-now ts so recent() keeps them.
    now = time.time()
    clean_ring.update(_snap("MICRO-USD", [(100.0, 90.0)], [(101.0, 10.0)], now - 2))
    clean_ring.update(_snap("MICRO-USD", [(100.0, 90.0)], [(101.0, 10.0)], now - 1))
    sig = ml._compute_signals_crypto("MICRO-USD", ofi_window_s=60.0)
    assert sig is not None
    assert sig["micro_price"] > sig["mid_price"]          # bid-heavy -> micro above mid
    assert sig["microprice_edge_bps"] > 0
    assert -1.0 <= sig["book_imbalance"] <= 1.0
    assert sig["book_imbalance"] == pytest.approx((90 - 10) / 100.0)
    assert sig["snapshot_count"] == 2


def test_compute_returns_none_on_thin_window(clean_ring):
    clean_ring.update(_snap("THIN-USD", [(100.0, 5.0)], [(101.0, 4.0)], time.time()))
    assert ml._compute_signals_crypto("THIN-USD", ofi_window_s=60.0) is None  # 1 snap < 2


# ── migration idempotency ──────────────────────────────────────────────

def test_migration_306_idempotent():
    with engine.connect() as conn:
        _migration_306_trading_microstructure_log(conn)
        _migration_306_trading_microstructure_log(conn)  # twice -> no error
        exists = conn.execute(text("SELECT to_regclass('public.trading_microstructure_log')")).scalar()
        default_exists = conn.execute(
            text("SELECT to_regclass('public.trading_microstructure_log_default')")
        ).scalar()
    assert exists is not None
    assert default_exists is not None


# ── drain writes the log table ─────────────────────────────────────────

def test_drain_writes_log_rows(clean_ring, monkeypatch):
    pid = "MLDRAIN-USD"
    now = time.time()
    clean_ring.update(_snap(pid, [(100.0, 5.0)], [(101.0, 4.0)], now - 2))
    clean_ring.update(_snap(pid, [(100.5, 7.0)], [(101.0, 4.0)], now - 1))
    monkeypatch.setattr(ml, "eligible_crypto_symbols", lambda db: [pid])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM trading_microstructure_log WHERE symbol = :s"), {"s": pid})
    try:
        ml.run_microstructure_log_drain_job()
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT asset_class, source, eligibility_state, book_imbalance, "
                "snapshot_count, fwd_label_at FROM trading_microstructure_log WHERE symbol = :s"
            ), {"s": pid}).fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r[0] == "crypto" and r[1] == "coinbase_ws" and r[2] == "eligible"
        assert -1.0 <= r[3] <= 1.0          # normalized imbalance
        assert r[4] >= 2                     # snapshot_count
        assert r[5] is None                  # fwd_label_at NULL (1b backfill fills it)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM trading_microstructure_log WHERE symbol = :s"), {"s": pid})


def test_drain_noop_when_not_eligible(clean_ring, monkeypatch):
    pid = "MLNONE-USD"
    clean_ring.update(_snap(pid, [(100.0, 5.0)], [(101.0, 4.0)], 1.0))
    clean_ring.update(_snap(pid, [(100.5, 7.0)], [(101.0, 4.0)], 2.0))
    monkeypatch.setattr(ml, "eligible_crypto_symbols", lambda db: [])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM trading_microstructure_log WHERE symbol = :s"), {"s": pid})
    ml.run_microstructure_log_drain_job()
    with engine.begin() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM trading_microstructure_log WHERE symbol = :s"
        ), {"s": pid}).scalar()
    assert n == 0


# ── no decision wiring (equity-safety) ─────────────────────────────────

def test_momentum_neural_does_not_read_micro_log():
    mn = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "momentum_neural"
    offenders = [
        py.name for py in mn.rglob("*.py")
        if "trading_microstructure_log" in py.read_text(encoding="utf-8", errors="ignore")
        or "microstructure_log" in py.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == [], f"momentum_neural reads the log-only layer: {offenders}"


def test_no_decision_module_imports_micro_log():
    base = Path(__file__).resolve().parents[1] / "app" / "services" / "trading"
    for fname in ("auto_trader.py", "momentum_neural/live_runner.py", "momentum_neural/entry_gates.py"):
        p = base / fname
        if p.exists():
            txt = p.read_text(encoding="utf-8", errors="ignore")
            assert "microstructure_log" not in txt, f"{fname} imports the log-only layer"
