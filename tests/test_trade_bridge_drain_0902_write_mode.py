"""P2 tape write mode: COPY -> execute_values -> values, never a dropped batch.

NASUKAT (bench_iqfeed_tape_drain.py, chili_b_test, 3,600-event batch):
  insert / sa.values INSERT...SELECT RETURNING : 1.9-2.5 s (client compile 1.5-2.0 s)
  insert / psycopg2 execute_values             : 0.45-0.51 s
  insert / COPY + nextval id pre-allocation    : 0.15-0.18 s

Ang panganib ay HINDI ang bilis kundi ang PAGKAWALA: pinu-pop ng writer ang
batch mula sa deques BAGO ang sulat (L1332-1335), kaya ang isang sistematikong
depekto sa isang bagong write mode ay MAGTATAPON ng bawat batch hanggang sa
susunod na restart. Kaya ang chain: copy -> execute_values -> values, at ang
lumang `values` path ay nananatiling hindi ginagalaw bilang huling link.

Ang session TimeZone ay bahagi ng CORRECTNESS: NAIVE ang nbbo `at` papasok sa
isang timestamptz column, kaya parehong nakadepende ang CAST bind path at ang
COPY text sa session TZ (7 oras na skew sa America/Los_Angeles).

DB-free (fake cursors/connections). Runnable:
    pytest tests/test_trade_bridge_drain_0902_write_mode.py -v
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import pathlib
import sys
from datetime import datetime, timezone

import pytest

_BRIDGE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "iqfeed_trade_bridge.py"
)
_MODULE = "iqfeed_trade_bridge_drain0902"
if _MODULE not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MODULE, _BRIDGE_PATH)
    bridge = importlib.util.module_from_spec(_spec)
    sys.modules[_MODULE] = bridge
    _spec.loader.exec_module(bridge)
else:  # pragma: no cover - module cached across test files
    bridge = sys.modules[_MODULE]

_T0 = datetime(2026, 9, 2, 13, 30, 0, tzinfo=timezone.utc)


def _trade(sequence: int) -> dict:
    return {
        "sym": "AAA",
        "at": _T0.replace(tzinfo=None),
        "px": 1.01,
        "sz": 100.0,
        "bid": 1.0,
        "ask": 1.02,
        "provider_at": None,
        "received_at": _T0,
        "provider_trade_reference_at": _T0,
        "basis": bridge.AUTHORITATIVE_TIMESTAMP_BASIS,
        "bridge": bridge.BRIDGE_BUILD,
        "message_type": "Q",
        "bridge_run_id": bridge.BRIDGE_RUN_ID,
        "connection_generation": 1,
        "source_frame_sequence": sequence,
        "source_frame_sha256": hashlib.sha256(f"f{sequence}".encode()).hexdigest(),
    }


class _FakeCursor:
    def __init__(self, owner, *, fail_copy=False, rowcount_override=None):
        self.owner = owner
        self.fail_copy = fail_copy
        self.rowcount = 0
        self._rowcount_override = rowcount_override
        self.closed = False

    def copy_expert(self, sql, buffer):
        if self.fail_copy:
            raise RuntimeError("simulated COPY failure")
        payload = buffer.read()
        self.owner.copy_sql.append(sql)
        self.owner.copy_payloads.append(payload)
        lines = [line for line in payload.split("\n") if line]
        self.rowcount = (
            len(lines)
            if self._rowcount_override is None
            else self._rowcount_override
        )

    def execute(self, sql, params=None):
        self.owner.cursor_sql.append(sql)

    def fetchall(self):  # pragma: no cover - execute_values path uses its own
        return []

    def close(self):
        self.closed = True


class _FakeDBAPI:
    def __init__(self, owner, **kwargs):
        self.owner = owner
        self.kwargs = kwargs

    def cursor(self):
        cursor = _FakeCursor(self.owner, **self.kwargs)
        self.owner.cursors.append(cursor)
        return cursor


class _FakeResult:
    def __init__(self, values, rowcount=None):
        self._values = list(values)
        self.rowcount = len(self._values) if rowcount is None else rowcount

    def scalars(self):
        return iter(self._values)


class _FakeConnection:
    """Records the SQL a write mode issues; hands out fake DBAPI cursors."""

    def __init__(self, *, next_id=1000, fail_copy=False, rowcount_override=None):
        self.sql: list[str] = []
        self.copy_sql: list[str] = []
        self.copy_payloads: list[str] = []
        self.cursor_sql: list[str] = []
        self.cursors: list[_FakeCursor] = []
        self.next_id = next_id
        self.connection = _FakeDBAPI(
            self, fail_copy=fail_copy, rowcount_override=rowcount_override
        )

    def execute(self, statement, params=None):
        text = str(statement)
        self.sql.append(text)
        if "nextval" in text:
            count = int(params["count"])
            ids = list(range(self.next_id, self.next_id + count))
            self.next_id += count
            return _FakeResult(ids)
        return _FakeResult([], rowcount=0)


class _Factory:
    """Mimics ``engine.begin`` — a context manager per attempt."""

    def __init__(self, connections):
        self.connections = list(connections)
        self.opened: list[_FakeConnection] = []

    def __call__(self):
        connection = self.connections.pop(0)
        self.opened.append(connection)
        factory = self

        class _Ctx:
            def __enter__(self):
                return connection

            def __exit__(self, *exc):
                return False

        return _Ctx()


def test_copy_text_value_formats_null_tz_naive_float_and_escapes():
    assert bridge._copy_text_value(None) == "\\N"
    assert bridge._copy_text_value(_T0) == "2026-09-02T13:30:00+00:00"
    assert bridge._copy_text_value(_T0.replace(tzinfo=None)) == "2026-09-02T13:30:00"
    assert bridge._copy_text_value(1.5) == "1.5"
    assert bridge._copy_text_value(float("nan")) == "nan"
    assert bridge._copy_text_value(7) == "7"
    assert bridge._copy_text_value(True) == "true"
    assert bridge._copy_text_value("a\\b\tc\nd\re") == "a\\\\b\\tc\\nd\\re"


def test_copy_batch_preallocates_ids_releases_exactly_those_ids_and_checks_identity(
    monkeypatch,
):
    monkeypatch.setitem(
        bridge._TAPE_SEQUENCES, "iqfeed_trade_ticks", "iqfeed_trade_ticks_id_seq"
    )
    monkeypatch.setitem(
        bridge._TAPE_SEQUENCES,
        "momentum_nbbo_spread_tape",
        "momentum_nbbo_spread_tape_id_seq",
    )
    connection = _FakeConnection(next_id=5000)
    rows = [_trade(1), _trade(2)]
    trade_ids, quote_ids, build_ms, execute_ms = bridge._insert_pending_batch_copy(
        connection, trade_rows=rows, quote_rows=[]
    )
    assert trade_ids == (5000, 5001)
    assert quote_ids == ()
    assert build_ms >= 0.0 and execute_ms >= 0.0
    assert connection.copy_sql[0].startswith(
        "COPY iqfeed_trade_ticks (id, symbol, observed_at"
    )
    assert "FROM STDIN WITH (FORMAT text)" in connection.copy_sql[0]
    payload_lines = connection.copy_payloads[0].strip("\n").split("\n")
    assert [line.split("\t")[0] for line in payload_lines] == ["5000", "5001"]

    # The pre-allocated ids still carry the full release identity invariants.
    released: list[tuple] = []

    class _ReleaseConnection:
        def execute(self, statement, params):
            released.append((str(statement), tuple(params["row_ids"])))
            return _FakeResult([], rowcount=len(params["row_ids"]))

    bridge._release_inserted_row_ids(
        _ReleaseConnection(),
        statement=bridge.MARK_TRADE_IDS_AVAILABLE,
        row_ids=trade_ids,
        expected=2,
        available_at=_T0,
        operation="trade primary-key release",
    )
    assert released[0][1] == (5000, 5001)
    with pytest.raises(RuntimeError, match="row identity mismatch"):
        bridge._release_inserted_row_ids(
            _ReleaseConnection(),
            statement=bridge.MARK_TRADE_IDS_AVAILABLE,
            row_ids=(5000, 5000),
            expected=2,
            available_at=_T0,
            operation="trade primary-key release",
        )


def test_copy_rowcount_mismatch_rolls_back_and_retries_via_execute_values_then_values(
    monkeypatch,
):
    monkeypatch.setitem(
        bridge._TAPE_SEQUENCES, "iqfeed_trade_ticks", "iqfeed_trade_ticks_id_seq"
    )
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", True)
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    monkeypatch.setattr(bridge, "_write_mode_fallbacks", {})
    rows = [_trade(1)]

    # COPY reports the wrong rowcount; execute_values then also fails; the
    # legacy `values` path takes the SAME batch and succeeds.
    def _boom_execute_values(connection, *, trade_rows, quote_rows):
        raise RuntimeError("simulated execute_values failure")

    values_calls: list[int] = []

    def _ok_values(connection, *, trade_rows, quote_rows):
        values_calls.append(len(trade_rows))
        return (7,), (), 0.0, 1.0

    monkeypatch.setitem(
        bridge._WRITE_MODE_INSERTERS, "execute_values", _boom_execute_values
    )
    monkeypatch.setitem(bridge._WRITE_MODE_INSERTERS, "values", _ok_values)
    factory = _Factory(
        [
            _FakeConnection(rowcount_override=99),
            _FakeConnection(),
            _FakeConnection(),
        ]
    )
    trade_ids, quote_ids, mode, timings = bridge._write_pending_batch(
        factory, trade_rows=rows, quote_rows=[]
    )
    assert mode == "values"
    assert trade_ids == (7,)
    assert values_calls == [1]
    assert bridge._write_mode_fallbacks == {"execute_values": 1, "values": 1}
    assert set(timings) == {
        "insert_client_build_ms",
        "insert_execute_ms",
        "insert_commit_ms",
        "insert_total_ms",
    }

    # And when EVERY link fails the exception surfaces so the writer's existing
    # capture-loss path runs -- exactly once, never silently.
    monkeypatch.setitem(bridge._WRITE_MODE_INSERTERS, "values", _boom_execute_values)
    with pytest.raises(RuntimeError, match="simulated execute_values failure"):
        bridge._write_pending_batch(
            _Factory(
                [
                    _FakeConnection(rowcount_override=99),
                    _FakeConnection(),
                    _FakeConnection(),
                ]
            ),
            trade_rows=rows,
            quote_rows=[],
        )


def test_sequence_resolution_failure_falls_back_to_execute_values_with_warning_never_raises(
    monkeypatch,
):
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", False)
    assert bridge._effective_tape_write_mode() == "execute_values"
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", True)
    assert bridge._effective_tape_write_mode() == "copy"
    # An unresolved sequence in the COPY path raises INSIDE the batch attempt,
    # which the fallback chain catches -- it never refuses startup.
    monkeypatch.setitem(bridge._TAPE_SEQUENCES, "iqfeed_trade_ticks", None)
    with pytest.raises(RuntimeError, match="id sequence is unresolved"):
        bridge._preallocate_row_ids(
            _FakeConnection(), table_name="iqfeed_trade_ticks", count=1
        )


def test_write_mode_env_selects_path_and_legacy_values_is_untouched():
    assert bridge._normalized_tape_write_mode("COPY ") == "copy"
    assert bridge._normalized_tape_write_mode("nonsense") == "copy"
    assert bridge._normalized_tape_write_mode("values") == "values"
    assert bridge._WRITE_MODE_CHAIN["copy"] == ("copy", "execute_values", "values")
    assert bridge._WRITE_MODE_CHAIN["execute_values"] == ("execute_values", "values")
    assert bridge._WRITE_MODE_CHAIN["values"] == ("values",)

    # AST guard: the legacy path stays the SQLAlchemy VALUES statement and
    # never grows a bulk-driver call, so `values` mode remains the byte-
    # identical last link of the fallback chain.
    tree = ast.parse(_BRIDGE_PATH.read_text(encoding="utf-8"))
    legacy = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_insert_pending_batch"
    )
    source = ast.dump(legacy)
    assert "from_select" in source
    assert "copy_expert" not in source
    assert "execute_values" not in source
    assert "nextval" not in source


def test_values_mode_bind_budget_clamps_with_warning_instead_of_raising(monkeypatch):
    assert bridge.VALUES_MODE_BIND_BUDGET_EVENTS == 3_640
    assert bridge.VALUES_MODE_BIND_BUDGET_EVENTS * 18 < 65_535
    monkeypatch.setattr(bridge, "DB_RELEASE_CATCHUP_BATCH_EVENTS", 10_000)
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", True)
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "values")
    assert bridge._batch_event_ceiling() == min(
        bridge.BATCH_EVENT_HARD_CEILING, bridge.VALUES_MODE_BIND_BUDGET_EVENTS
    )
    assert (
        bridge._release_batch_event_limit(pending_backlog=True)
        == bridge.VALUES_MODE_BIND_BUDGET_EVENTS
    )
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    assert bridge._batch_event_ceiling() == bridge.BATCH_EVENT_HARD_CEILING
    assert bridge._release_batch_event_limit(pending_backlog=True) == 10_000


def test_selftest_exercises_selected_mode_and_fallback(monkeypatch):
    monkeypatch.setattr(bridge, "_verify_bridge_schema", lambda: None)
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", True)
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    attempts: list[tuple[str, tuple[str, ...]]] = []

    def _fake_write(factory, *, trade_rows, quote_rows, mode=None):
        forced = tuple(sorted(bridge._FORCED_WRITE_MODE_FAILURES))
        chain = bridge._WRITE_MODE_CHAIN[
            bridge._normalized_tape_write_mode(
                mode or bridge._effective_tape_write_mode()
            )
        ]
        used = next(link for link in chain if link not in forced)
        attempts.append((used, forced))
        assert len(trade_rows) == 1
        assert trade_rows[0]["sym"] == "_SELFTEST"
        return (1,), (), used, {}

    released: list[tuple[int, ...]] = []

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=None):
            text = str(statement)
            if "count(*)" in text:
                return _Scalar(2)
            return _FakeResult([], rowcount=0)

    class _Scalar:
        def __init__(self, value):
            self.value = value

        def scalar(self):
            return self.value

    class _Engine:
        def begin(self):
            return _Ctx()

        def connect(self):
            return _Ctx()

    monkeypatch.setattr(bridge, "engine", _Engine())
    monkeypatch.setattr(bridge, "_write_pending_batch", _fake_write)
    monkeypatch.setattr(
        bridge,
        "_release_inserted_row_ids",
        lambda connection, **kwargs: released.append(kwargs["row_ids"]),
    )
    assert bridge._selftest() == 0
    assert attempts == [("copy", ()), ("execute_values", ("copy",))]
    assert released == [(1,), (1,)]
    assert not bridge._FORCED_WRITE_MODE_FAILURES


def test_engine_connect_sets_utc_and_verify_schema_asserts_timezone(monkeypatch):
    executed: list[str] = []

    class _Cursor:
        def execute(self, sql):
            executed.append(sql)

        def close(self):
            executed.append("close")

    class _DBAPI:
        def cursor(self):
            return _Cursor()

    bridge._set_bridge_session_utc(_DBAPI(), None)
    assert executed == ["SET TIME ZONE 'UTC'", "close"]

    class _TZResult:
        def __init__(self, value):
            self.value = value

        def scalar(self):
            return self.value

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=None):
            assert "SHOW TimeZone" in str(statement)
            return _TZResult("America/Los_Angeles")

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr(bridge, "engine", _Engine())
    with pytest.raises(RuntimeError, match="session time zone is not UTC"):
        bridge._verify_bridge_schema()
