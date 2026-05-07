# NEXT_TASK: f-thread-tail-2026-05-07-2

STATUS: DONE

**Promoted as a combined two-phase tail brief on 2026-05-07 14:30 UTC. Drains the two remaining unblocked QUEUED briefs in one CC run. Phase 1 is pure analysis (zero code, fast); Phase 2 is small code + tests + observability. Independent — Phase 2 is not blocked by Phase 1's findings.**

**Each phase produces its own CC_REPORT (per the protocol pattern from f-overnight-jumbo-2026-05-06). Combine into one closeout commit chain.**

## Meta-goal

Empty the QUEUED backlog of unblocked briefs, leaving only the deferred-on-trigger follow-ups (`f-exit-parity-cutover-gate-flip` waiting on the scheduled monitor's verdict, etc.). Both phases are well-scoped from prior queueings; no re-derivation required.

## Phase ordering

| # | Phase | Tier | Est | Reasoning |
|---|---|---|---|---|
| 1 | f8b-verification-soak-3 | **read-only analysis** | ~30 min | No code; zero risk; data is ready (>72h post-deploy). Run first because it informs the next strategic move (F8b validated vs. drop BTC vs. F9). |
| 2 | bracket-writer-cover-policy-clarify | **code + tests + observability** | ~60 min | Comments, label rename, startup warning, status surface, 6 tests. Behavior-preserving — no runtime decision-logic change. |

**Floor**: at least Phase 1 ships. Aspiration: both ship.

If Phase 2 surfaces a precondition issue (e.g., the FastAPI startup hook doesn't fit the warning pattern as written), commit Phase 1 alone, leave Phase 2 staged with a note in the CC report, and stop. Don't deadlock on Phase 2 design questions.

---

## Phase 1 — f8b-verification-soak-3

**Original brief queued 2026-05-03; preempted by `audit-missing-stop-emergency-repair` (DONE 2026-05-03). Re-promoted now.** Pure analysis. Zero code commits. Read-only against `fast_signal_decay`, `fast_alerts`, `fast_exits`, `fast_executions`, `fast_path_status`.

### Goal

Re-execute the F8b verification analysis with ≥ 24h of post-deploy realized data. Two prior soak runs (`f8b-verification-soak` at 10 min post-deploy, `f8b-verification-soak-2` at 28 min post-deploy) were correctly inconclusive: zero post-deploy distinct closed exits both times. **This is the briefed 24h target.** By now, ~15-25 BTC and ~12-18 SOL post-deploy distinct exits should have accumulated — enough for verdict-grade per-ticker decision-tree application.

After this phase:

1. **BTC's allowlist membership is decided** with verdict-grade evidence (n ≥ 20 distinct post-deploy exits).
2. **SOL's calibrated 25s delay is validated** against the +3.47 bps counterfactual target.
3. **The next strategic move is named:** F9 (both drift negative), F8b-tightened (BTC drops, SOL stays), F8b stays (both positive), or one-more-soak (still inconclusive).

Deliverable: `docs/STRATEGY/CC_REPORTS/2026-05-07_f8b-verification-soak-3.md`. Zero code commits.

### Pinned deploy timestamp

`2026-05-03 16:29:20 UTC`. All post-deploy filters use this.

### SQL — six lenses, in priority order

#### 1.1 Distinct realized P/L per-ticker, post-deploy cohort (PRIMARY)

```sql
WITH pullback_eids AS (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
    AND e.decided_at > '2026-05-03 16:29:20'
)
SELECT e.ticker, COUNT(*) AS exits,
       ROUND(SUM(x.realized_pnl_usd)::numeric, 4) AS pnl,
       COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0) AS wins,
       ROUND((100.0 * COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0)
              / NULLIF(COUNT(*), 0))::numeric, 1) AS win_rate_pct,
       ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps,
       ROUND(AVG(x.holding_period_s)::numeric, 0) AS avg_hold_s
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
WHERE x.entry_execution_id IN (SELECT id FROM pullback_eids)
GROUP BY e.ticker ORDER BY exits DESC;
```

**Critical: use IN-subquery, not top-level JOIN.** Same anti-inflation pattern as `docs/RUNBOOKS/fast_alerts-microsecond-dup.md`.

Report shape:

| Ticker | F8a-rerun-2 actual | F8b counterfactual | soak (10min) | soak-2 (28min) | This run | Verdict |
|---|---|---|---|---|---|---|
| BTC-USD | +5.66 bps n=8 | −0.75 bps n=69 | 0 | 0 | ? | ? |
| SOL-USD | +3.34 bps n=13 | +3.47 bps n=43 | 0 | 0 | ? | ? |

#### 1.2 Cluster-correlation handling

Two catchup paper_fills opened at 2026-05-03 16:29:33 (1 BTC + 1 SOL) are time-correlated. Treat their aggregate as ONE data point if both close in the same direction. Subsequent organic post-deploy fills are independent.

```sql
SELECT e.ticker, e.id, e.decided_at,
       x.realized_pnl_usd, x.realized_return_pct
FROM fast_executions e
LEFT JOIN fast_exits x ON x.entry_execution_id = e.id
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at BETWEEN '2026-05-03 16:29:30' AND '2026-05-03 16:29:40'
ORDER BY e.decided_at;
```

If both catchup fills closed in the same direction, deduct 1 from effective n.

#### 1.3 Allowlist gate efficacy

```sql
SELECT e.ticker, e.reject_reason, COUNT(*) AS n
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > '2026-05-03 16:29:20'
  AND e.decision='rejected'
GROUP BY 1, 2 ORDER BY 1, n DESC;

SELECT COUNT(*) AS false_rejects
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > '2026-05-03 16:29:20'
  AND e.ticker IN ('BTC-USD', 'SOL-USD')
  AND e.reject_reason LIKE 'pullback_ticker%';
-- Expected: 0.
```

#### 1.4 Validation-residual at h=1800 (SECONDARY)

```sql
SELECT ticker, score_bucket, horizon_s, sample_count,
       realized_validation_count AS val_n,
       ROUND(realized_validation_residual::numeric * 10000, 2) AS resid_bps,
       ROUND(mean_return::numeric * 10000, 3) AS miner_mean_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
  AND realized_validation_count > 0
ORDER BY ticker, horizon_s;
```

By 96h post-deploy, DOGE post-fix-only cells should average ~5 bps residuals (vs pre-fix 30+).

#### 1.5 SOL calibrated-delay efficacy (25s vs 30s)

```sql
WITH pullback_eids AS (
  SELECT e.id, e.decided_at FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long' AND e.ticker='SOL-USD'
)
SELECT
  CASE WHEN p.decided_at < '2026-05-03 16:29:20' THEN 'pre-F8b (30s)' ELSE 'post-F8b (25s)' END AS era,
  COUNT(DISTINCT p.id) AS exits,
  ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps,
  ROUND((100.0 * COUNT(DISTINCT p.id) FILTER (WHERE x.realized_pnl_usd > 0)
         / NULLIF(COUNT(DISTINCT p.id), 0))::numeric, 1) AS win_rate_pct
FROM fast_exits x
JOIN pullback_eids p ON p.id = x.entry_execution_id
GROUP BY era ORDER BY era;
```

Counterfactual predicts SOL post-F8b should be +3.47 bps vs pre-F8b's −2.45 bps — a ~6 bps swing. This SQL tests it on realized data.

#### 1.6 Verdict-grade decay cells with statistical bounds (TERTIARY)

```sql
SELECT ticker, score_bucket, horizon_s, sample_count,
       ROUND(mean_return::numeric * 10000, 2) AS mean_bps,
       ROUND((CASE WHEN sample_count > 1
                   THEN SQRT(m2_return/(sample_count-1))/SQRT(sample_count)
                   ELSE NULL END)::numeric * 10000, 2) AS stderr_bps,
       ROUND((mean_return - 2 * SQRT(GREATEST(m2_return / NULLIF(sample_count - 1, 0), 0))
              / SQRT(NULLIF(sample_count, 0)))::numeric * 10000, 2) AS lower_2sigma_bps,
       ROUND((mean_return + 2 * SQRT(GREATEST(m2_return / NULLIF(sample_count - 1, 0), 0))
              / SQRT(NULLIF(sample_count, 0)))::numeric * 10000, 2) AS upper_2sigma_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long' AND sample_count >= 30
ORDER BY ticker, horizon_s;
```

Track which cells crossed n=30 since soak-2. Watch BTC/SOL at horizons ≥ 5s. If any cell's mean ± 2σ lands fully one-sided, F6.5's calibrated gates start using that signal.

### Decision tree

```
For each of {BTC-USD, SOL-USD}:

  IF post-deploy effective n ≥ 20 AND mean_bps ≥ +1:    -> KEEP in allowlist
  ELIF post-deploy effective n ≥ 20 AND mean_bps ≤ -1:  -> DROP from allowlist
  ELIF post-deploy effective n ≥ 20 AND mean_bps in [-1, +1]: -> Inconclusive at this n; trading-cost noise dominates
  ELIF post-deploy effective n < 20:                    -> Insufficient. Recommend operator wait ≥12h before re-running.

Combine outcomes:
  Both KEEP: F8b validated; consider live-eligibility brief next.
  BTC DROP, SOL KEEP: F8b-tightened (drop BTC); consider F9 in parallel.
  BTC KEEP, SOL DROP: surprising; investigate.
  Both DROP: F9 immediately; full pivot.
  Both inconclusive (after 24h+): pivot to F9 — fade hypothesis isn't producing decisive realized signal even on the strongest subset.
```

### Phase 1 constraints

- **No code commits.**
- **No threshold tuning.**
- **No live placement enable.**
- **No migrations.**
- **Per-ticker analysis is mandatory.** Aggregate is misleading.
- **Realized P/L is the primary verdict lens.**
- **Use IN-subquery for distinct counts.**
- **Cluster-correlation interpretation mandatory** if catchup fills are part of the cohort.

### Phase 1 success criteria

1. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-07_f8b-verification-soak-3.md` per PROTOCOL format.
2. Per-ticker realized P/L on post-deploy cohort reported, with cluster-correlation interpretation applied.
3. Verdict named for each of BTC and SOL using the decision tree.
4. Verbatim verification SQL section reproduces verdict from raw table state.
5. Recommendation for next NEXT_TASK with one-line description.

---

## Phase 2 — bracket-writer-cover-policy-clarify

**Original brief queued 2026-05-03; bumped same day for `bracket-intent-stop-price-live-sync` (DONE 2026-05-03). Re-promoted now.** Code clarity + observability. Behavior-preserving — no runtime decision-logic change.

### Goal

Fix the misleading framing in `bracket_writer_g2.py` that conflates "broker has a working sell" with "the position is protected." Rename audit labels, rewrite docstrings, add startup warning for the silent-exposure flag combo, add status surface for `covered_by_existing_sell` rows, and 6 tests.

Deliverable: `docs/STRATEGY/CC_REPORTS/2026-05-07_bracket-writer-cover-policy-clarify.md`.

### Step 2.1 — Comment + label rewrite in `bracket_writer_g2.py`

**Change A (lines ~680-696, FIX 55 docstring):** Replace "the position is protected — skip placement entirely. The existing limit IS the exit; we don't need to add a stop on top of it" with honest framing: covered-by-limit is **NOT downside protection**; the trade-off is upside lock-in vs downside protection; operators flip via `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1`.

**Change B (lines ~745-755, DEFAULT POLICY block):** Same rewrite of inline DEFAULT vs OPT-IN POLICY comments. Replace "The position is still protected — by the existing limit-sell, just at a different price level" with an honest statement of the trade-off. Keep the DEFAULT-vs-OPT-IN structure.

**Change C (line ~781, audit reason rename):** Rename `:protected_by_limit` → `:no_stop_coverage` (or another phrase that makes clear the row is in the "limit covers position, no stop placed" state, NOT a protected state). Mark terminal-reject reason field is opaque text — this is a rename, not a schema change. Existing rows keep the old label until next sweep rewrites them; do NOT backfill.

**Change D (line ~790, WriterAction.reason):** Leave as `covered_by_existing_sell` — this accurately describes the writer's action.

### Step 2.2 — Startup-time WARNING for silent-exposure flag combo

Wire into `app/main.py` (FastAPI startup) AND broker-sync-worker entrypoint if it has its own startup hook. Emit WARNING if both:
- `settings.chili_bracket_missing_stop_repair_enabled is True`
- `settings.chili_bracket_writer_cancel_covering_sell is False`

```text
[bracket_writer] WARNING: emergency-repair is ENABLED but cancel-covering-sell
is DISABLED. Positions with `held_for_sells == broker_qty` (covered by an
existing limit-sell only) will be skipped by the emergency-repair path and
remain WITHOUT downside protection. Set CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1
to enable cancel-and-place-stop behavior, or accept the upside-lock default.
```

WARNING-level only; do NOT escalate to ERROR or fail startup. This is operator-judgment, not misconfiguration.

### Step 2.3 — Status surface

**Option A (preferred): JSON endpoint `GET /admin/bracket/cover-policy-snapshot`** matching existing admin-router conventions. Returns:

```json
{
  "as_of": "2026-05-07T14:30:00Z",
  "flags": {
    "chili_bracket_missing_stop_repair_enabled": true,
    "chili_bracket_writer_cancel_covering_sell": false
  },
  "rows": [
    {
      "intent_id": 220,
      "trade_id": 1812,
      "ticker": "AIDX",
      "intent_state": "terminal_reject",
      "last_diff_reason": "covered_by_existing_sell:no_stop_coverage",
      "stop_price_local": 0.65,
      "broker_qty": 150,
      "held_for_sells": 150,
      "advisory": "no downside protection; broker has limit-sell only"
    }
  ]
}
```

Read-only. Same auth as other admin routes. The `advisory` field synthesizes a plain-English summary.

**Option B (fallback if no admin-router fit):** document the canonical SQL in the CC report and skip the route. Use judgment.

### Step 2.4 — Tests at `tests/test_bracket_writer_cover_policy_clarify.py`

1. **Audit reason contains new label.** Open trade with `held_for_sells == broker_qty`, default policy. Assert `mark_terminal_reject` called with `reason='covered_by_existing_sell:no_stop_coverage'`.
2. **Old label not regenerated.** Same seed. Assert no `protected_by_limit` string in `last_diff_reason`.
3. **WriterAction reason unchanged.** Assert `WriterAction.reason == 'covered_by_existing_sell'`.
4. **Startup warning fires on the silent-exposure combo.** Mock settings; run startup; assert WARNING emitted with both flag names.
5. **Startup warning does NOT fire when either flag flips.** Three sub-cases (both True / both False / only `cancel_covering_sell=True`).
6. **Status endpoint shape (if Option A).** Mock 2-3 rows; assert JSON includes `advisory` synthesis + flag snapshot.

All tests use `chili_test`.

### Phase 2 constraints

- **No runtime decision-logic change.** Same flag values produce same actions.
- **No `place_missing_stop` decision-tree change.**
- **No live-fast-path safety belt change.** PROTOCOL Hard Rule 1.
- **Do NOT flip `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL` to default True in code.** Operator's call.
- **Do NOT backfill existing `last_diff_reason` rows.** Let rename propagate naturally.
- **No magic numbers.**
- **Tests use `_test`-suffixed DB.**

### Phase 2 success criteria

1. Comments at `bracket_writer_g2.py:680-696` and `:745-755` no longer claim covered-by-limit positions are "protected."
2. Persisted `last_diff_reason` on new `terminal_reject` rows uses the renamed label. Test #1 asserts.
3. Startup warning fires correctly per flag combo. Tests #4-5 assert.
4. Status surface (admin route OR documented SQL) exists.
5. 6 new tests pass against `chili_test`. Existing 9 (stale-label-cleanup) + 7 (emergency-repair) tests still pass.
6. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-07_bracket-writer-cover-policy-clarify.md`.

### Phase 2 open questions for Cowork (surface in CC report only if relevant)

1. **Replacement label for `:protected_by_limit`?** Brief proposes `:no_stop_coverage`. Other options: `:limit_only_coverage`, `:upside_lock_no_stop`, `:no_downside_stop`. Pick what reads best in `last_diff_reason` queries; surface choice.
2. **Admin route or SQL-only?** Pick whichever fits codebase conventions. Surface trade-off if Option B chosen.
3. **Does broker-sync-worker have its own startup hook?** Check during implementation; surface answer.
4. **Should startup warning include a count of rows currently in silent-exposure state?** Adds DB cost; default to flag-state-only if complexity adds friction.

---

## Combined-brief constraints (apply to both phases)

- **One CC_REPORT per phase.** Don't squash into one — they're different domains and different deliverables.
- **One commit per phase** (Phase 1 = docs only; Phase 2 = code + tests + docs in tight series).
- **Push after each phase.** If Phase 2 blocks, Phase 1 still ships.
- **No `git push --force` to main.** PROTOCOL Hard Rule.

## Push & deploy

- Phase 1: zero deploy impact. Pure analysis.
- Phase 2: comments + label rename + startup warning + admin route. Behavior-preserving. After commit + push, restart whichever container hosts the affected paths (likely chili main + broker-sync-worker) to pick up the new comments + warning. Bind-mount means no rebuild.

## Rollback plan

- Phase 1: N/A (no code).
- Phase 2: `git revert` the commit. Label rename, comment rewrite, warning all revert cleanly. Persisted `last_diff_reason` keeps the renamed label until next sweep — no migration backfill needed either way.
