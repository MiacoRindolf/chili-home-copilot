# f-adaptive-promotion-architecture (2026-05-11)

> **Type:** Architect proposal (READ-ONLY analysis + multi-phase implementation plan)
> **Priority:** P0 — the brain has gone from ~31 promoted patterns (pre-2026-04-27 audit) down to 3, and only one of them (585) is the actual workhorse. Live alert volume is concentrated on a single pattern. Promotion drought is the binding constraint on autonomous trading.
> **Scope:** Diagnose the drought, redesign the CPCV promotion gate with dynamic / data-driven thresholds, activate composite quality scoring as an event-driven node, and fix the backtest pipeline staleness the UI runtime tab is surfacing.
> **Status:** Phases 0 and 1a SHIPPED. Phase 1b in flight (CC session running). Phase 1c queued. Phases 2-4 still queued.
> **Last updated:** 2026-05-11 (post Phase 1a — corrected diagnosis below).

## TL;DR (updated 2026-05-11 after Phase 1a)

**The drought is an event-routing bug, not a threshold problem and not a
dispatcher-silence problem.** My initial diagnosis (Phase 0) said the dispatcher
was silent — that was wrong. Phase 0's grep used `brain_work:dispatch` (colon)
but the dispatcher's `LOG_PREFIX` is `[brain_work_dispatch]` (underscore). The
dispatcher has been running normally on a 25–90 min cadence the whole time.

Phase 1a (`docs/AUDITS/2026-05-11_dispatcher_silence.md`) found the actual
defect: **`enqueue_outcome_event` (`ledger.py:103`) writes `event_kind='outcome',
status='done', processed_at=now()` in a single INSERT, but `claim_work_batch`
(`ledger.py:184`) filters `event_kind='work' AND status IN ('pending','retry_wait')`**.
Rows enqueued via the outcome helper are born terminal and can never be
claimed. 7 of 9 handler-targeted event types route through this path:

| event_type                  | historical done rows | dispatched? | target handler        |
|-----------------------------|---------------------:|:-----------:|-----------------------|
| `backtest_completed`        | 1,055                | NO          | `cpcv_gate`           |
| `breakout_alert_resolved`   | 2,659                | NO          | `breakout_outcomes`   |
| `market_snapshots_batch`    | 179                  | NO          | `regime_ledger`       |
| `broker_fill_closed`        | 131                  | NO          | `execution_robustness`|
| `live_trade_closed`         | 4                    | NO          | `live_drift`          |
| `paper_trade_closed`        | 1                    | NO          | `live_drift`          |
| `pattern_eligible_promotion`| 0                    | NO          | `promote`             |
| `backtest_requested`        | 32                   | **YES**     | (dispatcher itself)   |
| `execution_feedback_digest` | 28                   | **YES**     | various               |

**~4,000 orphaned events. Nine handlers have never fired against production
traffic of their target event types.** The cpcv_gate, mine, promote, demote,
regime_ledger, pattern_stats, breakout_outcomes, live_drift, and
execution_robustness handlers exist, import cleanly at startup
(`[handler_verify] OK 6/6`), but nothing has ever called them. That's why
547 of 586 patterns have NULL `cpcv_n_paths`.

The CPCV threshold issue still exists — for the 39 patterns that DO have data,
DSR is pegged at 1.000 and PBO at 0.000 across every percentile, so the
hardcoded thresholds (DSR ≥ 0.95, PBO ≤ 0.2, median_sharpe ≥ 0.5) admit
everything that reaches them. But fixing thresholds doesn't matter until the
handler runs in the first place.

The proposal is now in three pieces (was two):

1. **Phase 1b — Architectural fix to the event queue.** Unify outcome and
   work event semantics so handlers actually fire. Behind a feature flag,
   reversible, byte-identical at flag-off. Ships before any backfill.
2. **Phase 1c — Controlled backfill of the 4,000 historical orphans.**
   Operator-rate-limited, per-event-type, kill switch. Ships only after
   Phase 1b is stable for 24h in prod.
