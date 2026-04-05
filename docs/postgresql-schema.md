# PostgreSQL schema — trading & brain

Generated from SQLAlchemy models in `app/models/trading.py`, `app/models/trading_brain_phase1.py`, plus migration-only columns/tables noted below. Types reflect PostgreSQL equivalents.

---

## `trading_watchlist`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| ticker | VARCHAR(20) | no | |
| added_at | TIMESTAMP | no | default now |

**Indexes:** `id` (PK), `user_id`.

---

## `trading_trades`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| ticker | VARCHAR(20) | no | |
| direction | VARCHAR(10) | no | default `long` |
| entry_price | DOUBLE PRECISION | no | |
| exit_price | DOUBLE PRECISION | yes | |
| quantity | DOUBLE PRECISION | no | default 1 |
| entry_date | TIMESTAMP | no | |
| exit_date | TIMESTAMP | yes | |
| status | VARCHAR(20) | no | open / working / closed / … |
| pnl | DOUBLE PRECISION | yes | |
| tags | VARCHAR(500) | yes | |
| notes | TEXT | yes | |
| indicator_snapshot | JSONB | yes | |
| broker_source | VARCHAR(20) | yes | |
| broker_order_id | VARCHAR(100) | yes | |
| broker_status | VARCHAR(30) | yes | |
| last_broker_sync | TIMESTAMP | yes | |
| filled_at | TIMESTAMP | yes | |
| avg_fill_price | DOUBLE PRECISION | yes | |
| tca_reference_entry_price | DOUBLE PRECISION | yes | |
| tca_entry_slippage_bps | DOUBLE PRECISION | yes | |
| tca_reference_exit_price | DOUBLE PRECISION | yes | |
| tca_exit_slippage_bps | DOUBLE PRECISION | yes | |
| strategy_proposal_id | INTEGER | yes | `index` |
| scan_pattern_id | INTEGER | yes | `index` |
| pattern_tags | VARCHAR(500) | yes | |

**Foreign keys:** none declared on model (logical links to proposals / `scan_patterns`).

**Indexes:** PK; `user_id`; `strategy_proposal_id`; `scan_pattern_id`; migration composite `idx_trades_sp_status (scan_pattern_id, status)`.

---

## `trading_journal`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| trade_id | INTEGER | yes | `index` |
| user_id | INTEGER | yes | `index` |
| content | TEXT | no | |
| indicator_snapshot | TEXT | yes | |
| created_at | TIMESTAMP | no | |

**Indexes:** PK; `trade_id`; `user_id`.

---

## `trading_insights`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| scan_pattern_id | INTEGER | no | FK → `scan_patterns.id` ON DELETE RESTRICT, `index` |
| pattern_description | TEXT | no | |
| hypothesis_family | VARCHAR(32) | yes | |
| confidence | DOUBLE PRECISION | no | default 0.5 |
| evidence_count | INTEGER | no | default 1 |
| win_count | INTEGER | no | default 0 |
| loss_count | INTEGER | no | default 0 |
| last_seen | TIMESTAMP | no | |
| created_at | TIMESTAMP | no | |
| active | BOOLEAN | no | default true |

**Foreign keys:** `scan_pattern_id` → `scan_patterns.id` (RESTRICT).

**Indexes:** PK; `user_id`; `scan_pattern_id`; partial `idx_insights_sp_id` (migration).

---

## `trading_scans`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| ticker | VARCHAR(20) | no | `index` |
| score | DOUBLE PRECISION | no | |
| signal | VARCHAR(10) | no | |
| entry_price | DOUBLE PRECISION | yes | |
| stop_loss | DOUBLE PRECISION | yes | |
| take_profit | DOUBLE PRECISION | yes | |
| risk_level | VARCHAR(10) | no | |
| rationale | TEXT | no | |
| indicator_data | JSONB | yes | |
| scanned_at | TIMESTAMP | no | |

**Indexes:** PK; `user_id`; `ticker`.

---

