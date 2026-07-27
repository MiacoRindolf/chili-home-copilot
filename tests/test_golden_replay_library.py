"""Pure safety tests for the diagnostic golden replay library tooling."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


batch = _load("replay_benchmark_batch", "scripts/replay_benchmark_batch.py")
derive = _load("derive_replay_windows", "scripts/derive_replay_windows.py")
db_guard = sys.modules["diagnostic_replay_db"]


def _manifest():
    receipt = {
        "schema": "chili.golden-window-content-receipt.v2",
        "query_contract_sha256": db_guard.query_contract_sha256(),
        "symbol": "AAA",
        "start": "2026-07-07T12:00:00",
        "end": "2026-07-07T14:00:00",
        "ticks": {"bytes": 10, "sha256": "1" * 64},
        "nbbo": {"bytes": 10, "sha256": "2" * 64},
    }
    return {
        "schema": "chili.replay-window-manifest.v2",
        "evidence_grade": "DIAGNOSTIC_ONLY",
        "causal_use_allowed": False,
        "ross_grade_credit_allowed": False,
        "source_backend_sealed": False,
        "child_source_snapshot_pinned": False,
        "build_sha": "b" * 40,
        "generator_sha256": "3" * 64,
        "receipt_helper_sha256": "4" * 64,
        "query_contract_sha256": db_guard.query_contract_sha256(),
        "source_database_name": "chili",
        "source_database_identity": {
            "host": "loopback",
            "port": 5433,
            "dbname": "chili",
            "user": "u",
        },
        "windows": [{
            "symbol": "AAA",
            "day": "2026-07-07",
            "tier": "baseline",
            "class": "retained_archive",
            "win_start": "2026-07-07T13:00:00",
            "win_end": "2026-07-07T14:00:00",
            "ohlcv_start": "2026-07-07T12:00:00",
            "window_source": "derived",
            "prepend": False,
            "source_content_receipt": receipt,
            "source_content_receipt_sha256":
                db_guard.content_receipt_sha256(receipt),
            "source_content_status": "CONTENT_HASHED",
            "coverage_status": "DIAGNOSTIC_ONLY",
        }],
    }


def test_batch_defaults_to_intended_default_on_arm():
    assert batch.DEFAULT_ARM == "both"


def test_sink_fill_normalization_uses_canonical_fsm_event_payloads():
    assert batch.SINK_FILL_EVENT_TYPES[:2] == (
        "live_entry_filled",
        "live_exit_filled",
    )
    entry = batch.normalize_sink_fill_event(
        "2026-07-27T13:31:00+00:00",
        "live_entry_filled",
        {
            "order_id": "entry-order-1",
            "avg": 5.25,
            "filled_size": 120,
        },
    )
    assert (entry["qty"], entry["px"], entry["fill_identity"]) == (
        120.0, 5.25, "entry-order-1",
    )
    assert entry["provider_or_broker_fill_at"] is None
    assert entry["coverage_status"] == "COVERAGE_UNAVAILABLE"

    exit_fill = batch.normalize_sink_fill_event(
        "2026-07-27T13:34:00+00:00",
        "live_exit_filled",
        {
            "reason": "failed_bid",
            "fill_price": 5.70,
            "sell_result": {
                "filled_size": 120,
                "order_id": "exit-order-1",
            },
        },
    )
    assert exit_fill["px"] == 5.70
    assert exit_fill["exit_reason"] == "failed_bid"
    assert exit_fill["qty"] is None
    assert exit_fill["fill_identity"] is None
    assert exit_fill["coverage_reason"] == (
        "canonical_exit_quantity_identity_fill_clock_unavailable"
    )


def test_sink_fill_normalization_retains_only_self_contained_exit_evidence():
    orphan = batch.normalize_sink_fill_event(
        "2026-07-27T13:34:01+00:00",
        "live_exit_filled",
        {
            "reason": "alpaca_orphan_reconcile",
            "fill_price": 5.70,
            "quantity": 120,
            "order_id": "exit-order-2",
            "client_order_id": "exit-client-2",
            "source_event_id": 42,
            "entry_filled_at_utc": "2026-07-27T13:31:00Z",
            "filled_at_utc": "2026-07-27T13:34:00Z",
        },
    )
    assert orphan["qty"] == 120.0
    assert orphan["px"] == 5.70
    assert orphan["fill_identity"] == "exit-order-2"
    assert orphan["provider_or_broker_fill_at"] == "2026-07-27T13:34:00Z"
    assert orphan["coverage_status"] == "COVERAGE_UNAVAILABLE"
    assert orphan["coverage_reason"] == (
        "immutable_entry_exit_cycle_lineage_unavailable"
    )
    legacy = batch.normalize_sink_fill_event(
        "2026-07-27T13:34:01+00:00",
        "live_exit_fill",
        {
            "fill_price": 5.70,
            "quantity": 120,
            "order_id": "legacy-exit",
            "filled_at_utc": "2026-07-27T13:34:00Z",
        },
    )
    assert legacy["coverage_reason"].startswith(
        "legacy_exit_alias_diagnostic_only:"
    )

    contradictory = batch.normalize_sink_fill_event(
        "2026-07-27T13:34:01+00:00",
        "live_exit_filled",
        {
            "quantity": -1,
            "filled_size": 120,
            "fill_price": 5.70,
            "order_id": "exit-order-3",
            "source_event_id": 43,
            "entry_filled_at_utc": "2026-07-27T13:31:00Z",
            "filled_at_utc": "2026-07-27T13:34:00Z",
        },
    )
    assert contradictory["qty"] is None
    assert contradictory["provider_or_broker_fill_at"] is None
    inverted = batch.normalize_sink_fill_event(
        "2026-07-27T13:34:01+00:00",
        "live_exit_filled",
        {
            "quantity": 1,
            "fill_price": 5.70,
            "order_id": "exit-order-4",
            "source_event_id": 44,
            "entry_filled_at_utc": "2026-07-27T13:35:00Z",
            "filled_at_utc": "2026-07-27T13:34:00Z",
        },
    )
    assert inverted["provider_or_broker_fill_at"] is None


@pytest.mark.parametrize(
    ("quantity", "price"),
    [
        (True, 5.25),
        (120, False),
        (0, 5.25),
        (120, 0),
        (-1, 5.25),
        (120, float("nan")),
        (float("inf"), 5.25),
    ],
)
def test_sink_fill_normalization_rejects_invalid_economics(quantity, price):
    event = batch.normalize_sink_fill_event(
        "2026-07-27T13:31:00+00:00",
        "live_entry_filled",
        {
            "order_id": "entry-order-1",
            "avg": price,
            "filled_size": quantity,
        },
    )
    assert event["coverage_status"] == "COVERAGE_UNAVAILABLE"
    assert event["coverage_reason"] != (
        "immutable_entry_exit_cycle_lineage_unavailable"
    )


def test_sink_fill_normalization_rejects_malformed_payload_and_clock():
    malformed = batch.normalize_sink_fill_event(
        "2026-07-27T13:31:00+00:00",
        "live_exit_filled",
        None,
    )
    assert malformed["qty"] is None
    assert malformed["px"] is None
    assert malformed["fill_identity"] is None
    assert malformed["provider_or_broker_fill_at"] is None
    naive_clock = batch.normalize_sink_fill_event(
        "2026-07-27T13:31:00+00:00",
        "live_exit_filled",
        {
            "quantity": 1,
            "fill_price": 5,
            "order_id": "exit-order",
            "filled_at_utc": "2026-07-27T13:31:00",
        },
    )
    assert naive_clock["provider_or_broker_fill_at"] is None
    with pytest.raises(ValueError, match="unsupported sink fill event"):
        batch.normalize_sink_fill_event(
            "2026-07-27T13:31:00+00:00",
            "paper_exit_filled",
            {},
        )


def test_database_guards_normalize_identity_and_reject_remote_or_live_sink():
    _, source_name, source_id = batch.guard_postgres_url(
        "postgresql://u:p@localhost:5433/chili", role="source"
    )
    _, _, equivalent = batch.guard_postgres_url(
        "postgresql://u:other@127.0.0.1:5433/chili", role="source"
    )
    assert source_name == "chili"
    assert source_id.server_key == equivalent.server_key
    with pytest.raises(SystemExit, match="loopback"):
        batch.guard_postgres_url(
            "postgresql://u:p@example.com/chili_replay_test", role="sink"
        )
    with pytest.raises(SystemExit, match="must end in _test"):
        batch.guard_postgres_url(
            "postgresql://u:p@localhost/chili", role="sink"
        )


def test_isolated_child_environment_excludes_credentials_and_proxies(monkeypatch):
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("ALPACA_API_SECRET", "do-not-inherit")
    monkeypatch.setenv("IQFEED_PASSWORD", "do-not-inherit")
    monkeypatch.setenv("HTTPS_PROXY", "http://network-proxy")
    env = batch.isolated_child_env()
    assert env["PATH"] == "safe-path"
    assert "ALPACA_API_SECRET" not in env
    assert "IQFEED_PASSWORD" not in env
    assert "HTTPS_PROXY" not in env


def test_manifest_requires_diagnostic_contract_and_rejects_duplicates(tmp_path):
    path = tmp_path / "manifest.json"
    raw = json.dumps(_manifest()).encode()
    path.write_bytes(raw)
    doc, digest = batch.load_diagnostic_manifest(str(path))
    assert doc["evidence_grade"] == "DIAGNOSTIC_ONLY"
    assert digest == hashlib.sha256(raw).hexdigest()

    forged = _manifest()
    forged["causal_use_allowed"] = True
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(SystemExit, match="diagnostic-only"):
        batch.load_diagnostic_manifest(str(path))

    path.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate object key"):
        batch.load_diagnostic_manifest(str(path))


def test_prepend_cache_receipt_is_content_bound_and_missing_fails(tmp_path):
    window = {
        "symbol": "AAA",
        "day": "2026-07-07",
        "prepend": True,
    }
    with pytest.raises(FileNotFoundError, match="unavailable"):
        batch.cache_receipt(str(tmp_path), window)
    cache = tmp_path / "AAA_2026-07-07_1m.csv"
    cache.write_bytes(b"timestamp,open\n")
    receipt = batch.cache_receipt(str(tmp_path), window)
    assert receipt["sha256"] == hashlib.sha256(cache.read_bytes()).hexdigest()
    assert batch.cache_receipt(str(tmp_path), {**window, "prepend": False}) is None


def test_wrong_sink_confirmation_stops_before_database_or_filesystem(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(batch, "source_window_snapshot", lambda *a: calls.append(a))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_benchmark_batch.py",
            "--manifest",
            str(tmp_path / "missing.json"),
            "--out-dir",
            str(tmp_path / "out"),
            "--source-database-url",
            "postgresql://u:p@localhost/chili",
            "--sink-database-url",
            "postgresql://u:p@localhost/chili_replay_test",
            "--confirm-test-sink-reset",
            "WRONG",
            "--ohlcv-cache-dir",
            str(tmp_path),
            "--equity",
            "100000",
            "--risk-fraction",
            "0.01",
            "--exec-family",
            "alpaca_spot",
            "--stop-at",
            "2026-07-27T03:00:00",
        ],
    )
    with pytest.raises(SystemExit, match="exact confirmation"):
        batch.main()
    assert calls == []
    assert not (tmp_path / "out").exists()


def test_database_guard_rejects_query_overrides_and_all_pg_environment(
    monkeypatch,
):
    with pytest.raises(SystemExit, match="canonical"):
        batch.guard_postgres_url(
            "postgresql://u:p@localhost:5433/chili?dbname=other",
            role="source",
        )
    with pytest.raises(SystemExit, match="canonical"):
        batch.guard_postgres_url(
            "postgresql://u:p@localhost:5433/chili?options=-csearch_path%3Devil",
            role="source",
        )
    monkeypatch.setenv("PGCLIENTENCODING", "LATIN1")
    with pytest.raises(SystemExit, match=r"PG\*"):
        batch.guard_postgres_url(
            "postgresql://u:p@localhost:5433/chili",
            role="source",
        )


def test_connected_endpoint_uses_client_mapping_not_server_internal_address():
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, _query):
            return None

        def fetchone(self):
            return ("chili", "u", "public", "public")

    class Connection:
        def get_dsn_parameters(self):
            return {"host": "127.0.0.1", "port": "5433", "user": "u"}

        def cursor(self):
            return Cursor()

    expected = db_guard.DatabaseIdentity("loopback", 5433, "chili", "u")
    db_guard.verify_connected_endpoint(Connection(), expected)


def test_content_receipt_is_query_contract_and_content_bound():
    receipt = _manifest()["windows"][0]["source_content_receipt"]
    digest = db_guard.content_receipt_sha256(receipt)
    assert receipt["query_contract_sha256"] == db_guard.query_contract_sha256()
    assert digest == _manifest()["windows"][0]["source_content_receipt_sha256"]
    tampered = json.loads(json.dumps(receipt))
    tampered["nbbo"]["bytes"] += 1
    assert db_guard.content_receipt_sha256(tampered) != digest
    assert "bid > 0" not in db_guard._NBBO_QUERY


def test_confined_paths_reject_traversal(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        batch.confined_child_path(str(tmp_path), "../outside.log")


def test_driver_rejects_secret_before_app_import_or_database_connect():
    env = batch.isolated_child_env()
    env.update(
        {
            "REPLAY_SOURCE_DATABASE_URL":
                "postgresql://u:p@127.0.0.1:5433/chili",
            "TEST_DATABASE_URL":
                "postgresql://u:p@127.0.0.1:5433/chili_replay_test",
            "GOLDEN": "1",
            "CHILI_CAPTURED_PAPER_CONFIG_ISOLATED": "true",
            "CHILI_DIAGNOSTIC_REPLAY_ISOLATED": "true",
            "CHILI_REPLAY_TEST_SINK_CONFIRMATION":
                "RESET_DISPOSABLE_REPLAY_TEST_SINK",
            "CHILI_ALPACA_LIVE_API_SECRET": "forbidden",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / "replay_ab_dark_flags.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode != 0
    assert "credentials are forbidden" in (proc.stdout + proc.stderr)
    assert "connection refused" not in (proc.stdout + proc.stderr).lower()


def test_driver_requires_exact_clean_build_authority_before_app_import():
    env = batch.isolated_child_env()
    env.update(
        {
            "REPLAY_SOURCE_DATABASE_URL":
                "postgresql://u:p@127.0.0.1:5433/chili",
            "TEST_DATABASE_URL":
                "postgresql://u:p@127.0.0.1:5433/chili_replay_test",
            "GOLDEN": "1",
            "CHILI_CAPTURED_PAPER_CONFIG_ISOLATED": "true",
            "CHILI_DIAGNOSTIC_REPLAY_ISOLATED": "true",
            "CHILI_REPLAY_TEST_SINK_CONFIRMATION":
                "RESET_DISPOSABLE_REPLAY_TEST_SINK",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / "replay_ab_dark_flags.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode != 0
    assert "build/driver authority is required" in (
        proc.stdout + proc.stderr
    )
    assert "connection refused" not in (proc.stdout + proc.stderr).lower()


def test_derive_window_is_deterministic_and_manifest_source_guard_is_local():
    t0 = datetime(2026, 7, 7, 13, 0)
    points = [(t0 + timedelta(minutes=i), 100 if 10 <= i <= 20 else 1)
              for i in range(40)]
    one = derive.derive_window(points, t0 + timedelta(minutes=15))
    two = derive.derive_window(points, t0 + timedelta(minutes=15))
    assert one == two
    _, name = derive.guard_source_database_url(
        "postgresql://u:p@127.0.0.1:5433/sealed_archive"
    )
    assert name == "sealed_archive"
    with pytest.raises(ValueError, match="loopback"):
        derive.guard_source_database_url(
            "postgresql://u:p@example.com/sealed_archive"
        )


def test_unsafe_harvest_and_network_fetch_tools_are_not_shipped():
    assert not (ROOT / "scripts" / "harvest_golden_windows.py").exists()
    assert not (ROOT / "scripts" / "fetch_ross_recap.py").exists()
    assert not (ROOT / "scripts" / "data" / "golden_harvest_inventory.json").exists()
    driver = (ROOT / "scripts" / "replay_ab_dark_flags.py").read_text(encoding="utf-8")
    assert "import yfinance" not in driver
    assert "continuing tick-only" not in driver
    assert "CHILI_REPLAY_PREPEND_CACHE_SHA256 is required" in driver
