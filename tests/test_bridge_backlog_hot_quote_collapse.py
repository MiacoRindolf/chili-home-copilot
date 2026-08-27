"""A-2: hot-quote collapse sa ilalim ng napatunayang backlog (2026-08-27).

ANG SINUKAT (close burst): tape ~3,285 trades/s pero committed ~330/s dahil ang
quotes ang kumain ng 74% ng batch budget — 16 min lag ⇒ FROZEN lane sa mismong
power hour na CELU +81%. Sa ilalim ng backlog, ang HOT-symbol quotes sa
QUOTE-ONLY frames ay kina-collapse na tulad ng cold (newest kada symbol); ang
quotes sa frames na MAY trades ay mandatory pa rin (exact-print provenance
pairing).

Runnable: pytest tests/test_bridge_backlog_hot_quote_collapse.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_BRIDGE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "iqfeed_trade_bridge.py"
_spec = importlib.util.spec_from_file_location("iqfeed_trade_bridge_a2test", _BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
if "iqfeed_trade_bridge_a2test" not in sys.modules:
    sys.modules["iqfeed_trade_bridge_a2test"] = bridge
    _spec.loader.exec_module(bridge)
else:  # pragma: no cover
    bridge = sys.modules["iqfeed_trade_bridge_a2test"]


def _drain(**kwargs):
    return bridge._drain_pending_write_batch(**kwargs)


def _seed(trades, quotes):
    with bridge._pending_lock:
        bridge._pending.clear()
        bridge._pending_nbbo.clear()
        bridge._pending.extend(trades)
        bridge._pending_nbbo.extend(quotes)


def _q(sym, seq):
    return {"sym": sym, "connection_generation": 1, "source_frame_sequence": seq}


def test_under_backlog_hot_quote_only_frames_collapse_to_newest():
    """ANG PANGUNAHING KASO: 3 sunud-sunod na HOT quote-only frames — sa
    collapse mode, IISA (ang pinakabago) ang mananatili."""
    _seed([], [_q("HOT", 1), _q("HOT", 2), _q("HOT", 3)])
    trades, quotes, _ = _drain(
        max_events=10, hot_symbols={"HOT"}, collapse_hot_quotes=True
    )
    assert trades == []
    assert [r["source_frame_sequence"] for r in quotes] == [3], (
        "newest lamang ang dapat manatili sa collapse mode"
    )


def test_without_the_flag_hot_quotes_stay_mandatory():
    """Byte-identical na lumang gawi kapag walang backlog signal."""
    _seed([], [_q("HOT", 1), _q("HOT", 2), _q("HOT", 3)])
    trades, quotes, _ = _drain(
        max_events=10, hot_symbols={"HOT"}, collapse_hot_quotes=False
    )
    assert [r["source_frame_sequence"] for r in quotes] == [1, 2, 3]


def test_trade_frame_quotes_remain_mandatory_even_under_collapse():
    """⚠️ PROVENANCE PAIRING: ang quote sa frame na MAY trade ay hindi
    kailanman kina-collapse — bahagi ito ng exact-print timestamp basis."""
    _seed(
        [_q("HOT", 5)],
        [_q("HOT", 4), _q("HOT", 5), _q("HOT", 6)],
    )
    trades, quotes, _ = _drain(
        max_events=10, hot_symbols={"HOT"}, collapse_hot_quotes=True
    )
    assert [r["source_frame_sequence"] for r in trades] == [5]
    seqs = [r["source_frame_sequence"] for r in quotes]
    assert 5 in seqs, "ang trade-frame quote (seq 5) ay mandatory"
    assert seqs.count(6) == 1, "ang pinakabagong quote-only ay retained pa rin"
    assert 4 not in seqs, "ang lumang quote-only ay na-collapse"


def test_cold_symbols_behave_the_same_in_both_modes():
    _seed([], [_q("COLD", 1), _q("COLD", 2)])
    for flag in (False, True):
        _seed([], [_q("COLD", 1), _q("COLD", 2)])
        _, quotes, _ = _drain(
            max_events=10, hot_symbols={"HOT"}, collapse_hot_quotes=flag
        )
        assert [r["source_frame_sequence"] for r in quotes] == [2]


def test_the_production_caller_keys_on_proven_backlog():
    src = _BRIDGE_PATH.read_text(encoding="utf-8")
    i = src.index("collapse_hot_quotes=(")
    region = src[i:i + 200]
    assert "pending_backlog" in region, (
        "ang collapse ay dapat naka-key sa napatunayang backlog"
    )
    assert "BACKLOG_HOT_QUOTE_COLLAPSE" in region, "may kill-switch dapat"


def test_the_incident_is_recorded_at_the_switch():
    src = _BRIDGE_PATH.read_text(encoding="utf-8")
    i = src.index("BACKLOG_HOT_QUOTE_COLLAPSE = ")
    region = src[max(0, i - 1200):i]
    assert "2026-08-27" in region and "74%" in region