3. **Phase 2 — Replace hardcoded gate thresholds with empirical,
   sample-size-aware ones** (Bayesian shrinkage + lower-CI percentiles +
   Pareto frontier multi-objective + portfolio marginal-Sharpe lift). Wire
   composite quality score as event-driven on `pattern_stats_updated` /
   `backtest_completed`. Ships in parallel with Phase 1c once Phase 1b prod
   flip is clean.

The runtime-tab UI anomaly ("patterns with trades but no recent backtest")
has the same root cause: PTR rows fall into `trading_pattern_trades` via
mining backtests, but `scan_patterns.oos_evaluated_at` only gets set when
the CPCV gate handler completes — and the handler never completes because
`backtest_completed` events are born terminal.

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

### Phase 0 — Diagnose the silent CPCV gate (READ-ONLY) — **SHIPPED commit `738a72d`**

Read-only audit: 50 of the 275 candidate patterns sampled, classified by
where the funnel breaks. Memo at `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`.

Initial finding: 100% of audited patterns had no `[brain_work:cpcv_gate]`
log line; conclusion was the dispatcher was silent. **That conclusion turned
out to be wrong** (see Phase 1a below) — but the audit also surfaced a
second-order finding that stood up: the ensemble pre-gate inside
`check_promotion_ready` (`mining_validation.py:341`) short-circuits BEFORE
CPCV runs for high-trade-count patterns, leaving `cpcv_n_paths` NULL even
when the handler would have been reached. Two force-eval samples (731 with
13,696 PTR rows; 1212 with 7,095) confirmed `detail.blocked='ensemble_failed'`
and `scan_pattern_patch={}`.

Phase 0 cost: 1 brief, 2 audit scripts, 1 memo, 1 CC_REPORT. Zero code
changes.

### Phase 1a — Find the real dispatcher state — **SHIPPED commit `4c1e46e`**

Read-only follow-up because Phase 0's "dispatcher silent" conclusion didn't
match other evidence (handlers imported clean at startup; 205 events/24h
marked `done`). Tested six hypotheses (H1–H6). Memo at
`docs/AUDITS/2026-05-11_dispatcher_silence.md`.

Verdicts:
- **H1 (dispatcher not running):** RULED OUT. Five dispatch rounds in the
  current 4.5h uptime, on the expected 25–90 min cadence.
- **H2 (logger filtered):** CONFIRMED as the cause of Phase 0's grep
  artifact (`dispatcher.py:25` uses `LOG_PREFIX = "[brain_work_dispatch]"`
  with underscore; Phase 0 grepped colon). RULED OUT as the cause of
  handler silence — handler prefixes are correctly colon-formed, and they
  still produced zero log lines because the handlers genuinely never run.
- **H3 (ledger flag off):** RULED OUT. `brain_work_ledger_enabled=True`,
  batch sizes sane.
- **H4 (legacy `run_learning_cycle` writing):** RULED OUT.
  `learning.py` doesn't touch `brain_work_events` at all.
- **H5 (`backtest_queue_worker.py` self-marking):** CONFIRMED with
  precision — the actual rogue writer is one level deeper:
  **`app/services/trading/brain_work/ledger.py:103`**, inside
  `enqueue_outcome_event` (lines 72–113). It INSERTs with
  `event_kind='outcome'`, `status='done'`, `processed_at=now()` —
  the row is born terminal.
- **H6 (different handler / different prefix):** RULED OUT. Zero handler
  log lines across all six worker containers.

The architectural defect: `claim_work_batch` (`ledger.py:184`) filters
`event_kind='work' AND status IN ('pending','retry_wait')`. The seven
emitters in `emitters.py` that produce handler-targeted outcome events all
route through `enqueue_outcome_event`, so their rows can never be claimed.

Phase 1a cost: 1 brief, 1 audit script, 1 memo, 1 CC_REPORT. Zero code
changes.

### Phase 1b — Architectural fix: unify the event queue — **IN FLIGHT (CC session running 2026-05-11)**

Behind feature flag `chili_brain_outcome_claimable_enabled` (default False,
reversible):

1. **`enqueue_outcome_event`** writes `status='pending'`, `processed_at=NULL`
   when flag True. `event_kind='outcome'` is preserved as a tag (audit
   semantics intact).
2. **`claim_work_batch`** drops the `event_kind='work'` filter when flag
   True. Both kinds become claimable through the same lifecycle
   (`pending → in_progress → done`).
