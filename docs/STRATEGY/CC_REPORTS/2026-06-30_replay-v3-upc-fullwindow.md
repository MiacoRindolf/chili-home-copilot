# Replay v3 FULL-WINDOW SCAN — does the CURRENT system ENTER UPC at ANY 06-29 instant?

**Date:** 2026-06-30
**Author:** CHILI Code
**Scope:** Answer ONE decisive operator question. With CHILI's **current deployed Ross-completeness
build** (recency-grace ON + fill-on-verticals + all gates), would **UPC** have **ENTERED** at **any**
instant of its 2026-06-29 premarket run, or does a gate block it at **every** instant?
**Harness:** `scripts/replay_v3_upc_0629.py --full-window` (new mode; extends the focused 13:08 A/B).
**Method:** drive the REAL `live_runner.tick_live_session` FSM across the FULL strong-move window
(~12:40..13:35Z — the recorded $7.4→$18.84 explosion + topping/fade) on a sim clock + the
deterministic mock broker, with grace ON, recording at EACH recorded NBBO instant: did eligibility
(the grace's gated input) pass? did the entry TRIGGER fire (which gate if not)? did it ENTER + FILL?
**Related:** `docs/STRATEGY/CC_REPORTS/2026-06-30_replay-v3-upc-0629-grace-ab.md` (the focused 13:08
A/B this extends); `docs/STRATEGY/CC_REPORTS/2026-06-30_replay-v3-fidelity-r1-tierA-trigger.md`
(the recorded-events root cause); `docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md`.

---

## TL;DR — VERDICT: **UPC-STILL-BLOCKED** (no entry at any 06-29 instant)

Across **all 622** scanned NBBO instants of UPC's 06-29 strong-move window, the **current system
NEVER enters UPC**. The block is a **two-stage gate**, not a single one — and it is NOT (only) the
eligibility-grace's domain:

| Question | Answer |
|---|---|
| Does the current system ENTER UPC at any instant? | **NO** — 0 entries across 622 instants |
| Did the GRACE pass eligibility across the window? | **YES** (initial-eligible until the recorded 13:08:31 block; 178/622 instants had real fwd-momentum the grace keys on) |
| Did any entry TRIGGER ever fire? | **YES — but only with the score gate cleared**, and only during the pullback-chop, where it is then **backside-benched** (a CORRECT gate) |
| The binding block (faithful arm) | **`score_below_bar`** (342/622) + **`eligibility_block`** (280/622) |
| The deeper block (score-cleared arm) | **`backside_benched`** (243 instants) once the trigger is reachable |

**Plain answer:** UPC's miss is **not** a single gate. In the faithful run two gates split the
window: the **viability score gate** (UPC's real `0.55` < the `0.56` impulse_breakout floor) blocks
the front half (12:40 launch), and the **eligibility block** holds the back half (after 13:08:31).
The recency-grace works (eligibility holds across the launch + the real forward-momentum tape is
present), but it **cannot help** because the score gate blocks *first* and, after 13:08:31, the
recorded eligibility flips and the grace's flicker window has elapsed. When we clear the score gate
to expose the trigger, the trigger **does** fire on the launch — but UPC has already spiked to
$15.53 and **pulled back below its high-of-day**, so the **sticky-backside-bench** (a CORRECT gate —
Ross doesn't chase the backside) vetoes the trigger for 243 instants. The genuine new-high
continuation (13:14–13:30, $15→$18.84) lands **after** the recorded eligibility block, so it never
gets a clean shot.

This is a **real finding for the operator**: removing the grace alone (or even passing eligibility
all day) would **not** make UPC enter. The binding constraints are the **score bar** and the
**backside-bench**, layered behind eligibility.

---

## What was scanned (the window + the grid)

UPC's recorded 06-29 tape (5-min price buckets, UTC):

| time | price lo→hi | note |
|---|---|---|
| 08:15–12:35 | ~$10 → ~$7.2 | overnight chop / fade |
| **12:40–12:50** | **$7.4 → $15.53** | **the explosion** (12:50 bucket) |
| 12:55–13:08 | $15.5 → $10.85–12.7 | **pullback below HOD** (the recorded sid 9505 arm/block instant) |
| 13:14–13:30 | $12.8 → **$18.84** | the genuine new-high continuation (the day's top) |
| 13:31+ | fade | tops out |

The scan grid: the recorded `momentum_nbbo_spread_tape` over **[12:40:00..13:35:00]Z**, down-sampled
to one instant per **5s** → **622 instants**. OHLCV bars are resampled **as-of each sim instant** from
the real `iqfeed_trade_ticks` (550,780 ticks from premarket open 08:00, so the 5m frame carries the
≥25 bars the volume-confirmation gate needs). The forward-momentum / OFI leg of the grace reads
437,129 real mirrored ticks. **No lookahead** — the as-of provider slices bars to `≤ t` each tick.

## Two arms (the same real window)

The current system entering UPC requires **all** of: (a) the **score** gate passes, (b) **eligibility**
passes (grace), (c) a **trigger** fires, (d) **not** backside/midday-benched. We ran two arms to
attribute the block precisely:

### ARM 1 — FAITHFUL (UPC's REAL recorded `0.55` viability score — the real system)

```
gate histogram (622 instants):
    342  score_below_bar       <- 0.55 < the 0.56 impulse_breakout entry floor
    280  eligibility_block      <- after the recorded 13:08:31 live_eligible flip
entered_any=False  trigger_fired_any=False  grace_passed_any=True
```

The score gate (`_score_ok` is False ⇒ the trigger is never even evaluated) holds the front of the
window (12:40 launch, where eligibility IS true); the eligibility block holds the back. **UPC never
enters. No trigger is even reached.**

### ARM 2 — TRIGGER-ISOLATION (score cleared to `0.90` so the entry TRIGGER is the deciding gate)

```
gate histogram (622 instants):
    280  eligibility_block       <- unchanged (after 13:08:31)
    243  trigger:backside_benched <- the CORRECT backside gate vetoes the fired trigger
     29  watching_silent
     28  no_event   (FSM in live_entry_candidate this tick)
     28  ADVANCED   (FSM reached live_pending_entry)
     14  trigger:insufficient_bars (the first ~70s, before 25 bars accrue)
entered_any=False  trigger_fired_any=True  grace_passed_any=True
```

With the score gate cleared the trigger **does** fire on the launch — the FSM repeatedly reaches
`live_entry_candidate → live_pending_entry` (28 ADVANCED instants, ~12:53–13:01). But:

1. UPC has already spiked to $15.53 and **pulled back to $11–13 — below its high-of-day**. The
   **sticky-backside-bench** (`evaluate_sticky_backside_bench`) vetoes the fired trigger at **243**
   instants (`live_entry_backside_bench_veto`). This is the **CORRECT** Ross behavior: do not chase
   the backside of a vertical that just topped. (It matches the recorded reality — sid 9448 logged
   2 and sid 9743 logged 5 `live_entry_backside_benched`.)
2. The genuine **new-high continuation** ($15→$18.84, 13:14–13:30) lands **after** the recorded
   `13:08:31` eligibility flip, so in this window it sits behind `eligibility_block`.

So even with the score gate removed, the trigger that fires is on the **backside** (correctly
vetoed), and the clean new-high leg is behind the eligibility block. **UPC still never completes an
entry.**

> **Honest harness note on the 28 `ADVANCED` instants:** in arm 2 the FSM reaches `live_pending_entry`
> but oscillates back to `watching_live` the next tick rather than completing to `live_entered`. The
> focused 13:08 A/B (grace-isolation mode) DID complete a full fill at $11.56 by serving a single
> sustained trigger-passing frame; this full-window scan instead serves the REAL as-of-t bars and
> steps a long-lived session, so each tick re-evaluates and the per-instant pending→fill hand-off is
> not carried through to a completed `live_entered` fill in the harness. We therefore do NOT claim a
> real-priced fill from arm 2 — only that the **trigger is reachable** there. The faithful arm (arm 1)
> is the authoritative answer and needs no such caveat: the trigger is never even reached.

---

## The per-instant gate breakdown (the operator's question, answered per instant)

| window segment | grace/elig | faithful gate | what blocks |
|---|---|---|---|
| 12:40–13:08 (launch + pullback) | **eligible (Y)** | **`score_below_bar`** | the 0.55 < 0.56 score bar (trigger never evaluated). With score cleared: trigger fires → **`backside_benched`** (correct) |
| 13:08:36 onward (post-block) | **NOT eligible (N)** | **`eligibility_block`** | the recorded `live_eligible` flip; the grace's flicker window has elapsed |

So the answer to "is it ALWAYS the trigger? always eligibility? a mix?" is: **a mix, layered.**
- Front half: the **score gate** (and, behind it, the **backside-bench** once score is cleared).
- Back half: the **eligibility block** (the recorded 13:08:31 flip).
The trigger gate itself is reachable but is correctly **backside-benched** on the pullback.

---

## Did the GRACE do its job? (separately from the trigger)

**YES.** The eligibility input the grace gates on is **True** across the entire launch (the timeline's
initial state, until the recorded 13:08:31 block), and the grace's real-tape forward-momentum leg has
genuine evidence — **178/622** scanned instants show `ofi_level>0 ∧ ofi_slope≥0` on the real mirrored
tape. The grace is not the thing blocking UPC during the launch; the **score bar** is. After 13:08:31
the recorded eligibility flips and (per the focused A/B) the grace tolerates only the ~3s flicker
window — it is not designed to, and does not, hold eligibility open for the 20+ minutes to the
new-high leg. That is correct behavior, not a grace failure.

---

## Honesty caveats (what is real vs reconstructed)

This scan is bounded by two reconstructions; neither fabricates an entry:

1. **Eligibility reconstruction (Tier-B, event-derived).** `MomentumSymbolViability.live_eligible` is
   a single mutable column with no history table (design R1), so the eligibility **time-series** is
   rebuilt from session-9505's recorded events (eligible at confirm → block at 13:08:31). The
   Tier-A feasibility probe confirms the as-of-t scorer inputs are NOT recorded, so Tier-A is
   honestly infeasible and we use Tier-B. The initial-eligible-until-13:08:31 shape is faithful to
   the recorded block.
2. **OHLCV bar reconstruction.** Bars are resampled from the real trade ticks as-of-t. The recorded
   live bars / feed timing may differ slightly from what the live runner saw in real time (the P3
   parity caveats). The trigger verdict is whatever the REAL `tick_live_session` decides on these
   bars — not asserted.

**No entry is fabricated.** The faithful arm reaches no trigger at all; the trigger-isolation arm's
advances are reported as "trigger reachable," not as a completed fill.

---

## So what would it take for UPC to enter? (implications, not changes)

This scan only **measures**; it changes nothing. But it pinpoints the levers (in order of how they
bind):

1. **The score bar** (0.55 vs 0.56) blocked the *launch*. A +0.01 selection-score lift on this kind
   of explosive name would expose the trigger — but then:
2. **The backside-bench** correctly vetoes the pullback. The catchable leg was the **new-high
   continuation** (13:14–13:30, $15→$18.84), which a *fresh* arm (not sid 9505, which had already
   blocked) could in principle reach — IF a session were armed-and-eligible into that leg.
3. **Eligibility** (the recorded 13:08:31 flip) closed sid 9505's window before the new-high leg.

The decisive operator takeaway: **the grace is not the remaining blocker for UPC.** Chasing the grace
further will not catch this name. The catchable money was the **new-high continuation leg**, gated by
(a) a sub-threshold selection score on the launch and (b) the session's eligibility having flipped
before that leg. The backside-bench correctly kept CHILI out of the falling-knife pullback.

---

## Reproduce

```bash
set DATABASE_URL=postgresql://chili:chili@localhost:5433/chili          # READ-ONLY
set TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
python scripts/replay_v3_upc_0629.py --full-window           # the two-arm scan + verdict
python scripts/replay_v3_upc_0629.py --full-window --json    # machine-readable summary
python scripts/replay_v3_upc_0629.py                         # the original focused 13:08 A/B (unchanged)
```

DB safety: reads `chili` READ-ONLY; writes the sim session + mirrored ticks to `chili_test` only
(guarded — refuses any DB not ending in `_test`); purges all replay rows from the throwaway DB on
exit. Verified post-run: live `chili` has 0 `source='replay_v3'` ticks and 0 UPC `replay-v3-p1`
sessions.
