

---

## Part 3 — 2026-09-04 evening: from "zero events" to the first scored case (PRs #1318–#1326)

**Question the plan asked:** on a hydrated tape, does the bench point at ONE line where CHILI diverged from Ross? **Answer tonight: yes, on Robinhood; on the Alpaca canon, five gates deep and the fifth is a deployment mode, now carried.**

### The first scored case — SDOT 2026-06-26 (robinhood_agentic_mcp, stride 1, 160,148 ticks, 159,863 NBBO rows, 44.5 t/s)

| | |
|---|---|
| Ross | +$5,885.15 (main), entry ~09:20 ET on the $10.00 break (pin `price_match` / `tape_ambiguous`) |
| CHILI (replay) | **+$3.50**, 18 states, 7 fills, 4,190 events; `filled_exited_worse(trail_stop)` |
| recorded stage | `unknown(armed_no_entry)` [xref_verdict] — session 9198 in the ledger |
| **first_divergence** | **09:17:48 ET — Ross `filled` vs CHILI `watching`: `live_entry_backside_bench_veto reason=benched_backside_sticky` — `live_runner.py:34043` (VERIFIED tree)** |
| first_money_divergence | 09:17:48 ET (same second) |
| then | CHILI entered 09:18:12 (24 s after the veto), bailed out 09:18:32; re-entered 09:23:23, scaled out 09:23:55 / 09:24:03, exited 09:24:34 |
| grade | UNSCORABLE for credit (`pin_ambiguous`: several tape clusters at $10.00 — the earliest was chosen, all are listed). Honest; expected per plan §0.3 step 3. |
| Capture ratio | 0.0006 (main bucket); `%-of-equity` null — no row states Ross's equity |

This is anchor 4 of the plan's verification (the sticky backside bench latched on the faded premarket `benched_at_hod=17.8626`, vetoing at Ross's exact level) — reproduced in REPLAY on the hydrated tape with a code reference, not inferred from the recorded lane.

