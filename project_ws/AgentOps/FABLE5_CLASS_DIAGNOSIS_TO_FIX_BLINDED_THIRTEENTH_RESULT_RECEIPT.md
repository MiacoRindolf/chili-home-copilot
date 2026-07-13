# Fable 5-Class Diagnosis-to-Fix Blinded Thirteenth Result Receipt

Date: 2026-07-13

## Frozen Run Identity

- Frozen implementation commit: `cd85207c5ccfe6bbf6a92564e4789bc17790ce07`.
- Frozen implementation tree: `00cc662c57375e439663e8d05ca510e302b803e9`.
- Fixture commit: `d627a8e4a1c3611644280ae6928e5aa733c1a45a`.
- Fixture Git subtree: `8f1b502ffdde4a0312d21e1a72132f04faa39dd3`.
- Run-lock commit: `72d7ed5446a74328e2755f881c98e90258cd4db5`.
- Canonical authored-byte aggregate: `e05b205217ab8e618d6946b7811df744c836db4b71ddbd81adf09cd4f4761803`.
- Full fixture aggregate: `8cc56e5e106b27b1b80bb2fd1ebd8731e16bbc07e197c45b5cebd2bc24e7f6f7`.
- Evaluation context: `protocol`.
- Reference family: `claude-fable-5`.

## Run Command

`python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_thirteenth --model qwen2.5-coder:7b --escalation-model qwen2.5-coder:14b --max-repairs 2 --max-escalation-repairs 1 --timeout 180 --case-model-time-budget 690 --evaluation-context protocol --report project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_THIRTEENTH_UNTOUCHED.md --results-json project_ws/AgentOps/fable5_class_diagnosis_to_fix_blinded_thirteenth_untouched.json`

Runner exit code: 0. Runner wall time: 2497.1 seconds.

## Immutable Outputs

- JSON SHA-256: `f03011fdcf4b67ba33a874208fb27e123ad5ac5a8e1a9e55ac31973456d39cba`.
- Markdown report SHA-256: `8d03e6f90351074001f24cf3dfd4c5cddac3ec2646baf4833313129a15c7e7df`.

## Result

- Overall score: 40.83/100.
- Verdict: `needs_improvement`.
- Evaluation verdict: `blinded_evaluation_failed`.
- Sealed-final solves: 2/12 (16.67%).
- Correct diagnosis families: 3/12 (25%).
- Causally accepted diagnoses: 0/12 (0%).
- Exact expected file sets: 3/12 (25%).
- Public-test preservation: 12/12.
- Repair-feedback passes: 3/12.
- Prompt-contract closure: 12/12.
- Patches retained: 5/12.
- Premium calls: 0.
- Local model calls: 157 total; 140 on `qwen2.5-coder:7b`, 17 on `qwen2.5-coder:14b`.
- Model-call transport/errors: 0.
- Deterministic disclosed-family operator attempts: 0, as expected for non-overlapping holdout mechanisms.
- Average case duration: 207630.08 ms.
- Fable 5 same-task head-to-head run: false.
- Fable 5 parity claim: false.

Language results:

| Language | Average | Sealed | Diagnosis | Exact Files | Feedback |
| --- | ---: | ---: | ---: | ---: | ---: |
| Dart | 26.67 | 0/3 | 0/3 | 0/3 | 0/3 |
| Python | 26.67 | 0/3 | 0/3 | 0/3 | 0/3 |
| SQL | 30.00 | 0/3 | 1/3 | 0/3 | 0/3 |
| TypeScript | 80.00 | 2/3 | 2/3 | 3/3 | 3/3 |

Solved sealed cases:

- `th13_node_facility_rollup` (100/100).
- `th13_node_response_compression` (100/100).

The third TypeScript case, `th13_node_job_recovery`, found the exact owner set and passed repair feedback but failed
the sealed final. No Dart, Python, or SQL case passed repair feedback or the sealed final.

## Interpretation Lock

This result is preserved before any failure-specific repair or replay. It shows real improvement on two unseen
multi-file TypeScript cases and complete premium independence, but it does not support a Fable 5 replacement or
parity claim. The dominant gaps are causal diagnosis acceptance, cross-language owner discovery, and coordinated
repair transfer in Dart, Python, and SQL. Authenticated Fable 5 outputs on these exact frozen tasks remain absent.
