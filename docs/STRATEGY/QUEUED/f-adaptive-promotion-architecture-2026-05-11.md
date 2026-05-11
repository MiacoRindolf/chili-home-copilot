# f-adaptive-promotion-architecture (2026-05-11)

> **Type:** Architect proposal (READ-ONLY analysis + multi-phase implementation plan)
> **Priority:** P0 — the brain has gone from ~31 promoted patterns (pre-2026-04-27 audit) down to 3, and only one of them (585) is the actual workhorse. Live alert volume is concentrated on a single pattern. Promotion drought is the binding constraint on autonomous trading.
> **Scope:** Diagnose the drought, redesign the CPCV promotion gate with dynamic / data-driven thresholds, activate composite quality scoring as an event-driven node, and fix the backtest pipeline staleness the UI runtime tab is surfacing.
> **Status:** QUEUED — operator review pending.

## TL;DR

**The drought is not a threshold problem.** I went into this expecting to tune
CPCV thresholds (DSR ≥ 0.95, PBO ≤ 0.2, median_sharpe ≥ 0.5). Empirical probe
of the 39 patterns that *do* have CPCV data shows DSR pegged at 1.000 and PBO
pegged at 0.000 across every percentile — these thresholds are not gating
anyone. The real bottleneck is upstream: of 586 active patterns, **547 have
NULL `cpcv_n_paths`** — the CPCV gate has never produced a verdict for them.

The proposal is therefore in two pieces:

1. **Restart the funnel.** Backfill cpcv_gate evaluations for the 314 patterns
   that have ≥ 30 PTR rows (the gate's existing minimum), and identify why the
   handler is not firing on `backtest_completed` for them today.
2. **Replace the hardcoded gate thresholds with data-driven, sample-size-aware
   ones** that adapt to the empirical distribution of the active pattern pool
   and never veto on a single magic number. Wire **composite quality score** as
   an event-driven node that updates on `pattern_stats_updated` /
   `backtest_completed` rather than a once-per-cycle batch.

The runtime-tab UI anomaly ("patterns with trades but no recent backtest") is
the same root cause: PTR rows fall into `trading_pattern_trades` via mining
backtests, but `scan_patterns.oos_evaluated_at` only gets set when the CPCV
gate handler completes — and the handler isn't completing for those patterns.

---

## Evidence (probes 1 through 6)

Probe artifacts (committed alongside this brief):
- `scripts/dispatch-promotion-drought-probe-out.txt`
- `scripts/dispatch-drought-probe-2-out.txt`
- `scripts/dispatch-drought-probe-3-out.txt`
- `scripts/dispatch-drought-probe-4-out.txt`
- `scripts/dispatch-drought-probe-6-out.txt`

### E1. Lifecycle distribution

```
candidate    639    (511 active)   ← ~80% stuck here
backtested    61    ( 50 active)
challenged    40    ( 19 active)
retired       23    (  0 active)
decayed        3    (  3 active)
promoted       3    (  3 active)   ← the entire live roster
```

### E2. The promoted-pattern roster

| id   | name                                             | trades | PTR rows | n_paths | dsr   | pbo   | med_sh |
|------|--------------------------------------------------|--------|----------|---------|-------|-------|--------|
|  585 | Intraday Squeeze + Declining Volume (drop-bb)    |     11 |      368 |      35 | 1.000 | 0.000 |  1.405 |
| 1011 | Reddit IBS (vaanam-dev) [No-BOS-breakout]        |    409 |     1122 |      84 | 1.000 | 0.000 |  1.990 |
| 1016 | Reddit IBS (vaanam-dev) [entry-add-bb_pct]       |    565 |     2024 |     105 | 1.000 | 0.000 |  1.429 |

Pattern 585 fires 1293 of 1294 `pattern_breakout_imminent` alerts in the last 7
days. 1011 and 1016 fire none — they are mean-reversion patterns and the
`pattern_breakout_imminent` producer is breakout-specific. So in practice the
brain's effective entry signal funnel has **one** active pattern.

### E3. CPCV gate population — 547 of 586 patterns have NO CPCV verdict

PTR row counts per pattern (the CPCV gate consumes
`trading_pattern_trades.outcome_return_pct`):

```
zero                  0   patterns (n_pat with PTR data: 341)
1-4                   4
5-14                 12
15-29 (below gate)   12
30-99 (gate OK)      27
100+                287   ← gate would accept these
```

**314 patterns have ≥ 30 PTR rows.** Only **39** have any CPCV verdict
persisted. The gate should have processed 314 patterns. It has processed 39
(13%). 275 patterns are silently waiting.

### E4. The CPCV metrics for those 39 are pathological

```
metric              min      p25     p50     p75      max
cpcv_n_paths        8.00    20.00   56.00   105.00   5050.00
deflated_sharpe     0.000    1.000   1.000    1.000    1.000  ← pegged
pbo                 0.000    0.000   0.000    0.000    0.000  ← pegged
cpcv_median_sharpe -2.525    1.103   3.906    6.055   45.126  ← wide
```

DSR pegged at 1.000 means either (a) the patterns truly look like statistical
gold, or (b) the computation collapses to a sentinel ceiling. Either way the
hardcoded `dsr >= 0.95` threshold has zero discriminatory power — it admits
everything. PBO pegged at 0.000 same story for `pbo <= 0.2`. The only gate
metric doing actual work is `cpcv_n_paths >= 20` (path-count minimum).

This is consistent with the operator's "no magic numbers" framing: the magic
numbers aren't even doing the gating they pretend to do. The system has been
gating on **path count alone**, which is itself a magic number (20).

### E5. The 24 EV-passing-but-not-promoted patterns

22 of these have ≥ 30 PTR rows. They should have entered the CPCV gate. They
did not get a verdict (NULL `cpcv_n_paths`). The Phase 2 `cpcv_gate.handler`
subscribes to `backtest_completed`, and that event type fires 175 times in
the last 24h. So the handler is alive — it's just not reaching these 22 by
ID.

Hypothesis (to be verified by Phase 0 below): the handler's
`backtest_completed` events carry a `scan_pattern_id` that matches only the
patterns the *cycle* writes events for, and the queue-driven `fast_backtest`
that produced these patterns' PTR rows doesn't emit `backtest_completed` with
that `scan_pattern_id`. The handler-import-broken-6-days history (memory
`reference_phase2_event_handlers.md`) corroborates: the silent no-op window
left a backlog of patterns that have data but never got the event.

### E6. Composite quality score is dormant by design

Of 586 patterns, **2** have `quality_composite_score` populated:

| id  | stage    | score | trades | wr   | avg_ret |
|-----|----------|-------|--------|------|---------|
| 585 | promoted | 0.877 |     11 | 0.64 |   8.04% |
| 586 | decayed  | 0.624 |      2 | 0.50 |   7.19% |

Both were scored when promoted into the Phase 4 cohort flow that is currently
gated off (`chili_cohort_promote_enabled=false`). No event re-scores them.
This is the operator's "should be event-driven like a neural node" — agreed.

### E7. The UI runtime-tab anomaly

15 patterns have 4,500-10,300 trades each and `oos_evaluated_at = NULL`:

```
id    name                                                trades  ptr_rows
731   Intraday Squeeze + Declining Volume [1m][BOS-tight] 10,341   13,696
732   "                                  "[1m][BOS-mod]    9,589   12,621
733   "                                  "[1m][BOS-wide]   8,700    8,700
...
```

Trade counts come from the cycle's `update_pattern_stats` (legacy path, now
gated off). PTR rows from the mining backtest writes. **Both are alive**.
What is *not* alive for these patterns is the CPCV gate verdict, which sets
`oos_evaluated_at`. The UI is correctly showing the inconsistency.

