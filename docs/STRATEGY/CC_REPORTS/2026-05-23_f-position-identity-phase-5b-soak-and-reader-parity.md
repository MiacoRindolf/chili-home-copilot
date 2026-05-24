# CC_REPORT: f-position-identity-phase-5b-soak-and-reader-parity

Date: 2026-05-23

## What shipped

Read-only soak inspection of the Phase 5B semantic layer against the live
`chili` database. Three SQL probes plus triangulation row counts. No code
changes. No migrations. No live trading behavior changes.

- Plan recorded at
  `scripts/_claude_session_consult/f-position-identity-phase-5b-soak-and-reader-parity/plan.request.md`
  with `proceed_without_review=true` (brief was self-contained; plan-gate
  consult skipped per the operator's instruction).
- Probe results captured below.

## Verification — three probes (all GREEN on the live linkage path)

### Probe A — `trading_phase5a_envelope_parity` (snapshot at 2026-05-23 18:39:46)

| Column                                | Value | Status |
|---------------------------------------|-------|--------|
| `trade_rows`                          | 688   | —      |
| `trades_with_decision`                | 621   | —      |
| `trades_missing_decision`             | 67    | expected — corrupt legacy dust rows (`entry_price <= 0 OR quantity <= 0`); intentionally excluded from the decision bridge |
| `broker_trades_with_position`         | 515   | —      |
| `broker_trades_missing_position`      | 173   | expected — 67 dust + 106 historical closed envelopes |
| `open_broker_trades_missing_position` | **0** | GREEN  |
| `orphan_decisions`                    | **0** | GREEN  |

Helper-equivalent filtered counts (matches `management_envelopes.phase5b_parity_summary`):

| Counter                                | Value | Status |
|----------------------------------------|-------|--------|
| `valid_trades_missing_decision`        | **0** | GREEN  |
| `corrupt_dust_rows`                    | 67    | expected legacy debt |
| `open_envelopes`                       | 7     | normal live state |
| `open_broker_trades_missing_position`  | **0** | GREEN  |

### Probe B — `trading_phase5b_decision_envelope_position` linkage status

| `linkage_status`                                | count |
|-------------------------------------------------|-------|
| `linked`                                        | 515   |
| `historical_broker_envelope_missing_position`   | 106   |
| `decision_without_envelope`                     | **0** |
| `broker_envelope_missing_position`              | **0** |
| `open_position_envelope_mismatch`               | **0** |

All three hard live linkage buckets are zero. The 106 historical-debt rows
are closed envelopes from before the position layer existed; the brief
explicitly accepts these as nonzero.

### Probe C — `trading_phase5b_pattern_decision_performance` top 20 by `total_pnl`

| pid  | decisions | envelopes | closed | open | total_pnl  | avg_entry_slip_bps | linkage_issues | hist_debt |
|------|-----------|-----------|--------|------|------------|--------------------|----------------|-----------|
| 585  | 87        | 87        | 87     | 0    | **521.11** | 26.35              | 0              | 0         |
| 537  | 23        | 23        | 12     | 1    | 81.28      | 78.65              | 0              | 0         |
| 586  | 30        | 30        | 30     | 0    | 57.96      | 314.16             | 0              | 0         |
| 1052 | 32        | 32        | 26     | 0    | 57.42      | -101.89            | 0              | 0         |
| 1246 | 15        | 15        | 7      | 0    | 27.50      | 0.00               | 0              | 4         |
| 1065 | 15        | 15        | 15     | 0    | 12.82      | 43.42              | 0              | 0         |
| 1242 | 8         | 8         | 8      | 0    | 7.81       | -4.61              | 0              | 0         |
| 1215 | 3         | 3         | 3      | 0    | 1.39       | 425.75             | 0              | 0         |

(Full top-20 in the probe output; remainder are zero-PnL open or
small-negative rows. Every row has `linkage_issues = 0`.)

Pattern 585 holds at +$521.11 with all 87 envelopes closed — matches the
2026-05-22 baseline from the prior CC report exactly. Pattern 537 moved
$82.09 → $81.28 with one additional closed envelope (12 closed vs. 11 the
day before), confirming the close path is also exercising the Phase 5B
read model on new outcomes.

## Triangulation row counts

| Table / view                       | rows | most recent `entry_date` / `updated_at` |
|------------------------------------|------|-----------------------------------------|
| `trading_decisions`                | 621  | 2026-05-23 15:11:20                     |
| `trading_positions`                | 210  | 2026-05-23 18:30:02 (`updated_at`)      |
| `trading_trades`                   | 688  | 2026-05-23 15:11:20                     |
| `trading_management_envelopes` (view) | 688 | 2026-05-23 15:11:20                  |

The most-recent `entry_date` matches between `trading_decisions` and
`trading_trades` — the Phase 5A future-insert trigger is still locking the
two together on new entries. The 688 − 621 = 67 row gap matches the
`trades_missing_decision` count exactly: only the corrupt legacy dust
rows are excluded from the decision bridge.

All four expected schema objects regclass-resolve cleanly:
`trading_management_envelopes`, `trading_phase5a_envelope_parity`,
`trading_phase5b_decision_envelope_position`,
`trading_phase5b_pattern_decision_performance`.

Last 7 days of decision activity (proves the bridge stays live):

| entry_day  | new_decisions |
|------------|---------------|
| 2026-05-23 | 3             |
| 2026-05-22 | 6             |
| 2026-05-21 | 4             |
| 2026-05-20 | 18            |
| 2026-05-19 | 22            |
| 2026-05-18 | 4             |
| 2026-05-17 | 2             |

## "Boring soak" interpretation

Phase 5B is reading as boring against the brief's green-state definitions:

- `valid_trades_missing_decision = 0` ✓
- `open_broker_trades_missing_position = 0` ✓
- `orphan_decisions = 0` ✓
- Phase 5B hard linkage issues = 0 ✓ (only `linked` + historical debt)
- `historical_broker_envelope_missing_position = 106` — accepted by the
  brief; needs to be watched for growth, not absolute value

Today's data also satisfies the spirit of the Phase 5C re-promotion criteria
in the brief:

- 3 fresh entries today (2026-05-23 15:11:20) flowed through the
  decision-future-insert trigger and joined cleanly to envelopes.
- Pattern 537 had at least one envelope close between yesterday's snapshot
  and today's, and its decision/envelope/position join still surfaces a
  consistent total_pnl with no linkage_issues.

Recommendation: extend the soak by 2–3 more daily probe runs before
promoting to Phase 5C. The current single-day delta (one close, three new
entries) is consistent with green; another two days of consistent zeros on
the hard counters plus visible fresh-entry/close traffic gives Cowork a
defensible "boring through multiple cycles" baseline before the first
reporting reader is pointed at `management_envelopes.py`.

## Surprises / deviations

- None operationally. The probes were uneventful, which is the desired
  outcome for a soak.
- One small numeric drift to flag: pattern 537's `total_pnl` moved from
  $82.09 (CC report 2026-05-22) to $81.28 (today). The closed-envelope
  count rose from 11 to 12 over the same window, so the delta is exactly
  one new closed envelope contributing ~−$0.81. This is normal soak
  behavior, not a regression.

## Deferred

- No physical rename of `trading_trades`. Brief explicitly defers this
  until at least one reader is migrated and the parity comparison is
  stable.
- No reader migration yet. Brief defines Phase 5C as that step, gated on
  "boring through multiple fresh entries and at least one close." See the
  recommendation above on extending the soak window.
- The 67 corrupt legacy dust rows in `trading_trades` (entry_price ≤ 0 or
  quantity ≤ 0) remain unbridged by design. If Cowork ever wants to retire
  them from reports, that is its own data-hygiene task — flagging here
  only because the row-count gap (688 − 621) will keep surfacing in every
  future probe until they're purged or filtered at the reader.

## Open questions for Cowork

1. **Soak duration before Phase 5C.** Brief says "multiple fresh entries
   and at least one close." Today's snapshot meets that literally. Should
   Phase 5C be queued for the next CC session, or do you want N more daily
   probes first to build confidence?
2. **First Phase 5C reader candidate.** When Phase 5C ships, which existing
   reporting query should we migrate first to
   `management_envelopes.pattern_decision_performance`? Brief is silent on
   the specific entry point; suggestion is to start with whichever
   dashboard/script today does the equivalent of "top-N patterns by total
   PnL" against `trading_trades`, since that is the closest match to the
   new helper's shape.
3. **Historical debt cleanup.** `historical_broker_envelope_missing_position`
   is 106 today. If this count keeps drifting up (would indicate live
   broker envelopes aren't getting position links on close), that's a
   regression. Suggest setting a soft alert threshold — e.g., daily probe
   flags if this count grows more than +10 between days. No action needed
   today; flagging as a watch item.
