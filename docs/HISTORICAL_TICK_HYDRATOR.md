# Historical tick / NBBO hydrator

**What it is:** infrastructure that makes *any symbol on any past date within
provider retention* drivable through the real FSM, by buying the tick and NBBO
history we never recorded and landing it in the same tables replay already
reads.

**Why it exists:** `iqfeed_trade_ticks` and `momentum_nbbo_spread_tape` only
ever hold symbols the live bridge happened to be *subscribed* to. A coverage
study found 307 in-band movers we never discovered and therefore have no tick
history for. That is not a feasibility wall — we pay for IQFeed and hold live
Massive/Polygon credentials, and Phase 1 proved every one of those symbol-days
is a request away.

---

## Safety: read this before the first run

### `chili_hydrated` is a separate DATABASE, not a separate CLUSTER

"Separate database" means **logical** separation only. `chili_hydrated` and the
live 222 GB `chili` share **one postmaster, one WAL, one 4 GB `shared_buffers`,
and one bind mount** (`E:/CHILI-Docker/postgres`). A multi-million-row `COPY`
into the hydrated database evicts the live lane's buffers, shares its WAL, and
competes for the same spindle. "No writes to `chili`" is **not** the same claim
as "no impact on the lane's database", and this codebase has a documented
history of Postgres I/O starvation incidents.

Re-verify rather than trusting this paragraph:

```powershell
docker inspect chili-home-copilot-postgres-1 --format '{{json .Mounts}}'
psql -h localhost -p 5433 -U chili -d chili_hydrated -c "
  SELECT datname, pg_size_pretty(pg_database_size(datname))
  FROM pg_database WHERE datname IN ('chili','chili_hydrated');
  SHOW shared_buffers; SHOW data_directory;"
```

Two interlocks follow from this and are enforced **in code**, not by convention:

- **Market hours.** A load started between **04:00 and 20:00 ET** is refused
  unless you pass `--allow-market-hours`. The window applies on every day of the
  week — the cluster is shared every day, and a rule with no calendar edge case
  is a rule that cannot be got wrong. Phase 4's clean run depended on 98 % of its
  35.5 M rows landing after the close; that was scheduling luck until this
  existed.
- **One hydrator at a time, machine-wide.** A session-level Postgres advisory
  lock on the hydrated database. IQFeed limits *simultaneous* lookup connections
  and the behaviour past that limit was never tested — deliberately, because the
  failure mode plausibly affects the shared IQConnect process the live L1 bridge
  depends on. Two agent sessions running the hydrator would each open their own
  `:9100` socket. The second one now exits **3** with `another hydrator is
  running`. The lock dies with the session, so a crashed run cannot wedge the
  next one.

### Do not use `--no-verify-lane` while the lane is live

`scripts/iqfeed_lookup_client.py --no-verify-lane` disables
`assert_lane_clients_present()`, the single mechanism keeping this client from
becoming IQConnect's **last** connection — after which IQConnect exits ~5 s
later and takes the live lane's market data with it.

It is genuinely useful when the lane is **down** (nothing holds `:5009`, so the
interlock would otherwise refuse a legitimate backfill). It is refused on its
own: it must be paired with `--i-accept-iqconnect-shutdown-risk`, and it prints
a warning banner. `resolve_verify_lane()` is the only path by which
`verify_lane` becomes `False`, and a test asserts that.

---

## Quick start

```powershell
# 0. one-time: create the hydration database and its schema
python scripts/historical_tick_hydrator.py --init-db

# 1. hydrate individual symbol-days (IQFeed is the default provider).
#    Refused inside 04:00-20:00 ET; run after the close, or say --allow-market-hours.
python scripts/historical_tick_hydrator.py --symbol-day CANF:2026-09-02

# 2. hydrate a corpus from CSV (needs a `symbol`/`ticker` column and a
#    `date`/`trading_day`/`session_date`/`day` column; extra columns ignored)
python scripts/historical_tick_hydrator.py --csv project_ws/.../corpus.csv

# 3. same corpus from Polygon/Massive instead
python scripts/historical_tick_hydrator.py --provider polygon --csv corpus.csv

# 4. ALWAYS canonicalize after loading (see trap 0 below) and verify coverage
python scripts/hydration_canonicalize.py --apply
python scripts/hydration_coverage_report.py --csv corpus.csv --json coverage.json

# 5. BEFORE running a study: prove it will not return an empty result (trap 3)
python scripts/hydration_preflight.py --csv corpus.csv --allow-pre-source-entries

# 6. what is loaded, what failed and why
python scripts/historical_tick_hydrator.py --status
```

