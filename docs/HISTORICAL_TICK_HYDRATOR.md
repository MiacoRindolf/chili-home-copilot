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

## Quick start

```powershell
# 0. one-time: create the hydration database and its schema
python scripts/historical_tick_hydrator.py --init-db

# 1. hydrate individual symbol-days (IQFeed is the default provider)
python scripts/historical_tick_hydrator.py --symbol-day CANF:2026-09-02

# 2. hydrate a corpus from CSV (needs a `symbol`/`ticker` column and a
#    `date`/`trading_day`/`session_date`/`day` column; extra columns ignored)
python scripts/historical_tick_hydrator.py --csv project_ws/.../corpus.csv

# 3. same corpus from Polygon/Massive instead
python scripts/historical_tick_hydrator.py --provider polygon --csv corpus.csv

# 4. what is loaded, what failed and why
python scripts/historical_tick_hydrator.py --status
```

Everything lands in a **separate database** (`chili_hydrated` by default,
`--db-name` to change). The hydrator *refuses* to target `chili` or
`chili_test`. Run it from a git worktree and it finds the main checkout's
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
| Fidelity vs our own tape | **bit-identical** — same feed our bridge records | very close, but a different clock and a reconstructed bid/ask |
| Verdict | **fit for FSM replay** | fit for replay where IQFeed retention has expired; fit for measurement everywhere |

Prefer IQFeed inside the 180-day window. Reach for Polygon when the date is
older than the IQFeed cliff, when you need breadth faster than 5 req/s, or as
an independent second opinion.

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

## Two traps this code exists to avoid

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
