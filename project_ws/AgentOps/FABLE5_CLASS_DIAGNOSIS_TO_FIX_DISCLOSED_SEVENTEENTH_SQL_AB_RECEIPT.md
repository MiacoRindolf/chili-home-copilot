# Seventeenth Disclosed SQL Recovery A/B Receipt

## Scope

- Case: `sqlite_effective_price_intervals`
- Status: disclosed development replay, not an unseen holdout
- Local reasoner: `qwen3:8b`
- Local editor: `qwen2.5-coder:7b`
- Premium calls: `0`
- Sealed seventeenth fixture and untouched result: unchanged

## Before Symbolic Contract

- Autonomy implementation: `fca0de2d` source, with disclosed fixture commit `b5c790d1`
- Score: `25/100`
- Functional sealed-final repair: false
- Local calls: `8/8` successful
- Selected owners: `db/schema.sql`, `db/resolved_prices.sql`
- Retained changed files: none
- Wall time: `388.5s`
- Failure: the local planner made the lower point-lookup bound exclusive, reversed the overlap predicate, and omitted UPDATE symmetry
- Artifact: `fable5_class_diagnosis_to_fix_disclosed_seventeenth_sql_recovery.json`

This established that timeout/transport recovery was working, while temporal reasoning remained wrong.

## Generic Repair

Commit `99122e485505b89dd3c887082917a877ff8bd877` added a reusable half-open effective-history contract:

- Legal adjacency: `old_end == new_start`
- Overlap: `existing_start < new_end AND new_start < existing_end`
- Open end: treated as infinity
- Point lookup: `start <= instant AND instant < end`
- Write integrity: identical INSERT and UPDATE overlap checks
- UPDATE identity: excludes the row being changed using the recognized primary key, with a guarded `rowid` fallback
- Safety: unrelated INSERT triggers are not cloned; an unrecognized write guard fails closed

The contract transfers to alternate `rates/effective_from/effective_to/as_of` source shapes in focused tests.

## Exact-Source Replay

- Source commit: `99122e485505b89dd3c887082917a877ff8bd877`
- Score: `100/100`
- Diagnosis: `data` (correct)
- Exact owners: `db/schema.sql`, `db/resolved_prices.sql`
- Public tests: passed
- Repair-feedback tests: passed
- Fresh isolated sealed final: passed
- Local calls: `2/2` successful, diagnosis only
- Generative planning/edit/repair calls: `0`
- Wall time: `88.3s`
- Premium calls: `0`
- Checkpoint removed only after atomic report and result writes
- Artifacts: `FABLE5_CLASS_DIAGNOSIS_TO_FIX_DISCLOSED_SEVENTEENTH_SQL_SYMBOLIC_RECOVERY.md` and `fable5_class_diagnosis_to_fix_disclosed_seventeenth_sql_symbolic_recovery.json`

## Validation

- Focused mechanism, transfer, fail-closed, and sealed-final tests: passed
- Broad affected suite: `348 passed`, with two pre-existing warnings
- Disclosed fixture validation: `8/8` valid; all public baselines pass and all feedback/final baselines fail
- Original case and final-oracle parity: `16/16` byte-identical to the sealed seventeenth fixture

## Interpretation

This is strong development evidence for one generalized symbolic contract family. It does not alter the untouched seventeenth `0/8` functional result, prove unseen transfer, or support a Fable 5 parity/superiority claim. The next authoritative evidence must be a newly authored holdout frozen after this source commit.
