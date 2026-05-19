# NEXT_TASK: f-position-identity-phase-4-flag-flip-paper-soak

STATUS: DONE

## Outcome (2026-05-19)

Phase 4 flag flipped + soak completed cleanly.

**Step 1 audit:**
- `sells_recorded` = 450 (mig 254 synthetic + future live) ✓
- `positions_with_sell` = 112 distinct positions ✓
- `schema_version` tip = `254_synthetic_exit_fill_events` ✓

**Step 2 flip + restart:**
- `.env` updated with `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true` (using ASCII-safe `[System.IO.File]::WriteAllBytes` per `feedback_never_powershell_outfile_env`)
- `docker compose up -d --force-recreate broker-sync-worker` clean
- Worker recreated, scheduler started, 8 jobs registered, zero tracebacks

**Step 3 soak (~6 min observed window):**
- Zero `INVERSE RECONCILE [phase4_*]` log lines (RH session is dead so `sync_positions_to_db` doesn't enter the inverse-reconcile branch — expected and benign)
- Zero `CONTRADICTION` log lines
- Zero Phase 4 related tracebacks / crashes
- Bracket reconciliation sweep + crypto stop-loss monitor both ran cleanly

**Step 4 PROMOTE.** Flag stays on. Integration verified; data plane complete; reader plane live. Decision recorded in `docs/STRATEGY/COWORK_DECISIONS_LOG.md`.

CC report: `docs/STRATEGY/CC_REPORTS/2026-05-19_f-position-identity-phase-4-flag-flip-paper-soak.md`

## Next strategic priority

Position-identity refactor is **operationally complete** (Phases 1-4 all shipped and in production). The natural next moves:

1. **`f-coinbase-exit-side-recording`** — Phase 4 covers Robinhood `sync_positions_to_db` only. Coinbase has its own sync path that doesn't share the inverse-reconcile code. If Coinbase positions ever hit the "broker alive, locally closed" edge case, the legacy event_count check still runs. Brief queued.
2. **`f-bracket-fired-stop-recording`** — Broker-fired stop fills aren't yet written as live sell events. Mig 254 backfills historical, but new bracket fires would be missed. Brief queued.
3. **Phase 5 envelope-rename + decision-layer split** — the next big position-identity refactor step per design doc § 6.2. Wait for Phase 4 to be exercised in real conditions (i.e., RH session restored + at least one inverse-reconcile firing observed) before starting.

Operator promotes whichever of the above is highest priority.
