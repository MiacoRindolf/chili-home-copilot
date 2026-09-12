# Ordinary shared structural observation owner — project9 [13]/[29]

The ordinary print publication journal is now connected to the incremental
structural reducer through `OrdinaryStructuralContextOwner`. One actual
repeatable-read transaction selects the enrolled symbols at the same committed
release revision. Selection, entry and exit read an immutable `ContextSnapshot`
from that owner; the observation ports do not query or independently reduce tape.

This is an isolated integration build, not the active lane's entry/exit policy.
The existing running lane/bridges and broker configuration have not been changed.
The reducer is process-local. Its durable publication sink and independent
consumer offsets are described in `durable_structural_context.md`. Service
activation and binding the real trading consumers remain unfinished. Warm owner
reconstruction is implemented in `structural_context_recovery.md`.
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
`update_demands` now stages related inventory/held/pending changes as one batch.
Every member specifies its own monotonically increasing source revision, observed
symbols, and completeness. Incomplete reads may add new observed symbols while
preserving previous membership and marking that source stale. Only an explicit
complete observation can remove a source's prior members. The caller must use
the broker book's paired-read completeness when a pending-to-held transition is
not fully observed; an independently successful section does not prove that the
other section can release coverage.

All batch members and the union resource capacity are validated before mutation.
Invalid/stale siblings leave every source revision and public snapshot unchanged.
The batch publishes one immutable output, so consumers do not see an intermediate
pending removal without the associated held addition. A failure after private
mutation fences the owner for reconstruction and exposes only an unresolved prior
view. The durable recovery journal retains the original observed symbols and
completeness in one canonical `demand_batch` capsule, not just the resulting union.
Tests verify one durable output, exact restoration, and transaction rollback that
recovers neither half of a pending-to-held transfer. This closes a demand API gap;
native broker identity/provider mapping and the actual service callers are still
required before enabling this owner in the trading lane.

`BrokerNativeDemandService` now connects the real account-pinned asset and
coverage adapter reads into a single immutable native-asset demand revision.
It joins by broker asset UUID/class, preserves the observed symbol aliases and
all pending order IDs, and retains delisted/nontradable held exposure independently
of catalog membership. No symbol ranking, quote currency, punctuation, market
price, quantity or top-N filter is applied. Nontradable catalog rows remain visible
without an inventory-tradable demand reason. Broker symbol equality alone cannot
combine distinct UUIDs, and conflicting asset classes do not publish.

Both underlying books are staged before publication, so invalid coverage cannot
consume a successful inventory revision. Transport failures publish stale retained
membership; an incomplete pending/held pair marks both demand reasons stale.
Symbol spelling changes for the same UUID/class retain coverage even during a
partial read. A changed orders bracket remains incomplete despite that identity
match. Original bracket evidence retains both spellings. The service supports
seeding from a retained immutable snapshot but does not yet supply its own durable
storage or connect this snapshot to the print owner. Provider binding is explicitly
`not_bound` for every member, so broker inventory is never presented as tick coverage.

Actual PAPER GET-only refresh at2026-09-12T09:22:49Z observed14,381 native assets
(14,308 equities and73 crypto),13,508 tradable inventory demands, no positions/open
orders and no stale read sections.60 targeted inventory/coverage/native-service/
demand-batch checks passed5.50s. This is source-integration evidence, not deployed
selection or actual simultaneous executions. Recorded evidence is in the operator
handoff's ASTRA_BROKER_NATIVE_DEMAND_VERIFY.json.gz and corresponding summary.

Retirement/storage compaction and full eligible-universe coverage remain lifecycle
work; this behavior is not a permanent memory-cap definition of the opportunity
universe. Missing provider subscription/fresh prints is not hidden by enrollment.

Source revisions are shared; consumers should supply their last consumed cursor.
If more than one publication was missed, the current in-memory owner reports a
gap requiring journal replay instead of presenting the latest events as a complete
history. The durable context journal now supports ordered consumer catch-up and
persistent offsets without a second reducer. Warm owner reconstruction now
replays original inputs and compares every durable output before resuming. Unchanged-symbol scopes are reused immutably; neither
a demand update nor another symbol's print recomputes that symbol's geometry.

Views expose all currently active source-defined structural references, exact
rational phase mass and ordered reference events. They explicitly report unknown
pre-anchor history and uncertified quote freshness. A populated prefix now exposes
`selected_parent_local=candidate_geometry` with the candidate wave context below;
empty prefixes remain `not_yet_derived`. Calibrating opportunity net P&L and
implementing a validated backside entry policy remain separate work.
`order_authority` and `provider_completeness_certified` are
false facts of this observation contract, not off-by-default strategy switches.

No Ross field enters this owner. The operator's latest requirement remains:
Ross is a benchmark only. The future connected selection/admission/size/exit must
be invariant to changing/missing Ross scores when tick/account/execution evidence
is unchanged. Quotes, fees, asset rules and account observations remain execution
inputs; strategy populations and structural boundaries come from prints.

## Candidate local and parent context

The staged reducer now computes quote-resolved local turns incrementally. A price
rise confirms a valley only when the current recorded bid exceeds the origin's
recorded ask; a fall confirms a peak only when the current ask is below the
origin's bid. Invalid quotes cannot confirm a turn. Delayed confirmation preserves
intervening extremes, rather than resetting the counter-extreme to the confirming
print. These recorded BBOs still do not certify independently fresh quotes.

