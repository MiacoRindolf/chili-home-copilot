"""Sequential batch benchmark runner over the golden replay library.

Iterates the window manifest (``scripts/derive_replay_windows.py``), replays each
window through the real FSM via ``scripts/replay_ab_dark_flags.py`` (GOLDEN=1,
explicit source/sink/equity/risk/execution family), mines the disposable test
sink between runs (the next driver run clears its boards), and appends one JSONL
record per window. Results are diagnostic only.

    python scripts/replay_benchmark_batch.py \
        --manifest D:/CHILI-Docker/chili-data/replay_batch/window_manifest.json \
        --out-dir  D:/CHILI-Docker/chili-data/replay_batch_policy_v3 \
        --source-database-url postgresql://.../sealed_source \
        --sink-database-url postgresql://.../disposable_test \
        --confirm-test-sink-reset RESET_DISPOSABLE_REPLAY_TEST_SINK \
        --ohlcv-cache-dir D:/.../ohlcv_cache \
        --equity 100000 --risk-fraction 0.01 --exec-family alpaca_spot \
        --tiers baseline

Guards (all process-local — nothing touches the sealed Monday activation):
  * sink db name MUST end in ``_test`` (mirrors tests/conftest.py — fixtures TRUNCATE);
  * ``pg_try_advisory_lock(hashtext('chili_replay_batch'))`` held on the sink for the
    batch lifetime — one replay at a time, refuses to double-run;
  * STOP_AT deadline: pre-launch fit check (1.25x estimate + 2min must fit), bounded
    source-receipt queries, and an in-flight subprocess timeout capped to the
    remaining time. This is a diagnostic operator deadline, not a scheduler.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from diagnostic_replay_db import (  # noqa: E402
    DatabaseIdentity,
    content_receipt_sha256,
    golden_window_content_receipt,
    guarded_database_identity,
    query_contract_sha256,
    verify_connected_endpoint,
)

BUILD = os.path.dirname(SCRIPT_DIR)
DRIVER = os.path.join(BUILD, "scripts", "replay_ab_dark_flags.py")
DERIVER = os.path.join(BUILD, "scripts", "derive_replay_windows.py")
RECEIPT_HELPER = os.path.join(BUILD, "scripts", "diagnostic_replay_db.py")

_DECIMAL_PATTERN = r"(?:\d+(?:\.\d+)?|\.\d+)"
SUMMARY_RE = re.compile(
    r"\[ARM=([a-z0-9_.-]+)\]\s+(\S+)\s+PnL\s+"
    rf"([+-]?{_DECIMAL_PATTERN})\s+entries=(\d+)\s+exits=(\d+)"
)
POLICY_ATTESTATION_RE = re.compile(
    r"^\[STRATEGY_POLICY=([a-z0-9_.-]+)\]\s+sha256=([0-9a-f]{64})$"
)
EXECUTION_SCOPE_ATTESTATION_RE = re.compile(
    r"^\[EXECUTION_SCOPE=([a-z0-9_.-]+)\]\s+sha256=([0-9a-f]{64})$"
)
FILL_RE = re.compile(
    rf"^\s*(BUY|SELL)\s+({_DECIMAL_PATTERN})\s+@\s+\$?"
    rf"({_DECIMAL_PATTERN})"
)
FINAL_RE = re.compile(r"final_state=(\S+)")
TEST_SINK_CONFIRMATION = "RESET_DISPOSABLE_REPLAY_TEST_SINK"
STRATEGY_POLICY_SCHEMA = "chili.replay-resolved-strategy-policy.v1"
EXECUTION_SCOPE_SCHEMA = "chili.replay-execution-scope.v1"
APPROVED_STRATEGY_FLAGS_BY_SLUG = (
    (
        "universe-float",
        "chili_momentum_universe_float_gate_enabled",
    ),
    (
        "orb-ihs-stop",
        "chili_momentum_orb_ihs_structural_stop_enabled",
    ),
    (
        "ross-stop",
        "chili_momentum_ross_stop_alignment_enabled",
    ),
    (
        "flush-dip-volume",
        "chili_momentum_flush_dip_volume_gate_enabled",
    ),
    (
        "tick-break-tape",
        "chili_momentum_tick_break_tape_confirm_enabled",
    ),
    (
        "catalyst-arb-flat",
        "chili_momentum_catalyst_arb_flat_gate_enabled",
    ),
    (
        "bail-no-confirm",
        "chili_momentum_bail_on_no_confirmation_enabled",
    ),
    (
        "fresh-ignition-reentry",
        "chili_momentum_fresh_ignition_reentry_bypass_enabled",
    ),
    (
        "sub-vwap-trap",
        "chili_momentum_sub_vwap_trap_entry_enabled",
    ),
    (
        "chase-defer",
        "chili_momentum_chase_defer_enabled",
    ),
    (
        "whipsaw-escalation",
        "chili_momentum_whipsaw_rapid_escalation_enabled",
    ),
    (
        "flush-dip-afternoon",
        "chili_momentum_flush_dip_fresh_hod_afternoon_enabled",
    ),
    (
        "monster-dip-context",
        "chili_momentum_dip_monster_context_enabled",
    ),
    (
        "late-ah-monster",
        "chili_momentum_late_ah_monster_placement_enabled",
    ),
)
DEFAULT_ARM = "intended"
# CLOSED compound arms: named multi-flag-off vectors. NOT a free-form grammar —
# each entry is an operator-approved, hash-bound vector like every other arm.
# "intended-minus-autopsy-0727" = the 2026-07-27 lever PR's parity vector
# (post-PR build with BOTH new levers OFF == the pre-PR "intended" vector);
# single-minus arms cannot express a two-flag-off parity proof.
COMPOUND_STRATEGY_ARMS = {
    "intended-minus-autopsy-0727": (
        "chili_momentum_chase_defer_enabled",
        "chili_momentum_whipsaw_rapid_escalation_enabled",
    ),
}
STRATEGY_ARM_CHOICES = (
    "base",
    "intended",
    *(f"intended-minus-{slug}" for slug, _ in APPROVED_STRATEGY_FLAGS_BY_SLUG),
    *COMPOUND_STRATEGY_ARMS,
)
UNSCOREABLE_POST_SELECTION_ARMS = (
    "intended-minus-universe-float",
    "intended-minus-catalyst-arb-flat",
)
POST_SELECTION_SCOREABLE_POLICY_FLAGS = (
    "chili_momentum_orb_ihs_structural_stop_enabled",
    "chili_momentum_ross_stop_alignment_enabled",
    "chili_momentum_flush_dip_volume_gate_enabled",
    "chili_momentum_tick_break_tape_confirm_enabled",
    "chili_momentum_bail_on_no_confirmation_enabled",
    "chili_momentum_fresh_ignition_reentry_bypass_enabled",
    "chili_momentum_sub_vwap_trap_entry_enabled",
    "chili_momentum_chase_defer_enabled",
    "chili_momentum_whipsaw_rapid_escalation_enabled",
    "chili_momentum_flush_dip_fresh_hod_afternoon_enabled",
    "chili_momentum_dip_monster_context_enabled",
    "chili_momentum_late_ah_monster_placement_enabled",
)
POST_SELECTION_UNSCOREABLE_POLICY_FLAGS = (
    "chili_momentum_universe_float_gate_enabled",
    "chili_momentum_catalyst_arb_flat_gate_enabled",
)
REPLAY_NEUTRALIZED_SETTINGS = {
    "chili_momentum_squeeze_fuel_tilt_enabled": False,
}
RESULT_STATUSES = {
    "ok",
    "error",
    "parse_fail",
    "timeout",
    "mine_error",
    "coverage_unavailable",
}
SINK_FILL_EVENT_TYPES = (
    "live_entry_filled",
    "live_exit_filled",
    # Historical diagnostic rows used this pre-canonical alias. Retaining it
    # here does not authorize positional entry/exit matching downstream.
    "live_exit_fill",
)
SAFE_PARENT_ENV_KEYS = {
    "COMSPEC",
    "CONDA_PREFIX",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
}

MARKET_SQL = """
SELECT count(*),
       max(price),
       min(price),
       (array_agg(price ORDER BY observed_at ASC, id ASC))[1],
       (array_agg(observed_at ORDER BY price DESC, observed_at ASC))[1]::text
