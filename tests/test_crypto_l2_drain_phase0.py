"""Phase 0 tests: full-book maintainer (coinbase_spot._handle_l2) + crypto L2
drain (fast_path/crypto_l2_drain) + equity-safety/parity.

The keystone regression guarded here is RT-1: the old _handle_l2 rebuilt the
ring from each message's `updates` alone (delta fragments), so a bid-only update
wiped the asks and any unreferenced bids. The full-book maintainer must MERGE
deltas onto a maintained book. Equity must stay byte-identical: no equity key may
ever enter the crypto ring, and no momentum_neural module may read the crypto L2
sink.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import engine
from app.services.trading.microstructure import get_book_buffer, get_features
from app.services.trading.venue.coinbase_spot import (
    CoinbaseWebSocketSeam,
    _parse_cb_ts,
)
from app.services.trading.fast_path import crypto_l2_drain as drain

_TS1 = "2026-06-13T01:20:45.761182Z"
_TS2 = "2026-06-13T01:20:46.000000Z"


@pytest.fixture
def clean_ring():
    """Track product_ids added during a test and remove them from the global
    ring afterward, so the shared singleton stays hermetic across tests."""
    buf = get_book_buffer()
    before = set(buf.product_ids())
    yield buf
    with buf._lock:  # noqa: SLF001 — test-only cleanup of the shared singleton
        for pid in list(buf._books.keys()):
            if pid not in before:
                buf._books.pop(pid, None)


def _snapshot_event(pid):
    return [{
        "type": "snapshot",
        "product_id": pid,
        "updates": [
            {"side": "bid", "price_level": "100.0", "new_quantity": "5"},
            {"side": "bid", "price_level": "99.0", "new_quantity": "3"},
            {"side": "offer", "price_level": "101.0", "new_quantity": "4"},
            {"side": "offer", "price_level": "102.0", "new_quantity": "2"},
        ],
    }]


# ── pure: normalization + timestamp ────────────────────────────────────

def test_norm_imbalance_bounds():
    assert drain._norm_imbalance(3.0, 1.0) == pytest.approx(0.5)
    assert drain._norm_imbalance(1.0, 3.0) == pytest.approx(-0.5)
    assert drain._norm_imbalance(0.0, 0.0) == 0.0
    # always within [-1, 1]
    for b, a in [(1e9, 1.0), (0.0, 5.0), (5.0, 0.0), (2.5, 2.5)]:
        assert -1.0 <= drain._norm_imbalance(b, a) <= 1.0


def test_parse_cb_ts():
    z = _parse_cb_ts(_TS1)
    nanos = _parse_cb_ts("2026-06-13T01:20:45.761182947Z")  # nanosecond precision
    assert z is not None and nanos is not None
    assert abs(z - nanos) < 1e-3  # truncation to micros, not a crash
    assert _parse_cb_ts(None) is None
    assert _parse_cb_ts("") is None
    assert _parse_cb_ts("garbage") is None
    assert _parse_cb_ts(1781313646.0) == 1781313646.0  # epoch passthrough


# ── full-book maintainer (RT-1 keystone) ───────────────────────────────

def test_handle_l2_maintains_full_book(clean_ring):
    """A bid-only UPDATE must keep the asks and the untouched bids — proves the
    book is MERGED, not rebuilt from the delta (the RT-1 regression)."""
    ws = CoinbaseWebSocketSeam()
    pid = "FULLBOOK-USD"
    ws._handle_l2(_snapshot_event(pid), _TS1)
    snap = clean_ring.latest(pid)
    assert snap is not None
    assert snap.bids[0].price == 100.0  # best bid
    assert snap.asks[0].price == 101.0  # best ask
    assert snap.event_ts == pytest.approx(_parse_cb_ts(_TS1))  # exchange event time
    assert snap.ts > 0  # local arrival (ring recency)

    # bid-only update: delete 100, add a new best 100.5
    ws._handle_l2([{
        "type": "update",
        "product_id": pid,
        "updates": [
            {"side": "bid", "price_level": "100.0", "new_quantity": "0"},   # delete
            {"side": "bid", "price_level": "100.5", "new_quantity": "7"},   # new best
        ],
    }], _TS2)
    snap2 = clean_ring.latest(pid)
    bid_prices = [l.price for l in snap2.bids]
    assert snap2.bids[0].price == 100.5            # new best bid
    assert 100.0 not in bid_prices                  # deleted level gone
    assert 99.0 in bid_prices                       # untouched snapshot level RETAINED
    assert snap2.asks[0].price == 101.0             # asks SURVIVED a bid-only update
    assert len(snap2.asks) == 2                      # both asks intact
    assert snap2.event_ts == pytest.approx(_parse_cb_ts(_TS2))


def test_handle_l2_delete_on_zero(clean_ring):
    ws = CoinbaseWebSocketSeam()
    pid = "DELZERO-USD"
    ws._handle_l2(_snapshot_event(pid), _TS1)
    ws._handle_l2([{
        "type": "update",
        "product_id": pid,
        "updates": [{"side": "offer", "price_level": "101.0", "new_quantity": "0"}],
    }], _TS2)
    snap = clean_ring.latest(pid)
    ask_prices = [l.price for l in snap.asks]
    assert 101.0 not in ask_prices       # best ask removed
    assert snap.asks[0].price == 102.0   # next ask promoted


def test_handle_l2_falls_back_to_wallclock_without_ts(clean_ring):
    ws = CoinbaseWebSocketSeam()
    pid = "NOTS-USD"
    ws._handle_l2(_snapshot_event(pid), None)  # no exchange timestamp
    snap = clean_ring.latest(pid)
    assert snap is not None and snap.event_ts > 0  # wall-clock fallback, no crash


# ── ring -> fast_orderbook param mapping ───────────────────────────────

def test_book_item_for_maps_ring(clean_ring):
    ws = CoinbaseWebSocketSeam()
    pid = "MAPITEM-USD"
    ws._handle_l2(_snapshot_event(pid), _TS1)
    item = drain._book_item_for(pid)
    assert item is not None
    assert item["ticker"] == pid
    assert item["source"] == "coinbase"
    assert -1.0 <= item["imbalance"] <= 1.0            # NORMALIZED (RT-9)
    # bid_total 8 (5+3), ask_total 6 (4+2) -> (8-6)/14
    assert item["imbalance"] == pytest.approx((8 - 6) / 14.0)
    assert item["snapshot_at"].tzinfo is None           # naive UTC (RT-3)
    bids = json.loads(item["bid_levels"])
    assert [tuple(x) for x in bids][0] == (100.0, 5.0)


def test_book_item_for_none_when_one_sided(clean_ring):
    ws = CoinbaseWebSocketSeam()
    pid = "ONESIDE-USD"
    ws._handle_l2([{
        "type": "snapshot",
        "product_id": pid,
        "updates": [{"side": "bid", "price_level": "100.0", "new_quantity": "5"}],
    }], _TS1)
    assert drain._book_item_for(pid) is None  # no asks -> skip


# ── drain writes fast_orderbook ────────────────────────────────────────

def test_drain_writes_fast_orderbook(clean_ring, monkeypatch):
    pid = "DRAINW-USD"
    ws = CoinbaseWebSocketSeam()
    ws._handle_l2(_snapshot_event(pid), _TS1)
    monkeypatch.setattr(drain, "eligible_crypto_symbols", lambda db: [pid])
    # clean any prior rows for this synthetic ticker
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fast_orderbook WHERE ticker = :t"), {"t": pid})
    try:
        drain.run_crypto_l2_drain_job()
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT source, imbalance FROM fast_orderbook WHERE ticker = :t"
            ), {"t": pid}).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "coinbase"
        assert -1.0 <= rows[0][1] <= 1.0
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM fast_orderbook WHERE ticker = :t"), {"t": pid})


def test_drain_noop_when_not_eligible(clean_ring, monkeypatch):
    pid = "NOTELIG-USD"
    ws = CoinbaseWebSocketSeam()
    ws._handle_l2(_snapshot_event(pid), _TS1)
    monkeypatch.setattr(drain, "eligible_crypto_symbols", lambda db: [])  # none eligible
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fast_orderbook WHERE ticker = :t"), {"t": pid})
    drain.run_crypto_l2_drain_job()
    with engine.begin() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM fast_orderbook WHERE ticker = :t"
        ), {"t": pid}).scalar()
    assert n == 0  # nothing warmed-and-eligible -> nothing written


# ── EQUITY SAFETY / PARITY ─────────────────────────────────────────────

def test_equity_features_all_none_after_crypto_drain(clean_ring, monkeypatch):
    """The crypto ring/drain must never make get_features() return data for an
    equity ticker — equity gets the all-None default that alerts/scanner skip."""
    pid = "PARITY-USD"
    ws = CoinbaseWebSocketSeam()
    ws._handle_l2(_snapshot_event(pid), _TS1)
    monkeypatch.setattr(drain, "eligible_crypto_symbols", lambda db: [pid])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fast_orderbook WHERE ticker = :t"), {"t": pid})
    try:
        drain.run_crypto_l2_drain_job()
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM fast_orderbook WHERE ticker = :t"), {"t": pid})
    eq = get_features("AAPL")
    assert eq.bid_ask_imbalance is None
    assert eq.depth_bid_total is None
    assert eq.spread_bps is None
    assert "AAPL" not in get_book_buffer().product_ids()


def test_no_equity_keys_enter_ring(clean_ring):
    ws = CoinbaseWebSocketSeam()
    ws._handle_l2(_snapshot_event("CRYPTOONLY-USD"), _TS1)
    pids = get_book_buffer().product_ids()
    assert all(p.endswith("-USD") for p in pids if p in {"CRYPTOONLY-USD"})
    assert "AAPL" not in pids and "TSLA" not in pids


def _references_in_code(src: str, needle: str) -> bool:
    """True iff ``needle`` appears in EXECUTABLE code — a (non-docstring) string
    literal (e.g. a SQL query ``... FROM fast_orderbook ...``), an identifier, or an
    import — as opposed to merely a docstring/comment mention. Comments are absent
    from the AST entirely; docstrings (the first string statement of a module / class
    / function) are the only string literals excluded. This is the original bare
    substring scan MINUS prose-only hits, so the isolation guarantee is not weakened:
    any real read/import of the sink is still flagged. Falls back to a bare substring
    scan if the file cannot be parsed (fail-closed)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover - never expected for in-tree modules
        return needle in src
    docstring_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_ids and needle in node.value:
                return True  # SQL query string / non-docstring literal -> a real read
        elif isinstance(node, ast.Name) and needle in node.id:
            return True
        elif isinstance(node, ast.Attribute) and needle in node.attr:
            return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module and needle in node.module) or any(needle in a.name for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(needle in a.name for a in node.names):
                return True
    return False


def test_momentum_neural_equity_path_isolated_from_crypto_l2_sink():
    """Equity decisions must never be perturbed by crypto rows. The crypto L2 sink
    table (``fast_orderbook``) may be READ only in ``pipeline.py`` and only inside
    ``-USD``-gated code: the crypto OFI/micro read and the v2 ladder reader fall back
    to the durable, cross-process table when the in-process ring is empty in this
    process (the JASMY case — a held name absent from the ring → ofi=None → the exit
    lock could never fire). The equity branch stays on ``iqfeed_depth_snapshots``; the
    crypto L2 WRITE path (``crypto_l2_drain``) and the microstructure audit log are
    never imported by the equity decision path."""
    mn = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "momentum_neural"
    writepath_offenders = []   # the write path / audit log must never be imported here
    sink_offenders = []        # the sink table may be READ ONLY in pipeline.py
    for py in mn.rglob("*.py"):
        txt = py.read_text(encoding="utf-8", errors="ignore")
        # IMPORTING the write-path module (not the bare substring — the legitimate
        # config key ``chili_crypto_l2_drain_seconds`` reads the drain CADENCE, which
        # is a number, not the module) or reading the audit-log table is forbidden.
        if (
            "crypto_l2_drain import" in txt
            or "import crypto_l2_drain" in txt
            or "trading_microstructure_log" in txt
        ):
            writepath_offenders.append(py.name)
        # Match a REAL read of the sink table (SQL string / identifier / import), not a
        # bare prose mention: ``entry_gates.py`` documents that its reusable L2 reader is
        # class-aware ("crypto ``fast_orderbook``") in a DOCSTRING but never queries the
        # table — the actual reads live in the crypto-gated ``pipeline.py`` reader it calls.
        if py.name != "pipeline.py" and _references_in_code(txt, "fast_orderbook"):
            sink_offenders.append(py.name)
    assert writepath_offenders == [], (
        f"momentum_neural imports the crypto L2 write path/audit log: {writepath_offenders}"
    )
    assert sink_offenders == [], (
        f"fast_orderbook read outside the crypto-gated pipeline.py reader: {sink_offenders}"
    )
    # The reads in pipeline.py are crypto-GATED — equity stays on the iqfeed source.
    pipe = (mn / "pipeline.py").read_text(encoding="utf-8", errors="ignore")
    assert "iqfeed_depth_snapshots" in pipe                 # equity L2 source (untouched)
    assert 'endswith("-USD")' in pipe                       # fast_orderbook reads are -USD-gated
