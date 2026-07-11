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

    assert result["stages"][0]["accepted"] is False
    assert any("different evidence family" in error for error in result["stages"][0]["errors"])
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
