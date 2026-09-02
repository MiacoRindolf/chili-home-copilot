"""P3 wall-time-bounded batches, drain telemetry, pending bound, hot overrides.

ANG KLOK NG CONSUMER ANG NAGDIDIKTA. Ang `received_age` ay sinusukat sa
CONSUME time = flush wait (<=1.0 s) + insert + release + delivery, kaya ang
WALL ng batch -- hindi ang frontier -- ang hadlang: anumang batch na lampas
~0.9 s ay nagpapabagsak sa BAWAT notify laban sa 2.0 s na authority gate ng
live loop kahit nakahabol na ang bridge. Kaya nagta-target ang controller sa
600 ms at humihinto nang matigas sa 900 ms, at HINDI itinataas ang event
ceiling sa itaas ng 3,600 sa PR na ito.

Hindi rin dinadagdagan ang exact-print heartbeat body: EKSAKTONG key-set
match ang ginagawa ng lane_health (L493) at ang content_sha256 ay sumasaklaw
sa body, kaya ang isang dagdag na susi ay magpapa-unparseable sa BAWAT
exact-print receipt at papatayin ang tape-liveness diagnostic. Sariling
job_type at sariling frozen key set ang drain telemetry.

DB-free. Runnable: pytest tests/test_trade_bridge_drain_0902_batching.py -v
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_BRIDGE_PATH = _REPO / "scripts" / "iqfeed_trade_bridge.py"
_MODULE = "iqfeed_trade_bridge_drain0902"
if _MODULE not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MODULE, _BRIDGE_PATH)
    bridge = importlib.util.module_from_spec(_spec)
    sys.modules[_MODULE] = bridge
    _spec.loader.exec_module(bridge)
else:  # pragma: no cover - module cached across test files
    bridge = sys.modules[_MODULE]

from app.services.trading import batch_job_constants
from app.services.trading.momentum_neural import lane_health

_T0 = datetime(2026, 9, 2, 13, 30, 0, tzinfo=timezone.utc)


def _row(symbol: str, sequence: int, *, age_s: float = 0.0) -> dict:
    return {
        "sym": symbol,
        "connection_generation": 1,
        "source_frame_sequence": sequence,
        "received_at": _T0 - timedelta(seconds=age_s),
        "source_frame_sha256": hashlib.sha256(f"f{sequence}".encode()).hexdigest(),
    }


def _seed(trades, quotes):
    with bridge._pending_lock:
        bridge._pending.clear()
        bridge._pending_nbbo.clear()
        bridge._pending.extend(trades)
        bridge._pending_nbbo.extend(quotes)


def test_batch_controller_targets_wall_time_and_never_exceeds_3600_or_values_cap():
    controller = bridge.BatchController(
        floor=256, ceil=3_600, target_ms=600.0, hard_stop_ms=900.0, max_bytes=1 << 23
    )
    assert controller.max_events == 256
    # A fast batch grows, but never by more than 2x per observation.
    assert controller.observe(100.0) == 512
    assert controller.observe(100.0) == 1_024
    # A batch over the hard stop halves IMMEDIATELY.
    assert controller.observe(4_400.0) == 512
    assert controller.observe(950.0) == 256
    # The floor holds.
    assert controller.observe(5_000.0) == 256
    # Growth toward the target is proportional and bounded by the ceiling.
    controller.max_events = 3_000
    assert controller.observe(300.0) == 3_600
    assert controller.observe(1.0) == 3_600
    controller.rebind_ceiling(bridge.VALUES_MODE_BIND_BUDGET_EVENTS)
    assert controller.max_events <= bridge.VALUES_MODE_BIND_BUDGET_EVENTS
    controller.rebind_ceiling(500)
    assert controller.max_events == 500
    with pytest.raises(ValueError):
        bridge.BatchController(
            floor=0, ceil=10, target_ms=1.0, hard_stop_ms=2.0, max_bytes=1
        )


def test_release_batch_event_limit_shim_keeps_two_level_result_without_controller(
    monkeypatch,
):
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", True)
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    assert bridge._release_batch_event_limit(pending_backlog=False) == (
        bridge.DB_RELEASE_BATCH_EVENTS
    )
    assert bridge._release_batch_event_limit(pending_backlog=True) == (
        bridge.DB_RELEASE_CATCHUP_BATCH_EVENTS
    )
    controller = bridge.BatchController(
        floor=256, ceil=3_600, target_ms=600.0, hard_stop_ms=900.0, max_bytes=1 << 23
    )
    controller.max_events = 1_500
    assert (
        bridge._release_batch_event_limit(
            pending_backlog=True, controller=controller
        )
        == 1_500
    )
    monkeypatch.setattr(bridge, "IQFEED_DB_BATCH_ADAPTIVE", False)
    assert (
        bridge._release_batch_event_limit(
            pending_backlog=True, controller=controller
        )
        == bridge.DB_RELEASE_CATCHUP_BATCH_EVENTS
    )
    # The non-adaptive branch must ALSO honour the hard ceiling. Without this
    # the documented day-1 kill switch (raise the catch-up env for COPY, then
    # IQFEED_DB_BATCH_ADAPTIVE=0) produced batches far above 3,600.
    monkeypatch.setattr(bridge, "DB_RELEASE_CATCHUP_BATCH_EVENTS", 20_000)
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", True)
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    assert (
        bridge._release_batch_event_limit(pending_backlog=True)
        == bridge.BATCH_EVENT_HARD_CEILING
    )
    assert (
        bridge._release_batch_event_limit(
            pending_backlog=True, controller=controller
        )
        == bridge.BATCH_EVENT_HARD_CEILING
    )


def test_drain_respects_byte_bound_without_splitting_a_frame():
    # Two rows share frame key (1, 1); the byte bound must never cut between
    # them, and it must never be tighter than one whole frame.
    _seed(
        [_row("AAA", 1), _row("AAA", 1)],
        [_row("AAA", 1), _row("BBB", 2)],
    )
    trades, quotes, backlog = bridge._drain_pending_write_batch(
        max_events=3_600,
        max_bytes=bridge.ESTIMATED_EVENT_BYTES,  # one event's worth
    )
    assert len(trades) == 2
    assert [row["source_frame_sequence"] for row in quotes] == [1]
    assert backlog is True
    _seed([], [])


def test_pending_bound_drops_oldest_quote_only_frames_via_capture_gap_never_trades(
    monkeypatch,
):
    assert bridge.IQFEED_PENDING_MAX_EVENTS == 0  # unbounded by default
    gaps: list[dict] = []
    monkeypatch.setattr(
        bridge,
        "_record_unreleased_capture_gap",
        lambda **kwargs: gaps.append(kwargs) or 1,
    )
    # A bound may only drop what it can gap-latch, so the handoff must be bound.
    monkeypatch.setattr(bridge, "_capture_handoff", object())
    # Frame 3 has a sibling TRADE, so it may never be dropped; frames 1 and 2
    # are quote-only and older.
    _seed(
        [_row("CCC", 3)],
        [_row("AAA", 1), _row("AAA", 1), _row("BBB", 2), _row("CCC", 3)],
    )
    # 1 trade + 4 quotes = 5 pending events; the bound of 3 drops the OLDEST
    # quote-only FRAME whole (both AAA rows share frame key (1, 1)).
    dropped = bridge._enforce_pending_bound(max_events=3, available_at=_T0)
    assert dropped == 2
    assert [gap["reason"] for gap in gaps] == [
        "iqfeed_l1_pending_bound_drop"
    ] * 2
    assert all(gap["streams"] == ("nbbo_quote",) for gap in gaps)
    assert [gap["symbol"] for gap in gaps] == ["AAA", "AAA"]
    with bridge._pending_lock:
        assert len(bridge._pending) == 1
        assert [row["sym"] for row in bridge._pending_nbbo] == ["BBB", "CCC"]
    # A trade-sibling frame at the head is SKIPPED, not a stop sign: the scan
    # continues past it and drops the quote-only frames behind it, preserving
    # order. Breaking here made the bound inert on any tape that has prints --
    # i.e. every tape during a session.
    gaps.clear()
    _seed(
        [_row("CCC", 3)],
        [_row("CCC", 3), _row("DDD", 4), _row("EEE", 5)],
    )
    assert bridge._enforce_pending_bound(max_events=2, available_at=_T0) == 2
    assert [gap["symbol"] for gap in gaps] == ["DDD", "EEE"]
    with bridge._pending_lock:
        assert [row["sym"] for row in bridge._pending_nbbo] == ["CCC"]
    # A tape made ENTIRELY of trade-paired frames still cannot be bounded -- and
    # must not lose a single row trying.
    gaps.clear()
    _seed([_row("CCC", 3)], [_row("CCC", 3)])
    assert bridge._enforce_pending_bound(max_events=1, available_at=_T0) == 0
    assert gaps == []
    with bridge._pending_lock:
        assert [row["sym"] for row in bridge._pending_nbbo] == ["CCC"]
    # And nothing is dropped when the backlog is already inside the bound.
    assert bridge._enforce_pending_bound(max_events=99, available_at=_T0) == 0
    assert gaps == []
    _seed([], [])


def test_pending_bound_is_inert_when_the_capture_handoff_is_unbound(monkeypatch):
    """Never trade unrecorded tape loss for a memory bound.

    With the handoff unbound, ``_record_unreleased_capture_gap`` either only
    logs (under --allow-uncaptured-diagnostic, which is exactly how production
    launches) or RAISES from a call site outside the writer's try -- killing the
    sole tape drain after the frames were already popped.
    """

    gaps: list[dict] = []
    monkeypatch.setattr(
        bridge,
        "_record_unreleased_capture_gap",
        lambda **kwargs: gaps.append(kwargs) or 1,
    )
    monkeypatch.setattr(bridge, "_capture_handoff", None)
    _seed([], [_row("AAA", 1), _row("BBB", 2), _row("CCC", 3)])
    assert bridge._enforce_pending_bound(max_events=1, available_at=_T0) == 0
    assert gaps == []
    with bridge._pending_lock:
        assert len(bridge._pending_nbbo) == 3
    _seed([], [])


def test_pending_bound_skips_trade_paired_frames_and_binds_a_dense_tape(
    monkeypatch,
):
    """The bound must bind on a realistic open tape, not only a print-free one.

    Every 5th frame carries a trade (a conservative open-tape print density).
    The old `break` returned after 4 drops on this shape; the bound is only
    real if it walks past the paired frames.
    """

    monkeypatch.setattr(
        bridge, "_record_unreleased_capture_gap", lambda **kwargs: 1
    )
    monkeypatch.setattr(bridge, "_capture_handoff", object())
    trades = [_row(f"S{seq:03d}", seq) for seq in range(5, 1_001, 5)]
    quotes = [_row(f"S{seq:03d}", seq) for seq in range(1, 1_001)]
    _seed(trades, quotes)
    before = len(trades) + len(quotes)
    dropped = bridge._enforce_pending_bound(max_events=400, available_at=_T0)
    with bridge._pending_lock:
        after = len(bridge._pending) + len(bridge._pending_nbbo)
        remaining_quotes = [
            row["source_frame_sequence"] for row in bridge._pending_nbbo
        ]
    assert dropped == before - after
    # Trades are never dropped, and every surviving quote is trade-paired, so
    # the floor is 200 trades + 200 paired quotes = 400 -- the bound exactly.
    assert after == 400
    assert all(seq % 5 == 0 for seq in remaining_quotes)
    # Order is preserved across the skips.
    assert remaining_quotes == sorted(remaining_quotes)
    _seed([], [])


def test_oldest_pending_received_age_reads_the_deques_not_the_tape():
    _seed([_row("AAA", 1, age_s=42.0)], [_row("BBB", 2, age_s=3.0)])
    age = bridge._oldest_pending_received_age_s(now=_T0)
    assert age == pytest.approx(42.0, abs=0.01)
    _seed([], [])
    assert bridge._oldest_pending_received_age_s(now=_T0) == 0.0


def test_overrides_file_hot_reload_whitelist_logs_and_ignores_malformed(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / "bridge_overrides.json"
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_COALESCE_PER_SYMBOL", True)
    monkeypatch.setattr(bridge, "IQFEED_PENDING_MAX_EVENTS", 0)
    monkeypatch.setattr(bridge, "_override_state", {"mtime_ns": None, "reloads": 0})

    assert bridge._reload_overrides(str(path)) == 0  # missing file = defaults

    path.write_text(
        json.dumps(
            {
                "IQFEED_TAPE_WRITE_MODE": "execute_values",
                "IQFEED_NOTIFY_COALESCE_PER_SYMBOL": 0,
                "IQFEED_PENDING_MAX_EVENTS": 250_000,
                "DATABASE_URL": "postgresql://nope",
                "IQFEED_TAPE_WRITE_MODE_TYPO": "copy",
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        assert bridge._reload_overrides(str(path)) == 3
    assert bridge.IQFEED_TAPE_WRITE_MODE == "execute_values"
    assert bridge.IQFEED_NOTIFY_COALESCE_PER_SYMBOL is False
    assert bridge.IQFEED_PENDING_MAX_EVENTS == 250_000
    assert "DATABASE_URL" not in bridge._OVERRIDE_COERCERS
    assert (
        sum(
            "not whitelisted" in record.getMessage()
            for record in caplog.records
        )
        == 2
    )
    assert bridge._override_state["reloads"] == 1

    # Unchanged mtime -> no work at all.
    assert bridge._reload_overrides(str(path)) == 0

    # Malformed content keeps the LAST GOOD values.
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert bridge._reload_overrides(str(path)) == 0
    assert bridge.IQFEED_TAPE_WRITE_MODE == "execute_values"

    # A rejected value never lands -- the key is PRESENT, so it keeps its last
    # good value rather than reverting. The two keys that DISAPPEARED from the
    # document do revert to baseline, which is the 2 changes reported here.
    path.write_text(json.dumps({"IQFEED_TAPE_WRITE_MODE": "sqlite"}), encoding="utf-8")
    assert bridge._reload_overrides(str(path)) == 2
    assert bridge.IQFEED_TAPE_WRITE_MODE == "execute_values"
    assert bridge.IQFEED_NOTIFY_COALESCE_PER_SYMBOL is True
    assert bridge.IQFEED_PENDING_MAX_EVENTS == 0


def test_overrides_revert_to_baseline_when_a_key_or_the_file_is_removed(
    tmp_path, monkeypatch
):
    """The kill switch must be TWO-way.

    IQFEED_NOTIFY_MAX_AGE_S is the one knob documented as dangerous (it silences
    the age-gate-less iqfeed_wake_listener). An operator who enables it during a
    backlog incident and then reverts the natural way -- delete the file -- kept
    the quiet-tape wake rail silenced for the life of the process, and there is
    no scheduled bridge restart.
    """

    path = tmp_path / "bridge_overrides.json"
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_MAX_AGE_S", 0.0)
    monkeypatch.setattr(bridge, "IQFEED_NOTIFY_COALESCE_PER_SYMBOL", True)
    monkeypatch.setattr(bridge, "IQFEED_PENDING_MAX_EVENTS", 0)
    monkeypatch.setattr(bridge, "_override_state", {"mtime_ns": None, "reloads": 0})
    monkeypatch.setattr(
        bridge,
        "_OVERRIDE_BASELINE",
        {
            "IQFEED_NOTIFY_MAX_AGE_S": 0.0,
            "IQFEED_NOTIFY_COALESCE_PER_SYMBOL": True,
            "IQFEED_PENDING_MAX_EVENTS": 0,
        },
    )

    path.write_text(
        json.dumps(
            {
                "IQFEED_NOTIFY_MAX_AGE_S": 2.0,
                "IQFEED_NOTIFY_COALESCE_PER_SYMBOL": 0,
            }
        ),
        encoding="utf-8",
    )
    assert bridge._reload_overrides(str(path)) == 2
    assert bridge.IQFEED_NOTIFY_MAX_AGE_S == 2.0
    assert bridge.IQFEED_NOTIFY_COALESCE_PER_SYMBOL is False

    # A key that DISAPPEARS from the document reverts to the baseline.
    path.write_text(
        json.dumps({"IQFEED_NOTIFY_MAX_AGE_S": 2.0}), encoding="utf-8"
    )
    os.utime(path, ns=(0, 1_000_000_000))
    assert bridge._reload_overrides(str(path)) == 1
    assert bridge.IQFEED_NOTIFY_MAX_AGE_S == 2.0
    assert bridge.IQFEED_NOTIFY_COALESCE_PER_SYMBOL is True

    # An emptied document is a FULL revert.
    path.write_text("{}", encoding="utf-8")
    os.utime(path, ns=(0, 2_000_000_000))
    assert bridge._reload_overrides(str(path)) == 1
    assert bridge.IQFEED_NOTIFY_MAX_AGE_S == 0.0

    # ...and so is deleting the file.
    path.write_text(
        json.dumps({"IQFEED_PENDING_MAX_EVENTS": 250_000}), encoding="utf-8"
    )
    os.utime(path, ns=(0, 3_000_000_000))
    assert bridge._reload_overrides(str(path)) == 1
    assert bridge.IQFEED_PENDING_MAX_EVENTS == 250_000
    path.unlink()
    assert bridge._reload_overrides(str(path)) == 1
    assert bridge.IQFEED_PENDING_MAX_EVENTS == 0
    # A file that never existed is still a no-op.
    assert bridge._reload_overrides(str(path)) == 0


def test_hot_write_mode_override_keeps_the_attested_capture_config_truthful(
    tmp_path, monkeypatch
):
    path = tmp_path / "bridge_overrides.json"
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    monkeypatch.setattr(bridge, "_override_state", {"mtime_ns": None, "reloads": 0})
    monkeypatch.setattr(
        bridge, "_OVERRIDE_BASELINE", {"IQFEED_TAPE_WRITE_MODE": "copy"}
    )
    bridge.BRIDGE_CAPTURE_CONFIGURATION["tape_write_mode"] = "copy"
    before_sha = bridge._capture_configuration_sha256()

    path.write_text(
        json.dumps({"IQFEED_TAPE_WRITE_MODE": "execute_values"}), encoding="utf-8"
    )
    assert bridge._reload_overrides(str(path)) == 1
    assert bridge.BRIDGE_CAPTURE_CONFIGURATION["tape_write_mode"] == "execute_values"
    assert bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256 != before_sha
    assert bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256 == (
        bridge._capture_configuration_sha256()
    )

    path.unlink()
    assert bridge._reload_overrides(str(path)) == 1
    assert bridge.BRIDGE_CAPTURE_CONFIGURATION["tape_write_mode"] == "copy"
    assert bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256 == before_sha


def test_drain_metrics_report_insert_split_and_oldest_pending_age(monkeypatch):
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", True)
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    monkeypatch.setattr(bridge, "_write_mode_fallbacks", {"values": 2})
    monkeypatch.setattr(bridge, "_write_mode_commit_in_doubt", 0)
    _seed([_row("AAA", 1, age_s=9.0)], [_row("BBB", 2, age_s=1.0)])
    metrics = bridge._DrainMetrics()
    for batch_ms, execute_ms in ((400.0, 120.0), (1_200.0, 800.0), (500.0, 150.0)):
        metrics.observe(
            events=3_600,
            batch_ms=batch_ms,
            insert={
                "insert_client_build_ms": 20.0,
                "insert_execute_ms": execute_ms,
                "insert_commit_ms": 5.0,
            },
            release_ms=100.0,
            capture_ms=1.0,
            notifies=180,
        )
    report = metrics.report(window_s=10.0)
    assert report["batches"] == 3
    assert report["events_per_s"] == 1080.0
    assert report["batch_p50_ms"] == 500.0
    assert report["batch_max_ms"] == 1200.0
    assert report["insert_execute_p50_ms"] == 150.0
    # SUMS carry a _total_ms name so they cannot be read as per-batch costs.
    assert report["insert_client_build_total_ms"] == 60.0
    assert report["release_total_ms"] == 300.0
    assert report["notify_count"] == 540
    assert report["write_mode"] == "copy"
    # The window DELTA is 0 (all 2 fallbacks predate the window); the lifetime
    # total says 2, and the names distinguish them.
    assert report["write_mode_fallbacks_window"] == 0
    assert report["write_mode_fallbacks_since_start"] == 2
    assert report["commit_in_doubt_window"] == 0
    assert report["failed_batches"] == 0
    assert report["pending_trades"] == 1 and report["pending_quotes"] == 1
    assert report["oldest_pending_received_age_s"] >= 9.0

    # A fallback INSIDE the window shows up as a window delta.
    bridge._write_mode_fallbacks["values"] = 5
    assert metrics.report(window_s=10.0)["write_mode_fallbacks_window"] == 3

    # A batch that exhausted the write chain is the SLOWEST wall in the window
    # and must be inside the percentile the acceptance gate reads.
    metrics.observe(
        events=0,
        batch_ms=9_000.0,
        insert={},
        release_ms=0.0,
        capture_ms=0.0,
        notifies=0,
        failed=True,
    )
    failed_report = metrics.report(window_s=10.0)
    assert failed_report["batches"] == 4
    assert failed_report["failed_batches"] == 1
    assert failed_report["batch_max_ms"] == 9_000.0

    metrics.reset()
    assert metrics.report(window_s=10.0)["batches"] == 0
    assert metrics.report(window_s=10.0)["write_mode_fallbacks_window"] == 0
    _seed([], [])


def test_writer_calls_the_new_seams():
    tree = ast.parse(_BRIDGE_PATH.read_text(encoding="utf-8"))
    writer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "writer"
    )
    called = {
        node.func.id
        for node in ast.walk(writer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "_write_pending_batch",
        "_emit_drain_metrics",
        "_reload_overrides",
        "_enforce_pending_bound",
        "_record_drain_metrics_heartbeat",
        "BatchController",
    } <= called
    # The writer must NOT call the legacy inserter directly any more; the
    # dispatcher owns the fallback chain.
    assert "_insert_pending_batch" not in called


def _exact_print_body() -> dict:
    trade_row = {
        "sym": "AAA",
        "basis": bridge.EXACT_PRINT_TIMESTAMP_BASIS,
        "message_type": "Q",
        "provider_at": _T0 - timedelta(milliseconds=80),
        "received_at": _T0 - timedelta(milliseconds=20),
        "bridge": bridge.BRIDGE_BUILD,
        "bridge_run_id": bridge.BRIDGE_RUN_ID,
        "connection_generation": 1,
        "source_frame_sequence": 7,
        "source_frame_sha256": hashlib.sha256(b"frame-7").hexdigest(),
    }
    body = bridge._canonical_exact_print_heartbeat_body(
        trade_rows=[trade_row], available_at=_T0
    )
    assert body is not None
    return body


def test_exact_print_heartbeat_body_and_sha_unchanged():
    body = _exact_print_body()
    assert set(body) == set(lane_health._IQFEED_EXACT_PRINT_META_KEYS)
    assert body["schema"] == batch_job_constants.IQFEED_EXACT_PRINT_HEARTBEAT_SCHEMA
    assert body["scope"] == batch_job_constants.IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE
    assert body["content_sha256"] == lane_health._heartbeat_content_sha256(body)
    assert (
        bridge.JOB_IQFEED_EXACT_PRINT_HEARTBEAT
        == batch_job_constants.JOB_IQFEED_EXACT_PRINT_HEARTBEAT
    )


class _Row:
    def __init__(self, meta, *, status="ok"):
        self.status = status
        self.started_at = _T0.replace(tzinfo=None) - timedelta(seconds=10)
        self.ended_at = _T0.replace(tzinfo=None)
        self.meta_json = meta


def test_drain_metrics_job_type_has_own_keyset_and_lane_health_parses_it(monkeypatch):
    assert (
        bridge.JOB_IQFEED_DRAIN_METRICS_HEARTBEAT
        == batch_job_constants.JOB_IQFEED_DRAIN_METRICS_HEARTBEAT
        != batch_job_constants.JOB_IQFEED_EXACT_PRINT_HEARTBEAT
    )
    assert (
        lane_health._IQFEED_DRAIN_METRICS_META_KEYS
        != lane_health._IQFEED_EXACT_PRINT_META_KEYS
    )
    monkeypatch.setattr(bridge, "BRIDGE_RUN_ID", str(uuid.uuid4()))
    monkeypatch.setattr(bridge, "_TAPE_SEQUENCES_RESOLVED", True)
    monkeypatch.setattr(bridge, "IQFEED_TAPE_WRITE_MODE", "copy")
    _seed([], [])
    metrics = bridge._DrainMetrics()
    metrics.observe(
        events=1_000,
        batch_ms=450.0,
        insert={
            "insert_client_build_ms": 10.0,
            "insert_execute_ms": 130.0,
            "insert_commit_ms": 4.0,
        },
        release_ms=90.0,
        capture_ms=1.0,
        notifies=140,
    )
    body = bridge._canonical_drain_metrics_body(
        metrics.report(window_s=10.0),
        window_started_at=_T0 - timedelta(seconds=10),
        window_ended_at=_T0,
        connection_generation=1,
    )
    assert body is not None
    assert set(body) == set(lane_health._IQFEED_DRAIN_METRICS_META_KEYS)
    assert body["content_sha256"] == lane_health._heartbeat_content_sha256(body)
    parsed = lane_health._validated_iqfeed_drain_metrics_row(_Row(body))
    assert parsed is not None
    assert parsed["write_mode"] == "copy"
    assert parsed["batch_p90_ms"] == 450.0
    assert parsed["notify_count"] == 140
    # An extra or missing key is refused (the key set is frozen, like the
    # exact-print one).
    extra = dict(body)
    extra["surprise"] = 1
    assert lane_health._validated_iqfeed_drain_metrics_row(_Row(extra)) is None
    missing = dict(body)
    missing.pop("release_total_ms")
    assert lane_health._validated_iqfeed_drain_metrics_row(_Row(missing)) is None
    # The receipt carries per-window DELTAS, so two consecutive rows are
    # diffable; a lifetime total under a bare name was not.
    assert body["write_mode_fallbacks_window"] == 0
    assert body["commit_in_doubt_window"] == 0
    assert body["failed_batches"] == 0
    # ...and a v1 body (the pre-rename key set) is refused outright.
    legacy = dict(body)
    legacy["release_ms"] = legacy.pop("release_total_ms")
    assert lane_health._validated_iqfeed_drain_metrics_row(_Row(legacy)) is None
    # And the exact-print parser must NOT accept it.
    assert lane_health._validated_exact_iqfeed_print_row(_Row(body)) is None


def test_launcher_idempotency_documented():
    launcher = (
        _REPO / "project_ws" / "AgentOps" / "iqfeed"
        / "start-iqfeed-trade-bridge-main.ps1"
    )
    text = launcher.read_text(encoding="utf-8", errors="replace")
    # There is NO scheduled restart: the Daily task exits 0 whenever either the
    # python OR the cmd supervisor already exists. New code loads only on an
    # operator whole-chain restart.
    assert "iqfeed_trade_bridge.py" in text
    assert "run-trade-bridge.cmd" in text
    assert "if ($existing.Count -gt 0) { exit 0 }" in text
