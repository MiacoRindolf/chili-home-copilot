# Cowork Review: f-partial-profit-wire-up

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-05_f-partial-profit-wire-up.md`
**Reviewer:** Cowork.
**Date:** 2026-05-05.

## Verdict

One commit, all 9 brief steps executed, 13/13 tests pass, 248/248 prior
exit-engine tests still pass, migration 226 applied cleanly. **Approve.**

The single most important judgment call: **Surprise #5 — moving the
partial emission AFTER BOS in `compute_live_exit_levels`.** The brief
placed it where the dead `partial_profit_eligible` block had lived (after
target, before time_decay/BOS), but Claude Code spotted that `result["action"]
== "hold"` is checked before BOS would have set its action — meaning a
position that hits 1R AND breaks structure on the same bar would have
emitted `partial` instead of `exit_bos`. **This is a real ordering bug
the brief introduced; Claude Code caught and corrected it.** Test #3
(`terminal_preempts_partial`) validates the corrected ordering. That's
the kind of catch that makes the difference between shipping the feature
working vs shipping it subtly wrong.

## What Claude Code did right

1. **Caught and fixed the brief's ordering bug.** Surprise #5 above.
   The brief's pseudocode had partial emission positioned before BOS;
   the `result["action"] == "hold"` gate would have leaked partial fires
   over what should have been BOS exits. Claude Code reordered without
   asking — correct because the priority discipline was already
   explicit in the brief ("partial fires only when the position would
   otherwise hold"). This is the right kind of executor-side judgment.

2. **Caught and fixed the table-name typo.** Brief said `paper_trades`;
   actual is `trading_paper_trades`. Migration uses the correct name.
   Worth noting in the briefing-cookbook for future SQL snippets — the
   `trading_*` prefix is consistent across the trading domain (see
   `trading_trades`, `trading_bracket_intents`, etc.).

3. **Caught the integer-quantity bug and widened proactively.**
   `PaperTrade.quantity` was `Integer` with default `1`. With
   `fraction=0.5`, integer math produces `0` — feature would have been
   silently broken. Migration 226 widens to `DOUBLE PRECISION`.
   Verified via Grep that all consumers are multiplicative (P&L,
   commission), no `range()` or int-specific ops. Existing rows
   promote losslessly. The schema-type-change risk is named openly in
   Surprise #2 + Open Question #2 so it's not a quiet drift.

4. **Refused to put paper-mode logic in `broker_service.py`.**
   Surprise #4. Brief's Step 6 suggested
   `broker_service.place_partial_close`; Claude Code observed that
   `broker_service.py` is exclusively live-broker primitives, and
   putting paper-mode code there would mix concerns. Routed to
   `paper_trading.py` next to `_close_paper_trade` and
   `check_paper_exits` — correct module split. Live-mode partial gets
   a separate brief later, with a thin live wrapper that delegates to
   the existing `place_sell_order(ticker, partial_qty)` (which
   already accepts a quantity parameter — verified by Grep, no new
   broker primitive needed).

5. **Refused live-mode partials cleanly.**
   `place_partial_close` returns `live_partial_not_yet_supported` for
   `Trade` (live) instances. Tested. The brief said live-mode is out
   of scope; refusing at the helper level prevents accidental routing.

6. **Found there's no paper-balance ledger.** Surprise #3. Brief said
   "credit the partial proceeds to the paper-balance ledger" — Claude
   Code grep'd, found no such ledger exists, and matched the existing
   pattern (mutate the row in place + the eventual full close
   computes its own pnl against the smaller quantity correctly).
   **Honest framing: the brief was wrong on this point**, and the
   surface area for "what should the partial do?" was solved by
   reading what `_close_paper_trade` already does. No fictional ledger
   was created.

7. **Test #10 sanity-guards against drift.** A test that asserts
   `partial_profit_eligible` is no longer set on the result dict.
   Catches future regressions where someone might re-add the dead
   flag thinking it's missing.

8. **`run_exit_engine` backward-compat preserved.** The `actions` key
   keeps its legacy meaning (terminal closes only); `partial_actions`
   is a new key. Existing consumers of the old contract don't change
   behaviour. Surface area expanded, not changed.

9. **Migration ID protection.** `verify-migration-ids.ps1` ran clean
   (`OK: 226 migrations, 0 retired; no ID collisions`). Schema check
   post-apply confirmed all 8 new columns + 2 indexes + the quantity
   widening landed.

## Findings

### The feature is now operationally real

Before this task, every `partial_at_1r=True` setting in the codebase
was a no-op. Canonical emitted the action; nothing acted on it. **Now
the wiring is end-to-end:** `exit_evaluator → compute_live_exit_levels →
run_exit_engine → _run_paper_trade_check_job → place_partial_close →
DB row updated + log line emitted.** The first time a paper position on
a `partial_at_1r=True` pattern reaches 1R, the audit trail will show
the partial.