## `trading_backtests`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| ticker | VARCHAR(20) | no | |
| strategy_name | VARCHAR(100) | no | |
| params | JSONB | yes | |
| return_pct | DOUBLE PRECISION | no | |
| win_rate | DOUBLE PRECISION | no | |
| sharpe | DOUBLE PRECISION | yes | |
| max_drawdown | DOUBLE PRECISION | no | |
| trade_count | INTEGER | no | |
| equity_curve | JSONB | yes | |
| ran_at | TIMESTAMP | no | |
| related_insight_id | INTEGER | yes | `index` |
| scan_pattern_id | INTEGER | yes | `index` |
| oos_win_rate | DOUBLE PRECISION | yes | |
| oos_return_pct | DOUBLE PRECISION | yes | |
| oos_trade_count | INTEGER | yes | |
| oos_holdout_fraction | DOUBLE PRECISION | yes | |
| in_sample_bars | INTEGER | yes | |
| out_of_sample_bars | INTEGER | yes | |
| archived_at | TIMESTAMP | yes | migration 067 (soft retention); not on ORM |

**Indexes:** PK; `user_id`; `related_insight_id`; `scan_pattern_id`; `idx_bt_sp_id_ran_at (scan_pattern_id, ran_at DESC)` (migration).

---

## `trading_snapshots`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| ticker | VARCHAR(20) | no | `index` |
| snapshot_date | TIMESTAMP | no | `index` |
| close_price | DOUBLE PRECISION | no | |
| indicator_data | JSONB | yes | |
| predicted_score | DOUBLE PRECISION | yes | |
| vix_at_snapshot | DOUBLE PRECISION | yes | |
| future_return_1d … 10d | DOUBLE PRECISION | yes | |
| news_sentiment | DOUBLE PRECISION | yes | |
| news_count | INTEGER | yes | |
| pe_ratio | DOUBLE PRECISION | yes | |
| market_cap_b | DOUBLE PRECISION | yes | |
| bar_interval | VARCHAR(16) | yes | `index` |
| bar_start_at | TIMESTAMP | yes | `index` |
| snapshot_legacy | BOOLEAN | no | default true |
| archived_at | TIMESTAMP | yes | migration 067; not on ORM |

**Indexes:** PK; `ticker`; `snapshot_date`; `(bar_interval, bar_start_at)`; unique bar key `ix_trading_snapshots_bar_key (ticker, bar_interval, bar_start_at)` (migration); `idx_snapshots_date_ticker`.

---

## `trading_insight_evidence`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| insight_id | INTEGER | no | FK → `trading_insights.id` CASCADE, `index` |
| ticker | VARCHAR(20) | no | |
| bar_interval | VARCHAR(16) | no | |
| bar_start_utc | TIMESTAMP | no | |
| source | VARCHAR(24) | no | |
| created_at | TIMESTAMP | no | |

**Foreign keys:** `insight_id` → `trading_insights.id` (CASCADE).

**Indexes:** PK; `insight_id`; `ix_tie_insight_id` (migration).

---

## `trading_learning_events`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| event_type | VARCHAR(30) | no | |
| description | TEXT | no | |
| confidence_before | DOUBLE PRECISION | yes | |
| confidence_after | DOUBLE PRECISION | yes | |
| related_insight_id | INTEGER | yes | |
| created_at | TIMESTAMP | no | |

**Indexes:** PK; `user_id`; `idx_learning_events_created` (migration).

---

## `trading_alerts`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| alert_type | VARCHAR(30) | no | |
| ticker | VARCHAR(20) | yes | |
| message | TEXT | no | |
| trade_type | VARCHAR(30) | yes | |
| duration_estimate | VARCHAR(60) | yes | |
| scan_pattern_id | INTEGER | yes | `index` |
| sent_via | VARCHAR(20) | no | |
| success | BOOLEAN | no | |
| created_at | TIMESTAMP | no | |

**Indexes:** PK; `user_id`; `scan_pattern_id`. (Migration batch may define `idx_alerts_status_created` on DBs that include a `status` column.)

---