PTR write volume itself has been decelerating: from 26K rows/day on 2026-04-28
across 214 distinct patterns, to 656 rows/day today across 31 patterns. The
~7x narrowing tracks the 2026-04-27 evidence audit (30 patterns demoted) plus
ongoing retirements, but the 40x row-count drop suggests the mining queue is
also down to a small effective working set.

---

## Architecture proposal

### Phase 0 — Diagnose the silent CPCV gate (READ-ONLY, ~2h)

Before any threshold redesign, prove or disprove the handler-coverage
hypothesis. Three concrete artifacts:

1. **Audit script** `scripts/audit-cpcv-gate-coverage.ps1` that, for each
   pattern with PTR ≥ 30 but `cpcv_n_paths IS NULL`, looks up the last
   `backtest_completed` event in `brain_work_events` and reports:
   - event payload (`scan_pattern_id`, `parent_event_id`)
   - whether the cpcv_gate handler logged a verdict (search `[brain_work:cpcv_gate]` for that event id)
   - last `[handler_verify] OK` line (handlers reload weekly)

2. **Per-pattern force-evaluate** path so the operator can pick a single
   pattern (e.g. 731) and trigger the cpcv_gate handler against it
   synchronously via a brain-worker shell. Used to confirm the gate
   *would* produce a verdict if it were reached.

3. **Phase 2 handler queue depth** — count distinct
   `(event_type, scan_pattern_id)` pairs where no downstream handler log
   exists within 60s. This tells us whether events are dropping on the floor
   or never being emitted in the first place.

Output: a one-page "where the funnel breaks" memo + the audit script,
committed under `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`. **No code
changes** in this phase — diagnostic only.

