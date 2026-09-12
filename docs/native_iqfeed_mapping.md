# Native inventory to IQFeed instruments

The mapper preserves every native UUID as either a catalog match or an explicit
mapping gap. It joins broker asset class, native symbol/aliases and listing
exchange to an actual provider equity record. Neither equal ticker spelling,
company-name similarity nor stripping a suffix is sufficient. Nontradable assets
stay visible with no inventory demand; crypto stays native and requires its own
source. This is observation routing evidence, not global-ID equivalence, provider
entitlement, fresh tick coverage or trading authorization.

The provider reader consumes the complete official tab-delimited symbol file,
hashes/counts every source row, and retains EQUITY records. Other security types
are consumed rather than mistaken for missing equities. A malformed row, duplicate
equity identity or resource-bound overrun rejects the whole read. Text names use a
reversible Latin-1 byte representation; no fuzzy name matching is performed. The
source digest, observation time and exact original equity-line digest are retained.
The provider's publication/effective date is unknown; download time is not it.

## Format evidence and observed corrections

The [IQFeed stock symbol guide](https://ws1.dtn.com/IQ/Guide/stocks_nyse.html)
defines preferred, rights and warrant suffixes. Alpaca's
[CQS/CMS format guide](https://alpaca.markets/support/what-is-the-difference-between-cqs-and-cms-symbol-format)
describes its host-format counterparts. The mapper supports explicit conversions
such as ABR.PRD to ABR-D and AIIA.RT to AIIA.R only when the resulting provider
instrument actually exists in the matching listing market. Unsupported compound
formats remain gaps. There is no fallback to common shares or a bare symbol root.

The 2022 Alpaca guide's series-character restriction excludes U, but the actual
paired catalogs contain FLG.PRU, NEE.PRU, PSA.PRU and TDS.PRU with provider symbols
ending -U. This observed format is supported with a separately reported
`paired_catalog_preferred_u_suffix` rule, instead of silently enforcing that older
documentation as an eligibility restriction. Their full native/provider metadata
is archived in ASTRA_IQFEED_PREFERRED_U_EVIDENCE.json; no name similarity becomes
an additional automatic mapping rule.

An actual cross-market collision demonstrates why listing identity matters:
Alpaca C.PRN is a NYSE-listed Citigroup instrument, whereas IQFeed C.PRN identifies
PROFOUND MEDICAL CORP on TSE. C-N is the NYSE provider match. Both candidate records
are retained in the receipt. A same-market ambiguity stays unbound. If two distinct
native UUIDs would attach to one provider instrument (for example, a reused ticker
and an older held identity), both retain observation demand but require identity
reconciliation before mapping; neither silently replaces the other.

Broker exchange names from the [assets endpoint](https://docs.alpaca.markets/us/reference/get-v2-assets-1)
are matched to explicit provider consolidated/listed-market pairs. For example,
broker ARCA corresponds to provider NYSE/NYSE_ARCA, whereas NASDAQ listing tiers
are distinct from its OTC feed group. These are instrument/venue identities, not
statistical thresholds. The actual complete catalogs provide the paired field
observations; mismatches stay explicit instead of relaxing markets by frequency.

## Full-catalog result and remaining integration

The downloaded [official provider archive](https://www.iqfeed.net/downloads/download_file.cfm?type=mktsymbols)
contains5,484,490 records and77,139 equities. The native snapshot contains14,381
assets. After the observed U-series correction,14,090 have catalog matches,
including386 suffix conversions;291 remain unbound (217 absent equity symbols,
one listing-market mismatch,73 native crypto assets). Of13,435 tradable native
equities,13,426 have catalog matches; remaining gaps are retained by UUID and reason.
This does not mean13,426 symbols are receiving ticks or have profitable setups.

Source text SHAa535f2c4f476e514a7c2439a4d978962eba598a680dd6724bd8e34f06bba5b5b;
archive SHAd536437eb5cc69dd26413da9f6547656b891bafa2dcbf4a614270ba45aa96c95.
The mapper binds its contract, both source observations and every result into a
content digest. Native/provider clocks identify the observed catalogs only.

The legacy ordinary owner still rejects all hyphens, including365 of these matched
provider instruments. Next connect catalog-backed equity binding evidence to its
durable enrollment/recovery contract and translate native consumer identities to
the same provider context. Do not merely remove its crypto guard. The native
producer/mapper must run outside per-print processing; consumers use published
immutable state. Provider-map persistence/publication, worker hosting, subscription
coverage and coordinated source rollout remain open. No mapping code is deployed
to the running lane, and no broker order or live database mutation was performed.