## `trading_breakout_alerts`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| ticker | VARCHAR(20) | no | `index` |
| asset_type | VARCHAR(10) | no | |
| alert_tier | VARCHAR(50) | no | |
| score_at_alert | DOUBLE PRECISION | no | |
| indicator_snapshot | JSONB | yes | |
| price_at_alert | DOUBLE PRECISION | no | |
| entry_price … optimal_exit_pct | various | yes | exit optimization |
| regime_at_alert … news_sentiment_at_alert | various | yes | context |
| alerted_at | TIMESTAMP | no | `index` |
| outcome fields | various | | |
| user_id | INTEGER | yes | `index` |
| scan_pattern_id | INTEGER | yes | `index` |
| related_insight_id | INTEGER | yes | `index` |
| scan_cycle_id | VARCHAR(40) | yes | `index` |

**Indexes:** PK; `ticker`; `alerted_at`; `scan_cycle_id`; `ix_breakout_scan_cycle`; per-column indexes from migration 062+.

---

## `trading_proposals`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| ticker | VARCHAR(20) | no | `index` |
| direction | VARCHAR(10) | no | |
| status | VARCHAR(20) | no | |
| entry_price, stop_loss, take_profit | DOUBLE PRECISION | no | |
| quantity, position_size_pct | DOUBLE PRECISION | yes | |
| projected_profit_pct, projected_loss_pct, risk_reward_ratio, confidence | DOUBLE PRECISION | no | |
| timeframe | VARCHAR(30) | no | |
| thesis | TEXT | no | |
| signals_json, indicator_json | JSONB | yes | |
| brain_score, ml_probability, scan_score | DOUBLE PRECISION | yes | |
| proposed_at … expires_at | TIMESTAMP | various | |
| broker_order_id | VARCHAR(100) | yes | |
| trade_id | INTEGER | yes | |
| scan_pattern_id | INTEGER | yes | `index` |

**Indexes:** PK; `user_id`; `ticker`; `scan_pattern_id`.

---

## `scan_patterns`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| name | VARCHAR(120) | no | |
| description | TEXT | yes | |
| rules_json | JSONB | no | |
| origin | VARCHAR(30) | no | |
| asset_class | VARCHAR(20) | no | |
| timeframe | VARCHAR(10) | no | |
| confidence | DOUBLE PRECISION | no | |
| evidence_count | INTEGER | no | |
| win_rate | DOUBLE PRECISION | yes | |
| avg_return_pct | DOUBLE PRECISION | yes | |
| backtest_count | INTEGER | no | |
| score_boost, min_base_score | DOUBLE PRECISION | no | |
| active | BOOLEAN | no | |
| parent_id | INTEGER | yes | `index` |
| exit_config | JSONB | yes | |
| variant_label | VARCHAR(40) | yes | |
| generation | INTEGER | no | |
| ticker_scope | VARCHAR(20) | no | |
| scope_tickers | TEXT | yes | |
| trade_count | INTEGER | no | |
| backtest_priority | INTEGER | no | |
| last_backtest_at | TIMESTAMP | yes | |
| created_at, updated_at | TIMESTAMP | no | |
| promotion_status | VARCHAR(32) | no | |
| oos_* | various | yes | OOS / promotion metrics |
| bench_walk_forward_json | JSONB | yes | |
| hypothesis_family | VARCHAR(32) | yes | |
| oos_validation_json, paper_book_json | JSONB | no | defaults `{}` |
| queue_tier | VARCHAR(16) | no | |
| lifecycle_stage | VARCHAR(20) | no | FSM: candidate → … → retired |
| lifecycle_changed_at | TIMESTAMP | yes | |
| user_id | INTEGER | yes | FK → `users.id` SET NULL, `index` |

**Foreign keys:** `user_id` → `users.id` (SET NULL).

**Indexes:** PK; `parent_id`; `user_id`; `idx_sp_active_lifecycle`; `idx_sp_origin_active`; partial unique `uq_sp_name_origin_active (name, origin) WHERE active` (migration).

---

