"""Pure safety tests for the diagnostic golden replay library tooling."""
from __future__ import annotations

import ast
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
    assert batch.DEFAULT_ARM == "intended"


def _literal_assignment(relative: str, name: str):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{relative} does not define literal {name}")


def test_resolved_strategy_policy_binds_all_operator_flags():
    # 9 weekend levers + the two 2026-07-27 autopsy levers (chase-defer,
    # whipsaw-escalation) — the roster IS the per-lever A/B surface, so a new
    # approved lever lands here deliberately, never via FLAGS_JSON.
    pairs = batch.APPROVED_STRATEGY_FLAGS_BY_SLUG
    assert len(pairs) == 16
    assert len({slug for slug, _ in pairs}) == 16
    assert len({flag for _, flag in pairs}) == 16

    intended = batch.resolve_strategy_policy("intended")
    baseline = batch.resolve_strategy_policy("base")
    assert intended["schema"] == batch.STRATEGY_POLICY_SCHEMA
    assert intended["label"] == "intended"
    assert intended["flags"] == {flag: True for _, flag in pairs}
    assert baseline["flags"] == {flag: False for _, flag in pairs}
    assert (
        batch.strategy_policy_sha256(intended)
        != batch.strategy_policy_sha256(baseline)
    )

    hashes = {batch.strategy_policy_sha256(intended)}
    for slug, flag in pairs:
        arm = f"intended-minus-{slug}"
        doc = batch.resolve_strategy_policy(arm)
        assert doc["label"] == arm
        assert doc["flags"][flag] is False
        assert sum(value is False for value in doc["flags"].values()) == 1
        hashes.add(batch.strategy_policy_sha256(doc))
    assert len(hashes) == 17

    # COMPOUND arms stay CLOSED: exactly one operator-approved multi-flag-off
    # vector (the 2026-07-27 parity arm), each named flag drawn from the
    # approved roster, flipping EXACTLY its declared flags and nothing else.
    assert set(batch.COMPOUND_STRATEGY_ARMS) == {"intended-minus-autopsy-0727"}
    approved_flags = {flag for _, flag in pairs}
    for arm, off_flags in batch.COMPOUND_STRATEGY_ARMS.items():
        assert arm in batch.STRATEGY_ARM_CHOICES
        assert set(off_flags) <= approved_flags
        doc = batch.resolve_strategy_policy(arm)
        assert doc["label"] == arm
        assert {f for f, v in doc["flags"].items() if v is False} == set(off_flags)
        hashes.add(batch.strategy_policy_sha256(doc))
    parity = batch.resolve_strategy_policy("intended-minus-autopsy-0727")
    assert parity["flags"]["chili_momentum_chase_defer_enabled"] is False
    assert parity["flags"]["chili_momentum_whipsaw_rapid_escalation_enabled"] is False
    assert sum(v is False for v in parity["flags"].values()) == 2
    batch.require_scoreable_post_selection_arm("intended-minus-autopsy-0727")
    assert len(hashes) == 18


@pytest.mark.parametrize(
    ("arm", "replacement"),
    (
        ("intended", 1),
        ("intended", 1.0),
        ("base", 0),
        ("base", 0.0),
    ),
)
def test_strategy_policy_rejects_numeric_boolean_aliases(arm, replacement):
    policy = batch.resolve_strategy_policy(arm)
    first_flag = batch.APPROVED_STRATEGY_FLAGS_BY_SLUG[0][1]
    policy["flags"][first_flag] = replacement
    with pytest.raises(ValueError, match="exact boolean vector"):
        batch.strategy_policy_sha256(policy)


def test_driver_and_batch_use_the_same_closed_strategy_policy_allowlist():
    driver_pairs = _literal_assignment(
        "scripts/replay_ab_dark_flags.py",
        "APPROVED_STRATEGY_FLAGS_BY_SLUG",
    )
    assert tuple(driver_pairs) == batch.APPROVED_STRATEGY_FLAGS_BY_SLUG
    driver_compound = _literal_assignment(
        "scripts/replay_ab_dark_flags.py",
        "COMPOUND_STRATEGY_ARMS",
    )
    assert dict(driver_compound) == batch.COMPOUND_STRATEGY_ARMS


