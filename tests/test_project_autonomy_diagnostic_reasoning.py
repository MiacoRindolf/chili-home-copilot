from __future__ import annotations

import json

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
    assert results["h-code"]["status"] == "blocked"
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
    assert report["conclusion"]["status"] == "confirmed"


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
    assert code_result["status"] == "blocked"
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

    assert report["hypothesis_results"][0]["status"] == "blocked"
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

    assert report["conclusion"]["status"] == "confirmed"
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
    assert result["report"]["conclusion"]["status"] == "confirmed"
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
    assert report["conclusion"]["status"] == "provisional"
    assert report["decision"] == "instrument_first"
    unknown = next(
        item for item in report["hypothesis_results"] if item["hypothesis_id"] == "h-unknown"
    )
    assert unknown["status"] == "provisional"


def test_full_operator_contract_breaks_non_discriminating_causal_family_tie():
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

    assert report["conclusion"]["dimension"] == "state"
    assert report["conclusion"]["status"] == "provisional"


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


def test_omitted_contrastive_proof_can_promote_the_correct_clock_family():
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
    assert report["conclusion"]["status"] == "confirmed"
    assert report["decision"] == "patch_root_cause"
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
    assert results["h-runtime"]["ownership_weight"] == 0
    assert results["h-config"]["causal_sufficiency"] == "isolated"
    assert report["conclusion"]["dimension"] == "config"
    assert report["conclusion"]["status"] == "confirmed"


def test_inferred_dimension_alignment_breaks_cross_family_causal_ties_softly():
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
    assert report["conclusion"]["dimension"] == "clock"
    assert report["conclusion"]["status"] == "confirmed"


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
    assert report["conclusion"]["status"] == "provisional"
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
