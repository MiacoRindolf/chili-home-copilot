"""Diagnostic golden-library scorecard for sealed replay-batch results.

Consumes ``results.jsonl`` + ``meta.json`` from ``scripts/replay_benchmark_batch.py``
(+ optionally an explicit read-only golden-table source URL) and emits a
machine-readable scorecard plus a markdown report. This tool is diagnostic only:
it cannot establish OOS improvement, live profitability, or Ross parity.

TWO capture denominators, NEVER mixed (both labeled in the report):
  * Label A — within-trade MFE capture contract: realized vs peak executable bid
    between entry and exit (bids only, no post-exit lookahead). Current replay
    stdout lacks immutable entry↔exit cycle lineage, so generated batch results
    fail this label closed as unavailable.
  * Label B — window capture: (pnl_usd / deployed_notional) / window_move_frac,
    where window_move_frac = (window_hi - first_px) / first_px. Sizing-independent
    conversion of the window's first->high move; 1.0 = full conversion.
Replay fills and post-session windows are not broker-executable certification.
Every dollar section is for same-input diagnostic deltas only.

    python scripts/replay_scorecard.py \
        --results D:/CHILI-Docker/chili-data/replay_batch_policy_v3/results.jsonl \
        --meta    D:/CHILI-Docker/chili-data/replay_batch_policy_v3/meta.json \
        --library-manifest D:/CHILI-Docker/chili-data/replay_batch/window_manifest.json \
        --source-database-url postgresql://.../sealed_golden_archive \
        --out-json D:/CHILI-Docker/chili-data/replay_batch_policy_v3/scorecard.json \
        --out-md   D:/CHILI-Docker/chili-data/replay_batch_policy_v3/scorecard.md

READ-ONLY everywhere (golden tables only when the explicit source is given). Never assumes the
batch completed — reports attempted vs library size.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

BUILD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BUILD)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

ROSS_BENCHMARK_PATH = os.path.join(
    BUILD,
    "app",
    "services",
    "trading",
    "momentum_neural",
    "ross_replay_benchmark.py",
)
POST_EXIT_PATH = os.path.join(
    BUILD,
    "app",
    "services",
    "trading",
    "momentum_neural",
    "post_exit_excursion.py",
)
PRODUCER_PATHS = {
    "driver_sha256": os.path.join(
        BUILD, "scripts", "replay_ab_dark_flags.py"
    ),
    "batch_sha256": os.path.join(
        BUILD, "scripts", "replay_benchmark_batch.py"
    ),
    "generator_sha256": os.path.join(
        BUILD, "scripts", "derive_replay_windows.py"
    ),
    "receipt_helper_sha256": os.path.join(
        BUILD, "scripts", "diagnostic_replay_db.py"
    ),
}
_PINNED_ANALYZER_SHA256: dict[str, str] = {}

from diagnostic_replay_db import (  # noqa: E402
    content_receipt_sha256,
    golden_window_content_receipt,
    guarded_database_identity,
    query_contract_sha256,
    verify_connected_endpoint,
)
from replay_benchmark_batch import (  # noqa: E402
    STRATEGY_ARM_CHOICES,
    canonical_result_log_name,
    execution_scope_sha256,
    parse_driver_stdout,
    replay_execution_scope,
    replay_fill_inventory_is_flat,
    require_scoreable_post_selection_arm,
    resolve_strategy_policy,
    result_key,
    strategy_policy_sha256,
)


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite number {value}")
    return parsed


def _verify_result_log_binding(results_path: str, record: dict) -> None:
    relative = record.get("log")
    try:
        expected_relative = "logs/" + canonical_result_log_name(
            str(record.get("symbol") or ""),
            str(record.get("day") or ""),
            str(record.get("arm") or ""),
            str(record.get("resolved_strategy_policy_sha256") or ""),
        )
    except ValueError as exc:
        raise ValueError("result log identity is invalid") from exc
    if (
        type(relative) is not str
        or relative != expected_relative
        or "\\" in relative
        or relative.startswith("/")
        or relative.split("/")[0] != "logs"
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError("result log path is not a confined relative path")
    root = os.path.realpath(os.path.dirname(os.path.abspath(results_path)))
    path = os.path.realpath(
        os.path.join(root, *relative.split("/"))
    )
    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        raise ValueError("result log is missing or escapes its artifact root")
    with open(path, "rb") as f:
        raw = f.read()
    expected_size = record.get("log_size")
    expected_sha256 = record.get("log_sha256")
    if (
        type(expected_size) is not int
        or expected_size != len(raw)
        or re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256 or "")) is None
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
        raise ValueError("result log size/hash binding mismatch")
    if record.get("status") != "ok":
        return
    try:
        stdout = raw.decode("utf-8").split(
            "\n===== STDERR =====\n",
            1,
        )[0]
    except UnicodeDecodeError as exc:
        raise ValueError("successful result log is not UTF-8") from exc
    parse_status, parsed = parse_driver_stdout(
        stdout,
        expected_symbol=str(record.get("symbol") or ""),
        expected_arm=str(record.get("arm") or ""),
        expected_policy_sha256=str(
            record.get("resolved_strategy_policy_sha256") or ""
        ),
        expected_execution_scope_sha256=str(
            record.get("execution_scope_sha256") or ""
        ),
    )
    if (
        parse_status != "ok"
        or parsed.get("pnl") != record.get("pnl")
        or parsed.get("entries") != record.get("entries")
        or parsed.get("exits") != record.get("exits")
        or parsed.get("final_state") != record.get("final_state")
        or not replay_fill_inventory_is_flat(parsed.get("fills") or [])
        or _canonical_sha256(parsed.get("fills"))
        != _canonical_sha256(record.get("fills"))
    ):
        raise ValueError("successful result does not match its driver log")


def _load_results_snapshot(
    paths: list[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Strict, idempotent dedupe on key; split ok vs non-ok."""
    by_key: dict[str, dict] = {}
    source_by_key: dict[str, str] = {}
    input_hashes: list[dict] = []
    for p in paths:
        with open(p, "rb") as f:
            raw = f.read()
        input_hashes.append(
            {
                "name": os.path.basename(p),
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{p}: results are not UTF-8") from exc
        for line_no, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(
                        line,
                        object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=_reject_nonfinite,
                        parse_float=_parse_finite_float,
                    )
                except ValueError as exc:
                    raise ValueError(f"{p}:{line_no}: invalid JSON result: {exc}") from exc
                if not isinstance(rec, dict):
                    raise ValueError(f"{p}:{line_no}: result must be an object")
                if rec.get("schema") != "chili.golden_replay_window_result.v3":
                    raise ValueError(f"{p}:{line_no}: v3 result schema required")
                key = rec.get("key")
                if not isinstance(key, str) or not key:
                    raise ValueError(f"{p}:{line_no}: non-empty result key required")
                if rec.get("status") not in {
                    "ok", "error", "parse_fail", "timeout", "mine_error",
                    "coverage_unavailable",
                }:
                    raise ValueError(f"{p}:{line_no}: invalid result status")
                if rec.get("status") == "ok":
                    if not isinstance(rec.get("symbol"), str) or not isinstance(
                        rec.get("day"), str
                    ):
                        raise ValueError(f"{p}:{line_no}: ok result identity is incomplete")
                    pnl = rec.get("pnl")
                    if (
                        isinstance(pnl, bool)
                        or not isinstance(pnl, (int, float))
                        or not math.isfinite(float(pnl))
                    ):
                        raise ValueError(f"{p}:{line_no}: ok result PnL must be finite")
                    if not isinstance(rec.get("fills"), list):
                        raise ValueError(f"{p}:{line_no}: ok result fills must be a list")
                    if any(
                        type(rec.get(field)) is not int
                        or rec.get(field) < 0
                        for field in ("entries", "exits")
                    ):
                        raise ValueError(
                            f"{p}:{line_no}: ok result counts must be exact "
                            "nonnegative integers"
                        )
                    if (
                        type(rec.get("final_state")) is not str
                        or not rec["final_state"]
                    ):
                        raise ValueError(
                            f"{p}:{line_no}: ok result final state is invalid"
                        )
                previous = by_key.get(key)
                if previous is not None:
                    kind = "divergent " if previous != rec else ""
                    raise ValueError(
                        f"{p}:{line_no}: {kind}duplicate result key {key}"
                    )
                by_key[key] = rec
                source_by_key[key] = p
    for key, record in by_key.items():
        _verify_result_log_binding(source_by_key[key], record)
    ok = [r for r in by_key.values() if r.get("status") == "ok"]
    bad = [r for r in by_key.values() if r.get("status") != "ok"]
    key = lambda r: (r.get("day") or "", r.get("symbol") or "")  # noqa: E731
    return sorted(ok, key=key), sorted(bad, key=key), input_hashes


def load_results(paths: list[str]) -> tuple[list[dict], list[dict]]:
    ok, bad, _ = _load_results_snapshot(paths)
    return ok, bad


def _reject_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate object key {key!r}")
        out[key] = value
    return out


def _reject_nonfinite(value: str):
    raise ValueError(f"non-finite number {value}")


def load_strict_json(path: str) -> tuple[dict, str]:
    with open(path, "rb") as f:
        raw = f.read()
    try:
        doc = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
            parse_float=_parse_finite_float,
        )
    except ValueError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: root must be an object")
    return doc, hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _require_sha256(value, *, field: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return text


def validate_run_bindings(
    meta: dict,
    manifest: dict,
    records: list[dict],
) -> dict[str, dict]:
    """Bind every result to one selected manifest window and one exact run."""
    for field in (
        "driver_sha256",
        "batch_sha256",
        "generator_sha256",
        "receipt_helper_sha256",
        "manifest_sha256",
        "run_identity_sha256",
        "selected_window_set_sha256",
        "resolved_strategy_policy_sha256",
        "execution_scope_sha256",
    ):
        _require_sha256(meta.get(field), field=f"meta.{field}")
    if re.fullmatch(r"[0-9a-f]{40,64}", str(meta.get("build_sha") or "")) is None:
        raise ValueError("meta.build_sha must be a lowercase Git object id")
    if meta.get("arm") not in STRATEGY_ARM_CHOICES:
        raise ValueError("meta.arm is invalid")
    try:
        require_scoreable_post_selection_arm(meta["arm"])
        resolved_strategy_policy = resolve_strategy_policy(meta["arm"])
        resolved_strategy_policy_sha256 = strategy_policy_sha256(
            meta.get("resolved_strategy_policy")
        )
        resolved_execution_scope = replay_execution_scope()
        resolved_execution_scope_sha256 = execution_scope_sha256(
            meta.get("execution_scope")
        )
    except ValueError as exc:
        raise ValueError(
            "meta resolved strategy policy/execution scope is invalid"
        ) from exc
    if (
        meta.get("resolved_strategy_policy") != resolved_strategy_policy
        or meta.get("resolved_strategy_policy_sha256")
        != resolved_strategy_policy_sha256
        or meta.get("execution_scope") != resolved_execution_scope
        or meta.get("execution_scope_sha256")
        != resolved_execution_scope_sha256
    ):
        raise ValueError(
            "meta resolved strategy policy/execution-scope binding mismatch"
        )
    if (
        meta.get("source_backend_sealed") is not False
        or meta.get("child_source_snapshot_pinned") is not False
        or meta.get("dependency_environment_sealed") is not False
        or meta.get("tracked_source_tree_clean") is not True
        or meta.get("source_receipt_semantics")
        != "PRE_POST_BOUNDARY_OBSERVATION_NOT_CHILD_ATTESTATION"
    ):
        raise ValueError("meta diagnostic source limitations are incomplete")
    run_payload = {
        key: value
        for key, value in meta.items()
        if key not in {"run_identity_sha256", "started_at", "stop_at"}
    }
    if _canonical_sha256(run_payload) != meta["run_identity_sha256"]:
        raise ValueError("meta.run_identity_sha256 does not bind run metadata")

    selected_keys = meta.get("selected_window_keys")
    selected_receipts = meta.get("selected_window_receipts")
    if (
        not isinstance(selected_keys, list)
        or not selected_keys
        or any(not isinstance(key, str) or not key for key in selected_keys)
        or len(selected_keys) != len(set(selected_keys))
        or not isinstance(selected_receipts, dict)
        or set(selected_receipts) != set(selected_keys)
        or meta.get("selected_tiers") != ["baseline"]
        or meta.get("selection_scope")
        not in {"baseline_tier_complete", "explicit_partial"}
    ):
        raise ValueError("meta selected-window roster is invalid")
    roster_payload = {
        "keys": selected_keys,
        "receipts": selected_receipts,
    }
    if _canonical_sha256(roster_payload) != meta["selected_window_set_sha256"]:
        raise ValueError("meta selected-window roster hash mismatch")

    if (
        manifest.get("build_sha") != meta.get("build_sha")
        or manifest.get("generator_sha256") != meta.get("generator_sha256")
        or manifest.get("receipt_helper_sha256")
        != meta.get("receipt_helper_sha256")
        or manifest.get("query_contract_sha256")
        != query_contract_sha256()
        or manifest.get("source_database_name")
        != meta.get("source_database_name")
        or manifest.get("source_database_identity")
        != meta.get("source_database_identity")
        or manifest.get("source_backend_sealed") is not False
        or manifest.get("child_source_snapshot_pinned") is not False
    ):
        raise ValueError("manifest generator/source provenance mismatch")

    windows = manifest.get("windows")
    if not isinstance(windows, list):
        raise ValueError("manifest windows must be a list")
    manifest_by_key: dict[str, dict] = {}
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            raise ValueError(f"manifest window {index} is not an object")
        symbol = window.get("symbol")
        day = window.get("day")
        if not isinstance(symbol, str) or not isinstance(day, str):
            raise ValueError(f"manifest window {index} identity is incomplete")
        try:
            win_start_day = datetime.fromisoformat(
                str(window.get("win_start") or "")
            ).date().isoformat()
        except ValueError as exc:
            raise ValueError(
                f"manifest window {index} has an invalid start clock"
            ) from exc
        if win_start_day != day:
            raise ValueError(
                f"manifest window {index} day/start clock mismatch"
            )
        expected_result_key = result_key(
            symbol,
            day,
            meta["arm"],
            resolved_strategy_policy_sha256,
        )
        if expected_result_key in manifest_by_key:
            raise ValueError(f"duplicate manifest window {symbol}|{day}")
        manifest_by_key[expected_result_key] = window

    if meta.get("selection_scope") == "baseline_tier_complete":
        complete_baseline_keys = {
            key
            for key, window in manifest_by_key.items()
            if window.get("tier") == "baseline"
        }
        if set(selected_keys) != complete_baseline_keys:
            raise ValueError(
                "complete baseline roster omits or adds a manifest window"
            )

    for key in selected_keys:
        window = manifest_by_key.get(key)
        if window is None:
            raise ValueError(f"selected key is absent from manifest: {key}")
        receipt = window.get("source_content_receipt")
        expected_receipt_sha256 = window.get(
            "source_content_receipt_sha256"
        )
        if (
            window.get("tier") != "baseline"
            or window.get("coverage_status") != "DIAGNOSTIC_ONLY"
            or window.get("source_content_status") != "CONTENT_HASHED"
            or not isinstance(receipt, dict)
            or receipt.get("schema")
            != "chili.golden-window-content-receipt.v2"
            or receipt.get("query_contract_sha256")
            != query_contract_sha256()
            or receipt.get("symbol") != window.get("symbol")
            or receipt.get("start") != window.get("ohlcv_start")
            or receipt.get("end") != window.get("win_end")
            or content_receipt_sha256(receipt)
            != expected_receipt_sha256
            or selected_receipts.get(key) != expected_receipt_sha256
        ):
            raise ValueError(f"selected source receipt is invalid: {key}")

    result_window_fields = (
        "symbol",
        "day",
        "class",
        "tier",
        "win_start",
        "win_end",
        "ohlcv_start",
        "window_source",
        "prepend",
    )
    seen_result_keys: set[str] = set()
    for record in records:
        key = record.get("key")
        window = manifest_by_key.get(str(key))
        try:
            record_policy_sha256 = strategy_policy_sha256(
                record.get("resolved_strategy_policy")
            )
            record_execution_scope_sha256 = execution_scope_sha256(
                record.get("execution_scope")
            )
        except ValueError as exc:
            raise ValueError(
                f"result strategy policy/execution scope is invalid for {key}"
            ) from exc
        child_attestation_count = record.get(
            "child_strategy_policy_attestation_count"
        )
        child_execution_scope_attestation_count = record.get(
            "child_execution_scope_attestation_count"
        )
        if (
            key not in selected_receipts
            or window is None
            or key in seen_result_keys
            or key
            != result_key(
                str(record.get("symbol") or ""),
                str(record.get("day") or ""),
                meta["arm"],
                resolved_strategy_policy_sha256,
            )
            or record.get("arm") != meta.get("arm")
            or type(record.get("prepend")) is not bool
            or any(
                record.get(field) != window.get(field)
                for field in result_window_fields
            )
            or record.get("build_sha") != meta.get("build_sha")
            or record.get("driver_sha256") != meta.get("driver_sha256")
            or record.get("batch_sha256") != meta.get("batch_sha256")
            or record.get("generator_sha256")
            != meta.get("generator_sha256")
            or record.get("receipt_helper_sha256")
            != meta.get("receipt_helper_sha256")
            or record.get("manifest_sha256") != meta.get("manifest_sha256")
            or record.get("run_identity_sha256")
            != meta.get("run_identity_sha256")
            or record.get("resolved_strategy_policy")
            != resolved_strategy_policy
            or record_policy_sha256 != resolved_strategy_policy_sha256
            or record.get("resolved_strategy_policy_sha256")
            != resolved_strategy_policy_sha256
            or record.get("execution_scope") != resolved_execution_scope
            or record_execution_scope_sha256
            != resolved_execution_scope_sha256
            or record.get("execution_scope_sha256")
            != resolved_execution_scope_sha256
            or type(child_attestation_count) is not int
            or child_attestation_count not in {0, 1}
            or (
                (
                    record.get("child_strategy_policy_label"),
                    record.get("child_strategy_policy_sha256"),
                    child_attestation_count,
                )
                not in {
                    (None, None, 0),
                    (meta["arm"], resolved_strategy_policy_sha256, 1),
                }
            )
            or (
                record.get("status") == "ok"
                and (
                    record.get("child_strategy_policy_label") != meta["arm"]
                    or record.get("child_strategy_policy_sha256")
                    != resolved_strategy_policy_sha256
                    or child_attestation_count != 1
                )
            )
            or type(child_execution_scope_attestation_count) is not int
            or child_execution_scope_attestation_count not in {0, 1}
            or (
                (
                    record.get("child_execution_scope_label"),
                    record.get("child_execution_scope_sha256"),
                    child_execution_scope_attestation_count,
                )
                not in {
                    (None, None, 0),
                    (
                        "post-selection-fsm",
                        resolved_execution_scope_sha256,
                        1,
                    ),
                }
            )
            or (
                record.get("status") == "ok"
                and (
                    record.get("child_execution_scope_label")
                    != "post-selection-fsm"
                    or record.get("child_execution_scope_sha256")
                    != resolved_execution_scope_sha256
                    or child_execution_scope_attestation_count != 1
                )
            )
            or record.get("source_content_receipt_sha256")
            != selected_receipts.get(key)
            or record.get("source_content_receipt_pre_sha256")
            != selected_receipts.get(key)
            or record.get("evidence_grade") != "DIAGNOSTIC_ONLY"
            or record.get("causal_use_allowed") is not False
            or record.get("ross_grade_credit_allowed") is not False
            or record.get("paper_policy_parity") is not False
            or record.get("fees_slippage_complete") is not False
        ):
            raise ValueError(f"result provenance mismatch for {key}")
        if record.get("status") == "ok" and (
            record.get("source_content_receipt_post_sha256")
            != selected_receipts[key]
            or record.get("source_verification_error") is not None
            or record.get("executable_verification_error") is not None
        ):
            raise ValueError(f"successful result lacks final proof for {key}")
        seen_result_keys.add(key)
    return {key: manifest_by_key[key] for key in selected_keys}


def guard_source_database_url(url: str) -> tuple[str, str]:
    identity = guarded_database_identity(url, sink=False)
    return url, identity.dbname


def clean_build_sha() -> str:
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=BUILD,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=BUILD,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if dirty.strip():
        raise RuntimeError("scorecard requires the exact clean build authority")
    return rev


def file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def verified_producer_file_sha256(meta: dict) -> dict[str, str]:
    observed = {
        field: file_sha256(path)
        for field, path in PRODUCER_PATHS.items()
    }
    if any(meta.get(field) != digest for field, digest in observed.items()):
        raise ValueError("producer bytes do not match run metadata")
    return observed


def pair_round_trips(fills: list[dict]) -> list[dict]:
    """Aggregate fills into flat-to-flat long position cycles.

    Scale-ins and scale-outs stay one cycle so exit tranches cannot inflate the
    sample size or expectancy denominator.
    """
    trades: list[dict] = []
    open_qty = buy_qty = buy_cost = sell_qty = sell_proceeds = 0.0
    for index, f in enumerate(fills or []):
        if not isinstance(f, dict):
            raise ValueError(f"fill {index} must be an object")
        side = str(f.get("side") or "").lower()
        try:
            qty = float(f.get("qty"))
            px = float(f.get("px"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"fill {index} has invalid quantity or price") from exc
        if (
            side not in {"buy", "sell"}
            or isinstance(f.get("qty"), bool)
            or isinstance(f.get("px"), bool)
            or not math.isfinite(qty)
            or not math.isfinite(px)
            or qty <= 0
            or px <= 0
        ):
            raise ValueError(f"fill {index} is not a finite positive BUY/SELL")
        if side == "buy":
            open_qty += qty
            buy_qty += qty
            buy_cost += qty * px
            continue
        if qty > open_qty + 1e-9:
            raise ValueError(f"fill {index} sells more quantity than is open")
        open_qty -= qty
        sell_qty += qty
        sell_proceeds += qty * px
        if open_qty <= 1e-9:
            if buy_qty <= 0 or abs(buy_qty - sell_qty) > 1e-7:
                raise ValueError("fill cycle quantity does not close exactly")
            trades.append({
                "qty": buy_qty,
                "entry_px": round(buy_cost / buy_qty, 4),
                "exit_px": round(sell_proceeds / sell_qty, 4),
                "pnl_usd": round(sell_proceeds - buy_cost, 2),
            })
            open_qty = buy_qty = buy_cost = sell_qty = sell_proceeds = 0.0
    if open_qty > 1e-9:
        raise ValueError("fill set leaves an open quantity")
    return trades


def attach_fill_times(rec: dict, trades: list[dict]) -> None:
    """Keep Label A unavailable until both sides carry immutable cycle lineage.

    The current stdout fill records contain only side/quantity/price. Sink event
    identities therefore cannot be joined without positional inference, even
    when quantities/prices happen to match. Positional inference is prohibited.
    """
    del rec
    for trade in trades:
        trade["entry_ts"] = None
        trade["exit_ts"] = None
        trade["trigger_reason"] = None
        trade["exit_reason"] = None
        trade["label_a_unavailable_reason"] = (
            "immutable_entry_exit_cycle_lineage_unavailable"
        )


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_window_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        parsed = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class _QuotePoint:
    ts: datetime
    bid: float
    ask: float

    def validate(self) -> None:
        if not isinstance(self.ts, datetime):
            raise ValueError("path timestamp is required")
        if not all(
            math.isfinite(float(value)) for value in (self.bid, self.ask)
        ):
            raise ValueError("path prices must be finite")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("path quote must be positive and uncrossed")


@lru_cache(maxsize=4)
def _load_pure_module(
    name: str,
    path: str,
    expected_sha256: str | None,
):
    with open(path, "rb") as f:
        raw = f.read()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise RuntimeError(f"analyzer helper bytes drifted: {path}")
    spec = importlib.util.spec_from_loader(name, loader=None, origin=path)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = path
    sys.modules[name] = module
    exec(compile(raw, path, "exec"), module.__dict__)
    return module


def _evaluate_long_trade_path(*args, **kwargs):
    module = _load_pure_module(
        "_chili_diagnostic_ross_replay_benchmark",
        ROSS_BENCHMARK_PATH,
        _PINNED_ANALYZER_SHA256.get(ROSS_BENCHMARK_PATH),
    )
    return module.evaluate_long_trade_path(*args, **kwargs)


def nbbo_path(conn, symbol: str, t0: datetime, t1: datetime, max_points: int = 5000):
    cur = conn.cursor()
    cur.execute(
        "SELECT observed_at, bid, ask FROM replay_golden_nbbo "
        "WHERE symbol = %s AND observed_at >= %s AND observed_at <= %s "
        "AND bid > 0 AND ask >= bid ORDER BY observed_at ASC, id ASC LIMIT %s",
        (symbol, t0, t1, max_points + 1))
    rows = cur.fetchall()
    if len(rows) > max_points:
        return None
    points = []
    for observed_at, bid, ask in rows:
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        else:
            observed_at = observed_at.astimezone(timezone.utc)
        points.append(
            _QuotePoint(
                ts=observed_at,
                bid=float(bid),
                ask=float(ask),
            )
        )
    return points


def within_trade_metrics(conn, rec: dict, trade: dict) -> dict | None:
    """Label A — diagnostic within-trade MFE on an exact bounded NBBO path.

    Impossible or unbound supplied fills fail this metric closed. They are never
    replaced with quote-derived fills.
    """
    t_in, t_out = _parse_ts(trade.get("entry_ts")), _parse_ts(trade.get("exit_ts"))
    if conn is None or t_in is None or t_out is None or t_out < t_in:
        return None
    points = nbbo_path(conn, rec["symbol"], t_in, t_out)
    if not points:
        return None
    base = dict(entry_ts=t_in, exit_ts=t_out, qty=trade["qty"])
    try:
        m = _evaluate_long_trade_path(
            points,
            **base,
            entry_fill_price=trade["entry_px"],
            exit_fill_price=trade["exit_px"],
        )
    except ValueError:
        return None
    return {"capture_ratio": m.realized_mfe_capture_ratio,
            "giveback_frac": m.open_profit_giveback_fraction,
            "peak_open_profit_usd": round(m.peak_open_profit_usd, 2),
            "seconds_to_peak": round(m.seconds_to_peak, 1)}


def post_exit(conn, rec: dict, trade: dict) -> dict | None:
    t_out = _parse_ts(trade.get("exit_ts"))
    win_end = _parse_window_ts(rec.get("win_end"))
    if conn is None or t_out is None or win_end is None or t_out >= win_end:
        return None
    cur = conn.cursor()
    cur.execute("SELECT max(price), min(price) FROM replay_golden_ticks "
                "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s AND price > 0",
                (rec["symbol"], t_out, win_end))
    hi, lo = cur.fetchone()
    if not hi:
        return None
    module = _load_pure_module(
        "_chili_diagnostic_post_exit_excursion",
        POST_EXIT_PATH,
        _PINNED_ANALYZER_SHA256.get(POST_EXIT_PATH),
    )

    out = module.compute_post_exit_excursion(
        entry_price=trade["entry_px"], exit_price=trade["exit_px"],
        original_target=None, original_stop=None, side_long=True,
        future_high=float(hi), future_low=float(lo),
        exit_reason=trade.get("exit_reason"), realized_pnl=trade.get("pnl_usd"))
    if not out.get("ok", True):
        return None
    return {k: out.get(k) for k in ("post_exit_mfe_pct", "post_exit_mae_pct", "outcome_class")}


def window_capture(rec: dict, trades: list[dict]) -> dict:
    """Label B — full-window conversion. Defined for every window; zero-entry
    windows report entered=false and are excluded from the conditional mean."""
    mk = rec.get("market") or {}
    first_px, hi = mk.get("first_px"), mk.get("hi")
    move_frac = ((hi - first_px) / first_px) if (first_px and hi and first_px > 0) else None
    deployed = sum(t["qty"] * t["entry_px"] for t in trades)
    reconciliation = rec.get("pnl_reconciliation") or {}
    pnl = (
        reconciliation.get("fill_pnl_usd")
        if reconciliation.get("status") == "MATCHED"
        else None
    )
    ratio = None
    if move_frac and move_frac > 0 and deployed > 0 and pnl is not None:
        ratio = (pnl / deployed) / move_frac
    return {"window_move_frac": round(move_frac, 4) if move_frac is not None else None,
            "deployed_notional": round(deployed, 2), "entered": bool(trades),
            "window_capture_ratio": round(ratio, 4) if ratio is not None else None}


def reconcile_result_pnl(rec: dict, trades: list[dict]) -> None:
    """Expose headline P&L only when replay summary and flat cycles agree."""
    fill_pnl = round(sum(trade["pnl_usd"] for trade in trades), 2)
    reported_pnl = round(float(rec["pnl"]), 2)
    delta = round(reported_pnl - fill_pnl, 2)
    matched = math.isclose(
        reported_pnl,
        fill_pnl,
        rel_tol=0.0,
        abs_tol=0.01,
    )
    rec["pnl_reconciliation"] = {
        "status": "MATCHED" if matched else "UNAVAILABLE",
        "reported_pnl_usd": reported_pnl,
        "fill_pnl_usd": fill_pnl,
        "delta_usd": delta,
    }
    rec["replay_reported_pnl_usd"] = reported_pnl
    rec["pnl"] = fill_pnl if matched else None


def diagnostic_stats(pnls: list[float]) -> dict:
    """Descriptive replay-fill statistics; never a promotion or structural-R gate."""
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {"n": n, "wins": len(wins), "losses": len(losses),
            "win_rate": (len(wins) / n if n else 0.0), "net": round(sum(pnls), 2),
            "profit_factor": (gross_win / gross_loss if gross_loss > 0
                              else None),
            "expectancy": (sum(pnls) / n if n else 0.0),
            "average_win": (statistics.mean(wins) if wins else None),
            "average_loss_abs": (statistics.mean([-x for x in losses]) if losses else None)}


def _family(reason: str | None) -> str:
    r = (reason or "").lower()
    for fam, keys in (("micro_pullback", ("micro_pullback",)),
                      ("orb/raw_break", ("orb_break", "raw_break", "range_break")),
                      ("vwap/deep_reclaim", ("vwap", "deep_reclaim", "backside")),
                      ("flush_dip/wick", ("flush_dip", "wick_reclaim")),
                      ("flag/abcd", ("bull_flag", "abcd", "flat_top")),
                      ("halt", ("halt",)),
                      ("ihs/reversal", ("head_shoulders", "bottom_reversal", "reversal"))):
        if any(k in r for k in keys):
            return fam
    return "other" if r else "unknown"


def fmt_money(v) -> str:
    return f"{v:+,.2f}" if isinstance(v, (int, float)) else "—"


def render_markdown(sc: dict) -> str:
    m = sc["meta"]
    L: list[str] = []
    L.append("# Golden-library diagnostic replay scorecard")
    L.append("")
    L.append(f"- generated: {sc['generated_at']} · build sha `{m.get('build_sha', '?')[:12]}` "
             f"· arm `{m.get('arm')}` · policy sha "
             f"`{str(m.get('resolved_strategy_policy_sha256') or '?')[:12]}` "
             f"· GOLDEN=1 · sink "
             f"`{m.get('sink_database_name', m.get('sink', '?'))}`")
    L.append(
        "- replay policy provenance: the complete operator-approved nine-flag "
        "configuration vector is parent-bound and every successful result is "
        "exactly once child-attested; execution credit is limited by the "
        "post-selection scope below"
    )
    analyzer_build = (sc.get("analyzer_provenance") or {}).get("build_sha")
    if analyzer_build:
        L.append(
            f"- analyzer build: `{analyzer_build[:12]}` with clean source "
            "hashes verified before and after"
        )
    L.append(f"- equity/exec: {m.get('equity_exec')}")
    L.append(
        f"- attempted {sc['coverage']['attempted']} / selected "
        f"{sc['coverage']['selected']} windows "
        f"(ok={sc['coverage']['ok']}, failed={sc['coverage']['failed']}, "
        f"remaining={sc['coverage']['remaining_selected']}); full manifest "
        f"inventory={sc['coverage']['library']}"
    )
    L.append("- **DIAGNOSTIC_ONLY**: post-session windows and simulated fills. "
             "Fees/slippage and executable coverage are not certified. Use only "
             "for same-input deltas; this is not OOS, profitability, or Ross-parity evidence.")
    L.append("- PAPER policy parity is false: this harness neutralizes operational "
             "kill-switch/connectivity/session gates and cannot certify activation behavior.")
    L.append("- Execution scope is **post-selection FSM only**: symbols are seeded "
             "`queued_live`; universe-float and catalyst-arb-flat selection are "
             "not executed, the ordinary entry-risk gate is bypassed, and Ortex "
             "admission is explicitly neutralized because no captured Ortex-v2 "
             "authority is present. Quote freshness uses the replay-sim clock. "
             "Whole-policy profitability is not allowed.")
    L.append("- Source receipts are pre/post boundary observations only. The child "
             "does not consume an exported PostgreSQL snapshot, the backend is not "
             "sealed, and mixed-snapshot races therefore receive no causal credit.")
    L.append("")
    L.append("## Per-window results")
    L.append("")
    L.append("| symbol | day | window UTC | class | move % | reconciled PnL | entries/exits "
             "| capture B | top trace | top reject |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sc["windows"]:
        mk, wc = r.get("market") or {}, r.get("window_capture") or {}
        st = ((r.get("sink") or {}).get("setup_trace") or {})
        traces = st.get("trace_alias_counts") or {}
        top_trace = max(traces.items(), key=lambda kv: kv[1])[0] if traces else "—"
        rejects = (r.get("sink") or {}).get("top_rejects") or []
        top_reject = rejects[0][0] if rejects else "—"
        L.append(f"| {r['symbol']} | {r['day']} | {r['win_start'][11:16]}–{r['win_end'][11:16]} "
                 f"| {r['class']} | {mk.get('up_pct', '—')} | {fmt_money(r.get('pnl'))} "
                 f"| {r.get('entries', 0)}/{r.get('exits', 0)} "
                 f"| {wc.get('window_capture_ratio') if wc.get('entered') else 'no-entry'} "
                 f"| {top_trace} | {top_reject} |")
    L.append("")
    L.append("## Per-setup aggregation (trigger_reason families, entries from fill events)")
    L.append("")
    L.append("| family | trades | net PnL | wins | losses |")
    L.append("|---|---|---|---|---|")
    for fam, agg in sc["per_setup"].items():
        L.append(f"| {fam} | {agg['n']} | {fmt_money(agg['net'])} "
                 f"| {agg['wins']} | {agg['losses']} |")
    L.append("")
    L.append("## Descriptive replay-fill statistics (no promotion gate)")
    L.append("")
    s = sc["expectancy"]
    L.append(f"round trips n={s['n']} · net {fmt_money(s['net'])} · PF "
             f"{s['profit_factor'] if s['profit_factor'] is not None else 'unavailable'} "
             f"· win rate {s['win_rate']:.0%} · expectancy {fmt_money(s['expectancy'])}")
    L.append("")
    L.append("## Label A — within-trade MFE capture (entry→exit, bids only)")
    L.append("")
    a = sc["label_a"]
    if a["n"]:
        L.append(f"- trades with exact bounded quote-path metrics: {a['n']}")
        L.append(f"- mean capture ratio {a['mean_capture']:.2f} · mean giveback "
                 f"{a['mean_giveback']:.2f}")
        L.append(f"- post-exit outcome classes: {a['outcome_classes']}")
    else:
        L.append("- unavailable (missing fill timestamps, exact path, or explicit source)")
    L.append("")
    L.append("## Ross evidence posture")
    L.append("")
    L.append("- This scorecard intentionally grants **zero Ross credit**. "
             "Ross phase/account/executable-price binding belongs to the sealed "
             "acceptance grader, not a symbol/day diagnostic join.")
    L.append("")
    L.append("## Failed / skipped windows")
    for r in sc["errors"]:
        L.append(f"- {r.get('day')} {r.get('symbol')}: {r.get('status')} "
                 f"(exit={r.get('exit_code')}) log={r.get('log')}")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--library-manifest", required=True,
                    help="v2 diagnostic window_manifest.json")
    ap.add_argument("--source-database-url", default=None,
                    help="explicit loopback read-only source for exact bounded quote paths")
    ap.add_argument(
        "--source-statement-timeout-ms",
        type=int,
        default=60_000,
        help="positive per-query ceiling for optional source verification",
    )
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    args = ap.parse_args()

    meta, meta_sha256 = load_strict_json(args.meta)
    try:
        scorecard_build_sha = clean_build_sha()
    except Exception as exc:
        raise SystemExit(
            f"[scorecard] REFUSING unsealed scorecard build: {exc}"
        ) from exc
    analyzer_paths = {
        "scorecard": os.path.abspath(__file__),
        "ross_replay_benchmark": ROSS_BENCHMARK_PATH,
        "post_exit_excursion": POST_EXIT_PATH,
    }
    analyzer_sha256 = {
        name: file_sha256(path)
        for name, path in analyzer_paths.items()
    }
    try:
        producer_sha256 = verified_producer_file_sha256(meta)
    except ValueError as exc:
        raise SystemExit(
            "[scorecard] REFUSING producer bytes do not match run metadata"
        ) from exc
    _PINNED_ANALYZER_SHA256.clear()
    _PINNED_ANALYZER_SHA256.update(
        {
            path: analyzer_sha256[name]
            for name, path in analyzer_paths.items()
            if name != "scorecard"
        }
    )
    if (
        meta.get("schema") != "chili.golden_replay_run_meta.v3"
        or meta.get("build_sha") != scorecard_build_sha
        or meta.get("evidence_grade") != "DIAGNOSTIC_ONLY"
        or meta.get("causal_use_allowed") is not False
        or meta.get("ross_grade_credit_allowed") is not False
        or meta.get("paper_policy_parity") is not False
        or meta.get("operational_safeguards_neutralized") is not True
        or meta.get("fees_slippage_complete") is not False
        or meta.get("source_backend_sealed") is not False
        or meta.get("child_source_snapshot_pinned") is not False
    ):
        raise SystemExit("[scorecard] REFUSING non-diagnostic or legacy run metadata")
    if args.source_statement_timeout_ms <= 0:
        raise SystemExit("[scorecard] REFUSING non-positive source timeout")
    ok, bad, result_hashes = _load_results_snapshot(args.results)
    library_doc, library_sha256 = load_strict_json(args.library_manifest)
    if (
        library_doc.get("schema") != "chili.replay-window-manifest.v2"
        or library_doc.get("evidence_grade") != "DIAGNOSTIC_ONLY"
        or library_doc.get("causal_use_allowed") is not False
        or library_doc.get("ross_grade_credit_allowed") is not False
    ):
        raise SystemExit("[scorecard] REFUSING non-diagnostic library manifest")
    if meta.get("manifest_sha256") != library_sha256:
        raise SystemExit("[scorecard] REFUSING manifest hash mismatch")
    try:
        selected_manifest = validate_run_bindings(
            meta,
            library_doc,
            [*ok, *bad],
        )
    except ValueError as exc:
        raise SystemExit(f"[scorecard] REFUSING {exc}") from exc
    library = len(library_doc.get("windows", []))

    conn = None
    if args.source_database_url:
        import psycopg2

        source_identity = guarded_database_identity(
            args.source_database_url, sink=False
        )
        source_url, source_name = (
            args.source_database_url,
            source_identity.dbname,
        )
        if source_name != meta.get("source_database_name"):
            raise SystemExit("[scorecard] REFUSING source database identity mismatch")
        if source_identity.public_dict() != meta.get("source_database_identity"):
            raise SystemExit("[scorecard] REFUSING source endpoint identity mismatch")
        conn = psycopg2.connect(source_url, connect_timeout=5)
        conn.set_session(
            readonly=True,
            autocommit=False,
            isolation_level="REPEATABLE READ",
        )
        verify_connected_endpoint(conn, source_identity)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL TIME ZONE 'UTC'")
            cur.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(args.source_statement_timeout_ms),),
            )
            cur.execute(
                "SELECT current_database(), current_setting('transaction_read_only'), "
                "current_setting('transaction_isolation'), current_setting('TimeZone')"
            )
            actual_name, read_only, isolation, time_zone = cur.fetchone()
            if str(actual_name) != source_name:
                raise SystemExit("[scorecard] source database identity mismatch")
            if str(read_only).lower() not in {"on", "true"}:
                raise SystemExit("[scorecard] source transaction is not read-only")
            if str(isolation).lower() != "repeatable read":
                raise SystemExit("[scorecard] source transaction is not repeatable read")
            if str(time_zone).upper() not in {"UTC", "ETC/UTC"}:
                raise SystemExit("[scorecard] source transaction timezone is not UTC")
        for record in [*ok, *bad]:
            window = selected_manifest[record["key"]]

            def bind_timeout() -> None:
                with conn.cursor() as timeout_cur:
                    timeout_cur.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(args.source_statement_timeout_ms),),
                    )

            observed_receipt = golden_window_content_receipt(
                conn,
                symbol=window["symbol"],
                start=window["ohlcv_start"],
                end=window["win_end"],
                before_query=bind_timeout,
            )
            if (
                observed_receipt != window["source_content_receipt"]
                or content_receipt_sha256(observed_receipt)
                != window["source_content_receipt_sha256"]
            ):
                raise SystemExit(
                    "[scorecard] COVERAGE_UNAVAILABLE: source content changed "
                    f"for {record['key']}"
                )

    trades_by_key: dict[str, list[dict]] = {}
    label_a_rows: list[dict] = []
    try:
        for r in ok:
            trades = pair_round_trips(r.get("fills") or [])
            reconcile_result_pnl(r, trades)
            attach_fill_times(r, trades)
            for t in trades:
                wt = within_trade_metrics(conn, r, t)
                if wt:
                    t["label_a"] = wt
                    pe = post_exit(conn, r, t)
                    if pe:
                        t["post_exit"] = pe
                    label_a_rows.append(t)
            trades_by_key[r["key"]] = trades
            r["window_capture"] = window_capture(r, trades)
            r["trades"] = trades
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()

    all_trades = [t for ts in trades_by_key.values() for t in ts]
    pnls = [t["pnl_usd"] for t in all_trades]
    per_setup: dict[str, dict] = {}
    for t in all_trades:
        fam = _family(t.get("trigger_reason"))
        agg = per_setup.setdefault(fam, {"n": 0, "net": 0.0, "wins": 0, "losses": 0})
        agg["n"] += 1
        agg["net"] = round(agg["net"] + t["pnl_usd"], 2)
        agg["wins"] += 1 if t["pnl_usd"] > 0 else 0
        agg["losses"] += 1 if t["pnl_usd"] < 0 else 0

    captures = [t["label_a"]["capture_ratio"] for t in label_a_rows
                if t.get("label_a", {}).get("capture_ratio") is not None]
    givebacks = [t["label_a"]["giveback_frac"] for t in label_a_rows
                 if t.get("label_a", {}).get("giveback_frac") is not None]
    oc: dict[str, int] = {}
    for t in label_a_rows:
        c = (t.get("post_exit") or {}).get("outcome_class")
        if c:
            oc[c] = oc.get(c, 0) + 1
    label_a = {"n": len(label_a_rows),
               "mean_capture": (statistics.mean(captures) if captures else 0.0),
               "mean_giveback": (statistics.mean(givebacks) if givebacks else 0.0),
               "outcome_classes": oc}

    stats = diagnostic_stats(pnls)
    if (
        clean_build_sha() != scorecard_build_sha
        or any(
            file_sha256(path) != analyzer_sha256[name]
            for name, path in analyzer_paths.items()
        )
        or any(
            file_sha256(path) != producer_sha256[field]
            for field, path in PRODUCER_PATHS.items()
        )
    ):
        raise SystemExit(
            "[scorecard] REFUSING analyzer source drift during scorecard run"
        )
    sc = {"schema": "chili.golden_replay_scorecard.v3",
          "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "evidence_grade": "DIAGNOSTIC_ONLY",
          "causal_use_allowed": False,
          "oos_evidence": False,
          "profitability_established": False,
          "ross_parity_established": False,
          "paper_policy_parity": False,
          "input_sha256": {
              "meta": meta_sha256,
              "library_manifest": library_sha256,
              "results": result_hashes,
          },
          "analyzer_provenance": {
              "build_sha": scorecard_build_sha,
              "clean_tree_verified_before_and_after": True,
              "file_sha256": analyzer_sha256,
              "producer_file_sha256": producer_sha256,
          },
          "meta": meta,
          "coverage": {
              "attempted": len(ok) + len(bad),
              "ok": len(ok),
              "failed": len(bad),
              "selected": len(meta["selected_window_keys"]),
              "remaining_selected": (
                  len(meta["selected_window_keys"]) - len(ok) - len(bad)
              ),
              "library": library,
          },
          "windows": ok, "errors": [{k: r.get(k) for k in
                                     ("key", "symbol", "day", "status", "exit_code", "log")}
                                    for r in bad],
          "per_setup": dict(sorted(per_setup.items(), key=lambda kv: -kv[1]["n"])),
          "expectancy": stats,
          "label_a": label_a}

    if os.path.abspath(args.out_json) == os.path.abspath(args.out_md):
        raise SystemExit("[scorecard] REFUSING identical JSON/Markdown output")
    md = render_markdown(sc)
    sc["output_binding"] = {
        "json_is_terminal_authority": True,
        "markdown_name": os.path.basename(args.out_md),
        "markdown_sha256": hashlib.sha256(md.encode("utf-8")).hexdigest(),
    }
    json_text = json.dumps(sc, indent=1, allow_nan=False)
    staged: list[tuple[str, str]] = []
    try:
        for destination, raw in (
            (args.out_json, json_text.encode("utf-8")),
            (args.out_md, md.encode("utf-8")),
        ):
            destination = os.path.abspath(destination)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            temp_path = f"{destination}.tmp-{os.getpid()}"
            with open(temp_path, "xb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            staged.append((temp_path, destination))
        # JSON is the terminal authority and binds the Markdown bytes. It is
        # replaced last, so a crash can leave only an unbound human-readable
        # alias, never a new authoritative JSON naming old Markdown.
        for temp_path, destination in reversed(staged):
            os.replace(temp_path, destination)
    finally:
        for temp_path, _ in staged:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    print(f"[scorecard] {len(ok)} ok / {len(bad)} failed -> {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