## `trading_pattern_trades`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| scan_pattern_id | INTEGER | yes | `index` |
| related_insight_id | INTEGER | yes | `index` |
| backtest_result_id | INTEGER | yes | `index` |
| ticker | VARCHAR(20) | no | `index` |
| as_of_ts | TIMESTAMP | no | `index` |
| timeframe | VARCHAR(10) | no | |
| asset_class | VARCHAR(20) | no | |
| fwd_ret_* , mfe_pct, mae_pct, hold_bars, r_multiple, outcome_return_pct | DOUBLE PRECISION / INTEGER | yes | |
| label_win | BOOLEAN | yes | |
| features_json | JSONB | no | |
| source | VARCHAR(40) | no | |
| feature_schema_version | VARCHAR(20) | no | |
| code_version | VARCHAR(40) | yes | |
| created_at | TIMESTAMP | no | |

**Indexes:** PK; `user_id`; `scan_pattern_id`; `ticker`; `as_of_ts`; `ix_ptt_pattern_asof`; `ix_ptt_ticker_asof`; `ix_ptt_pattern_ticker`.

---

## `trading_pattern_evidence_hypotheses`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| scan_pattern_id | INTEGER | no | `index` |
| title | VARCHAR(200) | no | |
| predicate_json | JSONB | no | |
| status | VARCHAR(20) | no | |
| metrics_json | JSONB | no | |
| created_at, updated_at | TIMESTAMP | no | |

**Indexes:** PK; `ix_peh_pattern`.

---

## `trading_learning_cycle_ai_reports`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| user_id | INTEGER | yes | `index` |
| created_at | TIMESTAMP | no | |
| content | TEXT | no | |
| metrics_json | JSONB | no | |

**Indexes:** PK; `user_id`; `ix_tlcai_user_created (user_id, created_at DESC)`.

---

## `trading_hypotheses`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| description | TEXT | no | |
| condition_a, condition_b | TEXT | no | |
| expected_winner | VARCHAR(5) | no | |
| origin | VARCHAR(30) | no | |
| status | VARCHAR(20) | no | |
| times_tested, times_confirmed, times_rejected | INTEGER | no | |
| last_result_json | JSONB | yes | |
| related_weight | VARCHAR(80) | yes | |
| related_pattern_id | INTEGER | yes | |
| created_at | TIMESTAMP | no | |
| last_tested_at | TIMESTAMP | yes | |

**Indexes:** PK.

---

## `trading_prescreen_snapshots`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | autoincrement |
| run_id | VARCHAR(64) | no | unique, `index` |
| run_started_at | TIMESTAMP | no | |
| run_finished_at | TIMESTAMP | yes | |
| timezone_label | VARCHAR(64) | no | |
| settings_json, status_json, source_map_json, inclusion_summary_json | JSONB | yes | |
| candidate_count | INTEGER | no | |

**Indexes:** PK; `run_id` unique; `ix_tps_run_started`.

---

## `trading_prescreen_candidates`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | autoincrement |
| snapshot_id | INTEGER | yes | FK → `trading_prescreen_snapshots.id` SET NULL, `index` |
| user_id | INTEGER | yes | FK → `users.id` CASCADE, `index` |
| ticker | VARCHAR(32) | no | |
| ticker_norm | VARCHAR(36) | no | `index` |
| asset_universe | VARCHAR(16) | no | |
| active | BOOLEAN | no | |
| first_seen_at, last_seen_at, modified_at | TIMESTAMP | no | |
| entry_reasons | JSONB | no | |
| sources_json | JSONB | yes | |

**Foreign keys:** `snapshot_id` → `trading_prescreen_snapshots`; `user_id` → `users`.

**Indexes:** PK; `snapshot_id`; `user_id`; `ticker_norm`; unique partial `uq_trading_prescreen_candidate_global` / `_user` (migration); `ix_tpc_active_global_norm`.

---

## `trading_paper_trades`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | |
| user_id | INTEGER | yes | `index` |
| scan_pattern_id | INTEGER | yes | `index` |
| ticker | VARCHAR(32) | no | |
| direction | VARCHAR(8) | no | |
| entry_price | DOUBLE PRECISION | no | |
| stop_price, target_price | DOUBLE PRECISION | yes | |
| quantity | INTEGER | no | |
| status | VARCHAR(16) | no | |
| entry_date | TIMESTAMP | no | |
| exit_date | TIMESTAMP | yes | |
| exit_price | DOUBLE PRECISION | yes | |
| exit_reason | VARCHAR(32) | yes | |
| pnl, pnl_pct | DOUBLE PRECISION | yes | |
| signal_json | JSONB | yes | |
| created_at | TIMESTAMP | no | |

