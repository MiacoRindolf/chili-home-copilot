"""Bounded read-only runtime evidence adapters for autonomous diagnosis."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_LOG_SUFFIXES = frozenset({".err", ".jsonl", ".log", ".out", ".txt"})
_SKIP_DIRS = frozenset({".git", ".venv", "build", "data", "dist", "node_modules", "vendor"})
_MAX_LOG_FILES_SCANNED = 2_000
_MAX_LOG_FILE_BYTES = 100_000_000
_MAX_LOG_TAIL_BYTES = 2_000_000
_MAX_LOG_LINE_CHARS = 700
_MAX_DB_GROUPS = 25
_MAX_DB_COLUMNS = 120
_DB_STATEMENT_TIMEOUT_MS = 2_500
_DB_LOCK_TIMEOUT_MS = 250
_AGGREGATES = frozenset({"avg", "max", "min", "sum"})


def _clip(value: object, limit: int) -> str:
    rendered = str(value or "").strip()
    return rendered if len(rendered) <= limit else rendered[: limit - 3].rstrip() + "..."


def _safe_db_error(exc: BaseException) -> str:
    rendered = str(exc)
    rendered = re.sub(
        r"postgresql(?:\+[a-z0-9_]+)?://[^\s'\"]+",
        "postgresql://[redacted]",
        rendered,
        flags=re.IGNORECASE,
    )
    return _clip(rendered, 1_200)


def _safe_identifier(value: object) -> str:
    rendered = str(value or "").strip()
    return rendered if _IDENTIFIER_RE.fullmatch(rendered) else ""


def _safe_rel(value: object) -> str:
    rendered = str(value or "").replace("\\", "/").strip().strip("/")
    if (
        not rendered
        or len(rendered) > 320
        or Path(rendered).is_absolute()
        or ".." in Path(rendered).parts
        or any(ord(char) < 32 for char in rendered)
    ):
        return ""
    return rendered


def _safe_root_path(root: Path, value: object) -> Path | None:
    rel = _safe_rel(value)
    if not rel:
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.exists() else None


def _log_candidates(root: Path, paths: list[str]) -> list[Path]:
    search_roots = [
        candidate
        for candidate in (_safe_root_path(root, value) for value in paths)
        if candidate is not None
    ] or [root]
    candidates: list[Path] = []
    scanned = 0
    seen: set[Path] = set()
    for search_root in search_roots:
        iterator = [search_root] if search_root.is_file() else search_root.rglob("*")
        for candidate in iterator:
            if scanned >= _MAX_LOG_FILES_SCANNED:
                break
            scanned += 1
            if not candidate.is_file() or candidate.suffix.lower() not in _LOG_SUFFIXES:
                continue
            try:
                relative = candidate.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if any(part in _SKIP_DIRS for part in relative.parts):
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)
        if scanned >= _MAX_LOG_FILES_SCANNED:
            break
    return candidates


def execute_log_inventory(root: Path, probe: Mapping[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    root = root.resolve()
    maximum = max(1, min(100, int(probe.get("max_results") or 30)))
    rows: list[dict[str, Any]] = []
    for path in _log_candidates(root, [str(value) for value in probe.get("paths") or []]):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > _MAX_LOG_FILE_BYTES:
            continue
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": stat.st_size,
                "modified_utc": datetime.fromtimestamp(
                    stat.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
            }
        )
    rows.sort(key=lambda item: (str(item["modified_utc"]), str(item["path"])), reverse=True)
    return {
        "status": "completed",
        "exit_code": 0,
        "output": json.dumps(
            {"log_files": rows[:maximum], "returned": min(len(rows), maximum)},
            sort_keys=True,
        ),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def execute_log_search(root: Path, probe: Mapping[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    root = root.resolve()
    query = str(probe.get("query") or "")
    maximum = max(1, min(100, int(probe.get("max_results") or 40)))
    tail_lines = max(1, min(20_000, int(probe.get("tail_lines") or 5_000)))
    case_sensitive = bool(probe.get("case_sensitive"))
    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []
    candidates = _log_candidates(root, [str(value) for value in probe.get("paths") or []])
    def modified_at(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    candidates.sort(key=modified_at, reverse=True)
    for path in candidates[:40]:
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > _MAX_LOG_TAIL_BYTES:
                    handle.seek(-_MAX_LOG_TAIL_BYTES, os.SEEK_END)
                    handle.readline()
                raw = handle.read(_MAX_LOG_TAIL_BYTES)
        except OSError:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()[-tail_lines:]
        for tail_index, line in enumerate(lines, start=1):
            haystack = line if case_sensitive else line.lower()
            if needle not in haystack:
                continue
            matches.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "tail_line": tail_index,
                    "text": _clip(line, _MAX_LOG_LINE_CHARS),
                }
            )
            if len(matches) >= maximum:
                break
        if len(matches) >= maximum:
            break
    return {
        "status": "completed",
        "exit_code": 0,
        "output": json.dumps(
            {
                "fixed_query": query,
                "matches": matches,
                "returned": len(matches),
                "searched_files": min(len(candidates), 40),
            },
            sort_keys=True,
        ),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _is_postgres_url(value: str) -> bool:
    lowered = (value or "").strip().lower()
    return lowered.startswith(
        ("postgresql://", "postgresql+psycopg2://", "postgresql+psycopg://")
    )


def _is_test_database_url(value: str) -> bool:
    if not _is_postgres_url(value):
        return False
    try:
        return str(make_url(value).database or "").endswith("_test")
    except Exception:
        return False


def resolve_readonly_database_url(explicit_test_url: str | None = None) -> tuple[str, bool, str]:
    """Return (URL, is_test, error) without ever falling back to DATABASE_URL."""
    explicit = (explicit_test_url or "").strip()
    if explicit:
        if _is_test_database_url(explicit):
            return explicit, True, ""
        return "", False, "Explicit diagnostic database URLs must target a _test database."
    test_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if _is_test_database_url(test_url):
        return test_url, True, ""
    readonly_url = (os.environ.get("CHILI_AUTONOMY_READONLY_DATABASE_URL") or "").strip()
    if not readonly_url:
        return "", False, (
            "Database probes require TEST_DATABASE_URL ending in _test or a dedicated "
            "CHILI_AUTONOMY_READONLY_DATABASE_URL."
        )
    if not _is_postgres_url(readonly_url):
        return "", False, "CHILI_AUTONOMY_READONLY_DATABASE_URL must be PostgreSQL."
    try:
        readonly_login = str(make_url(readonly_url).username or "")
    except Exception:
        readonly_login = ""
    if not readonly_login:
        return "", False, "The autonomy read-only DSN must name a dedicated database login."
    primary_url = (os.environ.get("DATABASE_URL") or "").strip()
    if primary_url and readonly_url == primary_url:
        return "", False, "The autonomy read-only DSN must be distinct from DATABASE_URL."
    if primary_url:
        try:
            primary_login = str(make_url(primary_url).username or "")
            readonly_login = str(make_url(readonly_url).username or "")
        except Exception:
            primary_login = readonly_login = ""
        if primary_login and readonly_login and primary_login == readonly_login:
            return "", False, (
                "The autonomy read-only DSN must use a database login distinct from DATABASE_URL."
            )
    return readonly_url, False, ""


def _database_engine(url: str):
    return create_engine(
        url,
        poolclass=NullPool,
        connect_args={"application_name": "chili-autonomy-readonly-probe"},
    )


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    return _clip(value, 160)


def _read_only_connection(engine):
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(
            text(f"SET LOCAL statement_timeout = '{_DB_STATEMENT_TIMEOUT_MS}ms'")
        )
        connection.execute(text(f"SET LOCAL lock_timeout = '{_DB_LOCK_TIMEOUT_MS}ms'"))
        read_only = str(connection.execute(text("SHOW transaction_read_only")).scalar() or "").lower()
        if read_only not in {"on", "true", "1"}:
            raise RuntimeError("PostgreSQL did not confirm a read-only transaction.")
        return connection, transaction
    except Exception:
        transaction.rollback()
        connection.close()
        raise


def _load_table(connection, table_name: str) -> Table:
    inspector = inspect(connection)
    if table_name not in set(inspector.get_table_names(schema="public")):
        raise ValueError(f"Unknown public table: {table_name}.")
    return Table(
        table_name,
        MetaData(),
        schema="public",
        autoload_with=connection,
    )


def _assert_select_only_table(connection, table_name: str) -> None:
    relation = f'public."{table_name}"'
    privileges = connection.execute(
        text(
            """
            SELECT
                has_table_privilege(current_user, :relation, 'SELECT') AS can_select,
                has_table_privilege(current_user, :relation, 'INSERT') AS can_insert,
                has_table_privilege(current_user, :relation, 'UPDATE') AS can_update,
                has_table_privilege(current_user, :relation, 'DELETE') AS can_delete,
                has_table_privilege(current_user, :relation, 'TRUNCATE') AS can_truncate,
                has_table_privilege(current_user, :relation, 'REFERENCES') AS can_reference,
                has_table_privilege(current_user, :relation, 'TRIGGER') AS can_trigger
            """
        ),
        {"relation": relation},
    ).mappings().one()
    if not bool(privileges["can_select"]):
        raise PermissionError("The diagnostic database role lacks SELECT on the requested table.")
    write_keys = (
        "can_insert",
        "can_update",
        "can_delete",
        "can_truncate",
        "can_reference",
        "can_trigger",
    )
    if any(bool(privileges[key]) for key in write_keys):
        raise PermissionError(
            "The diagnostic database role has write-capable privileges on the requested table."
        )


def execute_db_schema(
    probe: Mapping[str, Any],
    *,
    explicit_test_url: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    url, is_test, error = resolve_readonly_database_url(explicit_test_url)
    if error:
        return {"status": "blocked", "exit_code": 2, "output": error, "duration_ms": 0}
    table_name = _safe_identifier(probe.get("table"))
    if not table_name:
        return {"status": "blocked", "exit_code": 2, "output": "Unsafe table identifier.", "duration_ms": 0}
    engine = _database_engine(url)
    connection = transaction = None
    try:
        connection, transaction = _read_only_connection(engine)
        inspector = inspect(connection)
        table = _load_table(connection, table_name)
        if not is_test:
            _assert_select_only_table(connection, table_name)
        indexes = inspector.get_indexes(table_name, schema="public")[:40]
        payload = {
            "table": table_name,
            "columns": [
                {
                    "name": column.name,
                    "type": _clip(column.type, 80),
                    "nullable": bool(column.nullable),
                    "primary_key": bool(column.primary_key),
                }
                for column in list(table.columns)[:_MAX_DB_COLUMNS]
            ],
            "indexes": [
                {
                    "name": _clip(item.get("name"), 120),
                    "columns": [str(value) for value in item.get("column_names") or []],
                    "unique": bool(item.get("unique")),
                }
                for item in indexes
            ],
            "transaction_read_only": True,
        }
        return {
            "status": "completed",
            "exit_code": 0,
            "output": json.dumps(payload, sort_keys=True),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "exit_code": 2,
            "output": f"Read-only schema probe failed: {_safe_db_error(exc)}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        if connection is not None:
            connection.close()
        engine.dispose()


def execute_db_profile(
    probe: Mapping[str, Any],
    *,
    explicit_test_url: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    url, is_test, error = resolve_readonly_database_url(explicit_test_url)
    if error:
        return {"status": "blocked", "exit_code": 2, "output": error, "duration_ms": 0}
    table_name = _safe_identifier(probe.get("table"))
    timestamp_name = _safe_identifier(probe.get("timestamp_column"))
    group_name = _safe_identifier(probe.get("group_by"))
    aggregate_name = str(probe.get("aggregate") or "").strip().lower()
    aggregate_column_name = _safe_identifier(probe.get("aggregate_column"))
    lookback_minutes = max(0, min(10_080, int(probe.get("lookback_minutes") or 0)))
    if not table_name:
        return {"status": "blocked", "exit_code": 2, "output": "Unsafe table identifier.", "duration_ms": 0}
    if not is_test and (not timestamp_name or lookback_minutes <= 0):
        return {
            "status": "blocked",
            "exit_code": 2,
            "output": "Production aggregate probes require a timestamp_column and bounded lookback_minutes.",
            "duration_ms": 0,
        }
    engine = _database_engine(url)
    connection = transaction = None
    try:
        connection, transaction = _read_only_connection(engine)
        table = _load_table(connection, table_name)
        if not is_test:
            _assert_select_only_table(connection, table_name)
        columns = {column.name: column for column in table.columns}
        for name in (timestamp_name, group_name, aggregate_column_name):
            if name and name not in columns:
                raise ValueError(f"Unknown column {name!r} on {table_name}.")
        raw_filters = probe.get("filters") if isinstance(probe.get("filters"), Mapping) else {}
        filters: dict[str, Any] = {}
        clauses = []
        for raw_name, raw_value in list(raw_filters.items())[:4]:
            name = _safe_identifier(raw_name)
            if not name or name not in columns:
                raise ValueError(f"Unknown filter column {raw_name!r} on {table_name}.")
            if raw_value is not None and not isinstance(raw_value, (bool, int, float, str)):
                raise ValueError(f"Filter {name!r} must use a scalar equality value.")
            effective_value = _clip(raw_value, 180) if isinstance(raw_value, str) else raw_value
            filters[name] = effective_value
            clauses.append(
                columns[name].is_(None)
                if effective_value is None
                else columns[name] == effective_value
            )
        threshold = None
        if timestamp_name and lookback_minutes > 0:
            threshold = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
            clauses.append(columns[timestamp_name] >= threshold)

        count_statement = select(func.count()).select_from(table)
        if clauses:
            count_statement = count_statement.where(*clauses)
        row_count = int(connection.execute(count_statement).scalar() or 0)
        payload: dict[str, Any] = {
            "table": table_name,
            "count": row_count,
            "lookback_minutes": lookback_minutes or None,
            "timestamp_column": timestamp_name or None,
            "filters": {str(key): _json_scalar(value) for key, value in filters.items()},
            "transaction_read_only": True,
        }
        if timestamp_name:
            latest_statement = select(func.max(columns[timestamp_name])).select_from(table)
            if clauses:
                latest_statement = latest_statement.where(*clauses)
            payload["latest_timestamp"] = _json_scalar(
                connection.execute(latest_statement).scalar()
            )
        if group_name:
            count_label = func.count().label("row_count")
            grouped = select(columns[group_name], count_label).select_from(table)
            if clauses:
                grouped = grouped.where(*clauses)
            grouped = (
                grouped.group_by(columns[group_name])
                .order_by(count_label.desc())
                .limit(max(1, min(_MAX_DB_GROUPS, int(probe.get("max_groups") or 15))))
            )
            payload["groups"] = [
                {"value": _json_scalar(row[0]), "count": int(row[1])}
                for row in connection.execute(grouped).all()
            ]
        if aggregate_name or aggregate_column_name:
            if aggregate_name not in _AGGREGATES or not aggregate_column_name:
                raise ValueError("Aggregate probes require avg|max|min|sum and aggregate_column.")
            aggregate_func = getattr(func, aggregate_name)
            aggregate_statement = select(
                aggregate_func(columns[aggregate_column_name])
            ).select_from(table)
            if clauses:
                aggregate_statement = aggregate_statement.where(*clauses)
            payload["aggregate"] = {
                "operation": aggregate_name,
                "column": aggregate_column_name,
                "value": _json_scalar(connection.execute(aggregate_statement).scalar()),
            }
        return {
            "status": "completed",
            "exit_code": 0,
            "output": json.dumps(payload, sort_keys=True),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "exit_code": 2,
            "output": f"Read-only aggregate probe failed: {_safe_db_error(exc)}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        if connection is not None:
            connection.close()
        engine.dispose()
