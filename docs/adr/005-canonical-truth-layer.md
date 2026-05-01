# 005 — Canonical feature / label / execution-truth layer

**Status:** Draft (2026-04-30) — pending operator review.
**Authors:** internal (response to third-party audit, see
`docs/AUDITS/2026-04-30-third-party-response.md`).
**Supersedes:** none. **Superseded by:** none.

## Context

The 2026-04-30 third-party audit identified the lack of a single
authoritative truth layer as the **biggest blocker to durable
profitability**. CHILI today has many overlapping data stores that each
hold a partial view of the same underlying object — a (ticker,
timestamp, signal, label, fill) row — and the system relies on
implicit joins and ad-hoc filters to reconcile them. The audit found
several concrete consequences of this fragmentation:

1. The active `PatternMetaLearner` trains on `MarketSnapshot` rows with
   a `future_return_5d > 1%` binary label. The richer
   `trading_triple_barrier_labels` store exists but is shadow-only and
   does not drive promotion (`brain_triple_barrier_mode = "shadow"`).
2. `trading_pattern_trades` feature schema v1 has 11 fields and
   explicitly omits regime, sector, SPY context, and earnings flags
   (per `docs/pattern_trade_features_v1.md`).
3. `trading_execution_cost_estimates` and `trading_venue_truth_log`
   are inert (zero rows pre-R28; first real slippage will accrue when
   trading resumes post-R29).
4. Promotion governance is asymmetric: the realized-PnL CPCV gate
   requires ≥15 closed `trading_pattern_trades` rows, while the
   pre-CPCV path needs none. As of mig 213 the population is smaller
   than the audit's 27/28 baseline, but the asymmetry remains
   structural (see `docs/TECH_DEBT.md` T2.5).
5. Survivorship bias is acknowledged
   (`docs/DATA_SURVIVORSHIP_BIAS.md`) but not corrected by a historical
   universe table.

So the same logical row — *what was knowable about (ticker,
timestamp), what was the strategy's signal, what was the label, what
did the broker actually fill* — is split across at least five tables
with mismatched keys, mismatched horizons, and mismatched labels. This
makes:

- **Training** brittle (uses snapshots only).
- **Promotion** asymmetric (CPCV vs heuristic across pattern eras).
- **Drift detection** noisy (observation-only, not matched-baseline).
- **Post-trade attribution** impossible without join gymnastics.

The audit's recommendation is to build a canonical store keyed by
`(ticker, asset_class, bar_interval, bar_start_at, regime, sector,
benchmark_context, event_flags, ...)` so the same row supports
training, validation, promotion, runtime gating, and post-trade
diagnostics.

## Decision

Add **two new tables** keyed back to existing primaries, populated by
**materializer jobs** that synthesize rows from the existing fragmented
sources. Existing tables stay (no destructive migration), but reads
that need a unified view go through the new tables.

### `trading_feature_rows`

One row per `(ticker, asset_class, bar_interval, bar_start_at,
feature_schema_version)`. Rich-context features at a known bar boundary,
suitable for training and runtime evaluation.

```sql
CREATE TABLE trading_feature_rows (
    id BIGSERIAL PRIMARY KEY,

    -- Keys
    ticker TEXT NOT NULL,
    asset_class TEXT NOT NULL,                  -- 'equity'|'crypto'|'option'|'perp'
    bar_interval TEXT NOT NULL,                 -- '1m'|'5m'|'1h'|'1d'
    bar_start_at TIMESTAMP NOT NULL,            -- canonical bar start in UTC
    feature_schema_version INTEGER NOT NULL,    -- starts at 2; v1 lives in pattern_trades

    -- Context (the audit's "missing context" set)
    regime_macro TEXT,                          -- from trading_macro_regime_snapshots
    regime_breadth TEXT,                        -- from trading_breadth_relstr_snapshots
    regime_volatility TEXT,                     -- from trading_vol_dispersion_snapshots
    regime_intraday_session TEXT,               -- from trading_intraday_session_snapshots
    sector TEXT,
    benchmark_context_json JSONB,               -- {spy_change_pct, qqq_change_pct, ...}
    event_flags_json JSONB,                     -- {earnings_within_5d, fomc_window, ...}

    -- Microstructure (from venue_truth_log + execution_cost_estimates)
    spread_bps_p50 DOUBLE PRECISION,
    spread_bps_p90 DOUBLE PRECISION,
    adv_usd DOUBLE PRECISION,
    quote_freshness_seconds DOUBLE PRECISION,
    provider_chain TEXT[],                      -- ['massive', 'polygon', 'yfinance']

    -- Provenance
    source_snapshot_id INTEGER REFERENCES trading_snapshots(id),
    source_pattern_trade_id INTEGER REFERENCES trading_pattern_trades(id),
    source_triple_barrier_label_id INTEGER REFERENCES trading_triple_barrier_labels(id),
    materializer_run_id UUID NOT NULL,          -- the job batch that wrote this row
    materialized_at TIMESTAMP NOT NULL DEFAULT NOW(),

    -- The features themselves
    features_json JSONB NOT NULL,

    UNIQUE (ticker, asset_class, bar_interval, bar_start_at,
            feature_schema_version)
);

CREATE INDEX ix_feature_rows_bar_start
    ON trading_feature_rows (bar_start_at DESC);
CREATE INDEX ix_feature_rows_ticker_bar
    ON trading_feature_rows (ticker, bar_interval, bar_start_at DESC);
CREATE INDEX ix_feature_rows_regime_macro
    ON trading_feature_rows (regime_macro);
```

