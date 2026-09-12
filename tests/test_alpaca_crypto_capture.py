import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts import capture_alpaca_crypto_ticks as capture
from scripts.verify_crypto_tick_capture import inspect_capture


def fixture(tmp_path, monkeypatch, *, before_ack=False):
    import websockets
    creds = tmp_path / "paper.env"
    creds.write_text("CHILI_ALPACA_PAPER=1\nCHILI_ALPACA_API_KEY=fixture-key-not-recorded\n"
                     "CHILI_ALPACA_API_SECRET=fixture-secret-not-recorded\n", encoding="utf-8")
    assets = tmp_path / "assets.json"
    assets.write_text(json.dumps({"assets": [{"symbol": "BTC/USD", "class": "crypto",
        "status": "active", "tradable": True}]}), encoding="utf-8")
    trade = {"T": "t", "S": "BTC/USD", "i": 3447222699101865076,
             "p": 100, "s": 2, "tks": "S", "t": "2026-09-12T04:00:00.000000001Z"}
    messages = [
        [{"T": "success", "msg": "connected"}],
        [{"T": "success", "msg": "authenticated"}],
        ([trade] if before_ack else []) +
        [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]}],
        [trade],
    ]
    class WS:
        def __init__(self): self.sent = []
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def recv(self): return json.dumps(messages.pop(0))
        async def send(self, message): self.sent.append(json.loads(message))
    ws = WS()
    monkeypatch.setattr(websockets, "connect", lambda *_a, **_k: ws)
    args = SimpleNamespace(env_file=creds, assets_file=assets, symbols_file=None,
        output_dir=tmp_path / "capture", location="us", max_frames=4,
        max_seconds=60, max_frame_bytes=4096)
    return args, ws


def test_capture_and_verify_actual_message_path_without_recording_auth_material(tmp_path, monkeypatch):
    args, ws = fixture(tmp_path, monkeypatch)
    assert asyncio.run(capture.capture(args)) == 0
    result = inspect_capture(args.output_dir)
    assert result["frames"] == 4 and result["trade_counts"] == {"BTC/USD": 1}
    assert result["reported_taker_side_counts"] == {"S": 1}
    assert result["subscription_acknowledged"] and result["retained_chain_verified"]
    assert ws.sent[0]["action"] == "auth" and ws.sent[1]["action"] == "subscribe"
    for path in args.output_dir.iterdir():
        raw = path.read_text(encoding="utf-8")
        assert "fixture-key-not-recorded" not in raw and "fixture-secret-not-recorded" not in raw


def test_market_data_cannot_be_acknowledged_retroactively_in_same_frame(tmp_path, monkeypatch):
    args, _ = fixture(tmp_path, monkeypatch, before_ack=True)
    assert asyncio.run(capture.capture(args)) == 1
    result = json.loads((args.output_dir / "result.json").read_text(encoding="utf-8"))
    assert result["trade_counts"] == {} and result["acknowledged"] is False
    with pytest.raises(ValueError, match="before_subscription_ack"):
        inspect_capture(args.output_dir)


@pytest.mark.parametrize("corruption", ["drop", "price", "terminal", "ack"])
def test_replay_rejects_changed_or_missing_retained_evidence(tmp_path, monkeypatch, corruption):
    args, _ = fixture(tmp_path, monkeypatch)
    assert asyncio.run(capture.capture(args)) == 0
    path = args.output_dir / "frames.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    if corruption == "drop":
        lines.pop(1)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif corruption == "price":
        record = json.loads(lines[-1])
        record["raw"] = record["raw"].replace('"p": 100', '"p": 101')
        lines[-1] = json.dumps(record)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        path = args.output_dir / "result.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        if corruption == "terminal": result["frames"] += 1
        else: result["acknowledged"] = False
        path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError):
        inspect_capture(args.output_dir)
