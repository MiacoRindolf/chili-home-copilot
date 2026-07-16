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

## Operator posture

No operator action enables shorts today. Keep short-lane controls off. Do not change
`CHILI_ALPACA_PAPER`, inject a short family, or call adapter primitives to work around the
quarantine. The active task is to finish the long-only broker-truth recertification with the
runner disabled.

See `ALPACA_LANE.md` and `2026-07-13_chili_broker_truth_recertification.md`.
