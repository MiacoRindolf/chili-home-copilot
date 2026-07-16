# Alpaca execution lane — current recertification boundary

**Status (2026-07-13): IMPLEMENTED, UNDER RECERTIFICATION.** The current repair branch is
not merged or deployed, and the live runner is disabled. The certified scope is Alpaca
paper, US equities, long positions only. Live Alpaca, crypto, and short execution are not
certified and cannot be enabled by changing a strategy flag.

This document describes the enforced safety boundary in the current code. Earlier
greenfield, passive-posting, and live-ramp plans are historical context, not current
capability or authorization.

## What is actually certified

- The only certified account scope is `alpaca:paper`.
- A risk-increasing order must be an equity long entry with the exact pair
  `BUY + BUY_TO_OPEN` and `DAY` time in force.
- A new entry is regular-hours-only. Immediately before admission and again before
  transport, a fresh Alpaca clock must report the market open and provide a valid future
  `next_close`; the order is rejected at or beyond the derived close cutoff. A local clock,
  calendar assumption, or extended-hours quote cannot authorize an entry.
- Entry quantity is whole-share only and is submitted as one complete approved request.
  Scale-ins, partial-size tactics, pyramids, and generic chunked child entries are outside
  the certified lane. A broker partial fill is treated as real exposure; it is never a
  reason to resubmit the unfilled remainder under a new identity.
- A certified close is `SELL + SELL_TO_CLOSE` against exact long-position provenance.
- The adapter, readiness checks, runner, reconciler, and operator paths quarantine a
  non-paper posture before creating an Alpaca client or making an Alpaca broker call.
- Alpaca crypto, `alpaca_short`, ambiguous side/intent pairs, missing account scope, and
  contradictory direction evidence are rejected before transport.

The current primary entry is a marketable limit derived from the final ask. It is not a
demonstrated passive-maker order, does not prove posting inside the spread, and is not
venue-selection DMA. Any future passive-posting experiment is a separate hypothesis that
must be replayed and soaked after recertification.

## Final execution-price truth

Discovery and setup scoring may consume Massive/Polygon, IQFeed, and other contextual
feeds. Provider availability does not make a provider causal or authoritative for the
final order.

Immediately before an Alpaca entry, the runner calls the strict execution-BBO boundary:

1. accept only a provenance-valid IQFeed `Q` row whose bridge receive time and conservative
   provider trade-reference time independently satisfy the two-second, future-safe bound;
2. otherwise request a direct Alpaca quote and require a valid provider timestamp;
3. require exact symbol identity, a named source, positive bid/mid, and an uncrossed ask;
4. recheck the same freshness object immediately before transport; and
5. refuse to raise the previously approved price ceiling merely because the market moved.

A recent bridge-receive timestamp is preserved as receive-time evidence; it is not
relabeled as provider-event time. A fresh BBO proves only the execution-price observation,
not the strategy's tape-confirmation or expected profitability.

## Ownership, idempotency, and shared-account posture

Every risk-increasing order in the governed Alpaca momentum path requires:

- a deterministic client order id;
- an exact immutable order request;
- a durable `alpaca:paper` account/symbol ownership claim committed before broker HTTP;
- a durable owner-transport outbox, committed before the first broker `POST`, containing
  the same immutable client id, exact normalized request, and frozen order-type verb that
  every retry must reuse;
- a committed planned-risk reservation; and
- a strict broker preflight showing no positions and no open orders anywhere in the shared
  paper account.

The recertification posture permits one shared-account exposure across all users and
symbols. Any persisted position, unresolved execution claim, broker position, or broker
open order blocks a new entry. Adds and pyramids therefore cannot reserve risk while a
position exists. Unknown or manual broker state may block admission, but it is never
cancelled, adopted, or liquidated merely because its symbol resembles a CHILI session.

A timeout, disconnect, duplicate client id, or malformed submit response is not proof that
the order was rejected. The same client id is reconciled through exact broker truth; an
ambiguous result never authorizes a replacement order.

