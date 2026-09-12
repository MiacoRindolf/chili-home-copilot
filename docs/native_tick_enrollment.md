# Native identity in the shared ordinary tick context

`NativeTickEnrollment` projects a complete native/provider mapping into compact,
replayable observation inputs. Every matched equity retains its broker asset UUID,
broker aliases, provider symbol, listing market and provider-row hash. The input
also binds the native checkpoint revision/digest, provider catalog observation and
mapping digest. Matching is catalog evidence; it does not certify subscription,
entitlement, fresh quotes, complete prints or profitability.

The owner applies inventory, held and pending memberships atomically. Unresolved
equity mappings make the affected reasons incomplete: additions remain possible,
but a failed mapping cannot remove prior exposure demand. Crypto gaps stay visible
as `native_crypto_source_required`; they do not become IQFeed equity symbols.
Complete empty native observations clear their own demand reasons. Ranking/watch
remain independent and cannot erase held/pending membership.

Hyphenated provider equities are accepted only through typed native enrollment.
Legacy raw `BTC-USD` and slash pairs remain rejected by the ordinary equity owner.
A provider symbol already attached to another native UUID requires explicit
reconstruction; its old wave history is never inherited. The first native binding
also refuses populated legacy history that lacks that native identity. Catalog
observation frontiers cannot regress, including across an unavailable catalog.

The publisher accepts the typed enrollment. Its recovery input and the output
snapshot commit in the same transaction, under the canonical account stream.
Warm recovery reproduces native identity, stale membership and exact wave history.
Publication contract v5 carries compact identity/reference/gap records in the
changed-symbol representation. Old code
and payloads require explicit reconstruction/upgrade, not an implicit cold reset.

The current reader resolves broker aliases or native UUIDs to one shared provider
view. For example, `ABR.PRD` resolves to `ABR-D`. An ambiguous alias reports the
conflicting UUIDs; missing mappings and retained-but-stale bindings are explicit.
Once native enrollment is active, raw provider spelling is not an alias fallback.
All receipt states retain one context/source cursor and observation-only authority.

## Full catalog capacity evidence

Frozen actual catalogs enrolled all 13,426 mapped tradable equities, including 365
hyphen symbols, without rank truncation. Nine tradable equity mapping gaps and 73
native crypto gaps remain visible. No prints or subscriptions were manufactured.

Cold prefixes now allocate sparse tree nodes only for observed paths. At the same
76,000-print diagnostic retained capacity used by the frozen TNON oracle, the
13,426 cold prefixes used about 32.6 MB of traced retained allocations (34.4 MB
peak), with zero tree nodes. This is an allocation experiment, not a chosen live
strategy window or a complete process-memory measurement. The 76,000-print replay
still matched 148 quote turns, 32,208 recursive turns, 42 checkpoints and 432 exact
flow intervals.

The experiment exposed a scaling constraint: a whole cold output snapshot
is 10,434,541 bytes; encoding took 1.13 seconds and decoding 3.26 seconds on this
host. The enrollment recovery input is 4,407,221 bytes. These measurements do not
establish adequate per-release throughput. Changed-symbol publications now replace
whole-universe repeated serialization while preserving a coherent shared revision,
exact recovery and event delivery (changed_symbol_context_publications.md). The
compact complete manifest is still verified; no top-N filter is introduced.
Native catalog refresh belongs outside the tick path.

The component is tested in the isolated database and remains in draft PR1429.
Actual publisher/native worker hosting, coordinated producer/migration rollout,
native crypto execution and validated entry/exit policy wiring remain unfinished.
