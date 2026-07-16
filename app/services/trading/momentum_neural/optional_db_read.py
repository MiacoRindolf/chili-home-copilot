"""Transaction-safe reads from optional, externally-owned market-data tables.

PostgreSQL marks the whole transaction failed after an SQL error.  A plain
``try/except`` around a read from a host-bridge table therefore does not provide
the advertised best-effort behavior: a missing table/column is caught, but every
later FSM query and write fails with ``InFailedSqlTransaction``.  These helpers
contain the read in a SAVEPOINT and fully materialize the result before release.

Small unit-test fakes that do not expose ``begin_nested`` retain their historical
direct-execute behavior.  Real SQLAlchemy Sessions always take the savepoint.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Mapping


def _savepoint(db: Any):
    begin_nested = getattr(db, "begin_nested", None)
    return begin_nested() if callable(begin_nested) else nullcontext()


def optional_fetchall(
    db: Any, statement: Any, params: Mapping[str, Any] | None = None
) -> list[Any]:
    with _savepoint(db):
        return list(db.execute(statement, dict(params or {})).fetchall())


def optional_fetchone(
    db: Any, statement: Any, params: Mapping[str, Any] | None = None
) -> Any:
    with _savepoint(db):
        return db.execute(statement, dict(params or {})).fetchone()


def optional_scalar(
    db: Any, statement: Any, params: Mapping[str, Any] | None = None
) -> Any:
    with _savepoint(db):
        return db.execute(statement, dict(params or {})).scalar()
