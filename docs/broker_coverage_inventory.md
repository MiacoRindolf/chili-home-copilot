# Broker-held and pending coverage

Full opportunity coverage needs three independent inputs: the complete broker
asset catalog, actual broker-held assets, and actual pending orders. A strategy
session can be absent, recycled or stale while the broker still owns exposure.
Catalog removal or a change in Ross rank cannot release that coverage demand.

`AlpacaSpotAdapter.get_coverage_inventory_probe()` reads the pinned PAPER
account, open orders, all positions, open orders again, and the account again.
Each collection retains its observation interval and canonical payload hashes.
Both orders reads use `status=open`, `limit=500`, `direction=asc`, `nested=false`;
there is no symbol, side, class, strategy or submission-time filter. The position
collection has no filters. No broker mutation is performed.

The API maximum of 500 comes from the documented
[GetOrdersRequest](https://alpaca.markets/sdks/python/api_reference/trading/requests.html#getordersrequest).
It is a transport completeness boundary, not a tick window or trading limit.
The request has time filters but no tie-safe order-ID page cursor. A full page
is therefore explicitly incomplete. Its observed members remain available to
add coverage; they cannot replace a previous complete inventory. See also the
[orders](https://alpaca.markets/sdks/python/api_reference/trading/orders.html)
and [positions](https://alpaca.markets/sdks/python/api_reference/trading/positions.html)
SDK contracts.

## Preserve the order-to-position transition

An order can fill between separate REST reads. If positions were read before
the fill and orders afterwards, both responses could be empty even though a
position now exists. The reader brackets positions with two orders responses.
A changed or incomplete bracket retains the union of the observed order IDs,
marks pending coverage unavailable and prevents releasing previous demands.
This detects observable changes; it does not establish broker linearizability,
eliminate eventual-consistency risk, or certify every transition between calls.

`CoverageBook` independently accepts new members from either usable collection.
It replaces memberships only when the held collection and the orders bracket
are both complete. In particular, stable empty orders cannot erase a previously
pending symbol when the positions read failed: that order may have filled.
An incomplete observation retains both prior held and pending membership.
A complete paired empty observation can clear the book's own demands.

The book is account-pinned, immutable to readers and serialized during updates.
Regressing observation intervals and conflicting retained identities refuse
the update. Changed outer account identity invalidates both collections;
foreign account members discard the affected read. Malformed/duplicate records
make a collection incomplete rather than silently producing empty success.
No provider exception message or credential is included in error categories.

## Scope and integration

Native asset UUIDs, asset classes and broker symbols are preserved, including
crypto, punctuation, unknown classes/statuses, fractional/notional quantities,
pending cancellations and sells. Financial fields are retained as delivered,
without float conversion or whole-share defaults. This is a conservative
observation demand, not financial reconciliation, ownership certification,
available buying power, an executable position ledger or order authority.
An unsupported asset class must receive an explicit downstream coverage gap;
its existence must not be lost just because a reducer cannot process it.

Broker symbols are not guessed into provider symbols. Joining catalog and
position records should use broker asset UUIDs and explicit provider identity
evidence; for example, concatenated and slash-separated crypto names must not
be treated as interchangeable through an unverified string rewrite.

The ordinary bridge still derives ACTIVE from strategy sessions, and ELIGIBLE
from viability scores/age/limits. This reader and book are implemented inputs
for their replacement; they are not wired into the running bridges. Durable
catalog/exposure publication, provider mappings, event-driven updates between
broker snapshots, crash recovery of retained demands, subscription coverage
receipts and shared selection/entry/exit consumer integration remain required.
No timer is introduced as a strategy measurement. The book currently lives in
one process, so it must not be restarted empty and presented as recovered truth.

## Verification

Final inventory/adapter/identity targeted batch: 102 passed in 21.55 seconds.
Cases include mixed asset classes, exact metadata, response bounds, malformed
members, account changes, independent failures, order-to-position races,
partial-fill status changes, conservative retention and eventual complete
empty replacement. One attempted run refused because the isolated test DB had
other backends; no guard was bypassed. The later run passed when it was free.

Actual adapter verification at 2026-09-12T06:34:32.308437Z allowed only PAPER
account/positions/orders GETs. It made three account GETs (including client
initialization), two orders GETs and one positions GET. Both collections were
empty and complete at that observation. This verifies the real transport path
and empty-response handling; the nonempty/fill-race evidence comes from tests.
No order, lane restart, deployment or live database mutation was performed.
