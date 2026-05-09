# NEXT_TASK: f-phase-e-revert-and-bracket-writer-crash-fix

STATUS: DONE

## Goal

Two tightly-scoped fixes that remove ACTIVE risks tonight without
biting off the multi-week architectural rebuild:

1. **Revert Phase E** (`f-crypto-stale-trade-closer`, commit `c8aec21`).
   The brief was wrong — it took broker silent-empty as ground
   truth. The disable flags in `.env` are belt-and-suspenders;
   the actual code is still a footgun. One mechanical `git revert`.

2. **Fix the `bracket_writer.place_missing_stop` IndexError**
   (`error: "list index out of range"`). Active crash loop on
   ADA/SOL since 2026-05-09 01:57 UTC. Every minute, two events:
   `g2_place_missing_stop_submitting` then
   `g2_place_missing_stop_rejected` with the exception. Real-money
   risk: stops have NOT been placed for those crypto positions
   for hours.

The full root-cause analysis is at
`docs/STRATEGY/QUEUED/f-crypto-reconcile-architectural-rebuild.md`
(read for context). This brief addresses Anomalies 6 (crash) and
also clears the codebase of Phase E (which the architectural
rebuild calls out as "to be reverted, not extended").

## Why now (algo-trader-architect framing)

Tonight's incident: Phase E falsely cancelled 14 crypto trades.
None lost capital because the broker still held them and the
revert-restore was within 22 minutes. But the architectural audit
revealed Phase E is structurally unsafe AND there's an active code
crash loop in the bracket writer that's been preventing stop
placement on ADA/SOL.

These two are the highest-leverage moves before the multi-week
rebuild:
* Removing Phase E source removes the chance of accidental
  re-enable + repeat false-cancellation incident.
* Fixing the IndexError lets the bracket-writer's recovery path
  actually execute, restoring stop-loss protection on at least
  ADA/SOL (the two trades with bracket_intents that the writer
  is repeatedly trying to repair).

## Why this scope (vs. the alternatives)

* **Vs. Phase 1 of the architectural rebuild (auth liveness +
  typed result):** Phase 1 is a week of careful work that touches
  many call sites. Doing it tired is a recipe for the same class
  of mistake as Phase E. Defer to fresh-start tomorrow.
* **Vs. Anomaly 4 (crypto exit_monitor deterministic close):**
  Currently mitigated — auth is restored, exit_monitor's sells
  are filling at the broker, broker_sync stale-close eventually
  flips status='closed'. Loses exit_reason fidelity but doesn't
  lose money. Schedule for the rebuild's Phase 2.
* **Vs. Anomaly 5 (sync_pending_exit_order dead code):** Same
  as Anomaly 4 — wired into Phase 2 of the rebuild.
* **Vs. the missing bracket_intents on 10 crypto trades:**
  Schedule for Phase 3 of the rebuild. Today's IndexError fix
  doesn't address the missing-intents (those trades have NO
  intents at all, so place_missing_stop wouldn't run for them
  even with the crash fixed).

## The change

### Part 1: Revert Phase E

```bash
git revert c8aec21 --no-edit
```

Migration 234 (added `crypto_broker_zero_qty_streak` column)
should remain — the column is additive, doesn't hurt anything,
and a forward migration to drop a column is more risky than
leaving it. Document in the revert commit message that mig 234
is intentionally retained.

`.env` disable flags can stay (idempotent if the code is gone)
OR be removed — operator's choice. Recommend keeping them for
forensic clarity ("Phase E was here").

### Part 2: Fix `bracket_writer.place_missing_stop` IndexError

**Step 1**: Find the IndexError site. Trigger a controlled crash
in chili_test by calling `place_missing_stop` directly with the
audit fingerprint (qty=3621, stop_price=0.25663137, ticker='ADA-USD',
trade_id=1808 — but a chili_test seeded copy). Capture the full
traceback from the exception.