`--status` reports the job ledger. It cannot tell you whether a symbol-day is
actually *replayable* — a job marked `done` that produced zero rows is a hole,
not coverage. `hydration_coverage_report.py` counts rows in the tables the
replay reads, against your corpus as the denominator, and reports those holes.

**"Replayable" means trades AND NBBO**, with NBBO counted using the loader's own
predicate (`bid > 0 AND ask > 0 AND ask >= bid`). Trades alone are *not*
replayable: `counterfactual_replay._confidence` returns `no_tape` the moment the
NBBO tape is empty, and bar-candidate generation is guarded on NBBO ticks — so a
trades-only symbol-day yields **zero** candidates, not few. Those are reported
in their own tier, `tape_only_not_replayable`.

Everything lands in a **separate database** (`chili_hydrated` by default,
`--db-name` to change). The hydrator *refuses* to target `chili` or
`chili_test` — but see the safety section above for what "separate" does and
does not mean. Run it from a git worktree and it finds the main checkout's
`.env` automatically; `--env-file` / `CHILI_ENV_FILE` override.

---

## Provider comparison

| | IQFeed lookup (`:9100`) | Polygon / Massive v3 |
|---|---|---|
| Tick trades | yes | yes |
| NBBO quotes | **on every trade row** (L1 ships trade + top-of-book in one message) | **separate `/v3/quotes` endpoint** — trade rows carry no bid/ask at all |
| Extended hours | included, flagged (`basis` C vs E/O) | included, from 04:00:00 ET |
| Retention | **180 calendar days** (hard cliff) | back to ~2003 |
| Throughput | ~121k ticks/s, ~5.4 req/s latency-bound | ~47.6k rows/s, ~200 rps per front door |
| NBBO fidelity | **at-trade samples only** — no quote history between prints | the real NBBO stream |
| NBBO coverage | **not universal** — returns no top-of-book at all for some OTC names (REEMF 2026-08-19, NLST 2026-08-20: every trade row had null bid *and* ask, though the trades themselves loaded) | broader |
| At-trade bid/ask on a trade row | **native** — L1 ships it with the print | **reconstructed** by as-of merge from `/v3/quotes` |
| Fidelity vs our own tape | our recording is a near-perfect **subset** of it (99.995–100 % of our prints match, same µs, same price) — and it holds prints we never recorded | agrees with IQFeed to ~0.02 % on trades; fractional sizes; at-trade bid/ask is reconstructed |
| Verdict | **fit for FSM replay (trades)** | fit for replay too, and the **only** fit source for NBBO; required past the IQFeed cliff |

**Trades from IQFeed, NBBO from Polygon.** IQFeed lookup exposes no historical
quote *stream* — only the quote attached to each print — so an IQFeed-hydrated
NBBO tape cannot represent a quote that moves between trades, and any gate that
reads quote freshness (spread floors, stale-BBO vetoes) will behave differently
under it than it did live. Reach for Polygon trades when the date is older than
the IQFeed cliff, when you need breadth faster than 5 req/s, or as an
independent second opinion.

Phase 3 measured all of this on seven symbol-days where we hold both tapes; see
`project_ws/AgentOps/historical_hydrator_0902/PHASE3_VALIDATION.md` and run
`scripts/hydration_fidelity_check.py` to reproduce it.

### The at-trade quote is not a free choice — canonicalization already made it

The "at-trade bid/ask" row above is **not** a menu. Canonicalization keeps
IQFeed trades and Polygon quotes, and `load_trade_tape` puts
`iqfeed_trade_ticks.bid/ask` on **every tick it returns**. So on every
canonicalized symbol-day the FSM sees:

- **IQFeed's native at-trade quotes** on the trade tape (`load_trade_tape`), and
- **Polygon's quote stream** on the NBBO tape (`load_nbbo_tape`).

Two vendors, same symbol-day, same run. Canonicalization removed the vendor
flicker *within* the NBBO table; it did not remove it *across* the two tables
the replay reads together, and because the preference deliberately inverts, the
split is universal across every symbol-day rather than incidental.

