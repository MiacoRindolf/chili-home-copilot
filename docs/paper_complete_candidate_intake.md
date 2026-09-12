# Complete eligible-symbol intake for the ordinary PAPER lane

The previous intake applied Ross-universe membership and a score-sorted SQL row
limit before distinct-symbol selection, then a second symbol scan limit. A
symbol with many variants could consume the row prefix and hide another already
eligible symbol before its setup was ever evaluated.

The selected ordinary Alpaca PAPER equity route now reads one representative
for every currently live-eligible symbol, independently of Ross membership and
the configured scan count. Distinct selection occurs in PostgreSQL before rows
are materialized. The legacy highest-viability variant remains the per-symbol
representative; the score no longer removes a symbol from this intake. Market
and quote readiness still apply. A row later than the supplied decision clock
cannot enter its past candidate set. Scoped ignition requests retain their
explicit symbol restriction.

Full enumeration exposed another problem: each timed-out probe wave cancelled
its queued tail, but the next wave could repeatedly start the same ranking
prefix. PAPER probes now give least-recently-started symbols service first.
Only actual starts advance the service ordinal. Cancelled/unstarted names keep
their place. An in-flight request keeps its capacity across pass deadlines;
later passes cannot duplicate it or accumulate more active probes beyond their
configured worker capacity. Late results stay with the original pass; they are
not cached as fresh signals for later trading. This is process-lifetime
operational fairness, not a durable source/tick publication contract.

Pass receipts explicitly identify the legacy source, representative policy,
and unobserved symbols. A capacity deferral is not a negative momentum signal.
Service order is not a claim about relative profit. Financial admission and
held/pending/candidate reservations remain at the existing entry boundary.

This is a completed intake correction, not completion of the selection program.
The upstream live-eligible board still has legacy scoring, time-based freshness
and coverage limitations. Later admission still contains legacy conditions.
This patch does not supply full broker-universe observations, replace every Ross
dependency, derive selected parent/local states, enable crypto, or certify a
new backside veto. Shared tick owner/consumer integration and causal strategy
work remain required.

Verification uses persisted PostgreSQL cases, including201 eligible variants of
one symbol plus a low-score eligible sibling with scan limit1; both symbols are
returned. Membership remains unchanged after Ross and viability ordering swaps.
Separate tests cover stale/future/ineligible rows, native crypto exclusion from
the equity-only mode, scoped requests, quote/market readiness, service fairness,
cross-pass in-flight capacity, provider failure cleanup, actual pass-level Ross
independence, and deferred-probe reporting. The historical
test_market_closed_equity_skipped failure is separately reproduced on unchanged
main; it is not represented as passing.