**Indexes:** PK; `ix_paper_trades_user`; `ix_paper_trades_status`; `ix_paper_trades_pattern`; `idx_paper_trades_sp_status` (migration).

---

## `brain_batch_jobs`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | VARCHAR(36) | PK | |
| job_type | VARCHAR(64) | no | `index` |
| status | VARCHAR(24) | no | default `running`; check constraint in migration |
| started_at | TIMESTAMP | no | |
| ended_at | TIMESTAMP | yes | |
| error_message | TEXT | yes | |
| meta_json | JSONB | yes | |
| payload_json | JSONB | yes | migration 061 |
| user_id | INTEGER | yes | FK → `users.id` SET NULL |
| archived_at | TIMESTAMP | yes | migration 067; not on ORM |
| batch_key | VARCHAR | yes | if present in DB; partial unique index `uq_bbj_running` uses `(job_type, batch_key) WHERE status = 'running'` (migration) |

**Foreign keys:** `user_id` → `users.id` (SET NULL).

**Indexes:** PK; `ix_brain_batch_jobs_type_started`; `idx_batch_jobs_status_started`.

---

## Brain orchestration — `app/models/trading_brain_phase1.py`

### `brain_learning_cycle_run`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| correlation_id | VARCHAR(64) | no | `index` |
| universe_id | VARCHAR(64) | yes | `index` |
| status | VARCHAR(24) | no | |
| started_at, finished_at | TIMESTAMP | yes | |
| meta_json | JSONB | no | |
| created_at | TIMESTAMP | no | |

**Indexes:** PK; `ix_brain_lcr_correlation_id`; `ix_brain_lcr_universe_id`.

### `brain_stage_job`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | INTEGER | PK | `index` |
| cycle_run_id | INTEGER | no | FK → `brain_learning_cycle_run.id` CASCADE, `index` |
| stage_key | VARCHAR(64) | no | |
| ordinal | INTEGER | no | |
| status | VARCHAR(24) | no | |
| attempt | INTEGER | no | |
| lease_until | TIMESTAMP | yes | |
| worker_id | VARCHAR(128) | yes | |
| input_artifact_refs, output_artifact_refs | JSONB | no | |
| error_detail | TEXT | yes | |
| skip_reason | VARCHAR(255) | yes | |
| started_at, finished_at | TIMESTAMP | yes | |

**Foreign keys:** `cycle_run_id` → `brain_learning_cycle_run.id` (CASCADE).

**Indexes:** PK; `ix_brain_stage_job_cycle_run_id`.

### `brain_cycle_lease`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| scope_key | VARCHAR(64) | PK | |
| cycle_run_id | INTEGER | yes | FK → `brain_learning_cycle_run.id` SET NULL |
| holder_id | VARCHAR(128) | no | |
| acquired_at, expires_at | TIMESTAMP | yes | |

**Foreign keys:** `cycle_run_id` → `brain_learning_cycle_run.id` (SET NULL).

### `brain_prediction_snapshot`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | BIGINT | PK | autoincrement |
| as_of_ts | TIMESTAMP | no | |
| universe_fingerprint | VARCHAR(64) | no | `index` |
| ticker_count | INTEGER | no | |
| source_tag | VARCHAR(64) | no | |
| correlation_id | VARCHAR(40) | no | |

**Relationship:** one-to-many `brain_prediction_line`.

**Indexes:** PK; `ix_brain_prediction_snapshot_universe_fp`.

