# Fable 5-Class Diagnosis-to-Fix Tenth Run Receipt

## Authority

This is the first complete untouched run against the independently authored tenth sealed suite. It is authoritative even though it failed the requested replacement standard. No development replay may overwrite it.

## Frozen inputs

- Production source: `3241f6743e8b8745c39542f4645290a4a8d6f44a`
- Production repair commit: `550b7e4`
- Fixture-only freeze: `d927f5f9f93d30e30f40a4551f4b198a3bb31202`
- Fixture aggregate SHA-256: `dc9c72540142cc14d7043625b0112b8997cbda412d66ba5e58f105490769e59a`
- Runner SHA-256: `48c25c43cc9a2aa60ea967dff11111a4f2c5b3819b3439dce392128787350ad3`
- Protocol SHA-256: `1f8afeb51cd1d7467b9881a8c06f5453801f2c4b7289c0613175ef09953eca53`
- Run-policy SHA-256: `192856f16eb5d08e32400424659d549ece56f819bed4ac5d24f957e2b3f8c911`
- Environment SHA-256: `52164ccea09f89137b406e50eb5e79550f03b0527733a76b27dc84f345e86484`

The fixture author and independent validator were context-isolated and attested that they did not access CHILI source/history, prior fixtures/reports, chats/transcripts, Claude/Fable material, or the internet. The parent controller received metadata and hashes only before scoring and did not inspect fixture bodies.

## Precommitted policy

- Primary: `qwen2.5-coder:7b`
- Primary repair rounds: 2
- Escalation: `qwen2.5-coder:14b`
- Escalation rounds: 1
- Per-call timeout: 240 seconds
- Premium calls allowed: 0
- Cases: 8, all sealed and all requiring exactly three source owners

## Result

- Overall and blinded score: **68.75/100**
- Functional sealed-final solve rate: **25.0% (2/8)**
- Correct causal-diagnosis family: **37.5% (3/8)**
- Exact changed-file set: **87.5% (7/8)**
- Public regressions preserved: **100% (8/8)**
- Repair-feedback pass: **25.0% (2/8)**
- Sealed-final pass: **25.0% (2/8)**
- Patch produced: **7/8**
- Premium calls: **0**
- Verdict: `needs_improvement`
- Comparison verdict: `blinded_evaluation_failed`
- Fable 5 parity claim: **No**

| Case | Language | Expected family | Diagnosed | Score | Exact files | Final | Duration |
|---|---|---|---|---:|---:|---:|---:|
| `py_relay_rotation_window` | Python | dependency | state | 60 | yes | fail | 395.0s |
| `py_reservation_retry_scope` | Python | state | state | 80 | yes | fail | 491.1s |
| `ts_http_vary_isolation` | TypeScript | config | state | 60 | yes | fail | 383.3s |
| `ts_retry_budget_clock` | TypeScript | clock | state | 60 | yes | fail | 429.1s |
| `dart_offline_tombstone_join` | Dart | state | clock | 30 | no | fail | 442.5s |
| `dart_resumable_chunk_boundaries` | Dart | data | code | 80 | yes | pass | 138.3s |
| `sql_tenant_grant_intervals` | SQL | data | data | 100 | yes | pass | 79.3s |
| `sql_telemetry_correction_rollup` | SQL | data | data | 80 | yes | fail | 423.0s |

The two functional solves were the Dart resumable-chunk case and SQL tenant-grant case. The Dart solve still used the wrong diagnosis family; only the SQL grant case was a complete 100/100 solve.

## Execution evidence

- End-to-end wall time: **2,786.5 seconds**
- Average case time: **347.7 seconds**
- Model calls: **141/141 successful**
- 7B calls: **111**
- 14B calls: **30**
- Total output tokens: **31,736**
- Average call latency: **19.28 seconds**
- Maximum call latency: **102.78 seconds**
- Repair attempts: **20**
- Attempts producing a patch: **16**
- Validation rollbacks: **5**
- Model calls after final oracle load: **0**

## Interpretation

The post-fix architecture transferred partially: functional success doubled from the ninth run's 1/8 to 2/8, diagnosis accuracy rose from 1/8 to 3/8, and exact file ownership rose from 3/8 to 7/8. This is meaningful progress, especially for multi-file scope selection and local-only reliability.

It is still decisive negative evidence against current Fable 5 replacement readiness. Six final contracts failed, five diagnosis families were wrong, the 14B fallback imposed substantial latency, and only one case combined correct diagnosis, exact ownership, and final correctness. No authenticated same-task Fable 5 run is included, so superiority or parity cannot be claimed.

## Artifact hashes

```text
8f46a2e40a8d1ea7b482f86da43cf6b30d2d01baeb522c92990c09f88729538a  FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_TENTH_RUN.md
482544c28b16760f8ae344a3dc02868e062f3f69121475fe69cede1a9270c12d  fable5_class_diagnosis_to_fix_blinded_tenth.json
1f8afeb51cd1d7467b9881a8c06f5453801f2c4b7289c0613175ef09953eca53  PROTOCOL.md
192856f16eb5d08e32400424659d549ece56f819bed4ac5d24f957e2b3f8c911  RUN_POLICY.json
52164ccea09f89137b406e50eb5e79550f03b0527733a76b27dc84f345e86484  ENVIRONMENT.json
3cc6813b878f51db4862c83cde2f07106c230e043d453583b6e6459236903c40  FREEZE_RECEIPT.json
```

The Git worktree was clean after scoring, and the frozen runner and production-orchestrator hashes still matched the protocol.