**Step 2**: Patch the IndexError site. Likely culprits:
- `cost_bases[0]` without `if cost_bases:` check
- `executions[0]` similar
- A list-comprehension result indexed without bounds check

**Step 3**: Add a 5-min cooldown after ANY exception (not just
broker terminal-rejects). The current cooldown only fires on
known-retryable broker errors; code bugs hammer in a tight
loop. The cooldown should be:
```python
except Exception as e:
    logger.exception("[bracket_writer_g2] place_missing_stop crashed: %s", e)
    _set_reject_cooldown(intent_id, seconds=300, reason="exception_cooldown")
    raise  # for the audit event to record
```

**Step 4**: Tests:
- Reproduce the IndexError shape (mock the crashing branch to
  return empty list).
- Verify the patch returns a clean rejected-result with a
  meaningful error string instead of "list index out of range."
- Verify the 5-min cooldown is set.

## Acceptance criteria

1. Phase E feature commit (`c8aec21`) is reverted via
   `git revert`. Migration 234 retained.
2. The IndexError crash on `place_missing_stop` is patched with
   bounds-checked indexing.
3. A 5-min cooldown engages on any exception (not just broker
   rejects).
4. Tests for both:
   - `tests/test_bracket_writer_place_missing_stop_resilience.py`
     (or extend existing `tests/test_bracket_writer_g2.py`).
5. Live verification:
   - After deploy, watch ADA's `g2_place_missing_stop_submitting`
     events for 10 min. Either successfully places a stop OR
     fails with a meaningful broker-error reason (not "list index
     out of range").
   - Verify Phase E sweep is gone (`grep -r run_crypto_stale_trade_close
     app/` returns zero).
6. CC report at
   `docs/STRATEGY/CC_REPORTS/2026-05-08_f-phase-e-revert-and-bracket-writer-crash-fix.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/bracket_writer_g2.py:876+`
  (`place_missing_stop`). Patch the IndexError site only.
- Existing reject-cooldown infrastructure in `bracket_writer_g2`.
  Extend to fire on `except Exception` not just on broker-reject
  branch.
- Equity book (Phases A+B+C) untouched.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Don't touch the equity-side reconciler.**
- **Don't add Phase E logic back.** The revert removes the file;
  don't re-introduce the heuristic anywhere else.
- **Don't widen scope to the architectural rebuild.** That's a
  separate brief.
- **No magic numbers**: cooldown duration lifts from settings.
- **Edit-tool truncation discipline (HARD).** `bracket_writer_g2.py`
  is large.
- **Tests use `_test`-suffixed DB.**

## Out of scope

- Phase 1+ of the architectural rebuild
  (`f-crypto-reconcile-architectural-rebuild`).
- Anomaly 4 (crypto exit_monitor deterministic close).
- Anomaly 5 (`sync_pending_exit_order` wiring).
- Missing bracket_intents on 10 crypto trades.
- Auth-cache liveness (Anomaly 1).

## Sequencing

1. Truncation scan on `bracket_writer_g2.py`.
2. **Part 1**: `git revert c8aec21` with documented commit
   message. Verify imports don't break elsewhere.
3. **Part 2**: Reproduce IndexError; patch; add tests; verify.
4. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker`.
3. Watch ADA's bracket_writer activity for 10 min. Either a real
   stop gets placed (or a real broker rejection surfaces) — not
   another IndexError.
4. Optionally remove Phase E disable flags from `.env` (the code
   is gone, the flags are no-ops now).

## Rollback plan

`git revert` of this commit re-introduces both Phase E AND the
IndexError. Don't do it.

## What CC should do if it's unsure

1. **If the IndexError site is non-obvious**, surface in the CC
   report with the full traceback and propose a fix the operator
   can review before commit.
2. **If `git revert c8aec21` produces conflicts** (e.g., because
   later commits touched related files), surface and request
   guidance — don't force-resolve.
3. **If migration 234 retention causes ORM warnings** (e.g.,
   the column is on the model but the writer is gone), strip the
   ORM column reference too — but keep the DB column.
