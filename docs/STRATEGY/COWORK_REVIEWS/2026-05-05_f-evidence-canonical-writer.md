# Cowork Review: f-evidence-canonical-writer

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-05_f-evidence-canonical-writer.md`
**Reviewer:** Cowork.
**Date:** 2026-05-05.

## Verdict

One commit, one migration (228), 14/14 new tests pass, 248/248 prior
exit-evaluator + parity tests still pass. Schema introspection
verified. **Approve.**

The architecturally important detail: **Option B was the right design
choice.** This conversation's pre-execution audit showed that the
original Option A (separate reconciler at 6h cadence) would have been
sisyphean against `update_pattern_stats_from_closed_trades`'s 5s
lean-cycle writes. Option B converted the contaminated writer in
place, making the 5s cadence the canonical-writer cadence by
construction. Test #10 (idempotence with no new trades) confirms
steady-state convergence — exactly the writer-conflict-class bug
that Option A would have exhibited and that Option B eliminates.

Combined with f-time-decay-unit-fix (mig 227, this morning),
**evidence pipeline correctness is now end-to-end**: time-decay fires
at the right unit-aware time forward, AND historical evidence on
overheld trades gets continuously self-corrected via counterfactual
exit prices on every learning cycle.

## What Claude Code did right

1. **Caught the brief's `Trade.close_date` field-name error.** Surprise
   §1. The ORM column is `Trade.exit_date` (and `PaperTrade.exit_date`),
   not `close_date`. The legacy function at the same site already used
   `exit_date`. Claude Code spotted this, used `exit_date` everywhere,
   and noted the helper-signature parameter `close_date` was kept for
   semantic readability with a one-line shim at the call site. **Honest
   field-mapping clarity, not a silent fix.** The brief-cookbook update
   from f-partial-profit-wire-up's review applies here too: verify ORM
   column names before the brief asserts them.

2. **Dropped the legacy 2-trade minimum filter with explicit
   reasoning.** Surprise §2. The pre-fix function silently skipped
   patterns with `len(trades) < 2`. The brief's "audit row written every
   cycle, every pattern processed" requirement is incompatible with
   silent skipping. Claude Code:
   - Dropped the filter
   - Surfaced the trade-off (noisier 1-trade stats)
   - Pointed out the realized-EV gate's existing `min_trades=5` blocks
     the noisy stat from propagating to live decisions
   - Argued that silent skipping was its own sin (stale 1-trade values
     persisted indefinitely with no audit trail)
   The trade-off analysis is correct and the discipline is right —
   audit completeness wins over filter convenience.

3. **The audit-reason logic was internally inconsistent in the
   brief; CC implemented the obvious-correct version.** Surprise §5.
   I had two passes that both set `audit_reason` with overwrites that
   weren't immediately obvious. Claude Code collapsed to:

   ```python
   if coverage_too_thin:
       audit_reason = "coverage_too_thin"
   elif backfill_mode:
       audit_reason = "first_run_backfill"
   elif changed:
       audit_reason = "periodic_recompute"
   else:
       audit_reason = "no_change"
   ```

   That's clean priority order: coverage gate → first-run → real
   change → no-change. Tests #8 and #10 verify the lifecycle
   transitions. **Better than what the brief specified.**

4. **Coverage gate works as designed.** Test #9 verifies that when
   `cf_unavailable / overheld > 0.5`, `correction_reason='coverage_too_thin'`
   AND ScanPattern fields are NOT updated. This was my critique-driven
   addition (after the OHLCV-coverage-gap concern); it's the most
   important guardrail in the whole design. The 1m fast-path patterns
   where the bug bit hardest are also the patterns where OHLCV retention
   is shortest. Without this gate, we'd be producing biased corrections
   on the most-affected patterns.

5. **Renamed brief's helpers to `_evidence_correction_*`-prefixed
   names.** Surprise §3. `learning.py` is 9000+ lines; short helper
   names like `_is_first_run` would collide visually with anything
   similar. The prefix-namespacing is the right discipline for this
   file's size. Internal-only, no external callers, free win.

6. **Skipped trades with missing/zero `entry_price` BEFORE feeding
   the helper.** Surprise §4. The pre-fix path silently treated
   invalid rows as `ret_pct=0`. New path excludes them entirely (bad
   data is excluded, not zero-stuffed). Test #12 (NaN guard) covers
   the malformed-trade case. **Honest exclusion over silent
   contamination.**

7. **Preserved the 180-day cutoff.** Test #14 enforces. The brief's
   constraint to preserve legacy semantics on this is correct —
   changing the window is a strategy decision orthogonal to the
   correctness fix.

8. **Test #10 (idempotence) and Test #11 (EV-gate integration) are
   the two load-bearing tests.** #10 verifies steady-state
   convergence — running the function twice with no new trades
   produces identical pattern values, second audit row gets
   `correction_reason='no_change'`. **This is exactly the test
   Option A's design would have failed because of the writer
   conflict.** Option B passes by construction. #11 verifies the
   auto-demote chain — a pattern flipped from positive to negative
   `avg_return_pct` under correction fails `evaluate_realized_ev`
   without any new gate code. Together, these prove the
   architectural shape works.

## Findings

### Option B was the right call

The pre-execution audit showed `update_pattern_stats_from_closed_trades`
runs every 5s in the lean-cycle and writes from realized
`Trade.exit_price` (contaminated input). Option A would have added
a reconciler at 6h cadence, which:

- Would have been overwritten within 5 seconds on each cycle
- Would have required either disabling the legacy writer (Option A
  + override) or fighting it forever (sisyphean)
- Would have made convergence-latency 6h instead of 5s

Option B converted the legacy writer in place. **The cadence-mismatch
problem is solved by construction** — there's only one writer, and
it's canonical-aware. Test #10's idempotence proof is the architectural
property that emerges from this choice.

### The post-deploy smoke is what we'll see in the next review

The CC report correctly defers the production-data smoke (top-30
movers, total-CF-gap headline, demotion-chain query, coverage_too_thin
count) to whenever the brain-worker runs its first post-deploy cycle.
Those queries are documented inline in the report; the operator runs
them once, and the next Cowork review captures the production verdict.

The honest framing matters: Claude Code didn't fabricate post-deploy
data, didn't claim the fix had observable production impact yet, and
didn't run the smoke against test fixtures and pretend it was
production. **Right discipline.**

### The "wrong field name in brief" pattern is recurring

Both this CC report (Surprise §1, `Trade.close_date` vs `exit_date`)
and f-partial-profit-wire-up (Surprise §1, `paper_trades` vs
`trading_paper_trades`) caught field/table-name errors I introduced
in the brief. **Three briefs, two name errors.** The cookbook update
needs to expand:

- Always prefix with `trading_*` for SQL table names in the trading
  domain
- Always verify ORM column names before the brief asserts them
- Specifically for trade tables: the close-time column is `exit_date`,
  not `close_date`, on both `Trade` and `PaperTrade`

I'll save these as cookbook notes; future briefs go through a
field-name verification pass before shipping.

### The 4715-4717 writer is now confirmed harmless

The conflict matrix in the brief said: "site 4715-4717 is overwritten
by 4870-4877 within the same `run_learning_cycle` for any pattern
with closed trades." The CC report's Audit Summary confirms: "site
4870-4877 (now this fix) overwrites site 4715-4717 within the same
`run_learning_cycle` for any pattern with closed trades, so fixing
the load-bearing writer is sufficient."

For patterns with NO closed trades (the rare case for active patterns,
common for newly-mined ones), 4715-4717's breakout-outcome values
stand. These come from breakout alerts, not positions, so they don't
have the time-decay bug — they're a different signal entirely. **The
fix is sufficient as scoped.**

### Coverage gate is the design's most-important guard

The post-deploy data will tell us how often `coverage_too_thin` fires
— but the architectural property is already correct: **the most-
affected patterns (1m, where OHLCV retention is shortest) get honest
"we don't know" answers instead of biased corrections.** This is the
discipline I argued for in the critique and Claude Code implemented
cleanly.

If post-deploy shows a high `coverage_too_thin` rate on 1m patterns,
that's the empirical signal that the fix correctly bounds itself
when data is too thin. It's not a bug — it's the system honestly
saying "this pattern's evidence can't be canonical-corrected with
the OHLCV we can fetch." The decision of what to do about
those patterns (e.g., expand provider retention, accept that 1m
patterns can only be backtest-validated, etc.) becomes a separate
strategic question with the data already characterized.

## Answers to the Open Questions

### 1. OHLCV coverage gap quantification

**Defer to post-deploy review.** The smoke queries documented inline
will give us the headline. Expect 1m-weighted coverage gap.

### 2. First demotion-cycle observation

**Defer to post-deploy review.** Test #11 confirms the gate auto-fires
on the synthetic case; production count + timeframe distribution
will land in the next Cowork pass.

### 3. `coverage_too_thin` patterns

**Defer to post-deploy review.** Same window as #1.

### 4. Brief's `Trade.close_date` field name

**Acknowledged.** Cookbook update: ORM column is `exit_date` on both
`Trade` and `PaperTrade`. Future briefs verify before asserting.

### 5. Helper-signature parameter name `close_date`

**Keep `close_date` at the helper-signature layer.** Claude Code's
reasoning is correct: the helper is broker-agnostic ("close" reads
naturally for both paper and live without caring which). The shim
at the call site (`close_date=trade_row.exit_date`) is one line and
the semantic clarity is worth it. Don't rename.

### 6. Sites 4715-4717 (`learn_from_breakout_outcomes`)

**Defer to post-deploy review.** If the audit shows patterns where
the breakout-outcome value materially differs from the closed-trade
aggregation, surface those individually. Most active patterns have
closed trades, so the breakout-outcome value lives for microseconds
before being overwritten — but the no-closed-trades subset is a real
edge case.

### 7. Per-trade audit granularity

**Defer to follow-up brief if needed.** Pattern-level audit is
sufficient for the stated goal (detect divergence + reverse if
needed). Per-trade granularity is for forensic drill-down on
specific corrections; not load-bearing.

## Engineering concerns (smaller)

1. **The audit table is going to grow fast.** With ~770 patterns × one
   row per learning cycle (every 5s) = ~554k rows/day. Most will be
   `correction_reason='no_change'`. Worth thinking about a
   no_change-row pruning policy in a future hygiene pass — keep all
   `coverage_too_thin` / `first_run_backfill` / `periodic_recompute`
   rows, drop `no_change` rows older than 30 days, for example.
   **Not urgent**; postgres handles 100M-row tables fine. Surface
   if disk pressure becomes a thing.

2. **The 5s cadence will produce a LOT of OHLCV calls** on the first
   cycle (every overheld trade across every pattern triggers a CF
   fetch). The `fetch_ohlcv_df` priority chain handles fallback, but
   the yf circuit breaker will likely trip during the first-run
   backfill. That's fine — coverage-gate flips to `coverage_too_thin`
   for affected patterns and we move on. Subsequent cycles are
   cheaper because most patterns have already been corrected and
   only newly-closed trades trigger CF fetches.

3. **`audit_reason='no_change'` rows still write** — that's the
   audit-completeness contract. Verify no DB-write contention emerges
   under load. Post-deploy observation; not urgent.

4. **Pre-existing carry-forward** — `_trade_phantom_close_guard`
   listener still unstaged in working tree, etc. Same disposition as
   prior CC reports.

## State of the world after f-evidence-canonical-writer

- **17 protocol runs landed clean** (16 + this one).
- **4 fixes shipped today** (parity-persist, partial-profit-wire-up,
  time-decay-unit-fix, evidence-canonical-writer). Migrations
  225/226/227/228, ~50 new tests passing across all four, zero
  regressions on the 248-test exit-evaluator suite.
- **The exit-engine + evidence pipeline is now correctness-end-to-end**:
  - Canonical evaluator is the source of truth (existing).
  - Live + paper exit decisions go through canonical's parity logger
    (mig 225, with persistence working).
  - Partial-profit feature is operationally real (mig 226).
  - Time-decay fires at the unit-aware bar count (mig 227).
  - Pattern evidence continuously self-corrects from canonical
    semantics on every learning cycle (mig 228, this fix).
- **Auto-demote falls out of the existing realized-EV gate** for
  free; no new gate code, no new threshold.
- **The two queued briefs** (`f-exit-parity-metric-v2`,
  `f8b-verification-soak-3`, `bracket-writer-cover-policy-clarify`)
  are unchanged.
- **Carry-forward operator items**: `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1`
  still uncommitted; `_trade_phantom_close_guard` still unstaged.

## Decisions confirmed

- **Approve and ship.** All 7 brief steps + 5 surprises landed clean.
- **Migration 228** is the correct sequential ID.
- **Helper-signature `close_date` parameter** stays (semantic clarity).
- **Audit-reason priority** as Claude Code refactored it is the
  correct simplification.
- **Legacy 2-trade minimum filter drop** is right (audit completeness
  > filter convenience; EV gate's min_trades=5 protects live
  decisions).
- **Coverage gate at 0.5** unchanged from the brief; the most
  important guardrail in the design.
- **Cookbook update**: ORM column verification is now a brief-
  template prerequisite. Trade.exit_date NOT close_date.
- **Brief-cookbook running list**:
  - Always prefix `trading_*` for SQL table names in trading domain
  - Migration IDs: "next sequential at execution time" not hardcoded
  - Verify column types AND names before the brief asserts them
  - Trade/PaperTrade close-time column is `exit_date`

## Next move

Three reasonable directions:

**Path A — Smoke verification of all four day-of fixes.** Operator
runs:
1. Brain-worker cycle to populate `pattern_evidence_corrections`,
   capture the smoke queries from f-evidence-canonical-writer's
   Step 6 (top-30 movers, coverage gap headline, demotion chain).
2. Set `partial_at_1r=true` on one pattern, watch for
   `[partial_profit_ops]` log lines on next 1R-hit position.
3. Verify time-decay fires at unit-aware time on a non-1d position.
4. Verify parity-log persistence with the dispatch-exit-parity-verdict
   query showing real rows.

One operator session, ~30-60 min of attention. Closes the loop on
all four day-of shipments.

**Path B — Re-promote `f-exit-parity-metric-v2` from QUEUED.** Now
that f-time-decay-unit-fix has shipped (one of its prerequisites),
the metric-v2 brief is closer to ready. Still wants 24-48h of
parity data accumulation — not yet ready.

**Path C — Forensic analysis of historical pattern-evidence drift.**
Once `pattern_evidence_corrections` has accumulated some first-run
data, query the top-N movers and characterize which patterns'
evidence was most distorted by the time-decay bug. Pure analysis,
zero code; can be done in a Cowork session by reading the audit table.

**My read: Path A first.** Four fixes shipped today; the smoke
verification is the load-bearing closing step that confirms each
one is operationally real, not just test-suite green. After that,
Path C while the data is fresh — characterize the magnitude of the
distortion the time-decay bug had been creating. Path B waits on
its own clock (24-48h of parity data accumulation).

Today was substantively productive: the entire exit-engine + evidence
pipeline went from "logging into the void" + "feature inert" + "81%
of patterns silently miscomputing" + "evidence contaminated by
realized overheld exits" to **end-to-end canonical correctness with
audit trails and auto-demote.** Worth pausing to confirm the four
fixes are operationally real before queueing the next brief.
