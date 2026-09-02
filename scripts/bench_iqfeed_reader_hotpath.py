"""DB-free microbench ng READER hot path ng L1 trade bridge (kada frame na CPU).

Ang reader at writer ay nasa IISANG Python process (isang GIL). Kada Q frame
ay: sha256(raw) sa reader + decode + split, tapos sa ``_parse_selected_l1``:
PANGALAWANG sha256 + decode (verification), 2 lock acquisitions, strptime ×2,
regex, at dict build. Sinusukat dito ang µs/frame para malaman kung gaano
kalaking bahagi ng core ang kinakain ng reader sa ilang libong frame/s — CPU na
HINDI makukuha ng writer habang nagko-compile ito ng 60k-bind-parameter na
INSERT.

Runnable:  python scripts/bench_iqfeed_reader_hotpath.py --frames 20000
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import pathlib
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_HERE = pathlib.Path(__file__).resolve().parent


def _load_bridge():
    path = _HERE / "iqfeed_trade_bridge.py"
    spec = importlib.util.spec_from_file_location("iqfeed_trade_bridge_readerbench", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["iqfeed_trade_bridge_readerbench"] = module
    spec.loader.exec_module(module)
    return module


def _q_frame(bridge, symbol: str, tick_id: int, now_utc: datetime, *, trade: bool) -> bytes:
    et = now_utc.astimezone(ZoneInfo("America/New_York"))
    date_txt = et.strftime("%Y-%m-%d")
    time_txt = et.strftime("%H:%M:%S.%f")
    fields = {
        "Symbol": symbol,
        "Most Recent Trade": "10.42",
        "Most Recent Trade Size": "100",
        "Most Recent Trade Time": time_txt,
        "Most Recent Trade Date": date_txt,
        "Most Recent Trade Market Center": "19",
        "Most Recent Trade Conditions": "3D",
        "TickID": str(tick_id if trade else 1),
        "Bid": "10.41",
        "Bid Size": "300",
        "Bid Time": time_txt,
        "Ask": "10.43",
        "Ask Size": "200",
        "Ask Time": time_txt,
        "Total Volume": "1234567",
        "Delay": "0",
        "Message Contents": "Cba" if trade else "ba",
        "Decimal Precision": "4",
    }
    line = "Q," + ",".join(fields[name] for name in bridge.SELECTED_UPDATE_FIELDS) + ","
    return (line + "\r").encode("utf-8")


def _reader_prefix(raw: bytes):
    """Ang bahagi ng reader loop na tumatakbo BAGO ang parse (scripts/iqfeed_trade_bridge.py reader)."""
    decoded_wire = raw.decode(errors="replace")
    line = decoded_wire[:-1] if decoded_wire.endswith("\r") else decoded_wire
    received_at = datetime.now(timezone.utc)
    sha = hashlib.sha256(raw).hexdigest()
    return line, received_at, sha


def run(frames: int) -> dict:
    bridge = _load_bridge()
    gen = bridge._begin_connection_generation()
    ack = "a" * 64
    bridge._selected_fields_ack_sha256_by_generation[gen] = ack
    bridge.IGNITION_ENABLED = False  # detector ay hiwalay na sukat; dito reader+parse lang
    now = datetime.now(timezone.utc)
    trade_raws = [_q_frame(bridge, f"S{i % 480:04d}", 1_000_000 + i, now, trade=True) for i in range(frames)]
    quote_raws = [_q_frame(bridge, f"S{i % 480:04d}", 0, now, trade=False) for i in range(frames)]
    out = {}

    t0 = time.perf_counter()
    for raw in trade_raws:
        _reader_prefix(raw)
    out["reader_prefix_us"] = (time.perf_counter() - t0) / frames * 1e6

    def _parse_all(raws):
        with bridge._pending_lock:
            bridge._pending.clear()
            bridge._pending_nbbo.clear()
        bridge._last_trade.clear()
        t = time.perf_counter()
        for raw in raws:
            line, received_at, sha = _reader_prefix(raw)
            bridge._parse_selected_l1(
                line, connection_generation=gen, selected_fields_ack_sha256=ack,
                received_at=received_at, source_frame_sha256=sha, source_frame_bytes=raw,
            )
        return (time.perf_counter() - t) / len(raws) * 1e6, len(bridge._pending), len(bridge._pending_nbbo)

    out["trade_frame_us"], n_t, n_q = _parse_all(trade_raws)
    out["trade_frames_enqueued"] = (n_t, n_q)
    out["quote_only_frame_us"], n_t, n_q = _parse_all(quote_raws)
    out["quote_only_enqueued"] = (n_t, n_q)

    # Drain + release bookkeeping na tumatakbo sa writer kada batch (DB-free na bahagi).
    # Sariwang frames: ang 2.0s trade fence ay laktawan ang quotes ng lumang batch.
    fresh = datetime.now(timezone.utc)
    _parse_all([_q_frame(bridge, f"S{i % 480:04d}", 2_000_000 + i, fresh, trade=True) for i in range(3600)])
    t = time.perf_counter()
    trades, quotes, backlog = bridge._drain_pending_write_batch(
        max_events=3600, hot_symbols=set(), collapse_hot_quotes=True)
    out["drain_3600_ms"] = (time.perf_counter() - t) * 1e3
    out["drain_result"] = (len(trades), len(quotes), backlog)
    at = datetime.now(timezone.utc)
    t = time.perf_counter()
    payloads = [bridge._notify_payload({**r, "available_at": at}) for r in quotes]
    out["notify_payload_ms_per_batch"] = (time.perf_counter() - t) * 1e3
    out["notify_payload_bytes"] = sum(len(p) for p in payloads)
    for k, v in out.items():
        print(f"{k:<32} {v}")
    print(f"\nAt 3,000 frames/s the reader alone needs ~{3000 * out['trade_frame_us'] / 1e6:.2f} cores "
          f"of the single GIL (trade frames) / ~{3000 * out['quote_only_frame_us'] / 1e6:.2f} (quote-only).")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=20000)
    run(ap.parse_args().frames)