3. **Partial index** `ix_brain_work_events_claim_v2` on
   `(domain, event_type, status, scheduled_at) WHERE status IN ('pending','retry_wait')`
   added proactively so the broadened claim path doesn't hot-spot.
4. **Handler idempotency test suite** — each of the 9 handlers called
   twice with the same event payload, no duplicate side-effects. Hard
   gate for Phase 1c.
5. **Consult gate** for operator: when the dispatcher processes an
   outcome event, should `processed_at` reflect handler-completion time
   (Option 1, recommended) or stay at the original outcome timestamp
   (Option 2)?

Default OFF at merge. Operator-controlled rollout via `trading_settings`.

Brief: `docs/STRATEGY/QUEUED/f-brain-event-kind-unify.md`.

### Phase 1c — Controlled backfill of the 4,000 historical orphans — **QUEUED**

Hard prereq: Phase 1b shipped, flag True in prod, 24h of clean handler
activity observed.

The Phase 1b flag does NOT touch historical rows — legacy `status='done'`
keeps them ineligible to claim. Phase 1c is the controlled mechanism to
bring them forward.

Per-event-type, operator-rate-limited, kill switch. Recommended order
(smallest blast radius first):
1. `paper_trade_closed` (1 row) — smoke test
2. `live_trade_closed` (4 rows) — confidence builder
3. `market_snapshots_batch` (179 rows) — populates regime_ledger baseline
   for Phase 2's adaptive gate
4. `broker_fill_closed` (131 rows) — post-hoc execution audit
5. `backtest_completed` (1,055 rows) — **the actual drought relief**
6. `breakout_alert_resolved` (2,659 rows) — largest; last

The cpcv_gate handler's lifecycle guard
(`lifecycle_stage NOT IN ('promoted','retired')`) prevents
re-litigating decided patterns. Inter-batch sleep of 30s prevents
monopolizing the dispatcher.

Brief: `docs/STRATEGY/QUEUED/f-brain-event-kind-backfill.md`.

Operator decision gate after Phase 1c: how many of the 1,055 backfilled
`backtest_completed` events produce a CPCV verdict, how many short-circuit
at the ensemble pre-gate, and how many reach `pattern_eligible_promotion`?
Those numbers calibrate Phase 2's adaptive thresholds against the *real*
empirical distribution of the active pool, not the 39-pattern sample we
have now.

**Expected pattern emergence (post Phase 1c, before Phase 2 ships):**

Realistic estimate: 5–30 new patterns reach `promoted` lifecycle over the
first 24–48h after Phase 1c lands. Quality protection in that interim
window comes from three layers that are unaffected by the CPCV defect:
- **Ensemble pre-gate** (`mining_validation.py:341`) — proven to reject
  high-trade-count patterns in Phase 0 force-eval.
- **Realized-EV gate** — `avg_return_pct > 0 AND win_rate > 0 AND
  trade_count >= 5` filters before CPCV.
- **Autotrader gate stack** — rule gate, LLM revalidation, drawdown
  breaker, regime gate, PDT, cost-aware sizing, kill switch. Promotion
  ≠ trading; each new alert still has to clear these.

Caveat: with CPCV's hardcoded thresholds pegged at admit-all (DSR=1.000,
PBO=0.000 across all 39 patterns that have data), the new promotions
clear a gate that has zero discriminatory power. The downstream layers
catch bad ones, but the *brain's* promotion gate is doing rubber-stamping,
not gating. **Phase 2 should not lag Phase 1c by more than a few days.**

### Phase 2 — Replace hardcoded thresholds with empirical, sample-size-aware ones

This is the operator's "make it dynamic, adaptive, still profitable" ask.
The honest framing: Phase 2 doesn't eliminate ALL numbers — it eliminates
**arbitrary** numbers (the kind that bug us) and replaces them with
**operator-policy** numbers (the kind that have semantic meaning).

**Numbers that go away (arbitrary, no project-specific justification):**
```python
dsr      >= 0.95     # promotion_gate.py:903 — Lopez de Prado convention, inherited
pbo      <=  0.2     # promotion_gate.py:909 — same
med_sh   >=  0.5     # promotion_gate.py:921 — same
n_paths  >= 20       # paths_provisional_min — inherited
min_trades >= 30     # full_confidence_min — inherited
```

