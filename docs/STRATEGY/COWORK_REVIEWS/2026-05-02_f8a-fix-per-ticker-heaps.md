# Cowork Review: f8a-fix-per-ticker-heaps

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-02_f8a-fix-per-ticker-heaps.md`
**Reviewer:** Cowork.
**Date:** 2026-05-02.

## Verdict

Surgical defect repair, fully verified, exactly as briefed. **5.4% → 100% capture rate** is the headline number. One commit, no scope creep, no surprises. Approve.

The interesting follow-up question isn't about this fix — it's that the soak window keeps catching quiet patches and we don't have organic data yet to actually evaluate the F8a fade hypothesis. That's a "wait and observe" problem, not a "queue another task" problem.

## What Claude Code did right

1. **Single-commit defect repair, brief followed exactly.** No scope creep, no opportunistic refactors. The change is `_pullback_heap` (list) → `_pullback_heaps` (dict[str, list]) with the corresponding schedule/drain/stats updates. 54 insertions, 37 deletions. Surgical.

2. **Verification numbers are unambiguous.** Pre-fix: 2/37 captured. Post-fix: 15/15. Per-ticker distribution confirms all 5 pairs are draining correctly. This is the kind of measurement that makes a defect repair definitively done.

3. **Caught a subtle nuance about the pre-fix bias.** Pre-fix, DOGE was ~47% captured (not 5.4%) because BTC's book emits were the most common trigger, and the global heap drain coincidentally enriched any popped entry with BTC's book — but only when the popped entry happened to also be BTC. That made the cross-ticker loss not just lossy, but *systemically biased*: high-volume tickers got more capture, low-volume tickers got almost none. The 5.4% global rate hid this. Claude Code surfaced it.

4. **Honest substitution on the brief's verification SQL.** The brief specified a 15-min window. Claude Code observed that both post-fix batches (catchup + one organic firing) fell outside that window by the time it ran the verification, and switched to `id > 2300` (= "since the fix landed") with the substitution explained. That's the right move — the brief's window assumption was wrong for the actual timing; substituting it made the test correct, not the test definition.

5. **Asserted invariant, not silent skip.** The drain path now contains:
   ```python
   assert obs.ticker == triggering_ticker, (
       f"_drain_pullback_due invariant violated: heap key "
       f"{triggering_ticker} contained entry for {obs.ticker}"
   )
   ```
   If a future refactor breaks per-ticker keying, this crashes loudly rather than silently corrupting decay data. That's the right guard for a load-bearing invariant. Claude Code's Open Question 4 flagged the `-O` optimization risk (asserts get stripped); see my answer below.

## Findings

### Pre-fix bias was systemic, not random

The pre-fix 5.4% capture rate was actually the **mean** of a heavily-biased distribution. BTC (the busiest ticker, most frequent trigger) got ~7%; DOGE got ~47% (because DOGE deferred entries were often popped on BTC book emits where the BTC book happened to be the only available context). Other pairs essentially got 0%.

Practical impact on the F6 dataset: any decay-miner statistics drawn from that period are biased toward BTC and DOGE. We should be cautious about treating the pre-fix decay rows as authoritative for AVAX/ETH/SOL specifically. Post-fix data is clean.

### We still don't have organic data

Only 1 organic `volume_breakout_long` fired between restart and Claude Code's verification window. Volume breakouts at `MULT=2.0` average ~120/24h historically — the small soak window caught a quiet patch. The 15 post-fix pullback alerts were 14 catchup + 1 organic.

**This is the ongoing reality of the F8a experiment**: we need ~24h of soak to accumulate enough organic firings to evaluate the fade hypothesis with statistical confidence. The pipeline is now correct; we just need patience.

## Answers to the Open Questions

### 1. Schedule a longer soak before declaring F8a ready to evaluate?

**Yes.** Let it run 24h+ at minimum. F8b (calibrate `DELAY_S`) needs the data anyway. No new task to queue for this — just observe.

### 2. The pre-fix +21 bps n=1 datapoint is now blended into running stats

Acceptable. Welford running stats converge correctly even when the same observation is "seen" twice across restart-replay. The pre-fix data was sparse enough that any pollution from cross-batch double-counting is overwhelmed by post-fix correct observations within hours. Don't worry.

### 3. `pullback_pending_heap` field name stays as-is

Yes, keep it. Operator UX consistency over field-name clarity for an internal stats field.

### 4. `assert` vs `raise RuntimeError`

**Switch to `raise RuntimeError`.** Claude Code is right that `-O` strips asserts. We don't run `-O` today, but the fast-path subsystem might at some point — and an invariant this load-bearing shouldn't be silently neutralized by a flag. The change is one line; fold it into the next hygiene pass.

## Engineering concerns (smaller, follow-up)

1. **`-O`-safe invariant guard.** Replace `assert obs.ticker == triggering_ticker` with `if obs.ticker != triggering_ticker: raise RuntimeError(...)`. Trivial. Fold into next hygiene pass.

2. **Watchdog task on decay_miner** has been deferred since F6. Now is a fine time to land it during the soak — it doesn't change behavior, just adds visibility into a silent failure mode.

3. **Stale `last_error` in `fast_path_status`** has been deferred since cleanup-2. Same reasoning as #2: small, hygiene, soak-safe.

## State of the world after F8a-fix

- 7 protocol runs landed clean (F5 cleanup, cleanup-2, trades-history, F6, F6.5, F8a, F8a-fix).
- 100% price-capture rate on `volume_breakout_pullback_long` going forward.
- All 8 fast-path safety belts intact.
- Calibration gates still blocking everything (correctly — F6 finding stands).
- Decay miner accumulating organic data on the new alert type.
- Soak window has been quiet but the system is correctly sized for the natural cadence.

## Workflow assessment

Seven runs. Pattern is fully reliable. Operator effort: type `claude` 7 times. Net code: ~3,500 LOC across the fast_path subsystem, ~2,500 LOC in supporting files (gates, tests, scripts), 7 migrations, multiple architectural shifts, with surfaced findings and zero silent regressions. The protocol is paying its keep.

## Decisions confirmed

- Per-ticker heaps locked in. Global cap stays. `pullback_pending_heap` field name unchanged.
- F8a experiment continues — we wait for organic data.
- F7 (Kelly sizing) stays deferred until F8 produces a tradeable signal.
- `-O`-safety on the assert: replace with `raise RuntimeError` in the next hygiene pass.

## Next move

Two reasonable paths while the F8a soak runs:

**Path A — Hygiene pass while the soak runs.** Bundle three small deferred items into one task:
- Watchdog on decay_miner asyncio task (visibility into silent failure modes)
- Clear stale `last_error` in `fast_path_status` after N successful streaming minutes (deferred since cleanup-2)
- Replace `assert` in scanner drain with `raise RuntimeError` for `-O` safety (from this run)

Three small commits, no strategy impact, soak-safe. Useful forward, doesn't compete with the F8a observation window.

**Path B — Pure observation window.** Don't queue anything. Wait 24h, then write F8a-evaluation as a strategic review of the accumulated decay data. F8b (calibrate `DELAY_S`) or F9 (new signal types) follows from the verdict.

My recommendation: **Path A** if you want forward motion during the soak; **Path B** if you'd rather minimize moving parts during data accumulation. Either is defensible. Plan A buys hardening that we'll want anyway; Plan B keeps the data-gathering window pristine.

What's your call?
