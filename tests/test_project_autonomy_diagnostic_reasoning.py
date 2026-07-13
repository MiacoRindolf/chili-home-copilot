from __future__ import annotations

import json
import sqlite3

from app.services.project_autonomy import diagnostic_reasoning as reasoning


def _sim_clock_case():
    return {
        "case_id": "fable5-sim-clock",
        "problem_statement": "A replayed 07:34 ET entry was sized with the 15:00 ET wall-clock multiplier.",
        "observations": [
            {
                "evidence_id": "clock-result",
                "statement": "With replay time fixed at 07:34 ET, the observed multiplier was the 15:00 ET value 0.25.",
                "dimension": "clock",
                "kind": "experiment",
                "provenance": "replay-run-1",
                "independence_key": "replay-run-1",
                "reliability": 1.0,
                "discriminating": True,
            },
            {
                "evidence_id": "caller-source",
                "statement": "The caller omitted now_et_hour_frac even though its module exposes a replay-patched _utcnow().",
                "dimension": "clock",
                "kind": "artifact",
                "provenance": "live_runner.py:10150",
                "independence_key": "live_runner.py",
                "reliability": 0.95,
            },
        ],
    }


def _nbbo_drift_case():
    return {
        "case_id": "fable5-nbbo-replay-drift",
        "problem_statement": "The same JEM replay changed from +15034 to -5419 without a code change.",
        "observations": [
            {
                "evidence_id": "baseline-good",
                "statement": "Original replay outcome was +15034.",
                "dimension": "test_harness",
                "kind": "metric",
                "provenance": "scorecard-a",
                "comparison_key": "jem-0630",
                "code_revision": "same-sha",
                "input_fingerprint": "same-window",
                "environment_fingerprint": "sink-build-a",
                "outcome_fingerprint": "pnl:+15034",
                "reliability": 1.0,
            },
            {
                "evidence_id": "baseline-bad",
                "statement": "Repeat replay outcome was -5419 with the same code and input window.",
                "dimension": "test_harness",
                "kind": "metric",
                "provenance": "scorecard-b",
                "comparison_key": "jem-0630",
                "code_revision": "same-sha",
                "input_fingerprint": "same-window",
                "environment_fingerprint": "sink-build-b",
                "outcome_fingerprint": "pnl:-5419",
                "reliability": 1.0,
            },
            {
                "evidence_id": "source-count",
                "statement": "Production NBBO source contains 473990 rows for the replay window.",
                "dimension": "data",
                "kind": "metric",
                "provenance": "prod-count-query",
                "independence_key": "prod-db",
                "reliability": 1.0,
                "discriminating": True,
            },
            {
                "evidence_id": "sink-count",
                "statement": "Replay sink NBBO table contains zero rows for the same window.",
                "dimension": "data",
                "kind": "metric",
                "provenance": "sink-count-query",
                "independence_key": "sink-db",
                "reliability": 1.0,
                "discriminating": True,
            },
            {
                "evidence_id": "consumer-source",
                "statement": "The replay consumer fails closed when the sink NBBO tape is empty.",
                "dimension": "data",
                "kind": "artifact",
                "provenance": "entry_gates.py:consumer",
                "independence_key": "entry_gates.py",
                "reliability": 0.95,
            },
            {
                "evidence_id": "feature-source",
                "statement": "A recently edited exit feature can change realized PnL.",
                "dimension": "code",
                "kind": "artifact",
                "provenance": "live_runner.py:feature",
                "independence_key": "live_runner.py",
                "reliability": 0.9,
                "discriminating": True,
            },
        ],
    }


def test_taglish_incident_requests_enter_diagnostic_reasoning():
    assert reasoning.looks_like_diagnostic_request(
        "Bakit puro bug at nagregress ang worker? Tingnan mo kung may mali sa live state."
    )
    assert reasoning.looks_like_diagnostic_request(
        "Ayusin mo: hindi gumagana ang replay kahit pareho ang input."
    )
    assert reasoning.looks_like_diagnostic_request("Anyare na?") is False
    assert reasoning.looks_like_diagnostic_request("Ayos na lahat?") is False


def test_realworld_lenses_cover_fable_history_incident_shapes_without_assuming_cause():
    prompt = (
        "Bakit iba ang Ross-style live entry at replay PnL? Alpaca still has a pending "
        "position after the worker image deploy, then the ticker halted with a wide BBO spread."
    )

    lenses = reasoning.derive_diagnostic_lenses(prompt)

    assert lenses[:5] == [
        "expected_vs_observed",
        "causal_timeline",
        "root_cause_vs_downstream_symptom",
        "safety_boundary",
        "post_change_proof",
    ]
    assert "strategy_contract" in lenses
    assert "counterfactual_integrity" in lenses
    assert "state_reconciliation" in lenses
    assert "runtime_source_parity" in lenses
    assert "external_market_state" in lenses


def test_normalized_case_and_prompts_preserve_deep_diagnostic_lenses():
    case = reasoning.build_case_from_prompt(
        "Diagnose why broker state diverged from the local pending entry after a worker deploy."
    )

    assert "state_reconciliation" in case["constraints"]["diagnostic_lenses"]
    assert "runtime_source_parity" in case["constraints"]["diagnostic_lenses"]
    investigator = reasoning.investigator_prompt(case)
    judge = reasoning.judge_prompt(case, reasoning.heuristic_packet(case), {})
    assert "earliest causal break" in investigator
    assert "source-versus-running-revision parity" in investigator
    assert "profitable counterfactual is not proof" in judge


def test_reconstructs_earliest_break_from_shuffled_cross_service_events():
    case = {
        "case_id": "causal-timeline",
        "problem_statement": "A stale worker revision caused queue pressure and a provider timeout.",
        "observations": [
            {
                "evidence_id": "dependency-timeout",
                "statement": "The provider call timed out after queue delay.",
                "dimension": "dependency",
                "kind": "metric",
                "provenance": "provider-metric",
                "independence_key": "provider-metric",
                "reliability": 0.99,
                "discriminating": True,
                "observed_at": "2026-07-11T10:02:00Z",
                "entity_id": "request-17",
                "event_type": "provider_timeout",
                "expected_state": "completed",
                "actual_state": "timed_out",
                "causal_parent_ids": ["queue-saturated"],
            },
            {
                "evidence_id": "source-current",
                "statement": "The checked-out source is revision r2.",
                "dimension": "code",
                "kind": "artifact",
                "provenance": "git-head",
                "independence_key": "git-head",
                "reliability": 0.99,
                "discriminating": True,
                "observed_at": "2026-07-11T09:59:00Z",
                "entity_id": "worker-a",
                "event_type": "source_revision",
                "source_revision": "r2",
            },
            {
                "evidence_id": "queue-saturated",
                "statement": "The stale worker stopped consuming and the queue saturated.",
                "dimension": "state",
                "kind": "metric",
                "provenance": "queue-depth",
                "independence_key": "queue-depth",
                "reliability": 0.99,
                "discriminating": True,
                "observed_at": "2026-07-11T10:01:00Z",
                "entity_id": "queue-a",
                "event_type": "queue_depth",
                "expected_state": "draining",
                "actual_state": "saturated",
                "causal_parent_ids": ["runtime-stale"],
            },
            {
                "evidence_id": "runtime-stale",
                "statement": "The running worker still loads revision r1.",
                "dimension": "runtime",
                "kind": "artifact",
                "provenance": "runtime-image",
                "independence_key": "runtime-image",
                "reliability": 0.99,
                "discriminating": True,
                "observed_at": "2026-07-11T10:00:00Z",
                "entity_id": "worker-a",
                "event_type": "runtime_revision",
                "expected_state": "revision-r2",
                "actual_state": "revision-r1",
                "source_revision": "r2",
                "runtime_revision": "r1",
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The checked-out source is defective.",
                "dimension": "code",
                "support_evidence_ids": ["source-current"],
                "falsification": "Run the same source revision in the worker.",
            },
            {
                "hypothesis_id": "h-runtime",
                "claim": "The worker runs a stale revision.",
                "dimension": "runtime",
                "support_evidence_ids": ["runtime-stale"],
                "falsification": "Align only the worker revision.",
            },
            {
                "hypothesis_id": "h-state",
                "claim": "Queue saturation is the root cause.",
                "dimension": "state",
                "support_evidence_ids": ["queue-saturated"],
                "falsification": "Drain only the queue.",
            },
            {
                "hypothesis_id": "h-dependency",
                "claim": "The provider caused the timeout.",
                "dimension": "dependency",
                "support_evidence_ids": ["dependency-timeout"],
                "falsification": "Replace only the provider.",
            },
        ],
        "conclusion": {
            "hypothesis_id": "h-dependency",
            "status": "confirmed",
            "evidence_ids": ["dependency-timeout"],
        },
    }

    normalized = reasoning.normalize_case(case)
    timeline = reasoning.reconstruct_causal_timeline(normalized)
    report = reasoning.evaluate_packet(case, packet)
    results = {
        item["hypothesis_id"]: item for item in report["hypothesis_results"]
    }

    assert timeline["ordered_evidence_ids"] == [
        "source-current",
        "runtime-stale",
        "queue-saturated",
        "dependency-timeout",
    ]
    assert timeline["earliest_break"]["evidence_id"] == "runtime-stale"
    assert timeline["downstream_evidence_ids"] == [
        "queue-saturated",
        "dependency-timeout",
    ]
    assert timeline["runtime_source_parity"]["status"] == "mismatch"
    assert results["h-code"]["status"] == "untested"
    assert results["h-state"]["downstream_only_support"] is True
    assert results["h-dependency"]["downstream_only_support"] is True
    assert report["conclusion"]["dimension"] == "runtime"
    assert report["conclusion"]["status"] == "confirmed"
    assert report["decision"] == "patch_root_cause"
    assert "causal_timeline" in reasoning.judge_prompt(case, packet, report)


def test_timeline_detects_illegal_entity_transition_in_event_time_order():
    case = reasoning.normalize_case(
        {
            "case_id": "transition-order",
            "problem_statement": "A job completed through an illegal transition.",
            "observations": [
                {
                    "evidence_id": "completed",
                    "statement": "The job jumped to complete.",
                    "dimension": "state",
                    "observed_at": "2026-07-11T10:01:00Z",
                    "entity_id": "job-1",
                    "transition_from": "running",
                    "transition_to": "complete",
                },
                {
                    "evidence_id": "queued",
                    "statement": "The job entered the queue.",
                    "dimension": "state",
                    "observed_at": "2026-07-11T10:00:00Z",
                    "entity_id": "job-1",
                    "transition_to": "queued",
                },
            ],
        }
    )

    timeline = reasoning.reconstruct_causal_timeline(case)

    assert timeline["ordered_evidence_ids"] == ["queued", "completed"]
    assert timeline["earliest_break"]["evidence_id"] == "completed"
    assert "transition_from_mismatch" in timeline["earliest_break"]["violations"]


def test_provenance_graph_finds_consumer_starvation_before_missing_sink():
    case = {
        "case_id": "producer-consumer-lineage",
        "problem_statement": "Published alerts never reach the sink.",
        "observations": [
            {
                "evidence_id": "sink-missing",
                "statement": "The sink has no row for the alert.",
                "dimension": "data",
                "kind": "artifact",
                "provenance": "sink-profile",
                "independence_key": "trace:req-42",
                "reliability": 0.99,
                "discriminating": True,
                "observed_at": "2026-07-11T10:02:00Z",
                "service_id": "sink-db",
                "sink_id": "sink-db",
                "edge_from": "consumer-worker",
                "edge_to": "sink-db",
                "expected_edge_state": "persisted",
                "actual_edge_state": "missing",
                "causal_parent_ids": ["consumer-stalled"],
                "correlation_id": "req-42-sensitive",
            },
            {
                "evidence_id": "producer-published",
                "statement": "The scanner published the alert to the queue.",
                "dimension": "data",
                "kind": "artifact",
                "provenance": "producer-trace",
                "independence_key": "trace:req-42",
                "reliability": 0.99,
                "discriminating": True,
                "observed_at": "2026-07-11T10:00:00Z",
                "service_id": "scanner",
                "producer_id": "scanner",
                "edge_from": "scanner",
                "edge_to": "alert-queue",
                "expected_edge_state": "delivered",
                "actual_edge_state": "delivered",
                "correlation_id": "req-42-sensitive",
            },
            {
                "evidence_id": "consumer-stalled",
                "statement": "The consumer stopped draining the delivered alert.",
                "dimension": "state",
                "kind": "artifact",
                "provenance": "consumer-trace",
                "independence_key": "trace:req-42",
                "reliability": 0.99,
                "discriminating": True,
                "observed_at": "2026-07-11T10:01:00Z",
                "service_id": "consumer-worker",
                "consumer_id": "consumer-worker",
                "edge_from": "alert-queue",
                "edge_to": "consumer-worker",
                "expected_edge_state": "consumed",
                "actual_edge_state": "stalled",
                "causal_parent_ids": ["producer-published"],
                "correlation_id": "req-42-sensitive",
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-state",
                "claim": "The consumer is starved or stalled.",
                "dimension": "state",
                "support_evidence_ids": ["consumer-stalled"],
                "falsification": "Resume only the consumer and trace the same alert.",
            },
            {
                "hypothesis_id": "h-data",
                "claim": "The sink data is missing.",
                "dimension": "data",
                "support_evidence_ids": ["sink-missing"],
                "falsification": "Hold the consumer fixed and replace only the sink.",
            },
        ],
        "conclusion": {
            "hypothesis_id": "h-data",
            "status": "confirmed",
            "evidence_ids": ["sink-missing"],
        },
    }

    normalized = reasoning.normalize_case(case)
    graph = reasoning.build_provenance_graph(normalized)
    report = reasoning.evaluate_packet(case, packet)

    assert graph["first_broken_edge"]["evidence_id"] == "consumer-stalled"
    assert graph["first_broken_edge"]["from"] == "alert-queue"
    assert graph["first_broken_edge"]["to"] == "consumer-worker"
    assert graph["flow_classification"] == "consumer_starvation"
    assert list(graph["correlation_groups"].values()) == [
        ["producer-published", "consumer-stalled", "sink-missing"]
    ]
    assert graph["independence_clusters"]["trace:req-42"] == [
        "producer-published",
        "consumer-stalled",
        "sink-missing",
    ]
    assert "req-42-sensitive" not in json.dumps(graph)
    assert report["conclusion"]["dimension"] == "state"
    assert report["conclusion"]["status"] == "confirmed"
    assert report["decision"] == "patch_root_cause"
    assert "provenance_graph" in reasoning.judge_prompt(case, packet, report)


def test_dimension_inference_understands_control_flow_leases_and_tls_chains():
    assert reasoning.infer_dimension(
        "The control-flow trace shows the branch returning only on revision r184."
    ) == "code"
    assert reasoning.infer_dimension(
        "The identical captured request and dependency stubs stay fixed while comparing two revisions."
    ) == "code"
    assert reasoning.infer_dimension(
        "The lease snapshot retains busy_owner after release_requested and the owner process is gone."
    ) == "state"
    assert reasoning.infer_dimension(
        "TLS certificate verify failed because the peer chain has an expired intermediate."
    ) == "dependency"


def test_dimension_terms_do_not_match_inside_release_or_timeout():
    release_scores = reasoning._dimension_scores(
        "A routine release completed after the request cohort was captured."
    )
    timeout_scores = reasoning._dimension_scores(
        "The upload timed out while transferring a large request body."
    )

    assert release_scores["state"] == 0
    assert timeout_scores["clock"] == 0


def test_dimension_inference_handles_dense_operational_mechanisms():
    assert reasoning.infer_dimension(
        "Reverting only that source hunk restores the previous result."
    ) == "code"
    assert reasoning.infer_dimension(
        "The isolated replay succeeds when only that value is changed."
    ) == "config"
    assert reasoning.infer_dimension(
        "The network namespace has an overlay MTU above the underlay path."
    ) == "runtime"
    assert reasoning.infer_dimension(
        "The immutable manifest omitted two shard URIs."
    ) == "data"
    assert reasoning.infer_dimension(
        "Resetting only the persisted cursor restores scanning."
    ) == "state"


def test_heuristic_evidence_polarity_does_not_count_healthy_controls_as_support():
    case = {
        "case_id": "evidence-polarity",
        "problem_statement": "Uploads fail only on one node pool.",
        "observations": [
            {
                "evidence_id": "runtime-a",
                "statement": "The overlay MTU exceeds the underlay path.",
                "kind": "artifact",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "runtime-b",
                "statement": "Lowering only the pod-side MTU restores every transfer.",
                "kind": "experiment",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "dependency-control",
                "statement": "The upstream endpoint remains healthy and no retry occurs.",
                "kind": "artifact",
                "reliability": 0.95,
                "discriminating": False,
            },
        ],
    }

    packet = reasoning.heuristic_packet(case)
    report = reasoning.evaluate_packet(case, packet)
    dependency = next(
        item for item in packet["hypotheses"] if item["dimension"] == "dependency"
    )

    assert dependency["support_evidence_ids"] == []
    assert dependency["contradict_evidence_ids"] == []
    assert report["conclusion"]["dimension"] == "runtime"
    assert report["conclusion"]["status"] == "inconclusive"


def test_strong_independent_support_outweighs_weaker_same_family_counterevidence():
    case = {
        "case_id": "support-versus-control",
        "problem_statement": "A state transition duplicates work.",
        "observations": [
            {
                "evidence_id": "state-a",
                "statement": "A deterministic barrier reproduces the stale cursor race.",
                "dimension": "state",
                "kind": "experiment",
                "provenance": "barrier-a",
                "independence_key": "barrier-a",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "state-b",
                "statement": "Resetting only the cursor restores one transition.",
                "dimension": "state",
                "kind": "experiment",
                "provenance": "barrier-b",
                "independence_key": "barrier-b",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "state-control",
                "statement": "The unrelated session cache remains unchanged from baseline.",
                "dimension": "state",
                "kind": "artifact",
                "provenance": "cache-control",
                "independence_key": "cache-control",
                "reliability": 0.9,
                "discriminating": False,
            },
        ],
    }

    report = reasoning.evaluate_packet(case, reasoning.heuristic_packet(case))

    assert report["conclusion"]["dimension"] == "state"
    assert report["conclusion"]["status"] == "confirmed"


def test_hypothesis_without_evidence_links_is_not_auto_supported_by_family():
    case = {
        "case_id": "no-implicit-support",
        "problem_statement": "A code path may be wrong.",
        "observations": [
            {
                "evidence_id": "source-a",
                "statement": "The source hunk changed the branch.",
                "dimension": "code",
                "kind": "artifact",
                "reliability": 0.99,
                "discriminating": True,
            }
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The code branch is wrong.",
                "dimension": "code",
                "support_evidence_ids": [],
                "falsification": "Revert only the source hunk.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-code", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["hypothesis_results"][0]["status"] == "untested"
    assert report["conclusion"]["status"] == "inconclusive"


def test_normalize_case_retains_fresh_probe_evidence_at_observation_cap():
    observations = [
        {
            "evidence_id": f"operator-{index}",
            "statement": f"Operator observation {index}",
            "provenance": "operator_prompt",
        }
        for index in range(40)
    ]
    observations.append(
        {
            "evidence_id": "probe-fresh",
            "statement": "The typed probe found a runtime revision mismatch.",
            "provenance": "diagnostic_probe:repo_state",
            "reliability": 0.99,
            "discriminating": True,
        }
    )

    normalized = reasoning.normalize_case(
        {
            "case_id": "probe-at-cap",
            "problem_statement": "Diagnose the mismatch.",
            "observations": observations,
        }
    )

    assert len(normalized["observations"]) == 40
    assert normalized["observations"][-1]["evidence_id"] == "probe-fresh"
    assert "operator-39" not in {
        item["evidence_id"] for item in normalized["observations"]
    }


def test_dense_council_prompts_enforce_compact_closed_json_contract():
    case = {
        "case_id": "compact-council",
        "problem_statement": "Diagnose a dense cross-service operational failure.",
        "observations": [
            {
                "evidence_id": f"e{index}",
                "statement": (
                    "A bounded source, runtime, configuration, dependency, and state "
                    f"observation was recorded for service {index}."
                ),
                "provenance": f"source-{index}",
                "independence_key": f"source-{index}",
                "reliability": 0.9,
                "discriminating": index < 2,
            }
            for index in range(8)
        ],
        "constraints": {"minimum_hypothesis_dimensions": 4},
    }
    packet = reasoning.heuristic_packet(case)
    report = reasoning.evaluate_packet(case, packet)
    investigator = reasoning.investigator_prompt(case)
    judge = reasoning.judge_prompt(case, packet, report)

    assert "Hard output budget: at most 700 tokens" in investigator
    assert "Close every JSON array and object" in judge
    assert len(investigator) < 8_000
    assert len(judge) < 13_000
    assert '\"environment_fingerprint\":\"\"' not in judge


def test_local_packet_contract_repair_is_grounded_audited_and_fail_closed():
    case = {
        "case_id": "contract-repair",
        "problem_statement": "A source hunk changed the branch outcome.",
        "observations": [
            {
                "evidence_id": "code-proof",
                "statement": "Reverting only that source hunk restores the outcome.",
                "dimension": "code",
                "kind": "experiment",
                "provenance": "isolated-revert",
                "independence_key": "isolated-revert",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "state-control",
                "statement": "The unrelated session cache remains unchanged from baseline.",
                "dimension": "state",
                "kind": "artifact",
                "provenance": "state-control",
                "independence_key": "state-control",
                "reliability": 0.9,
            },
        ],
        "constraints": {"minimum_hypothesis_dimensions": 1},
    }
    malformed = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The source hunk changed control flow.",
                "dimension": "code",
                "support_evidence_ids": ["state-control"],
                "falsification": "",
            }
        ],
        "experiments": [
            {
                "experiment_id": "x-restart",
                "hypothesis_ids": ["h-code"],
                "safety": "runtime",
                "auto_execute": True,
            }
        ],
        "conclusion": {
            "hypothesis_id": "h-code",
            "status": "confirmed",
            "evidence_ids": ["code-proof"],
        },
    }

    result = reasoning.run_local_diagnostic_debate(
        case,
        lambda _stage, _prompt: json.dumps(malformed),
        stages_to_run=("judge",),
    )
    stage = result["stages"][0]
    experiment = result["packet"]["experiments"][0]

    assert stage["accepted"] is True
    assert "h-code:dropped_contradiction_support" in stage["contract_repairs"]
    assert "h-code:restored_grounded_support" in stage["contract_repairs"]
    assert "h-code:restored_falsification" in stage["contract_repairs"]
    assert "x-restart:demoted_unsafe_auto_execute" in stage["contract_repairs"]
    assert result["packet"]["hypotheses"][0]["support_evidence_ids"] == [
        "code-proof"
    ]
    assert experiment["auto_execute"] is False
    assert experiment["probe"] == {}
    assert result["report"]["valid"] is True