### Phase 1 — Backfill the 275 missing CPCV verdicts (controlled, idempotent)

Once Phase 0 confirms the hypothesis, the fix is mechanical: for each pattern
with ≥ 30 PTR rows and NULL cpcv_n_paths, enqueue a synthetic
`backtest_completed` event whose payload references the pattern, so the
existing `cpcv_gate.handler_backtest_completed` runs.

Safety properties:
- **Idempotent** — re-running re-evaluates with current PTR data; the gate
  handler's `lifecycle_stage NOT IN ('promoted','retired')` guard prevents
  re-litigating decided patterns.
- **Rate-limited** — one batch of N (config) per minute, so we don't stampede
  the brain-worker.
- **Audit-logged** — every synthetic event tagged
  `source='cpcv_backfill_2026_05_11'` in payload for later analysis.
- **No autotrader side effects** — handler #3 (`promote.handler`) requires
  `chili_cohort_promote_enabled` AND the existing CPCV gate-pass flag. As
  long as the cohort flag stays OFF, backfill cannot place a trade. Only
  surfaces *eligibility*.

Operator decision gate after Phase 1: how many of the 275 actually pass the
gate? That number drives whether we ramp `chili_cohort_promote_enabled` on,
or whether the gate itself needs redesign first (Phase 2).

### Phase 2 — Replace hardcoded thresholds with empirical, sample-size-aware ones

This is the operator's "make it dynamic, adaptive, still profitable" ask.
Concrete redesign:

**Current (hardcoded magic numbers):**
```python
dsr      >= 0.95     # promotion_gate.py:903
pbo      <=  0.2     # promotion_gate.py:909
med_sh   >=  0.5     # promotion_gate.py:921
n_paths  >= 20       # paths_provisional_min  (configurable but constant)
```

**Proposed (data-driven, adaptive):**

For each metric, compute thresholds from the **active pattern pool's
empirical distribution** with **Bayesian shrinkage toward the pool mean** and
**sample-size-aware lower confidence interval**:

1. **Empirical thresholds.** Promote a pattern when its metric's
   lower-CI estimate exceeds the pool's q-th percentile, where q is set by
   how many patterns we want live. Pool of 586 patterns × target promotion
   rate 5% → q=0.95 → admit top ~29 patterns by *each* metric. The
   intersection becomes the live roster.

2. **Sample-size-aware CIs.** For DSR and median_sharpe, use the Hansen
   (2005) closed-form CI for deflated Sharpe; for PBO, use the bootstrap
   CI Bailey/Lopez-de-Prado provide. A pattern with 30 trades gets a wide
   CI and must clear by margin; a pattern with 300 trades clears with less
   margin. **No magic threshold — the CI does the work.**

3. **Bayesian shrinkage.** Each pattern's metric is shrunk toward the
   pool mean by a sample-size-dependent weight `w = n / (n + n0)` where
   `n0` is the prior strength (empirically set to the pool's median
   trade-count, currently around 60). Removes the "11 trades → DSR = 1.000"
   inflation we see in pattern 585. The shrunken metric is what gates.

4. **Pareto frontier multi-objective.** Instead of all-AND on
   (DSR, PBO, med_sh), promote a pattern only if it lies on the Pareto
   frontier of the active pool across all three metrics simultaneously
   (with shrinkage applied). Removes the "checkbox-checked but not the
   best" effect.

5. **Portfolio risk-budget feedback.** Before flipping a pattern to live,
   check that adding it improves the *portfolio* CPCV median sharpe by at
   least `chili_portfolio_marginal_sharpe_min_bps` (configurable, default
   0 meaning "any positive marginal contribution"). Prevents adding a
   correlated 7th breakout pattern when the portfolio is already breakout-heavy.

Settings introduced (all with empirically-derived defaults, none hardcoded):

```python
# Percentile thresholds — what fraction of patterns are eligible per metric
chili_cpcv_target_promotion_pool_pct: float = 0.05  # promote top 5%

# Bayesian shrinkage strength
chili_cpcv_shrinkage_prior_n: int | None = None  # None ⇒ use pool median trade-count

# Portfolio risk-budget marginal contribution required
chili_portfolio_marginal_sharpe_min_bps: float = 0.0

# Confidence-interval level for sample-size-aware thresholds
chili_cpcv_ci_level: float = 0.90
```

Implementation notes:

- Add a new module `app/services/trading/cpcv_adaptive_gate.py` that wraps
  `promotion_gate.promotion_gate_passes` with the adaptive logic. Behind a
  feature flag `chili_cpcv_adaptive_gate_enabled` (default False).
