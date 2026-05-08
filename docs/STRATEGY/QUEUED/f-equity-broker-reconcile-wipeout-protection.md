# f-equity-broker-reconcile-wipeout-protection

STATUS: QUEUED
SLUG: equity-broker-reconcile-wipeout-protection
PROPOSED: 2026-05-08
SEVERITY: medium-high (R32 covers the empty-list case but partial-list case is unverified; phantom-row recurrence is a silent loss-event class)

## TL;DR

`f-pdt-count-broker-confirmed-only` (shipped 2026-05-08, commit `60c26f8`)
filters phantom `broker_reconcile_position_gone` rows out of the PDT
count so the operator's account stops self-locking. That's the symptom
fix. **Phase B is the cause fix**: verify that R32 (commit `539e1c2`,
2026-04-30) actually prevents NEW phantom rows from forming in the
equity book under all the failure modes the wipeout-event class can
produce, not just the empty-`rh_tickers` mode it was scoped for.

The brief is structured as **audit-first, code-second**: the audit
decides what (if anything) needs new code. If R32 is already
structurally complete, this brief becomes documentation + observability
only.

## Why now

Operator audit 2026-05-08 surfaced 14 PDT-counted rows, all phantoms,
all from 2026-04-30. The famous "9 trades exited at exact same second
00:56:01 on 2026-04-30" is the well-known pre-R32 wipeout cascade
(R32 was committed at 04:08 UTC 2026-05-01, ~3.5h after the cascade).
**But 5 of the 14 rows have less-clear timing**, and we have no
verification that R32 covers the *partial-list* failure mode (broker
returns SOME positions but truncates others).

The operator's account just self-locked because of these 14 rows.
The just-shipped PDT count filter prevents the self-lock from
recurring **for these 14**, but a future wipeout-class event (or
even a single phantom row per week from partial-list cases) would
re-create the same problem. The structural fix closes that loop.

## Audit phase (BEFORE writing any code)

1. **Pull all `broker_reconcile_position_gone` rows for the equity
   book.** Group by `exit_date` truncated to 5-second buckets.
   For each bucket: count rows, list tickers, list `last_broker_sync`
   timestamps.
2. **Cross-reference with R32 deployment.** R32 landed at
   `2026-05-01T04:08:57Z` (commit `539e1c2`). Any phantom row with
   `exit_date >= 2026-05-01T04:08:57Z` is **not** explained by the
   "pre-R32 wipeout cascade" narrative — it indicates a gap in the
   current protection.
3. **For post-R32 phantom rows (if any):** pull the broker_sync logs
   for the 5-minute window around each row's `exit_date`. Look for:
   - `[broker_sync] R32 GUARD` warnings (would indicate the empty-
     list case fired and was correctly refused).
   - The size of `rh_tickers` at the time the row was closed.
   - Whether the closed ticker was the only missing one or part of a
     larger drop.
