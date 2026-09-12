# Alpaca crypto PAPER integration — work in progress

The operator requests tradable momentum crypto in the shared tick-based
selection/entry/exit system, including weekend PAPER execution. Listing is data
preparation; this branch has not enabled crypto submission yet.

The initial correction reads crypto fractional min_order_size,
min_trade_increment and price_increment from the broker asset record. Previously
get_product_probe returned one whole unit for crypto as well as equities.
Exact decimal constraints are retained for later sizing and transport; the
existing normalized floats are compatibility/display fields. Missing or invalid
crypto constraints produce an explicit probe error instead of fabricated defaults.

get_crypto_products_probe retrieves actual active crypto inventory and retains
the base and quote currencies, tradability and exact constraints. It has no majors
list or asset-name veto; duplicate/malformed/unavailable catalogs are errors,
not successful empty universes. All quote currencies are inventoried. Trading
non-USD pairs additionally requires denomination-aware funding/fee accounting.
The ordinary equity get_products path and order/protection guards are unchanged
until the crypto lifecycle is implemented and verified.

Verification: 50 targeted inventory, existing crypto routing/quarantine and
borrow-probe checks passed. A separate test against all 73 actual asset records
captured on 2026-09-12 also passed, checking exact fractional constraints and
currencies. This is an asset-catalog fixture, not a tick replay or profitability
test. The same read-only preflight found an ACTIVE PAPER crypto account and
36 tradable USD pairs; no credentials or account details are in the fixture.

Remaining: connect crypto universe selection and actual venue-labelled trade/
quote stream; preserve reported taker side separately from inferred signs; bind
shared context revisions; implement fractional full-instruction reservations and
held accounting with non_marginable_buying_power and actual fees; certify crypto
GTC/IOC order/protection/full-sale/re-entry lifecycle; prevent LIVE fallback;
verify/review and coordinate deployment. Do not reuse IQFeed rows, quote-midpoint
bars, stock leverage or whole-share protection rules as crypto authority.

## Stream capture and side-convention correction

The bounded capture CLI now subscribes to actual Alpaca trades/quotes, retains
raw received frames in a checksummed journal before observing them, and records
source location, connection, membership, and capture limits. The decoder retains
integer trade IDs, exact decimal prices/sizes and nanosecond event timestamps.
Native Alpaca taker side is retained; missing side remains unknown. Invalid
members reject the whole frame. A replay verifier checks the retained chain,
subscription membership, terminal counts and source facts. These checks do not
authenticate upstream completeness or invent pre-capture history.

The legacy Coinbase WS path had a verified side-convention inversion: Coinbase
reports maker side, but TapeTrade/compute_features expected taker side. It now
inverts only documented BUY/SELL values and retains the reported side, convention
and provider trade ID. A regression demonstrates that three maker-buy units and
one maker-sell unit produce a known-side taker buy share of25%, not75%; unknown
units remain unresolved. No Coinbase order route or running process was changed.

34 capture/parser/side and Coinbase WS-neighbor checks passed; a subsequent
end-to-end known-side arithmetic check passed. The broader microstructure group
had31 passed/2 failed; the two historical log-only assertions fail identically on
unchanged main because live_runner already imports a hidden-seller calculation.
Those are recorded baseline failures, not a green whole-neighbor result.

## Measured data constraints, 2026-09-12

- The credentials accepted15 symbols with both trades+quotes and rejected16.
  Trades-only accepted30 and rejected31. This measures channel capacity, not a
  strategy top-N. The full36-USD-pair universe cannot be declared continuously
  observed by that one subscription. Diagnostic subsets are explicitly labelled.
- A second concurrent crypto location connection returned406, connection limit
  exceeded. It was closed; no attempt was made to bypass the account limit.
- An Alpaca/us sample had137 quotes and zero trades. REST history for the same
  capture interval also returned zero trades. A later us sample had1 trade and87
  quotes. Quotes/bars must not become fictional traded-volume evidence.
- A separate crypto/us-1 (Kraken) sample had22 trades across7 symbols and1213
  quotes. Its reported sides were16 buyer/6 seller initiated. Its retained chain
  of1158 frames verified with no observed duplicate/conflicting IDs or late trade
  events. The samples are small and not simultaneous; they prove neither an edge
  nor price equivalence between the reference and execution venues.

Next source integration must provide declared coverage across the broker-listed
universe under the measured channel budget, preserve HELD/pending subscriptions,
and evaluate REST trade catch-up/quote acquisition without silent gaps or a
seconds-based strategy window. If another venue supplies reference prints,
carry that venue explicitly and check executable Alpaca prices/costs separately.
The actual shared context owner and crypto order/protection lifecycle remain
unfinished. No crypto activation or profitability is claimed by this build.

Sources: https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data and
https://docs.cdp.coinbase.com/coinbase-business/advanced-trade-apis/websocket/websocket-channels
