"""DB-free guards for scripts/bench_iqfeed_tape_write_path.py.

The benchmark reproduces the trade bridge's per-batch statement shape against
a throwaway table.  These tests pin (1) the ``*_test``-only database guard,
(2) that the reproduced statement is the bridge's ``INSERT ... SELECT FROM
(VALUES ...) RETURNING id`` shape with one bind per column per row, and
(3) that the synthetic rows carry the release-identity fields the bridge's
``_require_release_identity`` demands.  No database connection is opened.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "bench_iqfeed_tape_write_path.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_iqfeed_tape_write_path", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://chili:chili@localhost:5433/chili",
        "postgresql://chili:chili@localhost:5433/chili_staging",
        "postgresql://chili:chili@localhost:5433/chili_test_not",
        "postgresql://chili:chili@localhost:5433/chili?sslmode=disable",
    ],
)
def test_refuses_non_test_database(bench, dsn):
    with pytest.raises(SystemExit):
        bench._require_test_db(dsn)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://chili:chili@localhost:5433/chili_repro_test",
        "postgresql://chili:chili@localhost:5433/chili_b_test?application_name=x",
    ],
)
def test_accepts_test_database(bench, dsn):
    bench._require_test_db(dsn)


def test_rows_carry_release_identity(bench):
    rows = bench._rows(5, ["AAA", "BBB"], 100, "run-id", 2, datetime.now(timezone.utc))
    assert [r["source_frame_sequence"] for r in rows] == [100, 101, 102, 103, 104]
    assert len({r["source_frame_sha256"] for r in rows}) == 5
    assert all(len(r["source_frame_sha256"]) == 64 for r in rows)
    assert all(r["connection_generation"] == 2 and r["bridge_run_id"] == "run-id" for r in rows)
    assert all(r["at"].tzinfo is None for r in rows)


def test_bridge_statement_shape_and_bind_count(bench):
    n = 50
    rows = bench._rows(n, ["AAA"], 1, "run-id", 1, datetime.now(timezone.utc))
    compiled = bench._bridge_values_insert(rows).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith(f"INSERT INTO {bench.TABLE}")
    assert "incoming_trade_rows" in sql and "VALUES" in sql
    assert sql.rstrip().endswith("RETURNING " + bench.TABLE + ".id")
    # one bind parameter per column per row, exactly as in the bridge
    assert len(compiled.params) == n * len(bench.COLUMNS)
    # the bind budget the bridge enforces: catch-up cap * 18 < 65,535
    assert 3600 * 18 < 65_535
