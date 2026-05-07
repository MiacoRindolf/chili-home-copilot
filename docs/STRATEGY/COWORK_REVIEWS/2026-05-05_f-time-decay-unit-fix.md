# Cowork Review: f-time-decay-unit-fix

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-05_f-time-decay-unit-fix.md`
**Reviewer:** Cowork.
**Date:** 2026-05-05.

## Verdict

One commit, one migration (227, not the brief's 226 — explained in
Surprises §1), 12/12 new tests pass, 248/248 prior exit-evaluator tests
still pass. **Approve.**

The headline finding is buried in the report — it deserves top billing:

> **625 of 769 patterns (81%) trade on non-1d timeframes** and have
> therefore been silently affected by the wall-clock-`.days` time-decay
> bug. Pre-fix, a 1m position with `max_bars=20` would have needed 20
> wall-clock days (28,800 minutes) to fire `exit_time_decay`, not 20
> minutes. Post-fix, it fires correctly at 20 bars.

**This was a much larger active bug than the brief framed.** The brief
said "operationally invisible until it bites" and assumed the impact
would be the first 1m fast-path position. The production survey shows
the bug has been silently affecting the majority of patterns for the
duration of the multi-timeframe era. Time-decay has been effectively
disabled for 81% of pattern positions. **This fix is meaningfully
larger in impact than the brief sized it as.**

## What Claude Code did right

1. **Pre-deploy production survey before writing the migration.** The
   brief asked for the survey AS PART of the smoke step (Step 6); Claude
   Code ran it BEFORE the migration to confirm no rows would be rejected
   by the CHECK constraint. That's the right discipline — sequencing the
   verification before the schema change makes the migration safer to
   land. Surfacing the 81%-affected finding from that survey is the
   actually-load-bearing fact this task produced.

2. **Caught and fixed a third bug site (`position_plan_generator.py`).**
   Surprise #3. Brief targeted only `live_exit_engine.py`. Research
   surfaced the same `(now - entry_date).days` lie at
   `position_plan_generator.py:185` where the LLM context dict is built
   for an open position. A 5m scalper 30 minutes old was showing up to
   the LLM as "0 days held" indefinitely. Same root cause, same fix,
   bundled into the same commit because:
   - Same lie at a different surface; bundling is cleaner than two PRs
   - `pat` is already in scope at line 181, so no `_compute_bars_held`
     round-trip needed
   - The output dict already includes `pattern_timeframe` so LLM
     consumers have the unit
   - Grep confirmed zero external readers of `days_held`
   - Easily revertable if Cowork wants it as a follow-up

   The brief said "Do not change other paths" but that constraint was
   about the canonical evaluator and backtest path — both of which
   stayed correctly untouched. `position_plan_generator.py` is a third
   adapter with the same bug; treating it as in-scope was the right
   call.

3. **Standalone module, not "extend an existing parallel."** Brief
   Open Q #1 asked Claude Code to extend an existing helper if one
   exists. Research found four parallel maps (`coinbase_ohlcv._GRANULARITY_MAP`,
   `market_data._VALID_INTERVALS`, `paper_trading._expiry_days_for_timeframe`,
   plus the new one) — and **explained why each serves a different
   purpose** (Coinbase API granularity / yfinance interval validation /
   expiry policy / unit conversion). Forcing one to do double duty would
   pollute domain boundaries. Standalone `timeframe_utils.py` with a
   docstring that enumerates the parallels is the right architectural
   choice.

4. **`days_held → bars_held` rename instead of preserving the legacy
   key.** The legacy key was actively misleading at non-1d timeframes —
   a value whose unit is "bars" living in a key named "days." Claude
   Code grep'd, confirmed zero external readers, renamed for honesty.
   Future readers see the unit-aware integer in a unit-honest key.
   Backward-compat alias would have been wrong here because the OLD
   value's unit was the bug.

5. **Test #11 is the regression the brief didn't explicitly ask for.**
   "1d paper trade with 21 minutes elapsed must NOT fire time-decay" —
   the negative-integration test that catches "did the fix accidentally
   over-fire?". Without it, a future regression that made
   `_compute_bars_held` always return wall-clock minutes regardless of
   timeframe would still pass test #10 (which was about firing on time).
   The negative case is the one that catches sign-error refactors.

6. **Migration-ID conflict surfaced cleanly.** Brief said 226; actual
   was 227 because `f-partial-profit-wire-up` consumed 226 first. Claude
   Code proceeded with 227, surfaced as Surprise §1 with the PROTOCOL
   citation. Right operating mode — the conflict is sequencing, not
   contract, so auto-mode authorization to proceed is correct.

7. **CHECK constraint mirrors `_TIMEFRAME_SECONDS.keys()`.** Single
   source of truth on the Python side, enforced at the SQL side. If
   anyone adds `12h` to `_TIMEFRAME_SECONDS` without also extending the
   CHECK list, inserts of `'12h'` will fail and the inconsistency is
   surfaced loudly. That's the right defensive discipline.

## Findings

### The 81% headline reframes the priority of this fix

The brief framed time-decay-unit-fix as "operationally invisible until
the first 1m fast-path position bites." The survey shows it's been
silently affecting 81% of patterns for an unknown duration. Specifically:

- 181 patterns on `1m` (the fast-path)
- 170 patterns on `1h`
- 116 patterns on `5m`
- 84 patterns on `15m`
- 74 patterns on `4h`
- 144 patterns on `1d` (correctly handled by the legacy code)

For all 625 non-1d patterns, every position they generated has been
holding past the intended `max_bars` cap because `.days` returned 0
for the entire intraday lifetime. **Some of those positions probably
should have time-decayed and didn't.** That explains a class of
"position lingering" symptom we may have attributed to other causes.

Worth a forensic pass post-deploy: query closed trades with non-1d
timeframes and unusually long hold durations. The pattern would be
"position closed via stop or BOS after a duration that exceeds the
intended `max_bars` × `timeframe_seconds` — should have time-decayed
earlier." That's a one-time analysis brief; not in scope here.

### `position_plan_generator.py` was a hidden third site

Surprise §3 is the more important one for operational correctness. The
LLM context dict's `days_held` was being read by every LLM call that
asked the brain about an open position. For 81% of positions, the
brain saw `days_held=0` indefinitely — meaning every LLM-driven
revalidation, exit recommendation, or position assessment was working
from a fictional "freshly-opened" framing. That's a hallucination
**input** to the LLM, not output, which is harder to catch and harder
to debug.

Post-fix, the LLM sees a real `bars_held` count that scales correctly
with the position's timeframe. LLM-driven exit decisions should now be
materially better-grounded for non-1d positions.

### Standalone `timeframe_utils.py` is the right architecture

The four parallel maps each answer a different question:

- `coinbase_ohlcv._GRANULARITY_MAP` — "what granularity does the
  Coinbase REST API accept?" (provider-specific; 1m/5m/15m/1h/6h/1d)
- `market_data._VALID_INTERVALS` — "is this a valid yfinance interval
  string?" (includes `1mo`, `3mo` calendar units that don't have
  fixed-second durations)
- `paper_trading._expiry_days_for_timeframe` — "after how many days do
  we auto-close a paper trade on this timeframe?" (strategy choice,
  not unit conversion)
- `timeframe_utils._TIMEFRAME_SECONDS` — "how many seconds is one bar
  at this timeframe?" (pure unit math)

Conflating them would either pollute domain boundaries or force the
unit map to handle calendar units it physically can't represent. The
standalone module with a docstring that names the parallels is the
clean answer.

### Test execution time of 533s for 12 tests is the truncate-per-test cost

Same fixture pattern as f-partial-profit-wire-up's tests; not a
regression introduced by this task. The 248 existing exit-evaluator
tests run in 1.31s because they don't hit the schema-truncate fixture.

## Answers to the Open Questions

### 1. Migration ID conflict

**Already resolved.** Claude Code proceeded with 227. Future briefs
should specify the migration ID as "next sequential at execution time;
verify with `verify-migration-ids.ps1`" rather than hardcoding a
number. I'll update the brief template.

### 2. Helper-reuse decision (standalone vs extend)

**Standalone is the right call.** The four parallel maps serve
genuinely different purposes; mixing them would create cross-domain
coupling. The docstring's enumeration of the parallels is the right
discoverability mechanism for future readers.

### 3. Scope expansion to `position_plan_generator.py`

**Approve the bundled fix.** Same root-cause bug at a different surface,
zero external readers of the renamed key, small hunk. Shipping it
together is the right scope discipline — separating it would have
left the LLM-context surface lying about position age while the exit
engine was fixed. That mismatch would have been worse than the
"do not change other paths" constraint Claude Code technically
expanded.

### 4. `days_held → bars_held` key rename

**Approve.** The old key's unit was the bug; backward-compat aliasing
would propagate the lie. Zero external readers verified by Grep.

### 5. CHECK constraint allowed list (keep `30m`, `2h`, `1w` even if absent from prod)

**Keep.** Adding values to the CHECK list later requires a migration;
adding values to `_TIMEFRAME_SECONDS` without one would create the
inconsistency described above. The current list is internally
consistent and forward-compatible.

### 6. Position-side timeframe metadata (Trade.timeframe / PaperTrade.timeframe)

**Defer to a follow-up brief.** Currently the helper reads
`scan_pattern_id → ScanPattern.timeframe`. For orphan positions
(`scan_pattern_id IS NULL`) the fallback is `1d`, which is correctly
the safe default but slightly wrong for non-1d orphans. Adding a
`timeframe` column to both Trade and PaperTrade would make positions
self-describing and remove the JOIN-on-pattern dependency.

That's worth doing eventually but isn't urgent — orphan positions are
rare (manual entry or broken backfill), and 1d-fallback for them is
the conservative direction (closes positions later than needed, never
earlier than needed). Queue it as `f-position-self-describing-timeframe`
when the operator wants to clean it up.

## Engineering concerns (smaller)

1. **The forensic analysis of "should-have-time-decayed-but-didn't"
   positions** is now possible with the survey data. A query that
   joins closed trades to their patterns, computes
   `(close_date - entry_date) × seconds_per_pattern_timeframe`, and
   compares to `pattern.exit_config.max_bars`, will surface positions
   that held past the intended cap. Worth a one-time analysis brief
   for the brain-evidence library. Not blocking.

2. **The `bars_held` key rename invalidates any out-of-repo consumer**
   that read `days_held` from the LLM context or exit-engine result.
   Grep confirmed zero in-repo readers, but an external dashboard,
   notebook, or LLM prompt template stored in a sister repo could
   break silently. If the operator has any such consumer, surface and
   add a backward-compat alias. Likelihood is low.

3. **Pre-existing carry-forward** — `_trade_phantom_close_guard`,
   `.env.example` `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE*`,
   `crypto_top.json` byte-shift, untracked `.commit_msg_*.txt` /
   `docs/AUDITS/*` backlog. Same disposition as prior CC reports;
   not this task's concern.

## State of the world after f-time-decay-unit-fix

- 16 protocol runs landed clean (15 + this one).
- 1 commit + 1 migration this run; 5 files touched + 1 new test file +
  1 new module.
- **Time-decay is operationally correct for 81% of patterns for the
  first time.** The bug had been silently disabling time-decay for the
  majority of pattern positions; the fix restores intended behavior.
- 12 new tests + 248 existing exit-evaluator tests all pass.
- LLM context dicts are now unit-honest about position age.
- The two remaining queued briefs (`f-exit-parity-metric-v2`,
  pre-existing `f8b-verification-soak-3`,
  `bracket-writer-cover-policy-clarify`) are unchanged.

## Decisions confirmed

- **Approve and ship.** All 7 brief steps + 5 surprises landed clean.
- **Migration 227 (not 226)** is the correct sequential ID.
- **Standalone `timeframe_utils.py` module** is the right home for
  unit-conversion math.
- **`position_plan_generator.py` scope expansion** approved.
- **`days_held → bars_held` rename** approved.
- **CHECK constraint allowed list** stays at `_TIMEFRAME_SECONDS.keys()`.
- **Position-side `timeframe` column** deferred as a follow-up
  (`f-position-self-describing-timeframe` when needed).
- **Brief-template update**: future briefs specify migration IDs as
  "next sequential at execution time" rather than hardcoded.

## Next move

Three reasonable directions:

**Path A — operator-side smoke verification** of both shipped features
(partial-profit-wire-up + time-decay-unit-fix) in one pass. Pick a
non-1d pattern with `partial_at_1r=True` flipped; verify on the next
position that:

1. `[partial_profit_ops]` log line fires at 1R.
2. `bars_held` in the LLM context dict is timeframe-correct.
3. `exit_time_decay` fires at the timeframe-correct elapsed time, not
   wall-clock days.

One operator session, ~30 min of attention.

**Path B — forensic analysis** of historical "should-have-time-decayed"
positions per the engineering concern above. Query closed trades on
non-1d patterns whose hold duration exceeded
`max_bars × timeframe_seconds`. Surface count and aggregate P/L impact.
Informs whether to backfill any pattern-evidence corrections. Pure
analysis, zero code.

**Path C — wait on `f-exit-parity-persist` data accumulation,** then
re-promote `f-exit-parity-metric-v2` from QUEUED. The metric brief
specifically waits on f-time-decay-unit-fix shipping (which it has,
now) AND 24-48h of parity data accumulation.

**My read: Path A first** — confirm BOTH recently-shipped features are
operationally real before queueing more work. If smoke is clean, **Path
B as a quick analysis brief** to quantify the historical-time-decay
miss. **Path C** waits on its own clock; check parity data quantity in
~24h.

The forensic analysis (Path B) is genuinely valuable — the brain's
pattern-evidence library currently includes win-rate / avg-return data
from positions that held past their intended time-decay cap. Some
"this pattern wins" judgments may have been buoyed by positions whose
correct exit would have been earlier and at worse prices. Quantifying
the impact tells us whether the pattern-evidence library needs a
forensic correction. Not urgent, but interesting.
