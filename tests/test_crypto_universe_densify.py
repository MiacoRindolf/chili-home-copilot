"""Crypto universe densification: the crypto twin of the equity ignition densifier.

The equity ignition loop mirrors the WHOLE equity universe into the NBBO tape
(source='massive_ws_universe') so every name is tick-replayable. Crypto runs a
separate path, so historically only ARMED crypto names left a 'coinbase_ws' tape.
``crypto_l2_drain._nbbo_row_for`` mirrors each eligible name's live top-of-book
into the same NBBO tape (source='coinbase_ws_universe') from the warmed L2 ring —
zero new WS load, write-only, fail-open. These tests cover the pure BBO derivation,
the dedupe, the source tag, and the degenerate fail-open paths.
"""
from __future__ import annotations

import pytest

from app.services.trading.fast_path import crypto_l2_drain
from app.services.trading.fast_path.crypto_l2_drain import _nbbo_row_for


# ── fakes for the in-memory book ring ────────────────────────────────────────

class _Level:
    def __init__(self, price: float, size: float):
        self.price = price
        self.size = size


class _Snap:
    def __init__(self, bids, asks, event_ts=1_700_000_000.0, ts=0.0):
        self.bids = bids
        self.asks = asks
        self.event_ts = event_ts
        self.ts = ts


class _Buf:
    def __init__(self, snap):
        self._snap = snap

    def latest(self, pid):
        return self._snap


@pytest.fixture(autouse=True)
def _reset_dedupe():
    """Each test starts with an empty dedupe map (module-level global)."""
    crypto_l2_drain._last_nbbo.clear()
    yield
    crypto_l2_drain._last_nbbo.clear()


def _patch_book(monkeypatch, snap):
    monkeypatch.setattr(crypto_l2_drain, "get_book_buffer", lambda: _Buf(snap))


# ── BBO derivation ───────────────────────────────────────────────────────────

def test_derives_topofbook_row(monkeypatch):
    snap = _Snap(bids=[_Level(100.0, 5.0), _Level(99.5, 9.0)],
                 asks=[_Level(100.4, 4.0), _Level(100.9, 7.0)])
    _patch_book(monkeypatch, snap)
    row = _nbbo_row_for("TAO-USD")
    assert row is not None
    assert row["symbol"] == "TAO-USD"
    assert row["bid"] == 100.0 and row["ask"] == 100.4
    assert row["mid"] == pytest.approx(100.2)
    assert row["spread_bps"] == pytest.approx((0.4 / 100.2) * 10_000.0)
    assert row["source"] == "coinbase_ws_universe"
    assert row["day_volume"] is None


def test_best_level_skips_zero_size(monkeypatch):
    # the top bid level has size 0 -> the next non-zero level is the real best bid
    snap = _Snap(bids=[_Level(101.0, 0.0), _Level(100.0, 3.0)],
                 asks=[_Level(100.5, 0.0), _Level(100.6, 2.0)])
    _patch_book(monkeypatch, snap)
    row = _nbbo_row_for("ICP-USD")
    assert row["bid"] == 100.0 and row["ask"] == 100.6


# ── dedupe (storage discipline) ──────────────────────────────────────────────

def test_dedupes_unchanged_bbo(monkeypatch):
    snap = _Snap(bids=[_Level(50.0, 1.0)], asks=[_Level(50.2, 1.0)])
    _patch_book(monkeypatch, snap)
    assert _nbbo_row_for("ETH-USD") is not None   # first write
    assert _nbbo_row_for("ETH-USD") is None        # identical BBO -> deduped


def test_changed_bbo_writes_again(monkeypatch):
    s1 = _Snap(bids=[_Level(50.0, 1.0)], asks=[_Level(50.2, 1.0)])
    _patch_book(monkeypatch, s1)
    assert _nbbo_row_for("ETH-USD") is not None
    s2 = _Snap(bids=[_Level(50.1, 1.0)], asks=[_Level(50.2, 1.0)])   # bid moved
    _patch_book(monkeypatch, s2)
    assert _nbbo_row_for("ETH-USD") is not None


def test_dedupe_map_is_bounded(monkeypatch):
    crypto_l2_drain._last_nbbo.update({f"X{i}": (1.0, 2.0) for i in range(crypto_l2_drain._NBBO_DEDUPE_MAX + 1)})
    snap = _Snap(bids=[_Level(9.0, 1.0)], asks=[_Level(9.1, 1.0)])
    _patch_book(monkeypatch, snap)
    row = _nbbo_row_for("NEW-USD")
    assert row is not None
    assert len(crypto_l2_drain._last_nbbo) == 1   # cleared then re-seeded with the new key


# ── fail-open / degenerate ───────────────────────────────────────────────────

def test_empty_book_returns_none(monkeypatch):
    _patch_book(monkeypatch, _Snap(bids=[], asks=[]))
    assert _nbbo_row_for("ABC-USD") is None


def test_none_snap_returns_none(monkeypatch):
    _patch_book(monkeypatch, None)
    assert _nbbo_row_for("ABC-USD") is None


def test_crossed_book_returns_none(monkeypatch):
    # ask < bid -> invalid; do not write
    snap = _Snap(bids=[_Level(100.0, 1.0)], asks=[_Level(99.0, 1.0)])
    _patch_book(monkeypatch, snap)
    assert _nbbo_row_for("ABC-USD") is None


def test_all_zero_size_levels_returns_none(monkeypatch):
    snap = _Snap(bids=[_Level(100.0, 0.0)], asks=[_Level(100.5, 0.0)])
    _patch_book(monkeypatch, snap)
    assert _nbbo_row_for("ABC-USD") is None
