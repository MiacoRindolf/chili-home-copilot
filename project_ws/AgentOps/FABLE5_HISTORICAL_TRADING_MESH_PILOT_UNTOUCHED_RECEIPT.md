# Historical Fable 5 Mesh Trading Pilot Untouched Receipt

## Status

The first post-freeze protocol run of `python_mesh_teacher_queue_pressure` is complete. CHILI source was frozen at
`b25c105878a2c802596cbfafc342e69c348aecc9`; the fixture-only commit was `ccd03ba9`. No CHILI source or fixture
file changed between fixture validation and scoring.

Historical reference commit: `d5ee0e92798398d6617a31ac092ec4757cfd4187`, `fix: cap mesh teacher under queue
pressure`. The user identifies the source conversation as Fable 5 work. This is user-attested historical reference
evidence, not provider-authenticated same-task Fable 5 output.

## Result

- Score: **40/100**
- Sealed-final functional solve: **0/1**
- Expected diagnosis family: `config`; retained family: `config`
- Expected owners: `mesh/settings.py`, `mesh/aggregator.py`
- First plan owners: `mesh/aggregator.py`, `mesh/repository.py`
- First feedback plan owners: both expected owners; retained changed files: none
- Public tests: passed
- Repair-feedback tests: failed
- Fresh isolated sealed-final tests: failed
- Patch retained: false
- Live-reasoning-qualified: false
- Local calls: **13 total, 11 successful, 2 timeouts**
- Wall time: **488.1 seconds**
- Premium calls: **0**
- Verdict: `needs_improvement`
- Git-normalized Markdown SHA-256: `fe5b33a36201514bf792feec10c2cb2f93de14b4166b147ccfedc9e1b3261a4e`
- Git-normalized results SHA-256: `b68b79d7c379898f64771d000e441c961d2b975fdb1989c0d7e1f217fe4bb1c6`
- Checkpoint: removed only after atomic report/result writes

## Failure Chain

1. The local reasoner selected the correct `config` family but promoted only an observational, inconclusive
   statement that a zero daily cap disabled shedding. It did not establish the complete boundary linking settings,
   measured queue pressure, optional teacher admission, and mechanical fallback.
2. The first plan assigned the daily cap and admission gate to `mesh/aggregator.py`, but incorrectly selected
   `mesh/repository.py` instead of the settings owner. It proposed modifying the queue-depth provider even though
   that provider already reported the required runtime state.
3. The editor changed the fallback constant to 50, then nested the pressure check under the daily-cap exhaustion
   branch, passed the settings object to `pending_queue_depth`, and referenced repository symbols without importing
   them. Its repository edit duplicated existing behavior instead of wiring policy at the consumer.
4. Compiler and public-regression correction calls removed some malformed output but did not repair ownership,
   imports, database provenance, threshold configuration, zero-threshold override, or fail-open telemetry. CHILI
   rolled the initial attempt back because it made no validated progress.
5. Feedback exposed both missing settings fields and continued calls at queue pressure. The next plan selected the
   correct two owners, but its edit searched for a hallucinated `def Settings()` instead of the actual dataclass and
   changed only daily-cap coercion in the aggregator. The atomic multi-file adapter rejected and rolled back the
   group.
6. The second repair-plan call exhausted the frozen case budget. The original source remained intact and the
   sealed final correctly failed below-threshold, zero-override, missing/failing probe, all-stage fallback, and
   continued daily-cap checks.

## Interpretation

This is a second, disjoint negative result against current Fable 5 replacement readiness. The earlier disclosed
short-direction mechanism cannot help with effective policy, live queue state, optional-work shedding, and
fallback continuity. CHILI classified the broad family correctly and its safety controls prevented a malformed
patch from surviving, but it did not produce a source-grounded causal proof or coherent two-owner repair within the
local budget.

The untouched score remains authoritative for this pilot even if a later disclosed replay passes. This replay is
current-agent-authored after source freeze, so it measures post-freeze transfer but does not satisfy the independent
author promotion gate.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_MESH_PILOT_UNTOUCHED.md`
- `project_ws/AgentOps/fable5_historical_trading_mesh_pilot_untouched.json`
- `tests/fixtures/autonomy_diagnosis_to_fix_fable5_trading_mesh_pilot/`
