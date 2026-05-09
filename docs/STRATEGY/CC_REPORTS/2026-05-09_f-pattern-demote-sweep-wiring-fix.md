# CC_REPORT: f-pattern-demote-sweep-wiring-fix

## Outcome

`run_thin_evidence_demote` is now called from
`run_brain_work_dispatch_round` instead of
`_handle_execution_feedback_digest`. The sweep fires every
~75–90 s round regardless of work-ledger state, closing Phase D's
intent: thin-evidence patterns auto-demote on a meaningful timeline.

Single source of truth: the prior call inside
`_handle_execution_feedback_digest` is removed (not gated). Live
verification path: brain-worker logs show
`[brain_work_dispatch] thin_evidence sweep: demoted=N ids=[...]`
once per round at INFO when there are demotions; DEBUG otherwise.

## Per-step status

### Step 1 — Survey + truncation scan — COMPLETE
* `dispatcher.py` 485 lines, AST clean. Two relevant call sites:
  * `_handle_execution_feedback_digest` at line 112 (the sparse
    event-driven hook).
  * `run_brain_work_dispatch_round` at line 282 (the per-cycle
    loop the new wiring targets).
* `learning.py` (Phase D) untouched per brief constraints.

### Step 2 — Re-wire (splice pattern) — SHIPPED
`dispatcher.py` splice (+~25 lines, AST clean):

* **Removed** the sweep call + import inside
  `_handle_execution_feedback_digest`. The hook still runs
  `run_live_pattern_depromotion` (live-vs-OOS depromotion is a
  different concern; it stays event-driven).
* **Removed** the merged-thin-counts block from the depromotion
  payload (no longer relevant since `thin` doesn't flow through
  this hook).
* **Added** a per-cycle sweep call at the END of
  `run_brain_work_dispatch_round`, just before the `return {...}`.
  Wrapped in try/except so a sweep failure doesn't poison the
  round; failure surfaces in the result dict's
  `thin_evidence_sweep.ok=False` + an `error` string.
* **Added** structured log lines:
  * `[brain_work_dispatch] thin_evidence sweep: demoted=N ids=[...]`
    at INFO when demotions fire (grep target for ops).
  * `[brain_work_dispatch] thin_evidence sweep: demoted=0 ids=[]`
    at DEBUG when no demotions fire (avoids per-round WARN noise).
* **New result-dict key**: `thin_evidence_sweep` carries the sweep
  result so observability + ops grep keep working through the
  dispatcher API.

### Step 3 — Integration test FIRST (per brief's lesson) — SHIPPED
`tests/test_pattern_demote_sweep_wiring.py:test_integration_dispatch_round_demotes_thin_evidence_pattern`:

Seed a thin-evidence pattern (id=585, 4 trades, 25% WR, no OOS,
`provisional_small_paths`). Call
`run_brain_work_dispatch_round(db, user_id=None)` directly. Assert:

* `res["ok"] == True`.
* `res["thin_evidence_sweep"]` carries the result dict.
* `585 in res["thin_evidence_sweep"]["demoted_ids"]`.
* DB row reads `lifecycle_stage='challenged'`, `demoted_at` set,
  `promotion_demote_reason='thin_evidence_low_realized_wr'`.

**This test was run ALONE first** (61.33s standalone). Tonight's
prior briefs all passed unit tests but failed in integration; this
brief baked the live-path test into the acceptance loop. PASSES.

### Step 4 — Helper + edge-case tests — SHIPPED
6 tests total in `tests/test_pattern_demote_sweep_wiring.py`:

1. `test_integration_dispatch_round_demotes_thin_evidence_pattern`
   (the integration test above).
2. `test_integration_dispatch_round_idempotent_on_already_challenged`
   — second round doesn't re-touch the row (lifecycle filter
   short-circuits).
3. `test_integration_dispatch_round_keeps_healthy_pattern` —
   pattern 1011 fingerprint (409 trades / 63.2% WR) stays
   promoted.