FROM replay_golden_ticks
WHERE symbol = %(s)s AND observed_at >= %(a)s AND observed_at < %(b)s AND price > 0
"""


def resolve_strategy_policy(arm: str) -> dict:
    """Return the complete closed operator-flag vector for one named replay arm."""

    flags = {
        flag: arm != "base"
        for _, flag in APPROVED_STRATEGY_FLAGS_BY_SLUG
    }
    if arm == "base":
        pass
    elif arm == "intended":
        pass
    elif arm in COMPOUND_STRATEGY_ARMS:
        for flag in COMPOUND_STRATEGY_ARMS[arm]:
            flags[flag] = False
    elif arm.startswith("intended-minus-"):
        slug = arm.removeprefix("intended-minus-")
        matches = [
            flag
            for candidate_slug, flag in APPROVED_STRATEGY_FLAGS_BY_SLUG
            if candidate_slug == slug
        ]
        if len(matches) != 1:
            raise ValueError(f"unknown strategy policy arm {arm!r}")
        flags[matches[0]] = False
    else:
        raise ValueError(f"unknown strategy policy arm {arm!r}")
    return {
        "schema": STRATEGY_POLICY_SCHEMA,
        "label": arm,
        "flags": flags,
    }


def strategy_policy_sha256(policy: dict) -> str:
    """Hash only an exact canonical policy document."""

    if type(policy) is not dict or set(policy) != {"schema", "label", "flags"}:
        raise ValueError("strategy policy must be an exact object")
    if type(policy.get("schema")) is not str or type(policy.get("label")) is not str:
        raise ValueError("strategy policy schema and label must be strings")
    flags = policy.get("flags")
    expected_flag_names = {
        flag for _, flag in APPROVED_STRATEGY_FLAGS_BY_SLUG
    }
    if (
        type(flags) is not dict
        or set(flags) != expected_flag_names
        or any(type(value) is not bool for value in flags.values())
    ):
        raise ValueError("strategy policy flags must be the exact boolean vector")
    expected = resolve_strategy_policy(policy["label"])
    if policy != expected:
        raise ValueError("strategy policy is not the canonical closed vector")
    return hashlib.sha256(
        json.dumps(
            policy,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def replay_execution_scope() -> dict:
    """Describe exactly which live-policy boundary this replay executes."""

    return {
        "schema": EXECUTION_SCOPE_SCHEMA,
        "label": "post-selection-fsm",
        "pipeline_start_state": "queued_live",
        "selection_pipeline_executed": False,
        "viability_input_mode": "synthetic_live_eligible_seed",
        "entry_risk_gate_executed": False,
        "ortex_entry_admission_mode":
            "neutralized_missing_captured_ortex_authority",
        "quote_freshness_clock_mode": "replay_sim",
        "neutralized_settings": dict(REPLAY_NEUTRALIZED_SETTINGS),
        "scoreable_policy_flags": list(POST_SELECTION_SCOREABLE_POLICY_FLAGS),
        "unscoreable_policy_flags":
            list(POST_SELECTION_UNSCOREABLE_POLICY_FLAGS),
        "profitability_scope": "post_selection_fsm_conditional",
        "whole_policy_profitability_allowed": False,
    }


def execution_scope_sha256(scope: dict) -> str:
    expected = replay_execution_scope()
    if type(scope) is not dict:
        raise ValueError("execution scope is not the canonical closed document")
    canonical = json.dumps(
        scope,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected_canonical = json.dumps(
        expected,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical != expected_canonical:
        raise ValueError("execution scope is not the canonical closed document")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_scoreable_post_selection_arm(arm: str) -> None:
    if arm in UNSCOREABLE_POST_SELECTION_ARMS:
        raise ValueError(
            f"{arm} changes only a pre-selection flag that this queued_live "
            "replay does not execute"
        )


def result_key(
    symbol: str,
    day: str,
    policy_label: str,
    policy_sha256: str,
) -> str:
    if (
        re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", symbol) is None
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None
        or policy_label not in STRATEGY_ARM_CHOICES
        or re.fullmatch(r"[0-9a-f]{64}", policy_sha256) is None
    ):
        raise ValueError("invalid result-key component")
    try:
        canonical_day = datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("result key day is not a calendar date") from exc
    if canonical_day != day:
        raise ValueError("result key day is not canonical")
    expected_policy_sha256 = strategy_policy_sha256(
        resolve_strategy_policy(policy_label)
    )
    if policy_sha256 != expected_policy_sha256:
        raise ValueError("result key policy label/hash mismatch")
    return f"{symbol}|{day}|{policy_label}|{policy_sha256}"


def canonical_result_log_name(
    symbol: str,
    day: str,
    policy_label: str,
    policy_sha256: str,
) -> str:
    result_key(symbol, day, policy_label, policy_sha256)
    return f"{symbol}_{day}_{policy_label}_{policy_sha256}.log"


def validate_resumed_run_meta(
    existing_meta: dict,
    candidate_meta: dict,
    *,
    expected_policy_sha256: str,
    expected_execution_scope_sha256: str,
) -> dict:
    if type(existing_meta) is not dict:
        raise ValueError("resumed run metadata must be an exact object")
    existing_policy_sha256 = strategy_policy_sha256(
        existing_meta.get("resolved_strategy_policy")
    )
    existing_execution_scope_sha256 = execution_scope_sha256(
        existing_meta.get("execution_scope")
    )
    existing_run_payload = {
        key: value
        for key, value in existing_meta.items()
        if key not in {"run_identity_sha256", "started_at", "stop_at"}
    }
    existing_run_identity_sha256 = hashlib.sha256(
        json.dumps(
            existing_run_payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        existing_meta.get("schema") != "chili.golden_replay_run_meta.v3"
        or existing_policy_sha256 != expected_policy_sha256
        or existing_meta.get("resolved_strategy_policy_sha256")
        != expected_policy_sha256
        or existing_execution_scope_sha256
        != expected_execution_scope_sha256
        or existing_meta.get("execution_scope_sha256")
        != expected_execution_scope_sha256
        or existing_meta.get("run_identity_sha256")
        != existing_run_identity_sha256
    ):
        raise ValueError("resumed run metadata authority is invalid")
    candidate_identity = {
        key: value
        for key, value in candidate_meta.items()
        if key not in {"started_at", "stop_at"}
    }
    existing_identity = {
        key: value
        for key, value in existing_meta.items()
        if key not in {"started_at", "stop_at"}
    }
    if json.dumps(
        candidate_identity,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) != json.dumps(
        existing_identity,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ):
        raise ValueError("resumed run metadata drifted")
    return existing_meta


def guard_postgres_url(
    url: str, *, role: str
) -> tuple[str, str, DatabaseIdentity]:
    try:
        identity = guarded_database_identity(url, sink=(role == "sink"))
    except ValueError as exc:
        raise SystemExit(f"[batch] REFUSING {role}: {exc}") from exc
    return url, identity.dbname, identity


def isolated_child_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in SAFE_PARENT_ENV_KEYS
    }
    # DETERMINISM (2026-08-02 TC-divergence follow-up): i-pin ang hash seed ng
    # bawat window subprocess — ang set/dict-hash iteration order ay hindi dapat
    # maging pinagmumulan ng run-to-run variance sa sealed replay. Hygiene ito
    # (ang na-root-cause na divergence ay ang tie-ambiguous ORDER BY, hindi ito),
    # pero sinasara nito ang natitirang nondeterminism channel ng child env.
    env["PYTHONHASHSEED"] = "0"
    return env


def confined_child_path(root: str, name: str) -> str:
    root_abs = os.path.abspath(root)
    path = os.path.abspath(os.path.join(root_abs, name))
    if os.path.commonpath([root_abs, path]) != root_abs:
        raise ValueError("path escapes the configured artifact root")
    return path


def v3_results_path(out_dir: str, override: str | None = None) -> str:
    expected = confined_child_path(
        os.path.abspath(out_dir),
        "results.jsonl",
    )
    if override is not None and os.path.abspath(override) != expected:
        raise ValueError(
            "v3 results must be <out-dir>/results.jsonl so logs stay bound"
        )
    return expected


def cache_receipt(cache_dir: str, window: dict) -> dict | None:
    if not window.get("prepend"):
        return None
    path = confined_child_path(
        cache_dir, f"{window['symbol']}_{window['day']}_1m.csv"
    )
    if not os.path.isfile(path):
        raise FileNotFoundError(f"required OHLCV cache is unavailable: {path}")
    with open(path, "rb") as f:
        raw = f.read()
    return {
        "name": os.path.basename(path),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def source_window_snapshot(
    source_url: str,
    expected: DatabaseIdentity,
    window: dict,
    *,
    deadline_at: datetime,
) -> dict:
    """Read one exact source snapshot under a bounded read-only transaction.

    The receipt hashes the same retained tick/NBBO superset mirrored by the
    child driver. Nonempty rows establish byte stability only; they do not
    establish causal completeness or executable coverage.
    """
    import psycopg2

    remaining_before_connect = (deadline_at - datetime.now()).total_seconds()
    if remaining_before_connect <= 3:
        raise TimeoutError("source verification deadline exhausted")
    conn = psycopg2.connect(
        source_url,
        connect_timeout=max(1, min(5, int(remaining_before_connect - 1))),
    )
    try:
        conn.set_session(
            readonly=True,
            autocommit=False,
            isolation_level="REPEATABLE READ",
        )
        with conn.cursor() as timeout_cur:
            timeout_cur.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                ("30000",),
            )
        verify_connected_endpoint(conn, expected)

        def bind_remaining_statement_timeout() -> None:
            remaining = (deadline_at - datetime.now()).total_seconds()
            if remaining <= 2:
                raise TimeoutError("source verification deadline exhausted")
            timeout_ms = int(min(60.0, remaining - 1.0) * 1000)
            with conn.cursor() as timeout_cur:
                timeout_cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(timeout_ms),),
                )

        with conn.cursor() as cur:
            cur.execute("SET LOCAL TIME ZONE 'UTC'")
            bind_remaining_statement_timeout()
            cur.execute(
                "SELECT current_database(), current_setting('transaction_read_only'), "
                "current_setting('transaction_isolation'), current_setting('TimeZone')"
            )
            name, read_only, isolation, time_zone = cur.fetchone()
        if str(name) != expected.dbname:
            raise RuntimeError("source database identity mismatch")
        if str(read_only).lower() not in {"on", "true"}:
            raise RuntimeError("source transaction is not read-only")
        if str(isolation).lower() != "repeatable read":
            raise RuntimeError("source transaction is not repeatable read")
        if str(time_zone).upper() not in {"UTC", "ETC/UTC"}:
            raise RuntimeError("source session timezone is not UTC")
        receipt = golden_window_content_receipt(
            conn,
            symbol=window["symbol"],
            start=window["ohlcv_start"],
            end=window["win_end"],
            before_query=bind_remaining_statement_timeout,
        )
        receipt_hash = content_receipt_sha256(receipt)
        bind_remaining_statement_timeout()
        with conn.cursor() as cur:
            cur.execute(
                MARKET_SQL,
                {
                    "s": window["symbol"],
                    "a": window["win_start"],
                    "b": window["win_end"],
                },
            )
            n, hi, lo, first_px, hi_at = cur.fetchone()
        if not n:
            market = {"win_ticks": 0}
        else:
            up_pct = (
                round(
                    (float(hi) - float(first_px))
                    / float(first_px)
                    * 100.0,
                    2,
                )
                if first_px
                else None
            )
            market = {
                "win_ticks": int(n),
                "first_px": float(first_px),
                "hi": float(hi),
                "lo": float(lo),
                "hi_at": str(hi_at),
                "up_pct": up_pct,
            }
        return {
            "source_content_receipt": receipt,
            "source_content_receipt_sha256": receipt_hash,
            "market": market,
        }
    finally:
        conn.rollback()
        conn.close()


def clean_build_sha() -> str:
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=BUILD,
        capture_output=True,
        text=True,
        check=True,
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=BUILD,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if dirty.strip():
        raise SystemExit("[batch] REFUSING dirty worktree; provenance requires a clean commit")
    return rev.stdout.strip()


def file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def verify_executable_state(
    expected_build_sha: str,
    expected_files: dict[str, str],
) -> None:
    if clean_build_sha() != expected_build_sha:
        raise RuntimeError("tracked source commit changed during batch")
    for path, expected_sha256 in expected_files.items():
        if file_sha256(path) != expected_sha256:
            raise RuntimeError(
                f"executed source bytes changed during batch: {path}"
            )


def load_diagnostic_manifest(path: str) -> tuple[dict, str]:
    with open(path, "rb") as f:
        raw = f.read()
    doc = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite,
        parse_float=_parse_finite_float,
    )
    if doc.get("schema") != "chili.replay-window-manifest.v2":
        raise SystemExit("[batch] REFUSING manifest: v2 sealed diagnostic contract required")
    if (
        doc.get("evidence_grade") != "DIAGNOSTIC_ONLY"
        or doc.get("causal_use_allowed") is not False
        or doc.get("ross_grade_credit_allowed") is not False
        or doc.get("source_backend_sealed") is not False
        or doc.get("child_source_snapshot_pinned") is not False
    ):
        raise SystemExit("[batch] REFUSING manifest: diagnostic-only provenance is incomplete")
    for field in (
        "generator_sha256",
        "receipt_helper_sha256",
        "query_contract_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", str(doc.get(field) or "")) is None:
            raise SystemExit(
                f"[batch] REFUSING manifest: invalid {field}"
            )
    if doc.get("query_contract_sha256") != query_contract_sha256():
        raise SystemExit(
            "[batch] REFUSING manifest: source query contract drift"
        )
    if not isinstance(doc.get("source_database_identity"), dict):
        raise SystemExit("[batch] REFUSING manifest: source identity is missing")
    windows = doc.get("windows")
    if not isinstance(windows, list):
        raise SystemExit("[batch] REFUSING manifest: windows must be a list")
    seen: set[tuple[str, str]] = set()
    required = {
        "symbol", "day", "tier", "class", "win_start", "win_end",
        "ohlcv_start", "window_source", "coverage_status",
        "source_content_receipt", "source_content_receipt_sha256",
        "source_content_status",
    }
    for i, window in enumerate(windows):
        if not isinstance(window, dict) or not required.issubset(window):
            raise SystemExit(f"[batch] REFUSING manifest: incomplete window at index {i}")
        symbol = window["symbol"]
        day = window["day"]
        if (
            not isinstance(symbol, str)
            or re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", symbol) is None
        ):
            raise SystemExit(f"[batch] REFUSING manifest: unsafe symbol at index {i}")
        if not isinstance(day, str):
            raise SystemExit(f"[batch] REFUSING manifest: invalid day at index {i}")
        try:
            parsed_day = datetime.fromisoformat(day)
        except ValueError as exc:
            raise SystemExit(
                f"[batch] REFUSING manifest: invalid day at index {i}"
            ) from exc
        if parsed_day.time() != datetime.min.time() or parsed_day.date().isoformat() != day:
            raise SystemExit(f"[batch] REFUSING manifest: non-canonical day at index {i}")
        if window["tier"] not in {"baseline", "library"}:
            raise SystemExit(f"[batch] REFUSING manifest: invalid tier at index {i}")
        if not isinstance(window["class"], str) or not window["class"].strip():
            raise SystemExit(f"[batch] REFUSING manifest: invalid class at index {i}")
        if window["window_source"] not in {
            "derived",
            "legacy_tieback_diagnostic",
        }:
            raise SystemExit(f"[batch] REFUSING manifest: invalid window source at index {i}")
        if type(window.get("prepend")) is not bool:
            raise SystemExit(f"[batch] REFUSING manifest: prepend must be boolean at index {i}")
        try:
            warmup = datetime.fromisoformat(str(window["ohlcv_start"]))
            start = datetime.fromisoformat(str(window["win_start"]))
            end = datetime.fromisoformat(str(window["win_end"]))
        except ValueError as exc:
            raise SystemExit(
                f"[batch] REFUSING manifest: invalid timestamp at index {i}"
            ) from exc
        if (
            any(value.tzinfo is not None for value in (warmup, start, end))
            or not (warmup <= start < end)
            or start.date().isoformat() != day
        ):
            raise SystemExit(f"[batch] REFUSING manifest: invalid window bounds at index {i}")
        receipt = window["source_content_receipt"]
        if window["source_content_status"] == "NOT_HASHED":
            if (
                receipt is not None
                or window["source_content_receipt_sha256"] is not None
                or window["coverage_status"] != "COVERAGE_UNAVAILABLE"
            ):
                raise SystemExit(
                    f"[batch] REFUSING manifest: invalid unhashed window at index {i}"
                )
            key = (symbol, day)
            if key in seen:
                raise SystemExit(f"[batch] REFUSING manifest: duplicate window {key}")
            seen.add(key)
            continue
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "chili.golden-window-content-receipt.v2"
            or receipt.get("query_contract_sha256")
            != query_contract_sha256()
            or receipt.get("symbol") != symbol
            or receipt.get("start") != window["ohlcv_start"]
            or receipt.get("end") != window["win_end"]
            or content_receipt_sha256(receipt)
            != window["source_content_receipt_sha256"]
        ):
            raise SystemExit(
                f"[batch] REFUSING manifest: invalid source receipt at index {i}"
            )
        rows_present = (
            int((receipt.get("ticks") or {}).get("bytes") or 0) > 0
            and int((receipt.get("nbbo") or {}).get("bytes") or 0) > 0
        )
        expected_content_status = (
            "CONTENT_HASHED" if rows_present else "ROWS_UNAVAILABLE"
        )
        expected_coverage = (
            "DIAGNOSTIC_ONLY"
            if int((receipt.get("ticks") or {}).get("bytes") or 0) > 0
            and int((receipt.get("nbbo") or {}).get("bytes") or 0) > 0
            else "COVERAGE_UNAVAILABLE"
        )
        if (
            window["source_content_status"] != expected_content_status
            or window["coverage_status"] != expected_coverage
        ):
            raise SystemExit(
                f"[batch] REFUSING manifest: coverage status mismatch at index {i}"
            )
        key = (symbol, day)
        if key in seen:
            raise SystemExit(f"[batch] REFUSING manifest: duplicate window {key}")
        seen.add(key)
    return doc, hashlib.sha256(raw).hexdigest()


def _reject_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate object key {key!r}")
        out[key] = value
    return out


def _reject_nonfinite(value: str):
    raise ValueError(f"non-finite number {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite number {value}")
    return parsed


def parse_driver_stdout(
    out: str,
    *,
    expected_symbol: str,
    expected_arm: str,
    expected_policy_sha256: str,
    expected_execution_scope_sha256: str,
) -> tuple[str, dict]:
    parsed: dict = {
        "fills": [],
        "final_state": None,
        "final_state_attestation_count": 0,
        "strategy_policy_label": None,
        "strategy_policy_sha256": None,
        "strategy_policy_attestation_count": 0,
        "execution_scope_label": None,
        "execution_scope_sha256": None,
        "execution_scope_attestation_count": 0,
    }
    try:
        canonical_expected_policy_sha256 = strategy_policy_sha256(
            resolve_strategy_policy(expected_arm)
        )
    except ValueError:
        return "parse_fail", parsed
    if expected_policy_sha256 != canonical_expected_policy_sha256:
        return "parse_fail", parsed
    try:
        canonical_expected_execution_scope_sha256 = execution_scope_sha256(
            replay_execution_scope()
        )
    except ValueError:
        return "parse_fail", parsed
    if (
        expected_execution_scope_sha256
        != canonical_expected_execution_scope_sha256
    ):
        return "parse_fail", parsed
    for line in out.splitlines():
        m = FILL_RE.fullmatch(line.strip())
        if m:
            try:
                qty = _parse_finite_float(m.group(2))
                px = _parse_finite_float(m.group(3))
            except ValueError:
                return "parse_fail", parsed
            if qty <= 0.0 or px <= 0.0:
                return "parse_fail", parsed
            parsed["fills"].append(
                {
                    "side": m.group(1).lower(),
                    "qty": qty,
                    "px": px,
                }
            )
        for fm in FINAL_RE.finditer(line):
            parsed["final_state_attestation_count"] += 1
            parsed["final_state"] = fm.group(1)
        pm = POLICY_ATTESTATION_RE.match(line.strip())
        if pm:
            parsed["strategy_policy_attestation_count"] += 1
            observed = (pm.group(1), pm.group(2))
            previous = (
                parsed["strategy_policy_label"],
                parsed["strategy_policy_sha256"],
            )
            if previous != (None, None) and previous != observed:
                return "parse_fail", parsed
            (
                parsed["strategy_policy_label"],
                parsed["strategy_policy_sha256"],
            ) = observed
        em = EXECUTION_SCOPE_ATTESTATION_RE.match(line.strip())
        if em:
            parsed["execution_scope_attestation_count"] += 1
            observed = (em.group(1), em.group(2))
            previous = (
                parsed["execution_scope_label"],
                parsed["execution_scope_sha256"],
            )
            if previous != (None, None) and previous != observed:
                return "parse_fail", parsed
            (
                parsed["execution_scope_label"],
                parsed["execution_scope_sha256"],
            ) = observed
    summaries = [
        match
        for line in out.splitlines()
        if (match := SUMMARY_RE.fullmatch(line.strip())) is not None
    ]
    if len(summaries) != 1:
        return "parse_fail", parsed
    summary = summaries[0]
    try:
        pnl = _parse_finite_float(summary.group(3))
        entries = int(summary.group(4))
        exits = int(summary.group(5))
    except (ValueError, OverflowError):
        return "parse_fail", parsed
    parsed.update(
        {
            "arm": summary.group(1),
            "symbol": summary.group(2),
            "pnl": pnl,
            "entries": entries,
            "exits": exits,
        }
    )
    if (
        parsed["symbol"] != expected_symbol
        or parsed["arm"] != expected_arm
        or parsed["strategy_policy_label"] != expected_arm
        or parsed["strategy_policy_sha256"] != expected_policy_sha256
        or parsed["strategy_policy_attestation_count"] != 1
        or parsed["execution_scope_label"] != "post-selection-fsm"
        or parsed["execution_scope_sha256"]
        != expected_execution_scope_sha256
        or parsed["execution_scope_attestation_count"] != 1
        or parsed["final_state_attestation_count"] != 1
        or sum(fill["side"] == "buy" for fill in parsed["fills"])
        != parsed["entries"]
        or sum(fill["side"] == "sell" for fill in parsed["fills"])
        != parsed["exits"]
    ):
        return "parse_fail", parsed
    return "ok", parsed


def normalize_child_strategy_policy_attestation(
    parsed: dict,
    *,
    expected_arm: str,
    expected_policy_sha256: str,
) -> tuple[str | None, str | None, int]:
    """Retain only an exact child attestation; the hash-bound log keeps rejects."""

    observed = (
        parsed.get("strategy_policy_label"),
        parsed.get("strategy_policy_sha256"),
        parsed.get("strategy_policy_attestation_count"),
    )
    expected = (expected_arm, expected_policy_sha256, 1)
    return expected if observed == expected else (None, None, 0)


def normalize_child_execution_scope_attestation(
    parsed: dict,
    *,
    expected_execution_scope_sha256: str,
) -> tuple[str | None, str | None, int]:
    """Retain only the canonical child execution-scope attestation."""

    observed = (
        parsed.get("execution_scope_label"),
        parsed.get("execution_scope_sha256"),
        parsed.get("execution_scope_attestation_count"),
    )
    expected = (
        "post-selection-fsm",
        expected_execution_scope_sha256,
        1,
    )
    return expected if observed == expected else (None, None, 0)


def replay_fill_inventory_is_flat(fills: list[dict]) -> bool:
    bought = sum(
        float(fill["qty"])
        for fill in fills
        if fill.get("side") == "buy"
    )
    sold = sum(
        float(fill["qty"])
        for fill in fills
        if fill.get("side") == "sell"
    )
    # The driver's FILL lines quantize each qty at 1e-10 (_fmt_fill_qty in
    # replay_ab_dark_flags.py — the mock's volume-capped partials are fractional
    # shares), so the parsed sums can drift from the raw inventory by up to
    # 5e-11 per fill. Budget exactly that; a REAL inventory leak is at least one
    # venue base increment — orders of magnitude above this bound.
    tolerance = max(1e-9, 5e-11 * len(fills))
    return math.isclose(bought, sold, rel_tol=0.0, abs_tol=tolerance)


def normalize_sink_fill_event(ts, event_type: str, payload, *, event_id=None) -> dict:
    """Normalize known FSM fill payload fields without inventing cycle lineage.

    E1 (fill-lineage): when a ``canonical_exit`` carries the complete
    self-contained clock contract (source_event_id + entry_filled_at_utc +
    filled_at_utc + quantity + price + identity), coverage is GRANTED — the
    exit binds to its entry fill by shared ID, no positional inference. Any
    missing element still fails closed exactly as before. ``event_id`` is the
    sink row id of THIS event so exits can be joined to entry records.
    """

    body = payload if isinstance(payload, dict) else {}

    def positive_number(key):
        value = body.get(key)
        if isinstance(value, bool) or value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0.0 else None

    def text_value(key):
        value = body.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def aware_utc(key):
        raw = text_value(key)
        if raw is None:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    if event_type == "live_entry_filled":
        quantity = positive_number("filled_size")
        price = positive_number("avg")
        identity = text_value("order_id")
        trigger_reason = text_value("trigger_reason") or text_value("entry_reason")
        exit_reason, contract = None, "canonical_entry"
    elif event_type == "live_exit_filled":
        quantity = positive_number("quantity")
        price = positive_number("fill_price")
        identity = text_value("order_id")
        trigger_reason = None
        exit_reason = text_value("exit_reason") or text_value("reason")
        contract = "canonical_exit"
    elif event_type == "live_exit_fill":
        quantity = price = identity = trigger_reason = None
        exit_reason = text_value("exit_reason") or text_value("reason")
        contract = "legacy_exit_alias"
    else:
        raise ValueError(f"unsupported sink fill event {event_type!r}")

    # Only the orphan-reconcile producer currently emits a self-contained
    # broker-fill clock contract. Normal FSM ``ts`` is a processing wall clock.
    source_event_id = body.get("source_event_id")
    entry_fill_at = aware_utc("entry_filled_at_utc")
    fill_at = aware_utc("filled_at_utc")
    if not (
        contract == "canonical_exit"
        and isinstance(source_event_id, int)
        and not isinstance(source_event_id, bool)
        and source_event_id > 0
        and entry_fill_at is not None
        and fill_at is not None
        and fill_at >= entry_fill_at
        and quantity is not None
        and price is not None
        and identity is not None
    ):
        provider_or_broker_fill_at = None
    else:
        provider_or_broker_fill_at = (
            fill_at.isoformat().replace("+00:00", "Z")
        )

    missing = [
        name
        for name, value in (
            ("quantity", quantity),
            ("price", price),
            ("identity", identity),
            ("fill_clock", provider_or_broker_fill_at),
        )
        if value is None
    ]
    if missing:
        coverage_status = "COVERAGE_UNAVAILABLE"
        reason = f"{contract}_{'_'.join(missing)}_unavailable"
    elif contract == "canonical_exit":
        # Complete lineage: shared entry event id + aware entry/exit fill clocks
        # (window time under the replay clock) + qty/price/identity — the bound
        # cycle the scorecard's Label A engine consumes.
        coverage_status = "COVERAGE_GRANTED"
        reason = "entry_exit_cycle_lineage_bound"
    else:
        coverage_status = "COVERAGE_UNAVAILABLE"
        reason = "immutable_entry_exit_cycle_lineage_unavailable"
    if contract == "legacy_exit_alias":
        reason = f"legacy_exit_alias_diagnostic_only:{reason}"

    return {
        "ts": ts,
        "event_type": event_type,
        "event_id": (
            int(event_id)
            if isinstance(event_id, int) and not isinstance(event_id, bool)
            else None
        ),
        "qty": quantity,
        "px": price,
        "fill_identity": identity,
        "trigger_reason": trigger_reason,
        "exit_reason": exit_reason,
        "source_event_id": (
            source_event_id
            if isinstance(source_event_id, int)
            and not isinstance(source_event_id, bool)
            and source_event_id > 0
            else None
        ),
        "entry_filled_at_utc": (
            entry_fill_at.isoformat().replace("+00:00", "Z")
            if entry_fill_at is not None
            else None
        ),
        "provider_or_broker_fill_at": provider_or_broker_fill_at,
        "coverage_status": coverage_status,
        "coverage_reason": reason,
    }


def mine_sink(
    sink_url: str,
    expected: DatabaseIdentity,
    symbol: str,
    deadline_at: datetime,
) -> dict:
    """Read-only sink mining — MUST run before the next driver run (it DELETEs boards)."""
    import psycopg2

    out: dict = {}
    conn = None
    try:
        remaining = (deadline_at - datetime.now()).total_seconds()
        if remaining <= 3:
            raise TimeoutError("sink mining deadline exhausted")
        conn = psycopg2.connect(
            sink_url,
            connect_timeout=max(1, min(5, int(remaining - 1))),
        )
        conn.set_session(
            readonly=True,
            autocommit=False,
            isolation_level="REPEATABLE READ",
        )
        with conn.cursor() as timeout_cur:
            timeout_cur.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                ("30000",),
            )
        verify_connected_endpoint(conn, expected)

        def execute(cur, query, params=None):
            query_remaining = (deadline_at - datetime.now()).total_seconds()
            if query_remaining <= 2:
                raise TimeoutError("sink mining deadline exhausted")
            cur.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(min(30.0, query_remaining - 1.0) * 1000)),),
            )
            cur.execute(query, params)

        cur = conn.cursor()
        cur.execute("SET LOCAL TIME ZONE 'UTC'")
        execute(
            cur,
            "SELECT max(id) FROM trading_automation_sessions WHERE symbol = %s",
            (symbol,),
        )
        row = cur.fetchone()
        sid = row[0] if row else None
        out["session_id"] = sid
        if sid is None:
            conn.close()
            return out
        execute(
            cur,
            "SELECT id, session_id, ts, event_type, payload_json "
            "FROM trading_automation_events WHERE session_id = %s ORDER BY id ASC",
            (sid,),
        )
        rows = [{"id": r[0], "session_id": r[1], "ts": r[2], "event_type": r[3],
                 "payload_json": r[4]} for r in cur.fetchall()]
        # the PURE audit fn (not the limit=1000 wrapper) — a 2h window emits thousands of events
        sys.path.insert(0, BUILD)
        from app.services.trading.momentum_neural.setup_trace_audit import audit_setup_trace_events
        if datetime.now() >= deadline_at:
            raise TimeoutError("sink mining deadline exhausted before audit")
        rep = audit_setup_trace_events(rows)
        if datetime.now() >= deadline_at:
            raise TimeoutError("sink mining deadline exhausted during audit")
        lc = rep.lifecycle_summary if isinstance(rep.lifecycle_summary, dict) else {}
        out["setup_trace"] = {
            "events_seen": rep.events_seen, "traces_seen": rep.traces_seen,
            "findings_count": len(rep.findings),
            "event_type_counts": lc.get("event_type_counts", {}),
            "trace_alias_counts": lc.get("trace_alias_counts", {}),
            "wait_reason_counts": lc.get("wait_reason_counts", {}),
            "issue_counts": lc.get("issue_counts", {}),
        }
        # top binding detector-rejects (SQL from scripts/nightly_replay_report.py:_top_rejects)
        execute(cur, """
            SELECT r.key || ':' || r.value AS reject, count(*)
            FROM trading_automation_events e,
                 jsonb_each_text(e.payload_json->'detector_rejects') r
            WHERE e.session_id = %s AND e.event_type = 'live_entry_trigger_wait'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """, (sid,))
        out["top_rejects"] = [[r[0], int(r[1])] for r in cur.fetchall()]
        # ``ts`` is normally the sink processing wall clock, not broker/event
        # time. Only an explicit, validated payload fill clock is retained as
        # such, and cycle lineage remains unavailable without a shared ID.
        execute(
            cur,
            "SELECT id, ts::text, event_type, payload_json "
            "FROM trading_automation_events "
            "WHERE session_id = %s AND event_type = ANY(%s) ORDER BY id ASC",
            (sid, list(SINK_FILL_EVENT_TYPES)),
        )
        out["fill_events"] = [
            normalize_sink_fill_event(ts, event_type, payload, event_id=row_id)
            for row_id, ts, event_type, payload in cur.fetchall()
        ]
    except Exception as exc:  # mining failure must never kill the batch
        out["mine_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None and not conn.closed:
            conn.rollback()
            conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True)
    ap.add_argument(
        "--out-dir",
        required=True,
        help=(
            "explicit fresh or matching-v3 artifact root; legacy v1/v2 roots "
            "are intentionally refused rather than migrated"
        ),
    )
    ap.add_argument(
        "--results",
        default=None,
        help=(
            "optional compatibility spelling; if supplied it must equal "
            "<out-dir>/results.jsonl"
        ),
    )
    ap.add_argument(
        "--source-database-url",
        required=True,
        help="explicit loopback PostgreSQL source; opened read-only for golden evidence",
    )
    ap.add_argument(
        "--sink-database-url",
        required=True,
        help="explicit loopback PostgreSQL replay sink whose database name ends in _test",
    )
    ap.add_argument(
        "--confirm-test-sink-reset",
        required=True,
        help=f"must equal {TEST_SINK_CONFIRMATION}",
    )
    ap.add_argument(
        "--ohlcv-cache-dir",
        required=True,
        help="local cache root; every PREPEND window must have a hashable CSV",
    )
    ap.add_argument(
        "--tiers",
        default="baseline",
        help=(
            "baseline only; library windows remain inventory-only until their "
            "exact source content is independently hashed"
        ),
    )
    ap.add_argument(
        "--arm",
        choices=STRATEGY_ARM_CHOICES,
        default=DEFAULT_ARM,
        help=(
            "post-selection FSM policy arm; defaults to the intended all-ON "
            "operator vector, but pre-selection-only one-flag arms are "
            "refused because this queued_live harness cannot execute them"
        ),
    )
    ap.add_argument("--equity", required=True, type=float)
    ap.add_argument("--risk-fraction", required=True, type=float)
    ap.add_argument("--exec-family", required=True)
    ap.add_argument(
        "--stop-at",
        required=True,
        help="explicit LOCAL naive ISO hard deadline — no window starts unless it fits",
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--window-timeout-seconds", type=int, default=3900,
        help="per-window subprocess timeout cap (default 3900; ang L8 park fix "
             "ay nagpapagising sa FSM sa buong late/AH tape kaya ang mga "
             "monster window ay maaaring mangailangan ng mas mahaba)",
    )
    ap.add_argument("--only", default=None,
                    help="comma list of SYMBOL|YYYY-MM-DD keys — run just these (smoke)")
    args = ap.parse_args()

    try:
        resolved_strategy_policy = resolve_strategy_policy(args.arm)
        resolved_strategy_policy_sha256 = strategy_policy_sha256(
            resolved_strategy_policy
        )
        require_scoreable_post_selection_arm(args.arm)
        resolved_execution_scope = replay_execution_scope()
        resolved_execution_scope_sha256 = execution_scope_sha256(
            resolved_execution_scope
        )
    except ValueError as exc:
        raise SystemExit(
            f"[batch] REFUSING strategy policy/execution scope: {exc}"
        ) from exc

    if args.confirm_test_sink_reset != TEST_SINK_CONFIRMATION:
        raise SystemExit("[batch] REFUSING disposable sink mutation without exact confirmation")
    if (
        not math.isfinite(args.equity)
        or not math.isfinite(args.risk_fraction)
        or args.equity <= 0
        or args.risk_fraction <= 0
    ):
        raise SystemExit("[batch] REFUSING non-positive equity or risk fraction")
    if re.fullmatch(r"[a-z0-9_.-]{1,64}", args.exec_family) is None:
        raise SystemExit("[batch] REFUSING invalid execution family")
    source, source_name, source_identity = guard_postgres_url(
        args.source_database_url, role="source"
    )
    sink, sink_name, sink_identity = guard_postgres_url(
        args.sink_database_url, role="sink"
    )
    if source_identity.server_key == sink_identity.server_key:
        raise SystemExit("[batch] REFUSING: source and sink URLs must differ")
    build_sha = clean_build_sha()
    stop_at = datetime.fromisoformat(args.stop_at)
    if stop_at.tzinfo is not None:
        raise SystemExit("[batch] REFUSING non-local STOP_AT timezone")
    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    if tiers != {"baseline"}:
        raise SystemExit(
            "[batch] COVERAGE_UNAVAILABLE: only the content-hashed baseline "
            "tier is runnable"
        )

    manifest, manifest_sha256 = load_diagnostic_manifest(args.manifest)
    current_generator_sha256 = file_sha256(DERIVER)
    current_receipt_helper_sha256 = file_sha256(RECEIPT_HELPER)
    if (
        manifest.get("build_sha") != build_sha
        or manifest.get("generator_sha256") != current_generator_sha256
        or manifest.get("receipt_helper_sha256")
        != current_receipt_helper_sha256
        or manifest.get("source_database_name") != source_name
        or manifest.get("source_database_identity") != source_identity.public_dict()
    ):
        raise SystemExit(
            "[batch] REFUSING manifest build/generator/source mismatch"
        )
    specs = [w for w in manifest["windows"] if w["tier"] in tiers]
    if args.only:
        want = {k.strip() for k in args.only.split(",") if k.strip()}
        specs = [
            w
            for w in specs
            if f"{w['symbol']}|{w['day']}" in want
        ]
    if args.limit is not None:
        if args.limit <= 0:
            raise SystemExit("[batch] REFUSING non-positive window limit")
        specs = specs[: args.limit]
    if not specs:
        raise SystemExit("[batch] COVERAGE_UNAVAILABLE: no selected windows")
    unavailable = [
        f"{window['symbol']}|{window['day']}"
        for window in specs
        if window["coverage_status"] != "DIAGNOSTIC_ONLY"
    ]
    if unavailable:
        raise SystemExit(
            "[batch] COVERAGE_UNAVAILABLE: selected windows are not content-hashed: "
            + ", ".join(unavailable)
        )
    cache_receipts: dict[str, dict] = {}
    for window in specs:
        try:
            receipt = cache_receipt(args.ohlcv_cache_dir, window)
        except FileNotFoundError as exc:
            raise SystemExit(f"[batch] COVERAGE_UNAVAILABLE: {exc}") from exc
        if receipt is not None:
            cache_receipts[f"{window['symbol']}|{window['day']}"] = receipt
    args.out_dir = os.path.abspath(args.out_dir)
    try:
        results_path = v3_results_path(args.out_dir, args.results)
    except ValueError as exc:
        raise SystemExit(f"[batch] REFUSING artifact root: {exc}") from exc
    logs_dir = os.path.join(args.out_dir, "logs")
    meta_path = os.path.join(args.out_dir, "meta.json")
    if os.path.exists(results_path) and not os.path.exists(meta_path):
        raise SystemExit("[batch] REFUSING orphan results without run metadata")

    done: set[str] = set()
    prior_records: list[tuple[int, dict]] = []
    if os.path.exists(results_path):
        with open(results_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                try:
                    rec = json.loads(
                        line,
                        object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=_reject_nonfinite,
                        parse_float=_parse_finite_float,
                    )
                except ValueError as exc:
                    raise SystemExit(
                        f"[batch] REFUSING malformed prior result at line {line_no}: {exc}"
                    ) from exc
                if not isinstance(rec, dict):
                    raise SystemExit(
                        f"[batch] REFUSING non-object prior result at line {line_no}"
                    )
                prior_records.append((line_no, rec))

    driver_sha256 = file_sha256(DRIVER)
    batch_sha256 = file_sha256(__file__)
    executable_files = {
        DRIVER: driver_sha256,
        __file__: batch_sha256,
        DERIVER: current_generator_sha256,
        RECEIPT_HELPER: current_receipt_helper_sha256,
    }
    selected_window_keys = [
        result_key(
            window["symbol"],
            window["day"],
            args.arm,
            resolved_strategy_policy_sha256,
        )
        for window in specs
    ]
    selected_window_receipts = {
        result_key(
            window["symbol"],
            window["day"],
            args.arm,
            resolved_strategy_policy_sha256,
        ): window["source_content_receipt_sha256"]
        for window in specs
    }
    selected_window_set_sha256 = hashlib.sha256(
        json.dumps(
            {
                "keys": selected_window_keys,
                "receipts": selected_window_receipts,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    meta = {"schema": "chili.golden_replay_run_meta.v3", "build_sha": build_sha,
            "driver": os.path.relpath(DRIVER, BUILD), "golden": True, "arm": args.arm,
            "resolved_strategy_policy": resolved_strategy_policy,
            "resolved_strategy_policy_sha256":
                resolved_strategy_policy_sha256,
            "execution_scope": resolved_execution_scope,
            "execution_scope_sha256": resolved_execution_scope_sha256,
            "driver_sha256": driver_sha256,
            "batch_sha256": batch_sha256,
            "generator_sha256": current_generator_sha256,
            "receipt_helper_sha256": current_receipt_helper_sha256,
            "manifest_sha256": manifest_sha256,
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
            "source_database_name": source_name,
            "source_database_identity": source_identity.public_dict(),
            "source_transaction": "PER_CONNECTION_REPEATABLE_READ_READ_ONLY",
            "sink_database_name": sink_name,
            "sink_database_identity": sink_identity.public_dict(),
            "prepend_cache_receipts": cache_receipts,
            "selected_window_keys": selected_window_keys,
            "selected_window_receipts": selected_window_receipts,
            "selected_window_set_sha256": selected_window_set_sha256,
            "selection_scope": (
                "explicit_partial"
                if args.only or args.limit is not None
                else "baseline_tier_complete"
            ),
            "selected_tiers": sorted(tiers),
            "equity": args.equity,
            "risk_fraction": args.risk_fraction,
            "risk_budget_usd": args.equity * args.risk_fraction,
            "execution_family": args.exec_family,
            "equity_exec": (
                f"explicit equity={args.equity} risk_fraction={args.risk_fraction} "
                f"execution_family={args.exec_family}"
            ),
            "stop_at": args.stop_at, "started_at": datetime.now().isoformat(timespec="seconds")}
    # STOP_AT is an operator scheduling boundary, not an economic/config input.
    # Excluding it lets a clean deadline stop resume under a later explicit
    # deadline while every semantic input remains hash-bound.
    run_identity_payload = {
        k: v for k, v in meta.items() if k not in {"started_at", "stop_at"}
    }
    meta["run_identity_sha256"] = hashlib.sha256(
        json.dumps(
            run_identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    spec_by_result_key = {
        result_key(
            window["symbol"],
            window["day"],
            args.arm,
            resolved_strategy_policy_sha256,
        ): window
        for window in specs
    }
    seen_prior_keys: set[str] = set()
    for line_no, rec in prior_records:
        key = rec.get("key")
        prior_window = spec_by_result_key.get(key)
        expected_log = (
            "logs/"
            + canonical_result_log_name(
                prior_window["symbol"],
                prior_window["day"],
                args.arm,
                resolved_strategy_policy_sha256,
            )
            if prior_window is not None
            else None
        )
        try:
            prior_policy_sha256 = strategy_policy_sha256(
                rec.get("resolved_strategy_policy")
            )
            prior_execution_scope_sha256 = execution_scope_sha256(
                rec.get("execution_scope")
            )
        except ValueError as exc:
            raise SystemExit(
                "[batch] REFUSING invalid prior policy/execution scope at line "
                f"{line_no}: {exc}"
            ) from exc
        child_attestation_count = rec.get(
            "child_strategy_policy_attestation_count"
        )
        child_execution_scope_attestation_count = rec.get(
            "child_execution_scope_attestation_count"
        )
        if (
            rec.get("schema") != "chili.golden_replay_window_result.v3"
            or not isinstance(key, str)
            or not key
            or prior_window is None
            or rec.get("build_sha") != build_sha
            or rec.get("driver_sha256") != driver_sha256
            or rec.get("batch_sha256") != batch_sha256
            or rec.get("generator_sha256")
            != current_generator_sha256
            or rec.get("receipt_helper_sha256")
            != current_receipt_helper_sha256
            or rec.get("manifest_sha256") != manifest_sha256
            or rec.get("run_identity_sha256")
            != meta["run_identity_sha256"]
            or rec.get("resolved_strategy_policy")
            != resolved_strategy_policy
            or prior_policy_sha256 != resolved_strategy_policy_sha256
            or rec.get("resolved_strategy_policy_sha256")
            != resolved_strategy_policy_sha256
            or rec.get("execution_scope") != resolved_execution_scope
            or prior_execution_scope_sha256
            != resolved_execution_scope_sha256
            or rec.get("execution_scope_sha256")
            != resolved_execution_scope_sha256
            or type(child_attestation_count) is not int
            or child_attestation_count not in {0, 1}
            or (
                (
                    rec.get("child_strategy_policy_label"),
                    rec.get("child_strategy_policy_sha256"),
                    child_attestation_count,
                )
                not in {
                    (None, None, 0),
                    (args.arm, resolved_strategy_policy_sha256, 1),
                }
            )
            or (
                rec.get("status") == "ok"
                and (
                    rec.get("child_strategy_policy_label") != args.arm
                    or rec.get("child_strategy_policy_sha256")
                    != resolved_strategy_policy_sha256
                    or child_attestation_count != 1
                )
            )
            or type(child_execution_scope_attestation_count) is not int
            or child_execution_scope_attestation_count not in {0, 1}
            or (
                (
                    rec.get("child_execution_scope_label"),
                    rec.get("child_execution_scope_sha256"),
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
                rec.get("status") == "ok"
                and (
                    rec.get("child_execution_scope_label")
                    != "post-selection-fsm"
                    or rec.get("child_execution_scope_sha256")
                    != resolved_execution_scope_sha256
                    or child_execution_scope_attestation_count != 1
                )
            )
            or rec.get("source_content_receipt_sha256")
            != prior_window["source_content_receipt_sha256"]
            or rec.get("arm") != args.arm
            or rec.get("status") not in RESULT_STATUSES
            or rec.get("golden") is not True
            or rec.get("evidence_grade") != "DIAGNOSTIC_ONLY"
            or rec.get("causal_use_allowed") is not False
            or rec.get("ross_grade_credit_allowed") is not False
            or rec.get("paper_policy_parity") is not False
            or rec.get("fees_slippage_complete") is not False
            or type(rec.get("prepend")) is not bool
            or (
                rec.get("status") == "ok"
                and any(
                    type(rec.get(field)) is not int
                    or rec.get(field) < 0
                    for field in ("entries", "exits")
                )
            )
            or key in seen_prior_keys
            or any(
                rec.get(field) != prior_window.get(field)
                for field in (
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
            )
            or rec.get("source_content_receipt_pre_sha256")
            != (
                prior_window["source_content_receipt_sha256"]
                if prior_window is not None
                else None
            )
            or rec.get("log") != expected_log
        ):
            raise SystemExit(
                f"[batch] REFUSING incompatible prior result at line {line_no}"
            )
        seen_prior_keys.add(key)
        prior_log_path = confined_child_path(args.out_dir, expected_log)
        if (
            not os.path.isfile(prior_log_path)
            or rec.get("log_sha256") != file_sha256(prior_log_path)
            or rec.get("log_size") != os.path.getsize(prior_log_path)
        ):
            raise SystemExit(
                f"[batch] REFUSING missing/drifted prior log at line {line_no}"
            )
        if rec.get("status") == "ok" and (
            rec.get("source_content_receipt_post_sha256")
            != prior_window["source_content_receipt_sha256"]
            or rec.get("source_verification_error") is not None
            or rec.get("executable_verification_error") is not None
        ):
            raise SystemExit(
                f"[batch] REFUSING unverified prior success at line {line_no}"
            )
        if rec.get("status") is not None:
            done.add(key)
    os.makedirs(logs_dir, exist_ok=True)
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            existing_meta = json.load(
                f,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
                parse_float=_parse_finite_float,
            )
        try:
            meta = validate_resumed_run_meta(
                existing_meta,
                meta,
                expected_policy_sha256=resolved_strategy_policy_sha256,
                expected_execution_scope_sha256=
                    resolved_execution_scope_sha256,
            )
        except ValueError as exc:
            raise SystemExit(
                f"[batch] REFUSING resume metadata: {exc}"
            ) from exc
    else:
        with open(meta_path, "x", encoding="utf-8") as f:
            json.dump(meta, f, indent=1, allow_nan=False)

    # ---- advisory lock: one replay batch at a time against this sink ----
    import psycopg2

    lock_conn = psycopg2.connect(sink, connect_timeout=5)
    lock_conn.autocommit = True
    try:
        with lock_conn.cursor() as timeout_cur:
            timeout_cur.execute("SET statement_timeout = '30s'")
        verify_connected_endpoint(lock_conn, sink_identity)
    except Exception:
        lock_conn.close()
        raise
    lc = lock_conn.cursor()
    lc.execute("SELECT pg_try_advisory_lock(hashtext('chili_replay_batch'))")
    if not lc.fetchone()[0]:
        lock_conn.close()
        raise SystemExit("[batch] another replay batch holds the sink advisory lock — aborting")

    print(f"[batch] {len(specs)} windows queued (tiers={sorted(tiers)}), "
          f"{len(done)} already done, sha={build_sha[:9]}, stop_at={args.stop_at}", flush=True)
    ran = 0
    try:
        for w in specs:
            key = result_key(
                w["symbol"],
                w["day"],
                args.arm,
                resolved_strategy_policy_sha256,
            )
            if key in done:
                continue
            log_name = canonical_result_log_name(
                w["symbol"],
                w["day"],
                args.arm,
                resolved_strategy_policy_sha256,
            )
            log_path = confined_child_path(logs_dir, log_name)
            if os.path.exists(log_path):
                raise SystemExit(
                    "[batch] REFUSING orphan log without durable result: "
                    f"{log_path}"
                )
            est = int(w.get("est_runtime_s") or 1800)
            remaining = (stop_at - datetime.now()).total_seconds()
            if 1.25 * est + 120 > remaining:
                print(f"[batch] DEADLINE — {key} needs ~{est}s, only {remaining:.0f}s left; "
                      f"stopping clean", flush=True)
                break
            try:
                pre_source = source_window_snapshot(
                    source,
                    source_identity,
                    w,
                    deadline_at=stop_at,
                )
            except Exception as exc:
                raise SystemExit(
                    "[batch] COVERAGE_UNAVAILABLE: bounded pre-run source "
                    f"verification failed for {key}: {type(exc).__name__}: {exc}"
                ) from exc
            if (
                pre_source["source_content_receipt"]
                != w["source_content_receipt"]
                or pre_source["source_content_receipt_sha256"]
                != w["source_content_receipt_sha256"]
            ):
                raise SystemExit(
                    "[batch] COVERAGE_UNAVAILABLE: source content changed before "
                    f"{key}; no replay launched"
                )
            try:
                verify_executable_state(build_sha, executable_files)
            except Exception as exc:
                raise SystemExit(
                    "[batch] REFUSING executable-state drift before "
                    f"{key}: {type(exc).__name__}: {exc}"
                ) from exc
            remaining = (stop_at - datetime.now()).total_seconds()
            if 1.25 * est + 120 > remaining:
                print(
                    f"[batch] DEADLINE — receipt verification left insufficient "
                    f"time for {key}; stopping clean",
                    flush=True,
                )
                break
            timeout = min(
                int(getattr(args, "window_timeout_seconds", 3900) or 3900),
                max(60, int(remaining - 120)),
            )
            env = isolated_child_env()
            prepend_receipt = cache_receipts.get(f"{w['symbol']}|{w['day']}")
            env.update({"SYMBOL": w["symbol"], "ARM": args.arm,
                        "CHILI_REPLAY_RESOLVED_STRATEGY_POLICY_JSON":
                            json.dumps(
                                resolved_strategy_policy,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                        "CHILI_REPLAY_RESOLVED_STRATEGY_POLICY_SHA256":
                            resolved_strategy_policy_sha256,
                        "CHILI_REPLAY_EXECUTION_SCOPE_JSON":
                            json.dumps(
                                resolved_execution_scope,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                        "CHILI_REPLAY_EXECUTION_SCOPE_SHA256":
                            resolved_execution_scope_sha256,
                        "WIN_START": w["win_start"], "WIN_END": w["win_end"],
                        "OHLCV_START": w["ohlcv_start"], "GOLDEN": "1",
                        "PREPEND_OHLCV": "1" if w.get("prepend") else "0",
                        "CHILI_REPLAY_PREPEND_CACHE_DIR": os.path.abspath(
                            args.ohlcv_cache_dir
                        ),
                        "CHILI_REPLAY_PREPEND_CACHE_SHA256": (
                            prepend_receipt["sha256"] if prepend_receipt else ""
                        ),
                        "REPLAY_SOURCE_DATABASE_URL": source,
                        "EQUITY": str(args.equity),
                        "RISK": str(args.equity * args.risk_fraction),
                        "EXEC_FAMILY": args.exec_family,
                        "ENTRY_DIAG": "1", "DATABASE_URL": sink, "TEST_DATABASE_URL": sink,
                        "PYTHONPATH": BUILD, "PYTHONUNBUFFERED": "1",
                        "PYTHONIOENCODING": "utf-8",
                        "CHILI_CAPTURED_PAPER_CONFIG_ISOLATED": "true",
                        "CHILI_DIAGNOSTIC_REPLAY_ISOLATED": "true",
                        "CHILI_REPLAY_EXPECTED_BUILD_SHA": build_sha,
                        "CHILI_REPLAY_EXPECTED_DRIVER_SHA256":
                            driver_sha256,
                        "CHILI_REPLAY_TEST_SINK_CONFIRMATION":
                            TEST_SINK_CONFIRMATION})
            t0 = time.time()
            print(f"[batch] RUN {key} ({w['ticks']:,}t, est {est // 60}min, timeout {timeout}s)",
                  flush=True)
            status = "ok"
            stdout = stderr = ""
            exit_code = None
            try:
                p = subprocess.run([sys.executable, DRIVER], env=env, cwd=BUILD,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout)
                stdout, stderr, exit_code = p.stdout or "", p.stderr or "", p.returncode
            except subprocess.TimeoutExpired as e:
                status = "timeout"
                stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
                stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
            with open(log_path, "x", encoding="utf-8") as f:
                f.write(stdout)
                if stderr:
                    f.write("\n===== STDERR =====\n" + stderr)
                f.flush()
                os.fsync(f.fileno())
            log_sha256 = file_sha256(log_path)
            log_size = os.path.getsize(log_path)
            if status == "ok":
                pstatus, parsed = parse_driver_stdout(
                    stdout,
                    expected_symbol=w["symbol"],
                    expected_arm=args.arm,
                    expected_policy_sha256=resolved_strategy_policy_sha256,
                    expected_execution_scope_sha256=
                        resolved_execution_scope_sha256,
                )
                if exit_code != 0:
                    status = "error"
                elif pstatus != "ok":
                    status = "parse_fail"
                    parsed = parsed or {}
                elif not replay_fill_inventory_is_flat(parsed["fills"]):
                    status = "coverage_unavailable"
            else:
                _, parsed = parse_driver_stdout(
                    stdout,
                    expected_symbol=w["symbol"],
                    expected_arm=args.arm,
                    expected_policy_sha256=resolved_strategy_policy_sha256,
                    expected_execution_scope_sha256=
                        resolved_execution_scope_sha256,
                )
            (
                child_policy_label,
                child_policy_sha256,
                child_policy_attestation_count,
            ) = normalize_child_strategy_policy_attestation(
                parsed,
                expected_arm=args.arm,
                expected_policy_sha256=resolved_strategy_policy_sha256,
            )
            (
                child_execution_scope_label,
                child_execution_scope_sha256,
                child_execution_scope_attestation_count,
            ) = normalize_child_execution_scope_attestation(
                parsed,
                expected_execution_scope_sha256=
                    resolved_execution_scope_sha256,
            )
            rec = {"schema": "chili.golden_replay_window_result.v3", "key": key,
                   "symbol": w["symbol"], "day": w["day"], "class": w["class"],
                   "tier": w["tier"], "win_start": w["win_start"], "win_end": w["win_end"],
                   "ohlcv_start": w["ohlcv_start"], "window_source": w["window_source"],
                   "prepend": bool(w.get("prepend")), "arm": args.arm, "golden": True,
                   "build_sha": build_sha,
                   "driver_sha256": driver_sha256,
                   "batch_sha256": batch_sha256,
                   "generator_sha256": current_generator_sha256,
                   "receipt_helper_sha256":
                       current_receipt_helper_sha256,
                   "manifest_sha256": manifest_sha256,
                   "run_identity_sha256": meta["run_identity_sha256"],
                   "resolved_strategy_policy": resolved_strategy_policy,
                   "resolved_strategy_policy_sha256":
                       resolved_strategy_policy_sha256,
                   "execution_scope": resolved_execution_scope,
                   "execution_scope_sha256":
                       resolved_execution_scope_sha256,
                   "child_strategy_policy_label":
                       child_policy_label,
                   "child_strategy_policy_sha256":
                       child_policy_sha256,
                   "child_strategy_policy_attestation_count":
                       child_policy_attestation_count,
                   "child_execution_scope_label":
                       child_execution_scope_label,
                   "child_execution_scope_sha256":
                       child_execution_scope_sha256,
                   "child_execution_scope_attestation_count":
                       child_execution_scope_attestation_count,
                   "source_content_receipt_sha256":
                       w["source_content_receipt_sha256"],
                   "source_content_receipt_pre_sha256":
                       pre_source["source_content_receipt_sha256"],
                   "evidence_grade": "DIAGNOSTIC_ONLY",
                   "causal_use_allowed": False,
                   "ross_grade_credit_allowed": False,
                   "paper_policy_parity": False,
                   "fees_slippage_complete": False,
                   "operator_stop_at": args.stop_at,
                   "started_at": datetime.fromtimestamp(t0).isoformat(
                       timespec="seconds"),
                   "duration_s": round(time.time() - t0, 1), "exit_code": exit_code,
                   "status": status,
                   "pnl": parsed.get("pnl"), "entries": parsed.get("entries"),
                   "exits": parsed.get("exits"), "final_state": parsed.get("final_state"),
                   "fills": parsed.get("fills", []),
                   "log": f"logs/{log_name}",
                   "log_sha256": log_sha256,
                   "log_size": log_size}
            rec["sink"] = mine_sink(
                sink,
                sink_identity,
                w["symbol"],
                stop_at,
            )  # BEFORE the next run's DELETE
            try:
                post_source = source_window_snapshot(
                    source,
                    source_identity,
                    w,
                    deadline_at=stop_at,
                )
            except Exception as exc:
                post_source = None
                rec["source_verification_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
            if post_source is None:
                rec["source_content_receipt_post_sha256"] = None
                rec["market"] = {
                    "stats_error": "post-run source verification unavailable"
                }
                rec["status"] = "coverage_unavailable"
            else:
                rec["source_content_receipt_post_sha256"] = post_source[
                    "source_content_receipt_sha256"
                ]
                rec["market"] = post_source["market"]
                if (
                    post_source["source_content_receipt"]
                    != w["source_content_receipt"]
                    or post_source["source_content_receipt_sha256"]
                    != w["source_content_receipt_sha256"]
                ):
                    rec["status"] = "coverage_unavailable"
                    rec["source_verification_error"] = (
                        "source content changed across replay"
                    )
            try:
                verify_executable_state(build_sha, executable_files)
            except Exception as exc:
                rec["status"] = "coverage_unavailable"
                rec["executable_verification_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
            if rec["status"] == "ok" and (
                rec["sink"].get("mine_error")
                or rec["sink"].get("session_id") is None
                or rec["market"].get("stats_error")
                or int(rec["market"].get("win_ticks") or 0) <= 0
            ):
                rec["status"] = "coverage_unavailable"
            with open(results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            ran += 1
            print(f"[batch] DONE {key} status={rec['status']} pnl={rec['pnl']} "
                  f"entries={rec['entries']} exits={rec['exits']} "
                  f"({rec['duration_s']:.0f}s)", flush=True)
    finally:
        lc.execute("SELECT pg_advisory_unlock(hashtext('chili_replay_batch'))")
        lock_conn.close()
    print(f"[batch] finished: {ran} windows this invocation; results -> {results_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
