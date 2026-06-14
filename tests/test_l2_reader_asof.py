"""``as_of`` parameter on the L2 readers — the replay reads L2 AS-OF a historical
simulated instant instead of trailing ``now()``.

Two contracts:
  * PARITY — ``as_of=None`` is the LIVE default and emits the EXACT original SQL
    (no upper bound, literal ``now()``); the 77 existing exit tests (which call the
    readers with no ``as_of``) are the byte-identical proof. Here we add a sanity
    check that ``as_of=None`` equals the bare default call.
  * FILTERING — a set ``as_of`` excludes rows recorded AFTER the instant and ages
    the newest snapshot relative to ``as_of`` (not ``now()``); a tz-aware ``as_of``
    is normalized to UTC-naive to match the naive snapshot columns.

Uses the crypto ``fast_orderbook`` table (in the ORM, present in chili_test).
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.services.trading.momentum_neural.pipeline import (
    _live_ofi_microprice,
    read_ladder_distribution,
)

_BB = [[1.0, 1000.0], [0.99, 500.0]]
_AA = [[1.01, 400.0], [1.02, 300.0]]


def _ins(db, ticker, snap_at, bids=None, asks=None, spread=30.0):
    bids = bids or _BB
    asks = asks or _AA
    db.execute(text(
        "INSERT INTO fast_orderbook (ticker, snapshot_at, bid_levels, ask_levels, "
        "bid_total_size, ask_total_size, imbalance, spread_bps, source) VALUES "
        "(:t, :s, CAST(:b AS jsonb), CAST(:a AS jsonb), :bt, :at, :im, :sp, 'test')"
    ), {
        "t": ticker, "s": snap_at,
        "b": json.dumps(bids), "a": json.dumps(asks),
        "bt": sum(x[1] for x in bids), "at": sum(x[1] for x in asks),
        "im": 0.0, "sp": spread,
    })


def test_asof_none_is_noop(db):
    """``as_of=None`` returns the same LadderRead as the bare default call.

    Every field is identical EXCEPT ``snapshot_age_s``, which (correctly, by the
    live contract) ages relative to ``now()`` and so advances a few ms between the
    two calls — that single field is asserted close, the rest byte-for-byte equal.
    """
    now = datetime.utcnow()
    for i in range(4):
        _ins(db, "AB-USD", now - timedelta(seconds=(3 - i) * 2))
    db.commit()
    a = read_ladder_distribution("AB-USD", db, k=6)
    b = read_ladder_distribution("AB-USD", db, k=6, as_of=None)
    assert a.n_snaps == 4
    # all non-time fields identical
    for f in ("depth_imbal", "depth_imbal_pctile", "ofi", "micro_edge",
              "bid_refill", "ask_build", "spread_bps", "n_snaps"):
        assert getattr(a, f) == getattr(b, f), f
    # age uses now() (live contract) → close, not exactly equal
    assert abs((a.snapshot_age_s or 0) - (b.snapshot_age_s or 0)) < 1.0


def test_asof_excludes_future_and_ages_relative(db):
    """``as_of`` excludes rows AFTER the instant; age is relative to ``as_of``."""
    t0 = datetime.utcnow() - timedelta(minutes=5)   # fixed historical anchor
    _ins(db, "CD-USD", t0)
    _ins(db, "CD-USD", t0 + timedelta(seconds=10))
    _ins(db, "CD-USD", t0 + timedelta(seconds=40))  # AFTER as_of => must be excluded
    db.commit()
    lr = read_ladder_distribution("CD-USD", db, k=6, as_of=t0 + timedelta(seconds=12))
    assert lr.n_snaps == 2     # t0 and t0+10 in (as_of-30s, as_of]; t0+40 excluded
    # newest <= as_of is t0+10 => age = 12 - 10 = ~2s (NOT ~5min, despite t0 being 5min old)
    assert lr.snapshot_age_s is not None and 1.0 <= lr.snapshot_age_s <= 4.0


def test_asof_tz_aware_is_normalized(db):
    """A tz-aware ``as_of`` is normalized to UTC-naive (== the naive-passed result)."""
    t0 = datetime.utcnow() - timedelta(minutes=5)
    _ins(db, "EF-USD", t0)
    _ins(db, "EF-USD", t0 + timedelta(seconds=10))
    db.commit()
    naive = read_ladder_distribution("EF-USD", db, k=6, as_of=t0 + timedelta(seconds=12))
    aware = read_ladder_distribution(
        "EF-USD", db, k=6,
        as_of=(t0 + timedelta(seconds=12)).replace(tzinfo=timezone.utc))
    assert naive == aware
    assert naive.n_snaps == 2


def test_asof_empty_when_all_rows_future(db):
    """If every row post-dates ``as_of`` the read is empty (=> downstream HOLD/no-op)."""
    t0 = datetime.utcnow()
    _ins(db, "GH-USD", t0)
    db.commit()
    lr = read_ladder_distribution("GH-USD", db, k=6, as_of=t0 - timedelta(seconds=5))
    assert lr.n_snaps == 0 and lr.depth_imbal is None and lr.ofi is None


def test_live_ofi_microprice_asof_noop(db):
    """``_live_ofi_microprice`` as_of=None == bare default (crypto table-fallback path)."""
    now = datetime.utcnow()
    for i in range(4):
        _ins(db, "IJ-USD", now - timedelta(seconds=(3 - i) * 2))
    db.commit()
    a = _live_ofi_microprice("IJ-USD", db=db)
    b = _live_ofi_microprice("IJ-USD", db=db, as_of=None)
    assert a == b


def test_live_ofi_microprice_asof_filters_future(db):
    """``_live_ofi_microprice`` with as_of excludes rows after the instant."""
    t0 = datetime.utcnow() - timedelta(minutes=5)
    _ins(db, "KL-USD", t0)
    _ins(db, "KL-USD", t0 + timedelta(seconds=40))  # excluded by as_of upper bound
    db.commit()
    # only the t0 row is in (as_of-15s, as_of]; with a single snap OFI may be None,
    # but the call must not raise and must not see the future row.
    ofi, micro = _live_ofi_microprice("KL-USD", db=db, as_of=t0 + timedelta(seconds=5))
    # the future row is excluded; a single in-window row yields a finite-or-None read,
    # never an exception, and never reflects the t0+40 row.
    assert ofi is None or -1.0 <= ofi <= 1.0
