# Ordinary shared structural observation owner — project9 [13]/[29]

The ordinary print publication journal is now connected to the incremental
structural reducer through `OrdinaryStructuralContextOwner`. One actual
repeatable-read transaction selects the enrolled symbols at the same committed
release revision. Selection, entry and exit read an immutable `ContextSnapshot`
from that owner; the observation ports do not query or independently reduce tape.

This is an isolated integration build, not the active lane's entry/exit policy.
The existing running lane/bridges and broker configuration have not been changed.
The owner is process-local. Service activation, durable cross-process publication,
checkpoint/replay recovery and binding the real trading consumers are unfinished.
The input-demand registry does not itself issue provider subscription commands;
its union still needs the actual one-owner provider dispatch/lifecycle connection.

## Source and transaction meaning

`read_publications` reads the whole selected-symbol membership of each release
from the actual journal, retains the full release ID/symbol manifest, and advances
through other-symbol publications without inventing prints. One release cannot
be split across symbols to fit a caller's trade-row resource capacity. The legacy
single-symbol reader retains its interface through this shared implementation.

`PublicationFrontierReceipt` deliberately differs from captured-sequence receipts:
its source sequence is the committed release revision, while the print ID remains
the database trade ID. Those are independent orders. Source frame sequence orders
the supplied rows; non-monotone provider/event history, repeated frames, missing
hashes, changed epochs, bad clocks, unknown/nonzero reported delay and quote-proxy
rows produce an explicit unresolved observation. There is no silent reorder into
a different historical event path, timestamp watermark, or rows-by-ID freshness
assumption. Recorded trade BBO remains quote/tick inference; it does not certify
quote freshness or actual aggressor side.

The reducer stages all changes before mutation. The owner prepares every symbol
in a release before committing any of them. A domain/resource failure leaves all
prefixes and the source cursor unchanged. If a catastrophic failure occurs during
commit, the last published immutable view is marked unresolved and further owner
writes are refused until reconstruction. This does not claim crash-safe state
without a durable checkpoint and replay. Checksums bind supplied retained evidence,
not upstream provider completeness or an independently authenticated capture.

Page size1 is one release transaction, not a one-print strategy window. An atomic
release can contain multiple symbols and an entire spike/reversal. All its ordered
birth/breach events are retained in the snapshot. They are learned together at
publication; no intermediate execution opportunity is invented from them.

## Demand and consumer behavior

Successful versioned inventory/ranking/held/pending/watch demands update their own
membership. A failed read (`symbols=None`) preserves that source's prior set and
records it as stale. An authoritative empty result clears only its own reason.
Previously enrolled contexts remain observed after ranking removal and after the
last reason clears; there is no implicit retirement/TTL or score-based reset.
Resource overflow refuses enrollment explicitly instead of evicting a held name.
Retirement/storage compaction and full eligible-universe coverage remain lifecycle
work; this behavior is not a permanent memory-cap definition of the opportunity
universe. Missing provider subscription/fresh prints is not hidden by enrollment.

Source revisions are shared; consumers should supply their last consumed cursor.
If more than one publication was missed, the current in-memory owner reports a
gap requiring journal replay instead of presenting the latest events as a complete
history. Automatic ordered replay/durable consumer offsets are still required
before trading integration. Unchanged-symbol scopes are reused immutably; neither
a demand update nor another symbol's print recomputes that symbol's geometry.

Views expose all currently active source-defined structural references, exact
rational phase mass and ordered reference events. They explicitly report unknown
pre-anchor history, uncertified quote freshness and `selected_parent_local` as
`not_yet_derived`. Choosing a local/parent scope, calibrating opportunity net P&L,
and implementing the validated backside entry policy are not smuggled into this
source integration. `order_authority` and `provider_completeness_certified` are
false facts of this observation contract, not off-by-default strategy switches.

No Ross field enters this owner. The operator's latest requirement remains:
Ross is a benchmark only. The future connected selection/admission/size/exit must
be invariant to changing/missing Ross scores when tick/account/execution evidence
is unchanged. Quotes, fees, asset rules and account observations remain execution
inputs; strategy populations and structural boundaries come from prints.

## Verification evidence

- First source/reducer/owner group:96 passed in17.15s.
- Captured-adapter/sequence/replay neighbors plus the expanded owner:179 passed,
  one new fixture failed in143.43s. The fixture tried to insert a missing source
  hash, which the existing bridge correctly rejects before persistence. It now
  models retained-row corruption after release inside the isolated test schema.
- Corrected owner/source/staging/boundary group:103 passed in18.20s, including
  atomic siblings, rank removal, authoritative demand failures, late/epoch/clock
  and hash gaps, rollback, concurrent wakes and catastrophic commit fencing.
- Offline reducer replay:125,454 supplied historical prints,16,452 frontiers,
  all61 saved checkpoints and1,159,630 comparisons passed in48.781s. This proves
  preserved observed-prefix calculations on those packets, not executable P&L,
  provider-complete history, ordinary-owner throughput or deployment.
- After immutable scope reuse was added, all15 owner tests passed in9.53s,
  including a test that refuses any geometry recomputation for an unchanged
  sibling symbol while its source frontier still advances.

Tests use the isolated `chili_rossbench26_test` database and unique throwaway
schemas. No running bridge was restarted and no broker orders were submitted.
Independent adversarial review, integration with
the pending source migrations/producer rollout and live consumer wiring remain
separate verification/release steps.