### `brain_prediction_line`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| id | BIGINT | PK | autoincrement |
| snapshot_id | BIGINT | no | FK → `brain_prediction_snapshot.id` CASCADE, `index` |
| sort_rank | INTEGER | no | |
| ticker | VARCHAR(32) | no | |
| score | DOUBLE PRECISION | no | |
| confidence | INTEGER | yes | |
| direction | VARCHAR(32) | yes | |
| price | DOUBLE PRECISION | yes | |
| meta_ml_probability | DOUBLE PRECISION | yes | |
| vix_regime | VARCHAR(32) | yes | |
| signals_json, matched_patterns_json | JSONB | no | |
| suggested_stop, suggested_target, risk_reward, position_size_pct | DOUBLE PRECISION | yes | |

**Foreign keys:** `snapshot_id` → `brain_prediction_snapshot.id` (CASCADE).

**Indexes:** PK; `ix_brain_prediction_line_snapshot_id`.

### `brain_integration_event`

| Column | Type | Nullable | Notes |
|--------|------|----------|--------|
| idempotency_key | VARCHAR(256) | PK | |
| event_id | VARCHAR(64) | no | |
| event_type | VARCHAR(64) | no | |
| payload_hash | VARCHAR(128) | no | |
| payload_json | JSONB | no | |
| received_at | TIMESTAMP | no | |
| processed_at | TIMESTAMP | yes | |
| status | VARCHAR(24) | no | |

---

## Migration-only trading tables (no SQLAlchemy model in `trading.py`)

Defined in migration `069_supporting_tables` (`app/migrations.py`).

### `trading_risk_state`

| Column | Type | Nullable |
|--------|------|----------|
| id | SERIAL PK | no |
| user_id | INTEGER | yes |
| snapshot_date | DATE | no |
| open_positions | INTEGER | no |
| total_heat_pct | FLOAT | no |
| breaker_tripped | BOOLEAN | no |
| breaker_reason | VARCHAR(256) | yes |
| capital | FLOAT | no |
| regime | VARCHAR(32) | yes |
| created_at | TIMESTAMP | no |

**Index:** `ix_risk_state_user_date (user_id, snapshot_date DESC)`.

### `trading_brain_performance_daily`

| Column | Type | Nullable |
|--------|------|----------|
| id | SERIAL PK | no |
| user_id | INTEGER | yes |
| perf_date | DATE | no |
| total_pnl, trade_count, win_count, loss_count | numeric | no |
| win_rate, avg_pnl, max_win, max_loss | FLOAT | yes |
| patterns_active, patterns_promoted, signals_generated | INTEGER | yes |
| created_at | TIMESTAMP | no |

**Indexes:** unique `(user_id, perf_date)`; `uix_perf_daily_user_date`.

### `trading_daily_playbooks`

| Column | Type | Nullable |
|--------|------|----------|
| id | SERIAL PK | no |
| user_id | INTEGER | yes |
| playbook_date | DATE | no |
| regime | VARCHAR(32) | yes |
| regime_guidance | TEXT | yes |
| max_new_trades | INTEGER | yes |
| ideas_json, watchlist_json, risk_snapshot_json, performance_json | JSONB | yes |
| created_at | TIMESTAMP | no |

**Indexes:** unique `(user_id, playbook_date)`; `ix_playbooks_user_date`.

### `trading_ml_model_versions`

| Column | Type | Nullable |
|--------|------|----------|
| id | SERIAL PK | no |
| version_id | VARCHAR(128) | no UNIQUE |
| model_type | VARCHAR(64) | no |
| trained_at | TIMESTAMP | no |
| is_active, is_shadow | BOOLEAN | no |
| metrics_json | JSONB | yes |
| file_path | VARCHAR(512) | yes |
| parent_version | VARCHAR(128) | yes |
| notes | TEXT | yes |
| created_at | TIMESTAMP | no |

**Index:** `ix_ml_versions_type_active (model_type, is_active)`.

---

## Operational notes

- **Diagnostics:** `GET /api/trading/brain/data-health` aggregates integrity counts (orphan backtests, invalid win rates, stuck batch jobs, pattern lifecycle counts, row totals).
- **Drift:** If a column appears in migrations but not in the ORM (e.g. `archived_at`, `batch_key`), treat this doc as “intended schema”; verify live DB with `\d table` or information_schema when debugging.