def test_local_packet_cannot_replace_qualified_causal_support_with_correlation():
    case = {
        "case_id": "causal-support-retention",
        "problem_statement": "Large jobs stall on a shared worker pool.",
        "observations": [
            {
                "evidence_id": "broad-control",
                "statement": (
                    "Reprocessing matched jobs on a dedicated diagnostic host restores "
                    "latency while several host resources change together."
                ),
                "dimension": "runtime",
                "kind": "experiment",
                "provenance": "bounded-control",
                "independence_key": "bounded-control",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "host-correlation",
                "statement": "Scheduler wait rises during the affected nightly window.",
                "dimension": "runtime",
                "kind": "metric",
                "provenance": "host-summary",
                "independence_key": "host-summary",
                "reliability": 0.97,
                "discriminating": True,
            },
            {
                "evidence_id": "event-gap",
                "statement": (
                    "Aggregate metrics cannot show whether any sampled event was "
                    "runnable but unscheduled."
                ),
                "dimension": "runtime",
                "kind": "artifact",
                "provenance": "trace-audit",
                "independence_key": "trace-audit",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
        "constraints": {"minimum_hypothesis_dimensions": 1},
    }
    correlation_only = {
        "hypotheses": [
            {
                "hypothesis_id": "h-runtime",
                "claim": "Shared-host contention causes the long tail.",
                "dimension": "runtime",
                "support_evidence_ids": ["host-correlation"],
                "falsification": "Capture event-level wait classes on both pools.",
            }
        ],
        "conclusion": {
            "hypothesis_id": "h-runtime",
            "status": "provisional",
        },
    }

    result = reasoning.run_local_diagnostic_debate(
        case,
        lambda _stage, _prompt: json.dumps(correlation_only),
        stages_to_run=("judge",),
    )
    stage = result["stages"][0]
    support = result["packet"]["hypotheses"][0]["support_evidence_ids"]

    assert stage["accepted"] is True
    assert (
        "h-runtime:restored_qualified_causal_support"
        in stage["contract_repairs"]
    )
    assert support == ["host-correlation", "broad-control"]
    assert result["report"]["conclusion"]["status"] == "provisional"
    assert result["report"]["decision"] == "instrument_first"


def test_local_packet_preserves_grounded_family_when_model_ids_and_breadth_change():
    case = {
        "case_id": "grounded-family-retention",
        "problem_statement": (
            "A dataset manifest misses its target, while aggregate host signals "
            "suggest an execution-pool effect."
        ),
        "observations": [
            {
                "evidence_id": "broad-control",
                "statement": (
                    "Reprocessing matched jobs on a dedicated diagnostic host restores "
                    "latency while several host resources change together."
                ),
                "dimension": "runtime",
                "kind": "experiment",
                "provenance": "bounded-control",
                "independence_key": "bounded-control",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "host-correlation",
                "statement": "Scheduler wait rises during the affected nightly window.",
                "dimension": "runtime",
                "kind": "metric",
                "provenance": "host-summary",
                "independence_key": "host-summary",
                "reliability": 0.97,
                "discriminating": True,
            },
            {
                "evidence_id": "event-gap",
                "statement": (
                    "Aggregate metrics cannot show whether any sampled event was "
                    "runnable but unscheduled."
                ),
                "dimension": "runtime",
                "kind": "artifact",
                "provenance": "trace-audit",
                "independence_key": "trace-audit",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
        "constraints": {"minimum_hypothesis_dimensions": 4},
    }
    relabeled = {
        "hypotheses": [
            {
                "hypothesis_id": f"model-{dimension}",
                "claim": f"The symptom belongs to {dimension}.",
                "dimension": dimension,
                "support_evidence_ids": ["host-correlation"],
                "falsification": "Capture one event-level owner trace.",
            }
            for dimension in ("dependency", "state", "config", "data")
        ],
        "conclusion": {
            "hypothesis_id": "model-data",
            "status": "provisional",
        },
    }

    result = reasoning.run_local_diagnostic_debate(
        case,
        lambda _stage, _prompt: json.dumps(relabeled),
        stages_to_run=("judge",),
    )
    stage = result["stages"][0]

    assert stage["accepted"] is True
    assert "runtime" in stage["preserved_hypothesis_dimensions"]
    assert result["report"]["conclusion"]["dimension"] == "runtime"
    assert result["report"]["conclusion"]["status"] == "provisional"
    assert result["report"]["decision"] == "instrument_first"


def test_local_dimension_aliases_preserve_system_layer_meaning():
    packet = reasoning.normalize_packet(
        {
            "hypotheses": [
                {
                    "hypothesis_id": "h-deploy",
                    "claim": "A stale deployment serves the old build.",
                    "dimension": "deployment",
                    "falsification": "Remove only the stale deployment.",
                },
                {
                    "hypothesis_id": "h-race",
                    "claim": "Concurrent workers race before reservation.",
                    "dimension": "concurrency",
                    "falsification": "Serialize only the reservation step.",
                },
            ],
            "conclusion": {"hypothesis_id": "h-deploy"},
        }
    )

    assert [item["dimension"] for item in packet["hypotheses"]] == [
        "runtime",
        "state",
    ]


def test_complex_case_rejects_single_family_model_collapse():
    case = {
        "case_id": "breadth-floor",
        "problem_statement": "Diagnose a multi-source operational failure.",
        "observations": [
            {
                "evidence_id": "e-code",
                "statement": "A source trace changed branches between revisions.",
                "dimension": "code",
                "provenance": "source-trace",
                "independence_key": "source",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "e-state",
                "statement": "A stale lease still owns the resource.",
                "dimension": "state",
                "provenance": "state-snapshot",
                "independence_key": "state",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "e-dependency",
                "statement": "The upstream dependency also logged a timeout.",
                "dimension": "dependency",
                "provenance": "bounded-log",
                "independence_key": "dependency",
                "reliability": 0.9,
                "discriminating": False,
            },
        ],
    }
    collapsed = {
        "hypotheses": [
            {
                "hypothesis_id": "h-dependency",
                "claim": "The dependency timeout caused the incident.",
                "dimension": "dependency",
                "support_evidence_ids": ["e-dependency"],
                "contradict_evidence_ids": [],
                "falsification": "Replace only the dependency and compare the outcome.",
            }
        ],
        "experiments": [],
        "conclusion": {
            "hypothesis_id": "h-dependency",
            "status": "provisional",
            "evidence_ids": ["e-dependency"],
        },
    }

    normalized = reasoning.normalize_case(case)
    assert normalized["constraints"]["minimum_hypothesis_dimensions"] == 3

    debate = reasoning.run_local_diagnostic_debate(
        case,
        lambda _stage, _prompt: json.dumps(collapsed),
        stages_to_run=("judge",),
    )
    dimensions = {
        item["dimension"] for item in debate["packet"]["hypotheses"]
    }
    assert debate["stages"][0]["accepted"] is True
    assert set(debate["stages"][0]["preserved_hypothesis_dimensions"]) == {
        "code",
        "state",
    }
    assert dimensions == {"code", "dependency", "state"}


def test_heuristic_packet_keeps_secondary_confounders_for_dense_single_family_evidence():
    case = {
        "case_id": "secondary-dimensions",
        "problem_statement": "A provider endpoint fails certificate validation in one runtime.",
        "observations": [
            {"evidence_id": "e1", "statement": "TLS certificate verify failed at the endpoint."},
            {"evidence_id": "e2", "statement": "Two UTC clock readings agree during the handshake."},
            {"evidence_id": "e3", "statement": "The runtime image hash matches the prior successful run."},
            {"evidence_id": "e4", "statement": "Resolved trust settings match the known-good host."},
            {"evidence_id": "e5", "statement": "The provider reports an incomplete peer chain."},
        ],
    }

    normalized = reasoning.normalize_case(case)
    packet = reasoning.heuristic_packet(case)
    dimensions = {
        item["dimension"] for item in packet["hypotheses"]
    }

    assert normalized["constraints"]["minimum_hypothesis_dimensions"] == 3
    assert "dependency" in dimensions
    assert len(dimensions) >= 3


def test_breadth_floor_prefers_testable_known_dimensions_over_unknown_bucket():
    case = {
        "case_id": "known-breadth",
        "problem_statement": (
            "Diagnose data loss while runtime, dependency, configuration, and state "
            "controls remain plausible."
        ),
        "observations": [
            {
                "evidence_id": "data-a",
                "statement": "The manifest omitted two shard records.",
                "dimension": "unknown",
                "discriminating": True,
            },
            {
                "evidence_id": "unknown-a",
                "statement": "The outcome also changed for an unknown reason.",
                "dimension": "unknown",
            },
            {
                "evidence_id": "state-a",
                "statement": "The queue cursor stayed at its prior checkpoint.",
                "dimension": "unknown",
            },
        ],
        "constraints": {"minimum_hypothesis_dimensions": 4},
    }

    packet = reasoning.heuristic_packet(case)
    dimensions = [item["dimension"] for item in packet["hypotheses"]]

    assert len(set(dimensions)) == 4
    assert "unknown" not in dimensions


def test_fable5_sim_clock_case_confirms_clock_root_cause():
    packet = {
        "problem_statement": "Replay sizing used the wrong clock.",
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "The caller used wall time instead of the replay clock.",
                "dimension": "clock",
                "support_evidence_ids": ["clock-result", "caller-source"],
                "contradict_evidence_ids": [],
                "falsification": "Pass the replay-derived ET hour explicitly and verify the multiplier changes to the 07:34 value.",
            }
        ],
        "experiments": [
            {
                "experiment_id": "x-clock",
                "hypothesis_ids": ["h-clock"],
                "changed_dimensions": ["clock"],
                "held_constant_dimensions": ["code", "data", "state", "config"],
                "expected_if_true": "The multiplier follows replay time.",
                "expected_if_false": "The multiplier remains at the wall-clock value.",
                "result_evidence_ids": ["clock-result"],
                "safety": "isolated",
                "status": "completed",
            }
        ],
        "conclusion": {
            "hypothesis_id": "h-clock",
            "status": "confirmed",
            "evidence_ids": ["clock-result", "caller-source"],
            "reason": "Independent runtime and source evidence agree.",
        },
    }

    report = reasoning.evaluate_packet(_sim_clock_case(), packet)

    assert report["valid"] is True
    assert report["conclusion"]["status"] == "confirmed"
    assert report["conclusion"]["dimension"] == "clock"
    assert report["decision"] == "patch_root_cause"
    assert report["premium_calls"] == 0


def test_evidence_gate_confirms_strong_root_cause_even_when_model_is_conservative():
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "The replay path reads wall time instead of simulated time.",
                "dimension": "clock",
                "support_evidence_ids": ["clock-result", "caller-source"],
                "contradict_evidence_ids": [],
                "falsification": "Substitute only the simulated hour and compare the multiplier.",
            }
        ],
        "experiments": [],
        "conclusion": {
            "hypothesis_id": "h-clock",
            "status": "provisional",
            "evidence_ids": ["clock-result", "caller-source"],
        },
    }

    report = reasoning.evaluate_packet(_sim_clock_case(), packet)

    assert report["conclusion"]["status"] == "confirmed"
    assert report["decision"] == "patch_root_cause"


def test_typed_probe_evidence_recovers_causal_family_omitted_by_local_model():
    case = {
        "case_id": "typed-probe-omission",
        "problem_statement": "A provider-backed worker failed while its source revision stayed fixed.",
        "observations": [
            {
                "evidence_id": "clock-source-a",
                "statement": "The worker reads a wall-clock timestamp.",
                "dimension": "clock",
                "kind": "artifact",
                "provenance": "worker.py:10",
                "independence_key": "worker.py",
                "reliability": 0.9,
                "discriminating": False,
            },
            {
                "evidence_id": "clock-source-b",
                "statement": "The request includes a timestamp field.",
                "dimension": "clock",
                "kind": "artifact",
                "provenance": "provider.py:20",
                "independence_key": "provider.py",
                "reliability": 0.9,
                "discriminating": False,
            },
            {
                "evidence_id": "probe-log-search",
                "statement": "Typed log_search found upstream connection refused.",
                "dimension": "dependency",
                "kind": "artifact",
                "provenance": "diagnostic_probe:log-search",
                "independence_key": "diagnostic_probe:log-search",
                "reliability": 0.95,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "Clock drift caused the provider failure.",
                "dimension": "clock",
                "support_evidence_ids": ["clock-source-a", "clock-source-b"],
                "contradict_evidence_ids": [],
                "falsification": "Hold code and dependency constant while changing only the clock.",
            }
        ],
        "experiments": [],
        "conclusion": {
            "hypothesis_id": "h-clock",
            "status": "confirmed",
            "evidence_ids": ["clock-source-a", "clock-source-b"],
        },
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["valid"] is True
    assert report["conclusion"]["hypothesis_id"] == "evidence-dependency"
    assert report["conclusion"]["dimension"] == "dependency"
    assert report["conclusion"]["status"] == "confirmed"
    assert report["conclusion"]["evidence_ids"] == ["probe-log-search"]
    assert any(
        item["hypothesis_id"] == "evidence-dependency"
        and item["origin"] == "deterministic_evidence_gate"
        for item in report["hypothesis_results"]
    )


def test_same_code_and_input_with_different_outcome_blocks_code_attribution():
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The recent exit feature caused the PnL regression.",
                "dimension": "code",
                "support_evidence_ids": ["feature-source", "baseline-bad"],
                "contradict_evidence_ids": [],
                "falsification": "Run the same data and environment on the prior code revision.",
            }
        ],
        "experiments": [],
        "conclusion": {
            "hypothesis_id": "h-code",
            "status": "confirmed",
            "evidence_ids": ["feature-source", "baseline-bad"],
        },
    }

    report = reasoning.evaluate_packet(_nbbo_drift_case(), packet)

    code_result = report["hypothesis_results"][0]
    assert report["baseline_drift"]
    assert code_result["status"] == "untested"
    assert report["conclusion"]["status"] == "inconclusive"
    assert report["decision"] == "instrument_first"
    assert any(item["dimension"] == "data" for item in report["next_experiments"])


def test_baseline_drift_blocks_provisional_code_attribution_too():
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The code revision caused the regression.",
                "dimension": "code",
                "support_evidence_ids": ["feature-source"],
                "contradict_evidence_ids": [],
                "falsification": "Re-run both revisions in one fingerprinted environment.",
            }
        ],
        "experiments": [],
        "conclusion": {
            "hypothesis_id": "h-code",
            "status": "provisional",
            "evidence_ids": ["feature-source"],
        },
    }

    report = reasoning.evaluate_packet(_nbbo_drift_case(), packet)

    assert report["hypothesis_results"][0]["status"] == "untested"
    assert report["conclusion"]["status"] == "inconclusive"
    assert report["decision"] == "instrument_first"


def test_new_counter_evidence_retracts_a_previous_confirmed_conclusion():
    initial_case = {
        "case_id": "before-data-check",
        "problem_statement": "Replay PnL regressed after an exit change.",
        "observations": [
            {
                "evidence_id": "feature-source",
                "statement": "The edited exit feature can change realized PnL.",
                "dimension": "code",
                "kind": "artifact",
                "provenance": "live_runner.py:feature",
                "independence_key": "source-review",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "feature-ab",
                "statement": "One isolated feature run reproduced the lower PnL.",
                "dimension": "code",
                "kind": "experiment",
                "provenance": "feature-ab-run",
                "independence_key": "feature-ab-run",
                "reliability": 0.9,
                "discriminating": True,
            },
        ],
    }
    code_packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The exit change caused the regression.",
                "dimension": "code",
                "support_evidence_ids": ["feature-source", "feature-ab"],
                "contradict_evidence_ids": [],
                "falsification": "Re-run the prior code in the same environment.",
            }
        ],
        "experiments": [],
        "conclusion": {"hypothesis_id": "h-code", "status": "confirmed"},
    }
    previous = reasoning.evaluate_packet(initial_case, code_packet)
    assert previous["conclusion"]["status"] == "confirmed"

    revised_packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The exit change caused the regression.",
                "dimension": "code",
                "support_evidence_ids": ["feature-source"],
                "contradict_evidence_ids": ["baseline-good", "baseline-bad"],
                "falsification": "Re-run the prior code in the same environment.",
            },
            {
                "hypothesis_id": "h-data",
                "claim": "The replay sink lost required NBBO data.",
                "dimension": "data",
                "support_evidence_ids": ["source-count", "sink-count", "consumer-source"],
                "contradict_evidence_ids": [],
                "falsification": "Mirror the window into the sink and verify the original behavior returns.",
            },
        ],
        "experiments": [
            {
                "experiment_id": "x-data",
                "hypothesis_ids": ["h-data"],
                "changed_dimensions": ["data"],
                "held_constant_dimensions": ["code", "clock", "state", "config"],
                "expected_if_true": "Re-entry behavior returns after the mirror.",
                "expected_if_false": "The behavior stays unchanged.",
                "result_evidence_ids": ["source-count", "sink-count"],
                "safety": "isolated",
                "status": "completed",
            }
        ],
        "conclusion": {
            "hypothesis_id": "h-data",
            "status": "confirmed",
            "evidence_ids": ["source-count", "sink-count", "consumer-source"],
        },
    }

    report = reasoning.evaluate_packet(
        _nbbo_drift_case(),
        revised_packet,
        previous_report=previous,
    )

    assert report["conclusion"]["status"] == "inconclusive"
    assert report["conclusion"]["dimension"] == "data"
    assert report["retractions"] == [
        {
            "hypothesis_id": "h-code",
            "previous_status": "confirmed",
            "new_status": "superseded",
            "reason": "New counter-evidence or a stronger competing explanation invalidated the earlier conclusion.",
        }
    ]


def test_local_debate_retracts_prior_case_conclusion_before_model_review():
    case = _nbbo_drift_case()
    case["prior_conclusion"] = {
        "hypothesis_id": "h-prior-code",
        "status": "confirmed",
        "dimension": "code",
        "claim": "The recent feature caused the replay collapse.",
    }

    result = reasoning.run_local_diagnostic_debate(
        case,
        None,
        stages_to_run=("judge",),
    )

    assert result["report"]["conclusion"]["dimension"] == "data"
    assert result["report"]["retractions"][0]["hypothesis_id"] == "h-prior-code"
    assert result["report"]["retractions"][0]["new_status"] == "superseded"


def test_unsafe_automatic_experiment_is_rejected():
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-runtime",
                "claim": "A worker restart changes the outcome.",
                "dimension": "runtime",
                "support_evidence_ids": ["clock-result"],
                "falsification": "Restart the worker.",
            }
        ],
        "experiments": [
            {
                "experiment_id": "x-live",
                "hypothesis_ids": ["h-runtime"],
                "changed_dimensions": ["runtime"],
                "expected_if_true": "Outcome changes.",
                "expected_if_false": "Outcome stays the same.",
                "safety": "live",
                "status": "planned",
                "auto_execute": True,
            }
        ],
        "conclusion": {"hypothesis_id": "h-runtime", "status": "provisional"},
    }

    report = reasoning.evaluate_packet(_sim_clock_case(), packet)

    assert report["valid"] is False
    assert any("unsafe automatic execution" in error for error in report["errors"])