- Shadow-log both verdicts (hardcoded vs adaptive) for 7 days before flip.
- Persist the adaptive verdict's intermediate values
  (shrunken metric, lower-CI, pool percentile) to a new table
  `cpcv_adaptive_eval_log` for post-hoc analysis.

### Phase 3 — Composite quality score as an event-driven node

The operator's instinct is right: "it should be event driven like a neural
node." Today's behavior is closer to "cron-batched if cohort flow is enabled,
otherwise dormant." Concrete proposal:

1. **Backfill.** One-shot job that computes
   `pattern_quality_score.compute(pattern)` for every active pattern (586).
   Confirms the score function is sound on real data; gives us a baseline
   distribution to set Phase 2 thresholds against. Existing logic — no new
   model.

2. **Event-driven recompute** via a new Phase 2 handler `quality_score`:
   - Subscribes to: `pattern_stats_updated`, `backtest_completed`,
     `live_trade_closed`, `regime_evidence_updated` (the last two already
     fire — see probe 4E)
   - Reloads pattern + computes score + writes
     `scan_patterns.quality_composite_score`
   - Emits `pattern_quality_recomputed` for downstream consumers
   - Same import safety as other handlers (absolute imports per the
     2026-05-05 audit)

3. **Use the score.** Phase 2's adaptive gate (above) can consume the
   composite score as a 4th dimension on the Pareto frontier — patterns
   that win on per-metric CIs *and* on aggregate quality earn promotion.

4. **Then** enable `chili_cohort_promote_enabled`. With backfill done and
   event-driven recompute live, the cohort flow has the data it needs and
   the score reflects the current state of the pattern, not a frozen
   snapshot from when it was first promoted.

### Phase 4 — Fix the UI runtime-tab staleness

Root cause is the same as the promotion drought: 547 patterns have NULL
`oos_evaluated_at`. Phase 1 backfill fixes most of this for free. Two
secondary touches:

1. **Surface "PTR-ready but ungated" state** in the runtime tab so the
   operator can see which patterns are waiting on gate evaluation vs which
   genuinely have no data.
2. **Update the brain-worker dashboard** to show CPCV-gate handler queue
   depth (events received vs verdicts emitted) so the silent-no-op state
   from 2026-04-29 → 2026-05-05 can never recur invisibly.

### Phase 5 — Pattern-discovery throughput (lower priority)

PTR write volume narrowed 40x in 13 days. The cause is partially explained
by the evidence audit + retirements, but worth verifying mining is still
discovering at the expected cadence. Out of scope for this brief; surfaces
as a follow-up `f-pattern-discovery-throughput-audit` if Phase 0 confirms
mining is undercapacity.

---

## What I am NOT proposing

- **Lowering the magic-number thresholds.** That would admit overfit
  patterns. The operator's bar was "more promoted patterns without cutting
  quality." Adaptive empirical thresholds do that; tuning the constants
  down does not.
- **Disabling the CPCV gate.** The gate is the only thing standing between
  the evidence audit's 30-pattern purge and re-promoting them all on thin
  data.
- **Live-trading any of this in a single jump.** Phase 1 backfill must
  show meaningful pass-through (target: 15+ new patterns reach
  `backtested` lifecycle) before Phase 2's adaptive gate ships. Phase 2's
  adaptive gate must shadow-log 7 days before it gates promotions.
  Phase 3's cohort flow must run with adaptive verdicts for 7 days before
  any flag flip.

---

## Sequencing & operator decision points

```
Phase 0 (diag, ~2h)             → operator green-lights Phase 1
Phase 1 (backfill, ~1d)         → operator reviews "how many passed?"; green-lights Phase 2 if drought relieves
Phase 2 (adaptive gate, ~3d)    → shadow-log 7d; operator reviews; flip
Phase 3 (composite event-driven, ~2d) → backfill + handler + cohort flag flip
Phase 4 (UI surfacing, ~1d)
```

Each phase is a separate `f-*` brief promoted through the normal Cowork /
Claude Code loop. This file is the architect rationale that anchors them.

## Open questions for the operator

1. **Target promotion pool size.** Phase 2 defaults to top 5% (~29
   patterns). Is that the right ceiling? Concern: too many live patterns
   dilutes portfolio attention and complicates the regime gate.
2. **Acceptable CI level.** Phase 2 uses 90% CI by default. Higher (95%) →
   stricter promotion → smaller pool. Lower (80%) → looser → larger pool.
3. **PortfolioSharpe gate.** Phase 2's portfolio risk-budget step is the
   most aggressive piece. Operator may want it OFF for first iteration —
   it adds a coupling between pattern promotions that the rest of the
   brain doesn't currently model.

---

*Author: Cowork (algo-trader architect hat), 2026-05-11.*
*Probes:* `dispatch-promotion-drought-probe`, `dispatch-drought-probe-{2..6}`.
