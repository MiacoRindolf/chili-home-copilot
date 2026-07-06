# Ross Capture Parity — P0 (evidence + integrity) report

**2026-07-06. Executor: Claude Code. Design: `docs/DESIGN/ROSS_CAPTURE_PARITY.md`. Task: `NEXT_TASK` P0.**
**Outcome: P0 COMPLETE. Oracle proven faithful; the meta-label data-gate root-caused to a real, fixable defect that is shared-root with criterion ③.**

## P0.1 — Evidence preserved ✅
The +$264.25 master scorecard + 07-05 session report committed to `docs/STRATEGY/CC_REPORTS/` (PR #853). No longer scratch-only.

## P0.2 — Oracle integrity: FAITHFUL, with a documented reproduction recipe ✅
- **JEM anchor reproduced byte-identical: +$314.53 (7 buys/7 exits); SVRE −$0.33 exact.** → the code + FSM instrument have NOT drifted.
- **Discovery — the replay is SINK-STATE SENSITIVE (~$1/mover).** JEM = +314.53 on a clean sink vs +313.34 on a residual-state clone. The absolute PnL depends on the test DB's residual session/counter rows. → the oracle is trustworthy for **A/B deltas (same sink, both arms)** — its intended use per the design's L4 — but NOT for byte-exact absolute reproduction across sink states.
- **Contamination confirmed, not code drift.** A first full run showed CELZ/CANF/TC → 0 buys; cause = a **parallel session concurrently using `chili_test`** (the classic "run ONE at a time vs chili_test" truncate collision), plus running during live market hours. Reproducing the same movers on an **isolated sink** restored non-zero results.
- **`chili_staging` cannot be the replay source** — it lacks the tape tables (`iqfeed_trade_ticks` absent); it is a partial snapshot.
- **Go-forward A/B recipe (for P1-P3):** source = `chili` (read-only, historical reads are stable under live writes); sink = a dedicated `*_test` DB isolated from any parallel runner (created `chili_repro_test`); both arms on the same sink state → clean delta.

## P0.3 — Meta-label data-gate: ROOT-CAUSED (the headline finding) 🎯
**Symptom:** `momentum_automation_outcomes.entry_regime_snapshot_json` is empty (`{}`) for essentially all prod live fills (2/942 all-time; 0/51 today) — the meta-label edge model (the #1 lever) has been silently data-starved.

**Full trace (each candidate cause tested, not assumed):**
1. ❌ *"Capture block not reached"* (my first guess) — **wrong.** `live_runner.py:8272` is the ONLY live `live_entry_filled` emission; the capture block (8284-8360) runs on every fill.
2. ❌ *OHLCV fetch fails* — **ruled out.** `_replay_aware_fetch_ohlcv_df(sym,'15m','5d')` returns data in the live container (AAPL 161 rows, LUCY 61).
3. ❌ *Input viability regime is empty* — **ruled out.** `momentum_symbol_viability.regime_snapshot_json` is fully populated in prod (3610/3610 today, 104308/104308 all-time). `regime = via.regime_snapshot_json` (line 8140) is fetched in the same fill-handler branch, right before the write.
4. ❌ *Key mismatch on persistence* — **ruled out.** `_commit_le` writes under `KEY_LIVE_EXEC="momentum_live_execution"`; `outcome_extract` reads the same key (`KEY_LIVE`, identical string).
5. ✅ **DEFINITIVE:** the persisted session `le` (`risk_snapshot_json->'momentum_live_execution'`) on the 7 filled sessions has **no `entry_regime_snapshot_json`, no `entry_features`, AND no `position`** — even though the fill handler sets `le["position"]` at 8128 and the snapshot at 8293. **The fill-handler's `le` writes do not survive.** All 7 sessions are `fill → live_cancelled`; the cancel/close path wipes `le`, discarding the frozen entry snapshot.

**Conclusion — shared root with criterion ③.** The capture is not broken; the sessions **fill then immediately cancel** and the cancel wipes the frozen entry snapshot. This is the SAME fill-then-cancel / orphan behavior behind CHILI's own give-back losses (③) — the class #854 (`bc36984`, reconcile-not-terminalize) began fixing. **The meta-label data-gate opens as sessions HOLD instead of churning.**

**The precise fix (queued, needs one held-fill confirmation):**
- Make the frozen entry snapshot **immutable/durable at fill** so a later cancel cannot erase it — e.g., persist `entry_regime_snapshot_json` + `entry_features` to the outcome (or a dedicated column) at fill time, not only onto the mutable `le` that the cancel path clears.
- **Confirm hypothesis first:** trace ONE session that fills AND holds to a clean exit — if its `le`/outcome retains the snapshot, the gap is purely a fill-then-cancel symptom (fix = durable snapshot + reduce churn); if a held session also loses it, there is a second persistence defect. This needs a stable lane + a held fill.

## P0.4 — Bindings: verified ✅
All design-appendix bindings matched the live container; two flagged discrepancies settled: `entry_extension_floor_pct=0.10` (env-override of the 0.08 default — the guardrail is correct), `mfe_target_live=True` (confirmed live; the earlier "not found" was searching the stale worktree). ⚠️ The lane image churned during P0 (`d98c924 → bc36984 → d8cc05d`) via a parallel session's #854/#855 — verify BINDINGS on the exec worker after each such deploy.

## Recommendation (next task)
Make the **entry-snapshot durability fix the first code change** (P2-prerequisite; unblocks the #1 lever AND is shared-root with ③). It is small, additive, and insulated — but gate it on the held-fill confirmation above. Defer P1 (event-driven arming port) until the lane image churn from the parallel replay/lane work settles, to avoid deploy collisions.