def test_replay_execution_scope_is_hash_bound_and_refuses_upstream_arms():
    scope = batch.replay_execution_scope()
    assert scope["schema"] == batch.EXECUTION_SCOPE_SCHEMA
    assert scope["pipeline_start_state"] == "queued_live"
    assert scope["selection_pipeline_executed"] is False
    assert scope["entry_risk_gate_executed"] is False
    assert scope["whole_policy_profitability_allowed"] is False
    assert scope["quote_freshness_clock_mode"] == "replay_sim"
    assert scope["neutralized_settings"] == {
        "chili_momentum_squeeze_fuel_tilt_enabled": False,
    }
    assert set(scope["scoreable_policy_flags"]) == set(
        batch.POST_SELECTION_SCOREABLE_POLICY_FLAGS
    )
    assert set(scope["unscoreable_policy_flags"]) == set(
        batch.POST_SELECTION_UNSCOREABLE_POLICY_FLAGS
    )
    assert len(batch.execution_scope_sha256(scope)) == 64

    driver_scoreable = _literal_assignment(
        "scripts/replay_ab_dark_flags.py",
        "POST_SELECTION_SCOREABLE_POLICY_FLAGS",
    )
    driver_unscoreable = _literal_assignment(
        "scripts/replay_ab_dark_flags.py",
        "POST_SELECTION_UNSCOREABLE_POLICY_FLAGS",
    )
    driver_unscoreable_arms = _literal_assignment(
        "scripts/replay_ab_dark_flags.py",
        "UNSCOREABLE_POST_SELECTION_ARMS",
    )
    assert tuple(driver_scoreable) == batch.POST_SELECTION_SCOREABLE_POLICY_FLAGS
    assert (
        tuple(driver_unscoreable)
        == batch.POST_SELECTION_UNSCOREABLE_POLICY_FLAGS
    )
    assert tuple(driver_unscoreable_arms) == batch.UNSCOREABLE_POST_SELECTION_ARMS
    for arm in batch.UNSCOREABLE_POST_SELECTION_ARMS:
        with pytest.raises(ValueError, match="pre-selection flag"):
            batch.require_scoreable_post_selection_arm(arm)
    batch.require_scoreable_post_selection_arm("intended")
    batch.require_scoreable_post_selection_arm(
        "intended-minus-orb-ihs-stop"
    )

    driver_source = (
        ROOT / "scripts" / "replay_ab_dark_flags.py"
    ).read_text(encoding="utf-8")
    assert (
        'setattr(settings, _setting, _value)'
        in driver_source
    )
    assert "canonical != expected_canonical" in driver_source
    assert "policy[\"label\"] in UNSCOREABLE_POST_SELECTION_ARMS" in driver_source
    assert 'freshness_mode="sim"' in driver_source
    assert 'freshness_mode="wall"' not in driver_source


def test_strategy_policy_hash_is_part_of_every_result_key():
    intended = batch.resolve_strategy_policy("intended")
    baseline = batch.resolve_strategy_policy("base")
    intended_hash = batch.strategy_policy_sha256(intended)
    baseline_hash = batch.strategy_policy_sha256(baseline)
    intended_key = batch.result_key(
        "AAA",
        "2026-07-07",
        intended["label"],
        intended_hash,
    )
    baseline_key = batch.result_key(
        "AAA",
        "2026-07-07",
        baseline["label"],
        baseline_hash,
    )
    assert intended_hash in intended_key
    assert baseline_hash in baseline_key
    assert intended_key != baseline_key
    with pytest.raises(ValueError, match="label/hash mismatch"):
        batch.result_key(
            "AAA",
            "2026-07-07",
            intended["label"],
            baseline_hash,
        )
    with pytest.raises(ValueError, match="calendar date"):
        batch.result_key(
            "AAA",
            "2026-99-99",
            intended["label"],
            intended_hash,
        )


