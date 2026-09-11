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


def _audit_call(audit: Any, method: str, **kwargs: Any) -> None:
    if audit is None:
        return
    try:
        getattr(audit, method)(**kwargs)
    except Exception as exc:
        # Observation cannot turn a successful read into failure, nor obscure
        # the original execute/fetch exception handled by the owning savepoint.
        try:
            audit.audit_failed(method=method, error=exc)
        except Exception:
            pass


def _fetchall(db: Any, statement: Any, params: Mapping[str, Any] | None, audit: Any):
    _audit_call(audit, "requested")
    try:
        rows = list(db.execute(statement, dict(params or {})).fetchall())
    except BaseException as exc:
        _audit_call(audit, "returned", error=exc)
        raise
    _audit_call(audit, "returned", rows=rows)
    return rows


def optional_fetchall(
    db: Any, statement: Any, params: Mapping[str, Any] | None = None,
    *, audit: Any = None,
) -> list[Any]:
    with _savepoint(db):
        return _fetchall(db, statement, params, audit)


def bounded_fetchall(
    db: Any,
    statement: Any,
    params: Mapping[str, Any] | None = None,
    *,
    timeout_ms: int,
    audit: Any = None,
) -> list[Any]:
    """``optional_fetchall`` with a per-statement ``statement_timeout`` (2026-09-10, [21]/[44]).

    The exit verdict reads the tape on EVERY held tick; a read that hangs must not hold the
    row-locked session transaction open past the tick cadence. ``SET LOCAL`` inside a nested
    savepoint, rows MATERIALISED, then the savepoint is ROLLED BACK: the rollback undoes the
    ``SET LOCAL`` (GUC changes are subtransaction-scoped) so it can never leak into the rest
    of the tick's transaction (a RELEASE would keep it). A timeout raises out of the savepoint
    with the outer transaction intact; the caller maps it to ``unreadable`` (fail-open).

    Small unit-test fakes without ``begin_nested`` execute directly, same contract as
    ``optional_fetchall``.
    """
    begin_nested = getattr(db, "begin_nested", None)
    if not callable(begin_nested):
        return _fetchall(db, statement, params, audit)
    from sqlalchemy import text as _sql

    sp = begin_nested()
    try:
        db.execute(_sql(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
        rows = _fetchall(db, statement, params, audit)
    finally:
        rollback = getattr(sp, "rollback", None)
        if callable(rollback):
            rollback()
        elif hasattr(sp, "__exit__"):
            # a fake that hands back a bare context manager (tests): leave it the way
            # `optional_fetchall` would -- nothing to roll back, nothing leaked.
            sp.__exit__(None, None, None)
    return rows


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
