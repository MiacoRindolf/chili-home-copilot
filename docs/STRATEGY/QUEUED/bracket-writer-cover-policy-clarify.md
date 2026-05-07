# QUEUED TASK: bracket-writer-cover-policy-clarify (PROMOTED)

**Promoted to `docs/STRATEGY/NEXT_TASK.md` on 2026-05-07 14:30 UTC as Phase 2 of the combined `f-thread-tail-2026-05-07-2` jumbo brief. `bracket-intent-stop-price-live-sync` shipped 2026-05-03.**

The full Phase 2 body lives in `NEXT_TASK.md`. This file is preserved as a placeholder so the queue history stays linkable; do not edit. If re-queued, restore from `docs/STRATEGY/CC_REPORTS/2026-05-07_bracket-writer-cover-policy-clarify.md` once it ships, or from git history.

---

The original body below is preserved verbatim for reference.

# QUEUED TASK: bracket-writer-cover-policy-clarify

**Originally queued as NEXT_TASK on 2026-05-03. Bumped same day in favor of `bracket-intent-stop-price-live-sync` after the latter's diagnostic surfaced that `bracket_intents.stop_price` is frozen at entry time and out of sync with `trade.stop_loss` for live positions — a more urgent structural gap than the comment/label clarification this task addresses.**

Re-promote after `bracket-intent-stop-price-live-sync` is DONE. The clarification work below is still valid; it's just second-priority now.

---

# NEXT_TASK: bracket-writer-cover-policy-clarify

STATUS: PENDING

## Goal

Fix the misleading framing in `bracket_writer_g2.py` that conflates "broker has a working sell" with "the position is protected." That conflation produced (a) the misleading audit label `covered_by_existing_sell:protected_by_limit`, (b) my own misread in the 2026-05-03 Cowork review (the "$2,107 → $276" walk-back I had to retract), and (c) the silent-exposure steady state where `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=1` + `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0` together produce "rejection storm avoided, downside still uncovered" without any operator-visible signal.

The actual semantics: when a position has only a take-profit **limit-sell** order at a higher price than current, it is **not protected on downside.** A limit-sell at $1.50 doesn't trigger if price falls to $0.80. The bracket writer's default policy of preserving the limit (skipping stop placement) is a deliberate trade-off — upside lock-in vs downside protection — but the code calls this "protected" and that's wrong.

This task ships:

1. Honest comments + audit reasons describing the trade-off accurately.
2. A startup-time WARNING when the silent-exposure flag combination is set.
3. A status query / endpoint surface for `covered_by_existing_sell` rows so operators can see at a glance which positions chose upside lock-in over downside protection.
4. Tests asserting the new naming + warning emission.

This task ships **code clarity + observability**, not behavior change. The runtime decision logic stays identical — the operator's `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` escape hatch remains the way to flip from upside-lock to downside-stop.

Deliverable: `docs/STRATEGY/CC_REPORTS/<date>_bracket-writer-cover-policy-clarify.md`.

## Why now

Pulled from the 2026-05-03 review of `bracket-intent-stale-label-cleanup` (`docs/STRATEGY/COWORK_REVIEWS/2026-05-03_bracket-intent-stale-label-cleanup.md`). The framing bug is upstream of the audit's risk-classification error and Cowork's misread; future audits will repeat the mistake unless the code's own self-description matches reality.

The 5 positions still exposed as of this writing (AIDX 1812, CCCC 1813, CRDL 1814, TLS 1821, VFS 1822) need operator action via flag flip BEFORE Monday 2026-05-04 13:30 UTC market open — that is a short-term ops issue and is not part of this task. This task is the long-term fix that prevents the misread from recurring.

## Step 1 — Comment + label rewrite

### File: `app/services/trading/bracket_writer_g2.py`

#### Change 1.1 — `lines ~680-696` (FIX 55 docstring header)

Replace the misleading framing. The current text says:

> "FIX 55 catches the case at the source: if all shares are already covered by an existing sell order, the position is protected — skip placement entirely. The existing limit IS the exit; we don't need to add a stop on top of it."

Rewrite to something honest. Suggested phrasing (CC may refine):

> "FIX 55 catches the case at the source: if all shares are already committed to an existing limit-sell (typically a take-profit), placing a SELL_STOP on top would require canceling the limit (since the broker rejects placements when `held_for_sells == quantity`). The default policy preserves the limit and skips the stop. **THIS IS NOT DOWNSIDE PROTECTION** — a take-profit limit at a higher price than current does nothing if price falls. The trade-off is deliberate: upside lock-in vs downside protection. Operators who want downside protection can set `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` to flip the policy: cancel the limit, place the stop. See operator runbook in docs."

