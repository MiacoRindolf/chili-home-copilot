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

The selected ordinary PAPER equity path also no longer applies the later
top-gainer concentration gate, including its displacement caller. A setup cannot
be refused solely because another stock has a higher daily percentage gain or
the symbol lacks a board-rank/structural-percentile exception. The existing route
selects this behavior; no new switch or arbitrary replacement rank is introduced.
`candidate_intake.top_gainer_membership_required=false` reports the policy.

A full arm-pass regression holds both synthetic firing setups and financial
admission seams fixed, then changes the benchmark membership from AAA to ZZZ to
unavailable. On unchanged main, the first two cases each arm only one name while
the missing-benchmark case arms both. After the correction all three cases must
reach both PAPER arm calls. This verifies benchmark-independent admission at this
gate, not actual broker fills or profitable entry signals. Setup/quote/ownership
and aggregate risk/buying-power checks still control execution. Other legacy
Ross/viability dependencies remain; this does not complete shared tick selection.

Verification uses persisted PostgreSQL cases, including201 eligible variants of
one symbol plus a low-score eligible sibling with scan limit1; both symbols are
returned. Membership remains unchanged after Ross and viability ordering swaps.
Separate tests cover stale/future/ineligible rows, native crypto exclusion from
the equity-only mode, scoped requests, quote/market readiness, service fairness,
cross-pass in-flight capacity, provider failure cleanup, actual pass-level Ross
independence, and deferred-probe reporting. The historical
test_market_closed_equity_skipped failure is separately reproduced on unchanged
main; it is not represented as passing.

The scheduler now writes `auto_arm coverage=` JSON whenever intake or probe
coverage changes, even when no session arms. It compares actual symbol membership,
so equal counts with different unobserved names are visible. Reordering the same
membership does not generate another line. Missing intake/probe fields are logged
as null, including after an earlier observation; null does not mean an empty
universe or complete coverage. Unchanged diagnostics are suppressed within the
process and emitted again after restart. Existing lane heartbeat and skip-reason
logs remain separate. These log records are not durable shared tick revisions.

A malformed diagnostic emits an explicit warning without preventing the wake of
an already committed arm. Scheduler callback tests cover membership transitions,
unchanged/reordered suppression, unavailable observations, intake-source changes,
armed passes and failure isolation.
