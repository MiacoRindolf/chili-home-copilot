# Native crypto trade history: complete page traversal

The native inventory request contained all 73 active, tradable PAPER crypto pairs,
including non-USD quote currencies. A first-page-only reader would have observed
only BTC/USD in the retained sample. Traversing the same fixed request through
its explicit null next-page token produced seven pages and 14 unique trades:
BTC/USD 5, PAXG/USD 2, UNI/USD 3, XRP/USD 4. The remaining 69 requested pairs had
zero returned trades in this source/interval; that is not an eligibility verdict.

The request was for Alpaca location `us`, from
2026-09-12T12:39:06.762775100Z through 2026-09-12T12:47:35.411311900Z inclusive.
The start is the deployed native inventory observation frontier; the end is the
earlier protocol sample's explicit capture boundary. Neither defines a strategy
window. The full traversal was received later; event timestamps must not be used
to pretend these records were already available to the live strategy.

`crypto_history_pages.py` checks whole-page membership, exact decimal prices and
sizes, integer provider IDs, nanosecond timestamps, pagination cycles, per-symbol
event order, duplicate identities and explicit exhaustion. A short or empty page
with a next token does not finish the request. Equal-timestamp records remain
unordered evidence; provider IDs are not interpreted as sequence numbers.

`capture_crypto_history_pages.py` pins the PAPER account before/after collection,
allows only the literal PAPER account GET and native data GET, and persists each
whole HTTP response before decoding it. Restart replays the durable chain and
revalidates its request/resources/decoder identities. A failed HTTP response
retains the same token; torn or malformed successful records remain unresolved
instead of being silently discarded. An OS file lock excludes concurrent writers.

The retained capture was restored offline with identical receipt and root hashes.
Request SHA256: `3a6286e75ca34596cdedb56de87cae8e946b55d07a92a613ff02e1c317bfbe2e`.
Record root: `25f404e601049e3edd865430abcd8565b61b318b3f77443206d2a8533f4474ce`.
Page root: `51d1efa42b93fa34ac9a6b8055760a1a18000ab2d5c732036eaa46b1cdd4415c`.
These are integrity/reproducibility evidence, not upstream authentication.

## Resource bindings

| Input | Bound | Basis |
| --- | --- | --- |
| Records/page | 2 | Earlier pagination protocol probe; deliberately forces page traversal |
| Pages | 100 | Explicit finite HTTP-call engineering allocation |
| Unique trades | 200 | Pages multiplied by requested records/page |
| Response bytes | 16 MiB | Existing native HTTP-reader engineering budget |
| Capture attempt | 45 seconds | Finite operational collection budget |
| HTTP timeout | 10 seconds | Existing native HTTP request budget |

Capacity exhaustion leaves incomplete evidence, never a truncated successful
universe. No resource limit is used for momentum detection or symbol ranking.

## Validation and remaining integration

65 targeted capture/page/decoder checks passed in 2.22 seconds on the isolated
DB26 test harness. An initial command used a nonexistent decoder test filename;
it collected no tests. The corrected run passed. A temporary DB26 occupancy
refusal was respected; the run started after a read-only check found no backends.

The actual PAPER-pinned capture submitted no orders. Page-chain exhaustion does
not establish event-time finality, completeness of live delivery, quote freshness
or profitable momentum. There is no rolling catch-up cursor or execution policy
activation in these scripts. Shared context ingestion, native quote access and
fractional funding/fees/protection/full-sale reconciliation remain required.

Primary API contract: [Alpaca historical crypto trades](https://docs.alpaca.markets/us/reference/cryptotrades-1).
The documented page limit is total records, with explicit next-page-token
traversal; start/end accept nanosecond RFC3339 bounds.