**This is a deliberate choice, not an oversight.** Keeping IQFeed's native
at-trade quotes preserves a measurement; overwriting them with a Polygon as-of
merge would substitute a reconstruction that is *itself* unvalidated. But a
consumer computing a spread off a trade tick is not holding the same number the
NBBO tape would return at that instant, and must know it.

Measure the seam rather than assuming its size:

```powershell
python scripts/hydration_quote_seam_check.py --csv corpus.csv --json seam.json
```

Every symbol-day in `hydration_coverage_report.py` also names both vendors
explicitly — `trade_quote_vendor` / `trade_quote_derivation` (`iqfeed` →
`native_at_trade_bid_ask`, `polygon` → `as_of_merge_from_v3_quotes`),
`nbbo_vendor`, and a `mixed_vendor_quote_seam` flag.

---

## Provenance: hydrated data can never be mistaken for recorded data

This is enforced by the **existing replay query**, not by convention.
`counterfactual_replay.load_trade_tape` / `load_nbbo_tape` take a
`require_causal_provenance` flag whose strict predicate demands, among others:

```sql
source = 'iqfeed_l1' AND provider_event_at IS NULL AND available_at IS NOT NULL
```

Every hydrated row violates all three, **independently**:

1. `source` is one of `iqfeed_lookup_hist`, `iqfeed_lookup_bbo`,
   `polygon_v3_trades`, `polygon_v3_quotes` — never `iqfeed_l1`.
2. `provider_event_at` is NOT NULL (we know the provider's event clock).
3. `available_at` is **NULL**, deliberately. For hydrated data there is no
   honest answer to *"when would the live lane have first seen this row"*, and
   `available_at` is the clock strict causal replay trusts. Fabricating one
   would be the most dangerous thing this tool could do.

So hydrated rows are **visible to ordinary replay** and **invisible to strict
causal replay**. Verified end to end: with 185,830 hydrated CANF rows present,
`load_trade_tape` returns 185,005 ticks in non-strict mode and **0** in strict
mode.

Per-row provenance rides in columns that already existed — no schema change:

| column | carries |
|---|---|
| `bridge_run_id` | the hydration batch UUID → joins `hydration_batches` |
| `bridge_version` | `chili-historical-hydrator/1.0.0` |
| `timestamp_basis` | which provider clock the timestamp came from |
| `message_type` | `H` (hydrated), vs the live bridge's `Q` |
| `source_frame_sha256` | sha256 of the exact provider record bytes |
| `source_frame_sequence` | IQFeed tickid / Polygon `sequence_number` |

Per-batch provenance lives in `hydration_batches`: request count, bytes,
window, rows loaded, rows replaced, and a sha256 over the exact COPY payload.

---

## Five traps this code exists to avoid

### 0. The replay reads *every* source at once

`counterfactual_replay.load_trade_tape` / `load_nbbo_tape` filter, in non-strict
mode, on **symbol, time and `price > 0` only**. There is **no `source`
predicate**. So a symbol-day hydrated from two providers is returned *twice*.

Measured on TMCR 2026-08-24, which held 16,933 `iqfeed_lookup_hist` rows and
16,933 `polygon_v3_trades` rows: `load_trade_tape` returned **33,866** ticks.
Every print doubled, every share of volume doubled, tape speed doubled at
ignition. Nothing looks malformed — the rows are individually valid and the
timestamps are right. Only the counts lie.

The NBBO case is worse: `load_nbbo_tape` returned **23,979** quotes on that
symbol-day — Polygon's 7,076 real stream quotes interleaved with IQFeed's 16,903
at-trade samples, two structurally different tapes blended into one.

So **always canonicalize after loading**, and gate any study on the check:

```powershell
python scripts/hydration_canonicalize.py --check   # exit 1 if violated
python scripts/hydration_canonicalize.py --apply   # enforce
```

It keeps exactly one source per `(symbol, ET day, table)` — trades prefer
`iqfeed_lookup_hist`, NBBO prefers `polygon_v3_quotes` (the preference
**inverts** between the tables; that is the Phase 3 verdict). The
lower-preference source is a *fallback, not a duplicate*: it survives when it
stands alone, which is real — IQFeed returned no top-of-book at all for the OTC
names REEMF 2026-08-19 and NLST 2026-08-20 whose trades loaded fine.

Dropping the non-preferred copy loses nothing durable; a cross-check copy is one
command away via `--db-name chili_hydrated_xcheck`.

### 0b. The tape is as-traded; aggregate-derived levels are split-adjusted

Provider **tick** endpoints (IQFeed `HTT`, Polygon `/v3/trades`) return
**as-traded** prices. Provider **aggregate** endpoints (Polygon grouped-daily
and per-ticker minute bars) return **split-adjusted** prices by default. A study
that sets entry/stop levels from aggregates and evaluates them against a
hydrated tick tape is comparing two different price scales.

Measured on the Phase 4 corpus: **15 of 119 symbol-days (11 symbols) off by
5×–160×** — LGCL 2026-08-26 corpus band midpoint $9.125 against a tape median
print of $0.0605.

The tape is not wrong. **Dollar volume is invariant under split adjustment**, and
on all 15 the tape's dollar volume matches the corpus to within 0.4 % (LGCL
$49,520,244 vs $49,503,126, ratio 1.0003). Both providers agree on the
as-traded price. The levels are simply denominated differently.

