"""Crypto exemption from the equity NBBO-tape freshness gate (2026-06-13).

momentum_nbbo_spread_tape records EQUITY only, so _filter_fresh_tape was silently
dropping EVERY crypto candidate (flag on but never arming). Crypto (-USD) is now
exempt; equity behavior must stay byte-identical (only fresh equity passes).
"""
from types import SimpleNamespace

import app.db as _appdb
from app.services.trading.momentum_neural import auto_arm


def _row(sym):
    return SimpleNamespace(symbol=sym)


class _FakeRes:
    def __init__(self, syms):
        self._syms = list(syms)

    def __iter__(self):
        return iter([(s,) for s in self._syms])


class _FakeSession:
    """Stand-in for the equity NBBO-tape query: returns the given fresh symbols."""
    def __init__(self, fresh):
        self._fresh = fresh

    def execute(self, *a, **k):
        return _FakeRes(self._fresh)

    def rollback(self):
        pass

    def close(self):
        pass


def test_all_crypto_passes_without_any_tape(monkeypatch):
    # No equity in the set => early return, no DB hit => ALL crypto pass (the bug fix).
    called = {"db": False}
    monkeypatch.setattr(_appdb, "SessionLocal", lambda: (_ for _ in ()).throw(AssertionError("DB hit for all-crypto")))
    rows = [_row("ETH-USD"), _row("DOGE-USD"), _row("ORCA-USD")]
    out = auto_arm._filter_fresh_tape(rows)
    assert {r.symbol for r in out} == {"ETH-USD", "DOGE-USD", "ORCA-USD"}


def test_mixed_crypto_exempt_equity_still_gated(monkeypatch):
    # Equity gate reports only AAPL fresh. Expect: crypto passes (exempt),
    # AAPL passes (fresh), STALE equity dropped (gate still works = equity parity).
    monkeypatch.setattr(_appdb, "SessionLocal", lambda: _FakeSession(["AAPL"]))
    rows = [_row("ETH-USD"), _row("AAPL"), _row("STALE")]
    out = {r.symbol for r in auto_arm._filter_fresh_tape(rows)}
    assert "ETH-USD" in out          # crypto exempt
    assert "AAPL" in out             # fresh equity passes
    assert "STALE" not in out        # stale equity still dropped (unchanged)


def test_all_equity_behaviour_unchanged(monkeypatch):
    # Pure-equity set: identical to the old gate — only fresh symbols survive.
    monkeypatch.setattr(_appdb, "SessionLocal", lambda: _FakeSession(["NVDA"]))
    rows = [_row("NVDA"), _row("OLDCO")]
    out = {r.symbol for r in auto_arm._filter_fresh_tape(rows)}
    assert out == {"NVDA"}           # equity byte-identical: stale OLDCO dropped


def test_empty_rows_passthrough():
    assert auto_arm._filter_fresh_tape([]) == []
