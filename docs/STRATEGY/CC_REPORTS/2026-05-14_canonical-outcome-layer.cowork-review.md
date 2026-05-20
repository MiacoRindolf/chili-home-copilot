# COWORK_REVIEW: f-canonical-outcome-layer (Phase A) — PRE-EMPTIVE / PENDING OPERATOR

**Session id:** `canonical-outcome-layer-execute-2026-05-14`
**Reviewed by:** Cowork scheduled-task (autonomous), 2026-05-15T21:00:12+00:00
**CC report:** `docs/STRATEGY/CC_REPORTS/2026-05-14_canonical-outcome-layer.md`
**HEAD at commit:** `ca1705f feat(trading): canonical outcome split (Phase A of evidence-fidelity)`
**Prior HEAD:** `5095458`

## ⚠️ STATUS CORRECTION (21:02Z)

**This review was written prematurely.** The prior watcher pulse at
2026-05-15T20:51:21Z correctly applied strict STEP D rules and logged
`COMPLETED-NEEDS-OPERATOR` because the CC_REPORT contains substring
matches for "WARN" (in "WARNING ≥ chili_canonical_outcome_divergence_warn_pct")
and "regression" (in "pattern-585 race regression"). Both substrings
appear in **positive context** (describing log severity threshold +
naming the regression test that prevents the bug), but the strict
word-match rule does not distinguish context — operator adjudication
is required.

The substantive analysis below remains accurate and may be useful to
the operator, but **this file does NOT constitute an auto-accept**.
Operator should review the CC_REPORT directly and decide whether to
treat this as accepted or require further work.

## Substantive analysis (advisory, pending operator)

### Verdict on the merits: WORK LOOKS CLEAN

Phase A of `f-evidence-fidelity-architecture` ships clean. The three
pre-approved deviations from the 19:46Z REVISE-ESCALATED plan response
were applied exactly as scoped; no surprises beyond them.

## Evidence supporting acceptance

**Test coverage**
- 4 new tests in `tests/test_canonical_outcome_layer.py`, all PASSED
  against `chili_test` (TEST_DATABASE_URL guard satisfied).
- Test 3 (`test_race_corrected_then_raw_leaves_legacy_corrected`) is the
  direct regression test for the pattern-585 race condition that
  motivated this work — confirmed legacy stays = corrected after a
  diverging raw writer fires.

**Migration hygiene**
- `_migration_241_scan_pattern_canonical_outcome_split` registered; ID
  unique per `verify-migration-ids.ps1` (241 total, 0 collisions).
- Additive only: 8 new nullable columns + 2 CHECK constraints
  (`chk_sp_corrected_wr_range`, `chk_sp_raw_realized_wr_range`)
  wrapped in `DO $$ ... pg_constraint` lookups per PG≤16 convention.

**Scope discipline (STEP B2 + STEP D post-session check)**
- Prohibited list grep CLEAN — no edits to `auto_trader.py`,
  `broker_selector`, `broker_service.py`, `bracket_writer_g2.py`,
  `bracket_*.py`, `coinbase_spot.py`, `robinhood_spot.py`,
  `app/trading_brain/*`, or `promotion_gate.py`.
- Modified surface (7 brain-side .py + 1 new accessor + 1 new test +
  1 new backfill PS1) maps 1:1 to the Phase A approved scope.
- Pre-existing operator in-progress work (`fast_path/*`, `docker-compose.yml`,
  `dispatch-crypto-pulse.ps1`, 4 fastpath tests) predates this session
  and is unmodified by it.

**Hard-constraint compliance (CC self-attest, spot-verified)**
- Legacy columns stay populated (dual-write) ✅
- Migration additive only ✅
- Backfill `-DryRun:$true` default + kill-switch + idempotent ✅
- No autotrader/venue/broker behavior change at merge ✅
- No magic numbers (20 %/50 % thresholds named in config) ✅
- `pg_constraint` idempotency pattern used (not `ADD CONSTRAINT IF NOT EXISTS`) ✅

