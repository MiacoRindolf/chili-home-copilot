# f-null-family-cohort-decision (2026-05-18)

> **Type:** Architect decision brief — three options + recommendation
> **Trigger:** scheduled task `null-family-architecture-decision-2026-05-18`
> followed up on the NULL `hypothesis_family` cohort surfaced by the
> 2026-05-15 evidence-fidelity activation.
> **Status:** awaiting operator judgment — do **not** auto-promote.

## TL;DR

The 2026-05-15 architect-flag attributed **-$1,710 / 30d** to "the
NULL `hypothesis_family` cohort". When probed directly at run-time
(2026-05-18) that figure splits in two and the bigger half lives
somewhere we weren't expecting.

| Cohort | n_trades (30d) | total_pnl (30d) |
|---|---:|---:|
| Patterns with NULL `hypothesis_family`              |  22 |   **-$96.96** |
| Trades with `scan_pattern_id IS NULL` (no pattern)  | 101 | **-$1,636.27** |
| **Both — what the headline aggregated**             | 123 | -$1,733.23 |

So the architect-flag is **two findings glued together**:

1. The actual NULL-family pattern cohort costs **-$97 / 30d** —
   meaningful but not the headline. 72 active patterns; -$1.32 per
   pattern over 30d on average.
2. **101 trades fired with no `scan_pattern_id` at all** and lost
   **-$1,636 / 30d**. This is the real bleed and it is **NOT** what
   the three architect options below are sized for.

This brief frames the three options for finding (1) and surfaces
finding (2) separately as **Open Question for Cowork** for a
follow-up brief.

## Current numbers (2026-05-18 run-time probe)

### Coverage census (`scan_patterns`)
```
null     :  74
unknown  :   0
tagged   : 700
total    : 774
```

(Up from the 2026-05-16 post-backfill snapshot of 72 NULL / 519
tagged. Two NULL-family additions in two days — variant inserts
still seed without family when their parent is NULL.)

### 30d PnL by family

```
family                  n_trades   total_pnl    avg_pnl
----------------------------------------------------------
<NULL>                       123    -1733.23     -14.21    ← see split below
mean_reversion                73      -85.02      -1.18
momentum_continuation         44      -10.32      -0.23
compression_expansion        107     +605.12      +5.76
```

### NULL bucket subdivided
```
no_pattern_link               101    -1636.27    -16.20    ← NOT a family problem
pattern_linked_NULL_family     22      -96.96     -4.41    ← the actual cohort
```

### NULL-family lifecycle distribution
```
challenged       (active)  : 47
candidate        (active)  : 18
shadow_promoted  (active)  :  4
promoted         (active)  :  1
pilot_promoted   (active)  :  1
backtested       (active)  :  1
candidate     (inactive)   :  1
retired       (inactive)   :  1
```

### Three patterns own 94% of the NULL-cohort 30d loss

| id   | lifecycle        | name                                                         | trades | pnl     |
|------|------------------|--------------------------------------------------------------|-------:|--------:|
| 1205 | shadow_promoted  | Above SMA20 + RSI>50 + ADX>20 (healthy uptrend)              |   3    | -$37.65 |
| 1068 | challenged       | Volume spike 2x+ with RSI<40 (capitulation / accumulation)   |   4    | -$32.46 |
| 1216 | challenged       | EMA stack + RSI neutral zone (healthy trend) [5m]            |  11    | -$21.04 |
| 1072 | shadow_promoted  | Triple confluence: RSI<35 + MACD bullish + near lower BB     |   1    | -$ 7.21 |
| 69 other NULL patterns                                                                  |   3    |  +$1.40 |

### Classifier reachability of the 74 NULL patterns

Re-running `_classify_by_name` over the current 74 NULL rows: the
existing keyword list tags **0 of 74**. Confirms `backfill` is
correctly idempotent and the residual tail is **structurally
unreachable with the current ruleset**.

Root vs child split among the 74: **20 roots, 54 children**. The
children all hang off NULL-family roots, so the inheritance pass
can't rescue them until the 20 roots are tagged.

