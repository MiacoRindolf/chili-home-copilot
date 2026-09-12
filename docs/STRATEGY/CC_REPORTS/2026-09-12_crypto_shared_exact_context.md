# Native crypto context uses the shared print reducer

The native decoder preserves Decimal prices and fractional volume. The former
structural reducer accepted only float/int inputs, and its classifier converted
prices back to float. That could collapse distinct crypto prices into a plateau.
`app.tick_math.structural_prefix` now hosts the shared pure implementation;
ordinary callers retain their stable import path and existing numeric behavior.
Exact rows use rational midpoint and tick comparisons, with no float conversion.
Mixed numeric domains require a new segment. Shared serialization retains exact
decimal values; recovery identity hashes the implementation as well as the shim.

`CryptoTickContext` consumes retained native trade/quote frames in member order.
Quotes must have been received before the print and have event time no later than
that print. An invalid as-of quote does not fall back to an older valid quote.
Provider trade IDs are not assumed monotonic. Identical duplicates do not add
volume; conflicting/late prints or broken frame membership quarantine the
segment. Every supplied asset remains visible, including cold symbols. This is
coverage of the supplied subscription, not proof of full broker-universe coverage.

Local and parent geometry use the same `WaveState` as ordinary equity. Immutable
views retain source identity/root/cursor, native asset ID, exact last print,
quote linkage and parent/local state. Provider-reported taker buy/sell/unknown
mass is measured separately over each turn's formation and follow-through.
Quote-inferred flow remains explicitly conditional; it does not overwrite
reported taker direction. Boundaries are structural print indices, not seconds
or a selected print-count window. Original capture/replay publication clocks
remain distinguishable.

Actual retained replay: 1,158 frames, 22 prints across seven traded symbols in a
15-symbol diagnostic subscription, location `us-1`. BTC had eight prints and a
confirmed local pullback; the parent remained unavailable. Since its valley,
reported net buying was +0.01936342 BTC, while peak formation contained net
selling of -0.00063658 BTC. The larger positive total did not erase the local
reversal evidence. This small sample does not validate a predictive policy.
The replay does not assume `us-1` quotes are the PAPER execution venue's quotes.

`credited_asset_roundtrip` evaluates supplied fee/price scenarios exactly. For
buy fee `f_b` in credited base and sell fee `f_s` in credited quote, net return is
`exit_bid / entry_ask * (1 - f_b) * (1 - f_s) - 1`. The strictly profitable exit
tick is the next broker price increment above break-even. No fee tier or zero-fee
fallback is embedded. Actual fees, fillability and base-quantity quantization
remain separate execution obligations.

The observed BTC print valley→peak excursion was about 0.01088%. Even hypothetical
ideal fills at those print prices yield about -0.48855% under the published tier1
taker/taker fee scenario. That is a cost scenario, not observed PAPER P&L or a
verified account tier. Alpaca documents fees on the credited asset and fee
activity reporting: https://docs.alpaca.markets/us/docs/crypto-fees.
Native taker-side schema: https://docs.alpaca.markets/us/v1.1/docs/real-time-crypto-pricing-data.

Validation includes exact midpoint precision, shared wave behavior, no future
quote joins, duplicate/conflicting trade identities, whole-frame rejection,
batching, cold-symbol coverage, native import without DB startup, ordinary
journal/recovery compatibility and fee-currency arithmetic. Tests use isolated
DB26 where a DB is needed. No orders were submitted by these changes or replay.

Still required before automatic trading: durable continuous source ownership,
recovery/late-symbol handling, subscription-capacity coverage, live shared
decision publication, entry/exit policy plus fee/quantity reconciliation, and
production owner/scheduler wiring. This module is not trading activation. The
current PAPER deployment remains PR1439; these changes are on the native owner
branch while that executable path is completed.
