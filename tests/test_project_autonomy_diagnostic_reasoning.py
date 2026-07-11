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
