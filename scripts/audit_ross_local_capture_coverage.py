"""Read-only Ross-label versus legacy local-capture availability audit.

This tool answers only whether the retained PostgreSQL rows are even capable of
supporting a later sealed ReplayV3 window.  It never upgrades legacy rows into a
certifying capture and never turns recap timestamps into event-time inputs.

Example::

    python scripts/audit_ross_local_capture_coverage.py \
      --database-url postgresql://.../chili \
      --manifest tests/fixtures/ross_replay/small_account_challenge_manifest.json

The database URL is mandatory and is never printed.  Every transaction is set
READ ONLY before the audit queries execute.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
import math
from pathlib import Path
from typing import Any, Collection, Iterable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, bindparam, create_engine, text
from sqlalchemy.exc import DBAPIError


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
SCHEMA_VERSION = "chili.ross-local-capture-audit.v1"


def _reject_json_constant(raw: str) -> None:
    raise ValueError(f"non-finite JSON number {raw!r} is forbidden")


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r} is forbidden")
        result[key] = value
    return result


@dataclass(frozen=True)
class RossLabel:
    label_id: str
    symbol: str
    trade_date: date
    timing_basis: str
    coverage_audit_time_role: str | None
    coverage_audit_phase_time_et: time | None
    benchmark_entry_price: float | None

    @property
    def coverage_audit_phase_at_utc(self) -> datetime | None:
        if self.coverage_audit_phase_time_et is None:
            return None
        return datetime.combine(
            self.trade_date,
            self.coverage_audit_phase_time_et,
            tzinfo=ET,
        ).astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--label-id", action="append", default=[])
    parser.add_argument("--statement-timeout-ms", type=int, default=30_000)
    return parser


def _clock(raw: Any) -> time | None:
    value = str(raw or "").strip()
    if not value:
        return None
    return time.fromisoformat(value)


def _finite_positive(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _label_from_row(row: Mapping[str, Any], *, trade_date: date) -> RossLabel:
    ross_outcome = row.get("ross_outcome")
    ross_outcome = ross_outcome if isinstance(ross_outcome, Mapping) else {}
    # Only this explicit, role-bound seam can become a coverage comparison
    # boundary.  Generic recap/enrichment clocks (approx_event_time_et,
    # headline_context_time_et, scanner observations, cross-account campaign
    # clocks) remain after-fact context and are intentionally ignored.
    phase_time_raw = row.get("coverage_audit_phase_time_et")
    phase_time_role = str(row.get("coverage_audit_time_role") or "").strip()
    if phase_time_role not in {"", "candidate_phase_boundary"}:
        raise ValueError(
            "coverage_audit_time_role must be absent or "
            "'candidate_phase_boundary'"
        )
    if phase_time_raw is not None and phase_time_role != "candidate_phase_boundary":
        raise ValueError(
            "coverage_audit_phase_time_et requires "
            "coverage_audit_time_role='candidate_phase_boundary'"
        )
    if phase_time_role == "candidate_phase_boundary" and phase_time_raw is None:
        raise ValueError(
            "candidate_phase_boundary requires coverage_audit_phase_time_et"
        )
    phase_time = (
        _clock(phase_time_raw)
        if phase_time_role == "candidate_phase_boundary"
        else None
    )
    if phase_time_role == "candidate_phase_boundary" and phase_time is None:
        raise ValueError(
            "candidate_phase_boundary requires non-empty coverage_audit_phase_time_et"
        )
    if phase_time is not None and phase_time.tzinfo is not None:
        raise ValueError(
            "coverage_audit_phase_time_et is an America/New_York wall clock "
            "and must not carry a timezone offset"
        )
    return RossLabel(
        label_id=str(row.get("label_id") or "").strip(),
        symbol=str(row.get("symbol") or "").strip().upper(),
        trade_date=trade_date,
        timing_basis=str(
            row.get("time_precision")
            or row.get("timing_basis")
            or "missing"
        ),
        coverage_audit_time_role=phase_time_role or None,
        coverage_audit_phase_time_et=phase_time,
        benchmark_entry_price=_finite_positive(
            row.get("ross_entry_price")
            or ross_outcome.get("entry_price")
        ),
    )


def load_labels(path: Path) -> tuple[RossLabel, ...]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_object_pairs_no_duplicates,
        parse_constant=_reject_json_constant,
    )
    labels: list[RossLabel] = []
    if isinstance(payload.get("entries"), list):
        for entry in payload["entries"]:
            if not isinstance(entry, Mapping):
                continue
            trade_date = date.fromisoformat(str(entry["date"]))
            for row in entry.get("phase_labels") or []:
                if isinstance(row, Mapping):
                    labels.append(_label_from_row(row, trade_date=trade_date))
    else:
        trade_date = date.fromisoformat(str(payload["trade_date"]))
        for row in payload.get("phase_labels") or []:
            if isinstance(row, Mapping):
                labels.append(_label_from_row(row, trade_date=trade_date))
    invalid = [row.label_id for row in labels if not row.label_id or not row.symbol]
    if invalid:
        raise ValueError(f"manifest contains malformed labels: {invalid}")
    return tuple(labels)


_STREAM_CLOCK_TIMEZONE = {
    "iqfeed_trade_ticks": False,
    "momentum_nbbo_spread_tape": True,
}
_STREAM_CORE_COLUMNS = frozenset({"symbol", "observed_at"})
_STREAM_PROVENANCE_COLUMNS = {
    "provider_event_at": "provider_clock_count",
    "received_at": "receive_clock_count",
    "available_at": "available_clock_count",
    "bridge_run_id": "run_identity_count",
    "connection_generation": "generation_count",
}
_STREAM_EXPECTED_COLUMNS = frozenset(
    {*_STREAM_CORE_COLUMNS, *_STREAM_PROVENANCE_COLUMNS}
)
_STREAM_SCHEMA_QUERY = text(
    """
    SELECT table_name, column_name
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name IN ('iqfeed_trade_ticks', 'momentum_nbbo_spread_tape')
     ORDER BY table_name, ordinal_position
    """
)


def _stream_schema_columns(conn: Any) -> dict[str, frozenset[str]]:
    """Inventory the fixed audited stream tables once inside the read-only tx."""

    columns: dict[str, set[str]] = {
        table: set() for table in _STREAM_CLOCK_TIMEZONE
    }
    for row in conn.execute(_STREAM_SCHEMA_QUERY).mappings().all():
        table = str(row["table_name"])
        if table in columns:
            columns[table].add(str(row["column_name"]))
    return {table: frozenset(values) for table, values in columns.items()}


def _empty_stream_stats(
    *,
    table_present: bool,
    columns_present: Collection[str],
    audit_query_status: str,
) -> dict[str, Any]:
    present = frozenset(str(column) for column in columns_present)
    return {
        "row_count": 0,
        "first_observed_at": None,
        "last_observed_at": None,
        "provider_clock_count": 0,
        "receive_clock_count": 0,
        "available_clock_count": 0,
        "run_identity_count": 0,
        "generation_count": 0,
        "audit_query_status": audit_query_status,
        "table_present": table_present,
        "schema_columns_present": sorted(present & _STREAM_EXPECTED_COLUMNS),
        "schema_columns_missing": sorted(_STREAM_EXPECTED_COLUMNS - present),
    }


def _stream_stats(
    conn: Any,
    table: str,
    label: RossLabel,
    *,
    available_columns: Collection[str] | None = None,
) -> dict[str, Any]:
    # The host IQFeed table retains naive-UTC ``TIMESTAMP`` while the shared
    # NBBO tape was created as ``TIMESTAMPTZ``. Bind each with its real schema
    # type; silently treating both alike makes the result session-timezone
    # dependent or relies on an implicit PostgreSQL cast.
    clock_timezone = _STREAM_CLOCK_TIMEZONE.get(table)
    if clock_timezone is None:
        raise ValueError(f"unsupported stream audit table: {table}")
    columns = (
        _STREAM_EXPECTED_COLUMNS
        if available_columns is None
        else frozenset(str(column) for column in available_columns)
    )
    table_present = available_columns is None or bool(columns)
    if not table_present or not _STREAM_CORE_COLUMNS.issubset(columns):
        return _empty_stream_stats(
            table_present=table_present,
            columns_present=columns,
            audit_query_status="not_run_schema_unavailable",
        )
    aware_since, aware_until = _utc_day_bounds(label.trade_date)
    since, until = (
        (aware_since, aware_until)
        if clock_timezone
        else (
            aware_since.replace(tzinfo=None),
            aware_until.replace(tzinfo=None),
        )
    )
    metric_expressions = []
    for column, alias in _STREAM_PROVENANCE_COLUMNS.items():
        # Column names and aliases come only from the fixed constants above.
        expression = (
            f"count(*) FILTER (WHERE {column} IS NOT NULL)"
            if column in columns
            else "0::bigint"
        )
        metric_expressions.append(f"{expression} AS {alias}")
    # Table names are fixed internal constants, never user input.
    query = _utc_bounds_query(
        f"""SELECT count(*) AS row_count,
                    min(observed_at) AS first_observed_at,
                    max(observed_at) AS last_observed_at,
                    {', '.join(metric_expressions)}
               FROM {table}
              WHERE symbol = :symbol
                AND observed_at >= :since
                AND observed_at < :until""",
        timezone_aware=clock_timezone,
    )
    try:
        # A timeout aborts PostgreSQL's current transaction.  The savepoint
        # lets this read-only audit roll back just the bounded probe and keep
        # grading the remaining streams fail closed.
        with conn.begin_nested():
            row = conn.execute(
                query,
                {"symbol": label.symbol, "since": since, "until": until},
            ).mappings().one()
    except DBAPIError as exc:
        if _db_error_sqlstate(exc) != "57014":
            raise
        return _empty_stream_stats(
            table_present=table_present,
            columns_present=columns,
            audit_query_status="statement_timeout",
        )
    stats = {
        "row_count": int(row["row_count"] or 0),
        "first_observed_at": _iso(row["first_observed_at"]),
        "last_observed_at": _iso(row["last_observed_at"]),
        "provider_clock_count": int(row["provider_clock_count"] or 0),
        "receive_clock_count": int(row["receive_clock_count"] or 0),
        "available_clock_count": int(row["available_clock_count"] or 0),
        "run_identity_count": int(row["run_identity_count"] or 0),
        "generation_count": int(row["generation_count"] or 0),
        "audit_query_status": "complete",
    }
    stats.update(
        {
            "table_present": table_present,
            "schema_columns_present": sorted(
                columns & _STREAM_EXPECTED_COLUMNS
            ),
            "schema_columns_missing": sorted(
                _STREAM_EXPECTED_COLUMNS - columns
            ),
        }
    )
    return stats


def _simple_stats(
    conn: Any,
    *,
    table: str,
    symbol_column: str,
    clock_column: str,
    label: RossLabel,
) -> dict[str, Any]:
    since, until = _naive_utc_day_bounds(label.trade_date)
    query = _naive_utc_bounds_query(
        f"""SELECT count(*) AS row_count,
                    min({clock_column}) AS first_observed_at,
                    max({clock_column}) AS last_observed_at
               FROM {table}
              WHERE {symbol_column} = :symbol
                AND {clock_column} >= :since
                AND {clock_column} < :until"""
    )
    try:
        with conn.begin_nested():
            row = conn.execute(
                query,
                {"symbol": label.symbol, "since": since, "until": until},
            ).mappings().one()
    except DBAPIError as exc:
        if _db_error_sqlstate(exc) != "57014":
            raise
        return {
            "row_count": 0,
            "first_observed_at": None,
            "last_observed_at": None,
            "audit_query_status": "statement_timeout",
        }
    return {
        "row_count": int(row["row_count"] or 0),
        "first_observed_at": _iso(row["first_observed_at"]),
        "last_observed_at": _iso(row["last_observed_at"]),
        "audit_query_status": "complete",
    }


def _db_error_sqlstate(exc: DBAPIError) -> str | None:
    original = getattr(exc, "orig", None)
    return getattr(original, "sqlstate", None) or getattr(
        original,
        "pgcode",
        None,
    )


def _naive_utc_day_bounds(trade_date: date) -> tuple[datetime, datetime]:
    """Return the ET calendar day as naive UTC for legacy DB columns.

    The audited clock columns are PostgreSQL ``timestamp without time zone``
    values whose application-level convention is UTC.  Passing aware values
    would make PostgreSQL apply the connection's session timezone while
    resolving a mixed ``timestamptz``/``timestamp`` comparison.  Convert once
    in Python and bind explicitly as timezone-free timestamps instead.
    """

    since, until = _utc_day_bounds(trade_date)
    return since.replace(tzinfo=None), until.replace(tzinfo=None)


def _utc_day_bounds(trade_date: date) -> tuple[datetime, datetime]:
    """Return exact aware-UTC bounds for one America/New_York calendar day."""

    since = datetime.combine(trade_date, time.min, tzinfo=ET).astimezone(UTC)
    until = datetime.combine(
        date.fromordinal(trade_date.toordinal() + 1),
        time.min,
        tzinfo=ET,
    ).astimezone(UTC)
    return since, until


def _naive_utc_bounds_query(sql: str):
    return _utc_bounds_query(sql, timezone_aware=False)


def _utc_bounds_query(sql: str, *, timezone_aware: bool):
    return text(sql).bindparams(
        bindparam("since", type_=DateTime(timezone=timezone_aware)),
        bindparam("until", type_=DateTime(timezone=timezone_aware)),
    )


def _iso(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, datetime):
        raise TypeError("database clock must be a datetime")
    value = raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def coverage_reasons(
    label: RossLabel,
    *,
    trades: Mapping[str, Any],
    quotes: Mapping[str, Any],
    depth: Mapping[str, Any],
    sessions: Mapping[str, Any],
) -> tuple[str, ...]:
    reasons: list[str] = []
    phase_boundary = label.coverage_audit_phase_at_utc
    if phase_boundary is None:
        reasons.append("phase_boundary_not_independently_verified")
    for name, stats in (("trade", trades), ("quote", quotes), ("l2", depth)):
        query_status = str(stats.get("audit_query_status") or "complete")
        if query_status != "complete":
            reasons.append(f"{name}_audit_query_{query_status}")
        elif int(stats.get("row_count") or 0) <= 0:
            reasons.append(f"{name}_stream_missing")
        first_raw = stats.get("first_observed_at")
        if phase_boundary is not None and first_raw:
            first = datetime.fromisoformat(str(first_raw).replace("Z", "+00:00"))
            if first > phase_boundary:
                reasons.append(f"{name}_stream_starts_after_labeled_phase")
    for name, stats in (("trade", trades), ("quote", quotes)):
        if stats.get("table_present") is False:
            reasons.append(f"{name}_stream_table_missing")
        for column in stats.get("schema_columns_missing") or ():
            reasons.append(f"{name}_{column}_column_missing")
        count = int(stats.get("row_count") or 0)
        if count and int(stats.get("provider_clock_count") or 0) != count:
            reasons.append(f"exact_{name}_provider_event_clock_incomplete")
        if count and int(stats.get("receive_clock_count") or 0) != count:
            reasons.append(f"exact_{name}_receive_clock_incomplete")
        if count and int(stats.get("available_clock_count") or 0) != count:
            reasons.append(f"exact_{name}_available_at_incomplete")
        if count and int(stats.get("run_identity_count") or 0) != count:
            reasons.append(f"{name}_run_identity_incomplete")
        if count and int(stats.get("generation_count") or 0) != count:
            reasons.append(f"{name}_connection_generation_incomplete")
    if phase_boundary is not None:
        session_query_status = str(
            sessions.get("audit_query_status") or "complete"
        )
        if session_query_status != "complete":
            reasons.append(
                f"fsm_session_audit_query_{session_query_status}"
            )
        session_first = sessions.get("first_observed_at")
        if not session_first:
            reasons.append("fsm_session_missing")
        else:
            first = datetime.fromisoformat(str(session_first).replace("Z", "+00:00"))
            if first > phase_boundary:
                reasons.append("fsm_session_starts_after_labeled_phase")
    reasons.extend(
        (
            "provider_watermark_not_proven",
            "bounded_lateness_not_proven",
            "sealed_read_receipts_not_available",
        )
    )
    return tuple(dict.fromkeys(reasons))


def _stats_for_symbol_day(
    conn: Any,
    label: RossLabel,
    *,
    stream_schema: Mapping[str, Collection[str]] | None = None,
) -> dict[str, Any]:
    schema = stream_schema or {
        table: _STREAM_EXPECTED_COLUMNS for table in _STREAM_CLOCK_TIMEZONE
    }
    return {
        "iqfeed_trades": _stream_stats(
            conn,
            "iqfeed_trade_ticks",
            label,
            available_columns=schema.get("iqfeed_trade_ticks", ()),
        ),
        "nbbo_quotes": _stream_stats(
            conn,
            "momentum_nbbo_spread_tape",
            label,
            available_columns=schema.get("momentum_nbbo_spread_tape", ()),
        ),
        "l2_snapshots": _simple_stats(
            conn,
            table="iqfeed_depth_snapshots",
            symbol_column="symbol",
            clock_column="observed_at",
            label=label,
        ),
        "viability": _simple_stats(
            conn,
            table="momentum_viability_history",
            symbol_column="symbol",
            clock_column="observed_at",
            label=label,
        ),
        "fsm_sessions": _simple_stats(
            conn,
            table="trading_automation_sessions",
            symbol_column="symbol",
            clock_column="started_at",
            label=label,
        ),
    }


def _audit_one(label: RossLabel, streams: Mapping[str, Any]) -> dict[str, Any]:
    trades = streams["iqfeed_trades"]
    quotes = streams["nbbo_quotes"]
    depth = streams["l2_snapshots"]
    viability = streams["viability"]
    sessions = streams["fsm_sessions"]
    reasons = coverage_reasons(
        label,
        trades=trades,
        quotes=quotes,
        depth=depth,
        sessions=sessions,
    )
    return {
        "label_id": label.label_id,
        "symbol": label.symbol,
        "trade_date": label.trade_date.isoformat(),
        "timing_basis": label.timing_basis,
        "coverage_audit_time_role": label.coverage_audit_time_role,
        "coverage_audit_phase_at_utc": _iso(label.coverage_audit_phase_at_utc),
        "benchmark_entry_price": label.benchmark_entry_price,
        "coverage_status": "coverage_unavailable" if reasons else "diagnostic_only",
        "coverage_reasons": list(reasons),
        "streams": dict(streams),
    }


def run_audit(
    *,
    database_url: str,
    labels: Iterable[RossLabel],
    statement_timeout_ms: int,
) -> dict[str, Any]:
    if statement_timeout_ms <= 0:
        raise ValueError("statement timeout must be positive")
    engine = create_engine(database_url, future=True, pool_pre_ping=False)
    try:
        with engine.connect() as conn, conn.begin():
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            # Retained clock schemas are mixed (legacy naive-UTC TIMESTAMP and
            # TIMESTAMPTZ). Pin the transaction so inherited role/database
            # settings cannot change any defensive server-side interpretation.
            conn.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
            conn.exec_driver_sql(
                "SET LOCAL statement_timeout = %s",
                (int(statement_timeout_ms),),
            )
            stream_schema = _stream_schema_columns(conn)
            cache: dict[tuple[date, str], dict[str, Any]] = {}
            rows = []
            for label in labels:
                key = (label.trade_date, label.symbol)
                streams = cache.get(key)
                if streams is None:
                    streams = _stats_for_symbol_day(
                        conn,
                        label,
                        stream_schema=stream_schema,
                    )
                    cache[key] = streams
                rows.append(_audit_one(label, streams))
    finally:
        engine.dispose()
    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "certification_eligible": False,
        "rows": rows,
        "summary": {
            "label_count": len(rows),
            "coverage_unavailable_count": sum(
                row["coverage_status"] == "coverage_unavailable" for row in rows
            ),
        },
    }


def main() -> int:
    args = _parser().parse_args()
    labels = load_labels(args.manifest)
    selected = {str(value) for value in args.label_id}
    if selected:
        labels = tuple(row for row in labels if row.label_id in selected)
        missing = selected - {row.label_id for row in labels}
        if missing:
            raise SystemExit(f"unknown label ids: {sorted(missing)}")
    payload = run_audit(
        database_url=str(args.database_url),
        labels=labels,
        statement_timeout_ms=int(args.statement_timeout_ms),
    )
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
