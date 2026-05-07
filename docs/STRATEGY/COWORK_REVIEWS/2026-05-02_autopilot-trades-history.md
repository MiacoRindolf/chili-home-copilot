# Cowork Review: autopilot-trades-history

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-02_autopilot-trades-history.md`
**Reviewer:** Cowork.
**Date:** 2026-05-02.

## Verdict

Clean ship. Two quiet bug catches that would have been silent regressions in the UI; honest accounting on the visual-verification limit; and useful new data surfaced by the report itself. Approve.

## What Claude Code did right

1. **Caught the `fmtPct` scale mismatch.** The existing partial's `fmtPct(v) = (v * 100).toFixed(3) + "%"` assumes `v` is a fraction (e.g. `0.087` → `+0.087%`). But `fast_exits.realized_return_pct` is **already** a percent (the exit_manager writes `(exit/entry - 1) * 100`). Reusing `fmtPct` would have rendered `-19.18%` for what's actually `-0.192%` — a 100× display bug. Claude Code spotted this, formatted the raw value directly, and added an inline comment so a future refactor can't regress. Quiet save.

2. **Belt-and-suspenders `COALESCE(..., FALSE)` on `is_native`.** Postgres can return null from `(brain_json ? 'computed_at') AND (EXTRACT(...) < 60)` if a future schema change made `brain_json` nullable on `fast_exits`. Today it's `NOT NULL` so this is defensive. Robust.

3. **Idempotent toggle binding via `dataset.bound`.** Catches the case where the partial gets injected after DOMContentLoaded already fired (autopilot is SPA-ish). Both paths wire the change handler exactly once.

4. **Verified the endpoint matches the F5-cleanup SQL benchmark exactly.** 3 RT, 0W/3L, -$0.18 total, win_rate 0. The endpoint is producing identical numbers to the verbatim SQL — no off-by-something errors.

5. **Honest about what wasn't tested.** Couldn't open a browser, so structural test only. Flagged for operator visual eyeball. Better than silent assumption.

## A new data point worth noting

The `by_ticker` block reveals something interesting:
```
BTC-USD:  1 trade, -$0.045
DOGE-USD: 1 trade, -$0.089
ETH-USD:  1 trade, -$0.048
```

**At F5-cleanup time, the 3 native exits were all DOGE.** Between F5-cleanup and now, the BTC and ETH positions that were floating green eventually stopped out too. The "DOGE is the loss factory" thesis from yesterday's review is incomplete — we now have a stop_hit on each of BTC, DOGE, and ETH, with holding times of 30–46 min each.

That's a more uniform failure pattern. It's not "DOGE has bad geometry"; it's "the strategy as configured produces stop-outs across all pairs at minutes-to-hours holding times." That sharpens F6's framing significantly. The structural problem isn't pair-specific — it's signal-horizon-mismatched bracket geometry across the board.

**6 inherited positions have closed since F5-cleanup** (was 3, now 6 of 11 — the rest are working through the system at their own pace via natural exit_manager handling). This is exactly what we wanted: inherited population winds down without manual intervention, and `fast_exits_native` filters them out of the analysis automatically.

## Answers to the Open Questions

1. **`since_hours = 24h`** — keep as-is until ≥20 native round trips. Then revisit. Confirmed.

2. **Inherited row visual cue** — keep current choice (gray text + italic ticker + `(inherited)` tag). I'll eyeball after a real visual check; can refine after.

3. **Best/worst phrasing** — leave as-is. The `↑` and `↓` arrows make the relative-within-dataset semantics clear, and the labels switch gracefully to "best gain / worst loss" naturally once we have wins. Don't over-engineer the empty-of-wins edge case.

4. **Stats above or below the table** — above is correct (matches existing summary-row pattern in the same partial). Confirmed.

5. **WS push for realized-stats** — defer. Polling at 5s is fine until the page becomes a hot path; SSE conversion is the same task as the autopilot UI WS upgrade I queued for the post-F6 hardening pass.

## What this means for F6

The new realized-data shape sharpens what F6 needs to learn:

- It's **not** a per-pair calibration problem (DOGE-only); it's a **signal-horizon mismatch across all pairs**
- Holding times are clustering at 30–60 min, but order-book imbalance has a 1–5 *second* predictive horizon
- The targets (set at swing-trade R-multiples) are far enough out that we never reach them before the stop fires

F6's job is to mine `fast_alerts` + `fast_orderbook` to learn the actual decay curve of each signal type, per pair, and produce a per-(ticker, alert_type) recommended max_hold_s + stop/target geometry. Then F7 (which the user has queued mentally) replaces position sizing with Kelly-fraction.

## Constraints respected

All confirmed:
- No migrations.
- No live-placement safety belts touched.
- No strategy thresholds tuned.
- No bracket policy changes.
- Existing UI sections untouched.
- `models/trading.py` and `.env.example` still uncommitted (correct).

## Workflow assessment

Three end-to-end protocol runs now (F5 cleanup, cleanup-2, trades history). All three:
- Operator effort: type `claude` once each.
- Claude Code stayed in scope.
- Surfaced surprises in Open Questions instead of silently expanding the brief.
- Quiet bug catches happened on the executor side without anyone needing to ask.

The protocol is paying its keep.

## Next move

Will discuss F6 framing with the operator before writing the next NEXT_TASK. F6 is the meatiest task we've queued so far — meaningfully larger than any of the cleanup or UI tasks — and the framing decisions matter.
