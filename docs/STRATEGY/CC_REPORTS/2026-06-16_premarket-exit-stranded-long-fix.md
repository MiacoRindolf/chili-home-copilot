# 2026-06-16 — Hours-aware equity exit: the BEEM/AHMA stranded-long bug

## TL;DR

CHILI **literally could not exit premarket equity positions.** The momentum reactive
exit (`live_runner._submit_live_market_exit`) was hours-blind: it submitted a
**regular-hours order during premarket/after-hours**, which Robinhood rejects
(`"Robinhood returned no order_id"`). After 8 retries → `live_exit_retry_cap_exceeded`
→ `STATE_LIVE_ERROR`, leaving a **naked long with no working exit**. This is exactly
why the operator had to manually exit AHMA — *holding wasn't a strategy choice, it was
a stuck order.* The operator's instinct to take over was correct.

This was the binding, money-at-risk explanation for "di ito marunong um-exit." Not a
judgment flaw in the brain — a plumbing bug.

## Root cause (verified live on the tree, not phantom line numbers)

`_submit_live_market_exit` placed `place_market_order` / `place_limit_order_gtc` with
**no** `market_hours_override` / `extended_hours_override`. With
`chili_autotrader_allow_extended_hours=False`, that becomes a regular-hours order.
- Premarket/after-hours → Robinhood rejects it outright.
- A bare **market** order is rejected in extended hours **even with** the override —
  RH only accepts **limits** in extended hours.

PROOF the venue works premarket: the scale-out LIMIT (`robinhood_spot.py` forces
`extended_hours=true`) placed successfully on BEEM premarket. The ENTRY already passed
`extended_hours=_entry_extended` (which is why BEEM/AHMA *entered* premarket fine). Only
the reactive exit was hours-blind.

## The fix (two parts, one PR)

**1. Hours-aware exit (the binding fix).** In `_submit_live_market_exit`: for an RH
equity session that is non-regular (`market_session_now(sess.symbol) != "regular"`),
pass the RH-only overrides AND **force a marketable LIMIT** — even on an urgent flatten
or the attempt-3+ market fallback (cross the bid hard, 8× guard, so it fills like a
market). Mirrors the entry idiom exactly. Caller-only — the adapters already thread the
kwargs; **no adapter signature change**.

  - **Parity (load-bearing):** the overrides are RH-only kwargs, passed **only** when
    equity + extended. Crypto (coinbase, which does not accept those kwargs) and
    regular-hours equity stay **byte-identical** — verified by 7 unit tests asserting
    the exact kwargs on a recording fake adapter.

**2. Honest stranded-position alert.** When the exit hits the retry cap AND the broker
**still holds** the position (not zero/dust), emit a distinct `live_exit_stranded_position`
event (`severity=critical`, held qty, last error) + an `_log.error`. This makes the
genuinely-stranded case (real money, no working exit) impossible to confuse with the
cosmetic arm-twin `live_error`s (blocked at arm, no position). The operator's monitoring
keys on this event.

  - *Not* a premarket protective stop: RH rejects stop orders in extended hours the same
    way it rejected the market sell, so a "protective stop" there would be false comfort.
    The hours-aware LIMIT (part 1) is the correct premarket flatten mechanism.

## Why the "84 live_errors" looked terrifying but were ~99% cosmetic

Two tiers: TIER 1 = the 2 *filled* premarket equities (BEEM, AHMA) that couldn't exit —
real money, the actual bug. TIER 2 = ~142 dual-venue arm-twins blocked at arm by the
active kill switch + wide spread — never held a position, no money at risk. The honest
alert (part 2) separates these going forward.

## Tests

- `tests/test_premarket_exit_hours_aware.py` (7, new): premarket/after-hours equity →
  extended-hours LIMIT with all three flags; urgent + attempt-3+ still LIMIT not market;
  regular-hours equity + crypto → no ext kwargs (byte-identical parity).
- `tests/test_evening_batch2_exits.py`: window widened 2500→5000 (the ladder block grew);
  behavioural parity now pinned by the new unit tests.

## Deploy

Crypto-flat at deploy (no live-state crypto sessions; no equity position held — only
queued_live). Per-git-sha image, recreate `chili-clean-recovery-scheduler`. Verify
`DATABASE_URL` key count == 1 and db-ping=chili after.

## Watch next

First live **premarket equity exit** — confirm it places (a LIMIT with the ext-hours
flags) and fills, instead of erroring. Until the operator SEES that, manually managing
premarket equity positions remains rational.

---

# Part 2 — Daily-loss kill switch froze a PROFITABLE day (the same incident's downstream)

## TL;DR

Operator asked why CHILI wasn't catching ANY of the day's big movers. Root cause: the
**global daily-loss kill switch was ACTIVE** — tripped 09:10 ET on a transient −$300, so
**every new automated entry was blocked all morning.** But the operator's account was
**+$286** and CHILI's own realized PnL was **+$265.60** (autotrader +$281, momentum −$16):
there was no live −$300 loss. The switch was **stale** — it tripped on a transient blip
(very likely the same stranded BEEM/AHMA premarket exits from Part 1 booking early
losses) and **never auto-cleared on recovery** because the only self-clear was the
ET-day-roll. A profitable day stayed locked out.

## Immediate action (operator-commanded)

Reset the kill switch via `governance.deactivate_kill_switch()` after verifying it was
safe: a fresh non-mutating `check_daily_loss_breach(activate=False)` returned
`breached=False` (realized +$265 ≫ −$300 cap). DB-persisted; scheduler poll interval = 0
(reads the flag on every arming check) so it picked up the un-freeze immediately. Verified:
25 sessions resumed arming within seconds (CPNG/COIN/INTC/… + crypto FLR-USD entry_candidate).

## Durable fix — intraday-recovery self-heal

New `governance._auto_clear_recovered_daily_breach`, wired into both `is_kill_switch_active`
entry points: when the switch is active for a `global_daily_loss_breach` AND today's
realized PnL has recovered to **above `-(cap * fraction)`**, auto-clear it.

  - **Hysteresis** (`chili_daily_loss_recovery_clear_fraction`, default 0.5): recovery must
    clear the cap by a margin, so realized hovering at the threshold can't trip/clear/trip.
    Adaptive — relative to the cap, not a fixed $. Set ≤ 0 to disable (date-roll/manual only).
  - **Throttle** (`chili_daily_loss_recovery_check_interval_s`, default 30s): the check runs
    a DB PnL sum and `is_kill_switch_active()` is on the hot order path.
  - **Gated**: never clears `manual_*` / non-daily / per-broker `backstop` activations.
    Re-trips normally if realized falls back to ≤ −cap.

9 unit tests: recovery-to-profit clears; still-in-loss + hysteresis-band stay frozen;
just-past-hysteresis clears; manual + backstop untouched; fraction=0 disables; throttle
skips within interval; hot-path integration.

## Why this recurs without the fix

This is the **second** time a transient/phantom daily-loss number froze the lane (06-15:
Coinbase-BP×margin basis bug, $60 cap, 8h freeze). The date-roll-only auto-clear means ANY
intraday blip that recovers locks out the rest of the day. The self-heal closes that class.

## Follow-up (not in this change)

The cap resolved via `usd_failsafe` (the adaptive 1.5%-of-equity leg couldn't compute →
fell back to the $300 floor). Investigate why `_account_equity_usd()` failed at 09:10 ET —
if equity resolution is flaky, the blunt $300 failsafe keeps biting instead of the intended
adaptive cap.
