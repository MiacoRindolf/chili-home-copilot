# Fable 5-Class Diagnosis-to-Fix Blinded Thirteenth Disclosed Development Receipt

Date: 2026-07-13

## Scope Lock

- Evaluation context: `disclosed_replay`.
- Reference family: `claude-fable-5`.
- The immutable untouched result remains commit `f0237e7745816e6277c0cefda460433db29b86de` at 40.83/100.
- This receipt records known-case development and regression evidence only.
- Fable 5 same-task head-to-head: false.
- Fable 5 parity or replacement claim: false.

## Frozen Implementation

- Implementation commit: `2cc8e9d446e0ceb66abf5bf688596efb869f0133`.
- Implementation tree: `3ebcc8cb37574185c404848464a62ff06612fefe`.
- Parent: `f0237e7745816e6277c0cefda460433db29b86de`.
- Fixture commit: `d627a8e4a1c3611644280ae6928e5aa733c1a45a`.
- Fixture Git subtree: `8f1b502ffdde4a0312d21e1a72132f04faa39dd3`.
- Canonical authored-byte aggregate: `e05b205217ab8e618d6946b7811df744c836db4b71ddbd81adf09cd4f4761803`.
- Full fixture aggregate: `8cc56e5e106b27b1b80bb2fd1ebd8731e16bbc07e197c45b5cebd2bc24e7f6f7`.

## Consolidated Replay

Command:

`python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_thirteenth --model qwen2.5-coder:7b --escalation-model qwen2.5-coder:14b --max-repairs 2 --max-escalation-repairs 1 --timeout 180 --case-model-time-budget 690 --evaluation-context disclosed_replay --report project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_THIRTEENTH_FULL_DISCLOSED_REPLAY_V1.md --results-json project_ws/AgentOps/fable5_class_diagnosis_to_fix_blinded_thirteenth_full_disclosed_replay_v1.json`

- Runner exit code: 0.
- Runner wall time: 1921.2 seconds.
- JSON SHA-256: `378ef8b2ca7e1c496d4fa8e3ea5f6fba8b1a583b53b0f5741a73e572e1bc7a11`.
- Markdown SHA-256: `456b6a14eb129f23a3ccda2a208e8eee5ec62a4dfc3cb52c4bfb9f8df31ecb3b`.
- Overall score: 100/100.
- Evaluation verdict: `disclosed_replay_passed`.
- Sealed-final solves: 12/12.
- Correct accepted diagnosis families: 12/12.
- Exact expected file sets: 12/12.
- Public, feedback, and prompt-closure passes: 12/12 each.
- Retained patches: 12/12.
- Deterministic repairs: 10/12.
- Bounded generative repairs: 2/12, both Node cases.
- Local model calls: 49 total; 45 on `qwen2.5-coder:7b`, 4 on `qwen2.5-coder:14b`.
- Model-call transport errors: 0.
- Premium calls: 0.

## Exact-Freeze Confirmation

After the consolidated replay, one transfer-hardening change made Python import insertion preserve module docstrings and
`from __future__` placement. No benchmark mechanism or expected output changed. The final implementation commit then
passed 253 focused architecture regressions and an exact-commit sealed rerun of the affected monthly scheduling case.

- Confirmation score: 100/100.
- Confirmation premium calls: 0.
- JSON SHA-256: `5a6d3596fada50a7f2c70d00f03ba2ef72d806232f426a6b7d3df904d1c577cd`.
- Markdown SHA-256: `fdef80ac242b6137805bb504c3277c1425d78c3cd53e553f2b6f09f146a09fe5`.
- Regression command result: 253 passed, 2 known warnings.
- `py_compile`: passed for every changed Python module and test.
- `git diff --check`: passed before the implementation commit.

## Capability Delta

The frozen implementation adds fail-closed SQL validation for missing application schemas, exact failed-contract
coverage recovery, atomic multi-file adapter retry, causal acceptance and retraction, accepted-family reporting, and
bounded structural operators for coupled contracts across SQL, Python, Dart, and Node. No operator contains a
thirteenth case identifier or fixture path, and every proposed repair remains subject to syntax, public, feedback,
prompt-closure, and sealed-final validation.

## Interpretation Lock

The disclosed score shows that CHILI can learn from the untouched failures and retain those repairs across four
languages without premium models. It does not measure transfer to unseen mechanisms. Promotion remains blocked until
the frozen implementation passes a newly authored, independently validated, untouched holdout. Authenticated Fable 5
outputs on the exact same frozen tasks are also still absent.
