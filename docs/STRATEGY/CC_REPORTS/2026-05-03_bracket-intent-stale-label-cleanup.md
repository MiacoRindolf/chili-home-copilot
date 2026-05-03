# CC_REPORT: bracket-intent-stale-label-cleanup

## What shipped

- **Commit `0775ae0`** — `fix(bracket): mirror broker_stop_order_id + auto-reconcile terminal_reject` (code + tests).
- **This commit** — `docs(strategy): bracket-intent-stale-label-cleanup CC report + flag flip`. Adds `CHILI_BRACKET_INTENT_MIRROR_ENABLED=1` to the `broker-sync-worker` service in `docker-compose.yml`, marks `NEXT_TASK.md` DONE, ships this report.
- Files touched: `app/config.py`, `app/services/trading/bracket_intent_writer.py`, `app/services/trading/bracket_reconciliation_service.py`, `tests/test_bracket_intent_stale_label_cleanup.py` (new), `docker-compose.yml`.
- Migrations added: **none** (schema unchanged; `broker_stop_order_id` already existed as nullable since the model was first introduced).

## Code

### Two new writers in `bracket_intent_writer.py`

1. **`sync_broker_stop_order_id_mirror(db, intent_id, *, broker_value) -> tuple[bool, str | None]`**
   - Idempotent UPDATE-when-changed helper. Reads current value, compares (NULL-normalized), UPDATEs only on diff. Returns `(changed, prev_value)`.
   - Documents the **advisory cache** contract: decision-time consumers MUST keep reading `BrokerView`. Test #8 enforces.

2. **`mark_auto_reconciled_after_terminal_reject(db, intent_id) -> bool`**
   - Raw-SQL bypass with `WHERE intent_state = 'terminal_reject'` precondition. Idempotent by construction (no row matches if already in any other state).
   - Sets `last_diff_reason='auto_reconciled_after_terminal_reject'`, bumps `last_observed_at` and `updated_at`.
   - Does NOT relax the standard `_LEGAL_TRANSITIONS` table (the FIX-52 cooldown guard for genuinely-rejected intents is preserved). The transition is the explicit, named, audited bypass.

### Sweep-loop hook in `bracket_reconciliation_service.py`

`_apply_intent_mirror_writes(db, *, sweep_id, mode, local, broker, decision)` — called from BOTH sweep loops (`_stage_log_all` and `_run_sweep_legacy`) AFTER the classifier returns and BEFORE `_invoke_writer_for_decision`. Three early-return guards: flag OFF, no `bracket_intent_id`, broker unavailable.

