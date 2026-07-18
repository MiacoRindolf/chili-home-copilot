# Weekend entry-gate levers — 2026-07-18 (branch `weekend/entry-gate-levers`)

Four verified, replay-validated fixes built while the equity tape was closed
(Fri 8 PM ET → Mon 4 AM ET), targeting the scorecard-v2 priority stack.
All work in an isolated worktree off the activation branch tip (`dfd7c2f`);
the Monday captured-paper activation tree was never touched.

## The commits

| Commit | Fix | Evidence basis |
|---|---|---|
| `633feeb` | **Price-granular broken-quote ceiling** on the skip-spread entry gate: `max()`-only — real-EM scale + ONE documented base `min_spread_usd=0.08`. A 3-7 cent spread on a $1-2 name is no longer a "broken book" (was: fixed 300 bps cap with no price term). | VIVS 07-15 (+90%/min): 4 `wide_bbo_spread` blocks at 302-411 bps on mids $0.94-$2.32 — the whole Ross $1-3 winner class was vetoed at ignition. |
| `3243f35` | **Reaper keep-leg re-derivation**: `daily_breaking_major` is recomputed from recorded daily levels at the reap check when the persisted stamp is missing. Root cause: concurrent viability writers wholesale-replace `execution_readiness_json.extra`, erasing the stamp. Keep-only, monotonic. | VRAX 07-09 (+172% day): reaped at 13:52:36Z on a decayed 0.62 score while the move ran 8→13; the keep-leg inputs were correct but the stamp had been erased. |
| `68b8357` | **Tick-based ignition detector**: bridge-side, separate pg_notify channel `momentum_iqfeed_ignition` (sealed captured-paper envelope untouched — 21/21 trigger tests), adaptive floors, caps+dedup, second LISTEN in the runner loop → `admit_ross_event(source="ignition_tick")`. Standing-watch eligible window widened (measured headroom: 259 peak vs ~500 limit). | PLSM/ERNA/VIVS were flagged 167-473 s after ignition; none was ever admitted at all on its day. Detector full-day sim: 93 nominations = the day's real mover list; nomination lands 1-5 s after first frames. |
| `54dbeca` | **Tick-stream volume confirmation fallback**: below 25 bars (15m bars — cold names are bar-starved until ~10:15 ET), the volume-confirmation intent is computed from the tick tape (60s $-vol, 10s prints, tick-VWAP, 60s-low; adaptive 4× own-baseline floors; as-of bounded, fail-closed; kill-switch `CHILI_MOMENTUM_TICK_VOL_FALLBACK_ENABLED`). Warm path byte-identical. | VIVS 07-15 after the spread fix: still 0 entries — 4,257 `insufficient_bars` waits. A freshly-ignited symbol cannot have 25 bars by construction. |

## Replay validation (sink `chili_weekend_test`, exact-prod DDL)

- **VIVS 2026-07-15, 11:23-13:00 window:**
  - Before (evidence run, `chili_test` session 8): 4 wide_bbo blocks, 4,257 bar-starve waits, **0 entries ever**.
  - After spread fix only: wide_bbo **0**, still 0 entries (bar starvation next).
  - After spread + tick-vol (session 4): **first-ever entry on this class** — BUY 156 @ 3.47 → stop 3.30, **−$26.53**, then 8 `g4_reentry_escalation_blocked (non_structural_trigger)`.
- **NXTC 2026-07-14 regression guard (session 3):** 4 entries + 4 exits — **identical round-trip count to the scorecard baseline** (fix provably inert on warm >$2.67 names).

## Honest read

The plumbing now works end-to-end on the cold-spiker class: CHILI can SEE
(detector), ADMIT (tick-vol fallback), and FILL a +90% mover minutes after
ignition — previously impossible at three separate layers by construction.
The one fill it took was **chase-shaped** (entered 3.47 near the 3.64 local
top, stopped −4.9%; discipline held, loss small). That residual gap is
**entry SHAPE** — exactly scorecard ①-b — and the first-dip candidate mode
that activates with Monday's captured-paper lane is the designed answer.
Secondary friction: the re-entry escalation path demanded a structural
trigger 8× after the stop (worth a look after Monday).

## PIT latency table (the discovery gap, measured lookahead-free)

| Symbol | Ignition | Actual subscribe | Detector (deployable) | Detector (if pre-watched) |
|---|---|---|---|---|
| PLSM 07-13 | 11:58:00Z | +274 s | +2 s after first frame | 11:57:27 (before ignition minute) |
| ERNA 07-15 | 11:50:00Z | +473 s | +5 s | +36 s |
| VIVS 07-15 | 12:05:00Z | +167 s | +1 s | +13 s |

Hard finding: none of the three had any pre-ignition presence in our data —
the remaining 3-8 min is the Massive snapshot funnel (universe eye), the
next lever after this ships.

## Open items

- Merge sequencing: default = land on the activation branch AFTER the first
  clean captured-paper session (same reasoning as PR #925); operator may
  override to include sooner.
- Deploy notes: host bridge restart + `chili_iqfeed_l1_authoritative_bridge_build`
  re-stamp needed for the detector; app image rebuild for the runner-side
  changes; verify in-container bindings per standing practice.
- Two auxiliary suites hang in this environment (`test_iqfeed_provider_loop_supervisor`,
  `test_iqfeed_capture_only_smoke`) — not verified against base HEAD; triage
  separately. Two pre-existing failures in `test_momentum_auto_arm` and the
  vertical-chase suites predate this branch (stash-verified).
- Ladder detectors (pullback/vwap/flush) remain 1m-bar-gated (~10 min
  warmup); if replays show winners starving there, same fix shape applies.