#### Change 1.2 — `lines ~745-755` (DEFAULT POLICY block)

Same rewrite of the inline DEFAULT POLICY / OPT-IN POLICY comments. Replace "The position is still protected — by the existing limit-sell, just at a different price level" with an honest statement of the trade-off. Keep the structure (DEFAULT vs OPT-IN); just fix the truth value of the framing.

#### Change 1.3 — Audit reason rename

`bracket_writer_g2.py:781` currently:

```python
_mtr(db, int(bracket_intent_id), reason="covered_by_existing_sell:protected_by_limit")
```

Rename `:protected_by_limit` to `:no_stop_coverage` (or another phrase that makes clear the row is in the "limit covers position, no stop placed" state — not in a protected state). The `mark_terminal_reject` reason field is opaque text; this is a rename, not a schema change.

Note: this label appears in `trading_bracket_intents.last_diff_reason` for live rows. After rename, existing rows will keep the old label until the next sweep rewrites them. Don't backfill — let the rename propagate naturally as rows transition. Document the propagation behavior in the CC_REPORT.

#### Change 1.4 — WriterAction `reason`

`bracket_writer_g2.py:790` currently `reason="covered_by_existing_sell"`. This is fine — it accurately describes the writer's *action* (skipped because covered). Leave unchanged. The label clarification is for the persisted state on `bracket_intents.last_diff_reason`, not the in-memory `WriterAction.reason`.

## Step 2 — Startup-time WARNING for silent-exposure flag combo

### Where the change lives

`app/main.py` (or wherever the FastAPI startup wiring lives) — emit a WARNING-level log line at app startup IF both:

- `settings.chili_bracket_missing_stop_repair_enabled is True`
- `settings.chili_bracket_writer_cancel_covering_sell is False`

Suggested log line:

```text
[bracket_writer] WARNING: emergency-repair is ENABLED but cancel-covering-sell
is DISABLED. Positions with `held_for_sells == broker_qty` (covered by an
existing limit-sell only) will be skipped by the emergency-repair path and
remain WITHOUT downside protection. Set CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1
to enable cancel-and-place-stop behavior, or accept the upside-lock default.
```

The warning fires on every app boot. Do NOT escalate to ERROR or fail startup — this is an operator-judgment-call combination, not a misconfiguration.

If the broker-sync-worker has its own startup hook (separate from the main FastAPI app), the warning should fire from there too. Check `scripts/start-broker-sync-worker.py` (or wherever the worker entrypoint is) and emit the same warning if both flags are set as observed.

## Step 3 — Status surface

### Option A (preferred): JSON endpoint

Add `GET /admin/bracket/cover-policy-snapshot` (or similar; pick the route convention used by other admin/diagnostic endpoints in `app/routers/`) that returns:

```json
{
  "as_of": "2026-05-03T22:58:23Z",
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

Read-only query. No mutations. Same auth model as other admin routes. The advisory field synthesizes a plain-English summary for operator scanning.

### Option B (fallback if no admin-router precedent fits): SQL pattern in CC_REPORT

If wiring an admin route is heavier than the value warrants, document the canonical SQL in the CC_REPORT instead and skip the route:

```sql
SELECT bi.id AS intent_id, bi.trade_id, t.ticker,
       bi.state AS intent_state, bi.last_diff_reason,
       bi.stop_price, bi.quantity AS local_qty,
       t.status AS trade_status
FROM trading_bracket_intents bi
JOIN trading_trades t ON t.id = bi.trade_id
WHERE bi.last_diff_reason LIKE 'covered_by_existing_sell%'
  AND t.status = 'open'
