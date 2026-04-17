"""Phase C: thin idempotent writer for ``trading_universe_snapshots``.

One row per (as_of_date, ticker). Callers supply asset_class / status /
primary_exchange / source; this module upserts on the unique constraint.

No automatic backfill this phase. Later phases (D for triple-barrier labels,
F for execution realism) will call ``record_snapshot`` from their own
ingest paths.
"""

from __future__ import annotations

import logging
from datetime import date as _date, datetime
from typing import Iterable, Mapping

from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.orm import Session

from ...models.trading import UniverseSnapshot

logger = logging.getLogger(__name__)

VALID_STATUSES: frozenset[str] = frozenset({"active", "halted", "delisted", "unknown"})
VALID_ASSET_CLASSES: frozenset[str] = frozenset({"equity", "etf", "crypto", "option", "future", "other"})


def _coerce_date(d) -> _date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, _date):
        return d
    if isinstance(d, str):
        try:
            return datetime.fromisoformat(d).date()
        except ValueError:
            pass
    raise ValueError(f"unsupported as_of_date type: {type(d).__name__}")


def record_snapshot(
    db: Session,
    *,
    as_of_date,
    ticker: str,
    asset_class: str,
    status: str,
    primary_exchange: str | None = None,
    source: str | None = None,
    provenance: dict | None = None,
    commit: bool = False,
) -> int | None:
    """Idempotently upsert one universe-snapshot row.

    Returns the row id (post-insert) or None on skip. Raises ``ValueError``
    on invalid enum inputs so calling code can decide to swallow.
    """
    if not ticker or not isinstance(ticker, str):
        raise ValueError("ticker required")
    ac = (asset_class or "").strip().lower() or "equity"
    if ac not in VALID_ASSET_CLASSES:
        ac = "other"
    st = (status or "").strip().lower() or "unknown"
    if st not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    aod = _coerce_date(as_of_date)
    tkr = ticker.strip().upper()

    stmt = (
        _pg_insert(UniverseSnapshot)
        .values(
            as_of_date=aod,
            ticker=tkr,
            asset_class=ac,
            status=st,
            primary_exchange=(primary_exchange or None),
            source=(source or None),
            provenance_json=(provenance or None),
        )
        .on_conflict_do_update(
            index_elements=["as_of_date", "ticker"],
            set_=dict(
                asset_class=ac,
                status=st,
                primary_exchange=(primary_exchange or None),
                source=(source or None),
                provenance_json=(provenance or None),
            ),
        )
        .returning(UniverseSnapshot.id)
    )
    result = db.execute(stmt)
    row_id = result.scalar_one_or_none()
    if commit:
        db.commit()
    else:
        db.flush()
    return int(row_id) if row_id is not None else None


def record_bulk(
    db: Session,
    rows: Iterable[Mapping],
    *,
    commit: bool = False,
) -> int:
    """Convenience bulk upsert. Rows must each include at least
    ``as_of_date``, ``ticker``, ``asset_class``, ``status``.

    Returns the number of rows attempted (not necessarily distinct inserts).
    """
    n = 0
    for r in rows:
        try:
            record_snapshot(
                db,
                as_of_date=r["as_of_date"],
                ticker=r["ticker"],
                asset_class=r["asset_class"],
                status=r["status"],
                primary_exchange=r.get("primary_exchange"),
                source=r.get("source"),
                provenance=r.get("provenance"),
                commit=False,
            )
            n += 1
        except Exception:
            logger.debug("[universe] record_bulk: skipped malformed row", exc_info=True)
    if commit:
        db.commit()
    return n


def lookup_status(
    db: Session,
    *,
    ticker: str,
    as_of_date,
) -> str | None:
    """Return the status recorded for (ticker, as_of_date) or None if missing.

    Falls back to the most-recent snapshot with as_of_date <= the requested date
    so callers can ask "what was the last known status of this ticker?".
    """
    tkr = (ticker or "").strip().upper()
    aod = _coerce_date(as_of_date)
    row = (
        db.query(UniverseSnapshot)
        .filter(UniverseSnapshot.ticker == tkr, UniverseSnapshot.as_of_date <= aod)
        .order_by(UniverseSnapshot.as_of_date.desc())
        .limit(1)
        .first()
    )
    return row.status if row is not None else None