```powershell
python scripts/hydration_price_scale_check.py --csv corpus.csv   # exit 1 if flagged
```

It separates the two hypotheses rather than just reporting a difference:
dollar volume agrees but price does not → `split_adjusted_levels` (tape fine,
rescale your levels); dollar volume disagrees → `volume_mismatch` (a coverage or
inclusion-rule question, *not* a split). It compares **median** price, not the
high — one stray one-share print moves the max by orders of magnitude and
produced three false positives when this was first tried against the max.

### 1. The timezone landmine

`iqfeed_trade_ticks.observed_at` is `TIMESTAMP **WITHOUT** TIME ZONE` holding
UTC. `momentum_nbbo_spread_tape.observed_at` is `TIMESTAMP **WITH** TIME ZONE`.
The IQFeed lookup port returns **ET-naive** timestamps.

Writing lookup timestamps straight through shifts every row by four or five
hours — and the rows still look perfectly well-formed. Worse, the offset is not
constant: DST began 2026-03-08 while retention reaches back to 2026-03-06, so
the two oldest retrievable days are EST and everything after is EDT.
Conversion therefore goes through `zoneinfo("America/New_York")`, never a fixed
offset, in exactly one function (`et_naive_to_utc`).

### 2. IQFeed truncation drops the *oldest* records

When an HTT response is capped by `MaxDatapoints`, IQFeed keeps the **newest**
N and discards the oldest — regardless of `DataDirection`, which only controls
the order of the lines it sends. Measured: a 1,670-tick window requested with
`MaxDatapoints=835` returned, under *both* directions, the records ending at the
window's last tick and starting mid-window.

A naive reader therefore silently loses the *front* of every busy window — the
ignition, which is the part a momentum study cares about most. The hydrator
**continues backward** instead: re-request `[begin, oldest_returned]` and filter
on `ts < oldest_seen` so the seam neither loses nor duplicates a tick. HTT
bounds are also *inclusive at second resolution*, so tiled windows end one
second before the next begins.

### 3. The source gate is empty for any corpus after early July 2026

**This is the trap that makes the whole deliverable return nothing, silently.**