What's *still* opt-in: actually setting `partial_at_1r=True` on any
pattern. That's deliberate per the brief — no silent default change.
Operator manually toggles via the SQL in Step 9 when ready to run a
smoke test.

### Surprise #5 is the load-bearing observation

The reordering of the partial block AFTER BOS is the kind of correction
that justifies the executor's judgment latitude. If Claude Code had
followed the brief's pseudocode literally, every position that hit 1R
*and* broke structure on the same bar would have logged `partial`
instead of `exit_bos`. That would have:

- Mislabeled a real terminal exit as a partial.
- Left the position open (partial doesn't close) when it should have
  closed.
- Quietly distorted post-cutover analytics that group by `reason_code`.

The brief's pseudocode had the bug; the executor caught it. This is
the right operating mode of the protocol — the brief is the default,
the executor is empowered to deviate when the default is wrong, and
the deviation lands in Surprises with the reasoning visible.

### `PaperTrade.quantity` widening is the one residual schema concern

Existing data promotes losslessly (`1 → 1.0`); all in-repo consumers
are multiplicative; no behavioural risk identified. **But it IS a
schema type change**, and any out-of-repo script that reads
`trading_paper_trades.quantity` with a strict integer assumption (e.g.,
external dashboards, ad-hoc analysis notebooks, downstream ETL) will
behave differently. Likelihood is low — paper trades are an internal
audit table with no obvious external consumer — but flagging.

If we ever discover a consumer that breaks, the fix is one type cast at
the consumer side, not a rollback of this task.

### `place_partial_close` location is the right call

The brief's `broker_service.place_partial_close` would have created a
single-entry-point dispatcher (paper vs live), but that violates the
established module split where `broker_service.py` is **exclusively**
live-broker API primitives and `paper_trading.py` is **exclusively**
paper-mode logic. Claude Code preserved that discipline.

When the live-mode partial brief lands, the live `place_partial_close`
wrapper will go in `broker_service.py` and delegate to existing
`place_sell_order(ticker, partial_qty)`. The split mirrors how
`_close_paper_trade` (paper) and live-broker close calls already
co-exist as parallel paths.

### Live-mode partial is correctly deferred

The `live_partial_not_yet_supported` refusal is an explicit kill-switch.
A future brief enabling live partials needs:
1. The live `place_partial_close` wrapper in `broker_service.py`.
2. Fast-path safety-belt review (PROTOCOL Hard Rule 1).
3. Confirmation that a partial is a SELL on an open position and
   doesn't fall outside any belt (likely fine, but explicit).

Open Question #5 surfaces this for an explicit answer when the live
brief gets queued.

## Answers to the Open Questions

### 1. Brief assumed `paper_trades` table name

**Acknowledged.** Adding to my Cowork-side cookbook:
**always prefix with `trading_*` for SQL in this domain.** The pattern
is consistent across `trading_trades`, `trading_bracket_intents`,
`trading_position_events`, `trading_paper_trades`, etc. Future briefs
will follow the convention.

### 2. `PaperTrade.quantity` Integer→Float widening

**Accept.** No in-repo behavioural risk identified per Claude Code's
Grep. Out-of-repo consumers are unlikely (paper trades aren't surfaced
externally), but if any break, the fix is at the consumer side. Worth
a one-line note in the migration history if we ever build an
external-consumer registry.

### 3. `place_partial_close` location

**Stay in `paper_trading.py`.** The module split is correct. Live
wrapper goes in `broker_service.py` when live-mode lands, delegating to
the existing `place_sell_order(ticker, partial_qty)` per Claude Code's
research. No unified dispatcher needed; the call site in
`_run_paper_trade_check_job` already routes by trade type
(`isinstance(trade, PaperTrade)` vs `Trade`).

### 4. No paper-balance ledger

**Accept.** Matches existing `_close_paper_trade` pattern — pnl is
computed at full close against the (now smaller) quantity. The
`partial_taken_*` columns are the audit trail. If a separate paper-cash
ledger ever gets introduced, the partial path needs an entry there too;
that's a follow-up brief, not a gap in this task.

### 5. Live-mode safety review for partials

**Defer to the live-mode partials brief.** My read matches the brief's
expectation: a partial is a SELL on an open position, allowed under
existing belts. But "explicit confirmation when the live brief is
queued" is the right discipline. No premature green-light from this
review.

### 6. Single partial per trade

**Accept the bool for now.** If future pattern data shows stacked
partials (33% at 1R + 33% at 2R) move the needle, the schema rework is
either a counter (`partial_count INT`) or a separate
`trading_partial_fills` table with a row per partial fill. Either is
straightforward; doesn't need to ship now.

## Engineering concerns (smaller)

1. **The `[partial_profit_ops]` log prefix is new.** Existing prefixes
   include `[exit_engine_ops]`, `[bracket_writer_g2]`,
   `[bracket_reconciliation_ops]`. The new one fits the convention.
   Future operator dashboards / log-grep scripts can subscribe to the
   prefix to see partial activity in aggregate.

2. **`partial_close_fraction` defaults to 0.5 in `_load_exit_config`.**
   Half-off-at-1R is the textbook rule and matches what the brief
   recommended. Override-able per pattern via `exit_config`. No magic
   number drift; no new constant in code.

3. **Test execution time of 1157.96s** is the truncate-per-test cost on
   the large schema, not a regression. Existing fixture pattern; not a
   new problem from this task.

4. **The pre-existing `_trade_phantom_close_guard` listener** is still
   in the working tree unstaged. Carry-forward from earlier sessions;
   not this task's concern. Same disposition as prior CC reports.

## State of the world after f-partial-profit-wire-up

- 15 protocol runs landed clean.
- 1 commit + 1 migration this run; 6 files touched + 1 new test file.
- Partial-profit-taking is operationally real for the first time. Opt-in
  per pattern; no default change.
- 13 new tests + 248 existing exit-engine tests all pass.
- Live-mode partials explicitly deferred behind a kill-switch return.
- The two queued briefs (`f-time-decay-unit-fix`,
  `f-exit-parity-metric-v2`) remain in `QUEUED/`.

## Decisions confirmed

- **Approve and ship.** All 9 brief steps + 6 surprises landed clean.
- **`PaperTrade.quantity` widening accepted** — schema type change with
  no identified behavioural risk.
- **`place_partial_close` in `paper_trading.py`** is the right module
  split.
- **Live-mode partials deferred** to a separate brief with safety-belt
  review.
- **Brief-cookbook update**: prefix all SQL table names with
  `trading_*` in the trading domain.

## Next move

Three reasonable directions:

**Path A — smoke verification first.** Before queueing the next brief,
operator runs the smoke setup query from the CC report (Step 9) on one
chosen pattern, watches for the first `[partial_profit_ops]` log line,
and confirms the audit trail in `trading_paper_trades`. ~30 min of
attention; closes out the deferred Smoke step concretely.

**Path B — re-promote `f-time-decay-unit-fix` from QUEUED.** The latent
bug is still real; ship while context is fresh. ~1-day execution.

**Path C — wait on f-exit-parity-persist data accumulation,** then
re-promote `f-exit-parity-metric-v2` when 24-48h of data is available.

**My read: Path A first** — confirm the partial-profit feature actually
fires in production before queueing more work. If smoke is clean,
**Path B next** — time-decay is the only remaining latent-bug brief in
the queue, worth shipping while the exit-engine area is hot in mind.
Path C waits on its own clock.

The `f8b-verification-soak-2-trigger` scheduled task at 16:30 UTC and
the persistent `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` working-
tree change are independent of all three paths.