When firing:
1. Mirror write — UPDATE `broker_stop_order_id` only when local ≠ broker (NULL-aware). Emits info log `mirror_write` (added/updated) or `mirror_clear` (NULL'd) when the column actually moves.
2. Auto-transition — gated by `decision.kind == "agree"` AND `local.intent_state == "terminal_reject"`. Calls the writer; on success emits **CRITICAL log** + `_g2_event(writer="auto_reconcile_terminal_reject")` audit row.

Failure paths swallow exceptions at debug log level — both effects are advisory; sweep continues.

### Feature flag

`app/config.py`: `chili_bracket_intent_mirror_enabled: bool = Field(default=False, validation_alias=AliasChoices("CHILI_BRACKET_INTENT_MIRROR_ENABLED"))`. Default OFF.

## Verification

### Test results
**9 of 9 new tests pass** in 422s against `chili_test`. Run command: `pytest tests/test_bracket_intent_stale_label_cleanup.py -v -p no:asyncio`.

| # | Test | Status |
|---|---|---|
| 1 | mirror write on agree with NULL local + broker value | ✅ |
| 2 | mirror write when local stale, broker different | ✅ |
| 3 | mirror clear on missing_stop with broker NULL | ✅ |
| 4 | no mirror write when broker unavailable | ✅ |
| 5 | no-op when both sides agree (`updated_at` unchanged) | ✅ |
| 6 | auto-transition idempotent on already-reconciled | ✅ |
| 7 | flag OFF preserves prior behavior | ✅ |
| 8 | authority contract canary (static grep — no decision-time reads of `broker_stop_order_id`) | ✅ |
| 9 | no auto-transition for non-terminal_reject states | ✅ |

**Regression check**: 7 of 7 prior `audit-missing-stop-emergency-repair` tests still pass. No interaction issue between the new sweep-loop hook and the existing emergency-repair branch.

### Live deploy timeline (2026-05-03 UTC)
1. Code committed at `0775ae0`, will push after this report.
2. `docker compose restart broker-sync-worker` — picked up new code via `./app:/app/app` mount, flag still OFF.
3. **Sweep `0ca01957...`** at 22:56:49 — baseline confirmed: no `mirror_write` / `auto_reconcile` lines, identical state_gated_skip output for the stuck rows. Behavior unchanged with new code + flag OFF.
4. Flag flipped: added `CHILI_BRACKET_INTENT_MIRROR_ENABLED=1` to `docker-compose.yml`. `docker compose up -d --force-recreate --no-deps broker-sync-worker`.
5. **Sweep `b68bf08a...`** at 22:58:23 — first post-flip sweep. Captured events (truncated):
   - `mirror_write trade=1815 ticker=EKSO old=NULL new=69f4badf-…` (other healthy open trade)
   - `mirror_write trade=1816 ticker=ELTX old=NULL new=69f7c5b8-…` (the stop placed by emergency-repair earlier today)
   - **`[CRITICAL] auto_reconcile trade=1816 ticker=ELTX from=terminal_reject to=reconciled`** ✅
   - `mirror_write trade=1817 ticker=GEO old=NULL new=69f4bae2-…`
   - `mirror_write trade=1818 ticker=IMTX old=NULL new=69f53eaf-…` (broker-side stop discovered)
   - **`[CRITICAL] auto_reconcile trade=1818 ticker=IMTX from=terminal_reject to=reconciled`** ✅
   - `mirror_write trade=1819 ticker=JOB old=NULL new=69f4bb22-…`
   - `mirror_write trade=1820 ticker=PED old=NULL new=69f4bd38-…`
6. **Sweep `213de036...`** at 22:59:17 — steady state. ELTX/IMTX no longer producing state_gated_skip log lines. AIDX/CCCC/CRDL/TLS/VFS still throttled+skipped (see "Surprise" below).

### SQL pre/post diff

**Pre-deploy** (all 7):
```
intent_state='terminal_reject', broker_stop_order_id=NULL, last_diff_reason='missing_stop:error'
```

**Post-deploy**:
| intent | trade | ticker | intent_state | broker_stop_order_id | last_diff_reason |
|---|---|---|---|---|---|
| 220 | 1812 | AIDX | terminal_reject | NULL | missing_stop:error |
| 221 | 1813 | CCCC | terminal_reject | NULL | missing_stop:error |
| 222 | 1814 | CRDL | terminal_reject | NULL | missing_stop:error |
| **224** | **1816** | **ELTX** | **reconciled** ✅ | **69f7c5b8-7e15-4176-a31f-1544696055d5** ✅ | **auto_reconciled_after_terminal_reject** ✅ |
| **226** | **1818** | **IMTX** | **reconciled** ✅ | **69f53eaf-b8d6-4f9a-a2ee-773daa92bd6d** ✅ | **auto_reconciled_after_terminal_reject** ✅ |
| 229 | 1821 | TLS | terminal_reject | NULL | missing_stop:error |
| 230 | 1822 | VFS | terminal_reject | NULL | missing_stop:error |

**2 of 7 transitioned to reconciled** (ELTX, IMTX). 5 of 7 remain terminal_reject — see Surprise #1 below.

### Audit events
Two `trading_execution_events` rows written with `writer='auto_reconcile_terminal_reject'`, one for ELTX and one for IMTX. `trade_id` is NULL on these rows because `_g2_event` deliberately passes `trade=None` to `record_execution_event` (per the safety comment about preventing `apply_execution_event_to_trade` from corrupting `Trade.status`). The ticker + writer fields are the audit identifier.

## Surprises / deviations

### 1. Brief's "all 7 rows transition" expectation was based on classifier `kind=agree` for all 7 — only 2 actually classify as agree
The brief's success criterion #4 stated *"All 7 rows: state='reconciled', last_diff_reason='auto_reconciled_after_terminal_reject'"*. Reality:

- **ELTX 1816**: classifier returns `kind=agree`. Reason: emergency-repair task placed a real stop-typed order at 22:01 (`69f7c5b8...`), so broker reports `stop_order_state='queued'`. Auto-transition fires. ✅
- **IMTX 1818**: classifier returns `kind=agree`. Reason: broker discovered a working stop (`69f53eaf...`) — likely the original stop placement that the local row's `terminal_reject` label said had been rejected. Auto-transition fires. ✅
- **AIDX 1812 / CCCC 1813 / CRDL 1814 / TLS 1821 / VFS 1822**: classifier returns `kind=missing_stop` (NOT agree). Reason: the prior emergency-repair sweep's `covered_by_existing_sell` finding showed `held_for_sells == broker_qty`, but those covering sells are **limit-sell orders** (the original target leg of the bracket), not stop-typed orders. From the classifier's POV, `broker.stop_order_state is None` → `broker_has_stop = False` → `kind=missing_stop`. Auto-transition correctly does NOT fire because the contract is "broker reports a working stop matching the intent."

This is **structurally correct behavior**. The 5 stuck rows are not actually "missing stops" in the dangerous sense — they have working limit-sell coverage at the broker. But the classifier's `missing_stop` semantic is "no stop-typed order on the books," and that is true. The auto-transition guard is conservative and correct.

The 5 will reconcile naturally when:
- the covering limit-sell fills → trade closes → reconciler closes the intent (existing path), OR
- operator manually swaps the limit-sell for a stop-loss at the broker → next sweep classifies as agree → auto-transition fires.

I'm not surfacing a one-shot SQL fix as a recommendation because the genuine `terminal_reject` cooldown (FIX-52) protects against silent self-healing of intents that should remain failed. The auto-transition path is the right gate; the 5 just don't meet it.

### 2. Mirror-write fires for the entire open-trade population, not just the 7 stuck rows
Sweep `b68bf08a` produced 6 `mirror_write` log lines: ELTX, IMTX (the two stuck), plus EKSO, GEO, JOB, PED (4 healthy open trades whose local mirror was also NULL). All 6 were `old=NULL new=<broker_id>` — the column had never been populated before because no writer existed for it. Steady state: subsequent sweeps will be silent (no-op when local matches broker).

This is the intended one-shot backfill effect. Future audits looking at `broker_stop_order_id IS NULL` will get accurate signal instead of ~85% false-alarm noise (the original audit's pattern).

### 3. Stale `LocalView.intent_state` produces transient `state_gated_skip` log lines on the auto-reconcile sweep
On the same sweep where auto-transition fires (e.g., ELTX/IMTX in `b68bf08a`), the subsequent `_invoke_writer_for_decision` call still sees `local.intent_state='terminal_reject'` because `LocalView` is a frozen dataclass loaded at sweep start. So a `state_gated_skip` writer_action is logged after the auto_reconcile CRITICAL line, for the same trade, on the same sweep.

This is cosmetic — the next sweep reloads `LocalView` and the row classifies as agree without the skip. Sweep `213de036` confirmed this: no state_gated_skip entries for ELTX or IMTX.

Could be fixed by mutating `LocalView` after auto-transition, but that breaks frozen-dataclass semantics. Could be fixed by re-querying inside `_invoke_writer_for_decision`, but that doubles DB reads on every sweep for an issue that self-heals in 2 minutes. Not worth either trade-off; flagging for visibility.

## Deferred

- **Cleanup for the 5 surviving `terminal_reject` rows** (AIDX, CCCC, CRDL, TLS, VFS). They will resolve naturally when their covering limit-sells fill. No code action recommended; operator can monitor or take broker-side action if they want stop-typed coverage instead of limit coverage.
- **Promoting `broker_stop_order_id` to authority**: explicitly out of scope per the brief and Open Q #1 in the prior CC report. Mirror stays advisory.
- **Fixing the cosmetic state_gated_skip lines** for the same-sweep transition (Surprise #3 above).
- **`trade_id` populated on `auto_reconcile_terminal_reject` audit events**: would require an extension to `_g2_event` since the `trade=None` safety guard intentionally suppresses it. Workaround for queries: filter on `payload_json->>'writer' = 'auto_reconcile_terminal_reject'` + `ticker`.

## Open questions for Cowork

1. **Should the 5 surviving terminal_reject rows be cleaned up via operator action, or left to natural exit?** Both paths are safe — broker has working coverage in either case. Operator preference question, not a code question.
2. **Should mirror_write log lines be downgraded for the steady-state case?** Currently info-level, fires on first-time backfill (today's sweep had 6 lines for non-stuck rows). After backfill, steady-state will be silent. If the population grows large enough that periodic broker_stop_order_id rotations cause churn, debug might be more appropriate. Recommend: leave at info for now; revisit if ops noise becomes real.
3. **Is the `kind=missing_stop` classification of "broker has limit-sell covering full position but no stop-typed order" actually the right signal?** The prior emergency-repair task discovered this via `covered_by_existing_sell`. If the writer treats covering limit-sells as protection (refuses to place duplicate stops), maybe the classifier should also treat them as a working leg — in which case the 5 rows would have classified as `kind=agree` and auto-reconciled too. This is a separate task class (classifier semantics for non-stop covering orders); surfacing for Cowork judgment.
4. **`f8b-verification-soak-3` re-promotion timing.** Brief notes it's queued for re-promotion on/after 2026-05-04 16:30 UTC. Not affected by today's task. Just confirming Cowork will queue it next.

## Rollback plan

1. Set `CHILI_BRACKET_INTENT_MIRROR_ENABLED=0` in `docker-compose.yml`. `docker compose up -d --force-recreate --no-deps broker-sync-worker`.
2. New code becomes a no-op. Pre-existing `mark_reconciled` / `bump_last_observed` / `_invoke_writer_for_decision` behavior resumes.
3. Already-mirrored `broker_stop_order_id` values stay populated. They are correct (broker truth at time of write); leaving them is harmless because no decision-time consumer reads them (test #8 enforces).
4. Already-transitioned `intent_state='reconciled'` rows (ELTX, IMTX) stay reconciled. They are also correct (broker has working stops). Reverting to `terminal_reject` would be a regression.
5. Code revert: `git revert 0775ae0 && git revert <this commit>`.

No live-broker rollback needed — this task makes no broker calls.
