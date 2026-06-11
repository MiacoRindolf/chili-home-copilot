# 2026-06-10 — Phase 5B hard linkage: first-fill envelope→position backlink (mig 304)

## Symptom

Both phase-5 soak probes sat at a standing alert (invisible until PR #586 fixed
the dispatch wrapper that died before writing verdicts):

```
VERDICT_STATUS=BLOCKED_LINKAGE
VERDICT_REASON=2 hard linkage issue(s) in Phase 5B view
HARD_LINKAGE_ISSUES=2
```

All other gate fields were healthy (0 fresh-close mismatches, 0 attribution
drift rows, $0.0000 drift).

## The 2 failing rows

| envelope | ticker | entered (UTC) | filled | status | position row | created |
|---|---|---|---|---|---|---|
| 2285 | BFLY | 2026-06-05 13:47:51 | 13:48:02, broker_status=filled | open | 332 (exact qty/price match) | 13:52:00 |
| 2286 | IYH | 2026-06-05 13:49:51 | 13:50:03, broker_status=filled | open | 331 (exact qty/price match) | 13:52:00 |

Both real, filled Robinhood equity longs (autotrader v1 lane), still open and
actively broker-synced. `linkage_status='broker_envelope_missing_position'`
because `envelope.position_id` was NULL — even though a perfectly matching
`trading_positions` row existed. The positions' `current_envelope_id` was NULL
too: unlinked on **both** sides.

## Root cause — first-fill race, no backlink path

- The Phase 5A AFTER INSERT trigger on the envelope table (mig 257) links
  `position_id` **only when a matching `trading_positions` row already exists
  at envelope-insert time**.
- For the FIRST fill ever in a natural key (user, broker, ticker, direction)
  no position row exists yet — the broker position observer creates it
  minutes later (here: ~4 min, on the 2-min broker-sync cadence).
- Nothing back-links afterwards: `bracket_intent_writer` / `execution_audit`
  resolve position_id only for their own tables, and
  `position_integrity.repair_current_envelope_links` (which would at least fix
  the position-side pointer) has **no callers** — dormant library code.
- Every other fresh envelope linked fine because crypto/repeat-ticker entries
  find an existing (open-or-reopened) position row at insert time. The gap
  only manifests on first-ever entries — which is why it appeared exactly when
  the equity lane started filling for real.

## Fix — mig `304_position_identity_phase5i_position_insert_backlink`

Same in-database style as Phase 5A ("every writer path participates"):

1. **Inverse trigger** `trg_trading_positions_phase5i_backlink_after_insert`
   (AFTER INSERT ON `trading_positions`): back-links open, unlinked,
   non-option broker envelopes matching the new position's natural key, then
   fills the position's `current_envelope_id` when exactly one open envelope
   resolves. Never raises (position observation inserts must not fail on
   linkage hygiene; failures degrade to a WARNING).
2. **Idempotent backfill** with an exactly-one-match ambiguity guard for the
   existing orphans. Closed envelopes with NULL position_id (217 rows) stay
   untouched — that is the accepted `historical_broker_envelope_missing_position`
   debt class the probes already tolerate.

Option envelopes are excluded (mirrors `position_resolver`; see mig 280
detach — a false link to the underlying equity row is worse than no link).

**Authority note (Hard Rule 5 flag):** no conflict in substance — the change
fills NULL FK linkage only. `trading_decisions` rows, the prediction-mirror
authority contract, and all log formats are untouched. Broker remains
authoritative for fills.

## Verification

- Backfill linked exactly the scoped 2 envelopes + filled 2 pointers
  (`[mig304] ... backfill linked 2 envelope(s) ... filled 2 current_envelope_id pointer(s)`).
- Re-run of the migration body: 0/0 — idempotent.
- Both rows now `linkage_status='linked'` bidirectionally; view hard issues = 0.
- `scripts\d-phase5i-post-rename-soak-probe.py` → `COMPLETE_POSITIVE`
  (decisions=140, envelopes=140, closes=121), exit 0.
- `scripts\d-phase5e-reporting-soak-probe.py` → `READY_FOR_RENAME_BRIEF`
  (decisions=143, envelopes=143, closes=129), exit 0.
- Scheduled tasks `CHILI-phase5i-post-rename-soak-probe` and
  `CHILI-phase5e-reporting-soak-probe`: Last Run Result = **0**.
  (One transient 3 when both tasks were force-started in the same second —
  `conda run` temp-file collision, unrelated to the fix; solo re-run clean.)
- Tests: `tests/test_position_identity_phase5i_backlink.py` (new, 5 tests) +
  phase5a/5b/5h suites — 21 passed.
- `scripts\verify-migration-ids.ps1` — PASS (294 migrations, no collisions).

## Deploy note

The trigger + backfill live in the database and are already active — no
container redeploy is required for the fix itself. Containers pick up the
migrations.py change (a no-op against the live DB, since 304 is recorded in
`schema_version`) on their next per-git-sha image build.

## Follow-up

- Phase 5I soak gate now reads COMPLETE_POSITIVE with the compare clean —
  Phase 5I closeout + Phase 5J selective reader cleanup can be queued
  (Cowork's call per `docs/STRATEGY/NEXT_TASK.md`).
- `position_integrity.repair_current_envelope_links` remains dormant
  (no callers); the new trigger covers its open-position case at insert time.