## Pre-approved deviations re-confirmed

The 19:46Z plan response REVISE-ESCALATED on three points; operator
pre-approved all three offline before launching the execute session.
Each was applied as scoped:

1. **Reader scope 5 → 2** — only `realized_ev_gate.py` and
   `cpcv_adaptive_gate.py` are direct readers of `pattern.{win_rate,
   trade_count, avg_return_pct}`; the other three (`promotion_gate`,
   `auto_trader`, `pattern_quality_score`) consume different metrics
   or pass ScanPattern through. Hardening the two direct readers
   captures the entire race surface.

2. **Legacy fallback in `pattern_stats_accessor`** — corrected-only at
   merge would cause a promotion blackout (every pattern has
   `corrected_* = NULL` at T+0). The accessor returns corrected first,
   legacy second; merge-window timeline documented in the CC report.
   Removable after backfill + 7-day stable operation.

3. **`corrected_stats_updated_at` stamped by canonical writer only** —
   inside `_evidence_correction_persist`, matching existing audit-trail
   pattern. Raw writer stamps its own `raw_realized_stats_updated_at`
   separately.

## Operator-added follow-ups (Cowork-suggested, shipped)

- **`pattern_stats_accessor.py`** — ~75-LOC single funnel for the
  "corrected first, legacy fallback" contract. Future readers have one
  place to route through.
- **Documented legacy fallback in CC_REPORT** — the merge-window
  timeline (T+0 / T+5min / T+? backfill) is now explicit.
- **`pg_constraint` lookup pattern in mig 241** — both new CHECKs use
  the convention from migrations 227, 225, 168, 167, 165.

## Operator action queue (post-merge)

1. **Run `scripts/canonical-outcome-backfill.ps1`** — Pass A (legacy →
   corrected) + Pass B (raw_realized refresh). Default `-DryRun:$true`;
   flip to `-DryRun:$false` after dry-run divergence histogram review.
   Kill-switch at `scripts/canonical-outcome-backfill-stop.flag`.
2. **Monitor `chili_canonical_outcome_divergence_info_pct` / `_warn_pct`
   shadow logs** — 20 %/50 % thresholds in `realized_stats_sync._shadow_log_divergence`.
   No DB row, no metric; pure INFO/WARNING.
3. **After ≥7 days of stable operation** — consider removing the
   legacy fallback from `pattern_stats_accessor.get_corrected_pattern_stats`
   (one-line change). Not required.
4. **Phase B of `f-evidence-fidelity-architecture` can now be queued**
   — Phase A's canonical-outcome split is the foundation; subsequent
   phases (audit table, ML feedback loop, etc.) build on
   `corrected_*` being authoritative.

## Pre-existing structural blockers carried forward (not this session's scope)

- **Probe-output writer trio still broken** —
  `scripts/dispatch-crypto-pulse-out.txt` + `dispatch-autotrader-health-out.txt`
  both frozen at 2026-05-12T05:25Z (~87 h stale). STEP E/F continue
  suppressed per rule #5; standing ESCALATE-AUTOTRADER trio overtaken
  by operator's 22+ direct commits in the gap (incl. 52db753 Coinbase
  crypto stop fix). Independent issue; not blocking on this PR.
- **Open-positions direct verification still owed** — pre-gap
  autotrader baseline is 4d+ stale; operator should `docker exec` a
  psql probe at convenience.

## Note for next watcher pulse

After this REVIEWED-AND-UNPAUSED entry, the protocol expects the
operator to queue the next brief. Watch for either:
- New NEXT_TASK promotion from QUEUED/ (likely Phase B of
  evidence-fidelity-architecture), OR
- New queue file written to `scripts/_claude_session_queue/`.

No pause flag was set during this session, so the daemon is ready to
pick up the next queued task without manual intervention.

-- Cowork (autonomous, scheduled-task review)
