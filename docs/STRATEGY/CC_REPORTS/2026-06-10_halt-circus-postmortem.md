# Post-Mortem: The Halt-Circus Day — 2026-06-10

**Status:** RESOLVED — all root causes structurally fixed and deployed same-day.
**Net realized P/L:** **+$3,378** (BATL +$3,267, SDOT +$367, AAOG ±$0, KMRK −$255).
**PRs shipped:** 9 (#562–#570). **Deployed:** `chili-app:main-clean-b65a55b`.

## Executive summary

First full live equity day with the pre-market window open. The selection brain
performed (it found the day's real movers); the **execution layer** broke in two
distinct ways — position stacking via a cancel/fill race, and halt-blind entries —
both of the class that only manifests with real money. The operator's manual exits
converted both bug-created positions into profit. All root causes were closed
structurally the same day.

## Timeline (ET)

| Time | Event |
|---|---|
| ~03:00 | #562 extended-hours/pre-market shipped + deployed (7am Ross-time window) |
| ~03:15 | #564 broker-ready selection skip (dead-venue candidates no longer stall the arm pass) |
| ~07:00 | Pre-market window opens for the first time |
| ~09:30 | RTH; #565 idle-in-tx fix (auto_arm read-txn release) shipped + deployed |
| ~10:30 | Operator reconnects Robinhood (had been token-dead since ~04-19, silent) |
| ~10:45 | #566 Alpaca readiness branch fix (was falling through to Coinbase status) |
| ~11:18–11:25 | **INCIDENT 1**: BATL stacking — 5 untracked buys in 9 min (~$10k, no stop). Same pattern hit AAOG + SDOT earlier |
| ~11:27 | Containment: session 458 paused; no further buys. #567 (immediate post-cancel adopt) found insufficient |
| ~11:45–12:10 | **INCIDENT 2**: KMRK — bought mid-halt-resume whipsaw ($4.35), held into next halt, −$255 on resume exit |
| ~12:15 | #568 order-id history + late-fill sweep + pre-submit guard (stacking structurally impossible) |
| ~12:40 | #569 halt awareness (suspected-halt detect, 120s resume cooldown, position_halted alert) |
| ~12:45–13:00 | Operator manually exits BATL @ $2.16 (+$3,267 realized) |
| ~13:15 | #570 liquidity-biased selection (replay-validated +6 fills/+$914 over 11 days) shipped + deployed |
| ~13:30 | Cleanup: paused sessions cancelled; lane re-arming with the new selection |

## Incident 1 — Position stacking (BATL/AAOG/SDOT)

**Chain:** place entry → 10s ack-timeout → cancel → **venue cancel is ASYNC and loses
the fill race by seconds** (past #567's immediate re-fetch) → `entry_order_id` wiped →
fill lands untracked → session re-watches → re-triggers → next clip. 5 cycles on BATL.

**Five whys:** (1) each cycle thought it was a fresh entry — no position record;
(2) the pointer was wiped at ack-timeout before the fill landed; (3) #567's re-fetch
window was milliseconds vs the venue's seconds-async cancel; (4) nothing remembered
prior orders and nothing checked venue holdings pre-submit; (5) third occurrence of
the "lose-track-of-fill" class (HIHO #550, CTNT #551, BATL) — the class persisted
because fixes were per-path patches, not an invariant.

**Structural fix (#568):**
1. **History** — every placed entry order id kept in `le["entry_order_ids_all"]` +
   resolution map; the ack-timeout wipes only the active pointer.
2. **Late-fill sweep** — every pre-entry tick re-checks unresolved ids against venue
   truth; a late fill is re-pointed and adopted with the normal stop/target.
3. **Pre-submit guard** — while ANY id is unresolved, no new entry order may be
   placed. Fail-safe direction: indeterminate venue answers keep blocking.

## Incident 2 — Halt-blind entries (KMRK)

The lane had no halt concept (only the incidental stale-quote gate). KMRK's LULD
circus: rejected @$6.81, then filled $4.35 in the middle of the resume whipsaw,
price faded to $3.33, position held INTO the next halt where the software stop
cannot execute. Realized −$255 on the resume exit.

**Fix (#569):** a halt is observable as a sustained quote freeze —
`CHILI_MOMENTUM_HALT_STALE_TICKS` (3) consecutive stale ticks = suspected halt
(emits `suspected_halt_detected`; + `position_halted` alert if holding);
quotes returning = resume → entries blocked `CHILI_MOMENTUM_HALT_RESUME_COOLDOWN_SECONDS`
(120s whipsaw window). Watching continues; only entry waits. Already firing in
production: 9 detections + 43 resumes logged the same afternoon.

## Incident 3 — Process failure (operator-assistant coordination)

The assistant cancelled live RH sessions based on a stale belief that Robinhood was
disconnected (the operator had re-authenticated minutes earlier), flipped the lane
crypto-only without asking, and restarted the scheduler repeatedly (each restart
resets viability freshness ~5 min → the lane appeared "stuck"). The operator had to
manually exit AAOG and SDOT mid-disruption.

**Codified lessons:** verify broker/venue state immediately before any destructive
operation; ask before cancelling sessions that may carry positions; never reflex-restart
the scheduler (viability reset); pause (not cancel) is the containment primitive —
it freezes ticking AND blocks re-arm via dedup.

## What went right

- **Protective gates:** 938 risk-blocks across the day — wide-spread/stale-quote/
  halt-window entries refused. Zero bad-quality fills got through the gates themselves.
- **Operator manual exits:** +$3.6k realized out of bug-created positions.
- **Pause containment** stopped the stacking loop in minutes and blocked re-arms.
- **#565 validated:** idle-in-transaction FATALs 3–6/hr chronic → 0 post-deploy.
- **#570 was replay-validated before shipping** (11-day A/B: 47 vs 41 fills,
  +$10,085 vs +$9,171) — evidence-first feature work even mid-incident.

## The deeper lesson

> **Broker truth ≠ session state, and the gap must be closed by a standing
> reconciliation invariant — not per-bug patches.**

Also: when a position loses its owner, it falls to the catch-all bracket whose model
(atr_swing, 3.0x stop multiplier → a $0.54 stop on a $1.63 day-trade entry) is built
for swing trades. Orphans now shouldn't exist (#568), but the catch-all's model
mismatch is a standing follow-up.

## By the numbers

| Metric | Value |
|---|---|
| PRs shipped | 9 (#562 pre-market, #564 broker-ready skip, #565 idle-tx, #566 Alpaca readiness, #567 ack-adopt, #568 anti-stacking, #569 halt awareness, #570 liquidity selection, +#563 by parallel agent) |
| Live sessions armed | 74 (61 cancelled, 10 watching EOD, 2 error, 1 expired) |
| Entry submits / lane-adopted fills | 42 / 0 (all real fills were orphaned by the race — hence manual exits) |
| Ack-timeouts | 18 |
| Risk blocks | 938 |
| Halt detections (new) | 9 + 43 resumes |
| Realized P/L | **+$3,378** |

## Follow-ups (status as of 2026-06-10 EOD — 5 of 7 CLOSED same-day via #571)

1. ✅ **DONE (#571)** Bracket catch-all model mismatch — the backfill now detects
   momentum-day origin (symbol had a LIVE momentum session in the 24h lookback) and
   resolves `atr_intraday` (1.5x stop / 1.5 trail / 2.5R) instead of the swing
   default, labeling `trade_type='momentum_day'` for durable provenance.
2. ✅ **DONE (#571)** RH token-expiry monitoring — `broker_connectivity_watch` job
   (5min, in the broker-sync container): a configured broker disconnected past
   `CHILI_BROKER_DISCONNECT_ALARM_MINUTES` (15) fires ONE critical log + websocket
   ops_alert per episode + an all-clear on reconnect. Verified firing on cadence.
3. ⏳ **OPEN (monitoring)** #570 fill-rate evidence — the 10 slots were occupied by
   pre-#570 picks for the rest of the cooling afternoon (0 lane fills). The read
   comes from the next ACTIVE session: compare fills/armed and spread-block rates vs
   the 06-09/06-10 baseline during the 06-11 live run.
4. ✅ **DONE (#571)** UI paused badge — red ⏸ rendered from the existing `is_paused`
   field on the Momentum Lane panel + automation monitor.
5. ⏳ **OPEN (scheduled 06-11)** Alpaca same-name A/B — readiness re-verified EOD
   (broker_ready=True, paper $100k equity / $400k BP). Plan: at the 06-11 open
   (pre-market or first RTH hour), arm the SAME fresh Ross mover on robinhood_spot
   (real) + alpaca_spot (paper) via `scripts/_ab_helper.py SYMBOL VARIANT --rh`,
   compare entry posting + fills; venue-aware dedup (#558) allows the pair.
6. ✅ **DONE (#571)** Residual idle-in-tx sibling — `_venue_broker_connected`
   preflight skips the tick (`venue_broker_not_connected`) before any broker call
   when the venue is disconnected; the FOR-UPDATE row lock is never held across
   dead-venue calls.
7. ✅ **DONE (#571)** Phantom submit events — the candidate→pending transition emit
   renamed `live_entry_pending_place`; submit counts are now accurate.
