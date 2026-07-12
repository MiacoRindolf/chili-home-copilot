# Tenth-Suite Mechanical-Contract Development Replay Receipt

Run completed: 2026-07-12

## Classification

This is a **disclosed development replay** of the previously scored tenth diagnosis-to-fix suite. It is not an
untouched holdout and cannot support a Fable 5 parity or superiority claim. The generated report retains the
fixture manifest's historical `blinded_holdout` label; this receipt is the authoritative evaluation
classification. The run does not replace the untouched tenth-suite result of 68.75/100 and 2/8 final passes.

## Frozen Inputs And Policy

- Source commit: `2675d4ef43c27d2e50697514f76a3ca5e0ee5ab1`
- Source tree: `ea0625fd78f7da1b9b5b29065f2a8f83da436199`
- Fixture tree: `f3c327cacdfba373dce2b635ad0c2db576abd667`
- Results schema: `chili.diagnosis-to-fix-results.v5`
- Primary model: local `qwen2.5-coder:7b`
- Primary repair rounds: 2
- Escalation model: local `qwen2.5-coder:14b`
- Escalation repair rounds: 1
- Per-call timeout: 240 seconds
- Premium calls allowed: 0
- Runner/source changes during execution: none

## Result

- Overall: **100/100**, `shadow_ready`
- Sealed-final functional solves: **8/8**
- Correct causal families: **8/8**
- Exact changed-file sets: **8/8**
- Public regressions preserved: **8/8**
- Premium calls: **0**
- Local calls: **58** total, 49 primary and 9 escalation
- Case-time sum: **1,445.4 seconds (24.1 minutes)**
- Average case time: **180.7 seconds**
- Previous disclosed full replay: 58.12/100, 168 calls, 6,334.5 seconds
- Relative change: 6 additional final solves, 65.5% fewer calls, and 77.2% less case time

## Route Audit

Six cases used a recognized mechanical contract repair after three local diagnostic stages and required no
generative repair rounds: relay rotation, scoped reservation retry, retry budget, Dart tombstone convergence,
Dart resumable ranges, and telemetry corrections. These cases averaged 64.2-77.1 seconds.

Two cases remained generative:

| Case | Calls | Repair rounds | Escalated | Duration |
|---|---:|---:|---:|---:|
| `ts_http_vary_isolation` | 19 | 3 | yes | 399.9s |
| `sql_tenant_grant_intervals` | 21 | 3 | yes | 631.7s |

Those two cases consumed 40/58 calls and both required 14B. They are the remaining disclosed reproducibility and
latency targets; the successful result does not justify routing arbitrary Vary or temporal SQL work through an
unvalidated shortcut.

## Artifact Integrity

- `DEVELOPMENT_REPLAY.md` SHA-256: `cdc0c0d8ff19c6400cd6e544a316211673cbdd025cec44bd80b9167c71f3db6e`
- `development_replay.json` SHA-256: `fc6e30357e55ac55c1b30aa3a90477cb0990d61fb3642c94152e4e53bf8bdefa`

## Interpretation

This replay proves that CHILI can combine local diagnosis, deterministic source-shape operators, strict contract
guards, and isolated validation to reproduce all eight disclosed repairs without premium calls. It does not prove
unseen transfer or Fable 5-level complex diagnosis. The next authoritative quality gate must use newly authored
tasks that did not inform these operators.
