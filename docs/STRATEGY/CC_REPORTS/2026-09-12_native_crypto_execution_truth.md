# Native fractional execution evidence

The ordinary Alpaca reader's `fill_truth_readable` requires a whole-share fill.
A valid 0.000040000000000001 BTC fill therefore cannot be certified through that
equity contract. The native crypto reader now retains exact cumulative Decimal
quantity and average price without changing the equity reader's authority.

`get_crypto_order_truth` and `get_crypto_position_truth` require an adapter bound
to the configured PAPER account before any lookup. They match provider asset UUID
and native pair; concatenated legacy symbols are accepted only with that exact
UUID. Order-ID lookup mismatches, fractional floats, inconsistent cumulative fills,
equity order shapes and malformed payloads remain unreadable. Only the actual
authenticated lookup's HTTP404 means not found. Timeout,429,500 and parser errors
never mean flat or zero filled.

Order cumulative fills, position quantity and `qty_available` remain separate.
A partially filled order can terminate with a positive fill. Replacement lineage
and unknown statuses do not release reservations. Missing available quantity is
unknown, not zero or the entire balance. A balance shortfall is not attributed to
a fee without further evidence. The records do not grant ownership, prove a
simultaneous account snapshot, or trigger an order.

The single-position mechanical PAPER full-close probe now uses the same native
position validator before its existing exact-asset full-close request. Its owner
lease, clean account/producer census and one-entry/no-ambiguous-retry rules remain.
The mock round trip verifies a gross fill of0.0001 and available quantity0.00009975
without requesting a sale of the unavailable remainder or declaring fees complete.

Validation:87 targeted native-reader, legacy crypto quarantine/order-leg, fill and
mechanical-probe checks passed5.94s.86 source/inventory/capture/side/history checks
passed6.85s in a separate targeted batch; no whole-suite claim. Runtime GET-only
verification at2026-09-12T13:13:19Z checked the PAPER account, native BTC asset and
position-by-UUID endpoint. The latter reported404, matching no held BTC. No actual
fractional fill or crypto order was observed. The first verification attempt used
an order-audit accessor before its generation was initialized; the corrected
verification uses the installed GET-only transport guard and its actual call log.

This is native evidence support, not crypto strategy activation. Source context
ingestion, native funding/fee policy, supported protection and actual entry/exit
consumer wiring remain in project9. PAPER release of this completed code does
not imply that crypto positions will begin opening.

Primary contracts: [Alpaca crypto orders, fees and funding](https://docs.alpaca.markets/us/docs/crypto-trading)
and [native position lookup](https://docs.alpaca.markets/us/reference/getopenposition-1).