### `trading_label_rows`

One row per `(feature_row_id, label_kind, label_horizon)`. Multi-horizon
labels keyed back to a feature row so the same feature can be trained
against multiple labels (binary 5d return, triple-barrier, realized
trade outcome).

```sql
CREATE TABLE trading_label_rows (
    id BIGSERIAL PRIMARY KEY,

    -- Keys
    feature_row_id BIGINT NOT NULL
        REFERENCES trading_feature_rows(id) ON DELETE CASCADE,
    label_kind TEXT NOT NULL,                   -- 'forward_return'|'triple_barrier'
                                                -- |'realized_trade_outcome'
    label_horizon_bars INTEGER NOT NULL,        -- e.g. 5 for 5-bar forward
    label_schema_version INTEGER NOT NULL,

    -- Outcome (sparse — populated based on label_kind)
    forward_return_pct DOUBLE PRECISION,        -- forward_return labels
    barrier_hit TEXT,                           -- triple_barrier labels
    barrier_outcome SMALLINT,                   -- triple_barrier {-1, 0, +1}
    realized_pnl DOUBLE PRECISION,              -- realized_trade_outcome
    realized_return_pct DOUBLE PRECISION,
    label_value_binary BOOLEAN,                 -- generic binary projection

    -- Realized execution context (post-trade only)
    venue_truth_log_id INTEGER
        REFERENCES trading_venue_truth_log(id),
    realized_cost_fraction DOUBLE PRECISION,
    realized_slippage_bps DOUBLE PRECISION,
    cost_gap_bps DOUBLE PRECISION,              -- realized minus expected

    -- Provenance
    source_table TEXT NOT NULL,                 -- 'snapshots'|'triple_barrier'|'trades'
    source_id INTEGER NOT NULL,
    materializer_run_id UUID NOT NULL,
    materialized_at TIMESTAMP NOT NULL DEFAULT NOW(),
    label_emitted_at TIMESTAMP NOT NULL,        -- when the outcome was knowable

    UNIQUE (feature_row_id, label_kind, label_horizon_bars,
            label_schema_version)
);

CREATE INDEX ix_label_rows_feature
    ON trading_label_rows (feature_row_id);
CREATE INDEX ix_label_rows_kind_horizon
    ON trading_label_rows (label_kind, label_horizon_bars);
CREATE INDEX ix_label_rows_emitted
    ON trading_label_rows (label_emitted_at DESC);
```

### Materializers

Three idempotent jobs synthesize rows from existing data:

1. **`materialize_features_from_snapshots`** — daily 03:00 PT.
   Reads recent `MarketSnapshot` rows, joins to regime/breadth/cross-asset
   snapshots, joins to `trading_execution_cost_estimates` for
   microstructure, writes `trading_feature_rows` rows. Idempotent on
   `(ticker, asset_class, bar_interval, bar_start_at, feature_schema_version)`.

2. **`materialize_labels_forward_return`** — daily 03:30 PT.
   For each feature row at `bar_start_at = T`, reads `MarketSnapshot`
   row at `T + horizon_bars` and computes forward return. Skips rows
   where the future bar doesn't exist yet (the natural label-lag
   gate).

3. **`materialize_labels_realized`** — hourly.
   For each closed `Trade` row, locates the feature row at
   `(trade.ticker, trade.entry_date)`, writes a
   `realized_trade_outcome` label row that joins
   `Trade.pnl / exit_price / tca_*_slippage_bps` and
   `trading_venue_truth_log` (if matched).

