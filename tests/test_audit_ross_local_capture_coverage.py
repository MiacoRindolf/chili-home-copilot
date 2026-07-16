from __future__ import annotations

from datetime import date, datetime, time
from contextlib import nullcontext
import json
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from scripts import audit_ross_local_capture_coverage as audit
from scripts.audit_ross_local_capture_coverage import RossLabel, coverage_reasons


def _label(*, approximate: bool = True) -> RossLabel:
    return RossLabel(
        label_id="ross_phase",
        symbol="PLSM",
        trade_date=date(2026, 7, 13),
        timing_basis="approximate" if approximate else "sequence_only",
        coverage_audit_time_role=(
            "candidate_phase_boundary" if approximate else None
        ),
        coverage_audit_phase_time_et=time(7, 58) if approximate else None,
        benchmark_entry_price=6.52,
    )


def _full_stream(first: str) -> dict:
    return {
        "row_count": 10,
        "first_observed_at": first,
        "provider_clock_count": 10,
        "receive_clock_count": 10,
        "available_clock_count": 10,
        "run_identity_count": 10,
        "generation_count": 10,
    }


def test_late_legacy_streams_and_missing_clocks_fail_closed():
    reasons = coverage_reasons(
        _label(),
        trades={
            **_full_stream("2026-07-13T12:02:34Z"),
            "provider_clock_count": 0,
            "receive_clock_count": 0,
            "available_clock_count": 0,
        },
        quotes={
            **_full_stream("2026-07-13T12:01:38Z"),
            "provider_clock_count": 0,
            "receive_clock_count": 0,
            "available_clock_count": 0,
        },
        depth={"row_count": 1, "first_observed_at": "2026-07-13T12:02:33Z"},
        sessions={"row_count": 1, "first_observed_at": "2026-07-13T12:04:03Z"},
    )

    assert "trade_stream_starts_after_labeled_phase" in reasons
    assert "quote_stream_starts_after_labeled_phase" in reasons
    assert "l2_stream_starts_after_labeled_phase" in reasons
    assert "fsm_session_starts_after_labeled_phase" in reasons
    assert "exact_trade_provider_event_clock_incomplete" in reasons
    assert "exact_quote_receive_clock_incomplete" in reasons
    assert "exact_trade_available_at_incomplete" in reasons
    assert "exact_quote_available_at_incomplete" in reasons
    assert "sealed_read_receipts_not_available" in reasons


def test_sequence_only_label_never_invents_a_phase_boundary():
    full = _full_stream("2026-07-13T10:00:00Z")
    reasons = coverage_reasons(
        _label(approximate=False),
        trades=full,
        quotes=full,
        depth={"row_count": 1, "first_observed_at": "2026-07-13T10:00:00Z"},
        sessions={"row_count": 1, "first_observed_at": "2026-07-13T10:00:00Z"},
    )

    assert "phase_boundary_not_independently_verified" in reasons
    assert "provider_watermark_not_proven" in reasons