ORDER BY bi.updated_at DESC;
```

Use judgment — the admin route is preferred but not mandatory.

## Step 4 — Tests

Add `tests/test_bracket_writer_cover_policy_clarify.py` covering:

1. **Audit reason contains new label.** Seed: open trade with `held_for_sells == broker_qty`, default policy. Call `place_missing_stop`. Assert: `mark_terminal_reject` was called with `reason='covered_by_existing_sell:no_stop_coverage'` (or whatever name was chosen).
2. **Old label not regenerated.** Same seed. Assert: no string `protected_by_limit` is written anywhere in the test transaction. Use a string match against the persisted row's `last_diff_reason`.
3. **WriterAction reason unchanged.** Same seed. Assert: returned `WriterAction.reason == 'covered_by_existing_sell'` (we left this alone).
4. **Startup warning fires on the silent-exposure combo.** Mock the settings. Run startup. Assert: a WARNING log line containing both flag names was emitted.
5. **Startup warning does NOT fire when either flag flips.** Three sub-cases:
   - Both True → no warning
   - Both False → no warning
   - Only `cancel_covering_sell=True` → no warning
6. **Status endpoint returns expected shape (if Option A).** Mock the DB to seed 2-3 rows; call the endpoint; assert the JSON includes the `advisory` synthesis and the flag snapshot.

All tests use `chili_test`.

## Brain integration (reuse, don't rewrite)

- `bracket_intent_writer.mark_terminal_reject` — the existing single-writer entry point. Reuse for the renamed reason; do not bypass.
- `WriterAction` — unchanged shape and semantics.
- Settings access via `settings.chili_bracket_missing_stop_repair_enabled` + `settings.chili_bracket_writer_cancel_covering_sell` — already defined in `app/config.py`. Read them at startup, not at each call site.
- Existing admin router conventions in `app/routers/admin.py` (or equivalent) — match style for new endpoint if Option A.

## Constraints / do not touch

- **Do not change runtime decision logic.** This task is comments + labels + warning + observability. The DEFAULT POLICY behavior stays identical.
- **Do not modify `place_missing_stop`'s decision tree.** Same flag values produce same actions.
- **Do not modify the live-fast-path safety belts.** PROTOCOL Hard Rule 1.
- **Do not flip `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL` to default True in code.** That decision belongs to the operator. The startup warning makes the trade-off visible; the operator decides.
- **Do not backfill `last_diff_reason` on existing `bracket_intents` rows.** Let the rename propagate naturally on next sweep.
- **No magic numbers.**
- **Tests use `_test`-suffixed DB.**

## Out of scope

- The actual operator action to protect AIDX/CCCC/CRDL/TLS/VFS via the flag flip + Monday morning sweep. That's an ops step, not a code task.
- Investigating WHY the original SELL_STOP submissions hit `terminal_reject` on 2026-05-01 (PDT? Short-sale restriction? Broker glitch?). Out of scope; separate investigation if appetite arises.
- Any change to the `kind=missing_stop` classifier semantics. The classifier is correct; the writer's framing is the issue.
- Promoting `broker_stop_order_id` to authority. Mirror stays advisory.
- Auto-flipping `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL` based on position metrics (e.g., "if position is down N%, flip to downside-protection mode"). That's a meaningful policy change; belongs in a separate task.

## Success criteria

1. Comments at `bracket_writer_g2.py:680-696` and `:745-755` no longer claim covered-by-limit positions are "protected." New phrasing names the trade-off honestly.
2. Persisted `last_diff_reason` on new `terminal_reject` rows uses the renamed label (e.g., `:no_stop_coverage`). Test #1 asserts this.
3. Startup warning fires when the silent-exposure flag combo is set; does not fire otherwise. Tests #4-5 assert this.
4. Status surface (admin route OR documented SQL pattern) exists for `covered_by_existing_sell` rows.
5. All 6 new tests pass against `chili_test`. Existing 9 (stale-label-cleanup) + 7 (emergency-repair) tests still pass — no regression.
6. CC_REPORT written at `docs/STRATEGY/CC_REPORTS/<date>_bracket-writer-cover-policy-clarify.md`. One commit (or tight series), pushed.

## Open questions for Cowork (surface in your report only if relevant)

1. **What's the right replacement label for `:protected_by_limit`?** I proposed `:no_stop_coverage`. Other reasonable options: `:limit_only_coverage`, `:upside_lock_no_stop`, `:no_downside_stop`. Pick what reads best in `last_diff_reason` queries; surface the choice.
2. **Admin route or SQL-only?** Pick whichever fits the codebase's conventions. If Option B (SQL only) is chosen, surface the trade-off so we can decide whether to revisit later.
3. **Does the broker-sync-worker have its own startup hook for the warning, or does it inherit from the FastAPI app?** Check during implementation; surface the answer.
4. **Should the startup warning also include a count of rows currently in the silent-exposure state?** Adding a DB query at startup is more expensive but more useful (zero-noise on green deployments, loud signal when there are stuck rows). Surface the implementation cost; default to flag-state-only if the count query adds complexity.

## Rollback plan

- **Code rollback:** `git revert <this commit>`. The label rename, comment rewrite, and warning all revert cleanly.
- **Persisted-data rollback:** Not needed. Old rows keep `:protected_by_limit` until next sweep; new code rewrites to `:no_stop_coverage`. After revert, new code rewrites back. The reason field is opaque text; consumers should not switch behavior on its content.
- **Status endpoint rollback (if Option A):** Removing the route is harmless — read-only endpoint.
- **Startup warning rollback:** Removing it returns to silent default. No state side effect.

This task makes no broker calls.