def test_resumed_run_meta_rehashes_policy_and_run_identity_strictly():
    policy = batch.resolve_strategy_policy("intended")
    policy_sha256 = batch.strategy_policy_sha256(policy)
    scope = batch.replay_execution_scope()
    scope_sha256 = batch.execution_scope_sha256(scope)
    candidate = {
        "schema": "chili.golden_replay_run_meta.v3",
        "arm": "intended",
        "resolved_strategy_policy": policy,
        "resolved_strategy_policy_sha256": policy_sha256,
        "execution_scope": scope,
        "execution_scope_sha256": scope_sha256,
        "build_sha": "b" * 40,
        "started_at": "2026-07-26T12:00:00",
        "stop_at": "2026-07-26T18:00:00",
    }
    run_payload = {
        key: value
        for key, value in candidate.items()
        if key not in {"run_identity_sha256", "started_at", "stop_at"}
    }
    candidate["run_identity_sha256"] = hashlib.sha256(
        json.dumps(
            run_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    existing = json.loads(json.dumps(candidate))
    existing["started_at"] = "2026-07-26T11:59:59"
    assert batch.validate_resumed_run_meta(
        existing,
        candidate,
        expected_policy_sha256=policy_sha256,
        expected_execution_scope_sha256=scope_sha256,
    ) == existing

    numeric_policy = json.loads(json.dumps(existing))
    first_flag = batch.APPROVED_STRATEGY_FLAGS_BY_SLUG[0][1]
    numeric_policy["resolved_strategy_policy"]["flags"][first_flag] = 1
    with pytest.raises(ValueError, match="exact boolean vector"):
        batch.validate_resumed_run_meta(
            numeric_policy,
            candidate,
            expected_policy_sha256=policy_sha256,
            expected_execution_scope_sha256=scope_sha256,
        )

    numeric_scope = json.loads(json.dumps(existing))
    numeric_scope["execution_scope"][
        "whole_policy_profitability_allowed"
    ] = 0
    with pytest.raises(ValueError, match="canonical closed document"):
        batch.validate_resumed_run_meta(
            numeric_scope,
            candidate,
            expected_policy_sha256=policy_sha256,
            expected_execution_scope_sha256=scope_sha256,
        )

    stale_identity = json.loads(json.dumps(existing))
    stale_identity["build_sha"] = "c" * 40
    with pytest.raises(ValueError, match="authority is invalid"):
        batch.validate_resumed_run_meta(
            stale_identity,
            candidate,
            expected_policy_sha256=policy_sha256,
            expected_execution_scope_sha256=scope_sha256,
        )

    legacy_meta = json.loads(json.dumps(existing))
    legacy_meta["schema"] = "chili.golden_replay_run_meta.v2"
    with pytest.raises(ValueError, match="authority is invalid"):
        batch.validate_resumed_run_meta(
            legacy_meta,
            candidate,
            expected_policy_sha256=policy_sha256,
            expected_execution_scope_sha256=scope_sha256,
        )


def test_driver_stdout_policy_attestation_is_exact_and_conflict_detected():
    policy = batch.resolve_strategy_policy("intended")
    policy_sha256 = batch.strategy_policy_sha256(policy)
    scope_sha256 = batch.execution_scope_sha256(
        batch.replay_execution_scope()
    )

    parse = lambda out: batch.parse_driver_stdout(  # noqa: E731
        out,
        expected_symbol="AAA",
        expected_arm="intended",
        expected_policy_sha256=policy_sha256,
        expected_execution_scope_sha256=scope_sha256,
    )

    def output(
        *,
        policy_lines=None,
        scope_lines=None,
        summary=None,
        fills=None,
        states=None,
    ):
        return "\n".join(
            [
                *(
                    policy_lines
                    if policy_lines is not None
                    else [
                        f"[STRATEGY_POLICY=intended] sha256={policy_sha256}"
                    ]
                ),
                *(
                    scope_lines
                    if scope_lines is not None
                    else [
                        "[EXECUTION_SCOPE=post-selection-fsm] "
                        f"sha256={scope_sha256}"
                    ]
                ),
                *(states if states is not None else ["final_state=flat"]),
                *(fills if fills is not None else ["BUY 1 @ 5.00", "SELL 1 @ 6.00"]),
                summary
                or "[ARM=intended] AAA PnL +12.34 entries=1 exits=1",
            ]
        )

    status, parsed = parse(output())
    assert status == "ok"
    assert parsed["strategy_policy_label"] == "intended"
    assert parsed["strategy_policy_sha256"] == policy_sha256
    assert parsed["execution_scope_label"] == "post-selection-fsm"
    assert parsed["execution_scope_sha256"] == scope_sha256

    status, _ = parse(output(policy_lines=[]))
    assert status == "parse_fail"
    status, _ = parse(output(scope_lines=[]))
    assert status == "parse_fail"
    status, _ = parse(
        output(
            scope_lines=[
                "[EXECUTION_SCOPE=post-selection-fsm] "
                f"sha256={scope_sha256}",
                "[EXECUTION_SCOPE=post-selection-fsm] "
                f"sha256={scope_sha256}",
            ]
        )
    )
    assert status == "parse_fail"
    status, _ = parse(
        output(
            scope_lines=[
                "[EXECUTION_SCOPE=post-selection-fsm] "
                f"sha256={scope_sha256}",
                f"[EXECUTION_SCOPE=other] sha256={'0' * 64}",
            ]
        )
    )
    assert status == "parse_fail"

    status, _ = parse(
        output(summary="[ARM=base] AAA PnL +12.34 entries=1 exits=1")
    )
    assert status == "parse_fail"

    status, _ = parse(output(summary="[ARM=intended] WRONG PnL +12.34 entries=1 exits=1"))
    assert status == "parse_fail"

    status, _ = parse(
        output(
            summary="\n".join(
                [
                    "[ARM=intended] AAA PnL +12.34 entries=1 exits=1",
                    "[ARM=intended] AAA PnL +99.99 entries=1 exits=1",
                ]
            )
        )
    )
    assert status == "parse_fail"

    status, _ = parse(
        output(
            policy_lines=[
                f"[STRATEGY_POLICY=intended] sha256={policy_sha256}",
                f"[STRATEGY_POLICY=intended] sha256={policy_sha256}",
            ]
        )
    )
    assert status == "parse_fail"

    status, _ = parse(
        output(
            policy_lines=[
                f"[STRATEGY_POLICY=intended] sha256={policy_sha256}",
                f"[STRATEGY_POLICY=base] sha256={'0' * 64}",
            ]
        )
    )
    assert status == "parse_fail"

    status, _ = parse(
        output(
            fills=["BUY 1 @ 5.00"],
            summary="[ARM=intended] AAA PnL +12.34 entries=1 exits=1",
        )
    )
    assert status == "parse_fail"

    open_status, open_parsed = parse(
        output(
            fills=["BUY 1 @ 5.00"],
            summary="[ARM=intended] AAA PnL -5.00 entries=1 exits=0",
        )
    )
    assert open_status == "ok"
    assert batch.replay_fill_inventory_is_flat(open_parsed["fills"]) is False

    for malformed_output in (
        output(
            fills=["BUY . @ 5.00"],
            summary="[ARM=intended] AAA PnL -5.00 entries=1 exits=0",
        ),
        output(
            fills=["BUY 0 @ 5.00"],
            summary="[ARM=intended] AAA PnL +0.00 entries=1 exits=0",
        ),
        output(
            fills=[f"BUY {'9' * 400} @ 5.00"],
            summary="[ARM=intended] AAA PnL +0.00 entries=1 exits=0",
        ),
        output(
            summary="[ARM=intended] AAA PnL . entries=1 exits=1",
        ),
    ):
        malformed_status, _ = parse(malformed_output)
        assert malformed_status == "parse_fail"

    status, _ = parse(output(states=[]))
    assert status == "parse_fail"

    status, _ = parse(
        output(states=["final_state=entered", "final_state=flat"])
    )
    assert status == "parse_fail"

    forged_expected_status, _ = batch.parse_driver_stdout(
        output(),
        expected_symbol="AAA",
        expected_arm="intended",
        expected_policy_sha256="0" * 64,
        expected_execution_scope_sha256=scope_sha256,
    )
    assert forged_expected_status == "parse_fail"
    forged_scope_status, _ = batch.parse_driver_stdout(
        output(),
        expected_symbol="AAA",
        expected_arm="intended",
        expected_policy_sha256=policy_sha256,
        expected_execution_scope_sha256="0" * 64,
    )
    assert forged_scope_status == "parse_fail"

    assert batch.normalize_child_strategy_policy_attestation(
        {
            "strategy_policy_label": "intended",
            "strategy_policy_sha256": policy_sha256,
            "strategy_policy_attestation_count": 2,
        },
        expected_arm="intended",
        expected_policy_sha256=policy_sha256,
    ) == (None, None, 0)
    assert batch.normalize_child_execution_scope_attestation(
        {
            "execution_scope_label": "post-selection-fsm",
            "execution_scope_sha256": scope_sha256,
            "execution_scope_attestation_count": 2,
        },
        expected_execution_scope_sha256=scope_sha256,
    ) == (None, None, 0)


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
    # E1 (fill-lineage): a COMPLETE self-contained clock contract now GRANTS
    # coverage — this is the deliberate relaxation the FSM lineage emit enables.
    # Every incomplete/contradictory variant below still fails closed.
    assert orphan["coverage_status"] == "COVERAGE_GRANTED"
    assert orphan["coverage_reason"] == "entry_exit_cycle_lineage_bound"
    assert orphan["source_event_id"] == 42
    assert orphan["entry_filled_at_utc"] == "2026-07-27T13:31:00Z"

    # NEW fail-closed cases paired with the relaxation (sealed posture kept):
    naive_entry_clock = batch.normalize_sink_fill_event(
        "2026-07-27T13:34:01+00:00",
        "live_exit_filled",
        {
            "reason": "trail_stop",
            "fill_price": 5.70,
            "quantity": 120,
            "order_id": "exit-order-n1",
            "source_event_id": 44,
            "entry_filled_at_utc": "2026-07-27T13:31:00",  # NAIVE -> rejected
            "filled_at_utc": "2026-07-27T13:34:00Z",
        },
    )
    assert naive_entry_clock["coverage_status"] == "COVERAGE_UNAVAILABLE"

    backwards_clock = batch.normalize_sink_fill_event(
        "2026-07-27T13:34:01+00:00",
        "live_exit_filled",
        {
            "reason": "trail_stop",
            "fill_price": 5.70,
            "quantity": 120,
            "order_id": "exit-order-n2",
            "source_event_id": 45,
            "entry_filled_at_utc": "2026-07-27T13:35:00Z",  # exit BEFORE entry
            "filled_at_utc": "2026-07-27T13:34:00Z",
        },
    )
    assert backwards_clock["coverage_status"] == "COVERAGE_UNAVAILABLE"

    bool_source_id = batch.normalize_sink_fill_event(
        "2026-07-27T13:34:01+00:00",
        "live_exit_filled",
        {
            "reason": "trail_stop",
            "fill_price": 5.70,
            "quantity": 120,
            "order_id": "exit-order-n3",
            "source_event_id": True,  # bool masquerading as int -> rejected
            "entry_filled_at_utc": "2026-07-27T13:31:00Z",
            "filled_at_utc": "2026-07-27T13:34:00Z",
        },
    )
    assert bool_source_id["coverage_status"] == "COVERAGE_UNAVAILABLE"
    assert bool_source_id["source_event_id"] is None
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


def test_upstream_only_arm_stops_before_database_or_filesystem(
    monkeypatch,
    tmp_path,
):
    calls = []
    monkeypatch.setattr(
        batch,
        "source_window_snapshot",
        lambda *args: calls.append(args),
    )
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
            batch.TEST_SINK_CONFIRMATION,
            "--ohlcv-cache-dir",
            str(tmp_path),
            "--arm",
            "intended-minus-universe-float",
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
    with pytest.raises(SystemExit, match="pre-selection flag"):
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
    expected = str((tmp_path / "results.jsonl").resolve())
    assert batch.v3_results_path(str(tmp_path)) == expected
    assert batch.v3_results_path(str(tmp_path), expected) == expected
    with pytest.raises(ValueError, match="results must be"):
        batch.v3_results_path(
            str(tmp_path),
            str(tmp_path / "elsewhere" / "results.jsonl"),
        )


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


def test_driver_fill_lines_do_not_round_fractional_quantities():
    """TVRD|2026-07-07 regression: the mock's volume-capped partials fill
    FRACTIONAL shares; printing each FILL qty with {q:.0f} rounded the legs of
    one split round-trip independently (178.25 -> 178 vs 26.6+151.65 -> 27+152),
    so a raw-flat window parsed as net -1 share -> coverage_unavailable."""
    driver = (ROOT / "scripts" / "replay_ab_dark_flags.py").read_text(encoding="utf-8")
    assert "{q:.0f} @" not in driver
    assert 'BUY  {_fmt_fill_qty(q)}' in driver
    assert 'SELL {_fmt_fill_qty(q)}' in driver
    # The helper's quantization is the budget the batch/scorecard checks assume.
    assert 'f"{float(q):.10f}"' in driver


def test_fill_inventory_flat_budgets_print_quantization_only():
    def fills(buys, sells):
        return (
            [{"side": "buy", "qty": q, "px": 2.75} for q in buys]
            + [{"side": "sell", "qty": q, "px": 2.9} for q in sells]
        )

    # The exact TVRD|2026-07-07 failure shape survives as a detected leak: the
    # OLD integer-rounded log quantities really do net -1 whole share.
    assert batch.replay_fill_inventory_is_flat(
        fills([178.0], [27.0, 152.0])
    ) is False
    # The same round-trip printed at 1e-10 quantization is flat.
    assert batch.replay_fill_inventory_is_flat(
        fills([178.25], [26.6, 151.65])
    ) is True
    # Per-fill print quantization (<= 5e-11 each) is budgeted by fill count...
    n_side = 29
    drift = 5e-11 * (2 * n_side) * 0.9
    assert batch.replay_fill_inventory_is_flat(
        fills([1.0] * n_side, [1.0] * (n_side - 1) + [1.0 + drift])
    ) is True
    # ...but the budget does NOT stretch for small fill sets (2 fills -> 1e-9).
    assert batch.replay_fill_inventory_is_flat(
        fills([1.0], [1.0 + 2e-9])
    ) is False
    # A real one-share / one-increment leak always fails, at any fill count.
    assert batch.replay_fill_inventory_is_flat(
        fills([1.0] * n_side, [1.0] * (n_side - 1) + [2.0])
    ) is False


def test_parse_driver_stdout_round_trips_fractional_fill_quantities():
    policy = batch.resolve_strategy_policy("intended")
    policy_sha256 = batch.strategy_policy_sha256(policy)
    scope_sha256 = batch.execution_scope_sha256(batch.replay_execution_scope())
    out = "\n".join(
        [
            f"[STRATEGY_POLICY=intended] sha256={policy_sha256}",
            f"[EXECUTION_SCOPE=post-selection-fsm] sha256={scope_sha256}",
            "final_state=watching_live",
            "    BUY  178.25 @ 2.7500",
            "    SELL 26.6 @ 2.9100",
            "    SELL 151.65 @ 2.8100",
            "[ARM=intended] TVRD PnL +12.34 entries=1 exits=2",
        ]
    )
    status, parsed = batch.parse_driver_stdout(
        out,
        expected_symbol="TVRD",
        expected_arm="intended",
        expected_policy_sha256=policy_sha256,
        expected_execution_scope_sha256=scope_sha256,
    )
    assert status == "ok"
    assert [f["qty"] for f in parsed["fills"]] == [178.25, 26.6, 151.65]
    assert batch.replay_fill_inventory_is_flat(parsed["fills"]) is True


def test_pair_round_trips_closes_print_quantized_fractional_cycles():
    scorecard = _load("replay_scorecard", "scripts/replay_scorecard.py")
    # Print order is all buys then all sells (the driver emits the two lists
    # separately); a fractional split cycle must still close.
    trades = scorecard.pair_round_trips(
        [
            {"side": "buy", "qty": 178.25, "px": 2.75},
            {"side": "sell", "qty": 26.6, "px": 2.91},
            {"side": "sell", "qty": 151.65, "px": 2.81},
        ]
    )
    assert len(trades) == 1
    assert trades[0]["qty"] == 178.25
    # Cumulative 5e-11-per-fill print drift on a many-fill window stays within
    # the slack budget instead of raising "sells more quantity than is open".
    n_side = 29
    drifted = (
        [{"side": "buy", "qty": 1.0, "px": 2.75} for _ in range(n_side)]
        + [{"side": "sell", "qty": 1.0, "px": 2.9} for _ in range(n_side - 1)]
        + [{"side": "sell", "qty": 1.0 + 5e-11 * (2 * n_side) * 0.9, "px": 2.9}]
    )
    assert len(scorecard.pair_round_trips(drifted)) == 1
    # A real oversell (one whole share) still fails closed.
    with pytest.raises(ValueError, match="sells more"):
        scorecard.pair_round_trips(
            [
                {"side": "buy", "qty": 178.0, "px": 2.75},
                {"side": "sell", "qty": 179.0, "px": 2.9},
            ]
        )
    # A real stranded remainder still fails closed.
    with pytest.raises(ValueError, match="open quantity"):
        scorecard.pair_round_trips(
            [
                {"side": "buy", "qty": 178.0, "px": 2.75},
                {"side": "sell", "qty": 177.0, "px": 2.9},
            ]
        )
