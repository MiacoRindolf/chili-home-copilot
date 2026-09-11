# Project 9 [29]: retain raw quote-side evidence

The [21] effort/response audit found a TNON structural path with trade price up
2 cents, bid down 1 cent, and unchanged midpoint. Ordinary quote rows confirmed
the endpoint source-frame identities, but neither ordinary tape retained the
selected-field Bid/Ask Size or Bid/Ask Time. A trade-reference freshness fence
does not prove that both quote sides independently refreshed.

This change preserves those four provider text fields in `iqfeed_trade_ticks`
and `momentum_nbbo_spread_tape`. It does not introduce a momentum window,
backside veto, liquidity score, size conversion, or dated side-clock inference.

| Binding | Value | Derivation |
| --- | --- | --- |
| Raw field mapping | `provider_bid_size_raw` / `provider_ask_size_raw` / `provider_bid_time_raw` / `provider_ask_time_raw` | Confirmed selected-update field positions for Bid Size, Ask Size, Bid Time, Ask Time |
| Storage types | Nullable TEXT, no defaults | Preserve exact provider lexical values; NULL is unavailable evidence, while empty text is an observed blank |
| Migration | `379_iqfeed_raw_quote_evidence` | Next unclaimed ID on base `bfd58ad7fcd4d1d574bf8faaed67b272dacbbb85`; recheck before merging |
| VALUES budget | `floor((65535 - 1) / max(trade_width, quote_width))` | PostgreSQL parameter limit and actual INSERT column tuples; currently widths 21 and 22, budget 2978 events per statement |
| Capture schemas | IQFeed L1 and exact-print provenance V2 | Four required text-or-null fields; V1 remains readable under its original exact key set |
| Notification | Existing authority envelope | Existing frame sequence/hash, run and generation identify the stored context; raw strings are not added to an unversioned strict notification contract |

The selected Q parser captures raw side text before the trade/quote admission
branches. A quote-only update retains its own side values and frame identity
without creating a duplicate print. Existing admission, deduplication, timestamp
basis, source identity and publication ordering remain unchanged. The diagnostic
legacy parser has no confirmed selected-field layout and continues to write
unknown values. No historical evidence is backfilled.

All three production writers, the reference INSERTs, and both write benchmarks
carry the new fields. Startup checks require the columns before any provider
socket opens. The synthetic write probe exercises non-NULL text. If a larger
COPY batch falls back to VALUES, it splits statements inside the same
transaction; a failure in a later statement rolls the entire attempt back.

New capture envelopes use V2 and retain the raw fields inside content-addressed
provenance. Registration and the captured-paper trigger accept validated V1 or
V2 exact prints. Old captures are not rewritten. The existing quote proxy and
own-clock admission/capture restrictions are not broadened: retaining raw
side clocks is not certification of exact independently dated quote events.

## Validation

Focused tests cover raw lexical values, blanks versus NULL, malformed provider
text, quote-only updates, differing side clocks, unchanged notification identity,
V1/V2 validation and trigger compatibility, every writer and fallback, commit
release, a later-chunk rollback, migration idempotence and missing-column startup
refusal. Tests use unique temporary schemas in the reserved
`chili_rossbench26_test` database. No application bootstrap or shared table
truncation is needed. The exact final command/result is recorded in the PR and
project 9 task [29].

## Release and remaining research

Two independent adversarial reviews and the program verification stage remain
required. Do not update or restart the active paper app/bridge. During the
coordinated post-close release, apply migration 379 through the normal app
migration owner, deploy the compatible capture readers with the new producer,
then run the bridge schema/write preflight. Verify newly emitted rows using
source-frame identity, both raw sides, receive/publication clocks, and the
actual executable/configuration pins. A merged commit alone is not a rollout
or proof that new provider rows are present. Retain the additive columns if the
producer is rolled back; old producers write NULL.

Task [29] remains open: locate any original exact capture objects for historical
frames; derive side-clock dates/status without freshness laundering; retain raw
trade identity/conditions and L2 update/availability provenance. Ordinary-table
omissions do not prove historical optional captures are absent. Task [21] still
needs causal conditioning and held-inventory replay on nested structural paths,
plus cross-symbol/day evaluation. This retention slice does not resolve the
clock-derived 255-print window or prove a profitable backside rule.
