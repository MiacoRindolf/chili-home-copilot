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
