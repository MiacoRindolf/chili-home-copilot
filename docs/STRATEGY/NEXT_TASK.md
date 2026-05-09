# NEXT_TASK: f-pattern-demote-sweep-wiring-fix

STATUS: PENDING

## Goal

Re-wire `run_thin_evidence_demote` from the event-driven
`_handle_execution_feedback_digest` (which fires only a few times
per day in this operating state) into the per-cycle
`run_brain_work_dispatch_round` loop (~75-90s cadence). Pattern
585 was manually hand-kicked tonight via direct invocation;
without the wiring fix, future thin-evidence patterns won't be
auto-demoted on a meaningful timeline.

The full brief is at
`docs/STRATEGY/QUEUED/f-pattern-demote-sweep-wiring-fix.md`
— read it first.

## Why now (algo-trader-architect framing)

Phase D (`f-pattern-demote-on-thin-evidence`, commit `dfb39f0`)
shipped earlier today. The sweep code is correct: 15 tests pass,
hand-kick demoted pattern 585 cleanly. But the wiring depends on
the brain-worker's `execution_feedback_digest` event, which fires
on `live_trade_closed` triggers — sparse in current state (24h
ledger showed 3 events).

Real algo impact: when the next thin-evidence pattern gets
promoted via `provisional_small_paths`, it won't auto-demote until
either (a) live_trade_closed fires, or (b) operator runs a manual
sweep. That's not the durable solution.

**Why this is the right next move tonight:**

- **Small scope** (~30–60 min CC). Late-night-friendly.
- **Tightly-bounded surface** (`brain_work/dispatcher.py` +
  optional gate flag). No multi-week architectural risk.
- **Hard integration-verification gate** in acceptance
  criteria — the lesson from tonight's three "tests-pass-but-
  system-fails" instances bakes in here.
- **Closes Phase D's intent.** Without this, Phase D is a
  partial fix.

## Why this scope (vs. the alternatives)

* **Vs. architectural rebuild Phase 1** (auth liveness + typed
  result): week of work touching many call sites. Doing tired is
  exactly the recipe for tonight's failure modes. Defer to fresh
  morning.
* **Vs. `f-pdt-crypto-bypass-cleanup`** (hygiene): small but no
  observable benefit. Less leverage.
* **Vs. `f-autotrader-pdt-aware-exit-deferral`**: premise was
  flawed; needs rewriting before it can ship.
* **Vs. wiring `rh.crypto.order_*` for actual crypto stops**:
  larger scope, real-money risk, NOT for tonight.

## The change

Per the queued brief
(`docs/STRATEGY/QUEUED/f-pattern-demote-sweep-wiring-fix.md`):

1. Wire `run_thin_evidence_demote` into
   `run_brain_work_dispatch_round` (the per-cycle dispatcher
   loop in `app/services/trading/brain_work/dispatcher.py`).
   Sweep runs once per round (~75-90s) regardless of
   work-ledger state.
2. Either remove the existing `_handle_execution_feedback_digest`
   sweep call (single source of truth), OR gate it behind
   `if not _PER_CYCLE_SWEEP_ENABLED:` so only one path fires.
3. Wrap the new per-cycle call in `try/except` so a sweep
   failure doesn't poison the round.
4. Log demoted IDs at INFO level for grep visibility:
   `[learning] thin_evidence sweep: demoted=N ids=[...]`.

## Acceptance criteria (with integration-verification baked in)

1. Per-cycle hook fires on every `run_brain_work_dispatch_round`
   invocation, NOT on `execution_feedback_digest` event.
2. `_handle_execution_feedback_digest`'s sweep call removed OR
   gated.
3. Tests in `tests/test_pattern_demote_sweep_wiring.py`:
   - **Helper-level**: dispatcher mocked, assert
     `run_thin_evidence_demote` is called per round.
   - **Helper-level**: sweep raises an exception, assert the
     round still completes and other dispatch work runs.
   - **INTEGRATION (LIVE PATH)**: seed a fresh thin-evidence
     pattern in chili_test (4 trades / 25% WR / no OOS /
     `provisional_small_paths`); call
     `run_brain_work_dispatch_round` directly; assert the
     pattern is `lifecycle_stage='challenged'` after the round
     completes. NOT just `run_thin_evidence_demote(db)` in
     isolation — the FULL CHAIN.
4. Existing 15 thin-evidence-demote tests still pass.
5. Live verification: post-deploy, watch brain-worker logs for
   `[learning] thin_evidence sweep` INFO line at expected
   cadence (~75-90s). Pattern 585 stays demoted.
6. CC report at
   `docs/STRATEGY/CC_REPORTS/2026-05-09_f-pattern-demote-sweep-wiring-fix.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/brain_work/dispatcher.py` —
  `run_brain_work_dispatch_round`. Add the sweep call after the
  existing fanout, in try/except.
- `app/services/trading/learning.py:run_thin_evidence_demote` —
  unchanged. Already idempotent and cheap.
- `app/services/trading/brain_work/dispatcher.py:_handle_execution_feedback_digest`
  — remove the sweep call OR gate it behind the new flag.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Don't touch the sweep predicate or the SQL UPDATE.** They're
  correct; only the wiring is broken.
- **Don't touch the four threshold constants.**
- **Don't widen scope** to the architectural rebuild or to other
  Phase 2/3/4 work.
- **No magic numbers** — dispatcher cadence stays whatever the
  existing loop gives.
- **Edit-tool truncation discipline (HARD).**
- **Tests use `_test`-suffixed DB.**

## Out of scope

- Re-promotion path for demoted patterns.
- Other lifecycle gaps (consecutive-loss-demote, regime-mismatch).
- Phase 2/3/4 of the architectural rebuild.
- The architectural rebuild Phase 1 (auth liveness).

## Sequencing

1. Truncation scan on `brain_work/dispatcher.py` and `learning.py`.
2. Locate `run_brain_work_dispatch_round` and confirm round
   cadence in current logs.
3. Add the post-fanout sweep call inside try/except.
4. Remove or gate the `_handle_execution_feedback_digest` sweep
   call.
5. Tests (3 minimum: helper x2 + integration x1).
6. **Run the integration test ALONE** — it must pass before the
   next step. This is the lesson from tonight.
7. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate brain-worker scheduler-worker`.
3. Watch brain-worker logs for ~3 min:
   ```
   docker logs -f --tail 0 chili-home-copilot-brain-worker-1 | grep -i thin_evidence
   ```
   Expected: a `[learning] thin_evidence sweep: demoted=0 ids=[]`
   line every dispatcher round (~75-90s) once pattern 585 is
   already demoted (no new candidates).
4. (Optional smoke) Insert a fake thin-evidence pattern in
   chili_test, run one dispatcher round, verify it gets demoted.

## Rollback plan

`git revert` the commit. Restores prior event-driven hook (still
present and correct, just dormant). Pattern 585 stays
`challenged` regardless because lifecycle_stage doesn't
auto-revert.

## What CC should do if it's unsure

1. **If the dispatcher cadence is unstable** (e.g., scales with
   work-ledger backlog), add a minimum-cadence guard so the sweep
   doesn't run more than once every 60s. Tunable via settings.
2. **If the integration test seed-fixture is hard to construct**
   (chili_test schema mismatch with prod scan_patterns),
   surface the gap and propose a smaller integration test that
   uses a real `scan_patterns` row but mocks the dispatcher
   round.
3. **If removing the `_handle_execution_feedback_digest` sweep
   call has broader implications** (e.g., the function is called
   from a code path I missed), gate it instead of remove. Surface
   the choice in the CC report.
