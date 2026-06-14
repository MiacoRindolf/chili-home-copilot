"""Crypto L2 candidate pre-subscribe (2026-06-13).

Warm Coinbase level2 books for the fresh live-eligible crypto candidates so
book_imbalance is populated at SCORING/arm time (equity parity with iqfeed depth).
Must (a) use the idempotent cb_ws.subscribe() DIRECTLY — never price_bus.subscribe_symbol,
which re-appends a tick closure each call (the _on_cb_tick growth); (b) be crypto-only
(-USD) so equity is untouched; (c) fail-open so a WS/DB hiccup never breaks the refresh.
"""
import io

import app.services.trading_scheduler as sched


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def distinct(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


class _FakeWS:
    def __init__(self):
        self.calls = []

    def subscribe(self, pids):
        self.calls.append(list(pids))


def _patch_ws(monkeypatch):
    fake = _FakeWS()
    import app.services.trading.venue.coinbase_spot as cb

    monkeypatch.setattr(cb, "get_coinbase_ws", lambda: fake)
    return fake


def test_subscribes_eligible_crypto_uppercased(monkeypatch):
    fake = _patch_ws(monkeypatch)
    db = _FakeDB([("ORCA-USD",), ("TRUMP-USD",), ("pepe-usd",)])
    n = sched._presubscribe_crypto_l2(db)
    assert n == 3
    assert fake.calls == [["ORCA-USD", "TRUMP-USD", "PEPE-USD"]]


def test_noop_when_no_eligible(monkeypatch):
    fake = _patch_ws(monkeypatch)
    assert sched._presubscribe_crypto_l2(_FakeDB([])) == 0
    assert fake.calls == []  # never call subscribe with an empty set


def test_fail_open_on_db_error():
    class _BoomDB:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    # must swallow and return 0 — the viability refresh must never break
    assert sched._presubscribe_crypto_l2(_BoomDB()) == 0


def test_uses_direct_subscribe_not_pricebus_listener_stacking():
    src = io.open("app/services/trading_scheduler.py", encoding="utf-8").read()
    i = src.index("def _presubscribe_crypto_l2")
    block = src[i:i + 3600]
    assert "get_coinbase_ws().subscribe(" in block       # direct, idempotent (not subscribe_symbol)
    # The crypto-only eligibility filter is now shared with the Phase-0 L2 drain
    # (eligible_crypto_symbols) so the warmed set == the drained set, no drift.
    assert "eligible_crypto_symbols(" in block             # uses the shared filter
    # the actual CALL must be the direct WS subscribe, never the tick-closure stacker
    call_region = block[block.index("eligible_crypto_symbols("):]  # code after the docstring
    assert "subscribe_symbol" not in call_region


def test_shared_eligibility_filter_is_crypto_only():
    """The crypto-only guarantee (live_eligible + -USD) lives in the shared
    eligible_crypto_symbols helper that both the pre-subscribe and the L2 drain use."""
    src = io.open(
        "app/services/trading/fast_path/crypto_l2_drain.py", encoding="utf-8"
    ).read()
    i = src.index("def eligible_crypto_symbols")
    block = src[i:i + 1200]
    assert 'like("%-USD%")' in block                       # crypto-only (equity untouched)
    assert "live_eligible" in block                        # bounded by eligibility, no magic N
    assert "freshness_ts" in block                         # fresh-only


def test_wired_into_crypto_refresh_job():
    src = io.open("app/services/trading_scheduler.py", encoding="utf-8").read()
    i = src.index("def _run_crypto_viability_refresh_job")
    block = src[i:i + 1400]
    assert "_presubscribe_crypto_l2(db)" in block
