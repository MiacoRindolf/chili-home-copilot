"""Bounded read-only Alpaca crypto WS capture for PAPER integration/research.

The duration/frame/byte bounds are collection resource limits, never a strategy
window. Each received frame is durably recorded before downstream observation.
There is no broker-order client, database write or silent reconnect in this tool.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.crypto_trade_frames import decode_frame


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def capture(args):
    from dotenv import dotenv_values
    import websockets

    settings = dotenv_values(args.env_file)
    if str(settings.get("CHILI_ALPACA_PAPER", "")).lower() not in {"1", "true"}:
        raise ValueError("saved_configuration_not_paper")
    key, secret = settings.get("CHILI_ALPACA_API_KEY"), settings.get("CHILI_ALPACA_API_SECRET")
    if not key or not secret:
        raise ValueError("paper_data_credentials_missing")
    inventory_bytes = args.assets_file.read_bytes()
    inventory = json.loads(inventory_bytes)
    inventory_symbols = sorted({row["symbol"] for row in inventory["assets"]
                      if row.get("class") == "crypto" and row.get("status") == "active"
                      and row.get("tradable") is True and row["symbol"].endswith("/USD")})
    symbols = inventory_symbols
    if args.symbols_file is not None:
        supplied = json.loads(args.symbols_file.read_text(encoding="utf-8"))
        if (not isinstance(supplied, list) or not supplied
                or any(not isinstance(s, str) for s in supplied)
                or len(set(supplied)) != len(supplied)
                or not set(supplied) <= set(inventory_symbols)):
            raise ValueError("capture_sample_outside_asset_inventory")
        symbols = sorted(supplied)
    if not symbols:
        raise ValueError("crypto_usd_inventory_empty")
    allowed = frozenset(symbols)
    connection_id = str(uuid4())
    args.output_dir.mkdir(parents=True, exist_ok=False)
    meta = {
        "schema": "alpaca_crypto_ws_capture_v1", "connection_id": connection_id,
        "source": f"wss://stream.data.alpaca.markets/v1beta3/crypto/{args.location}",
        "feed_location": args.location, "requested_symbols": symbols,
        "inventory_symbols": inventory_symbols,
        "full_inventory_requested": symbols == inventory_symbols,
        "sample_basis": "explicit diagnostic symbols file" if args.symbols_file else "full USD inventory",
        "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "resource_limits": {"max_frames": args.max_frames, "max_seconds": args.max_seconds,
                            "max_frame_bytes": args.max_frame_bytes},
        "broker_orders": False, "reconnects": 0, "history_before_start": "unknown",
    }
    (args.output_dir / "metadata.json").write_text(_json(meta), encoding="utf-8")
    root = hashlib.sha256(_json(meta).encode()).hexdigest()
    frame_count = 0
    trades, quotes, sides = Counter(), Counter(), Counter()
    acknowledged = False
    reason = "resource_limit"
    started = time.monotonic()
    with (args.output_dir / "frames.jsonl").open("x", encoding="utf-8", newline="\n") as out:
        try:
            async with websockets.connect(meta["source"], max_size=args.max_frame_bytes,
                                          open_timeout=15, close_timeout=5) as ws:
                while frame_count < args.max_frames:
                    remaining = args.max_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    received_ns = time.time_ns()
                    raw = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    if key in raw or secret in raw:
                        raise ValueError("provider_response_contains_credential_material")
                    frame_count += 1
                    error = None
                    try:
                        frame = decode_frame(raw, feed_location=args.location,
                            connection_id=connection_id, frame_sequence=frame_count,
                            received_ns=received_ns, subscribed_symbols=allowed)
                    except (ValueError, TypeError, UnicodeError) as exc:
                        frame = None
                        error = type(exc).__name__ + ":" + str(exc)
                    record = {"sequence": frame_count, "received_ns": received_ns,
                              "raw": raw, "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                              "previous_root": root, "decode_error": error}
                    root = hashlib.sha256(_json(record).encode()).hexdigest()
                    out.write(_json({**record, "root": root}) + "\n")
                    out.flush()
                    os.fsync(out.fileno())
                    # Nothing observes a frame before its journal write succeeds.
                    if frame is None:
                        raise ValueError("recorded_frame_rejected")
                    payload = json.loads(raw)
                    for item in payload:
                        if item.get("T") == "success" and item.get("msg") == "connected":
                            await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
                        elif item.get("T") == "success" and item.get("msg") == "authenticated":
                            await ws.send(json.dumps({"action": "subscribe", "trades": symbols, "quotes": symbols}))
                        elif item.get("T") == "subscription":
                            if set(item.get("trades", [])) != allowed or set(item.get("quotes", [])) != allowed:
                                raise ValueError("crypto_subscription_inventory_mismatch")
                            acknowledged = True
                        elif item.get("T") in {"t", "q"} and not acknowledged:
                            raise ValueError("crypto_market_data_before_subscription_ack")
                    for trade in frame.trades:
                        trades[trade.symbol] += 1
                        sides[trade.reported_taker_side or "unknown"] += 1
                    for quote in frame.quotes:
                        quotes[quote.symbol] += 1
        except Exception as exc:
            # No credentials/provider body in console errors. Raw input is in the
            # owned journal when it passed the explicit credential-material check.
            reason = "capture_error:" + type(exc).__name__
    result = {**meta, "finished_at": datetime.now(timezone.utc).isoformat(),
              "reason": reason, "acknowledged": acknowledged, "frames": frame_count,
              "final_root": root, "trade_counts": dict(trades), "quote_counts": dict(quotes),
              "reported_taker_side_counts": dict(sides),
              "source_complete_after_finish": False,
              "elapsed_seconds": time.monotonic() - started}
    (args.output_dir / "result.json").write_text(_json(result), encoding="utf-8")
    print(json.dumps({"output_dir": str(args.output_dir), "reason": reason,
        "acknowledged": acknowledged, "frames": frame_count, "trades": sum(trades.values()),
        "quotes": sum(quotes.values()), "symbols_with_trades": len(trades), "sides": dict(sides)}))
    return 0 if acknowledged and reason == "resource_limit" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--assets-file", type=Path, required=True)
    parser.add_argument("--symbols-file", type=Path,
                        help="Explicit diagnostic subset; never a hidden universe/ranking cutoff")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--location", choices=("us", "us-1", "eu-1"), required=True)
    parser.add_argument("--max-frames", type=int, required=True)
    parser.add_argument("--max-seconds", type=float, required=True)
    parser.add_argument("--max-frame-bytes", type=int, required=True)
    args = parser.parse_args()
    if args.max_frames <= 0 or not 0 < args.max_seconds < float("inf") or args.max_frame_bytes <= 0:
        parser.error("resource limits must be positive and finite")
    raise SystemExit(asyncio.run(capture(args)))


if __name__ == "__main__":
    main()
