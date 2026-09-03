# Phase 1 (IQFeed half) — historical tick entitlement, established by testing

Date: 2026-09-02 · Branch: `seam/historical-hydrator-0902` · Worktree: `E:/dev/wt-fix4`

## Headline

**The blocker was not real.** Every symbol in the missed set has full historical
tick data — including bid/ask at trade time and extended hours — one lookup
request away. And IQFeed lookup is not merely *comparable* to our recorded tape,
it is **bit-identical** to it once one timezone conversion is applied.

The counterfactual study does not need to be downgraded to minute-bar fidelity.

## 1. Safety against the running lane — proved, not assumed

IQConnect is a single process (pid 28832) listening on **five separate sockets**:
`:5009` (L1 stream), `:9100` (lookup), `:9200` (L2 depth), `:9300`, `:9400`.

| Port | Holder | Established |
|---|---|---|
| 5009 | trade bridge, pid 18544 | local port 58012, created 20:00:05Z |
| 9200 | depth bridge, pid 31232 | local port 61229, created 04:38:04Z |
| **9100** | **nobody** | **idle — the lane never opens it** |

The lookup port is physically distinct from both streams and unused by the lane,
so there is no socket to collide with and no lookup slot to steal.

The memory-recorded hazard — *IQConnect exits ~5s after its last client
disconnects* — cannot apply here: pids 18544 and 31232 hold `:5009` and `:9200`
open continuously, so this client is never the last one out. The client encodes
this as an interlock (`assert_lane_clients_present()`) that **refuses to connect**
if nothing is holding `:5009`, and refuses by construction to open 5009/9200/9300/9400.

### Falsifiable evidence

Observables chosen so a disturbance could not hide: the bridge's **socket
identity** (a drop/reconnect would change the ephemeral local port and the
creation time) and the tape's **cumulative insert counter** (`pg_stat_user_tables`,
O(1) — a predicate scan on the 90GB table blows the 30s timeout).

| Checkpoint | bridge socket | IQConnect pid/start | `iqfeed_trade_ticks` n_tup_ins |
|---|---|---|---|
| baseline T0 23:05:49Z | 58012 @ 20:00:05Z | 28832 @ 04:38:03Z | 7,629,337 |
| baseline T1 23:06:16Z | 58012 @ 20:00:05Z | 28832 @ 04:38:03Z | 7,629,621 |
| after smoke 23:06:51Z | 58012 @ 20:00:05Z | 28832 @ 04:38:03Z | 7,629,872 |
| +12s (past 5s window) 23:07:15Z | 58012 @ 20:00:05Z | 28832 @ 04:38:03Z | 7,630,129 |
| after full battery 23:10:30Z | 58012 @ 20:00:05Z | 28832 @ 04:38:03Z | 7,632,001 |
| after 500k-tick pull 23:12:29Z | 58012 @ 20:00:05Z | 28832 @ 04:38:03Z | 7,632,949 |
| final 23:16:11Z | 58012 @ 20:00:05Z | 28832 @ 04:38:03Z | 7,634,422 |

Identical socket identity throughout; tape advanced **+5,085 ticks** without a
pause. Escalation was staged: connect-and-ack only → one 5-tick request → the
full battery → a 500,000-record / 50 MB pull → 120 back-to-back requests.
**IQConnect spent 14.5s of CPU total** across all of it (1395.9s → 1409.6s).

## 2. What our subscription actually yields

Protocol ack on `:9100` is `S,CURRENT PROTOCOL,6.2` — same protocol the bridge
uses. Tick record layout (HTT/HTX):

```
LH,<timestamp ET>,<last>,<lastsize>,<totalvolume>,<bid>,<ask>,<tickid>,<basis>,<mktcenter>,<conditions>,<aggressor>,<daycode>
LH,2026-09-02 19:07:44.107328,324.7000,50,33763467,324.7000,324.7900,452964,E,19,17,0,2,
```

| Question | Answer | How established |
|---|---|---|
| Tick history for never-subscribed symbols? | **YES — 8/8** | FLYE, LIDR, GYGY, AUUD, RDAC, SSM, BIAF, CANF all returned ticks |
| Bid/ask at trade time? | **YES**, fields 6–7 of every tick | present in every record returned |
| Extended hours? | **YES**, unfiltered by default | pre-market 09:28 ET and post-market 19:58 ET ticks both returned |
| Extended-hours *flagged*? | **YES** — basis `C`=regular, `E`/`O`=extended; condition `87` = form-T | observed across regular vs after-hours windows |
| Timestamp precision | **microsecond** | `19:07:44.107328` |
| Retention | **exactly 180 calendar days** | see below |
| Per-request datapoint ceiling | **none found below 500,000 records** | 500k returned in 4.1s |
| Throughput | **~121,000 ticks/sec**, 50 MB in 4.1s | 500k-record request |
| Throttle | **none hit**; ~5.4 req/s, latency-bound (~186 ms RTT) | 120 sequential requests, 0 errors |

