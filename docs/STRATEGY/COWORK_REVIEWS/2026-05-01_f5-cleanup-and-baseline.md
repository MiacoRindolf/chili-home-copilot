# Cowork Review: f5-cleanup-and-baseline

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-01_f5-cleanup-and-baseline.md`
**Reviewer:** Cowork.
**Date:** 2026-05-01.

## Verdict

Excellent execution. All three success criteria met, plus three judgment calls Claude Code made unprompted that were the right calls. One real issue surfaced (recurring unhealthy state confirmed, root cause identified, structural fix proposed) — exactly what Open Questions are for. Approve.

## What Claude Code did right (specifically)

1. **Two-commit split, not bundled.** `cb137ea` (F5 feature) + `6bab79c` (the `fast_exits_native` SQL view migration 219). Per PROTOCOL hard rule 6 ("one task = one logical commit"), splitting feature from migration was correct because the view is a review-tooling artifact, not part of the F5 closure-of-the-loop semantic. Either could be reverted independently.

2. **Excluded unrelated working-tree noise.** `app/models/trading.py` (legacy Trade phantom-close guard), `.env.example` (pattern-evidence env vars), and `CLAUDE.md` + `docs/STRATEGY/` (the protocol's own infrastructure) were all sitting modified in the working tree but **not** part of F5. Claude Code identified all three, listed them in the report, and intentionally left them uncommitted. That's the discipline the protocol exists to enforce; it landed it on its first run.

3. **The bracket-age classifier is clever and free.** Instead of adding the schema column or writing a backfill UPDATE, Claude Code used the gap between `entered_at` (entry time) and `brain_json.computed_at` (bracket-decision time) as the natural classifier. Inherited entries have a ~2000s gap (because F5 bootstrapped their brackets at boot, long after F4 entered them); native entries have a ~0.3s gap (entry and bracket happen in the same code path). The classification space is bimodal at >2300× — no calibration risk, no schema delta, no backfill, future-proof. This is the kind of move that makes me trust the architecture.

4. **Stopped at the "don't pull on the thread" rule.** When the unhealthy investigation revealed the state is recurring (not transient), Claude Code stopped, documented findings, proposed two options for the fix, voted for option 2 with reasoning, and surfaced as Open Question. That's the protocol working as designed.

5. **Verbatim SQL provided.** The `fast_exits_native` aggregate query and per-trade detail query are pasted in the report. I can copy-paste them into future reviews without reconstructing.

## Findings I want to act on

### 1. Container unhealthy state is RECURRING and structural

The 90s `last_bar_at` freshness threshold in `app/services/trading/fast_path/healthz.py:110` is too tight relative to Coinbase's candle-channel cadence on quiet pairs. WS is alive (heartbeats + L2 books + alerts all flowing), but the candles channel goes silent on low-volatility pairs for minutes at a time, causing `/healthz` to oscillate 200↔503.

Claude Code's vote (and mine): **split the probe.** A `ws_connected` check (heartbeats + reconnects) and a `candle_freshness` check (long threshold like 5 min, on at least one pair). Don't ship F6 over a flapping unhealthy container — it muddies every future log review.

This is a small task. ~30 lines of code in `healthz.py`.

### 2. Strategy infrastructure is uncommitted

`CLAUDE.md` (the protocol pointer block I added) plus the entire `docs/STRATEGY/` directory tree are sitting in the working tree. Claude Code correctly didn't bundle these into the F5 commit. They need their own commit before any further work.

This is a 30-second task. Just: `git add CLAUDE.md docs/STRATEGY/ && git commit -m "chore: strategy protocol infrastructure" && git push`.

### 3. Sample size warning is correct

3 round trips, all stop_hits, 0% win rate, -$0.18 — Claude Code flagged this is consistent with the DOGE-too-tight-bracket thesis but not statistically separable from coin-flip variance. Edge-proof bar in `CURRENT_PLAN.md` is >50 round trips. We're at 3. **F6 is the right next move regardless of these specific outcomes** because F6 mines from alert-history (~500 alerts) plus L2 trajectory (~100k+ books), not from the realized round-trip sample.

### 4. Open Question #1 (extend soak vs. start F6) — answered.

Both Claude Code and I voted F6 starts now. Confirmed. Realized exits accumulate in parallel.

## What's still pending before F6

Two small cleanup items remain:

1. Healthcheck split-probe (item 1 above)
2. Strategy infrastructure commit (item 2 above)

Both should land in one task. Total effort < 30 lines of code + a `git add/commit/push`. Then F6.

## Algo trader read on the data we have

Don't read too much into 3 DOGE stop_hits. The dataset can't tell us yet:
- Whether DOGE-USD imbalance_long is structurally bad or whether DOGE just had a downtrend during the window
- Whether the 16-bp ATR-based stop is too tight or whether the signal genuinely doesn't have edge
- Whether the targets-too-far thesis is correct (0 target_hits across 3 exits is not a sample)

What we CAN say with confidence:
- The exit pipeline works correctly (idempotent writes, correct stop detection, realized P/L computed on real book bid)
- The bracket geometry produced by `stop_engine.compute_initial_bracket()` is sized to ATR(14) on 1m bars — this is exactly the same engine that sizes 1d-bar swing trades. Whether that timeframe match is right is the F6 question.
- Brain integration via `brain_json` capture means we can postmortem any trade with the full decision context

## Dev architect read on the code

`fast_exits_native` SQL view is the correct level of abstraction — review queries don't need to know about the bracket-age trick. View-level filtering keeps the trick localized to migration 219 where it's documented. Good engineering.

The `brain_json.computed_at` timestamp populated at exit_manager bootstrap time vs. entry time is a subtle but load-bearing detail. Worth a comment in `exit_manager.py` so future Claude Code (or me) doesn't accidentally normalize them and break the classifier. Flagging as a small follow-up.

## Verdict on the workflow

This was the protocol's first end-to-end run. It worked exactly as designed:
- I wrote `NEXT_TASK.md`
- Operator typed `claude`
- Claude Code read the protocol stack, executed within scope, surfaced surprises in Open Questions, committed cleanly, marked DONE
- Operator said "report's done"
- I'm reading + reviewing now

Total operator effort between strategy and report: typing `claude` once. That's the ergonomics we wanted.

## Decisions confirmed

- F5 ships as `cb137ea` + `6bab79c`. Both already pushed. ✅
- `fast_exits_native` is the canonical review query surface. Use it.
- The 11 inherited bootstrap positions stay in the system; exit_manager handles them naturally; they're filtered out of native P/L analysis automatically.

## Next move

Will discuss with operator before writing the next NEXT_TASK. Two options on the table:
- **Plan A (my preference):** Combined cleanup-2 task — healthcheck split-probe + strategy-infra commit. Then F6. Two commits, total maybe 60 lines of code.
- **Plan B:** Just the healthcheck fix as its own task; commit strategy-infra manually. Then F6.

Plan A is cleaner protocol-wise (let the workflow do the work). Plan B is faster wall-clock if operator wants to git-add the strategy infra in 30 seconds themselves.
