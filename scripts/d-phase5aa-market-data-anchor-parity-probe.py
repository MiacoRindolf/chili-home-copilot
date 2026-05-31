#!/usr/bin/env python
"""Read-only Phase 5AA market-data implausibility-anchor parity probe.

``market_data._resolve_implausibility_anchor(...)`` currently falls back to the
most-recent open ``Trade.entry_price`` when the in-memory known-good quote
cache has no ticker entry. That fallback sits in the quote boundary guard, so
it is live market-data safety behavior. This probe compares the old
compatibility-view source with the management-envelope base-table source before
any runtime conversion.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault(
    "DATABASE_URL",
    os.getenv("TEST_DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili_test"),
)

from app.db import SessionLocal  # noqa: E402
from app.models.trade_relation_symbols import (  # noqa: E402
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
)


LIVE_PROBE_OPT_IN = "PHASE5AA_ALLOW_LIVE_PROBE"


@dataclass(frozen=True)
class AnchorRow:
    ticker: str
    envelope_id: int | None
    entry_price: float | None
    entry_date: str | None


def _live_probe_enabled() -> bool:
    return str(os.getenv(LIVE_PROBE_OPT_IN, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _is_test_database_url(url: str | None) -> bool:
    return "_test" in str(url or "").split("?", 1)[0].lower()


def _assert_probe_database_allowed(database_url: str | None) -> None:
    if _is_test_database_url(database_url) or _live_probe_enabled():
        return
    raise RuntimeError(
        "Phase 5AA market-data anchor probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def _anchor_row_from_relation(db, *, relation_name: str, ticker: str) -> AnchorRow:
    if relation_name not in {
        LEGACY_TRADES_COMPAT_RELATION,
        MANAGEMENT_ENVELOPES_RELATION,
    }:
        raise ValueError(f"unsupported anchor relation: {relation_name!r}")

    tk = (ticker or "").upper()
    row = (
        db.execute(
            text(
                f"""
                SELECT id, ticker, entry_price, entry_date
                  FROM {relation_name}
                 WHERE UPPER(ticker) = :ticker
                   AND status = 'open'
                 ORDER BY entry_date DESC NULLS LAST, id DESC
                 LIMIT 1
                """
            ),
            {"ticker": tk},
        )
        .mappings()
        .first()
    )
    if not row:
        return AnchorRow(ticker=tk, envelope_id=None, entry_price=None, entry_date=None)
    price = _positive_float(row.get("entry_price"))
    return AnchorRow(
        ticker=str(row.get("ticker") or tk).upper(),
        envelope_id=int(row["id"]) if row.get("id") is not None else None,
        entry_price=price,
        entry_date=row.get("entry_date").isoformat() if row.get("entry_date") else None,
    )


def _comparison_tickers(db, extra_tickers: list[str] | None = None) -> list[str]:
    rows = db.execute(
        text(
            f"""
            SELECT DISTINCT UPPER(ticker) AS ticker
              FROM {LEGACY_TRADES_COMPAT_RELATION}
             WHERE status = 'open'
               AND ticker IS NOT NULL
               AND ticker <> ''
            UNION
            SELECT DISTINCT UPPER(ticker) AS ticker
              FROM {MANAGEMENT_ENVELOPES_RELATION}
             WHERE status = 'open'
               AND ticker IS NOT NULL
               AND ticker <> ''
             ORDER BY ticker
            """
        )
    ).mappings().all()
    tickers = {str(row["ticker"]).upper() for row in rows if row.get("ticker")}
    for ticker in extra_tickers or []:
        tk = (ticker or "").strip().upper()
        if tk:
            tickers.add(tk)
    return sorted(tickers)


def _anchors_match(old: AnchorRow, new: AnchorRow) -> bool:
    return (
        old.envelope_id == new.envelope_id
        and old.entry_price == new.entry_price
        and old.entry_date == new.entry_date
    )


def run_probe(db, *, extra_tickers: list[str] | None = None) -> dict[str, Any]:
    tickers = _comparison_tickers(db, extra_tickers=extra_tickers)
    comparisons: list[dict[str, Any]] = []
    mismatches = 0
    for ticker in tickers:
        old = _anchor_row_from_relation(
            db, relation_name=LEGACY_TRADES_COMPAT_RELATION, ticker=ticker
        )
        new = _anchor_row_from_relation(
            db, relation_name=MANAGEMENT_ENVELOPES_RELATION, ticker=ticker
        )
        match = _anchors_match(old, new)
        if not match:
            mismatches += 1
        comparisons.append(
            {
                "ticker": ticker,
                "match": match,
                "old": old.__dict__,
                "new": new.__dict__,
            }
        )

    relation_kinds = {
        MANAGEMENT_ENVELOPES_RELATION: _relation_kind(db, MANAGEMENT_ENVELOPES_RELATION),
        LEGACY_TRADES_COMPAT_RELATION: _relation_kind(db, LEGACY_TRADES_COMPAT_RELATION),
    }
    expected_relations = (
        relation_kinds.get(MANAGEMENT_ENVELOPES_RELATION) == "r"
        and relation_kinds.get(LEGACY_TRADES_COMPAT_RELATION) == "v"
    )
    status = "COMPLETE_POSITIVE" if mismatches == 0 and expected_relations else "ALERT"
    reason = (
        f"{len(tickers)} market-data anchor checks matched"
        if status == "COMPLETE_POSITIVE"
        else "market-data anchor parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "tickers": len(tickers),
        "mismatches": mismatches,
        "comparisons": comparisons,
    }


def _extra_tickers_from_env() -> list[str]:
    raw = os.getenv("PHASE5AA_EXTRA_TICKERS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    _assert_probe_database_allowed(database_url)
    db = SessionLocal()
    try:
        result = run_probe(db, extra_tickers=_extra_tickers_from_env())
    finally:
        db.rollback()
        db.close()

    print(f"VERDICT_STATUS={result['status']}")
    print(f"VERDICT_REASON={result['reason']}")
    print(f"RELATION_KINDS={result['relation_kinds']}")
    print(f"ANCHOR_TICKERS={result['tickers']}")
    print(f"ANCHOR_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "ANCHOR_CHECK "
            f"ticker={row['ticker']} match={row['match']} "
            f"old_id={row['old']['envelope_id']} new_id={row['new']['envelope_id']} "
            f"old_px={row['old']['entry_price']} new_px={row['new']['entry_price']}"
        )
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())