Raw turn extrema are recursively promoted using adjacent like-kind extrema and a
confirmed right witness. Each level stores the two latest distinct-price groups;
the next group establishes whether the middle one is an extremum. Equal-price
groups use their last representative. This structural adjacency does not select
a three-print strategy horizon, a hierarchy order, or a minimum price amplitude.
The implementation reuses Prefix's range tree and stages all state before commit.
It does not rescan retained tick history on each new print.

The local envelope includes the entire price path from the earlier local
peak/valley origin through the current print, including unconfirmed excursions.
Candidate parents must start earlier and strictly enclose that whole envelope.
Minimal elements under range/origin containment are retained; incomparable
parents remain ambiguous. Repeated promotions of identical source endpoints are
aliases, while distinct source endpoint identities remain distinct candidates.
The final view exposes local/parent references, phase reasons, and all turn
confirmation events learned in that release. A below-parent-high recovery may
still be local front. Phase describes the candidate geometry, not expected profit.

The immutable journal uses publication contractv3 for typed wave geometry and
formation/follow-through evidence.
Readers expose `new_wave_events` only when the source advances, so demand refreshes
do not repeat a trade-trigger event. Recovery's code identity includes the pure
wave module and replays/verifies the complete new output. Old v1 output cannot
silently decode into this shape; a code-mismatched durable stream requires an
explicit reconstruction/upgrade before resuming. No production stream or schema
was upgraded in this change.

Frozen TNON comparison:76,000 prints,148 quote-resolved turns,32,208 recursive
turns,42 saved-prefix/end comparisons all matched the earlier batch research
definitions, including selected parent and local/parent phases. The diagnostic
used synthetic transport boundaries to isolate geometry; it does not authenticate
real publication timing or establish net profitability. Full artifact is
ASTRA_INCREMENTAL_WAVE_CONTEXT_VERIFY.json in the operator handoff. Selection/
entry/exit service callers, source rollout and a measured execution policy remain
required; this branch does not enable a new backside veto.

## First ordinary selection consumer

The selected ordinary PAPER equity auto-arm path now reads the current shared
observation once for its candidate set and includes a `shared_tick_context`
receipt in the pass output/coverage log. The producer must use
`ordinary_paper_context_stream(expected_account_id)` so the account-scoped stream
name is deterministic and does not expose the bare account UUID. The read uses
a separate read-only repeatable-read connection and compares the published source
cursor with the current print-journal cursor in that transaction. A lagging or
changed source, absent writer, cold symbol, unenrolled symbol, missing schema and
unavailable stream remain distinct evidence states.

`read_current_context` validates the complete latest payload/chain against its
immediate predecessor. Its16MiB allocation cap is an I/O resource bound; an
oversized/corrupt/missing head fails explicitly rather than selecting an older
or truncated snapshot. This API supplies current state only: it neither replays
the event history nor advances any selection/entry/exit consumer offset. Event
trigger consumers must continue to use sequential `read_context` and transactional
`acknowledge`. All pass candidates share the same context cursor. The receipt
includes per-symbol phase/flow references and explicitly states that admission
still uses legacy rules. New wave evidence does not silently enable a veto.
An explicit caller-supplied decision instant refuses this current-state read;
that caller must supply a bound historical source cursor. This prevents attaching
later publications to a frozen earlier decision instant.

Actual read-only readiness check09:58:58Z returned
`shared_context_schema_not_installed` in the running database. This integration
is not deployed; migrations381–383, producer/service setup, native/provider
mapping and entry/exit consumption remain release work.107 targeted current-read,
journal, scheduler and auto-arm checks passed35.45s; the known unchanged-main
`test_market_closed_equity_skipped` synthetic crypto fixture still fails(0 vs1
armed), as recorded before this change. No whole-green assertion. The new reader
cases verify source lag, writer disappearance, corrupt/oversized head refusal,
immutable observations and unmodified event offsets.
After adding the explicit-decision-frontier refusal,17 focused reader/scheduler/
actual-selection-caller checks passed11.07s. The preceding107-pass/1-known-failure
batch remains the broader validation result; counts overlap.

Each local reference and every minimal-parent alias now carries formation
`(origin,confirmation]`, follow-through `(confirmation,current]` and whole-path
evidence. The confirming print belongs only to formation. Exact mass, print
counts and price changes partition additively; endpoint bid/ask changes stay
unavailable where a valid quote is missing. A positive whole-path change can
coexist with negative follow-through, rather than masking it.

A separate cumulative quote classifier uses exact doubled-price comparison with
bid+ask, only for trades inside a valid recorded BBO. Midpoint ties, out-of-book
prints and unavailable/crossed/locked quotes remain unresolved. Legacy quote/tick
fallback mass remains available separately; it cannot narrow these new bounds.
With classified buy B, sell S and unresolved U, conditional signed-volume bounds
are `[B-S-U, B-S+U]`. They assume the classified signs are correct and are not
confidence intervals, authenticated aggressor flow or certified quote freshness.
No fitted threshold, seconds population, hierarchy order or binary trade rule is
introduced. The new cumulative sums stage/commit with the source release and
support constant-time interval reads; no per-reference history rescan.

Formation, follow-through and whole partitions are immutable shared values,
reused when a symbol receives no new prints. Contractv3 validates source
endpoints, mass conservation, exact partitions, bounds and non-authority claims;
recovery's implementation hash includes the evidence module.144 targeted checks
passed57.85s. The76,000-print frozen TNON comparison additionally matched432
intervals against the earlier exact inside-quote batch study. At the−$109.08 leg's
entry proxy, valley bid formation is+$0.04 and follow-through−$0.03 despite
positive conditional quote bounds; at first-held, the valley is broken and the
bounds are negative. These archived checkpoints do not establish a deployable
veto, actual setup ancestry, authenticated release timing or profitable execution.

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
