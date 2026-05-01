# Fast-path architecture (F1+)

**Status**: Phase F1 in development (2026-05-01).
**Goal**: enable minute-level momentum scalping on crypto, exclusively through Coinbase Advanced Trade. Runs alongside the existing swing brain — neither depends on the other for liveness.

This doc is the **contract** for the fast lane. Code that violates it should fail review. Update this doc in the same PR as any architectural change.

---

## Why a separate lane

The existing chili pipeline (swing brain, bracket lifecycle, autotrader) is structurally a 15-min-to-multi-day system. Today's investigation (`docs/AUDITS/2026-05-01-trading-system-audit.md` and the broker-cadence audit) found minimum effective reaction time of ~15–30 minutes. That can't be retrofitted to <1s without rewriting the data ingestion + scanner + executor layers — which would risk the swing system that took today to stabilize.

The fast lane is a parallel module. They share `trading_trades` and `trading_bracket_intents` for accountability, but everything else (ingestion, scanning, execution, exit) is independent.

---

## Hard rules (violating any of these breaks correctness or safety)

1. **Coinbase Advanced Trade only.** Robinhood crypto's effective spread (0.5–1.5%) is structurally incompatible with sub-percent scalp edges. The fast lane refuses Robinhood as a venue. Robinhood crypto stays for slow swings only.
2. **Paper mode is the default.** `CHILI_FAST_PATH_MODE=paper` (default) means: ingest, scan, decide — but do NOT submit orders. `=live` flips real placement. Operator opt-in only.
3. **No memory leaks under sustained load.** Every in-memory structure has a hard size cap (sliding window, bounded queue, top-N order book). RSS must stay flat over a 24h soak.
4. **No silent data gaps.** Sequence numbers are tracked on every WS channel that exposes them. A detected gap triggers a REST snapshot recovery, with the recovery logged. Gaps that can't be recovered halt the affected pair (not the whole lane).
5. **One bad pair does not poison the lane.** Per-pair circuit breakers isolate a misbehaving stream. Other pairs keep flowing.
6. **Database unreachable halts the lane intentionally.** Better to drop signal than persist corrupt data. CRITICAL log + healthz fails. Operator decides recovery.
7. **No L1/L2 data leaks into the swing pipeline.** The swing brain reads only its existing data sources (`trading_snapshots`). The fast lane writes to `fast_*` tables that the swing brain doesn't subscribe to. Cross-contamination would invalidate swing pattern statistics.

---

## Data model

Three new tables. All partitioned by day for efficient drop.

### `fast_snapshots`

One row per closed bar per (ticker, interval).

```sql
CREATE TABLE fast_snapshots (
    id              BIGSERIAL,
    ticker          VARCHAR(32) NOT NULL,
    interval        VARCHAR(8) NOT NULL DEFAULT '1m',
    bar_open_at     TIMESTAMP NOT NULL,
    bar_close_at    TIMESTAMP NOT NULL,
    open_price      DOUBLE PRECISION NOT NULL,
    high_price      DOUBLE PRECISION NOT NULL,
    low_price       DOUBLE PRECISION NOT NULL,
    close_price     DOUBLE PRECISION NOT NULL,
    volume          DOUBLE PRECISION NOT NULL,
    trade_count     INTEGER,
    vwap            DOUBLE PRECISION,
    source          VARCHAR(32) NOT NULL DEFAULT 'coinbase',
    received_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, bar_close_at)
) PARTITION BY RANGE (bar_close_at);

CREATE INDEX ix_fast_snapshots_ticker_close
    ON fast_snapshots (ticker, bar_close_at DESC);
```

### `fast_orderbook` (used in F2; F1 ships the schema empty)

Periodic L2 snapshots — top 25 levels per side.

```sql
CREATE TABLE fast_orderbook (
    id              BIGSERIAL,
    ticker          VARCHAR(32) NOT NULL,
    snapshot_at     TIMESTAMP NOT NULL,
    bid_levels      JSONB NOT NULL,
    ask_levels      JSONB NOT NULL,
    bid_total_size  DOUBLE PRECISION,
    ask_total_size  DOUBLE PRECISION,
    imbalance       DOUBLE PRECISION,
    spread_bps      DOUBLE PRECISION,
    source          VARCHAR(32) NOT NULL DEFAULT 'coinbase',
    PRIMARY KEY (id, snapshot_at)
) PARTITION BY RANGE (snapshot_at);

CREATE INDEX ix_fast_orderbook_ticker_at
    ON fast_orderbook (ticker, snapshot_at DESC);
```

### `fast_path_status`

Per-pair health and circuit-breaker state. Single row per ticker, updated in place.

```sql
CREATE TABLE fast_path_status (
    ticker            VARCHAR(32) PRIMARY KEY,
    state             VARCHAR(16) NOT NULL,
        -- 'streaming' | 'degraded' | 'paused' | 'halted'
    last_bar_at       TIMESTAMP,
    last_seq          BIGINT,
    error_count_60s   INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    last_reconnect_at TIMESTAMP,
    reconnect_count   INTEGER NOT NULL DEFAULT 0,
    updated_at        TIMESTAMP NOT NULL DEFAULT NOW()
);
```

---

## Module layout

