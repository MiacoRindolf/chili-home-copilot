"""Pure fail-closed tests for the diagnostic golden replay scorecard."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "replay_scorecard", os.path.join(_ROOT, "scripts", "replay_scorecard.py")
)
rs = importlib.util.module_from_spec(_spec)
sys.modules["replay_scorecard"] = rs
_spec.loader.exec_module(rs)


def _result(**overrides):
    doc = {
        "schema": "chili.golden_replay_window_result.v2",
        "key": "AAA|2026-07-07|both",
        "symbol": "AAA",
        "day": "2026-07-07",
        "status": "ok",
        "pnl": 0.0,
        "fills": [],
    }
    doc.update(overrides)
    return doc


def _bound_docs():
    producer_sha256 = {
        field: rs.file_sha256(path)
        for field, path in rs.PRODUCER_PATHS.items()
    }
    receipt = {
        "schema": "chili.golden-window-content-receipt.v2",
        "query_contract_sha256": rs.query_contract_sha256(),
        "symbol": "AAA",
        "start": "2026-07-07T12:00:00",
        "end": "2026-07-07T14:00:00",
        "ticks": {"bytes": 10, "sha256": "1" * 64},
        "nbbo": {"bytes": 10, "sha256": "2" * 64},
    }
    receipt_sha = rs.content_receipt_sha256(receipt)
    window = {
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
        "source_content_receipt_sha256": receipt_sha,
        "source_content_status": "CONTENT_HASHED",
        "coverage_status": "DIAGNOSTIC_ONLY",
    }
    source_identity = {
        "host": "loopback",
        "port": 5433,
        "dbname": "chili",
        "user": "u",
    }
    manifest = {
        "schema": "chili.replay-window-manifest.v2",
        "evidence_grade": "DIAGNOSTIC_ONLY",
        "causal_use_allowed": False,
        "ross_grade_credit_allowed": False,
        "source_backend_sealed": False,
        "child_source_snapshot_pinned": False,
        "build_sha": "b" * 40,
        "generator_sha256": producer_sha256["generator_sha256"],
        "receipt_helper_sha256":
            producer_sha256["receipt_helper_sha256"],
        "query_contract_sha256": rs.query_contract_sha256(),
        "source_database_name": "chili",
        "source_database_identity": source_identity,
        "windows": [window],
    }
    key = "AAA|2026-07-07|both"
    receipts = {key: receipt_sha}
    roster = {"keys": [key], "receipts": receipts}
    meta = {
        "schema": "chili.golden_replay_run_meta.v2",
        "build_sha": "b" * 40,
        "driver_sha256": producer_sha256["driver_sha256"],
        "batch_sha256": producer_sha256["batch_sha256"],
        "generator_sha256": producer_sha256["generator_sha256"],
        "receipt_helper_sha256":
            producer_sha256["receipt_helper_sha256"],
        "manifest_sha256": "7" * 64,
        "arm": "both",
        "evidence_grade": "DIAGNOSTIC_ONLY",
        "causal_use_allowed": False,
        "ross_grade_credit_allowed": False,
        "paper_policy_parity": False,
        "operational_safeguards_neutralized": True,
        "fees_slippage_complete": False,
        "source_backend_sealed": False,
        "child_source_snapshot_pinned": False,
        "dependency_environment_sealed": False,
        "tracked_source_tree_clean": True,
        "source_receipt_semantics":
            "PRE_POST_BOUNDARY_OBSERVATION_NOT_CHILD_ATTESTATION",
        "source_database_name": "chili",
        "source_database_identity": source_identity,
        "sink_database_name": "chili_replay_test",
        "sink_database_identity": {
            **source_identity,
            "dbname": "chili_replay_test",
        },
        "selected_window_keys": [key],
        "selected_window_receipts": receipts,
        "selected_window_set_sha256": rs._canonical_sha256(roster),
        "selection_scope": "baseline_tier_complete",
        "selected_tiers": ["baseline"],
        "equity": 100_000.0,
        "risk_fraction": 0.01,
        "execution_family": "alpaca_spot",
        "started_at": "2026-07-26T12:00:00",
        "stop_at": "2026-07-26T18:00:00",
    }
    run_payload = {
        k: v
        for k, v in meta.items()
        if k not in {"started_at", "stop_at"}
    }
    meta["run_identity_sha256"] = rs._canonical_sha256(run_payload)
    result = {
        **_result(),
        **{
            field: window[field]
            for field in (
                "class",
                "tier",
                "win_start",
                "win_end",
                "ohlcv_start",
                "window_source",
                "prepend",
            )
        },
        "arm": "both",
        "build_sha": meta["build_sha"],
        "driver_sha256": meta["driver_sha256"],
        "batch_sha256": meta["batch_sha256"],
        "generator_sha256": meta["generator_sha256"],
        "receipt_helper_sha256": meta["receipt_helper_sha256"],
        "manifest_sha256": meta["manifest_sha256"],
        "run_identity_sha256": meta["run_identity_sha256"],
        "source_content_receipt_sha256": receipt_sha,
        "source_content_receipt_pre_sha256": receipt_sha,
        "source_content_receipt_post_sha256": receipt_sha,
        "evidence_grade": "DIAGNOSTIC_ONLY",
        "causal_use_allowed": False,
        "ross_grade_credit_allowed": False,
        "paper_policy_parity": False,
        "fees_slippage_complete": False,
        "sink": {},
        "market": {"win_ticks": 1, "first_px": 5.0, "hi": 5.0},
    }
    return manifest, meta, result


def test_fifo_pairing_basic():
    fills = [
        {"side": "buy", "qty": 100, "px": 5.00},
        {"side": "buy", "qty": 100, "px": 6.00},
        {"side": "sell", "qty": 150, "px": 7.00},
        {"side": "sell", "qty": 50, "px": 6.50},
    ]
    trades = rs.pair_round_trips(fills)
    assert len(trades) == 1
    assert trades[0]["qty"] == 200
    assert trades[0]["entry_px"] == 5.5
    assert trades[0]["exit_px"] == 6.875
    assert trades[0]["pnl_usd"] == 275.0


def test_fifo_pairing_rejects_unmatched_open_or_invalid_fills():
    with pytest.raises(ValueError, match="sells more"):
        rs.pair_round_trips([{"side": "sell", "qty": 1, "px": 5}])
    with pytest.raises(ValueError, match="open quantity"):
        rs.pair_round_trips([{"side": "buy", "qty": 1, "px": 5}])
    with pytest.raises(ValueError, match="finite positive"):
        rs.pair_round_trips([{"side": "buy", "qty": True, "px": 5}])


def test_window_capture_is_explicitly_descriptive():
    rec = {
        "pnl": 250.0,
        "market": {"first_px": 5.0, "hi": 7.5},
        "pnl_reconciliation": {
            "status": "MATCHED",
            "fill_pnl_usd": 250.0,
        },
    }
    trades = [{"qty": 100, "entry_px": 5.0, "exit_px": 7.5, "pnl_usd": 250.0}]
    assert rs.window_capture(rec, trades)["window_capture_ratio"] == 1.0
    assert rs.window_capture(rec, [])["window_capture_ratio"] is None


def test_unreconciled_reported_pnl_never_becomes_headline():
    rec = {"pnl": 999_999.0}
    rs.reconcile_result_pnl(rec, [])
    assert rec["pnl"] is None
    assert rec["replay_reported_pnl_usd"] == 999_999.0
    assert rec["pnl_reconciliation"]["status"] == "UNAVAILABLE"


def test_diagnostic_stats_have_no_pseudo_r_or_promotion_gate():
    stats = rs.diagnostic_stats([300.0, 150.0, -100.0, -50.0])
    assert stats["n"] == 4
    assert stats["profit_factor"] == 3.0
    assert stats["average_loss_abs"] == 75.0
    assert "r_unit" not in stats
    assert "avg_loser_r" not in stats
    assert not hasattr(rs, "stage0_gates")


def test_load_results_rejects_malformed_nonfinite_and_divergent_duplicate(tmp_path):
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text("{bad}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        rs.load_results([str(malformed)])

    nonfinite = tmp_path / "nonfinite.jsonl"
    nonfinite.write_text(
        json.dumps(_result()).replace('"status": "ok"', '"pnl": NaN, "status": "ok"')
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-finite"):
        rs.load_results([str(nonfinite)])

    overflow = tmp_path / "overflow.jsonl"
    overflow.write_text(
        json.dumps(_result()).replace('"pnl": 0.0', '"pnl": 1e999')
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-finite"):
        rs.load_results([str(overflow)])

    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(
        json.dumps(_result(pnl=1.0)) + "\n" + json.dumps(_result(pnl=2.0)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="divergent duplicate"):
        rs.load_results([str(duplicate)])


def test_load_results_rejects_byte_equivalent_duplicate(tmp_path):
    row = json.dumps(_result(pnl=1.0))
    path = tmp_path / "same.jsonl"
    path.write_text(row + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate result key"):
        rs.load_results([str(path)])


def test_strict_json_rejects_duplicate_object_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate object key"):
        rs.load_strict_json(str(path))


def test_source_url_must_be_explicit_loopback():
    _, name = rs.guard_source_database_url(
        "postgresql://user:secret@127.0.0.1:5433/sealed_archive"
    )
    assert name == "sealed_archive"
    with pytest.raises(ValueError, match="loopback"):
        rs.guard_source_database_url("postgresql://user:secret@example.com/chili")


def test_invalid_fill_does_not_fall_back_to_quote_derived_fill(monkeypatch):
    monkeypatch.setattr(rs, "nbbo_path", lambda *a, **k: [object()])

    def reject(*args, **kwargs):
        raise ValueError("impossible supplied fill")

    monkeypatch.setattr(rs, "_evaluate_long_trade_path", reject)
    trade = {
        "entry_ts": "2026-07-07T13:00:00Z",
        "exit_ts": "2026-07-07T13:01:00Z",
        "entry_px": 5.0,
        "exit_px": 6.0,
        "qty": 100,
    }
    assert rs.within_trade_metrics(object(), {"symbol": "AAA"}, trade) is None


def test_hermetic_label_a_path_accepts_valid_local_quote_points(monkeypatch):
    t0 = datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 7, 13, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        rs,
        "nbbo_path",
        lambda *a, **k: [
            rs._QuotePoint(t0, bid=5.0, ask=5.1),
            rs._QuotePoint(t1, bid=6.0, ask=6.1),
        ],
    )
    metrics = rs.within_trade_metrics(
        object(),
        {"symbol": "AAA"},
        {
            "entry_ts": t0.isoformat(),
            "exit_ts": t1.isoformat(),
            "entry_px": 5.1,
            "exit_px": 6.0,
            "qty": 100,
        },
    )
    assert metrics is not None
    assert metrics["capture_ratio"] == 1.0


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def execute(self, sql, params):
        self.executed = (sql, params)

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(rows)

    def cursor(self):
        return self.cursor_obj


def test_nbbo_path_never_stride_downsamples():
    t0 = datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc)
    rows = [(t0, 5.0, 5.1) for _ in range(6)]
    assert rs.nbbo_path(_Conn(rows), "AAA", t0, t0, max_points=5) is None
    exact = rs.nbbo_path(_Conn(rows[:5]), "AAA", t0, t0, max_points=5)
    assert len(exact) == 5


def test_render_states_zero_ross_credit_and_no_green_gate():
    sc = {
        "generated_at": "2026-07-26T12:00:00Z",
        "meta": {
            "build_sha": "abc123def456",
            "arm": "both",
            "sink_database_name": "chili_replay_test",
            "equity_exec": "explicit",
        },
        "coverage": {
            "attempted": 1,
            "ok": 1,
            "failed": 0,
            "selected": 1,
            "remaining_selected": 0,
            "library": 1,
        },
        "windows": [{
            "symbol": "AAA",
            "day": "2026-07-07",
            "win_start": "2026-07-07T13:00:00",
            "win_end": "2026-07-07T15:00:00",
            "class": "retained_archive",
            "pnl": 12.34,
            "entries": 1,
            "exits": 1,
            "market": {"up_pct": 42.0},
            "window_capture": {"entered": True, "window_capture_ratio": 0.12},
            "sink": {},
        }],
        "errors": [],
        "per_setup": {},
        "expectancy": rs.diagnostic_stats([12.34]),
        "label_a": {
            "n": 0,
            "mean_capture": 0.0,
            "mean_giveback": 0.0,
            "outcome_classes": {},
        },
    }
    md = rs.render_markdown(sc)
    assert "DIAGNOSTIC_ONLY" in md
    assert "zero Ross credit" in md
    assert "GREEN" not in md
    assert "Ross-parity evidence" in md


def test_run_binding_rejects_forged_key_config_and_receipt():
    manifest, meta, result = _bound_docs()
    selected = rs.validate_run_bindings(meta, manifest, [result])
    assert set(selected) == {result["key"]}

    forged = dict(result)
    forged["key"] = "FORGED-UNBOUND"
    forged["symbol"] = "FAKE"
    with pytest.raises(ValueError, match="provenance mismatch"):
        rs.validate_run_bindings(meta, manifest, [forged])

    drifted_meta = dict(meta)
    drifted_meta["risk_fraction"] = 0.02
    with pytest.raises(ValueError, match="run_identity"):
        rs.validate_run_bindings(drifted_meta, manifest, [result])

    drifted_result = dict(result)
    drifted_result["source_content_receipt_post_sha256"] = "9" * 64
    with pytest.raises(ValueError, match="final proof"):
        rs.validate_run_bindings(meta, manifest, [drifted_result])

    assert rs.verified_producer_file_sha256(meta)[
        "driver_sha256"
    ] == meta["driver_sha256"]
    forged_producer = dict(meta)
    forged_producer["driver_sha256"] = "9" * 64
    with pytest.raises(ValueError, match="producer bytes"):
        rs.verified_producer_file_sha256(forged_producer)


def test_fill_time_binding_rejects_positional_or_ornamental_identity():
    rec = {
        "sink": {
            "fill_events": [
                {
                    "event_type": "live_entry_filled",
                    "fill_identity": None,
                    "ts": "2026-07-07T13:00:00Z",
                    "qty": 10,
                    "px": 5,
                },
                {
                    "event_type": "live_exit_fill",
                    "fill_identity": None,
                    "ts": "2026-07-07T13:01:00Z",
                    "qty": 10,
                    "px": 6,
                },
            ]
        }
    }
    trades = [{"qty": 10, "entry_px": 5, "exit_px": 6}]
    rs.attach_fill_times(rec, trades)
    assert trades[0]["entry_ts"] is None

    rec["sink"]["fill_events"][0]["fill_identity"] = "entry-1"
    rec["sink"]["fill_events"][1]["fill_identity"] = "exit-1"
    rs.attach_fill_times(rec, trades)
    assert trades[0]["entry_ts"] is None
    assert trades[0]["label_a_unavailable_reason"] == (
        "immutable_entry_exit_cycle_lineage_unavailable"
    )

    identical_cycles = [
        {"qty": 10, "entry_px": 5, "exit_px": 6},
        {"qty": 10, "entry_px": 5, "exit_px": 6},
    ]
    rec["sink"]["fill_events"] = list(
        reversed(rec["sink"]["fill_events"] * 2)
    )
    rs.attach_fill_times(rec, identical_cycles)
    assert all(trade["entry_ts"] is None for trade in identical_cycles)


def test_main_renders_before_atomic_outputs(monkeypatch, tmp_path):
    manifest, meta, result = _bound_docs()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, separators=(",", ":")),
        encoding="utf-8",
    )
    meta["manifest_sha256"] = rs.hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    run_payload = {
        k: v
        for k, v in meta.items()
        if k not in {"run_identity_sha256", "started_at", "stop_at"}
    }
    meta["run_identity_sha256"] = rs._canonical_sha256(run_payload)
    result["manifest_sha256"] = meta["manifest_sha256"]
    result["run_identity_sha256"] = meta["run_identity_sha256"]
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    results_path = tmp_path / "results.jsonl"
    results_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
    out_json = tmp_path / "scorecard.json"
    out_md = tmp_path / "scorecard.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_scorecard.py",
            "--results",
            str(results_path),
            "--meta",
            str(meta_path),
            "--library-manifest",
            str(manifest_path),
            "--out-json",
            str(out_json),
            "--out-md",
            str(out_md),
        ],
    )
    monkeypatch.setattr(rs, "clean_build_sha", lambda: meta["build_sha"])
    original_render = rs.render_markdown
    monkeypatch.setattr(
        rs,
        "render_markdown",
        lambda _scorecard: (_ for _ in ()).throw(RuntimeError("render red")),
    )
    with pytest.raises(RuntimeError, match="render red"):
        rs.main()
    assert not out_json.exists()
    assert not out_md.exists()
    monkeypatch.setattr(rs, "render_markdown", original_render)
    assert rs.main() == 0
    scorecard = json.loads(out_json.read_text(encoding="utf-8"))
    markdown_bytes = out_md.read_bytes()
    assert scorecard["output_binding"]["json_is_terminal_authority"] is True
    assert scorecard["output_binding"]["markdown_sha256"] == (
        rs.hashlib.sha256(markdown_bytes).hexdigest()
    )
