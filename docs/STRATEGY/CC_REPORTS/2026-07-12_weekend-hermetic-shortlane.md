# 2026-07-12 — Weekend report: hermetic replay campaign + short-lane P1

## TL;DR
The replay instrument is now **deterministic to the cent**; every 07-10/07-11 feature
verdict was re-measured honestly on it. The leader-ignition trio proved **net-negative
(−$3,350 / 2 windows) and was rolled back** (flags OFF, verified in-container); boom v1 and
MACD v1 are **inert** (triggers never fire); chase-cap v2 **rejected**. Live enters Monday on
the measured-best configuration. The short lane advanced from design to **P1 nearly complete**
(PRs #915/#916/#918), gated OFF until P2.

## The instrument (the week's real asset)
Six layers fixed to reach cent-exact reproducibility (proven by three identical-to-the-cent
pairs plus a −697.07 ×4 chain):
1. Per-run NBBO mirror + viability-board reset (PR #913) — the sink NBBO went empty and
   silently killed `tape_confirms_hold` (fail-closed), flipping JEM +$15,034→−$5,419 with no
   code change; two feature A/Bs were misattributed to that artifact.
2. Per-run RUN-STATE reset — sink outcome history feeds streak/cushion/TOD sizing, so
   results drifted run-to-run (a feature A/B diverged with the feature never firing).
3. Fresh-sink rebuild from prod DDL (bloat + planner regressions: 5,973ms→27.7ms hot query
   after CLUSTER + BRIN drop on the sink).
4. `brain_graph_nodes` seed — the DDL-only sink lacked the node registry; the FSM's
   evolution-trace insert hit an FK violation that poisoned sessions (the hidden killer
   behind the PendingRollback/segfault chain; also found genuine Windows commit-limit
   exhaustion, 0xC000012D).
5. yfinance 1m prepend PIN per (symbol, session-date) (PR #917) — the live fetch drifts
   across days (−697.07 Sat → −692.73 Sun, zero code change).
6. faulthandler + memory caps in the driver.

## Hermetic verdicts (final)
| Feature | Verdict |
|---|---|
| Leader-ignition trio (#914) | **Net-negative** JEM −$697→−$1,970, VRAX +$3,051→+$974 ⇒ **rolled back** (flags OFF in .env, in-container verified) |
| Vertical in-out-boom v1 | **Inert** — the r_per_min≥rr vertical bar never met on JEM/VRAX |
| MACD-negative-falling veto v1 | **Inert** — keyed to the escalation path; the OFF config re-enters via terminalize→re-arm (level 0) |
| Chase-cap v2 (EMA9 anchor) | **Rejected** — JEM −$2,676 vs −$697 |

**Key insight:** the re-entry "lockout" is NOT a bug. Terminalize→re-arm is the correct
re-entry channel (fresh counters) and beats every gate-level "unlock" tried. The measured
next lever is **sizing inversion** (small probes / full size on conviction): the hermetic
fills show full-size probes bleeding −$11k+ while winners rode at ordinary size. That lever
is data-gated — the 07-06 snapshot fix is verified working in prod (100% entry-regime
coverage on all 37 filled outcomes since 07-07), accumulating meta-label training samples.

New defect found while forensically reading VRAX attempts: **frozen structural_stop across
re-arms** (5.825 on all 12 attempts while entries walked 6.02→7.89) — queued.

## Short lane (docs/DESIGN/SHORT_SIDE_LANE.md)
- P0 (adapter position_intent, shortable/SSR surfacing, `alpaca_short` family): already in main.
- **#915** P1a: Trigger A parabolic-exhaustion detector (pure confluence, fail-closed OFI,
  anti-chase lower-high guard) + master gate `chili_momentum_short_enabled` (default False by
  design) + 10 unit tests.
- **#916** P1b-1: `side_long` threaded through all 13 geometry call sites (byte-identical
  paired proof −692.73==−692.73).
- **#918** P1b-2: direction-aware order sides + position intent on entry/repeg/all exits;
  v1 guards (shorts never pyramid/add ×5 gates; dead-man long-only) (proof −692.73==−692.73).
- Remaining: P1c candidate routing, P2 pricing inversion (SHORT-P2 comment sites), P3
  halt-up KILL + SSR gate before any paper enable.

## Monday readiness
Containers up; trio flags OFF and paper=True verified in-container; IQFeed bridges alive
(ticks minutes-fresh), schtasks Ready; 20GB RAM free. Known items: the exec worker's
"unhealthy" label is the scheduler-env role-guard (load-bearing flags — deliberately not
touched); the Robinhood token is expired (operator re-login; Alpaca unaffected).
