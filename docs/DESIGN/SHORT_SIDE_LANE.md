# Short-side lane — archived and quarantined design note

**Status (2026-07-13): QUARANTINED; NO CERTIFIED SHORT EXECUTION.** This file preserves a
historical research direction only. It is not an implementation plan, rollout checklist,
timeline, or authorization to place a short order.

The `alpaca_short` execution-family seam and SDK position-intent mapping primitives exist,
but that does not constitute an executable short lane. The readiness layer, live runner,
risk reservation, reconciliation boundary, and adapter submit allowlist reject short
execution. Configuration flags cannot bypass those final transport checks.

## Enforced current state

- Alpaca is paper-only and US-equity long-only during recertification.
- A certified entry is exactly `BUY + BUY_TO_OPEN + DAY`.
- A certified close is exactly `SELL + SELL_TO_CLOSE` for an owned long.
- `SELL + SELL_TO_OPEN`, `BUY + BUY_TO_CLOSE`, the `alpaca_short` family, Alpaca crypto, and
  ambiguous or contradictory intent are blocked before broker transport.
- Unknown, manual, short, and opposite-sign broker positions are quarantined; CHILI does
  not infer ownership or flatten them by symbol resemblance.

The SDK's ability to represent `SELL_TO_OPEN` and `BUY_TO_CLOSE`, and generic helper code
with a `side_long=False` branch, prove only that primitives exist. They do not prove that
selection, sizing, borrow/locate, SSR handling, entry, partial fills, stops, emergency
covers, accounting, and reconciliation form a safe end-to-end short lifecycle.

## Why the prior plan is archived

The prior document proposed parabolic-exhaustion, failed-breakout, and gap-fade shorts and
included implementation phases and delivery estimates. Those ideas remain untested
hypotheses; they are not established Ross-derived edge and are not supported by a certified
broker lifecycle.

The earlier claim that a separate software execution family isolated short risk was also
insufficient. A long and short family pointed at the same Alpaca paper account still share
positions, orders, buying power, daily account change, and failure modes. The current long
recertification therefore uses one account-wide exposure guard across all users and symbols.
A future short lane cannot claim isolation merely by using another family string.

No structural stop, max-loss calculation, or halt watcher can bound the realized loss of a
short through a gap, trading halt, liquidity disappearance, borrow event, or broker delay.
Paper fills also do not reproduce live borrow availability, locate fees, SSR queue behavior,
or squeeze execution. The old wording and timelines understated these risks and are retired.

## Conditions for any future research

Any future short-side work requires a new, explicitly authorized recertification effort,
not a flag flip. At minimum it would need:

- a separate adapter and account identity with an independently modeled risk boundary;
- exact signed order intent and durable ownership for every entry, cover, and partial fill;
- broker-authoritative shortable/borrow/locate and SSR handling that fails closed;
- halt-up and gap-through recovery whose limitations are stated honestly;
- full lifecycle, ambiguity, restart, manual-position, and accounting tests;
- broker-fidelity replay and a forward paper soak; and
- a new safety review before any consideration of real money.

This list is an acceptance outline, not an operational sequence or estimate. Passing paper
tests would still not authorize live shorting.

## Observation-only instrumentation (2026-09-11, task [63])

The lane writes a **borrow receipt** on every Alpaca arm: `auto_arm.alpaca_borrow_receipt()` reads
the broker's own `asset.shortable` / `asset.easy_to_borrow` off the SAME asset probe the arm path
already needs, logs them as `[auto_arm] [alpaca_borrow]`, and appends a `live_alpaca_borrow_receipt`
event to the armed session. A missing flag is reported as the named string `"unknown"` — never
silently `False`.

It rides the **primary** `alpaca_spot` arm (`route=alpaca_primary`), which is the route the lane
actually runs (`CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER=true`; every live session of the
last 60 days is `alpaca_spot`), and the dormant RH-primary twin (`route=alpaca_twin`). The first
version of this receipt lived only inside the twin block, whose guard is
`_exec_family in ("robinhood_spot", "coinbase_spot")` — so it could never fire on the running lane.
That was machinery that cannot fire, the exact defect this work exists to remove, and it was caught
in review.

**No borrow flag reaches any decision.** `shortable` and `easy_to_borrow` are read by nothing: not
the arm decision, not sizing, not one order kwarg. The twin's **listing** verdict (`listed`) *is*
now sourced from the same record — one probe answers both questions instead of two — and a probe
that could not reach the broker is named (`alpaca_symbol_probe_error`) rather than collapsed into
"unavailable". `alpaca_short` remains blocked at `_alpaca_execution_quarantine_reason`
(`alpaca_short_execution_not_certified`) and every `side_long=False` envelope remains blocked
(`alpaca_long_direction_not_certified`).

### What the borrow already measured

On the 34 names the lane actually traded 2026-08-27..09-10: `shortable` **2/34 (5.9%)**,
`easy_to_borrow` **2/34** — DLTH and LIDR only. The rest of the momentum universe (TNON, WYHG,
MOBX, SKYQ, PCLA, FTFT, AHMA, BIAF, SUNE ...) is not shortable at all. The receipt is what keeps
that share a live, queryable number instead of a script someone ran once.

### What the tape measurement can and cannot say

`scripts/short_side_exhaustion_measure_63.py` replays the [62] population through the shipped
`PullbackCycleScanner`. Its answer to "is there a short in the exhausted state?" is **not
measurable on this universe**, and that is a different statement from "measured and negative":

* the **executable** population — exhausted-state setups on a name we can actually borrow — is
  **n=1** (LIDR 09-01, +0.34 R). One setup decides nothing;
* on the **whole** population (31 of 33 names unborrowable, i.e. reference only) the same
  measurement is **+2.60 R over 19 setups / 13 symbol-days** with a taker cover, **+3.62 R** with a
  resting-limit cover, and **−0.37 R** on the print-verdict exit — small, and sign-unstable across
  exit rules;
* **0 of 19 ever reached 1R** (max MFE 0.94 R). The stop must sit above the spike high, so 1R is
  **11.6% of price (p50)** while the realised downside is **2.1% (p50)**. The shorts mostly *do*
  reach their structure target (14 of 19); each one is only worth ~0.15 R.

An earlier draft of this section reported **−7.17 R** and "found no short edge to certify". That
number did not survive review: it was taken with **no scanner warm-up** (96% of it was one BIAF
setup decided on the 25th print of the day) and it counted **10 setups whose target sat at or above
their own entry** — not trades. Both are fixed in the script (`WARMUP_PRINTS = WINDOW_PRINTS`, and
non-trades excluded and reported separately). The conclusion for the lane is unchanged — **no short
arm** — but the reason is n=1 borrowable, not a measured loss.

## Operator posture

No operator action enables shorts today. Keep short-lane controls off. Do not change
`CHILI_ALPACA_PAPER`, inject a short family, or call adapter primitives to work around the
quarantine. The active task is to finish the long-only broker-truth recertification with the
runner disabled.

See `ALPACA_LANE.md` and `2026-07-13_chili_broker_truth_recertification.md`.