4. `test_dispatch_round_completes_when_sweep_raises` —
   try/except wrapper contract: sweep raises → round still
   `ok=True` and surfaces failure in the result.
5. `test_execution_feedback_digest_no_longer_calls_sweep` —
   single-source-of-truth check: monkey-patch the sweep with a
   call-spy, drive the digest hook, assert spy NOT invoked.
6. `test_round_result_dict_has_thin_evidence_sweep_key` — pin
   the new dispatcher result-dict surface so future refactors
   don't quietly drop the observability hook.

### Step 5 — CC report + commit + NEXT_TASK DONE — IN PROGRESS

## Surprises / deviations

1. **Removed (not gated) the old sweep call.** Brief offered a
   choice between removal and gating behind
   `_PER_CYCLE_SWEEP_ENABLED`. Went with removal because the
   single-source-of-truth invariant is easier to reason about
   than two paths whose behaviour depends on a flag, and the
   removed call is preserved in git history if a rollback is
   needed (`git revert` restores it).

2. **Used `python -m pytest` instead of `pytest`.** Windows
   Device Guard intermittently blocks the bare `pytest.exe`
   wrapper; `python -m pytest` invokes the same module via the
   trusted python.exe entry point and consistently runs.
   Documenting because operator-side test runs may hit the same
   thing.

3. **No min-cadence guard added.** Brief's "if unsure" §1 said to
   add one if the dispatcher cadence is unstable. Cadence is
   stable per the existing per-cycle loop's release/claim/process
   structure; the sweep itself is cheap (one indexed query +
   per-row UPDATE). If a future brief surfaces excessive sweep
   frequency, lift via a settings-tunable
   `chili_thin_evidence_sweep_min_interval_secs`. Not needed
   tonight.

4. **`thin_evidence_sweep` key in result dict is new contract.**
   Pinned by `test_round_result_dict_has_thin_evidence_sweep_key`.
   Downstream consumers of `run_brain_work_dispatch_round` would
   need updating if they care about the new field; today none do
   (grep confirms).

## Open questions (carried from brief)

1. **Cadence stability** — verified stable in current operating
   state.
2. **Integration seed-fixture complexity** — the chili_test schema
   is a superset of prod; ORM-backed seed via `ScanPattern(...)`
   handled all NOT-NULL JSONB defaults cleanly (no surprise
   `rules_json IS NULL` failure like Phase D's first run).
3. **Removing vs gating the old call** — went with removal (see
   Surprises §1).

## Verification

* `dispatcher.py`: `wc -l` 485 → 508 (+23); AST clean; importable.
* Integration test PASSES standalone in 61.33s.
* Full suite (6 wiring tests + 15 Phase D regression) passes.
* Phase D's `run_thin_evidence_demote` and the four threshold
  constants untouched per brief constraints.

## Operator-side after CC ships

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate brain-worker scheduler-worker`.
3. Watch brain-worker logs for ~3 min:
   ```
   docker logs -f --tail 0 chili-home-copilot-brain-worker-1 \
     | grep -i thin_evidence
   ```
   Expected steady-state cadence: one
   `[brain_work_dispatch] thin_evidence sweep: demoted=0 ids=[]`
   line every ~75–90s round (since pattern 585 is already
   `challenged` and no other thin-evidence candidates exist).
4. (Optional smoke) Insert a fake thin-evidence pattern in
   chili_test and verify it gets demoted within one round.

## Rollback plan

`git revert` the commit. Restores the sparse event-driven hook
(still correct, just dormant). Pattern 585 stays `challenged`
regardless because `lifecycle_stage` doesn't auto-revert.

## What's NEXT after this ships

* Architectural rebuild Phase 1 (auth liveness + typed result) —
  fresh-start tomorrow.
* `f-pattern-oos-revalidation` (re-promote on fresh OOS pass) —
  the natural complement to this brief's auto-demote loop.
* If new thin-evidence promotions accumulate after deploy, surface
  the IDs from the brain-worker logs to operator review.