### Reads

- Training (`PatternMetaLearner`): switch from `MarketSnapshot` direct
  reads to `trading_feature_rows` joined against
  `trading_label_rows[label_kind='triple_barrier']` once enough rows
  accumulate. Behind a feature flag for parallel-run validation.
- CPCV: read `trading_label_rows[label_kind='realized_trade_outcome']`
  for promoted patterns. The CPCV gate is already built; it just
  changes its source.
- Drift monitor: read both snapshot-side feature rows AND realized-
  label rows; compute matched-baseline divergence on the same row
  identity.
- Post-trade attribution: every closed trade has a feature row +
  realized-label row; attribution becomes a simple JOIN.

## Consequences

### Good

- One row identity for an event, not five. Training, validation,
  promotion, and post-trade attribution all key off the same surface.
- Feature schema versioning is explicit (`feature_schema_version`
  starts at 2 to differentiate from the existing v1 in
  `trading_pattern_trades`). New context columns (regime, event flags,
  etc) get new schema versions, not silent column adds.
- Survivorship bias becomes addressable: the materializer reads
  historical universe membership when computing benchmark context.
  (Universe-membership table is a separate ADR but plugs in here.)
- CPCV evidence asymmetry resolves naturally: every pattern's
  promotion is judged against `trading_label_rows`, not a per-era
  heuristic.

### Bad

- New tables = new ops surface. Indexes, vacuum, partitioning concerns.
- Materializer jobs are eventually-consistent; readers need to handle
  "no row yet" cleanly. CPCV's existing `min_trades` gate already
  does this.
- Schema versioning needs discipline. Each `feature_schema_version`
  bump must be paired with a backfill plan or the consumer stays on
  the older version until backfill completes.

### Risk: parallel-data divergence during rollout

Until consumers cut over, the fragmented existing tables and the new
canonical rows will diverge. The audit explicitly warned against this.
Mitigation: the materializer pulls from the existing tables and writes
the canonical rows derivatively, so the canonical store can never be
*ahead* of the source. Cutting consumers over is one-way (we don't
edit rows after they land).

## Implementation phases

| # | Phase | Scope | Gate |
|---|---|---|---|
| 1 | Schema + migration | mig N+1 creates the two tables. No code reads/writes them. | always-on after merge |
| 2 | Materializer skeleton | jobs registered but flag-gated to no-op. | `chili_truth_layer_materialize_enabled` |
| 3 | Materializer fills tables | flag flips on; jobs populate from existing data. Read-only consumer. | flag stays the same; new readers default to source-of-truth tables |
| 4 | First consumer cutover (drift monitor) | drift_monitor_service reads canonical rows when flag is on. Behind its own flag. | `chili_truth_layer_drift_consumer` |
| 5 | Training cutover | `PatternMetaLearner` switches to canonical reads in shadow first; flag flips after parallel-run agreement. | `chili_truth_layer_training_authoritative` |
| 6 | CPCV cutover | promotion_gate reads canonical labels. Final consumer. | `chili_truth_layer_cpcv_authoritative` |

Each phase is independently rollback-able. No phase touches existing
hot paths (entry/exit submission, broker_sync) — only the
training/validation/attribution surfaces.

## Estimated effort

Phases 1–3 are about 6 weeks of solo-dev work (schema, materializers,
tests, ops monitoring). Phases 4–6 are 4–8 weeks each because they
require parallel-run validation and consumer-by-consumer cutover.
Total: 18–32 weeks at solo-dev pace, 3–4 months at the audit's notional
3-person team pace.

## Open questions for the operator

1. **Schema version 2 vs 3?** Pattern-trade features v1 has 11 fields.
   Should `trading_feature_rows.feature_schema_version=2` be a strict
   superset of v1 (so v1 is auto-promoted), or a clean break (start
   from regime + microstructure)?
2. **Event-flag source?** Earnings flags require an external feed
   (FinnHub / Polygon / etc) that CHILI doesn't currently subscribe
   to. Phase 1 can ship with empty event_flags_json and add later.
3. **Universe-membership table** is a sibling ADR (006?) but the
   feature_rows benchmark_context column needs it. Sequence: ship
   feature_rows with `benchmark_context_json` allowing nulls, fill
   later when universe table lands.
4. **Cutover sequence priority?** drift monitor first (lowest risk) is
   recommended above. Operator preference: training first (highest
   research leverage) or CPCV first (closes promotion-asymmetry
   faster)?
