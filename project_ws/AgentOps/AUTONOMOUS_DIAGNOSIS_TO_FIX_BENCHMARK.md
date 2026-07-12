# Autonomous Diagnosis-to-Fix Benchmark

> Evidence classification correction (2026-07-11): the historical `Hidden` column below was repair feedback. Those tests could guide bounded repair after the initial patch and were then scored, so this is not sealed final-adjudication evidence. Runner schema v3 now requires a separate final oracle for blinded holdouts and first reads it after all model calls.

- Run: 2026-07-11T11:24:52.396491+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **97.1/100**
- Holdout score: **96.7/100**
- Multi-file holdout score: **100.0/100**
- Verdict: **needs_improvement**
- Premium calls: **0**
- Average wall time: **116.6s/case**
- Maximum bounded repair rounds: **5**
- Fable 5 parity claim: **No**. This is a local held-out repair benchmark, not a blinded frontier head-to-head.

| Case | Split | Score | Diagnosis | Changed files | Patch | Public | Hidden |
|---|---|---:|---|---|---:|---:|---:|
| clock-201 | holdout | 100 | clock | session_gate.py | true | true | true |
| config-202 | holdout | 80 | config | feature_gate.py | true | true | false |
| data-203 | holdout | 100 | data | replay_copy.py | true | true | true |
| clock-chain-301 | calibration-multifile | 100 | clock | replay_runner.py, session_policy.py | true | true | true |
| config-chain-302 | holdout-multifile | 100 | config | flags.py, worker_mode.py | true | true | true |
| data-contract-303 | holdout-multifile | 100 | data | quote_source.py, replay_mirror.py | true | true | true |
| state-dedupe-304 | holdout-multifile | 100 | state | dedupe_registry.py, worker.py | true | true | true |

## Targeted Repair Receipt

The primary uninterrupted run passed hidden behavior in 6/7 cases. Its only failed row was `config-202`, where a stochastic edit added `"0"` to `_TRUE_VALUES` and later stale SEARCH blocks were rejected. A newer run after adding the generic semantic-polarity guard scored **100/100** for that exact row with public and hidden tests passing and zero premium calls.

- Effective covered rows after the newer targeted repair: **7/7**
- Clean uninterrupted full-run 100 claim: **No**
- Targeted receipt: `project_ws/AgentOps/AUTONOMOUS_DIAGNOSIS_TO_FIX_TARGETED_REPAIR.md`
- Raw targeted receipt: `project_ws/AgentOps/autonomous_diagnosis_to_fix_targeted_repair.json`
- Untouched multi-file first-run baseline: `project_ws/AgentOps/MULTIFILE_HOLDOUT_FIRST_RUN.md`

## Interpretation

Each repository is created from a legacy structurally held-out or explicitly labeled calibration case. The model sees only the prompt, candidate source, and public tests for its initial patch. Oracle labels and the historical `Hidden` tests are loaded afterward, and those test failures may guide bounded repair. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. Targeted repair receipts may close failed rows but do not rewrite the primary run verdict. A high score proves this feedback-guided development contract only; broader Fable 5 parity still requires sealed, blinded multi-repository adjudication.