### Keyword density on the residual 74 (NULL-cohort only)

Hits inside the name+description text:

```
rsi>          : 23     ema           : 23     macd          : 20
rsi<          : 20     confluence    : 16     bounce        :  9
uptrend       :  9     lower bb      :  9     accumulation  :  8
sma           :  8     volume spike  :  8     bb            :  8
pullback      :  7     capitulation  :  7     ibs           :  6
engulf        :  6     bull engulf   :  6     fade          :  6
gap-down      :  6     mean reversion:  6     mean_reversion:  6
retest        :  1
```

Translation: **the tail is highly classifiable** if the keyword
list is extended. The classifier's blind spots are systematic
(modern mined-pattern naming uses `RSI>50` / `ADX>20` / `IBS<0.2` /
"capitulation" / "pullback" / "bounce" / "uptrend" / "Mean
Reversion" / "engulf" — none of which are in the current ruleset).

### BH-bypass path — confirmed still live

`promotion_gate._count_variants_in_family` (lines 80-129) returns
`1` for any pattern with `hypothesis_family IS NULL` AND a NULL
parent chain (or for patterns whose parent chain only contains other
NULL-family rows). `cpcv_adaptive_gate._evaluate_adaptive` then
short-circuits `use_bh` to `False` whenever `fam_m == 1` (line 471).

So the BH discipline — now authoritative since
`chili_family_fdr_enabled = True` was flipped on 2026-05-15
(commit `e7f8a10`) — **never fires on these 74 patterns**. They
get the legacy naive percentile threshold while everyone else
gets the family-FDR-adjusted threshold. Structurally unfair, and
the empirical PnL (only -$97 over 22 trades) doesn't yet justify
forcing an aggressive intervention.

## Three options

### Option A — Demote the NULL cohort entirely

Treat absence of `hypothesis_family` as evidence-failure; force
the 72 active NULL patterns to `lifecycle_stage = 'challenged'`
(or `retired`) via a migration.

**Pros**
- Closes the BH-bypass gap by removing the cohort from the gate
  surface.
- Ends the -$97 / 30d bleed.
- Sends a clear signal that tagging is load-bearing now.

**Cons**
- 4 are `shadow_promoted` and 1 is `promoted` — wiping them risks
  losing whatever genuine signal sits in the tail. Pattern 1205
  (only -$37 / 3 trades) is shadow_promoted and might still be
  proving its EV.
- Cost ($97 / 30d ≈ -$3.20 / day) doesn't itself justify the
  aggressive call. The 2026-05-15 architect-flag was sized to
  -$1,710; that motivation evaporates at run-time.
- Doesn't fix the seed bug — new variant inserts will keep
  creating NULL children whenever their parent is NULL.

### Option B — Tag-and-gate with synthetic `unclassified_tail`

Assign all 74 to a synthetic family (`unclassified_tail` or
`other`) so BH discipline applies. Add a migration that fixes
`learning.py` insert paths to seed the synthetic family when no
classifier rule fires.

**Pros**
- Forces multiple-testing rigor on the cohort without removing
  any patterns.
- Cheap (1 migration + 1 insert-path fix; no `pattern_family_backfill`
  changes).
- Preserves all promoted/shadow_promoted patterns intact.

**Cons**
- The synthetic family is **74 variants** by definition. BH at
  `m=74` is severe — naive_threshold gets adjusted by a Bonferroni-
  family factor that will almost always block promotion.
- Equivalent to a silent soft-demote: the patterns survive but
  can never clear the gate. That's the same end state as A with
  more ceremony.
- Doesn't reflect the truth: these are 20 distinct hypothesis
  roots, not 74 sibling variants. Grouping unrelated patterns
  into one synthetic family is a category error that the BH math
  punishes harder than the data deserves.

### Option C — Extend the classifier

Add ~15 keyword rules to `pattern_family_backfill._NAME_RULES`
covering the systematic patterns in the residual tail. Re-run
the backfill; the parent_chain inheritance pass will then carry
those tags down to the 54 children.

Proposed additions (priority-ordered, specific → broad):

```python
# Mean-reversion family (oscillator / engulf / volume-capitulation)
("ibs<", "mean_reversion"),
("bull engulf", "mean_reversion"),
("bear engulf", "mean_reversion"),
("capitulation", "mean_reversion"),
("accumulation", "mean_reversion"),
("rsi<", "mean_reversion"),
("oversold bounce", "mean_reversion"),
("lower bb", "mean_reversion"),
("fade", "mean_reversion"),
("mean reversion", "mean_reversion"),
("mean_reversion", "mean_reversion"),

# Momentum-continuation family (trend / pullback / stack / retest)
("rsi>", "momentum_continuation"),
("uptrend", "momentum_continuation"),
("downtrend", "momentum_continuation"),
("pullback", "momentum_continuation"),
("ema stacking", "momentum_continuation"),
("sma stack", "momentum_continuation"),
("retest", "momentum_continuation"),
("healthy trend", "momentum_continuation"),
("strong trend", "momentum_continuation"),
```

Estimated coverage on the 74 (from keyword census above):

- `ibs<` → 6
- `engulf` → 6
- `capitulation` → 7
- `rsi<` → 20 (overlaps with confluence/MACD names)
- `rsi>` → 23 (overlaps with uptrend/SMA names)
- `uptrend` → 9
- `pullback` → 7
- `bounce` → 9
- `mean reversion` → 6
- `lower bb` → 9

Conservative estimate (with deduplication): **65-70 of 74 tagged**
on a second backfill pass. Remaining 4-9 are genuine legacy
oddities (`[Unlinked legacy insight]`, `break_and_retest_long`, a
couple of standalone `Crypto Volume Spike` rows) and can be
hand-tagged in the same migration.

**Pros**
- Surgical. Addresses the structural problem (insufficient ruleset
  for modern mined-pattern naming) instead of papering over it.
- Restores parent_chain inheritance: 54 NULL children get tagged
  automatically once their 20 roots are tagged.
- Preserves all patterns — promoted, shadow_promoted, candidate.
  Whatever genuine signal exists in the tail keeps trading.
- Activates BH discipline correctly: tagged patterns flow into
  their real families (mostly mean_reversion / momentum_continuation
  which already have 100+ siblings) where `fam_m` is large enough
  for the BH adjustment to be statistically meaningful but small
  enough not to be Bonferroni-crushing.
- Cheapest of the three (15 lines in `pattern_family_backfill.py`
  + one re-run of the existing dispatch script).

**Cons**
- Doesn't address the seed bug — `learning.py` insert paths still
  need a fix or new variants will reseed the NULL bucket. Treat
  as a follow-up rather than blocker.
- Keyword classifier remains brittle by design. New naming
  conventions will need rule updates. This is the same brittleness
  the existing classifier already accepts.

## Architect recommendation: **Option C, with a Phase 2 follow-up**

**Phase 1 (this brief if promoted):**
1. Extend `_NAME_RULES` per the list above.
2. Re-run `dispatch-hypothesis-family-backfill.ps1` against
   `dry_run=False`.
3. Hand-tag the 4-9 residual orphans inside the same migration.
4. Verify post-coverage: `null=0, tagged=774`.

**Phase 2 (queue as a separate brief — `f-learning-insert-family-seed.md`):**
- Audit `learning.py` variant-creation paths; ensure the classifier
  fires at INSERT time for any new mined pattern.
- Add a startup audit that fails loud if `scan_patterns` ever has
  a NULL `hypothesis_family` after migration 185 + the backfill.

**Why not Option A or B:**
- The data does **not** justify a wholesale demote. The
  -$1,710 / 30d figure was 94% misattributed; the real NULL-family
  bleed is -$97 / 30d on a cohort that contains at least one
  promoted pattern with too-few trades to judge yet.
- The synthetic-family tag (B) would silently kill the cohort
  via BH at `m=74` while pretending to be conservative. Either
  demote them honestly (A) or tag them correctly (C).
- Option C is reversible. If the rule extensions over-tag, the
  next backfill pass with a tighter ruleset corrects in one
  migration. A demote (A) loses signal that's hard to recover.

## Open question for Cowork — the **real** -$1,636 / 30d loss

The 101 closed trades with `scan_pattern_id IS NULL` lost
**-$1,636 over 30d**. Breakdown:

```
exit_reason                            asset    n_trades   total_pnl
--------------------------------------------------------------------
<NULL>                                 equity         41    -$588.07
broker_reconcile_position_gone         equity         34    -$536.33
stop                                   equity          3    -$418.34   (!)
pattern_exit_now                       equity          6    -$220.48
broker_stop_filled_outside_chili       equity          2    -$ 71.80
zombie_reconcile_orphan                equity          7      $0.00
coinbase_position_sync_gone            crypto          2      $0.00
<NULL>                                 crypto          3      $0.00
target                                 equity          3    +$198.75
```

**This is a different problem from NULL-family classification.**
These trades fired with no pattern link at all — autotrader took
positions that were never traceable to a hypothesis. Notable:

- **34 `broker_reconcile_position_gone` equity trades cost -$536.**
  This is exactly the symptom that motivated the 2026-05-08 PDT
  phantom-rows fix on the **crypto** lane (R31/R32 → commit
  `60c26f8`). The equity lane's `broker_reconcile_position_gone`
  filter brief (`f-equity-broker-reconcile-wipeout-protection`)
  was queued but apparently never shipped. Look for it.
- **3 stop-outs for -$418** — 1 trade lost $243.99 on a single
  stop. Worth tracing.
- **41 trades with `exit_reason IS NULL`** suggests rows that
  closed without writing an exit_reason; possible parity-logger
  gap or a closed-path that doesn't set the field.

**Recommended follow-up brief:** `f-no-pattern-link-loss-audit-2026-05-18`
to investigate where these autotrader entries come from and why
they aren't carrying a `scan_pattern_id`. This is plausibly the
**largest single bleed source surfaced in May 2026** and was
hidden inside the "NULL-family" headline by aggregation.

## Constraints / do-not-touch

- No autotrader / venue / broker behavior change in Phase 1.
- No promotion-gate logic change. The classifier is a data fix.
- `pattern_family_backfill.py` updates only touch `_NAME_RULES`
  and (optionally) the residual orphan hand-tag mapping. Do not
  alter `_resolve_via_parent_chain` or the UPDATE SQL.
- Re-running the backfill is idempotent; existing tagged rows
  are unaffected by the WHERE filter.

## Rollback plan

If Phase 1 over-tags (e.g., a new rule fires on a pattern that
genuinely belongs to a different family):

1. The classifier writes a per-row `proposals` list in `dry_run=True`
   mode. Always run dry first; inspect.
2. If a tag is wrong, set the row back to NULL via a one-shot
   UPDATE: `UPDATE scan_patterns SET hypothesis_family = NULL
   WHERE id IN (...)`.
3. Adjust `_NAME_RULES`; re-run.

No migration is required for Phase 1 (the backfill is data-only).
Phase 2 would need a migration; that brief includes its own
rollback.

## Success criteria

- 74 NULL `hypothesis_family` rows → ≤5 NULL after backfill.
- Post-backfill smoke confirms the parent_chain inheritance pass
  carried tags to all 54 children of the 20 newly-tagged roots.
- `pattern_family_trial_log` shows the first BH-applied gate
  decisions on previously-untagged patterns within the next
  CPCV cycle.
- No promotion / lifecycle changes on the 4 shadow_promoted +
  1 promoted patterns inside the cohort (their families just
  get filled in; gate decisions remain in the same lane).

## Files touched (Phase 1, estimate)

- `app/services/trading/pattern_family_backfill.py`  (~15 LOC
  added to `_NAME_RULES`)
- `scripts/_smoke_family_backfill.py` (re-run; no code change)
- `scripts/dispatch-hypothesis-family-backfill.ps1` (re-run)
- This brief moves from `QUEUED` to `NEXT_TASK` on operator promote.