### Retention is a clean cliff at 180 calendar days

Probed every weekday across the boundary (1 datapoint each, trivially cheap):

| Date | Days back | Result |
|---|---|---|
| 2026-02-23 … 2026-03-05 | 191 … 181 | `NO_DATA` (all of them) |
| **2026-03-06** | **180** | **data** |
| 2026-03-09 | 177 | data |

Not a holiday artifact — every weekday in the range was tested. `NO_DATA`
responses at 2026-07-03 and 2026-04-03 in the coarse pass were separately
explained as market holidays (observed Independence Day; Good Friday).

## 3. Fidelity vs our own recorded tape — and the landmine

### The landmine: the two sources disagree on timezone by exactly 4 hours

- `iqfeed_trade_ticks.observed_at` is `timestamp WITHOUT time zone` holding **UTC**.
  Proved: `observed_at` interpreted as UTC minus `provider_event_at` (which *has*
  a zone) = **exactly 0.0 s** on all sampled rows.
- IQFeed **lookup returns ET-naive** (America/New_York wall clock). Proved: wall
  clock UTC minus last-tick timestamp = **4.001 hours**.

A hydrator that writes lookup timestamps straight into `observed_at` shifts every
hydrated tick by the ET→UTC offset and the corruption is *silent* — the rows look
perfectly well-formed. Worse, **the offset is not constant**: US DST began
2026-03-08, and retention reaches back to 2026-03-06, so the two oldest
retrievable days are EST (−5h) and everything after is EDT (−4h). Phase 2 must
convert with `zoneinfo("America/New_York")`, never a fixed offset.

Note also that `momentum_nbbo_spread_tape.observed_at` is `timestamp WITH time
zone` while the tick table is `WITHOUT` — the two hydration targets need
different handling.

### With the conversion applied: exact agreement

CANF, 10-minute window 22:45:43–22:55:43 UTC (= 18:45:43–18:55:43 ET), compared
against our own live recording tick by tick:

| Metric | Result |
|---|---|
| Recorded ticks | 29 |
| Hydrated ticks | 29 |
| Exact `(timestamp, price, size)` matches | **29 / 29** |
| Recorded-only (we have, provider missing) | **0** |
| Hydrated-only (provider has, we missed) | **0** |
| Max abs price difference | **0.0** |
| Max abs bid difference | **0.0** |
| Max abs ask difference | **0.0** |
| First/last timestamp agreement | identical to the microsecond |

Not "within tolerance" — **zero disagreement**. This is expected and it is the
whole argument for preferring this source: the lookup port is the *same feed* our
bridge records, so symbology, conditions, market centers and microsecond
timestamps are the same by construction rather than by reconciliation.

**Verdict: IQFeed lookup is fit for FSM replay**, not merely for measurement. The
Massive/Polygon half of Phase 1 still needs running, but it now has a demanding
benchmark to beat rather than an open question.

## 4. Practical limits for Phase 2

- **Retention caps the corpus at 2026-03-06.** Any requested symbol-day older
  than that is unobtainable from IQFeed at tick fidelity — the hydrator must fail
  those loudly rather than silently substituting a coarser source.
- **One lookup connection at a time**, reused across requests. IQFeed limits
  simultaneous lookup connections, not request count, and the measured rate is
  latency-bound anyway.
- **Chunk large pulls.** There is no provider-side record ceiling below 500k, so
  the binding constraint is client memory: 500k ticks = 50 MB of ASCII. Hydrate
  per symbol-day and stream into `COPY FROM STDIN` rather than buffering.
- **A byte-cap abort poisons the connection.** Found during the battery: aborting
  mid-response leaves the remainder of the aborted response in the socket, and
  the *next* request silently inherits it. Fixed — the client now marks itself
  poisoned and requires `reconnect()`.
- **Budget:** a full liquid-name session is ~500k ticks; the low-float missed
  movers are far smaller. At ~121k ticks/s the provider is not the bottleneck —
  the database write path is, which is why Phase 2 uses `COPY FROM STDIN`.

## 5. Artifacts

| File | Contents |
|---|---|
| `scripts/iqfeed_lookup_client.py` | the lookup client (new; repo had no `:9100` client at all) |
| `tests/test_iqfeed_lookup_client.py` | 11 passing unit tests binding the safety contract |
| `phase1_iqfeed_entitlement.json` | raw battery output |
| `phase1_iqfeed_refinement.json` | retention boundary, true ceiling, timezone, throttle |
| `phase1_fidelity_spotcheck.json` | tick-by-tick comparison vs our recording |
| `phase1_timestamp_basis.json` | proof that `observed_at` is UTC-naive |

Reproduce:

```bash
python scripts/iqfeed_lookup_client.py --smoke
python scripts/iqfeed_lookup_client.py --htx FLYE 5
python scripts/iqfeed_lookup_client.py --htt CANF 20260902 184543 20260902 185543
```
