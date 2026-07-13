# Fable 5-Class Diagnosis-to-Fix Blinded Fourteenth Result Receipt

Date: 2026-07-13

## Frozen Run Identity

- Frozen implementation commit: `2cc8e9d446e0ceb66abf5bf688596efb869f0133`.
- Frozen implementation tree: `3ebcc8cb37574185c404848464a62ff06612fefe`.
- Pre-authored protocol commit: `5764a0f9d2919c19cdd2f85b62ee20b8ee553169`.
- Active V3 authored target commit: `a249993262ce5c2f621ed17ce67b7cccf8e74fef`.
- Validation-complete tip: `5edae0f7a461bcba162a461ba390bcd6ae8ad15f`.
- Fixture Git subtree: `31bcca632bd5d467841757b07a152d0cc5556fb6`.
- Run-lock commit: `173f3ee8bc58a6c9f8f4ea31a3072698c7290227`.
- Active V3 core aggregate: `eba357467b11829cfb552a40230804a78e586c5cddd91c25a514e0a61923960d`.
- Full post-validation fixture aggregate: `4f49380bcd78c3670384bffb3e1bb87c1cf73eb94f076b3510221f7596401424`.
- Evaluation context: `protocol`.
- Reference family: `claude-fable-5`.

## Run Command

`python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --model qwen2.5-coder:7b --escalation-model qwen2.5-coder:14b --max-repairs 2 --max-escalation-repairs 1 --timeout 180 --case-model-time-budget 690 --evaluation-context protocol --report project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_FOURTEENTH_UNTOUCHED.md --results-json project_ws/AgentOps/fable5_class_diagnosis_to_fix_blinded_fourteenth_untouched.json`

Runner exit code: 0. Runner wall time: 6265.3 seconds.

## Immutable Outputs

- JSON SHA-256: `00a351b77d26ac2ab4a2ab3e697c5499ef1d64795ee9d4309d3e7d02eba23b63`.
- Markdown report SHA-256: `270136947c22180cd6d2d782ca8fda15f395bec3141128eff8a900cd6ff35e00`.

## Result

- Overall score: 27.92/100.
- Verdict: `needs_improvement`.
- Evaluation verdict: `blinded_evaluation_failed`.
- Sealed-final solves: 0/12 (0%).
- Separate sealed-final adjudications performed: 12/12.
- Correct diagnosis families: 1/12 (8.33%).
- Correct causally accepted diagnoses: 0/12 (0%).
- Exact expected changed-file sets: 1/12 (8.33%).
- Public-test preservation: 12/12.
- Repair-feedback passes: 0/12.
- Prompt-contract closure: 12/12.
- Patches retained: 2/12.
- Premium calls: 0.
- Local model calls: 168 total; 146 on `qwen2.5-coder:7b`, 22 on `qwen2.5-coder:14b`.
- Model-call transport/errors: 3, all local `qwen2.5-coder:14b` timeouts.
- Deterministic disclosed-family repairs applied: 0, as expected for the novel holdout mechanisms.
- Average case duration: 521621.0 ms.
- Total measured case duration: 6259452 ms.
- Fable 5 same-task head-to-head run: false.
- Fable 5 parity claim: false.

Language results:

| Language | Average | Sealed | Diagnosis | Causal | Exact Files | Feedback |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Dart | 25.00 | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 |
| TypeScript | 31.67 | 0/3 | 0/3 | 0/3 | 1/3 | 0/3 |
| Python | 25.00 | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 |
| SQL | 30.00 | 0/3 | 1/3 | 0/3 | 0/3 | 0/3 |

No case passed the repair-feedback or sealed-final oracle. `th14_node_esm_plugin_loading` selected and changed the
exact two expected source owners, but its retained patch did not close repair feedback or the sealed final.

## Frozen Promotion Gate

| Criterion | Required | Observed | Result |
| --- | ---: | ---: | --- |
| Overall score | >= 90 | 27.92 | FAIL |
| Sealed-final solves | >= 10/12 | 0/12 | FAIL |
| Sealed-final solves in every language | >= 2/3 | 0/3 each | FAIL |
| Correct accepted causal families | >= 10/12 | 0/12 | FAIL |
| Exact expected changed-file sets | >= 10/12 | 1/12 | FAIL |
| Public-test preservation | 12/12 | 12/12 | PASS |
| Prompt-contract closure | 12/12 | 12/12 | PASS |
| Premium calls | 0 | 0 | PASS |
| Model-call transport errors | 0 | 3 | FAIL |

Promotion gate result: **FAIL**.

## Interpretation Lock

This result is preserved before any failure-specific diagnosis, repair, or replay. It demonstrates premium
independence and safe public-test preservation, but it does not demonstrate reliable transfer to this untouched
cross-language multi-owner distribution. There is no authenticated same-task Fable 5 comparison, no blind human
head-to-head adjudication, and no Fable 5 parity or superiority claim. Any execution after this one must be labeled a
disclosed development replay and may not replace this untouched result.
