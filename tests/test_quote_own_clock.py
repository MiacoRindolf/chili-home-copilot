"""QUOTE OWN-CLOCK — ang 2-segundong trade fence na bumubulag sa BBO (2026-08-28).

Ang dating `quote_captured` ay nag-a-age ng quote laban sa timestamp ng HULING
TRADE na may 2.0s ceiling — kaya sa 480-symbol na watch, LAHAT ng quote update
ng pangalang hindi pumiprint kada 2s ay itinatapon. SINUKAT: 2.4–3.9M quote
frame/oras ang nawawala, ang premarket BBO ay 15-ORAS na luma (BDRX/RDIB), at
ang manipis na pangalan (AREN) ay naharangan sa entry nang 171×/40min.

Ang "Bid Time"/"Ask Time" ay NASA selected fields na — totoong quote-event
clock. Ang or-leg ay nag-a-age ng quote sa SARILING clock nito; ang mga row ay
may dalang totoong provider_at + sariling basis. Trade-fenced na landas ay
byte-identical.

Runnable: pytest tests/test_quote_own_clock.py -v
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "iqfeed_trade_bridge.py"
_spec = importlib.util.spec_from_file_location("iqfeed_trade_bridge_qoc", _BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("iqfeed_trade_bridge_qoc", bridge)
_spec.loader.exec_module(bridge)


def _values(**over) -> dict:
    base = {
        "Symbol": "AREN",
        "Most Recent Trade": "2.11",
        "Most Recent Trade Size": "100",
        "Most Recent Trade Time": "12:00:00.000000",
        "Most Recent Trade Date": "2026-08-28",
        "Most Recent Trade Market Center": "Q",
        "Most Recent Trade Conditions": "",
        "TickID": "17",
        "Bid": "2.10",
        "Bid Size": "200",
        "Bid Time": "12:42:06.999999",
        "Ask": "2.12",
        "Ask Size": "300",
        "Ask Time": "12:42:07.000000",
        "Total Volume": "10000",
        "Delay": "0",
        "Message Contents": "ba",
        "Decimal Precision": "4",
    }
    base.update(over)
    return base


def _parse(monkeypatch, values: dict, received_at: datetime, generation: int = 23):
    bridge._activate_connection_generation(generation)
    ack_line = "S,CURRENT UPDATE FIELDNAMES," + ",".join(
        bridge.SELECTED_UPDATE_FIELDS
    ) + ","
    ack_sha256 = hashlib.sha256(ack_line.encode()).hexdigest()
    assert bridge._observe_selected_update_fields_ack(
        ack_line, connection_generation=generation, source_frame_sha256=ack_sha256
    )
    frame = "Q," + ",".join(values[f] for f in bridge.SELECTED_UPDATE_FIELDS)
    calls: list[dict] = []
    monkeypatch.setattr(
        bridge, "_enqueue_pending_frame", lambda **kw: calls.append(kw)
    )
    ok = bridge._parse_selected_l1(
        frame,
        connection_generation=generation,
        selected_fields_ack_sha256=ack_sha256,
        received_at=received_at,
    )
    return ok, calls


def _quote_rows(calls):
    return [c.get("quote_row") for c in calls if c.get("quote_row") is not None]


def test_stale_trade_fresh_quote_is_now_captured_on_its_own_clock(monkeypatch):
    """ANG AREN KILL: huling trade 42 min ang tanda, pero ang bid/ask ay
    kaka-update lang — dating ITINATAPON, ngayon huli sa sariling clock."""
    # received 16:42:07.1 UTC = 12:42:07.1 ET; Bid/Ask Time = 12:42:06.9/07.0 ET
    received = datetime(2026, 8, 28, 16, 42, 7, 100000, tzinfo=timezone.utc)
    (_pv, qcap), calls = _parse(monkeypatch, _values(), received, generation=31)
    assert qcap is True
    rows = _quote_rows(calls)
    assert rows, "dapat may quote row"
    q = rows[-1]
    assert q["basis"] == bridge.QUOTE_EVENT_TIMESTAMP_BASIS
    assert q["provider_at"] is not None
    assert q["bid"] == 2.10 and q["ask"] == 2.12


def test_stale_trade_and_stale_quote_times_still_dropped(monkeypatch):
    """FAIL-CLOSED: lumang trade AT lumang bid/ask time — walang huhulihin."""
    received = datetime(2026, 8, 28, 16, 42, 7, 100000, tzinfo=timezone.utc)
    vals = _values(**{
        "Bid Time": "12:00:00.000000",
        "Ask Time": "12:00:00.000000",
    })
    (_pv, qcap), calls = _parse(monkeypatch, vals, received, generation=32)
    assert qcap is False
    assert not _quote_rows(calls)


def test_trade_fenced_path_is_byte_identical(monkeypatch):
    """Sariwang trade (loob ng 2s) — dating basis, provider_at None pa rin."""
    received = datetime(2026, 8, 28, 16, 0, 0, 500000, tzinfo=timezone.utc)
    vals = _values(**{
        "Most Recent Trade Time": "12:00:00.000000",  # 16:00:00 UTC — 0.5s
        "TickID": "44",
        "Bid Time": "11:00:00.000000",  # lumang quote times — di mahalaga
        "Ask Time": "11:00:00.000000",
    })
    (_pv, qcap), calls = _parse(monkeypatch, vals, received, generation=33)
    assert qcap is True
    q = _quote_rows(calls)[-1]
    assert q["basis"] == bridge.AUTHORITATIVE_TIMESTAMP_BASIS
    assert q["provider_at"] is None


def test_midnight_rollover_joins_to_yesterday(monkeypatch):
    """00:00:00.6 ET ang received; 23:59:59.9 ang Bid Time — kagabi iyon,
    0.7s ang edad, huli pa rin."""
    received = datetime(2026, 8, 28, 4, 0, 0, 600000, tzinfo=timezone.utc)
    vals = _values(**{
        "Most Recent Trade Date": "2026-08-27",
        "Most Recent Trade Time": "19:59:00.000000",
        "TickID": "45",
        "Bid Time": "23:59:59.900000",
        "Ask Time": "23:59:59.900000",
    })
    (_pv, qcap), calls = _parse(monkeypatch, vals, received, generation=34)
    assert qcap is True
    q = _quote_rows(calls)[-1]
    assert q["basis"] == bridge.QUOTE_EVENT_TIMESTAMP_BASIS


def test_helper_picks_the_newer_side_and_rejects_junk():
    received = datetime(2026, 8, 28, 16, 42, 8, 0, tzinfo=timezone.utc)
    got = bridge._quote_event_datetime_utc(
        "12:42:06.000000", "12:42:07.500000", received
    )
    assert got is not None
    assert got == datetime(2026, 8, 28, 16, 42, 7, 500000, tzinfo=timezone.utc)
    assert bridge._quote_event_datetime_utc("", "", received) is None
    assert bridge._quote_event_datetime_utc("hindi-oras", "25:99:99", received) is None
