# Autonomous Diagnosis-to-Fix Benchmark

- Run: 2026-07-14T08:58:56.349200+00:00
- Local model: `qwen2.5-coder:7b`
- Local reasoning model: `qwen3:8b`
- Local escalation model: `disabled`
- Reference family: `claude-fable-5`
- Overall score: **40.0/100**
- Development-regression score: **40.0/100**
- Blinded holdout score: **not run**
- Functional sealed-final solve rate: **0.0%**
- Causal-diagnosis accuracy: **100.0%**
- Exact changed-file-set accuracy: **0.0%**
- JSON-parsed diagnostic stages: **100.0%**
- Accepted diagnostic stages: **100.0%**
- Causally accepted diagnoses: **0.0%**
- Live-reasoning-qualified cases: **0.0%**
- Deterministic-only cases: **0.0%**
- Deterministic contracts disabled: **true**
- Autonomy verdict: **needs_improvement**
- Comparison verdict: **disclosed_replay_failed**
- Premium calls: **0**
- Average wall time: **488.2s/case**
- Maximum bounded repair rounds: **2**
- Escalation repair rounds: **0**
- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.
- Test subprocess containment: **static scan + seeded-file SHA-256 guard; not hostile-process proof**.

| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |
|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| python_split_candidate_scope_lanes | python | pytest | development_regression | disclosed_replay | 40 | data | - | false | true | false | false |

## Interpretation

Each repository is created from one benchmark case. The live model sees only the prompt, candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the initial patch. For sealed entries, final adjudication tests run once in a separate repository after all model calls and never enter a repair prompt. Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use a separate final oracle. Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any member edit is rejected. A high score proves this bounded repair contract only; broader Fable 5 parity still requires blinded multi-repository adjudication. A deterministic-only functional solve keeps its score but cannot receive shadow_ready or support a Fable 5-class reasoning claim. Test subprocesses are not OS-isolated in this runner; static screening and seeded-file mutation hashes reduce but do not eliminate hostile-process risk.