Run 1 (wall-clock events) vs run 2 (sim-clock events, #1319): same 7 fill instants, +$3.61 → +$3.50, sizes 24.71 → 24.24 sh on trades 2–3. The difference is real and production-faithful: the sizing now SEES the 09:18 bailout as recent (streak/fatigue derate); run 1 saw events 70 days in the future. **The no-op A/B (base vs base_copy, interleaved, same commit) is running on sink 1 to pin this — verification step 3.**

### The Alpaca canon — five gates, each a clean `rc=0` zero-event run until named

| # | tick skip / event | mechanism | fix |
|---|---|---|---|
| 1 | `alpaca_account_scope_unfrozen_or_mismatched` | quarantine gate reads ONLY `risk_snapshot_json` (#1304) | #1317 seeder freezes the admission proof field-for-field, all instants = arm anchor |
| 2 | `alpaca_account_generation_mismatch` | driver under `CHILI_PYTEST=1` does not read `.env` → expected id empty → seeder froze the mock identity | #1318 bench REFUSES an Alpaca run without `CHILI_ALPACA_EXPECTED_ACCOUNT_ID` (ambient, not contract) |
| 3 | `alpaca_adapter_account_generation_bind_failed`, `broker_calls: 0` | runner requires `adapter.bind_account_id(frozen) is True`; the mock had no such method | #1320 mock `bind_account_id` line-for-line vs `AlpacaSpotAdapter`; ONE identity source for seeder + mock; applied via `set_account_identity` (parity string pinned) |
| 4 | `live_held_execution_bbo_blocked` ×3 → `live_declined` → `live_cancelled` (71 s) | `_final_entry_bbo` requires `adapter.get_execution_bbo` with a provider-clocked age ≤ 2 s | #1322 mock `get_execution_bbo` mirroring the real adapter; provider clock = the sim instant; 7 tests drive the REAL validator |
| 5 | `live_entry_adaptive_risk_blocked reason=adaptive_risk_builder_source_invalid` ×26, 0 fills (31 min, 4,021 events) | the Alpaca entry builds adaptive-risk economics before legacy sizing; its capture provider is installed only by the sealed captured-paper service; the time-share lane runs on the LEGACY sizing escape (Option C, 2026-08-17) — `timeshare_supervisor.py:88` launches with `CHILI_MOMENTUM_LEGACY_ALPACA_DISPATCH_ENABLED=true`; the driver had the default (False) | #1326 the mode joins the account id as a required ambient env; the bench measures the lane AS DEPLOYED |

Observability fixed on the way: `live_entry_adaptive_risk_blocked` now carries `error_type` + `detail` (#1325) — the except arm folded `TypeError`/`ValueError` into the same label and dropped the builder's own detail (26 identical opaque blocks; L-G class).

### Instrument bugs the first real receipts exposed

- **Events on the wall clock (#1319).** `append_trading_automation_event` stamped `datetime.utcnow()` directly, bypassing the `_utcnow` chokepoint `replay_clock` freezes; fills were on the sim clock. 4,190 events graded `no_replay_events`. Writer takes `ts=None`; `_emit` passes `_utcnow()`; production byte-identical; other writers keep the wall clock (pinned).
- **Scorer graded a 7-fill run `no_arm_attempt` (#1323).** Tier-1 seeds admission and never emits arm events; the replay ladder now starts at `runner_never_started` when the receipt carries `seed_session_id` (`admission="harness_supplied"` in every detail).
- **`first_divergence 09:05:01 absent/watching` (#1323).** Row 0. Now anchored at Ross's first pin; with no pin, at CHILI's first fill (the Avoidance shape); with neither, NONE and the meta says why.
- **`candidate_no_submit` named the loudest veto (#1324).** 1,259 sticky-bench vetoes outvoted 26 attempt blockers. The qualifier is now the first refusal after each `pending_place`; both histograms stay in the detail.
- **Reporter hid the import error (#1323).** From a tree without `.env` the scorer import fails (`database_url Field required`) and every stage read `unavailable` as if the scorer had regressed. The problem line now carries the reason.
- **Evidence not in git (#1321).** `pins.json` / `corpus.json` / `corpus.csv` were untracked in wt-bench; any other tree died at `--pins: cannot read`.

### Sequencing lessons (mine)

- Merging moves `origin/main`; `verify_tree --ref origin/main` compares at run START — a bench launched from a not-yet-ff'd tree is refused (correct). Never ff, commit or edit tracked files in a tree while its driver runs: the receipt reads `tree.head` at the END (one RH receipt carries `4f801f63d ≠ 08de11057` from an evidence commit I made mid-run; content identical — 3 untracked files — proven by `git diff --stat`), and a DIRTY tree makes the timeline refuse verified code refs (one alpaca timeline is tagged UNVERIFIED for that reason). Launch from a SECOND worktree checked out at `origin/main`; keep a `wt-ref` worktree to check out the receipt's exact sha for timelines.
- Sink reads mid-run show only the last committed checkpoint (the driver holds one long transaction with savepoints): "657 events, stalled at 13:23:23" was a mirage; the receipt had 4,190.
- The per-tick skip reason lives only in the driver's `last_result` on stdout (the bench keeps 40 lines); a 3-minute direct driver run with the run's `contract_env` is the fastest read — but a window that starts mid-move has no frame warm-up and follows a different path (the 13:17–13:20 probe never reached a candidate).
- A scratchpad patch helper that opened the target for write BEFORE running the transform truncated `ross_bench_scoring.py` to 0 bytes when its assert fired; restored from git before any test ran. Transform first, then open.

### State at 22:15Z

- `main` @ #1325 (+#1326 pending): 8 PRs tonight on top of the 14 from the day.
- Sink 1: RH no-op A/B (base, base_copy) running, ETA ~22:42Z. Sink 2: free; alpaca SDOT relaunch with both lane keys next.
- Post-close task `CHILI-PostClose-Deploy-Hydrate-0904` armed 00:05Z (deploy main → containers; hydrate 60 IQFeed + 9 Polygon symbol-days). Premarket backup launch 2026-09-08 01:35 PT armed (09-07 is Labor Day).
- Not started: full 8-case baseline (needs tonight's hydration for UPC 06-29 / WETO 08-17), Phase 0.1 book, spread budget, burst-exit trigger.