def test_loader_ignores_generic_headline_scanner_and_cross_account_times(tmp_path):
    manifest = {
        "trade_date": "2026-07-13",
        "phase_labels": [
            {
                "label_id": "generic_context_only",
                "symbol": "VEEE",
                "approx_event_time_et": "08:50:00",
                "headline_context_time_et": "06:59:00",
                "scanner_observations_et": ["08:58:59", "08:59:08"],
                "cross_account_context": {
                    "symbol": "VE",
                    "campaign_start_approx_et": "08:50:00",
                },
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    (loaded,) = audit.load_labels(path)

    assert loaded.coverage_audit_time_role is None
    assert loaded.coverage_audit_phase_time_et is None
    assert loaded.coverage_audit_phase_at_utc is None


def test_loader_accepts_only_role_bound_explicit_coverage_phase_time(tmp_path):
    manifest = {
        "trade_date": "2026-07-13",
        "phase_labels": [
            {
                "label_id": "explicit_phase",
                "symbol": "PLSM",
                "coverage_audit_time_role": "candidate_phase_boundary",
                "coverage_audit_phase_time_et": "07:58:33",
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    (loaded,) = audit.load_labels(path)

    assert loaded.coverage_audit_time_role == "candidate_phase_boundary"
    assert loaded.coverage_audit_phase_time_et == time(7, 58, 33)
    assert loaded.coverage_audit_phase_at_utc == datetime(
        2026, 7, 13, 11, 58, 33, tzinfo=audit.UTC
    )


@pytest.mark.parametrize(
    "row",
    (
        {
            "coverage_audit_phase_time_et": "07:58:33",
            "coverage_audit_time_role": "headline_context",
        },
        {"coverage_audit_time_role": "headline_context"},
        {"coverage_audit_time_role": "candidate_phase_boundary"},
        {
            "coverage_audit_phase_time_et": "",
            "coverage_audit_time_role": "candidate_phase_boundary",
        },
        {
            "coverage_audit_phase_time_et": "07:58:33-04:00",
            "coverage_audit_time_role": "candidate_phase_boundary",
        },
    ),
)
def test_loader_rejects_unbound_explicit_coverage_phase_time(tmp_path, row):
    manifest = {
        "trade_date": "2026-07-13",
        "phase_labels": [
            {"label_id": "bad_phase", "symbol": "PLSM", **row}
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="coverage_audit"):
        audit.load_labels(path)


@pytest.mark.parametrize(
    ("manifest_json", "message"),
    (
        (
            '{"trade_date":"2026-07-13","phase_labels":[],"phase_labels":[]}',
            "duplicate JSON key",
        ),
        (
            '{"trade_date":"2026-07-13","phase_labels":[],"probe":NaN}',
            "non-finite JSON number",
        ),
        (
            '{"trade_date":"2026-07-13","phase_labels":[],"probe":Infinity}',
            "non-finite JSON number",
        ),
        (
            '{"trade_date":"2026-07-13","phase_labels":[],"probe":-Infinity}',
            "non-finite JSON number",
        ),
    ),
)
def test_loader_rejects_duplicate_keys_and_nonfinite_json(
    tmp_path,
    manifest_json,
    message,
):
    path = tmp_path / "manifest.json"
    path.write_text(manifest_json, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        audit.load_labels(path)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_finite_positive_rejects_all_nonfinite_values(value):
    assert audit._finite_positive(value) is None


def test_corrected_canonical_and_enrichment_fixtures_expose_no_phase_boundary():
    root = Path(__file__).parent / "fixtures" / "ross_replay"

    canonical = audit.load_labels(root / "small_account_challenge_manifest.json")
    enrichment = audit.load_labels(root / "2026-07-13_RZbM0qXOFbc.json")

    assert len(canonical) == 12
    assert all(row.coverage_audit_time_role is None for row in canonical)
    assert all(row.coverage_audit_phase_at_utc is None for row in canonical)
    assert enrichment == ()


@pytest.mark.parametrize(
    ("trade_date", "expected_since", "expected_until"),
    (
        (
            date(2026, 7, 13),
            datetime(2026, 7, 13, 4),
            datetime(2026, 7, 14, 4),
        ),
        (
            date(2026, 3, 8),
            datetime(2026, 3, 8, 5),
            datetime(2026, 3, 9, 4),
        ),
    ),
)
def test_et_day_bounds_are_bound_as_naive_utc_even_across_dst(
    trade_date,
    expected_since,
    expected_until,
):
    since, until = audit._naive_utc_day_bounds(trade_date)

    assert (since, until) == (expected_since, expected_until)
    assert since.tzinfo is None
    assert until.tzinfo is None


class _Rows:
    def mappings(self):
        return self

    def one(self):
        return {
            "row_count": 0,
            "first_observed_at": None,
            "last_observed_at": None,
            "provider_clock_count": 0,
            "receive_clock_count": 0,
            "available_clock_count": 0,
            "run_identity_count": 0,
            "generation_count": 0,
        }


class _SchemaRows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _QueryCapture:
    def __init__(self):
        self.calls = []

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        return _Rows()

    def begin_nested(self):
        return nullcontext()


def test_clock_queries_bind_utc_window_with_the_real_column_timezone_type():
    conn = _QueryCapture()
    label = _label()

    audit._stream_stats(conn, "iqfeed_trade_ticks", label)
    audit._stream_stats(conn, "momentum_nbbo_spread_tape", label)
    audit._simple_stats(
        conn,
        table="iqfeed_depth_snapshots",
        symbol_column="symbol",
        clock_column="observed_at",
        label=label,
    )

    assert len(conn.calls) == 3
    for index, (statement, parameters) in enumerate(conn.calls):
        compiled = statement.compile()
        timezone_aware = index == 1
        if timezone_aware:
            assert parameters["since"] == datetime(
                2026, 7, 13, 4, tzinfo=audit.UTC
            )
            assert parameters["until"] == datetime(
                2026, 7, 14, 4, tzinfo=audit.UTC
            )
            assert parameters["since"].utcoffset().total_seconds() == 0
            assert parameters["until"].utcoffset().total_seconds() == 0
        else:
            assert parameters["since"] == datetime(2026, 7, 13, 4)
            assert parameters["until"] == datetime(2026, 7, 14, 4)
            assert parameters["since"].tzinfo is None
            assert parameters["until"].tzinfo is None
        assert compiled.binds["since"].type.timezone is timezone_aware
        assert compiled.binds["until"].type.timezone is timezone_aware


def test_stream_query_omits_absent_optional_column_and_surfaces_schema_gap():
    conn = _QueryCapture()
    columns = audit._STREAM_EXPECTED_COLUMNS - {"available_at"}

    stats = audit._stream_stats(
        conn,
        "iqfeed_trade_ticks",
        _label(),
        available_columns=columns,
    )

    assert len(conn.calls) == 1
    statement = str(conn.calls[0][0])
    assert "available_at IS NOT NULL" not in statement
    assert "0::bigint AS available_clock_count" in statement
    assert stats["available_clock_count"] == 0
    assert stats["schema_columns_missing"] == ["available_at"]
    assert stats["table_present"] is True


def test_missing_stream_schema_is_an_explicit_fail_closed_coverage_reason():
    full = _full_stream("2026-07-13T10:00:00Z")
    trades = {
        **full,
        "table_present": True,
        "schema_columns_missing": ["available_at"],
    }
    quotes = {
        **full,
        "table_present": False,
        "schema_columns_missing": sorted(audit._STREAM_EXPECTED_COLUMNS),
    }

    reasons = coverage_reasons(
        _label(),
        trades=trades,
        quotes=quotes,
        depth={"row_count": 1, "first_observed_at": "2026-07-13T10:00:00Z"},
        sessions={"row_count": 1, "first_observed_at": "2026-07-13T10:00:00Z"},
    )

    assert "trade_available_at_column_missing" in reasons
    assert "quote_stream_table_missing" in reasons
    assert "quote_observed_at_column_missing" in reasons


def test_stream_schema_inventory_is_fixed_and_collected_once():
    class _SchemaConnection:
        def __init__(self):
            self.calls = []

        def execute(self, statement):
            self.calls.append(statement)
            return _SchemaRows(
                [
                    {
                        "table_name": "iqfeed_trade_ticks",
                        "column_name": "symbol",
                    },
                    {
                        "table_name": "iqfeed_trade_ticks",
                        "column_name": "observed_at",
                    },
                    {
                        "table_name": "ignored_table",
                        "column_name": "available_at",
                    },
                ]
            )

    conn = _SchemaConnection()

    inventory = audit._stream_schema_columns(conn)

    assert len(conn.calls) == 1
    assert inventory == {
        "iqfeed_trade_ticks": frozenset({"symbol", "observed_at"}),
        "momentum_nbbo_spread_tape": frozenset(),
    }


def test_statement_timeout_is_a_bounded_fail_closed_result_not_a_crash():
    class _Canceled(Exception):
        pgcode = "57014"

    class _TimeoutConnection:
        def begin_nested(self):
            return nullcontext()

        def execute(self, _statement, _parameters):
            raise OperationalError("SELECT", {}, _Canceled())

    stats = audit._stream_stats(
        _TimeoutConnection(),
        "iqfeed_trade_ticks",
        _label(),
        available_columns=audit._STREAM_EXPECTED_COLUMNS,
    )

    assert stats["audit_query_status"] == "statement_timeout"
    assert stats["row_count"] == 0
    reasons = coverage_reasons(
        _label(),
        trades=stats,
        quotes=_full_stream("2026-07-13T10:00:00Z"),
        depth={"row_count": 1, "first_observed_at": "2026-07-13T10:00:00Z"},
        sessions={"row_count": 1, "first_observed_at": "2026-07-13T10:00:00Z"},
    )
    assert "trade_audit_query_statement_timeout" in reasons
    assert "trade_stream_missing" not in reasons


class _Context:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeConnection(_Context):
    def __init__(self):
        self.session_timezone = "Pacific/Honolulu"
        self.driver_sql = []

    def begin(self):
        return _Context()

    def exec_driver_sql(self, statement, parameters=None):
        self.driver_sql.append((statement, parameters))
        if statement == "SET LOCAL TIME ZONE 'UTC'":
            self.session_timezone = "UTC"

    def execute(self, statement):
        return _SchemaRows(
            [
                {"table_name": table, "column_name": column}
                for table in audit._STREAM_CLOCK_TIMEZONE
                for column in audit._STREAM_EXPECTED_COLUMNS
            ]
        )


class _FakeEngine:
    def __init__(self):
        self.connection = _FakeConnection()
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def test_audit_pins_inherited_non_utc_postgres_session_before_queries(monkeypatch):
    engine = _FakeEngine()
    observed = []

    monkeypatch.setattr(audit, "create_engine", lambda *_args, **_kwargs: engine)

    def _stats(conn, label, *, stream_schema):
        assert set(stream_schema) == set(audit._STREAM_CLOCK_TIMEZONE)
        observed.append((conn.session_timezone, audit._naive_utc_day_bounds(label.trade_date)))
        empty = {"row_count": 0, "first_observed_at": None, "last_observed_at": None}
        stream = {
            **empty,
            "provider_clock_count": 0,
            "receive_clock_count": 0,
            "available_clock_count": 0,
            "run_identity_count": 0,
            "generation_count": 0,
        }
        return {
            "iqfeed_trades": stream,
            "nbbo_quotes": stream,
            "l2_snapshots": empty,
            "viability": empty,
            "fsm_sessions": empty,
        }

    monkeypatch.setattr(audit, "_stats_for_symbol_day", _stats)

    audit.run_audit(
        database_url="postgresql://unused",
        labels=(_label(),),
        statement_timeout_ms=1_000,
    )

    assert observed == [
        (
            "UTC",
            (datetime(2026, 7, 13, 4), datetime(2026, 7, 14, 4)),
        )
    ]
    assert [statement for statement, _ in engine.connection.driver_sql] == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL TIME ZONE 'UTC'",
        "SET LOCAL statement_timeout = %s",
    ]
    assert engine.disposed is True