`run_counterfactual_symbol_replay` gates every entry on
`require_source_before_entry` (**default `True`**) and skips with
`no_ross_source_before_entry`. Those source events do **not** come from the
database: `load_ross_source_events` reads JSONL files under
`D:\CHILI-Docker\chili-data\ross_stream\` —

| file | state (2026-09-02) |
|---|---|
| `transcript.jsonl` | last written **2026-07-07** |
| `ross_transcript_admission_audit.jsonl` | last written **2026-07-01** |
| `ross_admission_dry_run.jsonl` | last written **2026-07-01** |
| `ross_trade_events.jsonl` | **does not exist** |

Measured over the whole hydrated corpus window (2026-08-19 → 2026-09-03, no
symbol filter): `load_ross_source_events` returns **`{}` — zero events for every
symbol**. So the default run admits nothing, exits **0**, and prints a replay
with zero trades and an empty opportunity-label summary. Nothing in the output
says a flag was needed.

**Point `DATABASE_URL` at `chili_hydrated` and nothing else changes** is
therefore false. Pick an admission mode deliberately — the two flags are **not**
interchangeable:

| flag | `live_admission_mode` | what changes |
|---|---|---|
| `--allow-pre-source-entries` | stays **ON** | candidates still come only from live's real ladder (`_BAR_GATE_FAMILIES`: momentum_pullback / vwap_reclaim / ross_breakout_starter); entries no longer require a catalyst |
| `--no-live-admission-mode` | **OFF** | re-enables the `market_certified` synthetic-source window **and** two harness-only tick families (`tick_first_pullback`, `tick_vwap_reclaim_burst`) that `live_runner.py` never evaluates |

**Use `--allow-pre-source-entries` for a live-parity counterfactual.** It keeps
live's actual entry ladder and drops only the catalyst precondition, which is
the honest adjustment for symbols live never discovered.
`--no-live-admission-mode` is a **different strategy**, not a wider view of the
same one — the module's own docstring gives CELZ 2026-06-30, where it fabricated
three chop entries live would have refused. Use it for opportunity *labelling*,
and never present its output as "what live would have done".

**`--no-live-admission-mode` does NOT clear the source gate.** It leaves
`require_source_before_entry` at `True` and only *re-enables* the
`market_certified` synthetic-source window — which itself still requires
`rolling_volume >= market_volume_floor`. Measured through the real admission
gate on TMCR 2026-08-24 and SMTK 2026-08-20 (13:30–15:30Z): under
`--no-live-admission-mode` a candidate was generated and still skipped with
`no_ross_source_before_entry`. `--allow-pre-source-entries` is the flag that
actually admits.

Measured through `run_counterfactual_symbol_replay` — the real candidate
generator and the real admission gate, not the loaders
(`phase5_admission_acceptance.json`):

| symbol-day | mode | candidates | trades | `skipped_reasons` |
|---|---|---|---|---|
| TMCR 08-24 | default | 1 | 0 | `no_ross_source_before_entry: 1` |
| TMCR 08-24 | `--allow-pre-source-entries` | 1 | 0 | `simulate_no_trade: 1` |
| TMCR 08-24 | `--no-live-admission-mode` | 1 | 0 | `no_ross_source_before_entry: 1` |
| SMTK 08-20 | default | 1 | 0 | `no_ross_source_before_entry: 1` |
| SMTK 08-20 | `--allow-pre-source-entries` | 1 | **1** | — |
| SMTK 08-20 | `--no-live-admission-mode` | 1 | 0 | `no_ross_source_before_entry: 1` |

Both symbol-days are ones our own recorded tape holds **zero** rows for, and the
gate-reason counts show the real ladder evaluating
(`ross_breakout_starter_unavailable_on_main`, `pullback_too_deep`,
`vwap_reclaim_not_below_enough`, …). That is the FSM running on bought history.

### A confidence label the whole corpus degrades

`_confidence` assigns `tick_quote_complete` only when some NBBO tick's `source`
contains `"iqfeed"`. The canonical corpus's NBBO source is `polygon_v3_quotes`,
so **every** hydrated symbol-day returns `quote_replay_available` with reason
`no_iqfeed_source_marker`. Nothing in the shipped harness gates on that value
today, but a consumer filtering for `tick_quote_complete` would silently drop
the entire corpus. The label is telling the truth about the tape's provenance —
do not "fix" it by relabelling the source.

Gate every study on the preflight, which asserts both the usable-NBBO predicate
and the source gate and exits non-zero:

```powershell
python scripts/hydration_preflight.py --csv corpus.csv --allow-pre-source-entries
```

---

## Idempotency and resumability

- **Resume:** `hydration_jobs` is keyed `(symbol, trading_day, dataset,
  provider)`. A re-run skips anything already `done`; `--force` re-does it.
  Failures are recorded with their error and do not abort the corpus.
- **Idempotent per symbol-day:** each load runs `DELETE` for that
  `(symbol, source, ET-day)` slice and then `COPY`, inside **one transaction**.
  Re-running converges on exactly the rows the provider currently serves.
  Verified: a second CANF run deleted 185,828 and loaded 185,830; the table
  held 185,830 rows afterwards, not 371,658.
- **No row-level unique index**, deliberately. It would cost more on every COPY
  than it is worth over hundreds of millions of tape rows, *and* it would reject
  genuinely duplicated provider records (two prints at the same microsecond with
  the same size are legal).

## Rate limiting

- **IQFeed:** one pooled lookup connection, reused, serialized. The client
  refuses by construction to open `:5009`/`:9200`/`:9300`/`:9400`, and refuses
  to connect at all unless the live bridge is holding `:5009` — so it can never
  become IQConnect's last client and trigger the 5-second shutdown.
- **Polygon/Massive:** token bucket defaulting to 12 rps (`--rps` to change),
  far below the ~200 rps measured comfortable, because a live lane shares these
  credentials. 429 and 5xx are retried with `Retry-After` honoured and
  exponential backoff with full jitter. The hydrator does **not** inherit
  `settings.polygon_max_rps = 5`; that cap is tuned for the live loop.

## Known limits

- **IQFeed retention is a hard ceiling that will bite.** Any symbol-day before
  `today − 180 days` is unobtainable at tick fidelity. The hydrator fails such a
  request **loudly** (status `failed`, with the floor in the error) rather than
  silently falling back to a coarser source. Check your corpus against the floor
  before planning a hydration run.
- **Polygon trade rows get their bid/ask from an as-of merge**, not from the
  provider — Polygon splits trades and quotes across two endpoints while IQFeed
  ships both in one message. That is a *reconstruction*; `provider_request`
  records `bid_ask: as_of_merge_from_v3_quotes` and the quote-index size.
- **Polygon sizes can be fractional.** Measured on TMCR 2026-08-24: IQFeed
  reported 695,428 shares, Polygon 702,231.39.
- **Crossed, locked and zero quotes are dropped** from the NBBO tape (the replay
  read filters `bid > 0 AND ask > 0 AND ask >= bid` anyway), so the NBBO row
  count is slightly below the provider's raw quote count by design.
- **Nanoseconds are truncated to microseconds** — PostgreSQL's resolution.
- Memory is bounded per IQFeed window (`--window-minutes`, default 60), not per
  day. A mega-cap full session is still large; lower the window for those.
- **The canonical corpus is mixed-vendor at the quote level** — IQFeed at-trade
  quotes on the trade tape, Polygon quotes on the NBBO tape, on every
  symbol-day. See "The at-trade quote is not a free choice" above and measure it
  with `scripts/hydration_quote_seam_check.py`.
- **`hydration_canonicalize.py --apply` deletes the second provider's copy.**
  It is reproducible (`--db-name chili_hydrated_xcheck`, ~45 min for the Phase 4
  corpus), but after canonicalization the per-symbol-day comparison in the
  saved `*_canonicalize_plan.json` is the only surviving record of the tape that
  was dropped.
- **A large `--apply` leaves dead tuples.** Autovacuum reclaims them on its own
  cost throttle rather than competing with the live lane; confirm with
  `SELECT relname, n_dead_tup, last_autovacuum FROM pg_stat_user_tables` in
  `chili_hydrated` rather than assuming.

---

## Validating a hydrated day against our own recording

`scripts/hydration_fidelity_check.py` compares a hydrated symbol-day against
`chili` tick by tick. Run it on any day you also recorded, before trusting
either tape.

```powershell
python scripts/hydration_fidelity_check.py --symbol-day SDOT:2026-08-24 --json out.json
python scripts/hydration_fidelity_check.py --symbol-day SSM:2026-09-01 --provider iqfeed
```

It reads `chili` **read-only** under a 30 s statement timeout, with keyset
pagination that shrinks its page on timeout rather than raising the limit, and
writes nothing anywhere. It reports, per symbol-day:

- multiset alignment on `(observed_at, price)` — never a set, because two fills
  at one microsecond and price are legal and set semantics would flatter both
  tapes;
- the same alignment against a **frame-deduplicated** copy of our recording
  (`source_frame_sha256` repeats mean the same L1 frame was written twice, which
  happens when two bridge processes overlap);
- session high/low **and the exact instant of each**, plus day volume;
- miss rate bucketed by tape speed — whether the loss is concentrated in the
  fast seconds, i.e. at ignition;
- residual timestamp skew, which is where a systematic clock offset would live;
- at-trade bid/ask agreement on matched prints;
- NBBO agreement at 2,000 as-of sampled instants, plus a structural read of how
  many *distinct* instants each quote tape actually carries.

**What Phase 3 found with it (2026-09-02, seven symbol-days):** the hydrated
tape does not contradict our recording, it *contains* it — but our recording is
66–100 % complete depending on the day, duplicates up to 46 % of its rows when
two bridge processes overlap, and on SDOT 2026-08-24 misses the session high by
10.5 % inside a 9 min 27 s blackout. Treat the recorded tape as authoritative
for **lane behaviour** and hydrated data as authoritative for **market truth**.
