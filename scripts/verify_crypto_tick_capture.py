"""Verify the exact retained WS chain, then report source-quality facts.

This checks retained input integrity/decodability, not authentication of provider
completeness, trading profitability, or an unseen interval before/after capture.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.crypto_trade_frames import decode_frame


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def inspect_capture(directory: Path):
    meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if meta["schema"] != "alpaca_crypto_ws_capture_v1":
        raise ValueError("capture_schema_mismatch")
    if meta["source"] != "wss://stream.data.alpaca.markets/v1beta3/crypto/" + meta["feed_location"]:
        raise ValueError("capture_feed_binding_mismatch")
    if any(result.get(k) != v for k, v in meta.items()):
        raise ValueError("capture_terminal_metadata_mismatch")
    root = hashlib.sha256(canonical(meta).encode()).hexdigest()
    symbols = frozenset(meta["requested_symbols"])
    trades, quotes, sides = Counter(), Counter(), Counter()
    seen = {}
    duplicates = contradictions = late_events = frames = 0
    last_event = {}
    acknowledged = False
    for line in (directory / "frames.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        frames += 1
        if record["sequence"] != frames or record["previous_root"] != root:
            raise ValueError("capture_frame_chain_gap")
        claimed = record.pop("root")
        root = hashlib.sha256(canonical(record).encode()).hexdigest()
        if claimed != root or record["raw_sha256"] != hashlib.sha256(record["raw"].encode()).hexdigest():
            raise ValueError("capture_frame_digest_mismatch")
        if record["decode_error"] is not None:
            raise ValueError("capture_contains_rejected_frame")
        frame = decode_frame(record["raw"], feed_location=meta["feed_location"],
            connection_id=meta["connection_id"], frame_sequence=frames,
            received_ns=record["received_ns"], subscribed_symbols=symbols)
        for item in json.loads(record["raw"]):
            if item.get("T") == "subscription":
                if set(item.get("trades", [])) != symbols or set(item.get("quotes", [])) != symbols:
                    raise ValueError("capture_subscription_membership_mismatch")
                acknowledged = True
            elif item.get("T") in {"t", "q"} and not acknowledged:
                raise ValueError("capture_market_data_before_subscription_ack")
        for trade in frame.trades:
            trades[trade.symbol] += 1
            sides[trade.reported_taker_side or "unknown"] += 1
            key = trade.symbol, trade.trade_id
            identity = (trade.price, trade.size, trade.event_ns, trade.reported_taker_side)
            if key in seen:
                duplicates += 1
                contradictions += seen[key] != identity
            seen[key] = identity
            late_events += trade.event_ns < last_event.get(trade.symbol, trade.event_ns)
            last_event[trade.symbol] = max(trade.event_ns, last_event.get(trade.symbol, trade.event_ns))
        for quote in frame.quotes:
            quotes[quote.symbol] += 1
    if frames != result["frames"] or root != result["final_root"]:
        raise ValueError("capture_terminal_frontier_mismatch")
    if dict(trades) != result["trade_counts"] or dict(quotes) != result["quote_counts"]:
        raise ValueError("capture_terminal_count_mismatch")
    if dict(sides) != result["reported_taker_side_counts"]:
        raise ValueError("capture_terminal_side_count_mismatch")
    if acknowledged is not result["acknowledged"]:
        raise ValueError("capture_terminal_ack_mismatch")
    return {"frames": frames, "trade_counts": dict(trades), "quote_counts": dict(quotes),
        "reported_taker_side_counts": dict(sides), "duplicate_trade_occurrences": duplicates,
        "contradictory_trade_identities": contradictions, "late_trade_event_occurrences": late_events,
        "retained_chain_verified": True, "subscription_acknowledged": result["acknowledged"],
        "capture_reason": result["reason"], "full_inventory_requested": meta["full_inventory_requested"],
        "provider_completeness_certified": False, "final_root": root}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_capture(args.directory)))


if __name__ == "__main__":
    main()
