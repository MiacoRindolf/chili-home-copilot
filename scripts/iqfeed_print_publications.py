"""Durable membership of ordinary bridge release transactions.

This is a publication journal, not a provider-completeness certificate. A wake
notification may be lost/coalesced; the committed journal is the inventory.
The head row serializes publishers through COMMIT, so publication order never
uses allocation order of trade IDs or a timestamp as a substitute for visibility.
No strategy lookback or clock window is selected here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

import sqlalchemy as sa


CHANNEL = "iqfeed_print_publications"
MIGRATION_ID = "381_iqfeed_print_publications"
REQUIRED_COLUMNS = {
    "iqfeed_print_publication_head": frozenset({"singleton", "epoch", "revision"}),
    "iqfeed_print_publications": frozenset(
        {"epoch", "revision", "available_at", "trade_ids", "symbols"}
    ),
}


def create_schema(connection: Any) -> None:
    """Migration body; never called by the bridge at runtime."""
    connection.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS iqfeed_print_publication_head (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            epoch TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK (revision >= 0)
        )
    """))
    connection.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS iqfeed_print_publications (
            epoch TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK (revision > 0),
            available_at TIMESTAMPTZ NOT NULL,
            trade_ids BIGINT[] NOT NULL,
            symbols TEXT[] NOT NULL,
            PRIMARY KEY (epoch, revision),
            CHECK (cardinality(trade_ids) > 0),
            CHECK (cardinality(trade_ids) = cardinality(symbols))
        )
    """))


@dataclass(frozen=True)
class Cursor:
    epoch: str
    revision: int


@dataclass(frozen=True)
class Publication:
    cursor: Cursor
    available_at: datetime
    trade_ids: tuple[int, ...]
    rows: tuple[Mapping[str, Any], ...]
    source_trade_ids: tuple[int, ...] = ()
    source_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadResult:
    observed_frontier: Cursor
    consumed: Cursor
    publications: tuple[Publication, ...]

    @property
    def caught_up(self) -> bool:
        return self.consumed == self.observed_frontier


def append_publication(
    connection: Any, *, released: list[Mapping[str, Any]], available_at: datetime
) -> Cursor:
    """Call with UPDATE RETURNING membership, in the same release transaction.

    The order inside an atomic publication is only canonical storage order. A
    structural consumer must validate source epoch/frame/event order separately;
    it must not treat database ID order as provider order.
    """
    if not released:
        raise ValueError("empty_print_publication")
    ordered = sorted(released, key=lambda row: row["id"])
    ids = [row["id"] for row in ordered]
    symbols = [row["symbol"] for row in ordered]
    if (any(type(value) is not int or value <= 0 for value in ids)
            or len(set(ids)) != len(ids)
            or any(not isinstance(value, str) or not value for value in symbols)):
        raise ValueError("invalid_print_publication_membership")
    connection.execute(sa.text("""
        INSERT INTO iqfeed_print_publication_head (singleton, epoch, revision)
        VALUES (TRUE, :epoch, 0) ON CONFLICT (singleton) DO NOTHING
    """), {"epoch": str(uuid4())})
    head = connection.execute(sa.text("""
        UPDATE iqfeed_print_publication_head SET revision = revision + 1
        WHERE singleton RETURNING epoch, revision
    """)).mappings().one()
    cursor = Cursor(head["epoch"], head["revision"])
    connection.execute(sa.text("""
        INSERT INTO iqfeed_print_publications
            (epoch, revision, available_at, trade_ids, symbols)
        VALUES (:epoch, :revision, :available_at, :trade_ids, :symbols)
    """), {"epoch": cursor.epoch, "revision": cursor.revision,
           "available_at": available_at, "trade_ids": ids, "symbols": symbols})
    # Wake-only, tiny and transaction-coupled. Consumers always read the journal.
    connection.execute(sa.text("SELECT pg_notify(:channel, :payload)"),
                       {"channel": CHANNEL,
                        "payload": f"{cursor.epoch}:{cursor.revision}"})
    return cursor


def _require_snapshot(connection: Any) -> None:
    if connection.get_isolation_level() not in ("REPEATABLE READ", "SERIALIZABLE"):
        raise ValueError("print_publication_read_requires_snapshot")


def capture_frontier(connection: Any) -> Cursor:
    """Explicit cold-start anchor; it makes no claim about preceding history."""
    _require_snapshot(connection)
    row = connection.execute(sa.text("""
        SELECT epoch, revision FROM iqfeed_print_publication_head WHERE singleton
    """)).mappings().one_or_none()
    if row is None:
        raise ValueError("print_publication_source_not_started")
    return Cursor(row["epoch"], row["revision"])


def read_symbol_publications(
    connection: Any, *, symbol: str, after: Cursor, max_publications: int,
    max_trade_rows: int,
) -> ReadResult:
    result = read_publications(connection, symbols=frozenset({symbol}), after=after,
                               max_publications=max_publications, max_trade_rows=max_trade_rows)
    return ReadResult(result.observed_frontier, result.consumed,
                      tuple(p for p in result.publications if p.rows))


def read_publications(
    connection: Any, *, symbols: frozenset[str], after: Cursor, max_publications: int,
    max_trade_rows: int,
) -> ReadResult:
    """Read a complete page toward one observed frontier, without silent gaps.

    Both limits are caller resource capacities, not strategy windows. Even
    publications for other symbols advance the global cursor. An entire source
    publication is indivisible: callers cannot pretend to act between its prints.
    Missing retained trade rows or journal segments are explicit recovery errors.
    Historical ticks and their source epochs still need independent validation.
    """
    if (type(symbols) is not frozenset or not symbols
            or any(type(s) is not str or not s for s in symbols)):
        raise ValueError("invalid_symbol")
    if type(max_publications) is not int or max_publications <= 0:
        raise ValueError("invalid_publication_read_capacity")
    if type(max_trade_rows) is not int or max_trade_rows <= 0:
        raise ValueError("invalid_trade_read_capacity")
    if (type(after) is not Cursor or type(after.epoch) is not str or not after.epoch
            or type(after.revision) is not int or after.revision < 0):
        raise ValueError("invalid_publication_cursor")
    frontier = capture_frontier(connection)
    if frontier.epoch != after.epoch:
        raise ValueError("print_publication_epoch_changed")
    if after.revision > frontier.revision:
        raise ValueError("print_publication_cursor_ahead")
    end = min(frontier.revision, after.revision + max_publications)
    records = connection.execute(sa.text("""
        SELECT revision, available_at, trade_ids, symbols
        FROM iqfeed_print_publications
        WHERE epoch=:epoch AND revision>:start AND revision<=:end
        ORDER BY revision
    """), {"epoch": after.epoch, "start": after.revision, "end": end}).mappings().all()
    if [r["revision"] for r in records] != list(range(after.revision + 1, end + 1)):
        raise ValueError("print_publication_journal_gap")
    publications = []
    consumed_revision = after.revision
    row_count = 0
    for record in records:
        all_ids, member_symbols = record["trade_ids"], record["symbols"]
        if (len(all_ids) != len(member_symbols) or not all_ids
                or any(type(i) is not int or i <= 0 for i in all_ids)
                or any(type(s) is not str or not s for s in member_symbols)
                or len(set(all_ids)) != len(all_ids)):
            raise ValueError("invalid_stored_publication_membership")
        expected_symbols = dict(zip(all_ids, member_symbols))
        ids = tuple(row_id for row_id, member_symbol in zip(all_ids, member_symbols)
                    if member_symbol in symbols)
        if len(ids) > max_trade_rows:
            raise ValueError("atomic_publication_exceeds_trade_read_capacity")
        if row_count + len(ids) > max_trade_rows:
            break
        # Primary-key membership lookup, not an ID-watermark scan of the tape.
        rows = connection.execute(sa.text("""
            SELECT * FROM iqfeed_trade_ticks
            WHERE symbol=ANY(:symbols) AND id=ANY(:ids)
        """), {"symbols": sorted({expected_symbols[row_id] for row_id in ids}),
                "ids": list(ids)}).mappings().all() if ids else []
        by_id = {row["id"]: row for row in rows}
        if (set(by_id) != set(ids)
                or any(row["available_at"] != record["available_at"]
                       or row["symbol"] != expected_symbols[row["id"]] for row in rows)):
            raise ValueError("print_publication_trade_membership_gap")
        publications.append(Publication(
            Cursor(after.epoch, record["revision"]), record["available_at"], ids,
            tuple(MappingProxyType(dict(by_id[row_id])) for row_id in ids),
            tuple(all_ids), tuple(member_symbols),
        ))
        row_count += len(ids)
        consumed_revision = record["revision"]
    return ReadResult(frontier, Cursor(after.epoch, consumed_revision), tuple(publications))