```
app/services/trading/fast_path/
├── __init__.py
├── settings.py         # Pairs, queue size, window depth, mode flag
├── ws_client.py        # Coinbase WS connection lifecycle (connect, subscribe, reconnect)
├── bar_aggregator.py   # Aggregates ticker channel into 1m bars (or consumes candles channel)
├── orderbook.py        # F2 — L2 book maintenance from snapshot + deltas
├── db_writer.py        # Bounded write-coalescing queue + batched INSERT
├── status_tracker.py   # Updates fast_path_status table
├── supervisor.py       # asyncio.run() entry — wires everything
└── healthz.py          # tiny aiohttp endpoint for compose healthcheck

scripts/fast_data_worker.py  # Container entrypoint
```

### Component responsibilities

| Module | Owns | Doesn't own |
|---|---|---|
| `ws_client` | Connection lifecycle, reconnect, sequence tracking | Bar shape, DB writes |
| `bar_aggregator` | Tick-to-bar aggregation (or candle channel pass-through) | WS connection, DB writes |
| `orderbook` (F2) | L2 book in memory, top-N truncation | WS connection, DB writes |
| `db_writer` | Bounded queue, batched INSERT, write-side backpressure | Bar shape, scheduling |
| `status_tracker` | Per-pair state, error counts, circuit-breaker decisions | Anything else |
| `supervisor` | Boot, asyncio task supervision, graceful shutdown | Logic of any single component |
| `healthz` | HTTP `/healthz` for compose | Anything else |

Each component is independently importable + testable. No cross-references except through interfaces.

---

## Memory + queue bounds (concrete numbers)

| Bound | Default | Configurable |
|---|---|---|
| In-memory bar window per (ticker, interval) | 500 bars | `CHILI_FAST_PATH_BAR_WINDOW` |
| L2 book depth held in memory per ticker (each side) | 25 levels | `CHILI_FAST_PATH_BOOK_DEPTH` |
| DB write queue depth | 10,000 items | `CHILI_FAST_PATH_QUEUE_MAX` |
| DB write batch size | 50 rows | `CHILI_FAST_PATH_BATCH_SIZE` |
| DB write batch interval | 200 ms | `CHILI_FAST_PATH_BATCH_INTERVAL_MS` |
| WS reconnect backoff start / max | 1 s / 30 s | hard-coded |
| Per-pair circuit-breaker threshold | 5 errors / 60 s | `CHILI_FAST_PATH_CB_THRESHOLD` |
| Container memory limit (docker) | 512 MB | compose `mem_limit` |
| Container CPU limit (docker) | 1 core | compose `cpus` |

A container at full subscription (5 pairs, 1m bars, top-25 L2) should sit at ~120–180 MB RSS. The 512 MB limit is 3x headroom.

---

## Failure modes (explicit decisions, not "we'll handle that later")

### Soft-fail — keep the lane up, log + degrade

- WS disconnect → reconnect with backoff, log INFO, set state=`degraded` until reconnected
- Single-pair sequence gap → REST recovery, log WARNING; if recovery fails 3x, set that pair to `paused`
- Sub-second tick rate exceeds queue capacity → drop ticks below bar-close granularity; never drop a bar-close event
- Single-pair stream errors >5/60s → circuit-break to `paused`, other pairs continue, log CRITICAL
- Postgres write batch fails (transient) → retry once with exponential backoff, then drop oldest non-bar-close items

### Hard-fail — halt the lane

- Postgres unreachable >30s → halt all WS subscriptions, healthz returns 503, log CRITICAL. Operator restarts after fixing DB.
- Schema migration check fails on startup → exit 1, log CRITICAL. Don't run on the wrong schema.
- Coinbase auth required for selected channel + auth missing → exit 1, log CRITICAL.

### Operational levers

- `CHILI_FAST_PATH_ENABLED=0` (default) → container starts but immediately enters `paused` state for all pairs. Safe deploy without consuming Coinbase quota.
- `CHILI_FAST_PATH_PAIRS="BTC-USD,ETH-USD,..."` → which pairs to subscribe.
- `CHILI_FAST_PATH_MODE=paper|live` → only affects F4 (execution); F1 ingestion is read-only by definition.

---

## Observability

Every minute, the supervisor logs one structured line per pair:

```
[fast_path] pair=BTC-USD state=streaming bars_received=60 bars_dropped=0 ws_reconnects=0 seq_gaps=0 last_bar_age_s=2.3 queue_depth=4 mem_mb=147
```

Counters are also exposed via the `/healthz` endpoint as JSON for scripts to scrape.

---

## Phase plan (this doc covers F1; later phases extend)

| Phase | Status | What lands |
|---|---|---|
| F1 | **in progress** | WS ingestion of `candles` channel for 5 pairs, 1m bars to `fast_snapshots`, status tracking, smoke test |
| F2 | next | L2 order-book ingestion to `fast_orderbook` + imbalance/spread features |
| F3 | after F2 | Event-driven momentum scanner → emits `fast_alerts` rows |
| F4 | after F3 | Async execution path with parallel gates, no LLM, pre-warmed Coinbase auth, sub-second placement |
| F5 | after F4 | Streaming exit manager — checks open fast-lane positions on every L2 tick |
| F6 | after F5 | 1m pattern mining + dedicated CPCV gate calibrated for short-hold/high-noise strategies |
| F7 | after F6 | Position sizing tuned to Coinbase fees + Kelly fraction |

Each phase is mergeable independently. Paper-mode soak between F4 ship and live flip.
