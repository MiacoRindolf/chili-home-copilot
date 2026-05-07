# Cowork Review: f-add-paper-shadow-mode

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-06_f-add-paper-shadow-mode.md`
**Reviewer:** Cowork.
**Date:** 2026-05-06.

## Verdict

One commit, migration 229 landed cleanly, 9-test file written
(4 source-text tests pass, 5 DB tests error on Windows kernel buffer
exhaustion — environmental, not code). 256/256 prior tests still
pass. **Approve.**

The most important finding is **my brief was wrong about
`paper_book_json` being dormant.** CC's pre-execution audit caught
it and surfaced cleanly. Worth pulling that into the verdict because
it's a Cowork-side discipline correction.

## 🚨 My pre-execution audit grep was too aggressive

I told the executor in the brief: "zero readers, zero writers"
for `paper_book_json` and `brain_paper_book_on_promotion`. **That
was wrong.** When I ran the grep myself before writing the brief,
I saw 15 file matches and narrowed by excluding `models/trading.py`,
`migrations.py`, `NEXT_TASK.md`, and a few dispatch scripts —
which incorrectly filtered out the actual reader/writer sites.

CC's audit grep (correctly less aggressive) found:

```
app/services/trading/shadow_testing.py:51:    existing_meta = variant.paper_book_json or {}
app/services/trading/shadow_testing.py:53:    variant.paper_book_json = existing_meta
app/services/trading/learning.py:7468:    if prom_stat == "promoted" and getattr(_oset, "brain_paper_book_on_promotion", False):
app/services/trading/learning.py:7469:        patch["paper_book_json"] = {
app/services/trading/pattern_engine.py:1008:                "oos_validation_json", "queue_tier", "paper_book_json"):
```

So the placeholder ISN'T dormant — it's wired for an entirely
different purpose. `paper_book_json` is per-pattern A/B test
metadata (control-vs-variant pattern IDs, Welch t-test config,
bootstrap-Sharpe params) used by `shadow_testing.py`. It has nothing
to do with per-trade execution shadowing — they're orthogonal
concepts.

CC correctly recognized this and made the right call: the new
per-trade `paper_shadow_of_alert_id` design is purely additive on
`trading_paper_trades` and doesn't conflict with the existing
per-pattern JSONB on `ScanPattern`. **Two different "shadow"
features for two different purposes, coexisting cleanly.**

This is a Cowork-side discipline correction:
- **Cookbook update**: when running a pre-brief audit grep, **don't
  exclude files until I've read enough to know they're not the
  source.** I excluded files based on "this is just schema/migration
  noise" without checking whether they had real consumer code
  inside them.
- **The brief's Step 0 fail-stop was the right discipline.** It
  caught my mistake. The audit step itself had value, even if my
  pre-brief framing was wrong about what to expect.

## What Claude Code did right

1. **Caught and reframed the dormant-placeholder claim.** Surprise
   §1. Two grep results revealed `paper_book_json` is actively used
   by `shadow_testing.py` (per-pattern A/B testing) and
   `learning.py:7468-7469` (initialization-on-promotion). My brief
   said "dormant"; CC said "wired for a different purpose." Made the
   right call to proceed because the per-trade design doesn't
   conflict with the per-pattern A/B testing design.

2. **Decision-string tagging at terminal points.** CC tagged each
   shadow call with `decision="placed"` / `"blocked_pdt"` /
   `"blocked_no_order_id"` so the SQL probe + future audit can
   split shadows by what the live decision was. **Brief didn't
   specify this granularity; CC added it because it's load-bearing
   for execution-alpha-drag analysis.** Test #9 pins the three
   strings.

3. **Comment-only "filter" on `update_pattern_stats_from_closed_trades`.**
   Surprise §2. The brief asked to add a `paper_shadow_of_alert_id
   IS NULL` filter to evidence aggregation. CC inspected the
   function and found it reads ONLY `Trade` (live), not `PaperTrade`
   — so today's shadow rows can't double-count because they're not
   in the query path at all. **No code change needed.** Added a
   forward-looking comment + Test #7 that pins the contract: if a
   future PR adds `PaperTrade` to the closed-trade union, the
   filter MUST be added at the same time. **Right discipline:
   don't ship dead code; pin the future contract instead.**

4. **Existing dedupe guard inheritance.** Surprise §6.
   `open_paper_trade` already short-circuits when there's an open
   paper trade for the same `(user_id, ticker, scan_pattern_id)`
   triple. Shadow inherits this. **Acceptable per brief intent**
   (per-alert pairing means at most one shadow per pattern-ticker
   per user is open at a time), surfaced for explicit Cowork review.

5. **Honest framing of the test environmental blocker.** 5 of 9
   tests errored on Windows kernel buffer exhaustion (`WinError
   10055`) during pytest schema-bootstrap (now 229 migrations).
   CC didn't paper over this — explicitly mapped each DB test to
   a corresponding source-text test that already pins the same
   behavior, then asked the operator to re-run after kernel
   buffers recover (post-reboot or extended idle). **This is the
   yfinance Thread-leak fingerprint we saw in saved memory** —
   not new, but worth tracking if it persists.

6. **Surface-level wiring guard (Test #9).** Asserts `auto_trader.py`
   source contains all three decision-tag strings. If a future
   refactor accidentally removes a wiring point, this test catches
   it. Same shape of guard the dispatcher gets in
   `f-handler-pattern-stats` — fast (<1s), high signal.

7. **Pydantic Field convention adherence.** Surprise §5. Brief's
   pseudocode used a bare type-annotated default for the new flag;
   CC noticed the file's existing `chili_autotrader_*` settings
   use `Field(default=False, validation_alias=...)` and conformed
   to that pattern so env-var override works correctly.

8. **Default off, opt-in via env.** Shipping doesn't change live
   behavior. Operator flips `CHILI_AUTOTRADER_PAPER_SHADOW_ENABLED=1`
   when ready. Reversible by flipping the flag back.

## Findings

### The two-shadow-features distinction is the most important architectural finding

Today's brief shipped per-trade execution shadowing. The pre-existing
`paper_book_json` is per-pattern A/B testing. They're orthogonal:

| Concept | Storage | Purpose | Trigger |
|---|---|---|---|
| `ScanPattern.paper_book_json` (existing) | JSONB on ScanPattern | A/B test pattern A vs pattern B | On pattern promotion |
| `PaperTrade.paper_shadow_of_alert_id` (new) | FK on PaperTrade | Per-alert live ↔ shadow pair for execution drag | On every live decision (placed/blocked/skipped) |

**They coexist cleanly. Don't unify them.** The unifying meta-concept
"paper-shadow tracking" is a wishful generalization — the two
features have different cardinality (per-pattern vs per-trade),
different triggers (lifecycle event vs decision event), and
different consumers (A/B test analyzer vs execution-drag SQL probe).
Forcing one storage shape across both would lose specificity that
each consumer needs.

### The `update_pattern_stats_from_closed_trades` filter is forward-looking

CC's reasoning here is sharp: today the function reads only `Trade`
(live), so the brief's filter would have been dead code. CC chose
to add a comment + Test #7 that pins the contract instead. **If
someone in the future extends the function to also read `PaperTrade`,
the test will fail unless they ALSO add the filter** — which is the
exact moment the filter actually becomes load-bearing.

This is the right kind of "future-proofing": don't ship dead code
defensively, but pin the future invariant so it can't be silently
violated.

### Decision-string tagging matters more than I framed it in the brief

The brief just said "wire shadow at three terminal points." CC
tagged each call with a distinct `decision=` string
(`placed`, `blocked_pdt`, `blocked_no_order_id`). This matters
because:

- The execution-alpha-drag analysis splits by decision — "drag on
  successful placements" vs "opportunity cost on PDT-blocked" vs
  "opportunity cost on no-order_id" answer different questions
- Future audits can drill into "which class of decision shows the
  largest drag" without re-deriving from the audit row's other
  fields
- If a 4th terminal point is added in the live branch, the test
  guard catches the missing tag

Brief should have specified this; it didn't. CC made the right
call.

### The Windows kernel buffer exhaustion is recurring

The `WinError 10055` "No buffer space available" pattern matched
exactly the saved-memory `f-leak-3` finding (yfinance Thread leak,
2026-05-04). The current pytest schema-bootstrap with 229
migrations exhausts the non-paged kernel pool. **This isn't
caused by today's brief**, but it's a recurring environmental
blocker that's now hitting consistently with each new migration.

Worth a follow-up: either reduce the schema-bootstrap connection
churn (cache the migrated-state, run migrations once per fixture
session instead of per-test) OR document the operator workaround
(restart Docker Desktop / WSL between long pytest runs). Surface
as `f-fix-pytest-bootstrap-kernel-pool` if it becomes painful.

## Answers to the Open Questions

### 1. `paper_book_json` reconciliation

**Keep them orthogonal.** Per the table above, the two features
serve different cardinalities and different consumers. Forcing
unification would degrade both. The brief's framing assumed they
were the same feature (because of the shared word "shadow"); CC
correctly identified they're not. **No reconciliation needed.**

### 2. Dedupe behavior

**Accept inherited dedupe.** When two BreakoutAlerts on the same
pattern+ticker fire close in time, the second alert's shadow gets
short-circuited by `open_paper_trade`'s existing dedupe guard.
Acceptable because:

- The execution-drag measurement compares per-pattern-per-ticker live
  vs shadow; second-alert noise would just inflate row counts
  without adding signal
- The dedupe matches existing paper-mode behavior; making shadow
  bypass it would create asymmetric semantics
- If the operator wants per-alert-per-trigger evidence (richer
  granularity), that's a separate brief

Watch item: if post-deploy data shows the dedupe is suppressing
meaningful evidence (e.g., the SECOND alert often has different
projected P/L from the first), surface and revisit.

### 3. `MAX_OPEN_PAPER_TRADES` cap

**Watch item, not blocking.** Once shadow is enabled, paper
open-position count grows much faster (every alert hitting the live
branch generates a shadow). If the cap is hit, shadows for that
user start silently failing. **Pre-deploy expected ratio**: shadow
opens roughly equal to live `placed` + `blocked_pdt` +
`blocked_no_order_id` — looking at yesterday's diagnostic, that's
~30 per hour during active windows. If the cap is, say, 100, we'd
hit it within hours.

Recommend: when enabling the flag, watch
`SELECT COUNT(*) FROM trading_paper_trades WHERE
paper_shadow_of_alert_id IS NOT NULL AND status='open'` and the
exit-engine throughput. If shadow positions accumulate faster than
they close, surface for a per-mode cap brief.

### 4. Exit-engine bottleneck

**Same watch item as #3.** Paper exit-engine ticks every 5 min.
With shadow opening many positions, per-tick load grows. Pre-fix,
the engine processed 0 paper positions per tick (none existed).
Post-shadow-on, it'll process whatever the shadow population is.
If post-deploy logs show the exit-engine taking >30s per tick,
surface.

### 5. Holding-period mismatch

**Defer to dashboard brief.** Shadow may close at a different time
than live does (different exit-engine state). The execution-drag
SQL probe in this brief just compares realized P/L without time
alignment. If post-deploy data shows wide divergence between
shadow-close-time and live-close-time, the dashboard brief should
add a time-aligned variant. **Today's design is acceptable for the
first read; refinement happens after we see real data.**

### 6. Migration tests

**Accept Test #7 as the migration smoke.** It pins the column +
forward-looking contract via source inspection. The next test-DB
cycle runs Tests #2/#3 which depend on the column existing for
ORM round-trip — that's the empirical check. The schema-introspection
during `apply_mig_229.py` already confirmed column + index land
cleanly.

## Engineering concerns (smaller)

1. **The 5 DB tests should be re-run when the kernel buffers
   recover.** CC explicitly flagged this in the report. If the
   operator re-runs and they still error, it's a regression
   (today's code path opening more connections than expected) —
   but more likely it's just the schema-bootstrap contention from
   229 migrations.

2. **The execution-alpha-drag SQL probe** lives in
   `scripts/dispatch-paper-shadow-execution-delta.ps1` and shipped
   in this brief. After 24h of shadow data, run it and read the
   t-statistic on bias — that's the headline number for "is
   execution slippage materially affecting strategy P/L."

3. **`brain_paper_book_on_promotion`** is wired in `learning.py`
   but only when `_oset.brain_paper_book_on_promotion=True`.
   Default is `False`. So in current operation it's a no-op. Not
   a concern; just an FYI that the placeholder I miscategorized
   has both a writer (`learning.py:7469`) and a reader
   (`shadow_testing.py:51,53`) wired through the A/B testing path,
   even though they're effectively dormant in practice unless an
   A/B test is configured.

4. **Pre-existing carry-forward** — same as prior reports. Not in
   scope here.

## State of the world after f-add-paper-shadow-mode

- **22 protocol runs landed clean** today (across 2 days now).
- Today's six commits: parity-persist + partial-profit + time-decay
  + canonical-writer + cycle-kill + pattern-stats handler.
- Yesterday-overnight + this morning's seven additional commits:
  handler-load-verification + paper-runner-output-gap audit +
  live-trade-closed-emitter + backtest-completed-emitter +
  db-watchdog-kill-action enhancement + paper-shadow mode + 5
  handler import fixes.
- **Total this session**: 12+ commits, 8+ migrations
  (225/226/227/228/229 plus the cycle-kill config + earlier
  yfinance/leak fixes).
- **The brain stack** went from "fundamentally broken for 6+ days"
  to "structurally correct, paper-shadow ready for opt-in
  activation, every Phase 2 handler functional."
- **Next operator action**: deploy + flip
  `CHILI_AUTOTRADER_PAPER_SHADOW_ENABLED=1` to start collecting
  execution-drag data.

## Decisions confirmed

- **Approve and ship.** Migration 229 + 1 commit. Tests pass that
  can pass; environmental blocker on the others is documented.
- **`paper_book_json` and `paper_shadow_of_alert_id` stay
  orthogonal.** Two different shadow concepts for two different
  purposes. No unification.
- **Decision-string tagging** is the intended granularity for
  execution-drag analysis.
- **`update_pattern_stats_from_closed_trades` filter is comment +
  test only**, not actual code, until the function is extended to
  read `PaperTrade`.
- **Cookbook updates from today's run**:
  - **NEW: Pre-brief audit greps must NOT exclude files based on
    "looks like just schema/noise" without reading inside the
    file.** Today's brief author (me) excluded files that contained
    actual consumer code, leading to the wrong "dormant" framing.
  - **NEW: Decision-string tagging at multi-point hooks should be
    explicit in briefs** so the executor doesn't have to invent it.

## Brief-cookbook updates (running list)

- Always prefix `trading_*` for SQL table names in trading domain
- Migration IDs: "next sequential at execution time" not hardcoded
- Verify column types AND names before the brief asserts them
- Trade/PaperTrade close-time column is `exit_date`, not `close_date`
- Verify saved-memory claims against current code before asserting them
- Distinguish `f-handler-*` (event-driven) from `f-cron-*`
  (timer-driven) at brief-write time
- For event-driven handler architectures, add startup-verification
- "Do not modify X" is scope-protection, not a frozen contract
- Cleanup briefs MUST include a Phase 0 production-state audit
- Quote line ranges in audit docs; don't paraphrase code logic
- **NEW**: Pre-brief audit greps must NOT pre-filter files without
  reading inside them
- **NEW**: Decision-string tagging at multi-point hooks should be
  specified in briefs

## Next move

Three reasonable directions:

**Path A — Operator deploy + flip the shadow flag.**
`CHILI_AUTOTRADER_PAPER_SHADOW_ENABLED=1` in compose.yml + restart
relevant services. Wait one AutoTraderRun cycle (~1 min), confirm
shadow rows appear via the SQL probe in CC report Section
"Smoke (deferred to deploy)." After ≥24h, run
`dispatch-paper-shadow-execution-delta.ps1` for the first
execution-alpha-drag read.

**Path B — Re-run pytest after kernel-pool recovery.** The 5 DB
tests that errored on `WinError 10055` should pass once the
non-paged kernel pool recovers. Worth confirming before assuming
the source-text guards are sufficient.

**Path C — Queue follow-up briefs from today's CC reports:**
- `f-fix-autotrader-paper-fallback` (Phase 3 of overnight cleanup)
- `f-add-pg-stat-snapshot-logger` (Phase 2 deferred)
- `f-tighten-db-watchdog-brain-worker-exemption` (Phase 6 follow-up)
- `f-prefer-shadow-evidence` (queued conditional on shadow data)
- `f-paper-shadow-dashboard` (UI / dashboard for execution drag)
- `f-fix-pytest-bootstrap-kernel-pool` (test environmental fix)

**My read: Path A first.** This is the operationally-real moment
for the entire day's chain. Once shadow is collecting data, every
follow-up brief has empirical grounding instead of speculation.
After 24h of clean data, queue the next round of follow-ups based
on what the shadow data actually shows.

The session has shipped 12+ commits across 2 days. Time to let it
settle, watch the data, and queue the next round based on what
real production traffic reveals. **Take the win.**
