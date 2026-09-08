# Pass 3 — the exit-side ledger, and why the exit fix cannot ship alone

DATE: 2026-09-06
BASELINE: main @ `9383324b2` (burst #1354 + FSM edge #1356 + noise-floor OFF #1357 + replay
clock #1359), stride 1–2, `$100k / $4k`, interleaved, clean sink per run
COVERAGE: **86 Alpaca + 82 Robinhood symbol-days** — the full clean baseline, complete

## 1. Where the exit-side dollars are

`scratchpad/exit_census.py` reconstructs every leg from the receipts and asks what the leg
could still have made in the 30 minutes after it exited (max bid − exit price, times the
leg's shares).

### Alpaca — the deployment target — Ross winners

| exit reason | legs | Σ leg P&L | Σ left on table | ≤60 s | median hold |
|---|---:|---:|---:|---:|---:|
| bailout | 73 | −$1,442.37 | $12,792.75 | 63 | 23 s |
| target | 59 | +$3,972.99 | $10,267.84 | 29 | 61 s |
| trail_stop | 82 | −$192.05 | $9,852.07 | 42 | 59 s |
| stop | 22 | −$374.87 | $3,536.61 | 20 | 21 s |
| deadman_stop | 4 | −$55.35 | $268.78 | 3 | 54 s |

Across both families the opinion bailouts ended **189 Ross-winner legs** for **−$4,627**
with **$41,575** left behind, **163 of them inside 60 seconds** of the fill.

**The tick-cadence exit (#1261) ended ZERO legs in the entire baseline.** Not "rarely" —
zero.

`target` at 59 legs and $10,268 left is the whole-position flatten that PATH B fixes: on
Alpaca the first-target touch sold the entire position instead of a tranche, because the
decision carried an execution-family term. That is now a feasibility question instead
(PR #1360).

## 2. The operator's doctrine, replayed against all 189

The rule, stated for months: hold while the 10-second candles are green, exit on the first
red bar that breaks the prior bar's low. `scratchpad/bailout_cadence.py` rebuilds the 10-s
candles from the per-second tape and asks what the rule would have said at each bail.

| family | Ross | cadence said | legs | Σ leg P&L | Σ left on table |
|---|---|---|---:|---:|---:|
| alpaca | winner | **hold** | 30 | −$560.70 | $4,981.56 |
| alpaca | winner | exit | 43 | −$881.67 | $7,811.19 |
| rh | winner | **hold** | 71 | −$1,636.96 | $19,141.06 |
| rh | winner | exit | 45 | −$1,547.59 | $9,640.91 |

**101 of 189 (53%)** fired while the cadence still said HOLD, worth $24,123.

The other 88 are the finding that matters: **the rule agreed, and the price still came
back**, leaving $17,452. So the doctrine is not wrong, and it is also not sufficient on its
own. The median bail lands 22–25 s after the fill, two bars in, where any routine pullback
in a small-cap satisfies "a red bar broke the prior low".

What separates the two groups is not the rule. It is **when the rule is allowed to speak**.

## 3. What "left on the table" is NOT

It is measured per LEG. It does not subtract what the NEXT leg recaptured after the bail,
and the `diet` arm proves that recapture is large. So $41,575 says where the dollars ARE.
It does not promise any fix recovers them.

## 4. Removing the opinion exits is measured as WRONG

The `diet` arm turns all three off (`CHILI_MOMENTUM_BREAKOUT_BAILOUT_ENABLED`,
`..._BOS_EXIT_LIVE_ENABLED`, `..._LOST_VWAP_FLATTEN_ENABLED`).

| | Σ Δ | improved | worsened | pairs |
|---|---:|---:|---:|---:|
| Ross winners | −$119.91 | 0 | 2 | 2 |
| Ross losers (negative control) | −$45.94 | 3 | 3 | 7 |

It fails on both sides.

## 5. The mechanism, pointed at one line

VEEE 07-13 ml1 is the case that names it.

| | legs | fills | P&L |
|---|---:|---:|---:|
| baseline | 11 | 22 | **+$81.15** |
| diet (opinion exits off) | 2 | 4 | **−$29.89** |

The baseline's profit is two target hits at 12:58:25 (+$85.80) and 12:59:00 (+$46.74),
**reached after four fast bailouts cycled the position through the chop**.

In the diet run the second leg exits by `trail_stop` at 12:54:43, and then the session never
re-enters. The events after that timestamp say why:

| blocking event | count |
|---|---:|
| `live_entry_trigger_wait` (`g4_reentry_escalation_wait`) | 2,167 |
| `live_entry_backside_bench_veto` | 541 |
| `g4_reentry_escalation_blocked` | 440 |
| `momentum_reentry_chase_blocked` | 151 |

**The exit reason decides whether re-entry is allowed.** A bailout recycles; a trail_stop
counts as a stop-out and the G4 re-entry escalation then refuses every attempt. The fast
bailouts were not only an exit — they were the re-entry generator.

This is why the `v4e` lockout-watch arm is inert (winners −$4.69, losers $0.00, 0 worsened
on 7 pairs): it grants re-entries into an exit path that immediately bails them out again.
Every winner case in that arm exits almost entirely to `bailout`.

## 6. What follows for the program

**The exit fix and the re-entry fix are one lever, not two.** Measured separately, each
looks worthless: the exit fix removes the re-entry generator, and the re-entry fix hands
legs to a broken exit. They have to be benched together.

- The lever is a **structure floor**, not removal: the three opinion exits may not fire
  before `chili_momentum_opinion_exit_min_hold_seconds` = 30.0 s, derived as the p75 of the
  hold distribution over those 189 legs. The structural stop, the #769 max-loss circuit and
  the burst-window exit are evaluated above those blocks and are deliberately not gated, and
  the floor fails open when the fill time cannot be read.
- The shipping candidate is `fix/exit-floor-plus-reentry` = baseline + esc5d (PR #1361) +
  unlock2 (PR #1362) + the floor.

## 7. A/B verdicts so far

| arm | winners Σ Δ | losers Σ Δ | worsened | verdict |
|---|---:|---:|---:|---|
| esc5d (G4 substitute v5d) | **+$424.79** | $0.00 | 0 | **PASS** — [#1361](https://github.com/MiacoRindolf/chili-home-copilot/pull/1361) |
| unlock2 (07:00 seller-unlock) | **+$59.87** | +$116.82 | 0 | **PASS** — [#1362](https://github.com/MiacoRindolf/chili-home-copilot/pull/1362) |
| v4e (lockout watch) | −$4.69 | $0.00 | 0 | FAIL — inert, do not ship |
| diet (opinion exits OFF) | −$119.91 | −$45.94 | 3 | FAIL — removal is the wrong shape |
| tick (cadence exit ON) | — | 0.00 on the paired loser | 0 | INERT alone — the bailouts fire first |
| floor + re-entry (combined) | pending | pending | — | queued, bench is memory-bound |

## 8. Bench constraint

The ceiling is the Windows commit limit, not RAM: roughly 6 GB of commit per replay driver,
and free virtual memory has been sitting at 0.6–5.4 GB with 8–12 drivers running. Dropping
the docker-desktop VM caches returns about 1 GB. No arm may be added until the driver count
falls; a driver that starts without headroom dies with rc 3221225794 in hundredths of a
second and takes its window with it.
