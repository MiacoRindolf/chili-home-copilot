"""P1 NOTIFY selection: class-aware per-symbol coalescing (2026-09-02 open lag).

NASUKAT (raw psycopg2, chili_b_test): 1,800 distinct 660-B notifies kada
release batch => COMMIT 1.04-1.43 s dahil sa pg_notify SLRU pages na
isinusulat sa commit; 200 payload => 112 ms. Sa 13:30Z open ang isang
3,600-event na batch ay sumasaklaw lamang ng ~1-2 s ng tape (~70-300 na
natatanging symbol), kaya karamihan ng payload ay pinapatay ng consumer
mismo.

Ang mapanganib na bersyon ng optimisasyon na ito ay ang WALANG-KLASENG
"newest per symbol": ang bridge ay naglalabas ng DALAWANG klase ng quote --
trade-fenced (authority) at own-clock (2026-08-28 fix). Parehong tumatanggi
ang captured-paper trigger at ang live runner loop sa own-clock rows, kaya
ang isang mas bagong own-clock row na pumatay sa isang trade-fenced row ay
magbibigay sa consumer ng ZERO authoritative notify para sa symbol na iyon.
Ang mga test dito ang nagpi-pin ng klase-awareness at ng subsequence order.

DB-free. Runnable: pytest tests/test_trade_bridge_drain_0902_notify.py -v
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

_BRIDGE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "iqfeed_trade_bridge.py"
)
_MODULE = "iqfeed_trade_bridge_drain0902"
if _MODULE not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MODULE, _BRIDGE_PATH)
    bridge = importlib.util.module_from_spec(_spec)
    sys.modules[_MODULE] = bridge
    _spec.loader.exec_module(bridge)
else:  # pragma: no cover - module cached across test files
    bridge = sys.modules[_MODULE]

_T0 = datetime(2026, 9, 2, 13, 30, 0, tzinfo=timezone.utc)


def _quote(
    symbol: str,
    sequence: int,
    *,
    own_clock: bool = False,
    age_s: float = 0.05,
) -> dict:
    received_at = _T0 - timedelta(seconds=age_s)
    return {
        "sym": symbol,
        "at": received_at,
        "bid": 1.0,
        "ask": 1.02,
        "mid": 1.01,
        "spread_bps": 200.0,
        "provider_at": received_at if own_clock else None,
        "received_at": received_at,
        "provider_trade_reference_at": received_at,
        "basis": (
            bridge.QUOTE_EVENT_TIMESTAMP_BASIS
            if own_clock
            else bridge.AUTHORITATIVE_TIMESTAMP_BASIS
        ),
        "bridge": bridge.BRIDGE_BUILD,
        "message_type": "Q",
        "bridge_run_id": bridge.BRIDGE_RUN_ID,
        "connection_generation": 1,
        "source_frame_sequence": sequence,
        "source_frame_sha256": hashlib.sha256(f"f{sequence}".encode()).hexdigest(),
    }


def _trade(symbol: str, sequence: int) -> dict:
    row = _quote(symbol, sequence)
    row.update({"px": 1.01, "sz": 100.0})
    return row


def test_notify_coalesce_keeps_newest_trade_fenced_per_symbol():
    rows = [
        _quote("AAA", 1),
        _quote("BBB", 2),
        _quote("AAA", 3),
        _quote("AAA", 4),
        _quote("BBB", 5),
    ]
    selected = bridge._select_notify_rows(rows, [], coalesce=True, max_age_s=0.0)
    assert [row["source_frame_sequence"] for row in selected] == [4, 5]
    # Byte-identical envelope: coalescing selects rows, it never rewrites them.
    assert bridge._notify_payload(selected[0]) == bridge._notify_payload(rows[3])


def test_notify_own_clock_newest_never_suppresses_older_trade_fenced():
    fenced = _quote("AAA", 1)
    own_clock = _quote("AAA", 2, own_clock=True)
    selected = bridge._select_notify_rows(
        [fenced, own_clock], [], coalesce=True, max_age_s=0.0
    )
    assert selected == [fenced, own_clock]
    assert bridge._notify_row_class(fenced) == "trade_fenced"
    assert bridge._notify_row_class(own_clock) == "own_clock"


def test_notify_trade_frame_quotes_always_notified():
    # The captured-PAPER trigger matches the notify against the captured
    # exact-print source by equal source_frame_sequence AND equal bid/ask, so a
    # quote sharing a frame with a trade can never be coalesced away.
    provenance_quote = _quote("AAA", 1)
    newer_quote = _quote("AAA", 9)
    selected = bridge._select_notify_rows(
        [provenance_quote, newer_quote],
        [_trade("AAA", 1)],
        coalesce=True,
        max_age_s=0.0,
    )
    assert selected == [provenance_quote, newer_quote]


def test_notify_selected_is_subsequence_and_monotonic_in_sequence():
    rows = [
        _quote(symbol, sequence)
        for sequence, symbol in enumerate(
            ["AAA", "BBB", "CCC", "AAA", "BBB", "CCC", "AAA"], start=1
        )
    ]
    selected = bridge._select_notify_rows(rows, [], coalesce=True, max_age_s=0.0)
    positions = [rows.index(row) for row in selected]
    assert positions == sorted(positions)
    sequences = [row["source_frame_sequence"] for row in selected]
    assert sequences == sorted(sequences)
    assert all(row in rows for row in selected)


def test_notify_age_filter_off_by_default_and_recommended_value_pins_authoritative_max_age():
    # OFF by default: iqfeed_wake_listener has NO age gate of its own, so a
    # bridge-side age drop silences the quiet-tape wake rail for the whole
    # duration of any backlog.
    assert bridge.IQFEED_NOTIFY_MAX_AGE_S == 0.0
    stale = _quote("AAA", 1, age_s=45.0)
    fresh = _quote("BBB", 2, age_s=0.05)
    assert bridge._select_notify_rows(
        [stale, fresh], [], coalesce=False, max_age_s=0.0
    ) == [stale, fresh]
    # The DOCUMENTED recommendation when an operator does enable it.
    recommended = bridge.AUTHORITATIVE_MAX_AGE_S
    assert recommended == 2.0
    assert bridge._select_notify_rows(
        [stale, fresh], [], coalesce=False, max_age_s=recommended,
        available_at=_T0,
    ) == [fresh]
    # Rule 1 still wins: a trade-fenced provenance quote is never age-dropped.
    assert bridge._select_notify_rows(
        [stale], [_trade("AAA", 1)], coalesce=True, max_age_s=recommended,
        available_at=_T0,
    ) == [stale]


def test_notify_coalesce_disabled_is_identity():
    rows = [_quote("AAA", 1), _quote("AAA", 2), _quote("AAA", 3)]
    assert bridge._select_notify_rows(
        rows, [], coalesce=False, max_age_s=0.0
    ) == rows
    assert bridge._select_notify_rows([], [], coalesce=True) == []


def test_enqueue_notifications_emits_only_the_selected_rows(monkeypatch):
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_ENABLED", True)
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_COALESCE_PER_SYMBOL", True)
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_MAX_AGE_S", 0.0)
    quote_rows = [_quote("AAA", 1), _quote("AAA", 2), _quote("BBB", 3)]

    class _Result:
        rowcount = 2

    calls: list = []

    class _Connection:
        def execute(self, statement, *args, **kwargs):
            calls.append(statement)
            return _Result()

    emitted = bridge._enqueue_nbbo_notifications(
        _Connection(),
        quote_rows=quote_rows,
        available_at=_T0,
        trade_rows=[],
    )
    assert emitted == 2
    assert len(calls) == 1
    # available_at is stamped on EVERY released row, selected or not: the row
    # dicts are the same objects the capture handoff publishes.
    assert all(row["available_at"] == _T0 for row in quote_rows)


def test_enqueue_notifications_rowcount_mismatch_raises(monkeypatch):
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_ENABLED", True)
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_COALESCE_PER_SYMBOL", True)

    class _Result:
        rowcount = 99

    class _Connection:
        def execute(self, statement, *args, **kwargs):
            return _Result()

    with pytest.raises(RuntimeError, match="row-count mismatch"):
        bridge._enqueue_nbbo_notifications(
            _Connection(),
            quote_rows=[_quote("AAA", 1)],
            available_at=_T0,
            trade_rows=[],
        )
