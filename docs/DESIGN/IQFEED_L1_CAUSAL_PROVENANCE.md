# IQFeed L1 causal-provenance containment

## Status

This contract is a fail-closed containment boundary for IQFeed Level 1 quotes. It
does not claim that Most-Recent-Trade-Time is a quote-event timestamp. No bridge
row or notification is authoritative unless every v2 field below is present and
valid. Legacy and ambiguous rows remain readable for research but cannot price an
Alpaca order or trigger event admission.

## Authoritative v2 tuple

The host bridge may write an authoritative NBBO row and send its matching
PostgreSQL notification only for a `Q` update received after the current socket
connection began. A `P` summary never writes the authoritative NBBO tape and never
notifies admission.

The complete tuple is:

- `source = iqfeed_l1`
- an uppercase equity symbol (no crypto-pair or raw-symbol fallback)
- `message_type = Q`
- `timestamp_basis = iqfeed_q_receive_trade_reference_fenced`
- the exact content-addressed v2 `bridge_version`
- a process-unique UUID `bridge_run_id`
- a positive, monotonically non-decreasing `connection_generation` within that run
- aware UTC `received_at`
- aware UTC `provider_trade_reference_at`, parsed from Most-Recent-Trade-Time
- `provider_event_at = NULL`
- `observed_at` equal to the trade reference (stored naive UTC only because the
  historical tape schema uses `TIMESTAMP` as its indexed sort key)
- finite positive bid/ask with `ask >= bid`

The receive-minus-reference delta must be from -1 through +2 seconds. At use time,
both clocks must independently be no more than two seconds old and no more than one
second in the future. This prevents a reconnect replay from becoming fresh merely
because the bridge received it now.

P summaries and stale/unparseable Q frames are dropped from both the NBBO and trade
queues. The trade table has generic live recent-window consumers, so those frames
cannot safely remain there under a “research-only” label.

## Exact-build rollout pin

`CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD` defaults to empty. Empty, malformed,
or mismatched values disable IQFeed notification admission and make the Alpaca
adapter request a direct Alpaca BBO. The operator must pin the exact build printed
by the reviewed bridge process, for example:

`iqfeed-l1-quote-provenance-v2+sha256:<16 lowercase hex characters>`

The bridge build reviewed for this recertification is exactly
`iqfeed-l1-quote-provenance-v2+sha256:dc0185e65439364c`. That full content address,
not merely the `v2` family, must match the configured pin and every accepted row.

Changing bridge source changes this identity. Deploying a new bridge therefore
requires reviewing and deliberately updating the application pin; version-family
or prefix matching is not permitted.

## Consumer behavior

The live loop rejects invalid JSON rather than interpreting it as a symbol. It
validates the full tuple, fences connection-generation rollback, and deduplicates
the complete certified tuple. A tuple is not placed in the event watermark until
admission succeeds or a runner dispatch is actually scheduled.

When a certified notification has no existing session, IQFeed admission may create
or deduplicate the Ross-style candidate and arm. That admission always defers any
synchronous Ross tick. The active loop generation must win the lifecycle fence and
commit the admission transaction first; only then may the existing generation-owned
dispatch path schedule the runner tick. A rollback, failed commit, or stop-winning
generation check therefore has no pre-commit runner or broker effect.

The Alpaca adapter selects the newest IQFeed row using the existing
`(source, symbol, observed_at)` path, validates the full tuple and both clocks, and
otherwise falls back to a direct Alpaca quote. Migration 317 adds only nullable
metadata columns. It performs no backfill and creates no index over the historical
NBBO tape.

## Connection and loop-generation ownership

Each host connection owns a concrete socket, stop token, reader thread, and
monotonically increasing connection generation. A reader never dereferences a
mutable process-global socket. Reconnect closes that concrete socket and joins its
reader before any new socket may be bound; failure to quiesce is terminal rather
than an invitation to run overlapping generations. An old reader cannot stop or
publish for the current writer generation.

In the app loop, event admission is likewise owned by the active loop generation.
Stop invalidates that generation. An admission rechecks ownership under the
lifecycle lock immediately before commit and rolls back if stop won; stale work
does not refresh the tracker. Restart refuses while an old admission remains in
flight. Tracker refreshes carry an expected generation and publish atomically only
when it still matches the tracker's owner, so a delayed old query cannot overwrite
the new generation's session snapshot.

## Known limitation and follow-up

Most-Recent-Trade-Time is only a conservative causal proxy. A valid quote change
can occur without a trade inside the two-second fence, so this containment can
reject usable IQFeed quotes and fall back to Alpaca. The follow-up research task is
to select and verify IQFeed's actual `Bid Time` and `Ask Time` update fields (and
their date/session semantics), then replace this proxy with side-specific quote
event provenance under a new reviewed protocol version.