## Order lifecycle and replacement containment

An Alpaca `replaced` or `pending_replace` lifecycle is not permission to infer that the
predecessor vanished or to create a locally invented child. The runner adopts only the
broker-designated successor after a fresh read proves exact, bidirectional predecessor and
successor ids plus the frozen symbol, side, intent, quantity, stop price, time in force, and
extended-hours shape. The repair path never cancels an unresolved predecessor first merely
to manufacture a clean replacement.

Status labels that do not prove both lifecycle and fill truth remain ambiguous. A missing
successor, mismatched identity, uncertain terminal state, late or cumulative partial fill,
or missing fill price is contained under the retained ownership claim and durable fill
high-watermark. It is quarantined for exact reconciliation; it cannot be treated as absent,
terminalized optimistically, or used to release account exposure.

## Risk and daily-loss boundary

- Planned risk for one paper position is at most the lower positive configured cap and
  `$50`.
- The Alpaca paper daily-loss admission limit is at most the lower broker/equity-derived
  cap, lower positive configured cap, and `$250`.
- The broker account day change (`equity - last_equity`) is the conservative paper
  daily-loss authority because it includes broker activity omitted or mislabeled locally.
- Alpaca paper P&L is excluded from real-capital aggregate halts.

These controls limit admission; they do not guarantee a `$50` realized maximum or a `$250`
daily maximum. Gaps, halts, slippage, liquidity loss, delayed exits, and broker behavior can
produce larger realized losses.

## Exit and recovery boundary

Certified long exits use signed broker quantity and exact `SELL_TO_CLOSE` identity.
Emergency close attempts retain deterministic order identity and apply only newly confirmed
filled quantity.

If the ordinary exit retry cap is reached while an exact paper long remains open, the
runner retains the entry ownership claim as the account/symbol exposure guard. It promotes
a durable close-only emergency authority only after all of the following are exact:

- explicit local long direction and matching symbol;
- equal nonzero local and broker quantity;
- zero competing broker open orders;
- terminal filled entry identity; and
- every earlier exit client id is absent or terminal with zero fill.

Uncertain proof leaves the session held, entry-quarantined, and runner-serviceable. It does
not terminalize exposure or let the generic orphan reconciler infer ownership. The retained
entry claim is released only after exact broker-flat proof.

Historical accounting repair is separate from execution authority. It may use narrowly
scoped, read-only broker evidence to correct a local outcome, but it cannot place/cancel an
order or create ownership of a legacy position.

## Current rollout gate

Before any new selection, entry, passive-posting, or exit experiment:

1. finish the focused and relevant regression tests and record exact counts;
2. verify the retry-cap close path and idempotent order lifecycle;
3. commit the reviewed code and tests before touching production accounting state;
4. while the runner remains off, perform the source-scoped ACTU accounting correction using
   an exact allowlisted, GET-only broker adapter and the already committed repair code;
5. require the exact one-row repair counts, then prove an independent second run is a no-op;
6. re-verify the bound paper account has zero positions and zero open orders;
7. record the resulting evidence in the audit and make a separate evidence commit; and
8. keep the runner off with no merge or deployment.

After that gate, Ross-aligned hypotheses may be tested through broker-fidelity replay and a
forward paper soak under the `$50`/`$250` ceilings and single-exposure posture. Profitability
and parity with another trader must be demonstrated; they are not promised by this lane.

## Configuration and secrets

Relevant posture controls include `CHILI_ALPACA_ENABLED`, `CHILI_ALPACA_PAPER`, the external
paper credential settings, the IQFeed quote-source setting, and the execution-BBO age
setting. Credentials must remain in external secret configuration; never paste them into
this document or commit them to the repository.

`alpaca-py>=0.30` is already an installed project dependency. No operator action is needed
to enable more trading during recertification.

See `2026-07-13_chili_broker_truth_recertification.md` and `MOMENTUM_LANE.md`.
