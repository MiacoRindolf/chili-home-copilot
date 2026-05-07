"""f-trump-usd-poisoned-quote-source-audit Phase 1.1 diagnostic.

Hits each upstream quote source for TRUMP-USD in isolation so we can
identify which one is returning the cached ``$0.0003`` poison value.

Sources walked (in fetch_quote order):
  1. price_bus.get_live_quote    (unified WS cache)
  2. _massive.get_ws_quote        (Massive WS cache)
  3. _massive.get_last_quote      (Massive REST)

Plus Coinbase public ticker for ground truth.

Outputs to scripts/dispatch-trump-quote-trace-output.txt.

Run:
  conda run -n chili-env python scripts/dispatch-trump-quote-trace.py
"""
from __future__ import annotations

import json
import sys
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Repo root must be on sys.path so `import app.services...` resolves when
# the script is run via `conda run python scripts/...` (which doesn't put
# CWD on sys.path the way `python -m` would).
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

TICKER = "TRUMP-USD"
OUTPUT = Path(__file__).resolve().parent / "dispatch-trump-quote-trace-output.txt"


def _safe_repr(obj):
    """Best-effort serialization to JSON-friendly shape."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _safe_repr(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_repr(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _safe_repr(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return repr(obj)


def _section(title, body):
    return f"\n=== {title} ===\n{body}\n"


def _ground_truth_coinbase():
    """Hit Coinbase public ticker REST, no auth required."""
    url = f"https://api.exchange.coinbase.com/products/{TICKER}/ticker"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "chili-diagnostic"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return {"ok": True, "url": url, "data": data}
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e), "trace": traceback.format_exc()}


def _try(name, fn):
    """Run fn() with full exception capture; return a structured record."""
    try:
        result = fn()
        return {"ok": True, "name": name, "result": _safe_repr(result)}
    except Exception as e:
        return {"ok": False, "name": name, "error": str(e), "trace": traceback.format_exc()}


def main():
    started_at = datetime.now(timezone.utc).isoformat()
    sections = []

    sections.append(_section("Run metadata", json.dumps({
        "started_at_utc": started_at,
        "ticker": TICKER,
        "python": sys.version.split()[0],
        "purpose": "Phase 1.1 - identify poisoned upstream for TRUMP-USD",
    }, indent=2)))

    # 1. price_bus.get_live_quote
    def _price_bus():
        from app.services.trading.price_bus import get_live_quote
        return get_live_quote(TICKER)
    sections.append(_section("1. price_bus.get_live_quote", json.dumps(_try("price_bus", _price_bus), indent=2)))

    # 2. _massive.get_ws_quote
    def _ws():
        from app.services.trading import market_data as md
        return md._massive.get_ws_quote(TICKER)
    sections.append(_section("2. massive_client.get_ws_quote", json.dumps(_try("massive_ws", _ws), indent=2)))

    # 3. _massive.get_last_quote
    def _rest():
        from app.services.trading import market_data as md
        return md._massive.get_last_quote(TICKER)
    sections.append(_section("3. massive_client.get_last_quote", json.dumps(_try("massive_rest", _rest), indent=2)))

    # 4. Composed fetch_quote (the one the exit monitor actually calls)
    def _fetch():
        from app.services.trading.market_data import fetch_quote
        return fetch_quote(TICKER)
    sections.append(_section("4. market_data.fetch_quote (composed)", json.dumps(_try("fetch_quote", _fetch), indent=2)))

    # 5. Coinbase ground truth
    sections.append(_section("5. Coinbase public ticker (ground truth)", json.dumps(_ground_truth_coinbase(), indent=2)))

    # Diff summary: which paths returned the poisoned $0.0003 value
    def _extract_price(rec):
        if not rec.get("ok"):
            return None
        result = rec.get("result")
        if isinstance(result, dict):
            for k in ("price", "last_price"):
                v = result.get(k)
                if v is not None:
                    return v
        return None

    def _build_summary():
        records = [
            ("price_bus", _try("price_bus", _price_bus)),
            ("massive_ws", _try("massive_ws", _ws)),
            ("massive_rest", _try("massive_rest", _rest)),
            ("fetch_quote", _try("fetch_quote", _fetch)),
        ]
        prices = {name: _extract_price(rec) for name, rec in records}
        gt = _ground_truth_coinbase()
        gt_price = None
        if gt.get("ok") and isinstance(gt.get("data"), dict):
            try:
                gt_price = float(gt["data"].get("price"))
            except (TypeError, ValueError):
                gt_price = None
        prices["coinbase_ground_truth"] = gt_price
        return prices

    sections.append(_section("Summary (price per source)", json.dumps(_build_summary(), indent=2)))

    completed_at = datetime.now(timezone.utc).isoformat()
    sections.append(_section("Run footer", json.dumps({
        "completed_at_utc": completed_at,
    }, indent=2)))

    OUTPUT.write_text("".join(sections), encoding="utf-8")
    print(f"WROTE {OUTPUT}")
    # Also print the Summary section to stdout for quick eyeball.
    print(sections[-2])


if __name__ == "__main__":
    main()