4. **Decide brief scope from audit findings:**
   - **Case A: zero post-R32 phantom rows.** R32 is structurally
     complete. Brief becomes observability-only (see "Always-on
     deliverables" below).
   - **Case B: post-R32 phantoms in the empty-list mode.** R32 is
     bypassed somehow. Need to find why and fix it.
   - **Case C: post-R32 phantoms in the partial-list mode.** R32's
     guard needs to be extended (see "Partial-list extension"
     below).
   - **Case D: post-R32 phantoms in a third mode** (e.g., a different
     code path that closes via `broker_reconcile_position_gone`
     without going through the R32-guarded `sync_positions_to_db`).
     Brief expands to identify and patch the third mode.

## Always-on deliverables (regardless of audit findings)

These ship even if the audit shows R32 is structurally complete:

1. **Reconcile-close observability.** Today the stale-close path
   (`broker_service.py:2202+`) closes trades with no metric or alert
   beyond a debug log. Add a structured metric:
   `reconcile_stale_close_fired_total{ticker,bucket}` plus a
   `[broker_sync] RECONCILE_CLOSE: ticker=... reason=...` warning
   line. Operator should be paged via the existing alert pipeline if
   ≥3 stale-closes fire in a 5-minute window (signals a wipeout-
   class event).
2. **Bucket-coalesced phantom alert.** When ≥3 rows close in the
   same 5-second bucket via `broker_reconcile_position_gone`,
   `breaker.trip()` with reason
   `wipeout_burst_3_in_5s`. The breaker's existing critical-loss
   logic prevents new entries; operator manually resets after
   investigation. (R31 already excludes synthetic reconcile losses
   from the consecutive-loss rule, so this trip is on the row-burst
   pattern, not on aggregate PnL.)
3. **Module-docstring + R31/R32/R-Phase-B linkage.** The current
   `broker_service.py` R32 docstring is good; extend it with the
   Phase B audit findings + deliverables. Future readers should be
   able to trace the full fix chain from one place.
4. **Test for R32's exact behaviour.** Today's tests likely don't
   cover the empty-`rh_tickers`-with-open-locals case. Add a
   regression test that pins the R32 guard's return shape
   (`skipped_reason='empty_broker_positions_with_open_local_trades'`)
   and asserts `Trade.status` stays `'open'` for all rows.

## Partial-list extension (only if audit shows Case C)

Today's R32 guard fires only when `rh_tickers` is **completely**
empty. The structural concern is partial truncation: broker returns
2 positions when there should be 5; the missing 3 still go through
the stale-close path because `Trade.ticker.notin_(rh_tickers)` is
True for them.

Mitigation options to weigh in audit phase:

A. **Confidence threshold on cycle-over-cycle delta.** If
   `len(rh_tickers)` drops by ≥50% (configurable) vs the previous
   cycle's snapshot, refuse mass-close and emit an alert. Mirror's
   R32's "default-to-safety" stance.

B. **Per-position confirmation window.** Today's
   `_RECONCILE_CONFIRM_WINDOW` is global (any position missing for
   <X seconds skips close). Extend to a per-position grace window
   that only counts as "missing" once it's been absent for N
   consecutive cycles (not N seconds). Robust against transient
   single-cycle drops without changing the single-cycle latency.

C. **Position-snapshot history table.** Persist each cycle's full
   `rh_tickers` set into a small `broker_position_snapshots` table
   so the partial-truncation check has a reliable comparator instead
   of relying on in-memory state. Heavier but more defensible.

The audit decides which (if any) of A/B/C applies. Don't pick
proactively.

## Acceptance criteria

1. Audit deliverable: a CC report section listing all phantom rows
   (post-R32 if any) with their exact timing, `rh_tickers` size at
   close-time, and which case (A/B/C/D) they fall into.
2. Always-on deliverables shipped:
   - Reconcile-close observability metric + warning log + breaker
     trip for ≥3-in-5s burst.
   - Module-docstring updated with the Phase B linkage.
   - R32 regression test pinning the empty-list guard.
3. **If audit Case C (partial-list)**: one of options A/B/C above
   shipped with full test coverage; the chosen option is the one
   the audit data supports, NOT a default pick.
4. **If audit Case A (R32 complete)**: brief is closed at observability;
   no new code on the reconcile path.
5. CC report at `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-equity-broker-reconcile-wipeout-protection.md`
   documents the audit findings, the deliverables shipped, and any
   carry-over briefs proposed (e.g., separate audit if Case D
   surfaces a third code path).

## Brain integration (reuse, don't rewrite)

- `app/services/broker_service.py:2109-2150` — R32 empty-list guard.
  Phase B extends or documents; do NOT rewrite the existing block.
- `app/services/broker_service.py:2171-2280` (approximate; see
  `for trade in stale:` loop) — the stale-close path. Phase B adds
  observability to this path; the close logic itself stays.
- `app/services/trading/portfolio_risk.py:1016` already excludes
  `broker_reconcile_position_gone` from the consecutive-loss rule
  (R31). Don't touch.
- The breaker's `trip()` method already exists for kill-switch use;
  Phase B reuses it for the wipeout-burst signal.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Hard Rule 3**: data-first. If audit Case D surfaces, fix the DB
  + add a migration; don't paper over with a router/service filter.
- **Edit-tool truncation discipline (HARD).** `broker_service.py`
  is large (>2300 lines). Splice pattern only. `wc -l + ast.parse`
  post-edit verification mandatory. See memory
  `reference_2026_05_07_widespread_truncation.md`.
- **Tests use `_test`-suffixed DB.**
- **Audit-first.** Do NOT pre-write the partial-list extension. The
  audit data decides.

## Out of scope

- Crypto reconciler. Same code path served crypto until R32; if
  there are crypto phantom rows post-R32, surface as a separate
  brief (`f-crypto-broker-reconcile-wipeout-protection`).
- Options reconciler. The MMM filter (`broker_service.py:2189-2200`)
  already excludes options from the stale-close path. If the audit
  surfaces options-side phantoms, separate brief.
- The PDT count itself. `f-pdt-count-broker-confirmed-only` is the
  durable filter; don't re-touch it.
- Backfilling old phantom rows. They're forensically valuable as-is.
- Pattern-quality demotion (`f-pattern-demote-on-thin-evidence`).

## Sequencing

1. Audit query + categorization. Capture in CC report's "Audit"
   section.
2. Always-on deliverables (regardless of audit findings).
3. Conditional: partial-list extension only if Case C surfaces.
4. Tests for everything shipped.
5. CC report + commit + push.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili broker-sync-worker
   autotrader-worker`.
3. Verify:
   - `[broker_sync] RECONCILE_CLOSE` warnings appear (or not) in
     normal operation.
   - The R32 regression test passes.
4. Wait 24h; confirm zero new phantom rows accumulate.
5. If audit surfaced post-R32 phantoms: confirm the chosen
   partial-list mitigation prevented at least one new phantom in
   the 24h soak.

## Rollback plan

`git revert` the commit. Always-on deliverables are additive
(metrics + warnings + tests + docstring). Partial-list extensions
are gated by a settings flag
(`CHILI_RECONCILE_PARTIAL_LIST_GUARD_ENABLED`) defaulting to ON;
flip OFF to revert just the new guard while keeping the
observability changes.

## Open questions (to be answered by audit)

1. **Are there post-R32 phantom rows?** Rephrased above as Case A/B/C/D.
2. **What's the cadence of `sync_positions_to_db`?** If it's
   frequent enough that a single transient broker hiccup gets
   amplified (e.g., one bad cycle out of every 10), the
   per-position confirmation window (option B) is more robust than
   the cycle-over-cycle delta (option A).
3. **Is there a separate code path that emits
   `broker_reconcile_position_gone` outside `broker_service.py`?**
   Grep already shows the writer is only at `broker_service.py:2247`.
   Confirm in audit; if so, that's Case D.
4. **What's the operator's tolerance for false-positive paging
   alerts?** A "≥3 closes in 5s" trigger could fire on legitimate
   take-profit cascades if the strategy ever does coordinated
   exits. Audit checks: are there any legitimate same-second
   multi-exit events in the last 30d? If so, narrow the trigger.
