"""M5a: Coinbase broker-truth phantom reconcile.

Closes a local Trade whose Coinbase balance is *confirmed* zero (e.g. the
momentum lane already sold it directly), guarded against transient/partial
snapshots by a real-fetch check, a post-entry grace, and a time-confirm window.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import app.services.trading.crypto.exit_monitor as em
from app.services import coinbase_service


class _FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    def add(self, *_a, **_k) -> None:
        pass

    def commit(self) -> None:
        self.commits += 1


def _trade(**kw):
    base = dict(
        id=1, ticker="RSC-USD", broker_source="coinbase", status="open",
        direction="long", entry_price=0.10, quantity=2000.0, position_id=None,
        indicator_snapshot=None, crypto_broker_zero_qty_streak=0,
        entry_date=datetime.utcnow() - timedelta(hours=2), notes="",
        user_id=1, scan_pattern_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_base_currency_parsing() -> None:
    assert em._base_currency("RSC-USD") == "RSC"
    assert em._base_currency("btc-usd") == "BTC"
    assert em._base_currency("DOGEUSD") == "DOGE"


def test_confirm_window_spans_two_cache_snapshots() -> None:
    ttl = em._coinbase_cache_ttl_seconds()
    assert em._broker_zero_reconcile_confirm_seconds() == max(60, 2 * ttl)
    # grace strictly exceeds the confirm window (one extra cache cycle)
    assert em._broker_zero_reconcile_grace_seconds() > em._broker_zero_reconcile_confirm_seconds()


def test_balances_none_when_fetch_empty(monkeypatch) -> None:
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [])
    assert em._coinbase_currency_balances() is None  # disconnected -> unknown


def test_balances_map_on_success(monkeypatch) -> None:
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [
        {"currency": "USD", "available_balance": {"value": "100.0"}, "hold": {"value": "0"}},
        {"currency": "RSC", "available_balance": {"value": "5.0"}, "hold": {"value": "1.0"}},
    ])
    bal = em._coinbase_currency_balances()
    assert bal == {"USD": 100.0, "RSC": 6.0}


def test_unknown_balances_never_closes() -> None:
    db = _t = _FakeDB()
    assert em._maybe_reconcile_broker_zero_qty(db, _trade(), balances=None, now=datetime.utcnow()) is None


def test_non_coinbase_skipped() -> None:
    db = _FakeDB()
    t = _trade(broker_source="robinhood")
    assert em._maybe_reconcile_broker_zero_qty(db, t, balances={"USD": 1.0}, now=datetime.utcnow()) is None


def test_real_balance_clears_marker() -> None:
    db = _FakeDB()
    t = _trade(crypto_broker_zero_qty_streak=3,
               indicator_snapshot={em.CRYPTO_BROKER_ZERO_RECONCILE_SNAPSHOT_KEY: {"first_zero_at": "x"}})
    out = em._maybe_reconcile_broker_zero_qty(db, t, balances={"USD": 100.0, "RSC": 50.0}, now=datetime.utcnow())
    assert out is None
    assert t.crypto_broker_zero_qty_streak == 0
    assert em.CRYPTO_BROKER_ZERO_RECONCILE_SNAPSHOT_KEY not in (t.indicator_snapshot or {})


def test_within_grace_does_not_act() -> None:
    db = _FakeDB()
    t = _trade(entry_date=datetime.utcnow())  # just opened
    out = em._maybe_reconcile_broker_zero_qty(db, t, balances={"USD": 100.0}, now=datetime.utcnow())
    assert out is None  # RSC absent == zero, but inside grace -> wait


def test_first_zero_observation_defers_and_marks() -> None:
    db = _FakeDB()
    t = _trade()
    now = datetime.utcnow()
    out = em._maybe_reconcile_broker_zero_qty(db, t, balances={"USD": 100.0}, now=now)
    assert out == "deferred"
    assert t.crypto_broker_zero_qty_streak == 1
    meta = t.indicator_snapshot[em.CRYPTO_BROKER_ZERO_RECONCILE_SNAPSHOT_KEY]
    assert "first_zero_at" in meta


def test_confirmed_zero_past_window_closes(monkeypatch) -> None:
    closed = {}

    def _fake_close(db, trade, *, exit_px, elapsed_seconds, source="x"):
        closed["px"] = exit_px
        closed["elapsed"] = elapsed_seconds

    monkeypatch.setattr(em, "_close_broker_exited_crypto_trade", _fake_close)
    monkeypatch.setattr(em, "_resolve_recorded_sell_price", lambda db, t: 0.12)

    now = datetime.utcnow()
    confirm = em._broker_zero_reconcile_confirm_seconds()
    first_seen = (now - timedelta(seconds=confirm + 30)).isoformat()
    t = _trade(indicator_snapshot={em.CRYPTO_BROKER_ZERO_RECONCILE_SNAPSHOT_KEY: {"first_zero_at": first_seen}})
    out = em._maybe_reconcile_broker_zero_qty(_FakeDB(), t, balances={"USD": 100.0}, now=now)
    assert out == "closed"
    assert closed["px"] == 0.12
    assert closed["elapsed"] >= confirm


def test_close_helper_pnl_and_reason() -> None:
    t = _trade(entry_price=0.10, quantity=2000.0)
    em._close_broker_exited_crypto_trade(_FakeDB(), t, exit_px=0.12, elapsed_seconds=700.0)
    assert t.status == "closed"
    assert t.exit_price == 0.12
    assert t.pnl == 40.0  # (0.12 - 0.10) * 2000
    assert t.exit_reason == "broker_truth_zero_qty"
    assert t.crypto_broker_zero_qty_streak == 0


def test_close_helper_no_price_leaves_pnl_null() -> None:
    t = _trade()
    em._close_broker_exited_crypto_trade(_FakeDB(), t, exit_px=None, elapsed_seconds=700.0)
    assert t.status == "closed"
    assert t.exit_price is None
    assert t.pnl is None
    assert t.exit_reason == "broker_truth_zero_qty_no_exit_price"