**Numbers that remain (operator policy, genuinely meaningful):**
```python
chili_cpcv_target_promotion_pool_pct  = 0.05   # "I want top ~5% of patterns live"
chili_cpcv_ci_level                   = 0.90   # "I want 90% confidence in lower-bound"
chili_portfolio_marginal_sharpe_min_bps = 0.0  # "any positive marginal contribution admits"
```

These express risk appetite and statistical strength. They could be
defaulted to standard conventions or tuned to express explicit
preferences. The math (Bayesian shrinkage, empirical percentile, Pareto
frontier) is computed from the active pool's distribution, so the gate
adapts as the pattern population evolves.

**Concrete redesign:**

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

## Sequencing & operator decision points (updated 2026-05-11)

```
Phase 0  diagnose CPCV gate coverage         SHIPPED commit 738a72d
Phase 1a find real dispatcher state          SHIPPED commit 4c1e46e
Phase 1b architectural fix (event-kind unify)  IN FLIGHT (CC session running)
Phase 1c controlled backfill of 4000 orphans QUEUED — gated on 1b stable 24h
Phase 2  adaptive CPCV gate                  QUEUED — parallel with 1c
Phase 3  composite quality as event-driven   QUEUED
Phase 4  UI runtime-tab staleness fix        QUEUED
```

Each phase is a separate `f-*` brief promoted through the normal Cowork /
Claude Code loop. This file is the architect rationale that anchors them.

## Open questions for the operator

1. **Target promotion pool size.** Phase 2 defaults to top 5% (~29
   patterns). Is that the right ceiling? Concern: too many live patterns
   dilutes portfolio attention and complicates the regime gate.
2. **Acceptable CI level.** Phase 2 uses 90% CI by default. Higher (95%) →
   stricter promotion → smaller pool. Lower (80%) → looser → larger pool.
3. **Portfolio risk-budget gate.** Phase 2's portfolio marginal-Sharpe step
   is the most aggressive piece. Operator may want it OFF for first
   iteration — it adds a coupling between pattern promotions that the
   rest of the brain doesn't currently model.
4. **Phase 1b consult: `processed_at` semantics.** When the dispatcher
   processes an outcome event under the unified queue, should
   `processed_at` reflect handler-completion time (Option 1, recommended;
   gives latency-of-reaction observability) or stay at the original
   outcome timestamp (Option 2; preserves the "instant terminal" semantic
   of legacy rows)?

## Optional follow-ups Phase 1a surfaced

- **Normalize the dispatcher LOG_PREFIX.** `dispatcher.py:25` uses
  `[brain_work_dispatch]` (underscore); every handler uses
  `[brain_work:<name>]` (colon). The inconsistency caused Phase 0's grep
  mismatch. One-line cleanup. Defer until Phase 1b lands so the diff stays
  isolated.
- **Outcome vs work as a TAG, not a queue split.** With Phase 1b shipped,
  `event_kind` becomes pure metadata. The dispatcher's
  `_dispatch_limits` iteration shouldn't gate on it. Worth documenting
  in the runbook so future contributors don't reintroduce the split.
- **`breakout_alert_resolved` evidence path.** 2,659 historical events
  imply a massive missed-evidence opportunity. The
  `breakout_outcomes` handler aggregates this into the secondary-evidence
  scoring path. Worth a separate "what did we learn?" memo after Phase 1c
  drains the backlog through it.

---

*Author: Cowork (algo-trader architect hat).*
*First written 2026-05-11. Updated 2026-05-11 after Phase 1a corrected
the diagnosis.*
*Probes:* `dispatch-promotion-drought-probe`, `dispatch-drought-probe-{2..6}`.
*Phase 0 evidence:* `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`,
`scripts/audit-cpcv-gate-coverage-out.txt`,
`scripts/audit-cpcv-gate-force-eval-{731,1212}-out.txt`.
*Phase 1a evidence:* `docs/AUDITS/2026-05-11_dispatcher_silence.md`,
`scripts/audit-dispatcher-silence-out.txt`.