def test_inert_schema_placeholder_probe_is_dropped_but_executable_one_is_rejected():
    placeholder_kind = (
        "repo_state|search|file_excerpt|git_history|git_diff|compile|targeted_test"
    )
    base_experiment = {
        "experiment_id": "x-clock",
        "hypothesis_ids": ["h-clock"],
        "changed_dimensions": ["clock"],
        "expected_if_true": "The replay becomes stable.",
        "expected_if_false": "The mismatch remains.",
        "safety": "read_only",
        "status": "planned",
        "probe": {"probe_id": "placeholder", "kind": placeholder_kind},
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "The replay reads wall clock time.",
                "dimension": "clock",
                "support_evidence_ids": ["clock-result", "caller-source"],
                "falsification": "Inject replay time while holding code constant.",
            }
        ],
        "experiments": [{**base_experiment, "auto_execute": False}],
        "conclusion": {"hypothesis_id": "h-clock", "status": "confirmed"},
    }

    normalized = reasoning.normalize_packet(packet)
    accepted = reasoning.evaluate_packet(_sim_clock_case(), normalized)
    rejected = reasoning.evaluate_packet(
        _sim_clock_case(),
        {**packet, "experiments": [{**base_experiment, "auto_execute": True}]},
    )

    assert normalized["experiments"][0]["probe"] == {}
    assert accepted["valid"] is True
    assert rejected["valid"] is False
    assert any("Unknown diagnostic probe kind" in error for error in rejected["errors"])


def test_local_debate_runs_investigator_skeptic_and_judge_without_premium_calls():
    calls = []
    data_packet = {
        "problem_statement": "Replay evidence drifted.",
        "hypotheses": [
            {
                "hypothesis_id": "h-data",
                "claim": "The replay sink lost required NBBO data.",
                "dimension": "data",
                "support_evidence_ids": ["source-count", "sink-count", "consumer-source"],
                "contradict_evidence_ids": [],
                "falsification": "Mirror NBBO while holding code constant.",
            }
        ],
        "experiments": [
            {
                "experiment_id": "x-data",
                "hypothesis_ids": ["h-data"],
                "changed_dimensions": ["data"],
                "held_constant_dimensions": ["code", "clock", "state", "config"],
                "expected_if_true": "Behavior returns.",
                "expected_if_false": "Behavior remains absent.",
                "result_evidence_ids": ["source-count", "sink-count"],
                "safety": "isolated",
                "status": "completed",
            }
        ],
        "conclusion": {
            "hypothesis_id": "h-data",
            "status": "confirmed",
            "evidence_ids": ["source-count", "sink-count", "consumer-source"],
        },
    }

    def fake_model(stage, prompt):
        calls.append((stage, prompt))
        return json.dumps(data_packet)

    result = reasoning.run_local_diagnostic_debate(_nbbo_drift_case(), fake_model)

    assert [stage for stage, _prompt in calls] == ["investigator", "skeptic", "judge"]
    assert result["report"]["conclusion"]["dimension"] == "data"
    assert result["report"]["conclusion"]["status"] == "inconclusive"
    assert result["premium_calls"] == 0
    assert all(stage["accepted"] for stage in result["stages"])


def test_repo_evidence_collection_is_bounded_and_provenanced(tmp_path):
    target = tmp_path / "app/replay_clock.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def choose_hour(sim_clock, wall_clock):\n"
        "    return wall_clock.hour\n",
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data/ignored.py").write_text("sim_clock = 'ignored'\n", encoding="utf-8")

    evidence = reasoning.collect_repo_evidence(
        tmp_path,
        "diagnose why replay_clock uses wall_clock",
        max_files=10,
        max_records=3,
    )

    assert evidence
    assert len(evidence) <= 3
    assert evidence[0]["provenance"].startswith("app/replay_clock.py:")
    assert all("data/ignored.py" not in item["provenance"] for item in evidence)


def test_repo_evidence_collection_supports_non_python_source(tmp_path):
    target = tmp_path / "lib/replay_clock.dart"
    target.parent.mkdir(parents=True)
    target.write_text(
        "int chooseHour(DateTime simClock, DateTime wallClock) => wallClock.hour;\n",
        encoding="utf-8",
    )

    evidence = reasoning.collect_repo_evidence(
        tmp_path,
        "diagnose replay_clock wallClock",
        max_files=10,
        max_records=3,
    )

    assert any(item["provenance"].startswith("lib/replay_clock.dart:") for item in evidence)


def test_diagnostic_request_detection_is_specific_to_investigation_language():
    assert reasoning.looks_like_diagnostic_request("Root-cause the replay regression") is True
    assert reasoning.looks_like_diagnostic_request("Add a label to the settings page") is False


def test_dimension_inference_prefers_changed_cause_over_held_constant_mentions():
    assert reasoning.infer_dimension(
        "Recreating only an isolated worker from revision r9 fixes the pre-fix behavior without a source edit."
    ) == "runtime"
    assert reasoning.infer_dimension(
        "Toggling only that setting swaps outcomes while dependency versions and clock remain fixed."
    ) == "config"
    assert reasoning.infer_dimension(
        "The ingestion source has quote rows while the evaluator sink is empty."
    ) == "data"
    assert reasoning.infer_dimension(
        "A single-flight promise stays poisoned after failure and blocks retry for the same key."
    ) == "state"
    assert reasoning.infer_dimension(
        "A partial unique index enforces the wrong one-to-many data contract."
    ) == "data"
    assert reasoning.infer_dimension(
        "The provider adapter drops the caller AbortSignal before the dependency call."
    ) == "dependency"


def test_mechanism_contracts_are_derived_without_model_output():
    state = reasoning.derive_contract_invariants(
        "A single-flight promise poisons later retry for the same key."
    )
    cancellation = reasoning.derive_contract_invariants(
        "Propagate AbortSignal and stop retry on AbortError."
    )
    aggregate = reasoning.derive_contract_invariants(
        "A one-to-many join causes Cartesian multiplication."
    )

    assert any("evicted by the state owner" in value for value in state)
    assert any("exact cancellation signal" in value for value in cancellation)
    assert any("Aggregate each independent child" in value for value in aggregate)

    case = reasoning.build_case_from_prompt(
        "A single-flight promise poisons later retry for the same key."
    )
    report = reasoning.evaluate_packet(case, reasoning.heuristic_packet(case))
    assert report["contract_invariants"] == state
    assert "mechanism invariant" in reasoning.report_context(report)


def test_cross_boundary_mechanism_contracts_are_derived_without_fixture_labels():
    prompts = {
        "decoder": "Reject non-canonical base64url aliases through the shared canonical decoder.",
        "reload": "A replacement config reload must clear omitted overrides and rebind runtime state.",
        "snapshot": "An async policy request must audit the same generation snapshot it authorized.",
        "checkpoint": "A file checkpoint must reset after source replacement or truncation.",
        "graph": "An unordered category hierarchy must detect unknown parents and cycles.",
        "lifecycle": "Release reader handles survive generation activation until the last reader closes.",
        "proxy": "Resolve a trusted proxy CIDR and forwarded multi-hop chain.",
        "tristate": "A member override is tri-state: explicit disable wins and NULL inherits workspace config.",
        "transition": "Archive, restore, and project move transitions must keep counters exact.",
    }

    derived = {
        name: reasoning.derive_contract_invariants(prompt)
        for name, prompt in prompts.items()
    }

    assert all(derived.values())
    assert any("decode-then-encode" in value for value in derived["decoder"])
    assert any("fresh candidate from defaults" in value for value in derived["reload"])
    assert any("immutable deep snapshot" in value for value in derived["snapshot"])
    assert any("stable source identity" in value for value in derived["checkpoint"])
    assert any("independent of input row order" in value for value in derived["graph"])
    assert any("reader counts and retirement" in value for value in derived["lifecycle"])
    assert any("walks forwarded hops" in value for value in derived["proxy"])
    assert any("preserves NULL as inherit" in value for value in derived["tristate"])
    assert any("cross-product" in value for value in derived["transition"])


def test_cross_boundary_contract_guards_reject_known_partial_mechanisms():
    assert any(
        "textual aliases" in value
        for value in reasoning.contract_invariant_warnings(
            "Reject non-canonical base64url aliases through the canonical decoder.",
            {"decoder.js": "export const decode = value => Buffer.from(value, 'base64url');"},
        )
    )
    assert any(
        "retained configuration" in value
        for value in reasoning.contract_invariant_warnings(
            "A replacement config reload clears omitted overrides.",
            {"settings.py": "def reload_config(payload): current.update(payload)"},
        )
    )
    assert any(
        "stable source identity" in value
        for value in reasoning.contract_invariant_warnings(
            "A checkpoint resets after source replacement or truncation.",
            {"checkpoint.py": "class CheckpointStore:\n def save(self, offset): pass\n def load(self): pass"},
        )
    )
    assert any(
            "forbids NULL" in value
            for value in reasoning.contract_invariant_warnings(
                "A member override is tri-state and NULL inherits workspace config.",
                {
                    "schema.sql": (
                        "CREATE TABLE member_override (\n"
                        "  member_id INTEGER PRIMARY KEY,\n"
                        "  enabled INTEGER NOT NULL DEFAULT 0\n"
                        ");"
                    )
                },
            )
        )


def test_configured_delimiter_contract_guards_joining_and_quoting_together():
    prompt = (
        "A configured delimited export profile uses the selected separator, and values "
        "containing that separator must be quoted."
    )
    invariants = reasoning.derive_contract_invariants(prompt)
    rejected = reasoning.contract_invariant_warnings(
        prompt,
        {
            "render.sql": (
                "SELECT id || p.field_separator || CASE "
                "WHEN instr(COALESCE(label, ''), ',') > 0 THEN quote(label) ELSE label END "
                "FROM item JOIN export_profile p;"
            )
        },
    )
    partial_join_rejected = reasoning.contract_invariant_warnings(
        prompt,
        {
            "render.sql": (
                "SELECT id || ',' || CASE "
                "WHEN instr(COALESCE(label, ''), p.field_separator) > 0 "
                "THEN '\"' || replace(label, '\"', '\"\"') || '\"' ELSE label END "
                "FROM item JOIN export_profile p;"
            )
        },
    )
    accepted = reasoning.contract_invariant_warnings(
        prompt,
        {
            "render.sql": (
                "SELECT id || p.field_separator || CASE "
                "WHEN instr(COALESCE(label, ''), p.field_separator) > 0 "
                "THEN '\"' || replace(label, '\"', '\"\"') || '\"' ELSE label END "
                "FROM item JOIN export_profile p;"
            )
        },
    )

    assert any("used consistently for field joining and quoting" in value for value in invariants)
    assert reasoning.contract_invariant_dimension(prompt) == "config"
    assert any("not used consistently" in value for value in rejected)
    assert any("not used consistently" in value for value in partial_join_rejected)
    assert accepted == []


def test_configured_delimiter_contract_repair_updates_join_and_quote_predicate():
    prompt = (
        "Exports generated with a non-default delimited profile still arrive comma-separated, "
        "and values containing the selected separator must be quoted."
    )
    source = (
        "SELECT CAST(c.id AS TEXT) || ',' || CASE\n"
        "  WHEN instr(COALESCE(c.label, ''), ',') > 0 THEN quote(c.label)\n"
        "  ELSE c.label END || p.record_separator\n"
        "FROM customer AS c JOIN export_profile AS p ON p.name = :profile;\n"
    )

    proposals = reasoning.contract_repair_proposals(
        prompt,
        {"sql/render.sql": source},
    )

    assert proposals["sql/render.sql"] == (
        "SELECT CAST(c.id AS TEXT) || p.field_separator || CASE\n"
        "  WHEN instr(COALESCE(c.label, ''), p.field_separator) > 0 THEN quote(c.label)\n"
        "  ELSE c.label END || p.record_separator\n"
        "FROM customer AS c JOIN export_profile AS p ON p.name = :profile;\n"
    )
    assert reasoning.contract_repair_dimension(
        prompt,
        {"sql/render.sql": source},
    ) == "config"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_configured_delimiter_contract_repair_requires_one_profile_alias():
    prompt = (
        "A configured delimited export profile must use its selected separator for joining "
        "and quoting."
    )
    source = (
        "SELECT id || ',' || label FROM export_profile AS first "
        "JOIN export_profile AS second ON second.name = first.name;"
    )

    assert reasoning.contract_repair_proposals(
        prompt,
        {"sql/render.sql": source},
    ) == {}
    assert reasoning.contract_repair_dimension(
        prompt,
        {"sql/render.sql": source},
    ) == "unknown"


