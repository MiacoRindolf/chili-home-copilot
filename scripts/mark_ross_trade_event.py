from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_ross_symbol_incidents import DEFAULT_ROSS_TRADE_EVENTS_PATH, _sym
from app.services.trading.momentum_neural.counterfactual_replay import (
    DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH,
    _ross_trade_event_certifiable,
    _visual_review_rows_by_evidence_id,
)


def _utc_iso(value: str | None = None) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat()
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def build_ross_trade_event(
    *,
    symbol: str,
    action: str = "",
    price: str | float | None = None,
    ts: str | None = None,
    note: str = "",
    visual_evidence_id: str = "",
) -> dict[str, Any]:
    sym = _sym(symbol)
    if not sym:
        raise ValueError("symbol_required")
    event: dict[str, Any] = {
        "symbol": sym,
        "ts": _utc_iso(ts),
    }
    action_s = str(action or "").strip().lower()
    if action_s:
        event["action"] = action_s
    if price not in (None, ""):
        try:
            event["price"] = float(price)
        except (TypeError, ValueError) as exc:
            raise ValueError("price_must_be_numeric") from exc
    note_s = str(note or "").strip()
    if note_s:
        event["note"] = note_s[:300]
    evidence_s = str(visual_evidence_id or "").strip()
    if evidence_s:
        event["visual_evidence_id"] = evidence_s[:120]
    return event


def append_ross_trade_event(event: dict[str, Any], path: str | Path = DEFAULT_ROSS_TRADE_EVENTS_PATH) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    return out


def validate_certified_visual_evidence(
    event: dict[str, Any],
    *,
    visual_review_manifest: str | Path = DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH,
) -> tuple[bool, str]:
    visual_reviews = _visual_review_rows_by_evidence_id(Path(visual_review_manifest))
    return _ross_trade_event_certifiable(event, visual_reviews=visual_reviews)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append a read-only Ross trade marker for CHILI comparison audits.")
    parser.add_argument("symbol")
    parser.add_argument("--action", default="", help="Optional action label, e.g. buy/sell/scalp/attempt.")
    parser.add_argument("--price", default=None)
    parser.add_argument("--ts", default=None, help="UTC ISO timestamp. Defaults to now.")
    parser.add_argument("--note", default="")
    parser.add_argument(
        "--visual-evidence-id",
        default="",
        help="Optional reviewed video/frame evidence id. Certification still requires the review manifest.",
    )
    parser.add_argument(
        "--visual-review-manifest",
        default=DEFAULT_ROSS_VISUAL_REVIEW_MANIFEST_PATH,
        help="Review manifest used to validate review_certified markers.",
    )
    parser.add_argument("--out", default=DEFAULT_ROSS_TRADE_EVENTS_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the marker without writing it.")
    args = parser.parse_args(argv)

    try:
        event = build_ross_trade_event(
            symbol=args.symbol,
            action=args.action,
            price=args.price,
            ts=args.ts,
            note=args.note,
            visual_evidence_id=args.visual_evidence_id,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if str(event.get("action") or "").strip().lower() == "review_certified":
        ok, reason = validate_certified_visual_evidence(
            event,
            visual_review_manifest=args.visual_review_manifest,
        )
        if not ok:
            print(reason, file=sys.stderr)
            return 2
        event["certification_reason"] = reason
    if args.dry_run:
        print(json.dumps({"dry_run": True, "would_write": False, "event": event}, sort_keys=True))
        return 0
    path = append_ross_trade_event(event, args.out)
    print(str(path))
    print(json.dumps(event, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