def test_required_factory_binding_repair_coordinates_planner_and_invocation():
    prompt = (
        "Extension activation fails when a factory declares a required collaborator after a "
        "keyword-only separator, while ordinary dependencies still work."
    )
    files = {
        "dependency_plan.py": (
            "from inspect import Parameter, signature\n"
            "from typing import Callable\n\n"
            "class Dependency:\n"
            "    def __init__(self, name, binding):\n"
            "        self.name = name\n"
            "        self.binding = binding\n\n"
            "def dependency_plan(factory: Callable[..., object]):\n"
            "    dependencies = []\n"
            "    for parameter in signature(factory).parameters.values():\n"
            "        if parameter.default is not Parameter.empty:\n"
            "            continue\n"
            "        if parameter.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD):\n"
            "            dependencies.append(Dependency(parameter.name, \"positional\"))\n"
            "    return tuple(dependencies)\n"
        ),
        "service_container.py": (
            "class Container:\n"
            "    def resolve(self, name: str) -> object:\n"
            "        factory = self._providers[name]\n"
            "        dependencies = [self.resolve(item.name) for item in dependency_plan(factory)]\n"
            "        return factory(*dependencies)\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert set(proposals) == set(files)
    assert "Parameter.KEYWORD_ONLY" in proposals["dependency_plan.py"]
    assert 'Dependency(parameter.name, "keyword")' in proposals["dependency_plan.py"]
    assert "**keyword_dependencies" in proposals["service_container.py"]
    assert reasoning.contract_repair_dimension(prompt, files) == "dependency"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_monthly_local_schedule_repair_clamps_day_and_converts_instant():
    prompt = (
        "Monthly settlement near month end fails in February, and a worker completion in UTC "
        "must schedule against the local timezone without slipping a billing cycle."
    )
    files = {
        "billing_clock.py": (
            "from __future__ import annotations\n"
            "from datetime import datetime\n\n"
            "def next_monthly_run(after: datetime, billing_day: int, hour: int, minute: int = 0):\n"
            "    candidate = after.replace(day=billing_day, hour=hour, minute=minute)\n"
            "    return candidate\n"
        ),
        "settlement_runner.py": (
            "from zoneinfo import ZoneInfo\n\n"
            "def next_scheduled_run(completed_at, timezone_name):\n"
            "    completed = datetime.fromisoformat(completed_at)\n"
            "    return completed.replace(tzinfo=ZoneInfo(timezone_name))\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert "calendar.monthrange" in proposals["billing_clock.py"]
    assert proposals["billing_clock.py"].startswith(
        "from __future__ import annotations\nimport calendar\n"
    )
    assert "day=min(billing_day, last_day)" in proposals["billing_clock.py"]
    assert ".astimezone(ZoneInfo(timezone_name))" in proposals["settlement_runner.py"]
    assert ".replace(tzinfo=" not in proposals["settlement_runner.py"]
    assert reasoning.contract_repair_dimension(prompt, files) == "clock"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_task_teardown_repair_drains_lifo_and_preserves_original_termination():
    prompt = (
        "Worker shutdown teardown hooks must all run after process-level SystemExit or "
        "KeyboardInterrupt, and incident reporting must preserve the original termination."
    )
    files = {
        "teardown_stack.py": (
            "class TeardownStack:\n"
            "    def __init__(self):\n"
            "        self._callbacks = []\n\n"
            "    def close(self):\n"
            "        while self._callbacks:\n"
            "            function = self._callbacks.pop()\n"
            "            function()\n"
        ),
        "task_runtime.py": (
            "def run_task(task):\n"
            "    teardowns = TeardownStack()\n"
            "    try:\n"
            "        result = task(teardowns)\n"
            "    except Exception:\n"
            "        teardowns.close()\n"
            "        raise\n"
            "    teardowns.close()\n"
            "    return result\n"
        ),
    }

    rejected = reasoning.contract_invariant_warnings(prompt, files)
    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert any("misses BaseException" in warning for warning in rejected)
    assert any("stops at the first failed hook" in warning for warning in rejected)
    assert "except BaseException as error" in proposals["teardown_stack.py"]
    assert "first_error" in proposals["teardown_stack.py"]
    assert "except BaseException:" in proposals["task_runtime.py"]
    assert reasoning.contract_repair_dimension(prompt, files) == "runtime"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_dart_dependency_report_repair_adapts_schema_and_compound_licenses():
    prompt = (
        "After the inventory scanner was upgraded, newly produced report files are empty; "
        "compound license choices for runtime records must work while development-only tools "
        "remain outside violations and legacy reports continue to work."
    )
    files = {
        "lib/scan_report_adapter.dart": (
            "import 'dart:convert';\n"
            "class DependencyRecord { final String name; final String version; "
            "final String licenseExpression; final bool runtime; "
            "const DependencyRecord({required this.name, required this.version, "
            "required this.licenseExpression, required this.runtime}); }\n"
            "class ScanReportAdapter {\n"
            "  List<DependencyRecord> decode(String payload) {\n"
            "    final decoded = jsonDecode(payload);\n"
            "    if (decoded is! Map<String, dynamic>) throw const FormatException();\n"
            "    final components = decoded['components'];\n"
            "    if (components is! List) return const <DependencyRecord>[];\n"
            "    return const <DependencyRecord>[];\n"
            "  }\n"
            "}\n"
        ),
        "lib/license_gate.dart": (
            "class LicenseGate {\n"
            "  final Set<String> allowedLicenses;\n"
            "  LicenseGate(this.allowedLicenses);\n"
            "  bool allows(DependencyRecord record) {\n"
            "    return allowedLicenses.contains(record.licenseExpression);\n"
            "  }\n"
            "}\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert set(proposals) == set(files)
    assert "decoded['components']" in proposals["lib/scan_report_adapter.dart"]
    assert "decoded['document']" in proposals["lib/scan_report_adapter.dart"]
    assert "document['artifacts']" in proposals["lib/scan_report_adapter.dart"]
    assert "component['scope'] == 'runtime'" in proposals["lib/scan_report_adapter.dart"]
    assert "class _LicenseExpressionParser" in proposals["lib/license_gate.dart"]
    assert "bool _parseAnd()" in proposals["lib/license_gate.dart"]
    assert "bool _parseOr()" in proposals["lib/license_gate.dart"]
    assert reasoning.contract_repair_dimension(prompt, files) == "dependency"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_dart_offset_schedule_repair_uses_transition_and_candidate_offsets():
    prompt = (
        "A daily local wall-clock schedule is late on an offset change; the transition instant "
        "must use the new offset and the next run must use the offset at that run."
    )
    files = {
        "lib/offset_schedule.dart": (
            "class OffsetSchedule {\n"
            "  Duration offsetAt(DateTime instantUtc) {\n"
            "    var result = initialOffset;\n"
            "    for (final change in _changes) {\n"
            "      if (!instantUtc.isAfter(change.atUtc)) { break; }\n"
            "      result = change.offsetAfter;\n"
            "    }\n"
            "    return result;\n"
            "  }\n"
            "}\n"
        ),
        "lib/daily_window.dart": (
            "class DailyWindowScheduler {\n"
            "  DateTime nextRun(DateTime nowUtc) {\n"
            "    final localTarget = DateTime.utc(2026, 3, 8, 9);\n"
            "    return localTarget.subtract(offsets.offsetAt(nowUtc));\n"
            "  }\n"
            "}\n"
        ),
    }

    rejected = reasoning.contract_invariant_warnings(prompt, files)
    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert any("exact boundary" in warning for warning in rejected)
    assert any("candidate-time offset" in warning for warning in rejected)
    assert "instantUtc.isBefore(change.atUtc)" in proposals["lib/offset_schedule.dart"]
    assert "offsets.offsetAt(candidateUtc)" in proposals["lib/daily_window.dart"]
    assert reasoning.contract_repair_dimension(prompt, files) == "clock"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_dart_portable_export_repair_coordinates_name_and_collision_domains():
    prompt = (
        "Windows report directories reject some filenames, and labels differing only by letter "
        "case replace one another instead of receiving a numeric suffix in the shared folder."
    )
    files = {
        "lib/export_name.dart": (
            "String portableSegment(String input) {\n"
            "  final replaced = input.replaceAll(RegExp(r'[<>:\"/\\\\|?*\\x00-\\x1f]'), '_');\n"
            "  final cleaned = replaced.trim();\n"
            "  return cleaned.isEmpty ? 'untitled' : cleaned;\n"
            "}\n"
        ),
        "lib/report_bundle.dart": (
            "class ReportBundle {\n"
            "  final Set<String> _usedEntries = <String>{};\n"
            "  String allocateEntry(String folder, String title) {\n"
            "    var candidate = '$folder/$title.txt';\n"
            "    while (_usedEntries.contains(candidate)) { candidate = '$candidate (2)'; }\n"
            "    _usedEntries.add(candidate);\n"
            "    return candidate;\n"
            "  }\n"
            "}\n"
        ),
    }

    rejected = reasoning.contract_invariant_warnings(prompt, files)
    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert any("Windows reserved" in warning for warning in rejected)
    assert any("case-sensitive" in warning for warning in rejected)
    assert "com[1-9]" in proposals["lib/export_name.dart"]
    assert "lpt[1-9]" in proposals["lib/export_name.dart"]
    assert "[. ]+$" in proposals["lib/export_name.dart"]
    assert "candidate.toLowerCase()" in proposals["lib/report_bundle.dart"]
    assert reasoning.contract_repair_dimension(prompt, files) == "code"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_job_attempt_fencing_repair_covers_success_and_failure_settlement():
    prompt = (
        "A watchdog can requeue a job for a replacement worker, but the original worker may "
        "settle later; overlapping attempts must not change the replacement attempt's status."
    )
    files = {
        "src/job-store.mjs": (
            "export class JobStore {\n"
            "  #jobs = new Map();\n"
            "  claim() { const job = this.#jobs.values().next().value; job.attempt += 1; return job; }\n"
            "  requeueRunning(id) { return true; }\n"
            "  complete(id, result) {\n"
            "    const job = this.#jobs.get(id);\n"
            "    if (!job || job.status !== 'running') return false;\n"
            "    job.status = 'succeeded';\n"
            "    job.result = result;\n"
            "    return true;\n"
            "  }\n"
            "  fail(id, error) {\n"
            "    const job = this.#jobs.get(id);\n"
            "    if (!job || job.status !== 'running') return false;\n"
            "    job.status = 'queued';\n"
            "    job.lastError = String(error);\n"
            "    return true;\n"
            "  }\n"
            "}\n"
        ),
        "src/job-runner.mjs": (
            "export class JobRunner {\n"
            "  async runNext() {\n"
            "    const job = this.store.claim();\n"
            "    try {\n"
            "      const result = await this.execute(job.payload);\n"
            "      return this.store.complete(job.id, result);\n"
            "    } catch (error) {\n"
            "      return this.store.fail(job.id, error);\n"
            "    }\n"
            "  }\n"
            "}\n"
        ),
    }

    rejected = reasoning.contract_invariant_warnings(prompt, files)
    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert any("not both fenced" in warning for warning in rejected)
    assert any("both settlement branches" in warning for warning in rejected)
    assert proposals["src/job-store.mjs"].count("job.attempt !== attempt") == 2
    assert "complete(id, attempt, result)" in proposals["src/job-store.mjs"]
    assert "fail(id, attempt, error)" in proposals["src/job-store.mjs"]
    assert "complete(job.id, job.attempt, result)" in proposals["src/job-runner.mjs"]
    assert "fail(job.id, job.attempt, error)" in proposals["src/job-runner.mjs"]
    assert reasoning.contract_repair_dimension(prompt, files) == "state"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_terminal_state_contract_requires_predecessor_in_update_predicate():
    prompt = (
        "A queued job becomes running and then completed, but a late worker retry must not "
        "change completed or canceled terminal state."
    )
    invariants = reasoning.derive_contract_invariants(prompt)
    rejected = reasoning.contract_invariant_warnings(
        prompt,
        {
            "claim.sql": "UPDATE job SET state = 'running' WHERE job_id = :job_id;",
            "finish.sql": (
                "UPDATE job SET state = CASE WHEN state = 'completed' THEN state "
                "ELSE 'completed' END WHERE job_id = :job_id AND worker_id = :worker_id;"
            ),
        },
    )
    accepted = reasoning.contract_invariant_warnings(
        prompt,
        {
            "claim.sql": (
                "UPDATE job SET state = 'running' WHERE job_id = :job_id "
                "AND state = 'queued';"
            ),
            "finish.sql": (
                "UPDATE job SET state = 'completed' WHERE job_id = :job_id "
                "AND worker_id = :worker_id AND state = 'running';"
            ),
        },
    )

    assert any("matches exactly its allowed predecessor" in value for value in invariants)
    assert reasoning.contract_invariant_dimension(prompt) == "state"
    assert any("predecessor queued" in value for value in rejected)
    assert any("predecessor running" in value for value in rejected)
    assert accepted == []


def test_export_job_state_repair_adds_predecessor_and_worker_predicates():
    prompt = (
        "Export workers take over jobs already started; canceled jobs reappear as running and "
        "a late worker retry can change a completed result. Queued becomes running, then completed."
    )
    files = {
        "sql/claim.sql": (
            "UPDATE export_job SET state = 'running', worker_id = :worker_id "
            "WHERE job_id = :job_id;"
        ),
        "sql/finish.sql": (
            "UPDATE export_job SET state = 'completed', result_uri = :result_uri "
            "WHERE job_id = :job_id;"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert "AND state = 'queued'" in proposals["sql/claim.sql"]
    assert "AND state = 'running'" in proposals["sql/finish.sql"]
    assert "AND worker_id = :worker_id" in proposals["sql/finish.sql"]
    assert reasoning.contract_repair_dimension(prompt, files) == "state"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_measurement_contract_requires_shared_unit_lookup_for_derived_values():
    prompt = (
        "Supplier package lengths use a supported unit table; cubic volume and oversized "
        "routing must normalize non-centimeter dimensions."
    )
    invariants = reasoning.derive_contract_invariants(prompt)
    rejected = reasoning.contract_invariant_warnings(
        prompt,
        {
            "volume.sql": (
                "SELECT length_value * width_value * height_value AS volume_cm3 "
                "FROM shipping_package;"
            ),
            "oversize.sql": (
                "SELECT package_id FROM shipping_package "
                "WHERE max(length_value, width_value, height_value) >= :limit_cm;"
            ),
        },
    )
    accepted = reasoning.contract_invariant_warnings(
        prompt,
        {
            "volume.sql": (
                "SELECT (p.length_value * u.centimeters_per_unit) * "
                "(p.width_value * u.centimeters_per_unit) * "
                "(p.height_value * u.centimeters_per_unit) AS volume_cm3 "
                "FROM shipping_package p JOIN length_unit u ON u.unit_code = p.unit_code;"
            ),
            "oversize.sql": (
                "SELECT p.package_id FROM shipping_package p JOIN length_unit u "
                "ON u.unit_code = p.unit_code WHERE max("
                "p.length_value * u.centimeters_per_unit, "
                "p.width_value * u.centimeters_per_unit, "
                "p.height_value * u.centimeters_per_unit) >= :limit_cm;"
            ),
        },
    )

    assert any("Normalize every physical length" in value for value in invariants)
    assert reasoning.contract_invariant_dimension(prompt) == "data"
    assert len([value for value in rejected if "length-unit" in value]) == 2
    assert accepted == []


def test_package_unit_repair_normalizes_all_dimensions_before_derivation():
    prompt = (
        "Supplier packages use a supported length-unit table, but non-centimeter lengths have "
        "wrong cubic volume and oversized routing."
    )
    files = {
        "sql/volume.sql": (
            "SELECT p.package_id, p.length_value * p.width_value * p.height_value AS volume_cm3 "
            "FROM shipping_package AS p WHERE p.package_id = :package_id;"
        ),
        "sql/oversize.sql": (
            "SELECT p.package_id FROM shipping_package AS p "
            "WHERE max(p.length_value, p.width_value, p.height_value) >= :limit_cm;"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)

    for content in proposals.values():
        assert "JOIN length_unit AS lu" in content
        assert "lu.unit_code = p.unit_code" in content
        assert content.count("lu.centimeters_per_unit") == 3
    assert reasoning.contract_repair_dimension(prompt, files) == "data"
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_contract_invariant_guard_rejects_and_accepts_known_mechanisms():
    prompt = "A single-flight promise poisons later retry for the same key."
    rejected = reasoning.contract_invariant_warnings(
        prompt,
        {
            "inflight.ts": (
                "const pending = new Map();\n"
                "async function run(key, task) { try { return await task(); } "
                "catch (error) { pending.set(key, { error }); throw error; } }\n"
            ),
            "service.ts": "async function load() { try {} catch { return 'fallback'; } }\n",
        },
    )
    accepted = reasoning.contract_invariant_warnings(
        prompt,
        {
            "inflight.ts": (
                "const pending = new Map();\n"
                "async function run(key, task) { try { return await task(); } "
                "catch (error) { pending.delete(key); throw error; } }\n"
            ),
            "service.ts": "async function load() { return await run(); }\n",
        },
    )

    assert any("must delete/remove" in value for value in rejected)
    assert any("swallows the original error" in value for value in rejected)
    assert accepted == []

    assert reasoning.contract_invariant_warnings(
        "A partial unique index must prevent two open rows but allow closed history.",
        {"schema.sql": "CREATE UNIQUE INDEX x ON orders(account_id) WHERE status = 'open';"},
    ) == []
    assert reasoning.contract_invariant_warnings(
        "An AbortSignal must cross the provider adapter and AbortError stops retry.",
        {"provider.ts": "type ProviderAdapter = any; return adapter(signal);"},
    ) == []
    assert reasoning.contract_invariant_warnings(
        "An AbortSignal must cross the provider adapter.",
        {
            "provider.ts": (
                "type ProviderAdapter = any;\n"
                "function callProvider(client: ProviderAdapter, caller: AbortSignal) {\n"
                "  return client(caller);\n}\n"
            )
        },
    ) == []
    assert any(
        "exact signal" in warning
        for warning in reasoning.contract_invariant_warnings(
            "An AbortSignal must cross the provider adapter.",
            {
                "provider.ts": (
                    "type ProviderAdapter = any;\n"
                    "function callProvider(client: ProviderAdapter, caller: AbortSignal) {\n"
                    "  const detached = new AbortController(); return client(detached.signal);\n}\n"
                )
            },
        )
    )
    assert reasoning.contract_invariant_warnings(
        "A TTL cache uses an injected clock and refreshes expiration.",
        {
            "cache.dart": "if (_now().isAfter(entry.expiresAt)) return null;",
            "entry.dart": (
                "DateTime deadline;\n"
                "void refresh(T nextValue, DateTime nextDeadline) { deadline = nextDeadline; }"
            ),
        },
    ) == []
    assert reasoning.contract_invariant_warnings(
        prompt,
        {
            "inflight.ts": (
                "const requests = new Map<string, Promise<string>>();\n"
                "function singleFlight(id: string, task: () => Promise<string>) {\n"
                "  const operation = task(); void operation.catch(() => requests.delete(id));\n"
                "  requests.set(id, operation); return operation;\n}\n"
            )
        },
    ) == []
    assert reasoning.contract_invariant_warnings(
        "A subscription worker must cancel on stop.",
        {
            "subscription.dart": "return source.listen(onData);",
            "worker.dart": "Future<void> stop() async { await subscription.cancel(); }",
        },
    ) == []
    assert reasoning.contract_invariant_warnings(
        "A one-to-many join causes Cartesian multiplication.",
        {
            "report.sql": (
                "WITH fill_totals AS (SELECT order_id, SUM(quantity) quantity FROM fills GROUP BY order_id), "
                "fee_totals AS (SELECT order_id, SUM(amount) amount FROM fees GROUP BY order_id) "
                "SELECT * FROM orders LEFT JOIN fill_totals USING(order_id) LEFT JOIN fee_totals USING(order_id);"
            )
        },
    ) == []


def test_contract_repair_proposals_satisfy_supported_state_dart_and_sql_guards():
    state_prompt = "A single-flight promise poisons later retry for the same key."
    state_files = {
        "inflight.ts": (
            "const pending = new Map<string, Promise<string>>();\n"
            "export function singleFlight(\n"
            "  key: string, task: () => Promise<string>,\n"
            "): Promise<string> {\n"
            "  const operation = task();\n"
            "  pending.set(key, operation);\n"
            "  return operation;\n"
            "}\n"
        ),
        "service.ts": (
            "export async function loadUser(key: string, provider: () => Promise<string>): Promise<string> {\n"
            "  try { return await singleFlight(key, provider); } catch { return 'fallback'; }\n"
            "}\n"
        ),
    }
    state_proposals = reasoning.contract_repair_proposals(state_prompt, state_files)
    projected_state = {**state_files, **state_proposals}

    assert set(state_proposals) == {"inflight.ts", "service.ts"}
    assert "pending.delete(key)" in state_proposals["inflight.ts"]
    assert reasoning.contract_invariant_warnings(state_prompt, projected_state) == []

    cancellation_prompt = (
        "Propagate the caller AbortSignal through the provider adapter and make AbortError terminal for retry."
    )
    cancellation_files = {
        "provider.ts": (
            "type ProviderAdapter = (signal: AbortSignal) => Promise<string>;\n"
            "export async function callProvider(client: ProviderAdapter, caller: AbortSignal): Promise<string> {\n"
            "  const detached = new AbortController(); return client(detached.signal);\n}\n"
        ),
        "retry.ts": (
            "export async function retryRequest(client: ProviderAdapter, caller: AbortSignal): Promise<string> {\n"
            "  for (let attempt = 0; attempt < 2; attempt += 1) {\n"
            "    try { return await callProvider(client, caller); } catch (failure) { lastError = failure; }\n"
            "  } throw lastError;\n}\n"
        ),
    }
    cancellation_proposals = reasoning.contract_repair_proposals(
        cancellation_prompt,
        cancellation_files,
    )
    projected_cancellation = {**cancellation_files, **cancellation_proposals}

    assert set(cancellation_proposals) == set(cancellation_files)
    assert "return client(caller);" in cancellation_proposals["provider.ts"]
    assert "failure.name === 'AbortError'" in cancellation_proposals["retry.ts"]
    assert reasoning.contract_invariant_warnings(
        cancellation_prompt,
        projected_cancellation,
    ) == []

    sql_prompt = "A partial unique index must prevent two open rows but allow closed history."
    sql_files = {
        "schema.sql": (
            "CREATE UNIQUE INDEX x ON orders(account_id) WHERE status = 'closed';\n"
        )
    }
    sql_proposals = reasoning.contract_repair_proposals(sql_prompt, sql_files)

    assert "WHERE status = 'open'" in sql_proposals["schema.sql"]
    assert reasoning.contract_invariant_warnings(sql_prompt, sql_proposals) == []

    pending_prompt = "A partial unique index permits two pending jobs but must allow completed history."
    pending_proposals = reasoning.contract_repair_proposals(
        pending_prompt,
        {"jobs.sql": "CREATE UNIQUE INDEX x ON jobs(owner_id) WHERE status = 'completed';\n"},
    )
    ambiguous_proposals = reasoning.contract_repair_proposals(
        "A partial unique index should cover only the active-row predicate and reject two active rows.",
        {"jobs.sql": "CREATE UNIQUE INDEX x ON jobs(owner_id) WHERE status = 'completed';\n"},
    )

    assert "WHERE status = 'pending'" in pending_proposals["jobs.sql"]
    assert ambiguous_proposals == {}

    aggregate_prompt = (
        "Independent one-to-many payments and refunds are cross multiplied before aggregation."
    )
    aggregate_files = {
        "report.sql": (
            "SELECT customers.region, SUM(payments.cents) AS paid, SUM(refunds.cents) AS refunded\n"
            "FROM customers\n"
            "LEFT JOIN payments ON payments.customer_id = customers.id\n"
            "LEFT JOIN refunds ON refunds.customer_id = customers.id\n"
            "GROUP BY customers.region;\n"
        )
    }
    aggregate_proposals = reasoning.contract_repair_proposals(
        aggregate_prompt,
        aggregate_files,
    )

    assert "chili_payments_aggregate" in aggregate_proposals["report.sql"]
    assert "GROUP BY customer_id" in aggregate_proposals["report.sql"]
    assert "SUM(chili_refunds_aggregate.sum_cents)" in aggregate_proposals["report.sql"]
    assert reasoning.contract_invariant_warnings(
        aggregate_prompt,
        aggregate_proposals,
    ) == []

    clock_prompt = (
        "A TTL cache with an injected clock compares wall-clock expiration and refresh keeps the old expiry."
    )
    clock_files = {
        "lib/cache.dart": (
            "typedef Clock = DateTime Function();\n"
            "class Cache { final Clock _now; Cache(this._now);\n"
            "bool expired(DateTime expiry) => DateTime.now().isAfter(expiry); }\n"
        ),
        "lib/cache_entry.dart": (
            "class Entry<T> { Entry(this.value, this.expiresAt); T value; DateTime expiresAt;\n"
            "void refresh(T nextValue, DateTime nextExpiry) { value = nextValue; } }\n"
        ),
    }
    clock_proposals = reasoning.contract_repair_proposals(clock_prompt, clock_files)
    projected_clock = {**clock_files, **clock_proposals}

    assert set(clock_proposals) == set(clock_files)
    assert "_now().isAfter" in clock_proposals["lib/cache.dart"]
    assert "expiresAt = nextExpiry;" in clock_proposals["lib/cache_entry.dart"]
    assert reasoning.contract_invariant_warnings(clock_prompt, projected_clock) == []

    subscription_prompt = (
        "A subscription wrapper returns a dummy handle and worker stop must cancel the actual subscription."
    )
    subscription_files = {
        "lib/subscription.dart": (
            "StreamSubscription<T> bindSubscription<T>(Stream<T> source, void Function(T) onData) {\n"
            "  source.listen(onData); return Stream<T>.empty().listen((_) {});\n}\n"
        ),
        "lib/worker.dart": (
            "class Worker { StreamSubscription<String>? _subscription;\n"
            "Future<void> stop() async { _subscription = null; } }\n"
        ),
    }
    subscription_proposals = reasoning.contract_repair_proposals(
        subscription_prompt,
        subscription_files,
    )
    projected_subscription = {**subscription_files, **subscription_proposals}

    assert set(subscription_proposals) == set(subscription_files)
    assert "return source.listen(onData);" in subscription_proposals["lib/subscription.dart"]
    assert "await subscription.cancel();" in subscription_proposals["lib/worker.dart"]
    assert reasoning.contract_invariant_warnings(
        subscription_prompt,
        projected_subscription,
    ) == []


def test_async_rejection_slot_recognition_is_name_independent_and_guarded_after_rewrite(
    monkeypatch,
):
    prompt = (
        "Rejected async work remains in a keyed slot and gets reused; evict failures before retry."
    )
    source = (
        "const workByToken = new Map<string, Promise<number>>();\n"
        "export async function shareWork(\n"
        "  token: string, produce: () => Promise<number>,\n"
        "): Promise<number> {\n"
        "  const active = workByToken.get(token);\n"
        "  if (active) return active;\n"
        "  const request = produce();\n"
        "  workByToken.set(token, request);\n"
        "  return request;\n"
        "}\n"
    )
    guard_calls = []
    original_guard = reasoning.contract_invariant_warnings

    def recording_guard(statement, projected):
        guard_calls.append(dict(projected))
        return original_guard(statement, projected)

    monkeypatch.setattr(reasoning, "contract_invariant_warnings", recording_guard)

    proposals = reasoning.contract_repair_proposals(prompt, {"lib/work.ts": source})

    assert set(proposals) == {"lib/work.ts"}
    repaired = proposals["lib/work.ts"]
    assert "request.catch" in repaired
    assert "workByToken.get(token) === request" in repaired
    assert "workByToken.delete(token)" in repaired
    assert guard_calls == [{"lib/work.ts": repaired}]


def test_async_rejection_slot_recognition_adds_coalescing_without_fixture_names():
    prompt = "An async rejection is retained in a keyed cache slot and stale work is reused on retry."
    source = (
        "const jobs = new Map();\n"
        "export async function reuseOrStart(resource, createJob) {\n"
        "  const execution = createJob();\n"
        "  jobs.set(resource, execution);\n"
        "  return execution;\n"
        "}\n"
    )

    proposals = reasoning.contract_repair_proposals(prompt, {"runtime/share.mjs": source})
    repaired = proposals["runtime/share.mjs"]

    assert "const existing = jobs.get(resource);" in repaired
    assert "if (existing) return existing;" in repaired
    assert "jobs.get(resource) === execution" in repaired
    assert "jobs.delete(resource)" in repaired
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_async_rejection_slot_repair_abstains_when_the_owner_is_ambiguous():
    prompt = "Rejected async work remains in keyed slots and must be evicted before retry."
    source = (
        "const slots = new Map();\n"
        "async function first(key, start) {\n"
        "  const operation = start();\n"
        "  slots.set(key, operation);\n"
        "  return operation;\n"
        "}\n"
        "async function second(key, start) {\n"
        "  const operation = start();\n"
        "  slots.set(key, operation);\n"
        "  return operation;\n"
        "}\n"
    )

    assert reasoning.contract_repair_proposals(prompt, {"runtime/slots.mjs": source}) == {}


def test_class_slot_and_ordered_identity_repairs_transfer_across_names():
    prompt = (
        "A transient producer rejection survives every retry until restart while concurrent misses "
        "coalesce. Cache identity must preserve caller order after case normalization."
    )
    files = {
        "lib/priority-key.mjs": (
            "export function priorityKey(scope, choices) {\n"
            "  const canonical = Array.from(new Set(choices.map((item) => item.trim().toUpperCase()))).sort();\n"
            "  return JSON.stringify({ scope, canonical });\n"
            "}\n"
        ),
        "lib/work-registry.mjs": (
            "export class WorkRegistry {\n"
            "  #finished = new Map();\n"
            "  #running = new Map();\n"
            "  load(scope, producer) {\n"
            "    if (this.#running.has(scope)) return this.#running.get(scope);\n"
            "    const request = Promise.resolve().then(producer);\n"
            "    this.#running.set(scope, request);\n"
            "    request.then(\n"
            "      (result) => { this.#finished.set(scope, result); this.#running.delete(scope); },\n"
            "      (_reason) => {}\n"
            "    );\n"
            "    return request;\n"
            "  }\n"
            "}\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert set(proposals) == set(files)
    assert "new Set(choices.map" in proposals["lib/priority-key.mjs"]
    assert ")).sort()" not in proposals["lib/priority-key.mjs"]
    assert "this.#running.get(scope) === request" in proposals["lib/work-registry.mjs"]
    assert proposals["lib/work-registry.mjs"].count("this.#running.delete(scope)") == 2
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_structural_contract_repairs_abstain_across_multiple_candidate_owners():
    prompt = (
        "A rejected promise fails on every retry until restart despite coalescing, and caller order "
        "is contractual for cache identity."
    )
    slot = (
        "const slots = new Map();\n"
        "async function share(key, start) {\n"
        "  const pending = start();\n"
        "  slots.set(key, pending);\n"
        "  return pending;\n"
        "}\n"
    )
    key_owner = (
        "export function key(scope, values) {\n"
        "  const normalized = [...new Set(values.map((value) => value.toLowerCase()))].sort();\n"
        "  return JSON.stringify([scope, normalized]);\n"
        "}\n"
    )

    assert reasoning.contract_repair_proposals(
        prompt,
        {
            "lib/left-slot.mjs": slot,
            "lib/right-slot.mjs": slot.replace("slots", "requests"),
        },
    ) == {}
    assert reasoning.contract_repair_proposals(
        prompt,
        {"lib/left-key.mjs": key_owner, "lib/right-key.mjs": key_owner},
    ) == {}


def test_disclosed_node_cache_order_and_private_slot_fixture_is_repaired():
    prompt = (
        "After a caller requests French before English, a later caller requesting English before French "
        "can receive the French catalog. One transient loader rejection leaves every retry failing until "
        "the worker restarts. Concurrent successful misses invoke the loader once, locale tags compare "
        "case-insensitively, and caller preference order is contractual. Preserve request coalescing and "
        "cached object identity."
    )
    files = {
        "src/catalog-cache-key.mjs": (
            "export function catalogCacheKey({ tenant, preferredLocales }) {\n"
            "  const normalized = [...new Set(preferredLocales.map((locale) => locale.toLowerCase()))].sort();\n"
            "  return JSON.stringify([tenant, normalized]);\n"
            "}\n"
        ),
        "src/async-slot-cache.mjs": (
            "export class AsyncSlotCache {\n"
            "  #values = new Map();\n"
            "  #inflight = new Map();\n\n"
            "  get(key, loader) {\n"
            "    if (this.#values.has(key)) return Promise.resolve(this.#values.get(key));\n"
            "    if (this.#inflight.has(key)) return this.#inflight.get(key);\n"
            "    const pending = Promise.resolve().then(loader);\n"
            "    this.#inflight.set(key, pending);\n"
            "    pending.then(\n"
            "      (value) => { this.#values.set(key, value); this.#inflight.delete(key); },\n"
            "      () => {}\n"
            "    );\n"
            "    return pending;\n"
            "  }\n"
            "}\n"
        ),
        "src/locale-choice.mjs": "export function chooseLocale(values) { return values[0]; }\n",
        "src/catalog-service.mjs": "export class CatalogService {}\n",
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert set(proposals) == {"src/catalog-cache-key.mjs", "src/async-slot-cache.mjs"}
    assert ")).sort()" not in proposals["src/catalog-cache-key.mjs"]
    assert "this.#inflight.get(key) === pending" in proposals["src/async-slot-cache.mjs"]
    assert reasoning.contract_invariant_warnings(prompt, {**files, **proposals}) == []


def test_monotonic_materialized_head_recognition_transfers_across_sql_names():
    prompt = "A monotonic SQL materialized head lets stale sequence data replace newer metadata."
    variants = {
        "sql/document_head.sql": (
            "INSERT INTO document_heads "
            "(document_key, generation_no, body_json, observed_at, received_at)\n"
            "VALUES (:document_key, :generation_no, :body_json, :observed_at, :received_at)\n"
            "ON CONFLICT(document_key) DO UPDATE SET\n"
            "  generation_no = MAX(document_heads.generation_no, excluded.generation_no),\n"
            "  body_json = CASE WHEN excluded.generation_no > document_heads.generation_no "
            "THEN excluded.body_json ELSE document_heads.body_json END,\n"
            "  observed_at = excluded.observed_at,\n"
            "  received_at = excluded.received_at;\n"
        ),
        "sql/projection_head.sql": (
            "INSERT INTO projection_state AS current "
            "(stream_key, source_offset, rendered, checksum)\n"
            "VALUES (:stream_key, :source_offset, :rendered, :checksum)\n"
            "ON CONFLICT(stream_key) DO UPDATE SET\n"
            "  source_offset = GREATEST(excluded.source_offset, current.source_offset),\n"
            "  rendered = CASE WHEN excluded.source_offset >= current.source_offset "
            "THEN excluded.rendered ELSE current.rendered END,\n"
            "  checksum = excluded.checksum;\n"
        ),
    }

    proposals = {
        path: reasoning.contract_repair_proposals(prompt, {path: source})[path]
        for path, source in variants.items()
    }

    assert (
        "WHERE excluded.generation_no > document_heads.generation_no;"
        in proposals["sql/document_head.sql"]
    )
    assert (
        "WHERE excluded.source_offset >= current.source_offset;"
        in proposals["sql/projection_head.sql"]
    )
    assert "observed_at = excluded.observed_at" in proposals["sql/document_head.sql"]
    assert "received_at = excluded.received_at" in proposals["sql/document_head.sql"]
    assert "checksum = excluded.checksum" in proposals["sql/projection_head.sql"]
    assert reasoning.contract_invariant_warnings(prompt, proposals) == []


def test_monotonic_materialized_head_repair_abstains_on_conflicting_order_signals():
    prompt = "A monotonic SQL materialized head must ignore stale updates."
    source = (
        "INSERT INTO aggregate_heads AS stored (item_key, revision_no, source_offset, payload, note)\n"
        "VALUES (:item_key, :revision_no, :source_offset, :payload, :note)\n"
        "ON CONFLICT(item_key) DO UPDATE SET\n"
        "  revision_no = MAX(stored.revision_no, excluded.revision_no),\n"
        "  payload = CASE WHEN excluded.source_offset > stored.source_offset "
        "THEN excluded.payload ELSE stored.payload END,\n"
        "  note = excluded.note;\n"
    )

    assert reasoning.contract_repair_proposals(prompt, {"sql/ambiguous.sql": source}) == {}


def test_cross_file_monotonic_head_repair_transfers_across_sql_names():
    prompt = (
        "An older reconnect can regress stored head metadata and the current version read. "
        "Source sequence is the authoritative ordering for each scope."
    )
    files = {
        "sql/schema.sql": (
            "CREATE TABLE scopes (scope TEXT PRIMARY KEY);\n"
            "CREATE TABLE snapshots (\n"
            "  scope TEXT NOT NULL, snapshot_id TEXT NOT NULL, source_sequence INTEGER NOT NULL, payload TEXT,\n"
            "  PRIMARY KEY (scope, snapshot_id), UNIQUE (scope, source_sequence)\n"
            ");\n"
            "CREATE TABLE visible_snapshots (\n"
            "  scope TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, source_sequence INTEGER NOT NULL,\n"
            "  FOREIGN KEY (scope, snapshot_id) REFERENCES snapshots(scope, snapshot_id)\n"
            ");\n"
        ),
        "sql/update-visible.sql": (
            "CREATE TRIGGER snapshot_updates_visible AFTER INSERT ON snapshots BEGIN\n"
            "  INSERT INTO visible_snapshots(scope, snapshot_id, source_sequence)\n"
            "  VALUES (NEW.scope, NEW.snapshot_id, NEW.source_sequence)\n"
            "  ON CONFLICT(scope) DO UPDATE SET\n"
            "    snapshot_id = excluded.snapshot_id,\n"
            "    source_sequence = excluded.source_sequence;\n"
            "END;\n"
        ),
        "sql/read-visible.sql": (
            "SELECT s.scope, r.snapshot_id, r.payload, h.source_sequence\n"
            "FROM scopes AS s\n"
            "JOIN visible_snapshots AS h ON h.scope = s.scope\n"
            "JOIN snapshots AS r ON r.scope = s.scope\n"
            "WHERE r.rowid = (\n"
            "  SELECT MAX(candidate.rowid) FROM snapshots AS candidate\n"
            "  WHERE candidate.scope = s.scope\n"
            ") ORDER BY s.scope;\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)

    assert set(proposals) == {"sql/update-visible.sql", "sql/read-visible.sql"}
    assert (
        "WHERE excluded.source_sequence > visible_snapshots.source_sequence;"
        in proposals["sql/update-visible.sql"]
    )
    assert "r.scope = h.scope" in proposals["sql/read-visible.sql"]
    assert "r.snapshot_id = h.snapshot_id" in proposals["sql/read-visible.sql"]
    assert "rowid" not in proposals["sql/read-visible.sql"].lower()
    assert reasoning.contract_invariant_warnings(prompt, {**files, **proposals}) == []


def test_cross_file_monotonic_head_repair_abstains_on_order_or_owner_ambiguity():
    prompt = (
        "A stale import regresses the stored head and current version; sequence and revision ordering "
        "must remain monotonic."
    )
    schema = (
        "CREATE TABLE history (scope TEXT, item_id TEXT, sequence_no INTEGER, revision_no INTEGER, "
        "PRIMARY KEY(scope, item_id));\n"
        "CREATE TABLE heads (scope TEXT PRIMARY KEY, item_id TEXT, sequence_no INTEGER, revision_no INTEGER, "
        "FOREIGN KEY(scope, item_id) REFERENCES history(scope, item_id));\n"
    )
    ambiguous_upsert = (
        "INSERT INTO heads(scope, item_id, sequence_no, revision_no)\n"
        "VALUES (NEW.scope, NEW.item_id, NEW.sequence_no, NEW.revision_no)\n"
        "ON CONFLICT(scope) DO UPDATE SET item_id = excluded.item_id, "
        "sequence_no = excluded.sequence_no, revision_no = excluded.revision_no;\n"
    )
    read = (
        "SELECT r.* FROM scopes s JOIN heads h ON h.scope = s.scope "
        "JOIN history r ON r.scope = s.scope "
        "WHERE r.rowid = (SELECT MAX(c.rowid) FROM history c WHERE c.scope = s.scope);\n"
    )

    assert reasoning.contract_repair_proposals(
        prompt,
        {"schema.sql": schema, "head.sql": ambiguous_upsert, "read.sql": read},
    ) == {}
    single_order_upsert = ambiguous_upsert.replace(
        ", revision_no = excluded.revision_no",
        "",
    )
    assert reasoning.contract_repair_proposals(
        prompt,
        {
            "schema.sql": schema,
            "head-a.sql": single_order_upsert,
            "head-b.sql": single_order_upsert,
            "read.sql": read,
        },
    ) == {}


def test_cross_file_monotonic_head_repair_requires_unique_scoped_order():
    prompt = (
        "A stale import regresses the stored head and current version; source sequence is the "
        "authoritative ordering."
    )
    schema = (
        "CREATE TABLE history (scope TEXT, item_id TEXT, source_sequence INTEGER, "
        "PRIMARY KEY(scope, item_id));\n"
        "CREATE TABLE heads (scope TEXT PRIMARY KEY, item_id TEXT, source_sequence INTEGER, "
        "FOREIGN KEY(scope, item_id) REFERENCES history(scope, item_id));\n"
    )
    upsert = (
        "INSERT INTO heads(scope, item_id, source_sequence) "
        "VALUES (NEW.scope, NEW.item_id, NEW.source_sequence) "
        "ON CONFLICT(scope) DO UPDATE SET item_id = excluded.item_id, "
        "source_sequence = excluded.source_sequence;\n"
    )
    read = (
        "SELECT r.* FROM scopes s JOIN heads h ON h.scope = s.scope "
        "JOIN history r ON r.scope = s.scope "
        "WHERE r.rowid = (SELECT MAX(c.rowid) FROM history c WHERE c.scope = s.scope);\n"
    )

    assert reasoning.contract_repair_proposals(
        prompt,
        {"schema.sql": schema, "head.sql": upsert, "read.sql": read},
    ) == {}


def test_disclosed_sql_document_head_fixture_replays_monotonically():
    prompt = (
        "After an older reconnect batch is imported behind a newer approved current version, stored head "
        "metadata and the endpoint regress. Logical clocks are unique per document and are the authoritative "
        "ordering; SQLite row order is operational metadata only."
    )
    files = {
        "sql/001_schema.sql": (
            "CREATE TABLE documents (document_id TEXT PRIMARY KEY, title TEXT NOT NULL);\n"
            "CREATE TABLE document_versions (\n"
            "  document_id TEXT NOT NULL, version_id TEXT NOT NULL, logical_clock INTEGER NOT NULL,\n"
            "  body TEXT NOT NULL, import_batch TEXT NOT NULL,\n"
            "  PRIMARY KEY (document_id, version_id), UNIQUE (document_id, logical_clock)\n"
            ");\n"
            "CREATE TABLE document_heads (\n"
            "  document_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, logical_clock INTEGER NOT NULL,\n"
            "  FOREIGN KEY (document_id, version_id)\n"
            "    REFERENCES document_versions(document_id, version_id)\n"
            ");\n"
        ),
        "sql/002_head_triggers.sql": (
            "CREATE TRIGGER document_version_updates_head AFTER INSERT ON document_versions BEGIN\n"
            "  INSERT INTO document_heads(document_id, version_id, logical_clock)\n"
            "  VALUES (NEW.document_id, NEW.version_id, NEW.logical_clock)\n"
            "  ON CONFLICT(document_id) DO UPDATE SET\n"
            "    version_id = excluded.version_id, logical_clock = excluded.logical_clock;\n"
            "END;\n"
        ),
        "sql/read_current.sql": (
            "SELECT d.document_id, v.version_id, v.body, h.logical_clock\n"
            "FROM documents AS d\n"
            "JOIN document_heads AS h ON h.document_id = d.document_id\n"
            "JOIN document_versions AS v ON v.document_id = d.document_id\n"
            "WHERE v.rowid = (SELECT MAX(candidate.rowid) FROM document_versions AS candidate "
            "WHERE candidate.document_id = d.document_id)\n"
            "ORDER BY d.document_id;\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)
    projected = {**files, **proposals}

    assert set(proposals) == {"sql/002_head_triggers.sql", "sql/read_current.sql"}
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript(projected["sql/001_schema.sql"])
    database.executescript(projected["sql/002_head_triggers.sql"])
    database.executemany(
        "INSERT INTO documents(document_id, title) VALUES (?, ?)",
        [("policy", "Policy"), ("runbook", "Runbook")],
    )
    database.executemany(
        "INSERT INTO document_versions VALUES (?, ?, ?, ?, ?)",
        [
            ("policy", "p20", 20, "draft", "live-a"),
            ("runbook", "r4", 4, "boot", "live-a"),
            ("policy", "p35", 35, "ratified", "live-b"),
            ("runbook", "r7", 7, "recovered", "live-c"),
            ("policy", "p30", 30, "archive replay", "archive-restore"),
        ],
    )
    visible = {
        row["document_id"]: (row["version_id"], row["body"], row["logical_clock"])
        for row in database.execute(projected["sql/read_current.sql"])
    }

    assert visible == {
        "policy": ("p35", "ratified", 35),
        "runbook": ("r7", "recovered", 7),
    }
    assert reasoning.contract_invariant_warnings(prompt, projected) == []


def test_unknown_hypothesis_cannot_be_confirmed_when_known_family_has_evidence():
    case = {
        "case_id": "unknown-is-not-root-cause",
        "problem_statement": "A report duplicates rows across a one-to-many join.",
        "observations": [
            {
                "evidence_id": "unknown-a",
                "statement": "The outcome changed for an unknown reason.",
                "dimension": "unknown",
                "kind": "artifact",
                "provenance": "operator",
                "independence_key": "unknown-a",
                "reliability": 1.0,
            },
            {
                "evidence_id": "data-a",
                "statement": "The join multiplies each fill by every fee row.",
                "dimension": "data",
                "kind": "artifact",
                "provenance": "report.sql",
                "independence_key": "report.sql",
                "reliability": 0.95,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-unknown",
                "claim": "Unknown drift caused the report change.",
                "dimension": "unknown",
                "support_evidence_ids": ["unknown-a"],
                "falsification": "Identify a known causal family.",
            },
            {
                "hypothesis_id": "h-data",
                "claim": "A one-to-many join multiplies aggregates.",
                "dimension": "data",
                "support_evidence_ids": ["data-a"],
                "falsification": "Pre-aggregate child rows and compare totals.",
            },
        ],
        "experiments": [],
        "conclusion": {"hypothesis_id": "h-unknown", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["conclusion"]["dimension"] == "data"
    assert report["conclusion"]["status"] == "inconclusive"
    assert report["decision"] == "investigate"
    unknown = next(
        item for item in report["hypothesis_results"] if item["hypothesis_id"] == "h-unknown"
    )
    assert unknown["status"] == "untested"


def test_operator_wording_does_not_break_noncausal_family_tie():
    case = {
        "case_id": "state-over-trigger",
        "problem_statement": (
            "A provider failure permanently poisons a single-flight promise for the same key; "
            "later retries must start fresh."
        ),
        "observations": [
            {
                "evidence_id": "dependency-a",
                "statement": "The provider can fail temporarily.",
                "dimension": "dependency",
                "kind": "artifact",
                "provenance": "provider.ts",
                "independence_key": "provider.ts",
                "reliability": 0.9,
            },
            {
                "evidence_id": "dependency-b",
                "statement": "The user service calls the provider.",
                "dimension": "dependency",
                "kind": "artifact",
                "provenance": "service.ts",
                "independence_key": "service.ts",
                "reliability": 0.9,
            },
            {
                "evidence_id": "state-a",
                "statement": "A rejected promise remains in the pending map for the key.",
                "dimension": "state",
                "kind": "artifact",
                "provenance": "inflight.ts",
                "independence_key": "inflight.ts",
                "reliability": 0.95,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-dependency",
                "claim": "Provider instability is the root cause.",
                "dimension": "dependency",
                "support_evidence_ids": ["dependency-a", "dependency-b"],
                "falsification": "Hold local state constant and replace the provider.",
            },
            {
                "hypothesis_id": "h-state",
                "claim": "Rejected in-flight state poisons later retries.",
                "dimension": "state",
                "support_evidence_ids": ["state-a"],
                "falsification": "Evict only rejected work and retry the same key.",
            },
        ],
        "experiments": [],
        "conclusion": {"hypothesis_id": "h-dependency", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["conclusion"]["dimension"] == "dependency"
    assert report["conclusion"]["status"] == "inconclusive"


def test_local_judge_cannot_relabel_clock_evidence_as_data_support():
    wrong_packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-data",
                "claim": "The replay data changed.",
                "dimension": "data",
                "support_evidence_ids": ["clock-result", "caller-source"],
                "contradict_evidence_ids": [],
                "falsification": "Hold data constant and compare outcomes.",
            }
        ],
        "experiments": [],
        "conclusion": {
            "hypothesis_id": "h-data",
            "status": "confirmed",
            "evidence_ids": ["clock-result", "caller-source"],
        },
    }

    result = reasoning.run_local_diagnostic_debate(
        _sim_clock_case(),
        lambda stage, prompt: json.dumps(wrong_packet),
        stages_to_run=("judge",),
    )

    assert result["stages"][0]["accepted"] is True
    assert "h-data:dropped_mismatched_support" in result["stages"][0]["contract_repairs"]
    assert result["packet"]["hypotheses"][0]["support_evidence_ids"] == []
    assert result["report"]["conclusion"]["dimension"] == "clock"


def test_empty_or_unknown_conclusion_packet_is_invalid():
    empty = reasoning.evaluate_packet(_sim_clock_case(), {})
    unknown = reasoning.evaluate_packet(
        _sim_clock_case(),
        {
            "hypotheses": [
                {
                    "hypothesis_id": "h-clock",
                    "claim": "Clock drift caused the mismatch.",
                    "dimension": "clock",
                    "support_evidence_ids": ["clock-result"],
                    "falsification": "Substitute only the replay clock.",
                }
            ],
            "conclusion": {
                "hypothesis_id": "h-clock",
                "status": "provisional",
                "evidence_ids": ["invented-evidence"],
            },
        },
    )

    assert empty["valid"] is False
    assert "At least one falsifiable hypothesis is required." in empty["errors"]
    assert any("unknown evidence" in error for error in unknown["errors"])


def test_discriminating_controls_keep_negative_polarity_but_mixed_interventions_support():
    healthy = reasoning.normalize_evidence(
        {
            "evidence_id": "healthy-control",
            "statement": "The clean worker remains healthy and checksums are identical.",
            "kind": "experiment",
            "discriminating": True,
        }
    )
    intervention = reasoning.normalize_evidence(
        {
            "evidence_id": "mixed-intervention",
            "statement": (
                "The affected revision reproduces the fault while the earlier revision remains correct."
            ),
            "kind": "experiment",
            "discriminating": True,
        }
    )
    negated_intervention = reasoning.normalize_evidence(
        {
            "evidence_id": "negated-intervention",
            "statement": "Changing only the cache does not change the failure.",
            "kind": "experiment",
            "discriminating": True,
        }
    )

    assert healthy["causal_role"] == "contradiction"
    assert intervention["causal_role"] == "support"
    assert negated_intervention["causal_role"] == "contradiction"


def test_inferred_dimension_is_advisory_but_explicit_dimension_is_hard_owned():
    case = reasoning.normalize_case(
        {
            "case_id": "dimension-ownership",
            "problem_statement": "An output changed after a source edit.",
            "observations": [
                {
                    "evidence_id": "inferred-code",
                    "statement": "Reverting only the source hunk restores the output.",
                    "kind": "experiment",
                    "discriminating": True,
                },
                {
                    "evidence_id": "explicit-code",
                    "statement": "Reverting only the source hunk restores the output.",
                    "dimension": "code",
                    "kind": "experiment",
                    "discriminating": True,
                },
            ],
        }
    )
    inferred_packet = reasoning.normalize_packet(
        {
            "hypotheses": [
                {
                    "hypothesis_id": "h-data",
                    "claim": "A transformed input caused the output.",
                    "dimension": "data",
                    "support_evidence_ids": ["inferred-code"],
                    "falsification": "Hold the transformed input constant.",
                }
            ],
            "conclusion": {"hypothesis_id": "h-data", "status": "provisional"},
        }
    )
    explicit_packet = reasoning.normalize_packet(
        {
            **inferred_packet,
            "hypotheses": [
                {
                    **inferred_packet["hypotheses"][0],
                    "support_evidence_ids": ["explicit-code"],
                }
            ],
        }
    )

    assert case["observations"][0]["dimension_origin"] == "inferred"
    assert case["observations"][1]["dimension_origin"] == "explicit"
    assert not any(
        "different evidence family" in error
        for error in reasoning._validate_packet(case, inferred_packet)
    )
    assert any(
        "different evidence family" in error
        for error in reasoning._validate_packet(case, explicit_packet)
    )


def test_case_normalization_preserves_dimension_origin_idempotently():
    once = reasoning.normalize_case(
        {
            "case_id": "dimension-origin-idempotence",
            "problem_statement": "A source stage plan changed.",
            "observations": [
                {
                    "evidence_id": "inferred",
                    "statement": "The stage plan differs at one ordering point.",
                },
                {
                    "evidence_id": "explicit",
                    "statement": "The stage plan differs at one ordering point.",
                    "dimension": "code",
                },
            ],
        }
    )
    twice = reasoning.normalize_case(once)

    assert [item["dimension"] for item in twice["observations"]] == [
        item["dimension"] for item in once["observations"]
    ]
    assert [item["dimension_origin"] for item in twice["observations"]] == [
        "inferred",
        "explicit",
    ]


def test_prompt_and_source_heuristics_remain_inferred_not_explicit(tmp_path):
    source = tmp_path / "owner.py"
    source.write_text(
        "def order_stages(stages):\n    return sorted(stages)\n",
        encoding="utf-8",
    )

    case = reasoning.build_case_from_prompt(
        "The source stage ordering changed after a refactor.",
        repo_path=tmp_path,
        candidate_paths=["owner.py"],
    )

    assert case["observations"]
    heuristic = [
        item
        for item in case["observations"]
        if str(item.get("provenance") or "").startswith(
            ("operator_prompt:", "owner.py:")
        )
    ]
    assert heuristic
    assert all(item["dimension_origin"] != "explicit" for item in heuristic)
    assert all(
        item["dimension_origin"] == "inferred"
        for item in heuristic
        if item["dimension"] != "unknown"
    )


def test_post_probe_family_change_requires_stronger_causal_evidence():
    previous = {
        "conclusion": {
            "dimension": "config",
            "status": "provisional",
            "causal_sufficiency": "observational",
            "evidence_ids": ["old"],
        }
    }
    weak_candidate = {
        "conclusion": {
            "dimension": "data",
            "status": "provisional",
            "causal_sufficiency": "observational",
            "evidence_ids": ["probe-weak"],
        }
    }
    strong_candidate = {
        "conclusion": {
            "dimension": "clock",
            "status": "provisional",
            "causal_sufficiency": "isolated",
            "evidence_ids": ["probe-strong"],
        }
    }

    rejected = reasoning.evidence_gated_report_revision(
        previous,
        weak_candidate,
        [
            {
                "evidence_id": "probe-weak",
                "dimension_origin": "inferred",
                "causal_role": "support",
                "evidence_lifecycle": "observed_result",
                "discriminating": True,
            }
        ],
    )
    accepted = reasoning.evidence_gated_report_revision(
        previous,
        strong_candidate,
        [],
    )

    assert rejected["accepted"] is False
    assert rejected["reason"] == "causal_family_change_lacks_stronger_evidence"
    assert accepted["accepted"] is True
    assert accepted["reason"] == "stronger_causal_sufficiency"


def test_council_prompts_share_specific_causal_ownership_rubric():
    case = {"case_id": "rubric", "problem_statement": "Diagnose retry delay."}

    prompt = reasoning.investigator_prompt(case)

    assert "not vector clocks" in prompt
    assert "wall/event time" in prompt
    assert "retry budgets" in prompt
    assert "wire protocol" in prompt


def test_realworld_boundary_language_yields_mechanical_contract_invariants():
    vary = reasoning.derive_contract_invariants(
        "Vary values have mixed casing and a wildcard response is still found in cache."
    )
    retry = reasoning.derive_contract_invariants(
        "Numeric Retry-After exceeds the remaining allowance and an explicit immediate retry is dropped."
    )
    interval = reasoning.derive_contract_invariants(
        "A capability remains visible at the exact instant its grant expires."
    )
    scoped = reasoning.derive_contract_invariants(
        "Two tenants reuse the same identifier and one row collapses."
    )
    convergence = reasoning.derive_contract_invariants(
        "Replicas concurrent after reconnect need convergence bookkeeping; deletion resurrects."
    )

    assert any("normalize both sides" in value for value in vary)
    assert any("non-cacheable" in value for value in vary)
    assert any("converts to milliseconds exactly once" in value for value in retry)
    assert any("zero retry delay" in value for value in retry)
    assert any("delay actually granted" in value for value in retry)
    assert any("exclusive upper bound" in value for value in interval)
    assert any("same composite identity" in value for value in scoped)
    assert any("tombstones win" in value for value in convergence)


def test_retry_contract_operator_transfers_across_field_and_local_names():
    prompt = (
        "Numeric Retry-After exceeds the remaining allowance; an explicit zero delay is "
        "dropped and queue time uses the requested duration."
    )
    files = {
        "lib/parse.mjs": (
            "export function parseRetryAfter(value, nowMs) {\n"
            "  const text = String(value).trim();\n"
            "  if (/^\\d+$/.test(text)) return Number(text);\n"
            "  return Date.parse(text) - nowMs;\n"
            "}\n"
        ),
        "lib/cap.mjs": (
            "export class RetryBudget {\n"
            "  constructor(capMs) { this.capMs = capMs; this.consumedMs = 0; }\n"
            "  claim(askedMs) {\n"
            "    if (askedMs < 0 || this.consumedMs + askedMs > this.capMs) return null;\n"
            "    this.consumedMs += askedMs;\n"
            "    return askedMs;\n"
            "  }\n"
            "}\n"
        ),
        "lib/queue.mjs": (
            "export function queue(retryAfter, nowMs, budget, enqueue) {\n"
            "  const askedMs = parseRetryAfter(retryAfter, nowMs);\n"
            "  const allowedMs = budget.claim(askedMs);\n"
            "  if (!allowedMs) return null;\n"
            "  const runAt = nowMs + askedMs;\n"
            "  enqueue(runAt);\n"
            "  return runAt;\n"
            "}\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)
    projected = {path: proposals.get(path, content) for path, content in files.items()}

    assert set(proposals) == set(files)
    assert "Number(text) * 1000" in projected["lib/parse.mjs"]
    assert "Math.min(askedMs, remainingMs)" in projected["lib/cap.mjs"]
    assert "allowedMs === null" in projected["lib/queue.mjs"]
    assert "nowMs + allowedMs" in projected["lib/queue.mjs"]
    assert reasoning.contract_invariant_warnings(prompt, projected) == []


def test_query_range_and_event_time_operators_preserve_generic_contracts():
    query_prompt = "Duplicate query parameters and blank values are lost before canonical wire signing."
    query_files = {
        "wire/url.py": (
            "from urllib.parse import parse_qsl, urlencode\n"
            "def render(url):\n"
            "    pairs = dict(parse_qsl(url, keep_blank_values=True))\n"
            "    return urlencode(sorted(pairs.items()))\n"
        )
    }
    query_proposals = reasoning.contract_repair_proposals(query_prompt, query_files)
    assert "pairs = parse_qsl" in query_proposals["wire/url.py"]
    assert "urlencode(sorted(pairs))" in query_proposals["wire/url.py"]
    assert reasoning.contract_invariant_warnings(query_prompt, query_proposals) == []

    range_prompt = "Inclusive Content-Range chunk boundaries reject adjacent independently retried chunks."
    range_files = {
        "lib/range.dart": "int get length => end - start;\n",
        "lib/ledger.dart": (
            "final separated = range.end < existing.start - 1 ||\n"
            "    range.start > existing.end + 1;\n"
        ),
        "lib/assemble.dart": "offsets.sort((a, b) => a.toString().compareTo(b.toString()));\n",
    }
    range_proposals = reasoning.contract_repair_proposals(range_prompt, range_files)
    assert set(range_proposals) == set(range_files)
    assert reasoning.contract_invariant_warnings(range_prompt, range_proposals) == []

    sql_prompt = (
        "Out of order telemetry correction replay mixes metadata, and event-time hourly buckets use receipt time."
    )
    sql_files = {
        "sql/upsert.sql": (
            "INSERT INTO samples (site_id, sensor_id, event_id, observed_at, received_at, value) "
            "VALUES (:site_id, :sensor_id, :event_id, :observed_at, :received_at, :value)\n"
            "ON CONFLICT(site_id, sensor_id, event_id) DO UPDATE SET\n"
            "observed_at = excluded.observed_at, received_at = MAX(samples.received_at, excluded.received_at),\n"
            "value = CASE WHEN excluded.received_at >= samples.received_at THEN excluded.value ELSE samples.value END;\n"
        ),
        "sql/rollup.sql": (
            "SELECT strftime('%Y-%m-%dT%H:00:00Z', received_at, 'unixepoch') AS hour "
            "FROM samples WHERE received_at >= :start AND received_at < :end;\n"
        ),
    }
    sql_proposals = reasoning.contract_repair_proposals(sql_prompt, sql_files)
    projected_sql = {
        path: sql_proposals.get(path, content) for path, content in sql_files.items()
    }
    assert "WHERE excluded.received_at >= samples.received_at" in projected_sql["sql/upsert.sql"]
    assert "strftime('%Y-%m-%dT%H:00:00Z', observed_at" in projected_sql["sql/rollup.sql"]
    assert "WHERE observed_at >= :start" in projected_sql["sql/rollup.sql"]
    assert reasoning.contract_invariant_warnings(sql_prompt, projected_sql) == []


def test_vector_clock_operator_uses_declared_parameter_names():
    prompt = (
        "Replicas concurrent after reconnect need convergence bookkeeping and a deletion "
        "must win an equal-time tie."
    )
    files = {
        "lib/clock.dart": (
            "enum ClockOrder { before, after, equal, concurrent }\n"
            "ClockOrder compareClocks(Map<String, int> lhs, Map<String, int> rhs) {\n"
            "  var less = false; var greater = false;\n"
            "  for (final actor in lhs.keys) {\n"
            "    final a = lhs[actor]!; final b = rhs[actor] ?? a;\n"
            "    if (a < b) less = true; if (a > b) greater = true;\n"
            "  }\n"
            "  if (less && greater) return ClockOrder.concurrent;\n"
            "  if (less) return ClockOrder.before; if (greater) return ClockOrder.after;\n"
            "  return ClockOrder.equal;\n"
            "}\n"
        ),
        "lib/resolve.dart": (
            "import 'clock.dart';\n"
            "class SyncRecord { dynamic clock, modifiedAt, deleted, deviceId; "
            "SyncRecord withClock(dynamic value) => this; }\n"
            "SyncRecord resolveRecord(SyncRecord current, SyncRecord incoming) {\n"
            "  final order = compareClocks(current.clock, incoming.clock);\n"
            "  if (order == ClockOrder.before) return incoming;\n"
            "  return current;\n"
            "}\n"
        ),
        "lib/engine.dart": (
            "import 'resolve.dart';\n"
            "class Engine { final records = <String, SyncRecord>{};\n"
            "  void applyRemote(SyncRecord incoming) {\n"
            "    final local = records[incoming.id];\n"
            "    if (local == null) { records[incoming.id] = incoming; return; }\n"
            "    records[incoming.id] = resolveRecord(local, incoming);\n"
            "  }\n"
            "}\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)
    projected = {path: proposals.get(path, content) for path, content in files.items()}

    assert "...lhs.keys, ...rhs.keys" in projected["lib/clock.dart"]
    assert "current.deleted != incoming.deleted" in projected["lib/resolve.dart"]
    assert "joinClocks(local.clock, incoming.clock)" in projected["lib/engine.dart"]
    assert reasoning.contract_invariant_warnings(prompt, projected) == []


def test_scoped_retry_operator_transfers_to_alternate_storage_attribute():
    prompt = (
        "Two tenants reuse the same client retry token; retries move stock twice and emit duplicate messages "
        "across attempt numbers."
    )
    files = {
        "holds/ledger.py": (
            "class Hold: pass\n"
            "class Ledger:\n"
            "    def __init__(self):\n"
            "        self._entries: dict[str, Hold] = {}\n"
            "    def find(self, tenant_id, request_id):\n"
            "        return self._entries.get(request_id)\n"
            "    def reserve(self, tenant_id, request_id, sku, quantity):\n"
            "        hold = Hold()\n"
            "        self._entries[request_id] = hold\n"
            "        return hold\n"
        ),
        "holds/events.py": (
            "def created(hold, attempt):\n"
            "    return {'event_id': f\"hold:{hold.request_id}:{attempt}\"}\n"
        ),
        "holds/service.py": (
            "class Service:\n"
            "    def reserve(self, tenant_id, request_id, sku, quantity, attempt):\n"
            "        reservation = self.ledger.reserve(\n"
            "            tenant_id, request_id, sku, quantity\n"
            "        )\n"
            "        self.publisher.publish(created(reservation, attempt))\n"
            "        return reservation\n"
        ),
    }

    proposals = reasoning.contract_repair_proposals(prompt, files)
    projected = {path: proposals.get(path, content) for path, content in files.items()}

    assert "self._entries: dict[tuple[str, str], Hold]" in projected["holds/ledger.py"]
    assert "self._entries.get((tenant_id, request_id))" in projected["holds/ledger.py"]
    assert "self._entries[(tenant_id, request_id)]" in projected["holds/ledger.py"]
    assert "{hold.tenant_id}:{hold.request_id}" in projected["holds/events.py"]
    assert "existing = self.ledger.find(tenant_id, request_id)" in projected["holds/service.py"]
    assert reasoning.contract_invariant_warnings(prompt, projected) == []


def test_relay_operator_preserves_raw_wire_time_with_alternate_local_name():
    prompt = (
        "A forwarder wire rollout combines secret rotation, v2 millisecond timestamps, and repeated "
        "parameters; preserve freshness and age checks."
    )
    files = {
        "relay/url.py": (
            "from urllib.parse import parse_qsl, urlencode\n"
            "def canonical(value):\n"
            "    fields = dict(parse_qsl(value, keep_blank_values=True))\n"
            "    return urlencode(sorted(fields.items()))\n"
        ),
        "relay/ring.py": (
            "class Ring:\n"
            "    def candidates(self, key_id, seen_at):\n"
            "        return [key for key in self.keys if key.key_id == key_id "
            "and key.valid_from <= seen_at < key.valid_until]\n"
        ),
        "relay/check.py": (
            "def verify(headers, keyring, key_id, now):\n"
            "    wire_timestamp = headers.get('X-Relay-Timestamp')\n"
            "    issued = int(wire_timestamp)\n"
            "    message = f'{wire_timestamp}\\n'.encode()\n"
            "    return message, keyring.candidates(key_id, now), issued\n"
        ),
    }
    files["relay/ring.py"] = files["relay/ring.py"].replace(
        "class Ring:\n",
        "class Ring:\n    grace_seconds = 30\n",
    )

    proposals = reasoning.contract_repair_proposals(prompt, files)
    projected = {path: proposals.get(path, content) for path, content in files.items()}

    assert "issued_value = int(wire_timestamp)" in projected["relay/check.py"]
    assert "if issued_value >= 100_000_000_000" in projected["relay/check.py"]
    assert "candidates(key_id, issued)" in projected["relay/check.py"]
    assert "f'{wire_timestamp}\\n'" in projected["relay/check.py"]
    assert "key.valid_until + self.grace_seconds" in projected["relay/ring.py"]
    assert reasoning.contract_invariant_warnings(prompt, projected) == []


def test_vary_and_scoped_interval_operators_transfer_to_alternate_shapes():
    vary_prompt = (
        "Vary fields use mixed casing and extra whitespace; wildcard responses remain in cache."
    )
    vary_files = {
        "edge/vary.mjs": (
            "export function parseVary(raw) { return raw.split(',').filter(Boolean); }\n"
        ),
        "edge/key.mjs": (
            "export function headerValue(headers, name) { return headers[name] ?? ''; }\n"
            "export function makeVariantKey(path, fields, headers) {\n"
            "  return JSON.stringify([path, ...fields.map((field) => `${field}:${headerValue(headers, field)}`)]);\n"
            "}\n"
        ),
        "edge/store.mjs": (
            "class EdgeStore {\n"
            "  put(path, requestHeaders, response) {\n"
            "    const fields = parseVary(response.vary);\n"
            "    this.entries.set(makeVariantKey(path, fields, requestHeaders), response);\n"
            "  }\n"
            "}\n"
        ),
    }
    vary_proposals = reasoning.contract_repair_proposals(vary_prompt, vary_files)
    projected_vary = {
        path: vary_proposals.get(path, content) for path, content in vary_files.items()
    }
    assert "part.trim().toLowerCase()" in projected_vary["edge/vary.mjs"]
    assert "Object.entries(headers ?? {})" in projected_vary["edge/key.mjs"]
    assert 'fields.includes("*")' in projected_vary["edge/store.mjs"]
    assert reasoning.contract_invariant_warnings(vary_prompt, projected_vary) == []

    sql_prompt = (
        "Two organizations share the same principal identifier, and a permission remains visible at the exact "
        "instant its active interval expires."
    )
    sql_files = {
        "schema.sql": (
            "CREATE TABLE grants (org_id TEXT, account_id TEXT, permission TEXT, "
            "active_from INTEGER, active_until INTEGER);\n"
            "CREATE UNIQUE INDEX uq_grant ON grants(account_id, permission);\n"
        ),
        "upsert.sql": (
            "INSERT INTO grants (org_id, account_id, permission, active_from, active_until) "
            "VALUES (:org_id, :account_id, :permission, :active_from, :active_until)\n"
            "ON CONFLICT(account_id, permission) DO UPDATE SET active_until = excluded.active_until;\n"
        ),
        "effective.sql": (
            "SELECT permission FROM grants WHERE org_id = :org_id AND account_id = :account_id "
            "AND :instant BETWEEN active_from AND active_until;\n"
        ),
    }
    sql_proposals = reasoning.contract_repair_proposals(sql_prompt, sql_files)
    projected_sql = {
        path: sql_proposals.get(path, content) for path, content in sql_files.items()
    }
    assert "ON grants(org_id, account_id, permission)" in projected_sql["schema.sql"]
    assert "ON CONFLICT(org_id, account_id, permission)" in projected_sql["upsert.sql"]
    assert "active_from <= :instant AND :instant < active_until" in projected_sql[
        "effective.sql"
    ]
    assert reasoning.contract_invariant_warnings(sql_prompt, projected_sql) == []


def test_causal_taxonomy_disambiguates_protocol_policy_and_logical_state():
    assert reasoning.infer_dimension(
        "Vary header values use mixed casing and a wildcard response follows the wrong policy."
    ) == "config"
    assert reasoning.infer_dimension(
        "Numeric Retry-After exceeds the remaining allowance and queue time is wrong."
    ) == "clock"
    assert reasoning.infer_dimension(
        "A forwarder rollout disagrees with wire behavior during secret rotation."
    ) == "dependency"
    assert reasoning.infer_dimension(
        "Replicas concurrent after reconnect need convergence bookkeeping for a tombstone."
    ) == "state"
    assert reasoning.infer_dimension(
        "Inclusive Content-Range boundaries reconstruct bytes out of order."
    ) == "data"


def test_decisive_taxonomy_requires_score_and_margin():
    assert reasoning.decisive_inferred_dimension(
        "Numeric Retry-After exceeds the remaining retry budget and queue delay."
    ) == "clock"
    assert reasoning.decisive_inferred_dimension(
        "Secret rotation changed the signed wire protocol and retired-key grace window."
    ) == "dependency"
    assert reasoning.decisive_inferred_dimension(
        "The behavior changed and more evidence is needed."
    ) == "unknown"


def test_generic_mechanism_vocabulary_maps_to_causal_families_and_ties_stay_unknown():
    assert reasoning.infer_dimension(
        "The source stage plan differs at one ordering point."
    ) == "code"
    assert reasoning.infer_dimension(
        "The rendered policy denies the exact service principal."
    ) == "config"
    assert reasoning.infer_dimension(
        "A transitive package version changed in the signed dependency bundle."
    ) == "dependency"
    assert reasoning.infer_dimension(
        "The visual qualification comparison record used a floating runner."
    ) == "test_harness"
    assert reasoning.infer_dimension(
        "The rebuild ordered events by producer event time instead of broker sequence."
    ) == "clock"
    assert reasoning.infer_dimension(
        "Unicode identifier normalization restores every route join."
    ) == "data"
    assert reasoning.infer_dimension(
        "A process snapshot found a loaded module hash outside the signed image inventory."
    ) == "runtime"
    assert reasoning.infer_dimension(
        "Memory-control termination occurs above the effective container boundary."
    ) == "runtime"
    assert reasoning.infer_dimension(
        "The durable workflow row is orphaned because transition rules never restore a claimable state."
    ) == "state"
    assert reasoning.infer_dimension(
        "A browser profile and service-worker persist across a parallel test shard."
    ) == "test_harness"
    assert reasoning.infer_dimension(
        "The effective settings snapshot adds a leading slash to the topic filter."
    ) == "config"
    assert reasoning.infer_dimension(
        "The deployed paging function chooses its cursor after filtering."
    ) == "code"
    assert reasoning.infer_dimension(
        "Two locked package versions parse the same calendar feed differently."
    ) == "dependency"
    assert reasoning.infer_dimension(
        "Replacing only each shortened key restores the exact join to the canonical identifier."
    ) == "data"
    assert reasoning.infer_dimension(
        "An offline topic-matcher compares the rendered topic filter."
    ) == "config"
    assert reasoning.infer_dimension(
        "A local wall-time value is recomputed under a different parsing zone."
    ) == "clock"
    assert reasoning.infer_dimension("clock state") == "unknown"


def test_evidence_dimension_prefers_changed_variable_over_held_constants():
    examples = {
        "dependency": (
            "Pinning only the prior resolved component restores output without changing "
            "application code, settings, data, or runtime image."
        ),
        "code": (
            "Reverting only the half-open interval predicate semantics accepts shared "
            "endpoints and leaves policy and controller output unchanged."
        ),
        "data": (
            "A sealed proof rejects reused producer identities and accepts a "
            "collision-resistant identity while legacy records remain unchanged."
        ),
        "config": (
            "The effective listener dump shows a rendered server-name value from the "
            "region-alias environment setting."
        ),
        "test_harness": (
            "An isolated runner can capture injected-input acknowledgments and virtual "
            "speech-device readiness in the retained trace."
        ),
        "clock": (
            "An offline replay fails with offset-free local values and succeeds when "
            "the retained UTC instants keep their offsets."
        ),
    }

    assert {
        reasoning.infer_evidence_dimension(statement)
        for statement in examples.values()
    } == set(examples)
    for expected, statement in examples.items():
        assert reasoning.infer_evidence_dimension(statement) == expected


def test_evidence_dimension_distinguishes_keys_settings_packages_and_matchers():
    examples = {
        "data": (
            "Changing only the duplicated facility key to an unused key restores "
            "the route-stop table lookup."
        ),
        "config": (
            "Changing only OFFLINE_GRACE_SECONDS in the effective-settings snapshot "
            "restores the approved profile behavior."
        ),
        "dependency": (
            "Adding the exact signed compatibility package restores libheif.so.1 "
            "without changing the worker image or launch settings."
        ),
        "code": (
            "The release comparison shows the interval matcher replaced a two-bound "
            "overlap check with a one-bound check."
        ),
    }

    for expected, statement in examples.items():
        assert reasoning.infer_evidence_dimension(statement) == expected


def test_evidence_metadata_is_bounded_visible_and_idempotent():
    raw = {
        "evidence_id": "metadata-code",
        "statement": "A deterministic comparison isolates one implementation change.",
        "dimension": "unknown",
        "kind": "artifact",
        "metadata": {
            "code": {
                "good_semantics": "start < other_end and other_start < end",
                "bad_semantics": "start <= other_end and other_start <= end",
            },
            "comparison": {"pairs": 12000, "disagreements": 1204},
        },
    }

    once = reasoning.normalize_evidence(raw)
    twice = reasoning.normalize_evidence(once)

    assert once["dimension"] == "code"
    assert once["dimension_origin"] == "inferred"
    assert "code.good_semantics=" in once["structured_context"]
    assert len(once["structured_context"]) <= 700
    assert twice["structured_context"] == once["structured_context"]
    assert twice["dimension"] == once["dimension"]
    assert twice["dimension_origin"] == once["dimension_origin"]


def test_unknown_hypothesis_dimension_recovers_from_specific_claim():
    packet = reasoning.normalize_packet(
        {
            "hypotheses": [
                {
                    "hypothesis_id": "h-listener",
                    "claim": "The rendered listener configuration uses the wrong server-name value.",
                    "dimension": "unknown",
                    "falsification": "Capture the redacted effective listener output.",
                }
            ],
            "conclusion": {
                "hypothesis_id": "h-listener",
                "status": "provisional",
            },
        }
    )

    assert packet["hypotheses"][0]["dimension"] == "config"


def test_semantic_baseline_pairs_and_comparability_gaps_are_detected():
    cases = [
        "Inputs are identical between the final good build and the first bad build.",
        "Counts match the prior week until the first post-maintenance upload.",
        "The service unit on new hosts differs from the archived unit on prior hosts.",
        "The old and current checksums are incomparable after serializer changes.",
    ]

    findings = reasoning.detect_baseline_drift(
        [
            reasoning.normalize_evidence(
                {
                    "evidence_id": f"baseline-{index}",
                    "statement": statement,
                    "kind": "artifact",
                    "reliability": 0.99,
                },
                index,
            )
            for index, statement in enumerate(cases)
        ]
    )

    assert {item["finding_type"] for item in findings} == {
        "semantic_baseline_drift",
        "baseline_comparability_gap",
    }
    assert {item["evidence_ids"][0] for item in findings} == {
        "baseline-0",
        "baseline-1",
        "baseline-2",
        "baseline-3",
    }
    assert reasoning.detect_baseline_drift(
        [
            reasoning.normalize_evidence(
                {
                    "evidence_id": "ordinary-control",
                    "statement": (
                        "The settings digest matches the prior seven days and the "
                        "latest run remains intermittent."
                    ),
                }
            )
        ]
    ) == []


def test_retained_before_after_metadata_marks_semantic_baseline_drift():
    retained = reasoning.normalize_evidence(
        {
            "evidence_id": "retained-onset",
            "statement": (
                "The first failures appeared on the new image while prior image "
                "controls completed the same work."
            ),
            "metadata": {"retained": True},
        }
    )
    unretained = reasoning.normalize_evidence(
        {
            "evidence_id": "unretained-onset",
            "statement": (
                "The first failures appeared on the new image while prior image "
                "controls completed the same work."
            ),
        }
    )

    findings = reasoning.detect_baseline_drift([retained])

    assert findings[0]["finding_type"] == "retained_semantic_baseline_drift"
    assert reasoning.detect_baseline_drift([unretained]) == []


def test_retained_control_onset_marks_drift_without_misreading_cursor_order():
    cohort_onset = reasoning.normalize_evidence(
        {
            "evidence_id": "cohort-onset",
            "statement": (
                "Failures began as work moved to replacement hosts; prior hosts "
                "continued processing the same inputs successfully."
            ),
            "metadata": {"retained": True},
        }
    )
    cursor_order = reasoning.normalize_evidence(
        {
            "evidence_id": "cursor-order",
            "statement": (
                "For one retained sequence, the next query begins after the final "
                "retained row and returns filtered rows before reaching its limit."
            ),
        }
    )

    findings = reasoning.detect_baseline_drift([cohort_onset, cursor_order])

    assert [finding["evidence_ids"] for finding in findings] == [["cohort-onset"]]
    assert cohort_onset["retained_comparison"] == "changed"
    assert cursor_order["retained_comparison"] == "none"


def test_causal_owner_follows_the_manipulated_factor_not_the_probe_apparatus():
    examples = {
        "data": (
            "A replay runs in an isolated worker. Replacing only colliding sample "
            "identifiers with distinct surrogate values restores two streams while "
            "the processor image remains unchanged."
        ),
        "config": (
            "Changing only the response deadline in the effective route definition "
            "from 20 to 200 seconds restores the complete recording."
        ),
        "dependency": (
            "The same source produces altered output with the newer decoder package "
            "and correct output with the older decoder while recipe inputs are identical."
        ),
        "code": (
            "A candidate artifact re-reads and compares the current generation before "
            "publishing, while the prior workflow continuation captures a stale value."
        ),
        "state": (
            "Loading the same jobs and durable ledger without unmatched persisted fence "
            "entries restores one pending dispatch per job."
        ),
        "clock": (
            "A replay using the recorded offset delays the action, while the same event "
            "with a synchronized wall reading emits immediately."
        ),
    }

    for expected, statement in examples.items():
        evidence = reasoning.normalize_evidence(
            {
                "evidence_id": f"owner-{expected}",
                "statement": statement,
                "kind": "experiment",
                "reliability": 0.99,
                "discriminating": True,
            }
        )

        assert evidence["causal_dimension"] == expected
        assert evidence["dimension"] == expected
        assert evidence["intervention_scope"] == "component"
        assert evidence["causal_role"] == "support"


def test_planned_measurements_and_broad_relocations_are_not_completed_proof():
    planned = reasoning.normalize_evidence(
        {
            "evidence_id": "planned-probe",
            "statement": (
                "A bounded next measurement can collect per-process scheduler delay "
                "for fifty matched jobs."
            ),
            "dimension": "runtime",
            "kind": "experiment",
            "reliability": 0.99,
            "discriminating": True,
        }
    )
    broad = reasoning.normalize_evidence(
        {
            "evidence_id": "broad-control",
            "statement": (
                "Reprocessing the same payload on a dedicated diagnostic host restores "
                "latency while the internal contention source remains unknown."
            ),
            "dimension": "runtime",
            "kind": "experiment",
            "reliability": 0.99,
            "discriminating": True,
        }
    )

    assert planned["evidence_lifecycle"] == "planned_measurement"
    assert planned["causal_role"] == "context"
    assert planned["intervention_scope"] == "none"
    assert reasoning._is_contrastive_experiment(planned) is False
    assert broad["evidence_lifecycle"] == "observed_result"
    assert broad["intervention_scope"] == "broad"
    assert reasoning._is_contrastive_experiment(broad) is False


def test_retained_comparison_distinguishes_changed_stable_and_incomparable_outcomes():
    changed = reasoning.normalize_evidence(
        {
            "evidence_id": "changed-baseline",
            "statement": (
                "Retained runs completed in forty seconds before maintenance and took "
                "nine hundred seconds after it with the same input cohort."
            ),
        }
    )
    stable = reasoning.normalize_evidence(
        {
            "evidence_id": "stable-baseline",
            "statement": (
                "Retained samples show the same 2.1 percent error rate for six weeks "
                "before and two weeks after the alert; only request volume increased."
            ),
        }
    )
    incomparable = reasoning.normalize_evidence(
        {
            "evidence_id": "missing-baseline",
            "statement": (
                "The pre-change baseline snapshot is unavailable, so the current "
                "outcome cannot be compared to a retained cohort."
            ),
        }
    )

    assert changed["retained_comparison"] == "changed"
    assert stable["retained_comparison"] == "stable"
    assert incomparable["retained_comparison"] == "incomparable"
    findings = reasoning.detect_baseline_drift([changed, stable, incomparable])
    assert {item["finding_type"] for item in findings} == {
        "retained_semantic_baseline_drift",
        "baseline_comparability_gap",
    }
    assert "stable-baseline" not in {
        evidence_id
        for item in findings
        for evidence_id in item["evidence_ids"]
    }


def test_effective_status_depends_on_qualified_proof_not_model_posture():
    case = {
        "case_id": "status-invariance",
        "problem_statement": "A gateway closes long streams too early.",
        "observations": [
            {
                "evidence_id": "setting-proof",
                "statement": (
                    "Changing only the effective deadline setting restores the full "
                    "stream while code, inputs, packages, and runtime remain fixed."
                ),
                "dimension": "config",
                "kind": "experiment",
                "reliability": 0.99,
                "discriminating": True,
            }
        ],
    }

    outcomes = set()
    for requested_status in ("confirmed", "provisional", "inconclusive", "rejected"):
        report = reasoning.evaluate_packet(
            case,
            {
                "hypotheses": [
                    {
                        "hypothesis_id": "h-config",
                        "claim": "The effective deadline setting truncates the stream.",
                        "dimension": "config",
                        "support_evidence_ids": ["setting-proof"],
                        "falsification": "Restore only the prior deadline value.",
                    }
                ],
                "conclusion": {
                    "hypothesis_id": "h-config",
                    "status": requested_status,
                },
            },
        )
        outcomes.add((report["conclusion"]["status"], report["decision"]))

    assert outcomes == {("confirmed", "patch_root_cause")}


def test_broad_family_localization_with_event_gap_remains_instrument_first():
    case = {
        "case_id": "broad-localization",
        "problem_statement": "Large jobs stall on a shared execution pool.",
        "observations": [
            {
                "evidence_id": "broad-control",
                "statement": (
                    "Reprocessing matched jobs on a dedicated diagnostic host restores "
                    "latency, but several execution resources change together."
                ),
                "dimension": "runtime",
                "kind": "experiment",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "missing-owner",
                "statement": (
                    "Current traces omit per-process wait counters and cannot show whether "
                    "scheduler delay or memory reclaim owns any failed event."
                ),
                "dimension": "runtime",
                "kind": "artifact",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "planned-probe",
                "statement": (
                    "A bounded next measurement can collect both counters for matched jobs."
                ),
                "dimension": "runtime",
                "kind": "experiment",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-runtime",
                "claim": "Shared execution-pool pressure causes the long tail.",
                "dimension": "runtime",
                "support_evidence_ids": ["broad-control", "planned-probe"],
                "falsification": "Capture event-level wait classes on both pools.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-runtime", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["attribution_assessment"]["mechanism_gap"] is True
    assert report["conclusion"]["dimension"] == "runtime"
    assert report["conclusion"]["status"] == "provisional"
    assert report["decision"] == "instrument_first"


def test_coarse_runtime_reset_with_missing_mechanism_stays_provisional():
    case = {
        "case_id": "coarse-runtime-reset",
        "problem_statement": "A long-running worker stops accepting new events.",
        "observations": [
            {
                "evidence_id": "worker-reset",
                "statement": (
                    "A supervised recycle restores forwarding without changing the "
                    "host, artifact, settings, or assigned inputs."
                ),
                "dimension": "runtime",
                "kind": "experiment",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "missing-owner",
                "statement": (
                    "Current snapshots are too coarse to identify which handle class "
                    "grows, and no incident preserved creation stacks."
                ),
                "dimension": "runtime",
                "kind": "artifact",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-runtime",
                "claim": "Worker-local runtime state causes the stall.",
                "dimension": "runtime",
                "support_evidence_ids": ["worker-reset", "missing-owner"],
                "falsification": "Capture handle classes and creation paths before recycling.",
            }
        ],
        "conclusion": {
            "hypothesis_id": "h-runtime",
            "status": "confirmed",
            "evidence_ids": ["worker-reset"],
        },
    }

    report = reasoning.evaluate_packet(case, packet)
    result = report["hypothesis_results"][0]

    assert report["attribution_assessment"]["mechanism_gap"] is True
    assert result["coarse_reset_support"] is True
    assert result["attribution_resolving_support"] is False
    assert report["conclusion"]["status"] == "provisional"
    assert report["decision"] == "instrument_first"


def test_event_level_harness_gap_blocks_sparse_experiment_confirmation():
    case = {
        "case_id": "harness-event-attribution",
        "problem_statement": "A qualification rig intermittently reports a late edge.",
        "observations": [
            {
                "evidence_id": "sparse-reproduction",
                "statement": (
                    "A fixed signal generator reproduces one false late report in "
                    "one hundred isolated runs."
                ),
                "dimension": "test_harness",
                "kind": "experiment",
                "reliability": 0.98,
                "discriminating": True,
            },
            {
                "evidence_id": "missing-correlation",
                "statement": (
                    "Raw edge timestamps were rotated before preservation and the "
                    "records do not share one identifier, preventing event-by-event attribution."
                ),
                "dimension": "test_harness",
                "kind": "artifact",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-harness",
                "claim": "The qualification harness creates the false late result.",
                "dimension": "test_harness",
                "support_evidence_ids": ["sparse-reproduction", "missing-correlation"],
                "falsification": "Capture one immutable event id across every recorder.",
            }
        ],
        "conclusion": {
            "hypothesis_id": "h-harness",
            "status": "confirmed",
            "evidence_ids": ["sparse-reproduction"],
        },
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["baseline_drift"][0]["finding_type"] == "baseline_comparability_gap"
    assert report["attribution_assessment"]["mechanism_gap"] is True
    assert report["conclusion"]["dimension"] == "test_harness"
    assert report["conclusion"]["status"] == "inconclusive"
    assert report["decision"] == "instrument_first"


def test_omitted_inferred_contrastive_proof_cannot_promote_clock_family():
    case = {
        "case_id": "clock-proof-completion",
        "problem_statement": "Elapsed duration is wrong during a repeated local hour.",
        "observations": [
            {
                "evidence_id": "clock-schema",
                "statement": "The retained row stores offset-free local values.",
                "kind": "artifact",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "clock-proof",
                "statement": (
                    "An offline replay reproduces the fault with offset-free local "
                    "values and restores elapsed duration with retained UTC instants."
                ),
                "kind": "experiment",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "Offset-free local time breaks elapsed duration.",
                "dimension": "clock",
                "support_evidence_ids": ["clock-schema"],
                "falsification": "Replay the same rows with offset-aware instants.",
            }
        ],
        "conclusion": {
            "hypothesis_id": "h-clock",
            "status": "provisional",
            "evidence_ids": ["clock-schema"],
        },
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["conclusion"]["dimension"] == "clock"
    assert report["conclusion"]["status"] == "inconclusive"
    assert report["decision"] == "investigate"
    assert any(
        item.get("origin") == "deterministic_evidence_gate"
        and item["dimension"] == "clock"
        for item in report["hypothesis_results"]
    )


def test_ambiguous_counterfactual_experiment_remains_context():
    evidence = reasoning.normalize_evidence(
        {
            "evidence_id": "ambiguous-offset",
            "statement": (
                "The result changes depending on the assumed parsing zone, and neither assumption explains all observations."
            ),
            "kind": "experiment",
            "reliability": 0.99,
            "discriminating": True,
        }
    )

    assert evidence["causal_role"] == "context"


def test_context_and_inferred_observations_rank_without_causal_support():
    observations = [
        {
            "evidence_id": "context-only",
            "statement": "The policy dashboard reports a retained lifecycle mismatch.",
            "dimension": "state",
            "kind": "metric",
            "causal_role": "context",
            "independence_key": "dashboard",
            "reliability": 0.99,
            "discriminating": True,
        },
        {
            "evidence_id": "inferred-only",
            "statement": (
                "Changing only durable workflow lifecycle state restores processing "
                "without changing source, settings, or dependencies."
            ),
            "kind": "experiment",
            "independence_key": "inferred-replay",
            "reliability": 0.99,
            "discriminating": True,
        },
    ]

    for observation in observations:
        normalized = reasoning.normalize_evidence(observation)
        assert normalized["dimension"] != "unknown"
        if observation["evidence_id"] == "inferred-only":
            assert normalized["dimension_origin"] == "inferred"
            assert normalized["causal_role"] == "support"

        report = reasoning.evaluate_packet(
            {
                "case_id": observation["evidence_id"],
                "problem_statement": "A workflow outcome changed.",
                "observations": [observation],
            },
            {
                "hypotheses": [
                    {
                        "hypothesis_id": "h-ranked-only",
                        "claim": "The observed family owns the workflow failure.",
                        "dimension": normalized["dimension"],
                        "support_evidence_ids": [observation["evidence_id"]],
                        "falsification": "Run an explicit owner-aligned intervention.",
                    }
                ],
                "experiments": [
                    {
                        "experiment_id": "x-ranked-only",
                        "hypothesis_ids": ["h-ranked-only"],
                        "changed_dimensions": [normalized["dimension"]],
                        "held_constant_dimensions": ["code", "config"],
                        "expected_if_true": "The workflow outcome changes.",
                        "expected_if_false": "The workflow outcome remains unchanged.",
                        "result_evidence_ids": [observation["evidence_id"]],
                        "safety": "isolated",
                        "status": "completed",
                    }
                ],
                "conclusion": {
                    "hypothesis_id": "h-ranked-only",
                    "status": "confirmed",
                },
            },
        )
        result = report["hypothesis_results"][0]

        assert result["context_weight"] > 0
        assert result["causal_support_weight"] == 0
        assert result["support_weight"] == 0
        assert result["status"] == "untested"
        assert report["conclusion"]["status"] == "inconclusive"


def test_proxy_policy_config_paraphrases_cannot_lexically_override_state_owner():
    problem_statements = [
        "The rendered policy denies the exact trusted proxy principal.",
        "Policy configuration rejects a trusted proxy principal and wildcard response.",
        "Vary header values use mixed casing and a wildcard response follows the wrong policy.",
    ]
    observations = [
        {
            "evidence_id": "state-owner",
            "statement": "The reader generation retires while an active handle still owns it.",
            "dimension": "state",
            "kind": "artifact",
            "causal_role": "support",
            "expected_state": "retained_until_last_reader_closes",
            "actual_state": "retired_with_active_reader",
            "independence_key": "reader-lifecycle",
            "reliability": 0.99,
            "discriminating": True,
        },
        {
            "evidence_id": "policy-surface",
            "statement": "The same lifecycle mismatch appears at the rendered policy boundary.",
            "dimension": "state",
            "kind": "artifact",
            "causal_role": "support",
            "expected_state": "stable_generation",
            "actual_state": "stale_generation",
            "independence_key": "policy-surface",
            "reliability": 0.99,
            "discriminating": True,
        },
    ]
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-state",
                "claim": "Reader ownership and retirement state are inconsistent.",
                "dimension": "state",
                "support_evidence_ids": ["state-owner"],
                "falsification": "Trace reader counts through retirement.",
            },
            {
                "hypothesis_id": "h-config",
                "claim": "Rendered proxy policy configuration is inconsistent.",
                "dimension": "config",
                "support_evidence_ids": ["policy-surface"],
                "falsification": "Compare the effective proxy policy.",
            },
        ],
        "conclusion": {"hypothesis_id": "h-state", "status": "provisional"},
    }

    for problem_statement in problem_statements:
        assert reasoning.infer_dimension(problem_statement) == "config"
        assert reasoning.decisive_inferred_dimension(problem_statement) == "config"
        report = reasoning.evaluate_packet(
            {
                "case_id": "lexical-policy-dispute",
                "problem_statement": problem_statement,
                "observations": observations,
            },
            packet,
        )
        results = {
            item["hypothesis_id"]: item for item in report["hypothesis_results"]
        }

        assert results["h-state"]["explicit_owner_aligned_causal_support"] is True
        assert results["h-config"]["explicit_owner_aligned_causal_support"] is False
        assert report["conclusion"]["dimension"] == "state"
        assert report["conclusion"]["status"] == "provisional"


def test_prompt_taxonomy_override_requires_and_accepts_explicit_owner_support():
    problem_statement = "The rendered policy denies the exact trusted proxy principal."
    case = {
        "case_id": "grounded-lexical-policy",
        "problem_statement": problem_statement,
        "observations": [
            {
                "evidence_id": "state-owner",
                "statement": "The reader generation changed unexpectedly.",
                "dimension": "state",
                "kind": "artifact",
                "causal_role": "support",
                "expected_state": "active",
                "actual_state": "retired",
                "independence_key": "reader-state",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "config-owner",
                "statement": "The effective trusted-principal setting differs from the approved value.",
                "dimension": "config",
                "kind": "artifact",
                "causal_role": "support",
                "expected_state": "approved_principal",
                "actual_state": "stale_principal",
                "independence_key": "effective-config",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-state",
                "claim": "Reader lifecycle state caused the rejection.",
                "dimension": "state",
                "support_evidence_ids": ["state-owner"],
                "falsification": "Hold reader lifecycle constant.",
            },
            {
                "hypothesis_id": "h-config",
                "claim": "The effective trusted-principal setting caused the rejection.",
                "dimension": "config",
                "support_evidence_ids": ["config-owner"],
                "falsification": "Restore only the approved principal.",
            },
        ],
        "conclusion": {"hypothesis_id": "h-state", "status": "provisional"},
    }

    report = reasoning.evaluate_packet(case, packet)
    config_result = next(
        item
        for item in report["hypothesis_results"]
        if item["hypothesis_id"] == "h-config"
    )

    assert reasoning.decisive_inferred_dimension(problem_statement) == "config"
    assert config_result["explicit_owner_aligned_causal_support"] is True
    assert report["conclusion"]["dimension"] == "config"
    assert report["conclusion"]["status"] == "provisional"


def test_isolated_intervention_outranks_multiple_downstream_symptoms():
    case = {
        "case_id": "intervention-over-symptoms",
        "problem_statement": "Two monitors report failures after a policy migration.",
        "observations": [
            {
                "evidence_id": "symptom-a",
                "statement": "The sink failure counter increased.",
                "dimension": "runtime",
                "kind": "metric",
                "causal_role": "context",
                "independence_key": "monitor-a",
                "reliability": 0.99,
            },
            {
                "evidence_id": "symptom-b",
                "statement": "The delivery latency monitor alerted.",
                "dimension": "runtime",
                "kind": "metric",
                "causal_role": "context",
                "independence_key": "monitor-b",
                "reliability": 0.99,
            },
            {
                "evidence_id": "policy-intervention",
                "statement": "Changing only the rendered principal restores delivery.",
                "dimension": "config",
                "kind": "experiment",
                "independence_key": "policy-replay",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-runtime",
                "claim": "Runtime load caused delivery failure.",
                "dimension": "runtime",
                "support_evidence_ids": ["symptom-a", "symptom-b"],
                "falsification": "Hold runtime load constant.",
            },
            {
                "hypothesis_id": "h-config",
                "claim": "The rendered principal blocks delivery.",
                "dimension": "config",
                "support_evidence_ids": ["policy-intervention"],
                "falsification": "Restore only the prior principal.",
            },
        ],
        "conclusion": {"hypothesis_id": "h-runtime", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)
    results = {item["hypothesis_id"]: item for item in report["hypothesis_results"]}

    assert results["h-runtime"]["context_weight"] > 0
    assert results["h-runtime"]["support_weight"] == 0
    assert results["h-runtime"]["status"] == "untested"
    assert results["h-runtime"]["ownership_weight"] == 0
    assert results["h-config"]["causal_sufficiency"] == "isolated"
    assert report["conclusion"]["dimension"] == "config"
    assert report["conclusion"]["status"] == "confirmed"


def test_inferred_dimension_alignment_ranks_without_resolving_causal_ties():
    case = {
        "case_id": "soft-dimension-alignment",
        "problem_statement": "An event-order replay changes the final state.",
        "observations": [
            {
                "evidence_id": "ordering-replay",
                "statement": (
                    "Changing only event time ordering restores the final state."
                ),
                "kind": "experiment",
                "independence_key": "ordering-replay",
                "reliability": 0.99,
                "discriminating": True,
            }
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-dependency",
                "claim": "A dependency changed event ordering.",
                "dimension": "dependency",
                "support_evidence_ids": ["ordering-replay"],
                "falsification": "Hold the dependency constant.",
            },
            {
                "hypothesis_id": "h-clock",
                "claim": "Event-time ordering changed the final state.",
                "dimension": "clock",
                "support_evidence_ids": ["ordering-replay"],
                "falsification": "Hold event-time ordering constant.",
            },
        ],
        "conclusion": {"hypothesis_id": "h-dependency", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)
    results = {item["hypothesis_id"]: item for item in report["hypothesis_results"]}

    assert report["valid"] is True
    assert results["h-dependency"]["dimension_mismatch_weight"] == 0.99
    assert results["h-clock"]["dimension_alignment_weight"] == 0.99
    assert results["h-clock"]["causal_dimension_alignment_weight"] == 0
    assert results["h-clock"]["causal_support_weight"] == 0
    assert results["h-clock"]["status"] == "untested"
    assert report["conclusion"]["dimension"] == "dependency"
    assert report["conclusion"]["status"] == "inconclusive"


def test_edge_break_is_earliest_and_same_correlation_sink_is_downstream():
    case = reasoning.normalize_case(
        {
            "case_id": "edge-break-correlation",
            "problem_statement": "A policy edge denied a produced package.",
            "observations": [
                {
                    "evidence_id": "produced",
                    "statement": "The producer emitted the package.",
                    "dimension": "data",
                    "observed_at": "2026-07-11T10:00:00Z",
                    "correlation_id": "request-42",
                },
                {
                    "evidence_id": "denied",
                    "statement": "The exact principal was denied at the policy edge.",
                    "dimension": "config",
                    "observed_at": "2026-07-11T10:00:01Z",
                    "edge_from": "producer",
                    "edge_to": "consumer",
                    "expected_edge_state": "allowed",
                    "actual_edge_state": "denied",
                    "correlation_id": "request-42",
                },
                {
                    "evidence_id": "sink-absent",
                    "statement": "The sink has no package for the request.",
                    "dimension": "data",
                    "observed_at": "2026-07-11T10:00:02Z",
                    "expected_state": "present",
                    "actual_state": "absent",
                    "correlation_id": "request-42",
                },
            ],
        }
    )

    timeline = reasoning.reconstruct_causal_timeline(case)

    assert timeline["earliest_break"]["evidence_id"] == "denied"
    assert "edge_state_mismatch" in timeline["earliest_break"]["violations"]
    assert "sink-absent" in timeline["downstream_evidence_ids"]


def test_isolated_proof_is_not_overwritten_by_weaker_earliest_or_provenance_break():
    case = {
        "case_id": "proof-precedence",
        "problem_statement": "A package parser drops records before persistence.",
        "observations": [
            {
                "evidence_id": "sink-break",
                "statement": "The persistence edge receives fewer records.",
                "dimension": "data",
                "kind": "artifact",
                "observed_at": "2026-07-11T10:00:00Z",
                "edge_from": "parser",
                "edge_to": "database",
                "expected_edge_state": "complete",
                "actual_edge_state": "reduced",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "package-ab",
                "statement": "Changing only the locked package version restores every parsed record.",
                "dimension": "dependency",
                "kind": "experiment",
                "observed_at": "2026-07-11T10:01:00Z",
                "independence_key": "package-ab",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-data",
                "claim": "Persistence loses parser records.",
                "dimension": "data",
                "support_evidence_ids": ["sink-break"],
                "falsification": "Hold persistence constant.",
            },
            {
                "hypothesis_id": "h-dependency",
                "claim": "The locked parser package drops records.",
                "dimension": "dependency",
                "support_evidence_ids": ["package-ab"],
                "falsification": "Pin only the prior package.",
            },
        ],
        "conclusion": {"hypothesis_id": "h-data", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["causal_timeline"]["earliest_break"]["evidence_id"] == "sink-break"
    assert report["provenance_graph"]["first_broken_edge"]["evidence_id"] == "sink-break"
    assert report["conclusion"]["dimension"] == "dependency"
    assert report["conclusion"]["status"] == "confirmed"


def test_unresolved_execution_attribution_blocks_runtime_confirmation():
    case = {
        "case_id": "unresolved-runtime-attribution",
        "problem_statement": (
            "The process evidence cannot identify which executing process emitted duplicates."
        ),
        "observations": [
            {
                "evidence_id": "runtime-anomaly",
                "statement": "The loaded module hash mismatches the signed image inventory.",
                "dimension": "runtime",
                "kind": "artifact",
                "independence_key": "process-snapshot",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "duplicate-symptom",
                "statement": "One reservation produced two confirmations.",
                "dimension": "runtime",
                "kind": "observation",
                "causal_role": "context",
                "expected_state": "one_confirmation",
                "actual_state": "two_confirmations",
                "independence_key": "sink-ledger",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "missing-link",
                "statement": (
                    "No retained span can identify whether that module handled the failing correlation."
                ),
                "dimension": "runtime",
                "kind": "artifact",
                "independence_key": "retention-audit",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "healthy-control",
                "statement": "The clean worker remains healthy and checksums are identical.",
                "dimension": "runtime",
                "kind": "artifact",
                "independence_key": "clean-worker",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-runtime",
                "claim": "The anomalous runtime emitted duplicate confirmations.",
                "dimension": "runtime",
                "support_evidence_ids": [
                    "runtime-anomaly",
                    "duplicate-symptom",
                    "missing-link",
                ],
                "contradict_evidence_ids": ["healthy-control"],
                "falsification": "Capture worker identity on dequeue and send spans.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-runtime", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)
    result = report["hypothesis_results"][0]

    assert report["attribution_assessment"]["unresolved"] is True
    assert result["attribution_gap_blocked"] is True
    assert report["conclusion"]["dimension"] == "runtime"
    assert report["conclusion"]["status"] == "inconclusive"
    assert report["decision"] == "instrument_first"


def test_single_decisive_attribution_gap_blocks_harness_confirmation():
    case = {
        "case_id": "decisive-harness-gap",
        "problem_statement": "A browser upload test redirects unexpectedly.",
        "observations": [
            {
                "evidence_id": "profile-state",
                "statement": "The browser profile persists after scenario cleanup.",
                "dimension": "test_harness",
                "kind": "artifact",
                "expected_state": "clean_profile",
                "actual_state": "persisted_profile",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "missing-lifecycle",
                "statement": (
                    "The retained archive cannot distinguish persisted browser state from a leaked proxy rule."
                ),
                "dimension": "test_harness",
                "kind": "artifact",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-harness",
                "claim": "Leaked harness state redirects the browser.",
                "dimension": "test_harness",
                "support_evidence_ids": ["profile-state", "missing-lifecycle"],
                "falsification": "Capture profile and proxy lifecycle per scenario.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-harness", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["attribution_assessment"]["unresolved"] is True
    assert report["conclusion"]["dimension"] == "test_harness"
    assert report["conclusion"]["status"] == "inconclusive"
    assert report["decision"] == "instrument_first"


def test_visual_baseline_drift_selects_test_harness_without_claiming_exact_component():
    case = {
        "case_id": "visual-baseline-drift",
        "problem_statement": (
            "A visual qualification comparison changed with constant product source and inputs."
        ),
        "observations": [
            {
                "evidence_id": "reference",
                "statement": "The reference comparison record used runner A.",
                "dimension": "test_harness",
                "kind": "artifact",
                "comparison_key": "page-1",
                "code_revision": "same-revision",
                "input_fingerprint": "same-input",
                "environment_fingerprint": "runner-a",
                "outcome_fingerprint": "reference-hash",
                "reliability": 1.0,
            },
            {
                "evidence_id": "candidate",
                "statement": "The candidate comparison record used runner B.",
                "dimension": "test_harness",
                "kind": "artifact",
                "comparison_key": "page-1",
                "code_revision": "same-revision",
                "input_fingerprint": "same-input",
                "environment_fingerprint": "runner-b",
                "outcome_fingerprint": "candidate-hash",
                "reliability": 1.0,
            },
            {
                "evidence_id": "missing-environment",
                "statement": "The original runner image is no longer available.",
                "dimension": "runtime",
                "kind": "artifact",
                "reliability": 0.99,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-runtime",
                "claim": "A runtime component changed rendering.",
                "dimension": "runtime",
                "support_evidence_ids": ["missing-environment"],
                "falsification": "Reconstruct the original runner exactly.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-runtime", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["baseline_drift"]
    assert report["conclusion"]["dimension"] == "test_harness"
    assert report["conclusion"]["status"] == "inconclusive"
    assert report["decision"] == "instrument_first"


def test_unreproducible_baseline_keeps_noncode_direct_signal_provisional():
    case = {
        "case_id": "unreproducible-clock-baseline",
        "problem_statement": "A latency dashboard changed after a device rollout.",
        "observations": [
            {
                "evidence_id": "clock-artifact",
                "statement": "The client stores local wall-time without a UTC offset.",
                "dimension": "clock",
                "kind": "artifact",
                "expected_state": "offset_present",
                "actual_state": "offset_absent",
                "independence_key": "event-schema",
                "reliability": 0.99,
                "discriminating": True,
            },
            {
                "evidence_id": "baseline-gap",
                "statement": (
                    "The pre-rollout baseline retained only daily percentiles and cannot establish the new cohort's prior delay distribution."
                ),
                "dimension": "clock",
                "kind": "artifact",
                "independence_key": "baseline-retention",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "Local wall-time interpretation distorts latency.",
                "dimension": "clock",
                "support_evidence_ids": ["clock-artifact", "baseline-gap"],
                "falsification": "Capture monotonic and offset-aware durations.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-clock", "status": "confirmed"},
    }

    report = reasoning.evaluate_packet(case, packet)

    assert report["baseline_drift"][0]["finding_type"] == "baseline_comparability_gap"
    assert report["conclusion"]["dimension"] == "clock"
    assert report["conclusion"]["status"] == "provisional"
    assert report["decision"] == "instrument_first"


def test_provisional_promotion_requires_isolated_or_graph_linked_proof():
    artifact_case = {
        "case_id": "artifact-only-promotion",
        "problem_statement": "A source ordering change altered output.",
        "observations": [
            {
                "evidence_id": "source-a",
                "statement": "The stage plan differs at one ordering point.",
                "dimension": "code",
                "kind": "artifact",
                "independence_key": "stage-plan",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "source-b",
                "statement": "The source trace differs at the same branch.",
                "dimension": "code",
                "kind": "artifact",
                "independence_key": "source-trace",
                "reliability": 0.95,
                "discriminating": True,
            },
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The ordering change altered output.",
                "dimension": "code",
                "support_evidence_ids": ["source-a", "source-b"],
                "falsification": "Revert only the ordering change.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-code", "status": "provisional"},
    }

    artifact_report = reasoning.evaluate_packet(artifact_case, packet)
    experiment_case = {
        **artifact_case,
        "case_id": "isolated-promotion",
        "observations": [
            *artifact_case["observations"],
            {
                "evidence_id": "isolated-revert",
                "statement": "Reverting only the ordering change restores output.",
                "dimension": "code",
                "kind": "experiment",
                "independence_key": "isolated-revert",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
    }
    experiment_packet = {
        **packet,
        "hypotheses": [
            {
                **packet["hypotheses"][0],
                "support_evidence_ids": ["source-a", "source-b", "isolated-revert"],
            }
        ],
    }
    experiment_report = reasoning.evaluate_packet(experiment_case, experiment_packet)
    rejected_packet = {
        **experiment_packet,
        "conclusion": {"hypothesis_id": "h-code", "status": "rejected"},
    }
    rejected_report = reasoning.evaluate_packet(experiment_case, rejected_packet)

    assert artifact_report["hypothesis_results"][0]["status"] == "supported"
    assert artifact_report["conclusion"]["status"] == "provisional"
    assert experiment_report["conclusion"]["status"] == "confirmed"
    assert experiment_report["conclusion"]["promotion_reason"]
    assert rejected_report["conclusion"]["status"] == "confirmed"


def test_stage_history_separates_requested_and_effective_conclusions():
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "Replay sizing reads wall time.",
                "dimension": "clock",
                "support_evidence_ids": ["clock-result", "caller-source"],
                "falsification": "Pass only the replay clock.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-clock", "status": "provisional"},
    }

    debate = reasoning.run_local_diagnostic_debate(
        _sim_clock_case(),
        lambda _stage, _prompt: json.dumps(packet),
        stages_to_run=("judge",),
    )
    stage = debate["stages"][0]

    assert stage["requested_conclusion"]["status"] == "provisional"
    assert stage["effective_conclusion"]["status"] == "confirmed"
    assert stage["conclusion"] == stage["effective_conclusion"]


def test_causal_fallback_restores_omitted_isolation_evidence_and_syncs_final_packet():
    case = {
        "case_id": "omitted-isolation-evidence",
        "problem_statement": "A source ordering change altered output.",
        "observations": [
            {
                "evidence_id": "stage-plan",
                "statement": "The stage plan differs at one ordering point.",
                "dimension": "code",
                "kind": "artifact",
                "independence_key": "stage-plan",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "isolated-revert",
                "statement": "Reverting only the ordering change restores output.",
                "dimension": "code",
                "kind": "experiment",
                "independence_key": "isolated-revert",
                "reliability": 0.99,
                "discriminating": True,
            },
        ],
        "constraints": {"minimum_hypothesis_dimensions": 1},
    }
    model_packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The ordering change altered output.",
                "dimension": "code",
                "support_evidence_ids": ["stage-plan"],
                "falsification": "Revert only the ordering change.",
            }
        ],
        "conclusion": {"hypothesis_id": "h-code", "status": "inconclusive"},
    }

    debate = reasoning.run_local_diagnostic_debate(
        case,
        lambda _stage, _prompt: json.dumps(model_packet),
        stages_to_run=("judge",),
    )

    assert debate["stages"][0]["requested_conclusion"]["status"] == "inconclusive"
    assert debate["report"]["conclusion"]["status"] == "confirmed"
    assert debate["report"]["conclusion"]["hypothesis_id"] == "evidence-code"
    assert debate["packet"]["conclusion"]["status"] == "confirmed"
    assert debate["packet"]["conclusion"]["evidence_ids"] == [
        "stage-plan",
        "isolated-revert",
    ]
    assert any(
        item["hypothesis_id"] == "evidence-code"
        for item in debate["packet"]["hypotheses"]
    )


def test_revision_parity_compares_only_compatible_identifier_namespaces():
    unrelated = reasoning.normalize_case(
        {
            "case_id": "unrelated-revisions",
            "problem_statement": "A process snapshot records source and image identifiers.",
            "observations": [
                {
                    "evidence_id": "snapshot",
                    "statement": "The process reports one source id and one image id.",
                    "source_revision": "src-8f41",
                    "runtime_revision": "img-a91",
                }
            ],
        }
    )
    comparable = reasoning.normalize_case(
        {
            "case_id": "comparable-revisions",
            "problem_statement": "The worker runs an older revision.",
            "observations": [
                {
                    "evidence_id": "snapshot",
                    "statement": "The worker reports revision r1 instead of r2.",
                    "source_revision": "r2",
                    "runtime_revision": "r1",
                }
            ],
        }
    )

    unrelated_parity = reasoning.reconstruct_causal_timeline(unrelated)[
        "runtime_source_parity"
    ]
    comparable_parity = reasoning.reconstruct_causal_timeline(comparable)[
        "runtime_source_parity"
    ]

    assert unrelated_parity["status"] == "unknown"
    assert unrelated_parity["comparable_pair_count"] == 0
    assert comparable_parity["status"] == "mismatch"
    assert comparable_parity["comparable_pair_count"] == 1
