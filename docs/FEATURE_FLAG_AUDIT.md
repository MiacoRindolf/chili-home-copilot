# FEATURE_FLAG_AUDIT — Q1.T8

Source inventory: `audit_readonly_inventory.md` (721 fields at generation time; this doc uses live `Settings.model_fields`, **723** fields in this checkout).

**Operator overrides (Q1.T8):** `chili_cpcv_promotion_gate_enabled` → Category **2** (runbook readiness; **not** flipped). `chili_unified_signal_enabled`, `chili_regime_classifier_enabled`, `chili_cpcv_weekly_backfill_enabled` → Category **1**. `chili_regime_force_cold_fit` → Category **2** (quarterly one-shot).

## Categories

| Cat | Meaning |
|-----|---------|
| **1** | Diagnostic-safe-to-flip — shadow/telemetry, additive DB, no money enforcement. |
| **2** | Enforcement-gated — trading, sizing, lifecycle, live routing; runbook review. |
| **3** | Infrastructure — URLs, pools, scheduler tuning, numeric limits, LLM routing. |
| **4** | Unknown / needs investigation — stale docs or narrow consumer. |

## Category 1 — Diagnostic-safe (action list for default flip)

| Flag | Env alias(es) | Default | Config | What / why | Shadow semantics | Rollout doc |
|------|---------------|---------|--------|------------|------------------|-------------|
| `brain_prediction_ops_log_enabled` | `BRAIN_PREDICTION_OPS_LOG_ENABLED` | True | `app/config.py:217` | Mirror ops log prefix `[chili_prediction_ops]`; telemetry for rollout reviews. | — | docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md |
| `chili_cpcv_weekly_backfill_enabled` | `CHILI_CPCV_WEEKLY_BACKFILL_ENABLED` | True | `app/config.py:812` | Registers weekly CPCV backfill job; diagnostic accumulation on canonical DB. | — | docs/CPCV_PROMOTION_GATE_RUNBOOK.md |
| `chili_regime_classifier_enabled` | `CHILI_REGIME_CLASSIFIER_ENABLED` | True | `app/config.py:816` | Regime tags + regime_snapshot; /brain heatmap early-return only. | — | docs/REGIME_CLASSIFIER_RUNBOOK.md |
| `chili_unified_signal_enabled` | `CHILI_UNIFIED_SIGNAL_ENABLED` | True | `app/config.py:814` | Additive INSERTs to unified_signals; no consumer enforces yet. | — | docs/ROADMAP_DEVIATION_005.md (context) |

## Category 2 — Enforcement-gated

| Flag | Env alias(es) | Default | Config | What / why | Shadow | Rollout doc |
|------|---------------|---------|--------|------------|--------|-------------|
| `brain_allocator_live_hard_block_enabled` | `BRAIN_ALLOCATOR_LIVE_HARD_BLOCK_ENABLED` | True | `app/config.py:2129` | Matches enforcement/live-routing heuristic `'live_hard_block'` — review consumer before diagnostic flip. | — | — |
| `brain_allocator_live_soft_block_enabled` | `BRAIN_ALLOCATOR_LIVE_SOFT_BLOCK_ENABLED` | False | `app/config.py:2128` | Matches enforcement/live-routing heuristic `'live_soft_block'` — review consumer before diagnostic flip. | — | — |
| `brain_bench_walk_forward_enabled` | `BRAIN_BENCH_WALK_FORWARD_ENABLED` | True | `app/config.py:1986` | Matches enforcement/live-routing heuristic `'walk_forward_enabled'` — review consumer before diagnostic flip. | — | — |
| `brain_capacity_hard_block_live` | `CHILI_BRAIN_CAPACITY_HARD_BLOCK_LIVE` | True | `app/config.py:2155` | Matches enforcement/live-routing heuristic `'hard_block_live'` — review consumer before diagnostic flip. | — | — |
| `brain_cycle_lease_enforcement_enabled` | `BRAIN_CYCLE_LEASE_ENFORCEMENT_ENABLED` | False | `app/config.py:189` | Matches enforcement/live-routing heuristic `'cycle_lease_enforcement'` — review consumer before diagnostic flip. | — | — |
| `brain_execution_robustness_hard_block_live_enabled` | `BRAIN_EXECUTION_ROBUSTNESS_HARD_BLOCK_LIVE_ENABLED` | False | `app/config.py:2094` | Matches enforcement/live-routing heuristic `'hard_block_live'` — review consumer before diagnostic flip. | — | — |
| `brain_execution_robustness_v2_hard_block_live_enabled` | `BRAIN_EXECUTION_ROBUSTNESS_V2_HARD_BLOCK_LIVE_ENABLED` | False | `app/config.py:2098` | Matches enforcement/live-routing heuristic `'hard_block_live'` — review consumer before diagnostic flip. | — | — |
| `brain_live_depromotion_enabled` | `BRAIN_LIVE_DEPROMOTION_ENABLED` | True | `app/config.py:2055` | Matches enforcement/live-routing heuristic `'depromotion'` — review consumer before diagnostic flip. | — | — |
| `brain_live_depromotion_max_gap_pct` | `BRAIN_LIVE_DEPROMOTION_MAX_GAP_PCT` | 25.0 | `app/config.py:2057` | Matches enforcement/live-routing heuristic `'depromotion'` — review consumer before diagnostic flip. | — | — |
| `brain_live_depromotion_min_closed_trades` | `BRAIN_LIVE_DEPROMOTION_MIN_CLOSED_TRADES` | 8 | `app/config.py:2056` | Matches enforcement/live-routing heuristic `'depromotion'` — review consumer before diagnostic flip. | — | — |
| `brain_live_drift_auto_challenged_enabled` | `BRAIN_LIVE_DRIFT_AUTO_CHALLENGED_ENABLED` | True | `app/config.py:2074` | Matches enforcement/live-routing heuristic `'auto_challenged'` — review consumer before diagnostic flip. | — | — |
| `brain_live_drift_auto_challenged_max_p_like` | `BRAIN_LIVE_DRIFT_AUTO_CHALLENGED_MAX_P_LIKE` | 0.02 | `app/config.py:2075` | Matches enforcement/live-routing heuristic `'auto_challenged'` — review consumer before diagnostic flip. | — | — |
| `brain_oos_gate_enabled` | `BRAIN_OOS_GATE_ENABLED` | True | `app/config.py:1960` | Matches enforcement/live-routing heuristic `'oos_gate_enabled'` — review consumer before diagnostic flip. | — | — |
| `brain_pattern_regime_autopilot_enabled` | `BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED` | False | `app/config.py:700` | Matches enforcement/live-routing heuristic `'pattern_regime_autopilot_enabled'` — review consumer before diagnostic flip. | — | — |
| `brain_pattern_regime_killswitch_kill` | `BRAIN_PATTERN_REGIME_KILLSWITCH_KILL` | False | `app/config.py:679` | Matches enforcement/live-routing heuristic `'killswitch_kill'` — review consumer before diagnostic flip. | — | — |
| `brain_prediction_dual_write_enabled` | `BRAIN_PREDICTION_DUAL_WRITE_ENABLED` | False | `app/config.py:200` | Prediction-mirror phase flag (ADR-004); progressive rollout — not blanket diagnostic. | — | docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md |
| `brain_prediction_read_authoritative_enabled` | `BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED` | False | `app/config.py:208` | Matches enforcement/live-routing heuristic `'read_authoritative'` — review consumer before diagnostic flip. | — | docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md |
| `brain_prediction_read_compare_enabled` | `BRAIN_PREDICTION_READ_COMPARE_ENABLED` | False | `app/config.py:204` | Prediction-mirror phase flag (ADR-004); progressive rollout — not blanket diagnostic. | — | docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md |
| `brain_work_delegate_queue_from_cycle` | `BRAIN_WORK_DELEGATE_QUEUE_FROM_CYCLE` | False | `app/config.py:244` | Matches enforcement/live-routing heuristic `'delegate_queue_from_cycle'` — review consumer before diagnostic flip. | — | — |
| `chili_autopilot_price_bus_enabled` | `CHILI_AUTOPILOT_PRICE_BUS_ENABLED` | False | `app/config.py:1004` | Matches enforcement/live-routing heuristic `'autopilot_price_bus'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_allow_extended_hours` | `CHILI_AUTOTRADER_ALLOW_EXTENDED_HOURS` | False | `app/config.py:1892` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_assumed_capital_usd` | `CHILI_AUTOTRADER_ASSUMED_CAPITAL_USD` | 25000.0 | `app/config.py:1906` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_broker_equity_cache_enabled` | `CHILI_AUTOTRADER_BROKER_EQUITY_CACHE_ENABLED` | False | `app/config.py:1927` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_broker_equity_cache_max_stale_seconds` | `CHILI_AUTOTRADER_BROKER_EQUITY_CACHE_MAX_STALE_SECONDS` | 900 | `app/config.py:1937` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_broker_equity_cache_ttl_seconds` | `CHILI_AUTOTRADER_BROKER_EQUITY_CACHE_TTL_SECONDS` | 300 | `app/config.py:1931` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_confidence_floor` | `CHILI_AUTOTRADER_CONFIDENCE_FLOOR` | 0.7 | `app/config.py:1754` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_daily_loss_cap_usd` | `CHILI_AUTOTRADER_DAILY_LOSS_CAP_USD` | 150.0 | `app/config.py:1743` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_enabled` | `CHILI_AUTOTRADER_ENABLED` | False | `app/config.py:1717` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_live_enabled` | `CHILI_AUTOTRADER_LIVE_ENABLED` | True | `app/config.py:1721` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_llm_revalidation_enabled` | `CHILI_AUTOTRADER_LLM_REVALIDATION_ENABLED` | True | `app/config.py:1902` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_max_concurrent` | `CHILI_AUTOTRADER_MAX_CONCURRENT` | 3 | `app/config.py:1748` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_max_entry_slippage_pct` | `CHILI_AUTOTRADER_MAX_ENTRY_SLIPPAGE_PCT` | 1.0 | `app/config.py:1770` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_max_symbol_price_usd` | `CHILI_AUTOTRADER_MAX_SYMBOL_PRICE_USD` | 50.0 | `app/config.py:1765` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_min_projected_profit_pct` | `CHILI_AUTOTRADER_MIN_PROJECTED_PROFIT_PCT` | 12.0 | `app/config.py:1760` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_monitor_interval_seconds` | `CHILI_AUTOTRADER_MONITOR_INTERVAL_SECONDS` | 30 | `app/config.py:1776` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_per_trade_notional_usd` | `CHILI_AUTOTRADER_PER_TRADE_NOTIONAL_USD` | 300.0 | `app/config.py:1729` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_rth_only` | `CHILI_AUTOTRADER_RTH_ONLY` | True | `app/config.py:1888` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_synergy_enabled` | `CHILI_AUTOTRADER_SYNERGY_ENABLED` | False | `app/config.py:1739` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_synergy_scale_notional_usd` | `CHILI_AUTOTRADER_SYNERGY_SCALE_NOTIONAL_USD` | 150.0 | `app/config.py:1734` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_tick_interval_seconds` | `CHILI_AUTOTRADER_TICK_INTERVAL_SECONDS` | 10 | `app/config.py:1911` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_autotrader_user_id` | `CHILI_AUTOTRADER_USER_ID` | None | `app/config.py:1725` | Matches enforcement/live-routing heuristic `'autotrader'` — review consumer before diagnostic flip. | — | — |
| `chili_bracket_watchdog_enabled` | `CHILI_BRACKET_WATCHDOG_ENABLED` | False | `app/config.py:1464` | Matches enforcement/live-routing heuristic `'bracket_watchdog'` — review consumer before diagnostic flip. | — | — |
| `chili_bracket_watchdog_stale_after_sec` | `CHILI_BRACKET_WATCHDOG_STALE_AFTER_SEC` | 300 | `app/config.py:1468` | Matches enforcement/live-routing heuristic `'bracket_watchdog'` — review consumer before diagnostic flip. | — | — |
| `chili_bracket_writer_g2_enabled` | `CHILI_BRACKET_WRITER_G2_ENABLED` | True | `app/config.py:1876` | Matches enforcement/live-routing heuristic `'bracket_writer_g2'` — review consumer before diagnostic flip. | — | — |
| `chili_bracket_writer_g2_partial_fill_resize` | `CHILI_BRACKET_WRITER_G2_PARTIAL_FILL_RESIZE` | True | `app/config.py:1880` | Matches enforcement/live-routing heuristic `'bracket_writer_g2'` — review consumer before diagnostic flip. | — | — |
| `chili_bracket_writer_g2_place_missing_stop` | `CHILI_BRACKET_WRITER_G2_PLACE_MISSING_STOP` | True | `app/config.py:1884` | Matches enforcement/live-routing heuristic `'bracket_writer_g2'` — review consumer before diagnostic flip. | — | — |
| `chili_coinbase_spot_adapter_enabled` | `CHILI_COINBASE_SPOT_ADAPTER_ENABLED` | True | `app/config.py:996` | Matches enforcement/live-routing heuristic `'coinbase_spot_adapter'` — review consumer before diagnostic flip. | — | — |
| `chili_coinbase_ws_enabled` | `CHILI_COINBASE_WS_ENABLED` | False | `app/config.py:1000` | Matches enforcement/live-routing heuristic `'coinbase_ws'` — review consumer before diagnostic flip. | — | — |
| `chili_cpcv_promotion_gate_enabled` | `CHILI_CPCV_PROMOTION_GATE_ENABLED` | False | `app/config.py:794` | HR1 promotion blocking; readiness in docs/CPCV_PROMOTION_GATE_RUNBOOK.md — do not flip. | — | docs/CPCV_PROMOTION_GATE_RUNBOOK.md |
| `chili_drift_escalation_enabled` | `CHILI_DRIFT_ESCALATION_ENABLED` | False | `app/config.py:1850` | Matches enforcement/live-routing heuristic `'drift_escalation'` — review consumer before diagnostic flip. | — | — |
| `chili_drift_escalation_interval_seconds` | `CHILI_DRIFT_ESCALATION_INTERVAL_SECONDS` | 120 | `app/config.py:1854` | Matches enforcement/live-routing heuristic `'drift_escalation'` — review consumer before diagnostic flip. | — | — |
| `chili_drift_escalation_lookback_minutes` | `CHILI_DRIFT_ESCALATION_LOOKBACK_MINUTES` | 60 | `app/config.py:1866` | Matches enforcement/live-routing heuristic `'drift_escalation'` — review consumer before diagnostic flip. | — | — |
| `chili_drift_escalation_min_count` | `CHILI_DRIFT_ESCALATION_MIN_COUNT` | 5 | `app/config.py:1860` | Matches enforcement/live-routing heuristic `'drift_escalation'` — review consumer before diagnostic flip. | — | — |
| `chili_execution_event_lag_enabled` | `CHILI_EXECUTION_EVENT_LAG_ENABLED` | True | `app/config.py:1816` | Matches enforcement/live-routing heuristic `'execution_event_lag'` — review consumer before diagnostic flip. | — | — |
| `chili_execution_event_lag_error_p95_ms` | `CHILI_EXECUTION_EVENT_LAG_ERROR_P95_MS` | 60000.0 | `app/config.py:1838` | Matches enforcement/live-routing heuristic `'execution_event_lag'` — review consumer before diagnostic flip. | — | — |
| `chili_execution_event_lag_interval_seconds` | `CHILI_EXECUTION_EVENT_LAG_INTERVAL_SECONDS` | 60 | `app/config.py:1820` | Matches enforcement/live-routing heuristic `'execution_event_lag'` — review consumer before diagnostic flip. | — | — |
| `chili_execution_event_lag_lookback_seconds` | `CHILI_EXECUTION_EVENT_LAG_LOOKBACK_SECONDS` | 300 | `app/config.py:1826` | Matches enforcement/live-routing heuristic `'execution_event_lag'` — review consumer before diagnostic flip. | — | — |
| `chili_execution_event_lag_warn_p95_ms` | `CHILI_EXECUTION_EVENT_LAG_WARN_P95_MS` | 15000.0 | `app/config.py:1832` | Matches enforcement/live-routing heuristic `'execution_event_lag'` — review consumer before diagnostic flip. | — | — |
| `chili_feature_parity_enabled` | `CHILI_FEATURE_PARITY_ENABLED` | False | `app/config.py:1627` | Matches enforcement/live-routing heuristic `'feature_parity_enabled'` — review consumer before diagnostic flip. | — | — |
| `chili_mesh_plasticity_daily_budget` | `CHILI_MESH_PLASTICITY_DAILY_BUDGET` | 0.5 | `app/config.py:978` | Matches enforcement/live-routing heuristic `'mesh_plasticity'` — review consumer before diagnostic flip. | — | — |
| `chili_mesh_plasticity_drift_cap` | `CHILI_MESH_PLASTICITY_DRIFT_CAP` | 1.0 | `app/config.py:968` | Matches enforcement/live-routing heuristic `'mesh_plasticity'` — review consumer before diagnostic flip. | — | — |
| `chili_mesh_plasticity_dry_run` | `CHILI_MESH_PLASTICITY_DRY_RUN` | False | `app/config.py:961` | Matches enforcement/live-routing heuristic `'mesh_plasticity'` — review consumer before diagnostic flip. | — | — |
| `chili_mesh_plasticity_enabled` | `CHILI_MESH_PLASTICITY_ENABLED` | True | `app/config.py:957` | Matches enforcement/live-routing heuristic `'mesh_plasticity'` — review consumer before diagnostic flip. | — | — |
| `chili_mesh_plasticity_learning_rate` | `CHILI_MESH_PLASTICITY_LEARNING_RATE` | 0.05 | `app/config.py:973` | Matches enforcement/live-routing heuristic `'mesh_plasticity'` — review consumer before diagnostic flip. | — | — |
| `chili_mesh_plasticity_per_edge_cooldown_trades` | `CHILI_MESH_PLASTICITY_PER_EDGE_COOLDOWN_TRADES` | 5 | `app/config.py:983` | Matches enforcement/live-routing heuristic `'mesh_plasticity'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_entry_gates_enabled` | `CHILI_MOMENTUM_ENTRY_GATES_ENABLED` | True | `app/config.py:932` | Matches enforcement/live-routing heuristic `'momentum_entry_gates'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_live_runner_dev_tick_enabled` | `CHILI_MOMENTUM_LIVE_RUNNER_DEV_TICK_ENABLED` | False | `app/config.py:1693` | Matches enforcement/live-routing heuristic `'live_runner'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_live_runner_enabled` | `CHILI_MOMENTUM_LIVE_RUNNER_ENABLED` | False | `app/config.py:1685` | Matches enforcement/live-routing heuristic `'live_runner'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_live_runner_scheduler_enabled` | `CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED` | False | `app/config.py:1689` | Matches enforcement/live-routing heuristic `'live_runner'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_live_runner_scheduler_interval_minutes` | `CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_INTERVAL_MINUTES` | 2 | `app/config.py:1704` | Matches enforcement/live-routing heuristic `'live_runner'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_neural_enabled` | `CHILI_MOMENTUM_NEURAL_ENABLED` | True | `app/config.py:922` | Matches enforcement/live-routing heuristic `'momentum_neural_enabled'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_neural_feedback_enabled` | `CHILI_MOMENTUM_NEURAL_FEEDBACK_ENABLED` | True | `app/config.py:927` | Matches enforcement/live-routing heuristic `'momentum_neural_feedback'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_paper_runner_dev_tick_enabled` | `CHILI_MOMENTUM_PAPER_RUNNER_DEV_TICK_ENABLED` | False | `app/config.py:1679` | Matches enforcement/live-routing heuristic `'paper_runner'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_paper_runner_enabled` | `CHILI_MOMENTUM_PAPER_RUNNER_ENABLED` | False | `app/config.py:1671` | Matches enforcement/live-routing heuristic `'paper_runner'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_paper_runner_scheduler_enabled` | `CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_ENABLED` | False | `app/config.py:1675` | Matches enforcement/live-routing heuristic `'paper_runner'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_paper_runner_scheduler_interval_minutes` | `CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_INTERVAL_MINUTES` | 3 | `app/config.py:1698` | Matches enforcement/live-routing heuristic `'paper_runner'` — review consumer before diagnostic flip. | — | — |
| `chili_momentum_performance_sizing_enabled` | `CHILI_MOMENTUM_PERFORMANCE_SIZING_ENABLED` | True | `app/config.py:942` | Matches enforcement/live-routing heuristic `'momentum_performance_sizing'` — review consumer before diagnostic flip. | — | — |
| `chili_order_state_machine_enabled` | `CHILI_ORDER_STATE_MACHINE_ENABLED` | False | `app/config.py:1480` | Matches enforcement/live-routing heuristic `'order_state_machine'` — review consumer before diagnostic flip. | — | — |
| `chili_regime_force_cold_fit` | `CHILI_REGIME_FORCE_COLD_FIT` | False | `app/config.py:818` | Quarterly operator action; one-shot cold EM — default stays OFF. | — | — |
| `chili_robinhood_spot_adapter_enabled` | `CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED` | False | `app/config.py:990` | Matches enforcement/live-routing heuristic `'robinhood_spot_adapter'` — review consumer before diagnostic flip. | — | — |
| `chili_stuck_order_limit_timeout_seconds` | `CHILI_STUCK_ORDER_LIMIT_TIMEOUT_SECONDS` | 1800 | `app/config.py:1804` | Matches enforcement/live-routing heuristic `'stuck_order'` — review consumer before diagnostic flip. | — | — |
| `chili_stuck_order_market_timeout_seconds` | `CHILI_STUCK_ORDER_MARKET_TIMEOUT_SECONDS` | 300 | `app/config.py:1798` | Matches enforcement/live-routing heuristic `'stuck_order'` — review consumer before diagnostic flip. | — | — |
| `chili_stuck_order_watchdog_enabled` | `CHILI_STUCK_ORDER_WATCHDOG_ENABLED` | True | `app/config.py:1788` | Matches enforcement/live-routing heuristic `'stuck_order'` — review consumer before diagnostic flip. | — | — |
| `chili_stuck_order_watchdog_interval_seconds` | `CHILI_STUCK_ORDER_WATCHDOG_INTERVAL_SECONDS` | 60 | `app/config.py:1792` | Matches enforcement/live-routing heuristic `'stuck_order'` — review consumer before diagnostic flip. | — | — |
| `chili_trading_automation_hud_enabled` | `CHILI_TRADING_AUTOMATION_HUD_ENABLED` | True | `app/config.py:1019` | Matches enforcement/live-routing heuristic `'trading_automation_hud'` — review consumer before diagnostic flip. | — | — |
| `chili_venue_rate_limit_cb_burst` | `CHILI_VENUE_RATE_LIMIT_CB_BURST` | 5 | `app/config.py:1194` | Matches enforcement/live-routing heuristic `'venue_rate_limit'` — review consumer before diagnostic flip. | — | — |
| `chili_venue_rate_limit_cb_orders_per_sec` | `CHILI_VENUE_RATE_LIMIT_CB_ORDERS_PER_SEC` | 3.0 | `app/config.py:1188` | Matches enforcement/live-routing heuristic `'venue_rate_limit'` — review consumer before diagnostic flip. | — | — |
| `chili_venue_rate_limit_enabled` | `CHILI_VENUE_RATE_LIMIT_ENABLED` | True | `app/config.py:1200` | Matches enforcement/live-routing heuristic `'venue_rate_limit'` — review consumer before diagnostic flip. | — | — |
| `chili_venue_rate_limit_rh_burst` | `CHILI_VENUE_RATE_LIMIT_RH_BURST` | 5 | `app/config.py:1180` | Matches enforcement/live-routing heuristic `'venue_rate_limit'` — review consumer before diagnostic flip. | — | — |
| `chili_venue_rate_limit_rh_orders_per_min` | `CHILI_VENUE_RATE_LIMIT_RH_ORDERS_PER_MIN` | 20.0 | `app/config.py:1174` | Matches enforcement/live-routing heuristic `'venue_rate_limit'` — review consumer before diagnostic flip. | — | — |
| `chili_walk_forward_enabled` | `CHILI_WALK_FORWARD_ENABLED` | False | `app/config.py:1553` | Matches enforcement/live-routing heuristic `'walk_forward_enabled'` — review consumer before diagnostic flip. | — | — |

## Category 3 — Infrastructure / tuning

| Flag | Env alias(es) | Default | Config | What / why | Shadow | Rollout doc |
|------|---------------|---------|--------|------------|--------|-------------|
| `alerts_enabled` | `ALERTS_ENABLED` | True | `app/config.py:130` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `backtest_commission` | `BACKTEST_COMMISSION` | 0.001 | `app/config.py:1957` | Numeric tuning / limit; not a mode flag. | — | — |
| `backtest_spread` | `BACKTEST_SPREAD` | 0.0002 | `app/config.py:1956` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_allocator_enabled` | `BRAIN_ALLOCATOR_ENABLED` | True | `app/config.py:2126` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_allocator_incumbent_score_margin` | `BRAIN_ALLOCATOR_INCUMBENT_SCORE_MARGIN` | 0.08 | `app/config.py:2130` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_allocator_shadow_mode` | `BRAIN_ALLOCATOR_SHADOW_MODE` | False | `app/config.py:2127` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_backtest_parallel` | `BRAIN_BACKTEST_PARALLEL` | 18 | `app/config.py:227` | Infrastructure / connectivity / tuning bucket (`'parallel'`). | — | — |
| `brain_bar_quality_max_gap_bars` | `BRAIN_BAR_QUALITY_MAX_GAP_BARS` | 5 | `app/config.py:2105` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_bar_quality_strict` | `BRAIN_BAR_QUALITY_STRICT` | False | `app/config.py:2104` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_bench_cost_stress_commission_mult` | `BRAIN_BENCH_COST_STRESS_COMMISSION_MULT` | 1.5 | `app/config.py:1996` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_bench_cost_stress_spread_mult` | `BRAIN_BENCH_COST_STRESS_SPREAD_MULT` | 2.0 | `app/config.py:1995` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_bench_interval` | `BRAIN_BENCH_INTERVAL` | '1d' | `app/config.py:1990` | String / URL / identifier; infrastructure. | — | — |
| `brain_bench_min_bars_per_window` | `BRAIN_BENCH_MIN_BARS_PER_WINDOW` | 35 | `app/config.py:1992` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_bench_min_positive_fold_ratio` | `BRAIN_BENCH_MIN_POSITIVE_FOLD_RATIO` | 0.375 | `app/config.py:1993` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_bench_n_windows` | `BRAIN_BENCH_N_WINDOWS` | 8 | `app/config.py:1991` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_bench_period` | `BRAIN_BENCH_PERIOD` | '10y' | `app/config.py:1989` | String / URL / identifier; infrastructure. | — | — |
| `brain_bench_require_stress_pass` | `BRAIN_BENCH_REQUIRE_STRESS_PASS` | False | `app/config.py:1998` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_bench_tickers` | `BRAIN_BENCH_TICKERS` | 'SPY,QQQ' | `app/config.py:1988` | String / URL / identifier; infrastructure. | — | — |
| `brain_bench_walk_forward_gate_enabled` | `BRAIN_BENCH_WALK_FORWARD_GATE_ENABLED` | True | `app/config.py:1987` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_breadth_relstr_cron_hour` | `BRAIN_BREADTH_RELSTR_CRON_HOUR` | 6 | `app/config.py:465` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_breadth_relstr_cron_minute` | `BRAIN_BREADTH_RELSTR_CRON_MINUTE` | 45 | `app/config.py:466` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_breadth_relstr_lookback_days` | `BRAIN_BREADTH_RELSTR_LOOKBACK_DAYS` | 14 | `app/config.py:474` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_breadth_relstr_min_coverage_score` | `BRAIN_BREADTH_RELSTR_MIN_COVERAGE_SCORE` | 0.5 | `app/config.py:467` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_breadth_relstr_mode` | `BRAIN_BREADTH_RELSTR_MODE` | 'shadow' | `app/config.py:463` | String / URL / identifier; infrastructure. | — | — |
| `brain_breadth_relstr_ops_log_enabled` | `BRAIN_BREADTH_RELSTR_OPS_LOG_ENABLED` | True | `app/config.py:464` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_breadth_relstr_risk_off_ratio` | `BRAIN_BREADTH_RELSTR_RISK_OFF_RATIO` | 0.35 | `app/config.py:472` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_breadth_relstr_risk_on_ratio` | `BRAIN_BREADTH_RELSTR_RISK_ON_RATIO` | 0.65 | `app/config.py:471` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_breadth_relstr_strong_trend_threshold` | `BRAIN_BREADTH_RELSTR_STRONG_TREND_THRESHOLD` | 0.03 | `app/config.py:469` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_breadth_relstr_tilt_threshold` | `BRAIN_BREADTH_RELSTR_TILT_THRESHOLD` | 0.02 | `app/config.py:470` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_breadth_relstr_trend_up_threshold` | `BRAIN_BREADTH_RELSTR_TREND_UP_THRESHOLD` | 0.01 | `app/config.py:468` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_budget_miner_error_trip` | `BRAIN_BUDGET_MINER_ERROR_TRIP` | 5 | `app/config.py:1978` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_budget_miner_rows_per_cycle` | `BRAIN_BUDGET_MINER_ROWS_PER_CYCLE` | 100000 | `app/config.py:1976` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_budget_ohlcv_per_cycle` | `BRAIN_BUDGET_OHLCV_PER_CYCLE` | 280 | `app/config.py:1975` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_budget_pattern_injects_per_cycle` | `BRAIN_BUDGET_PATTERN_INJECTS_PER_CYCLE` | 32 | `app/config.py:1977` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_capacity_hard_block_paper` | `CHILI_BRAIN_CAPACITY_HARD_BLOCK_PAPER` | True | `app/config.py:2152` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_capital_reweight_cron_day_of_week` | `BRAIN_CAPITAL_REWEIGHT_CRON_DAY_OF_WEEK` | 'sun' | `app/config.py:398` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_capital_reweight_cron_hour` | `BRAIN_CAPITAL_REWEIGHT_CRON_HOUR` | 18 | `app/config.py:399` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_capital_reweight_lookback_days` | `BRAIN_CAPITAL_REWEIGHT_LOOKBACK_DAYS` | 14 | `app/config.py:400` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_capital_reweight_max_single_bucket_pct` | `BRAIN_CAPITAL_REWEIGHT_MAX_SINGLE_BUCKET_PCT` | 35.0 | `app/config.py:401` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_capital_reweight_mode` | `BRAIN_CAPITAL_REWEIGHT_MODE` | 'shadow' | `app/config.py:396` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_RISK_DIAL_ROLLOUT.md |
| `brain_capital_reweight_ops_log_enabled` | `BRAIN_CAPITAL_REWEIGHT_OPS_LOG_ENABLED` | True | `app/config.py:397` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_coinbase_universe_extra_cap` | `BRAIN_COINBASE_UNIVERSE_EXTRA_CAP` | 600 | `app/config.py:782` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_config_profile` | `BRAIN_CONFIG_PROFILE` | 'default' | `app/config.py:40` | Infrastructure / connectivity / tuning bucket (`'config_profile'`). | — | — |
| `brain_cross_asset_beta_window_days` | `BRAIN_CROSS_ASSET_BETA_WINDOW_DAYS` | 60 | `app/config.py:489` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_cross_asset_composite_min_agreement` | `BRAIN_CROSS_ASSET_COMPOSITE_MIN_AGREEMENT` | 2 | `app/config.py:490` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_cross_asset_cron_hour` | `BRAIN_CROSS_ASSET_CRON_HOUR` | 7 | `app/config.py:483` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_cross_asset_cron_minute` | `BRAIN_CROSS_ASSET_CRON_MINUTE` | 0 | `app/config.py:484` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_cross_asset_fast_lead_threshold` | `BRAIN_CROSS_ASSET_FAST_LEAD_THRESHOLD` | 0.01 | `app/config.py:486` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_cross_asset_lookback_days` | `BRAIN_CROSS_ASSET_LOOKBACK_DAYS` | 14 | `app/config.py:492` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_cross_asset_min_coverage_score` | `BRAIN_CROSS_ASSET_MIN_COVERAGE_SCORE` | 0.5 | `app/config.py:485` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_cross_asset_mode` | `BRAIN_CROSS_ASSET_MODE` | 'shadow' | `app/config.py:481` | String / URL / identifier; infrastructure. | — | — |
| `brain_cross_asset_ops_log_enabled` | `BRAIN_CROSS_ASSET_OPS_LOG_ENABLED` | True | `app/config.py:482` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_cross_asset_slow_lead_threshold` | `BRAIN_CROSS_ASSET_SLOW_LEAD_THRESHOLD` | 0.03 | `app/config.py:487` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_cross_asset_vix_percentile_shock` | `BRAIN_CROSS_ASSET_VIX_PERCENTILE_SHOCK` | 0.8 | `app/config.py:488` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_crypto_universe_max` | `BRAIN_CRYPTO_UNIVERSE_MAX` | 200 | `app/config.py:776` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_crypto_universe_min_volume_usd` | `BRAIN_CRYPTO_UNIVERSE_MIN_VOLUME_USD` | 0.0 | `app/config.py:784` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_daily_market_scan_scheduler_enabled` | `BRAIN_DAILY_MARKET_SCAN_SCHEDULER_ENABLED` | True | `app/config.py:2041` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_decision_packet_required_for_runners` | `CHILI_BRAIN_DECISION_PACKET_REQUIRED_FOR_RUNNERS` | True | `app/config.py:2134` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_default_user_id` | `CHILI_BRAIN_DEFAULT_USER_ID, BRAIN_DEFAULT_USER_ID` | None | `app/config.py:894` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_deployment_degrade_drawdown_pct` | `CHILI_BRAIN_DEPLOYMENT_DEGRADE_DRAWDOWN_PCT` | 8.0 | `app/config.py:2171` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_deployment_degrade_missed_fill_rate` | `CHILI_BRAIN_DEPLOYMENT_DEGRADE_MISSED_FILL_RATE` | 0.35 | `app/config.py:2173` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_deployment_degrade_negative_expectancy_rolls` | `CHILI_BRAIN_DEPLOYMENT_DEGRADE_NEGATIVE_EXPECTANCY_ROLLS` | 3 | `app/config.py:2174` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_deployment_degrade_slippage_bps` | `CHILI_BRAIN_DEPLOYMENT_DEGRADE_SLIPPAGE_BPS` | 35.0 | `app/config.py:2172` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_deployment_promote_min_paper_trades` | `CHILI_BRAIN_DEPLOYMENT_PROMOTE_MIN_PAPER_TRADES` | 3 | `app/config.py:2170` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_cron_hour` | `BRAIN_DIVERGENCE_SCORER_CRON_HOUR` | 6 | `app/config.py:427` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_divergence_scorer_cron_minute` | `BRAIN_DIVERGENCE_SCORER_CRON_MINUTE` | 15 | `app/config.py:428` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_divergence_scorer_layer_weight_bracket` | `BRAIN_DIVERGENCE_SCORER_LAYER_WEIGHT_BRACKET` | 1.0 | `app/config.py:432` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_layer_weight_exit` | `BRAIN_DIVERGENCE_SCORER_LAYER_WEIGHT_EXIT` | 1.0 | `app/config.py:430` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_layer_weight_ledger` | `BRAIN_DIVERGENCE_SCORER_LAYER_WEIGHT_LEDGER` | 1.0 | `app/config.py:429` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_layer_weight_sizer` | `BRAIN_DIVERGENCE_SCORER_LAYER_WEIGHT_SIZER` | 1.0 | `app/config.py:433` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_layer_weight_venue` | `BRAIN_DIVERGENCE_SCORER_LAYER_WEIGHT_VENUE` | 0.8 | `app/config.py:431` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_lookback_days` | `BRAIN_DIVERGENCE_SCORER_LOOKBACK_DAYS` | 7 | `app/config.py:426` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_min_layers_sampled` | `BRAIN_DIVERGENCE_SCORER_MIN_LAYERS_SAMPLED` | 1 | `app/config.py:423` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_mode` | `BRAIN_DIVERGENCE_SCORER_MODE` | 'shadow' | `app/config.py:421` | String / URL / identifier; infrastructure. | — | — |
| `brain_divergence_scorer_ops_log_enabled` | `BRAIN_DIVERGENCE_SCORER_OPS_LOG_ENABLED` | True | `app/config.py:422` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_divergence_scorer_red_threshold` | `BRAIN_DIVERGENCE_SCORER_RED_THRESHOLD` | 1.8 | `app/config.py:425` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_divergence_scorer_yellow_threshold` | `BRAIN_DIVERGENCE_SCORER_YELLOW_THRESHOLD` | 0.9 | `app/config.py:424` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_drift_monitor_cron_hour` | `BRAIN_DRIFT_MONITOR_CRON_HOUR` | 5 | `app/config.py:413` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_drift_monitor_cron_minute` | `BRAIN_DRIFT_MONITOR_CRON_MINUTE` | 30 | `app/config.py:414` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_drift_monitor_cusum_k` | `BRAIN_DRIFT_MONITOR_CUSUM_K` | 0.05 | `app/config.py:410` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_drift_monitor_cusum_threshold_mult` | `BRAIN_DRIFT_MONITOR_CUSUM_THRESHOLD_MULT` | 0.6 | `app/config.py:411` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_drift_monitor_min_red_sample` | `BRAIN_DRIFT_MONITOR_MIN_RED_SAMPLE` | 20 | `app/config.py:406` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_drift_monitor_min_yellow_sample` | `BRAIN_DRIFT_MONITOR_MIN_YELLOW_SAMPLE` | 10 | `app/config.py:407` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_drift_monitor_mode` | `BRAIN_DRIFT_MONITOR_MODE` | 'shadow' | `app/config.py:404` | String / URL / identifier; infrastructure. | — | — |
| `brain_drift_monitor_ops_log_enabled` | `BRAIN_DRIFT_MONITOR_OPS_LOG_ENABLED` | True | `app/config.py:405` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_drift_monitor_red_brier_abs` | `BRAIN_DRIFT_MONITOR_RED_BRIER_ABS` | 0.2 | `app/config.py:409` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_drift_monitor_sample_lookback_days` | `BRAIN_DRIFT_MONITOR_SAMPLE_LOOKBACK_DAYS` | 30 | `app/config.py:412` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_drift_monitor_yellow_brier_abs` | `BRAIN_DRIFT_MONITOR_YELLOW_BRIER_ABS` | 0.1 | `app/config.py:408` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_economic_ledger_mode` | `BRAIN_ECONOMIC_LEDGER_MODE` | 'shadow' | `app/config.py:286` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_ECONOMIC_LEDGER_ROLLOUT.md |
| `brain_economic_ledger_ops_log_enabled` | `BRAIN_ECONOMIC_LEDGER_OPS_LOG_ENABLED` | True | `app/config.py:287` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_economic_ledger_parity_tolerance_usd` | `BRAIN_ECONOMIC_LEDGER_PARITY_TOLERANCE_USD` | 0.01 | `app/config.py:288` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_edge_evidence_enabled` | `BRAIN_EDGE_EVIDENCE_ENABLED` | True | `app/config.py:2014` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_edge_evidence_fdr_enabled` | `BRAIN_EDGE_EVIDENCE_FDR_ENABLED` | True | `app/config.py:2022` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_edge_evidence_fdr_q` | `BRAIN_EDGE_EVIDENCE_FDR_Q` | 0.1 | `app/config.py:2023` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_edge_evidence_gate_enabled` | `BRAIN_EDGE_EVIDENCE_GATE_ENABLED` | True | `app/config.py:2015` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_edge_evidence_max_is_perm_p` | `BRAIN_EDGE_EVIDENCE_MAX_IS_PERM_P` | None | `app/config.py:2018` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_edge_evidence_max_oos_perm_p` | `BRAIN_EDGE_EVIDENCE_MAX_OOS_PERM_P` | 0.2 | `app/config.py:2019` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_edge_evidence_max_wf_perm_p` | `BRAIN_EDGE_EVIDENCE_MAX_WF_PERM_P` | 0.25 | `app/config.py:2020` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_edge_evidence_permutations` | `BRAIN_EDGE_EVIDENCE_PERMUTATIONS` | 400 | `app/config.py:2016` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_edge_evidence_require_wf_when_available` | `BRAIN_EDGE_EVIDENCE_REQUIRE_WF_WHEN_AVAILABLE` | False | `app/config.py:2021` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_edge_evidence_seed` | `BRAIN_EDGE_EVIDENCE_SEED` | 42 | `app/config.py:2017` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_enable_capacity_governor` | `CHILI_BRAIN_ENABLE_CAPACITY_GOVERNOR` | True | `app/config.py:2141` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_enable_decision_ledger` | `CHILI_BRAIN_ENABLE_DECISION_LEDGER` | True | `app/config.py:2133` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_enable_deployment_ladder` | `CHILI_BRAIN_ENABLE_DEPLOYMENT_LADDER` | True | `app/config.py:2142` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_enable_execution_realism` | `CHILI_BRAIN_ENABLE_EXECUTION_REALISM` | True | `app/config.py:2140` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_enforce_net_expectancy_live` | `CHILI_BRAIN_ENFORCE_NET_EXPECTANCY_LIVE` | True | `app/config.py:2149` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_enforce_net_expectancy_paper` | `CHILI_BRAIN_ENFORCE_NET_EXPECTANCY_PAPER` | True | `app/config.py:2146` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_evolution_min_trades` | `BRAIN_EVOLUTION_MIN_TRADES` | 5 | `app/config.py:838` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_evolution_min_trades_penalty` | `BRAIN_EVOLUTION_MIN_TRADES_PENALTY` | 0.25 | `app/config.py:839` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_evolution_weight_return` | `BRAIN_EVOLUTION_WEIGHT_RETURN` | 0.01 | `app/config.py:837` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_evolution_weight_sharpe` | `BRAIN_EVOLUTION_WEIGHT_SHARPE` | 1.0 | `app/config.py:835` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_evolution_weight_wr` | `BRAIN_EVOLUTION_WEIGHT_WR` | 2.0 | `app/config.py:836` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_capacity_max_adv_frac` | `BRAIN_EXECUTION_CAPACITY_MAX_ADV_FRAC` | 0.05 | `app/config.py:318` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_cost_default_fee_bps` | `BRAIN_EXECUTION_COST_DEFAULT_FEE_BPS` | 1.0 | `app/config.py:316` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_cost_impact_cap_bps` | `BRAIN_EXECUTION_COST_IMPACT_CAP_BPS` | 50.0 | `app/config.py:317` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_cost_mode` | `BRAIN_EXECUTION_COST_MODE` | 'shadow' | `app/config.py:315` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_EXECUTION_REALISM_ROLLOUT.md |
| `brain_execution_robustness_critical_fill_rate` | `BRAIN_EXECUTION_ROBUSTNESS_CRITICAL_FILL_RATE` | 0.45 | `app/config.py:2089` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_robustness_critical_slippage_bps` | `BRAIN_EXECUTION_ROBUSTNESS_CRITICAL_SLIPPAGE_BPS` | 65.0 | `app/config.py:2091` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_robustness_flag_weak_truth_live` | `BRAIN_EXECUTION_ROBUSTNESS_FLAG_WEAK_TRUTH_LIVE` | True | `app/config.py:2093` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_execution_robustness_live_not_recommended` | `BRAIN_EXECUTION_ROBUSTNESS_LIVE_NOT_RECOMMENDED` | True | `app/config.py:2092` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_execution_robustness_min_orders` | `BRAIN_EXECUTION_ROBUSTNESS_MIN_ORDERS` | 5 | `app/config.py:2087` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_robustness_shadow_mode` | `BRAIN_EXECUTION_ROBUSTNESS_SHADOW_MODE` | True | `app/config.py:2096` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_execution_robustness_v2_enabled` | `BRAIN_EXECUTION_ROBUSTNESS_V2_ENABLED` | True | `app/config.py:2095` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_execution_robustness_v2_live_not_recommended` | `BRAIN_EXECUTION_ROBUSTNESS_V2_LIVE_NOT_RECOMMENDED` | False | `app/config.py:2097` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_execution_robustness_warn_fill_rate` | `BRAIN_EXECUTION_ROBUSTNESS_WARN_FILL_RATE` | 0.65 | `app/config.py:2088` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_robustness_warn_slippage_bps` | `BRAIN_EXECUTION_ROBUSTNESS_WARN_SLIPPAGE_BPS` | 35.0 | `app/config.py:2090` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_execution_robustness_window_days` | `BRAIN_EXECUTION_ROBUSTNESS_WINDOW_DAYS` | 120 | `app/config.py:2086` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_exit_engine_mode` | `BRAIN_EXIT_ENGINE_MODE` | 'shadow' | `app/config.py:276` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_EXIT_ENGINE_ROLLOUT.md |
| `brain_exit_engine_ops_log_enabled` | `BRAIN_EXIT_ENGINE_OPS_LOG_ENABLED` | True | `app/config.py:277` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_exit_engine_parity_sample_pct` | `BRAIN_EXIT_ENGINE_PARITY_SAMPLE_PCT` | 1.0 | `app/config.py:278` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_expectancy_allocator_shadow_mode` | `CHILI_BRAIN_EXPECTANCY_ALLOCATOR_SHADOW_MODE` | False | `app/config.py:2137` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_fast_eval_enabled` | `BRAIN_FAST_EVAL_ENABLED` | True | `app/config.py:761` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_fast_eval_interval_minutes` | `BRAIN_FAST_EVAL_INTERVAL_MINUTES` | 10 | `app/config.py:765` | Infrastructure / connectivity / tuning bucket (`'interval_'`). | — | — |
| `brain_fast_eval_max_tickers` | `BRAIN_FAST_EVAL_MAX_TICKERS` | 400 | `app/config.py:766` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_fast_eval_scheduler_enabled` | `BRAIN_FAST_EVAL_SCHEDULER_ENABLED` | False | `app/config.py:764` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_high_vol_miner_enabled` | `BRAIN_HIGH_VOL_MINER_ENABLED` | True | `app/config.py:1981` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_internal_secret` | `BRAIN_INTERNAL_SECRET` | '' | `app/config.py:889` | Infrastructure / connectivity / tuning bucket (`'secret'`). | — | — |
| `brain_intraday_intervals` | `BRAIN_INTRADAY_INTERVALS` | '1m,5m,15m' | `app/config.py:771` | String / URL / identifier; infrastructure. | — | — |
| `brain_intraday_max_tickers` | `BRAIN_INTRADAY_MAX_TICKERS` | 1000 | `app/config.py:772` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_intraday_session_cron_hour` | `BRAIN_INTRADAY_SESSION_CRON_HOUR` | 22 | `app/config.py:589` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_cron_minute` | `BRAIN_INTRADAY_SESSION_CRON_MINUTE` | 0 | `app/config.py:590` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_gap_fade` | `BRAIN_INTRADAY_SESSION_GAP_FADE` | 0.005 | `app/config.py:608` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_gap_go` | `BRAIN_INTRADAY_SESSION_GAP_GO` | 0.005 | `app/config.py:607` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_interval` | `BRAIN_INTRADAY_SESSION_INTERVAL` | '5m' | `app/config.py:593` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_lookback_days` | `BRAIN_INTRADAY_SESSION_LOOKBACK_DAYS` | 14 | `app/config.py:612` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_midday_compression_cut` | `BRAIN_INTRADAY_SESSION_MIDDAY_COMPRESSION_CUT` | 0.5 | `app/config.py:606` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_min_bars` | `BRAIN_INTRADAY_SESSION_MIN_BARS` | 40 | `app/config.py:597` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_min_coverage_score` | `BRAIN_INTRADAY_SESSION_MIN_COVERAGE_SCORE` | 0.5 | `app/config.py:599` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_mode` | `BRAIN_INTRADAY_SESSION_MODE` | 'shadow' | `app/config.py:585` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_ops_log_enabled` | `BRAIN_INTRADAY_SESSION_OPS_LOG_ENABLED` | True | `app/config.py:586` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_or_minutes` | `BRAIN_INTRADAY_SESSION_OR_MINUTES` | 30 | `app/config.py:601` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_or_range_high` | `BRAIN_INTRADAY_SESSION_OR_RANGE_HIGH` | 0.012 | `app/config.py:605` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_or_range_low` | `BRAIN_INTRADAY_SESSION_OR_RANGE_LOW` | 0.003 | `app/config.py:604` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_period` | `BRAIN_INTRADAY_SESSION_PERIOD` | '5d' | `app/config.py:594` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_power_minutes` | `BRAIN_INTRADAY_SESSION_POWER_MINUTES` | 30 | `app/config.py:602` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_reversal_close` | `BRAIN_INTRADAY_SESSION_REVERSAL_CLOSE` | 0.003 | `app/config.py:610` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_source_symbol` | `BRAIN_INTRADAY_SESSION_SOURCE_SYMBOL` | 'SPY' | `app/config.py:592` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_session_trending_close` | `BRAIN_INTRADAY_SESSION_TRENDING_CLOSE` | 0.006 | `app/config.py:609` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `brain_intraday_snapshots_enabled` | `BRAIN_INTRADAY_SNAPSHOTS_ENABLED` | True | `app/config.py:770` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_io_effective_cpus_override` | `BRAIN_IO_EFFECTIVE_CPUS_OVERRIDE` | None | `app/config.py:253` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_io_workers_high` | `BRAIN_IO_WORKERS_HIGH` | None | `app/config.py:254` | Infrastructure / connectivity / tuning bucket (`'workers'`). | — | — |
| `brain_io_workers_low` | `BRAIN_IO_WORKERS_LOW` | None | `app/config.py:256` | Infrastructure / connectivity / tuning bucket (`'workers'`). | — | — |
| `brain_io_workers_med` | `BRAIN_IO_WORKERS_MED` | None | `app/config.py:255` | Infrastructure / connectivity / tuning bucket (`'workers'`). | — | — |
| `brain_live_brackets_mode` | `BRAIN_LIVE_BRACKETS_MODE` | 'shadow' | `app/config.py:332` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_LIVE_BRACKETS_ROLLOUT.md |
| `brain_live_brackets_ops_log_enabled` | `BRAIN_LIVE_BRACKETS_OPS_LOG_ENABLED` | True | `app/config.py:333` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_live_brackets_price_drift_bps` | `BRAIN_LIVE_BRACKETS_PRICE_DRIFT_BPS` | 25.0 | `app/config.py:335` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_brackets_qty_drift_abs` | `BRAIN_LIVE_BRACKETS_QTY_DRIFT_ABS` | 1e-06 | `app/config.py:336` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_brackets_reconciliation_interval_s` | `BRAIN_LIVE_BRACKETS_RECONCILIATION_INTERVAL_S` | 60 | `app/config.py:334` | Infrastructure / connectivity / tuning bucket (`'interval_'`). | — | — |
| `brain_live_brackets_staged_sweep_enabled` | `BRAIN_LIVE_BRACKETS_STAGED_SWEEP_ENABLED` | True | `app/config.py:342` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_live_deployment_enforcement` | `CHILI_BRAIN_LIVE_DEPLOYMENT_ENFORCEMENT` | True | `app/config.py:2161` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_live_drift_baseline_p0_high` | `BRAIN_LIVE_DRIFT_BASELINE_P0_HIGH` | 0.95 | `app/config.py:2064` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_baseline_p0_low` | `BRAIN_LIVE_DRIFT_BASELINE_P0_LOW` | 0.05 | `app/config.py:2063` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_confidence_cap` | `BRAIN_LIVE_DRIFT_CONFIDENCE_CAP` | 0.95 | `app/config.py:2073` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_confidence_floor` | `BRAIN_LIVE_DRIFT_CONFIDENCE_FLOOR` | 0.1 | `app/config.py:2072` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_confidence_mult_critical` | `BRAIN_LIVE_DRIFT_CONFIDENCE_MULT_CRITICAL` | 0.88 | `app/config.py:2071` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_confidence_mult_healthy` | `BRAIN_LIVE_DRIFT_CONFIDENCE_MULT_HEALTHY` | 1.0 | `app/config.py:2069` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_confidence_mult_warning` | `BRAIN_LIVE_DRIFT_CONFIDENCE_MULT_WARNING` | 0.94 | `app/config.py:2070` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_confidence_nudge_enabled` | `BRAIN_LIVE_DRIFT_CONFIDENCE_NUDGE_ENABLED` | True | `app/config.py:2068` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_live_drift_critical_delta_pp` | `BRAIN_LIVE_DRIFT_CRITICAL_DELTA_PP` | 18.0 | `app/config.py:2066` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_live_min_primary` | `BRAIN_LIVE_DRIFT_LIVE_MIN_PRIMARY` | 8 | `app/config.py:2061` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_min_trades` | `BRAIN_LIVE_DRIFT_MIN_TRADES` | 12 | `app/config.py:2062` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_shadow_mode` | `BRAIN_LIVE_DRIFT_SHADOW_MODE` | False | `app/config.py:2077` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_live_drift_strong_p_like` | `BRAIN_LIVE_DRIFT_STRONG_P_LIKE` | 0.02 | `app/config.py:2067` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_v2_critical_expectancy_ratio` | `BRAIN_LIVE_DRIFT_V2_CRITICAL_EXPECTANCY_RATIO` | 0.4 | `app/config.py:2079` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_v2_critical_profit_factor` | `BRAIN_LIVE_DRIFT_V2_CRITICAL_PROFIT_FACTOR` | 0.8 | `app/config.py:2081` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_v2_critical_slippage_bps` | `BRAIN_LIVE_DRIFT_V2_CRITICAL_SLIPPAGE_BPS` | 45.0 | `app/config.py:2083` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_v2_enabled` | `BRAIN_LIVE_DRIFT_V2_ENABLED` | True | `app/config.py:2076` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_live_drift_v2_warn_expectancy_ratio` | `BRAIN_LIVE_DRIFT_V2_WARN_EXPECTANCY_RATIO` | 0.7 | `app/config.py:2078` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_v2_warn_profit_factor` | `BRAIN_LIVE_DRIFT_V2_WARN_PROFIT_FACTOR` | 1.0 | `app/config.py:2080` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_v2_warn_slippage_bps` | `BRAIN_LIVE_DRIFT_V2_WARN_SLIPPAGE_BPS` | 25.0 | `app/config.py:2082` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_warning_delta_pp` | `BRAIN_LIVE_DRIFT_WARNING_DELTA_PP` | 8.0 | `app/config.py:2065` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_live_drift_window_days` | `BRAIN_LIVE_DRIFT_WINDOW_DAYS` | 120 | `app/config.py:2060` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_macro_regime_cron_hour` | `BRAIN_MACRO_REGIME_CRON_HOUR` | 6 | `app/config.py:445` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_macro_regime_cron_minute` | `BRAIN_MACRO_REGIME_CRON_MINUTE` | 30 | `app/config.py:446` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_macro_regime_lookback_days` | `BRAIN_MACRO_REGIME_LOOKBACK_DAYS` | 14 | `app/config.py:455` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_macro_regime_min_coverage_score` | `BRAIN_MACRO_REGIME_MIN_COVERAGE_SCORE` | 0.5 | `app/config.py:447` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_macro_regime_mode` | `BRAIN_MACRO_REGIME_MODE` | 'shadow' | `app/config.py:443` | String / URL / identifier; infrastructure. | — | — |
| `brain_macro_regime_ops_log_enabled` | `BRAIN_MACRO_REGIME_OPS_LOG_ENABLED` | True | `app/config.py:444` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_macro_regime_promote_threshold` | `BRAIN_MACRO_REGIME_PROMOTE_THRESHOLD` | 0.35 | `app/config.py:450` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_macro_regime_strong_trend_threshold` | `BRAIN_MACRO_REGIME_STRONG_TREND_THRESHOLD` | 0.03 | `app/config.py:449` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_macro_regime_trend_up_threshold` | `BRAIN_MACRO_REGIME_TREND_UP_THRESHOLD` | 0.01 | `app/config.py:448` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_macro_regime_weight_credit` | `BRAIN_MACRO_REGIME_WEIGHT_CREDIT` | 0.35 | `app/config.py:452` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_macro_regime_weight_rates` | `BRAIN_MACRO_REGIME_WEIGHT_RATES` | 0.45 | `app/config.py:451` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_macro_regime_weight_usd` | `BRAIN_MACRO_REGIME_WEIGHT_USD` | 0.2 | `app/config.py:453` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_market_snapshot_defer_while_learning_running` | `BRAIN_MARKET_SNAPSHOT_DEFER_WHILE_LEARNING_RUNNING` | True | `app/config.py:259` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_market_snapshot_interval_minutes` | `BRAIN_MARKET_SNAPSHOT_INTERVAL_MINUTES` | 15 | `app/config.py:1948` | Infrastructure / connectivity / tuning bucket (`'interval_'`). | — | — |
| `brain_market_snapshot_scheduler_enabled` | `BRAIN_MARKET_SNAPSHOT_SCHEDULER_ENABLED` | True | `app/config.py:1944` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_max_adv_notional_pct` | `CHILI_BRAIN_MAX_ADV_NOTIONAL_PCT` | 0.25 | `app/config.py:2164` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_max_correlated_positions` | `BRAIN_MAX_CORRELATED_POSITIONS` | 0 | `app/config.py:2125` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_max_cpu_pct` | `BRAIN_MAX_CPU_PCT` | None | `app/config.py:226` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_max_open_per_sector` | `BRAIN_MAX_OPEN_PER_SECTOR` | 0 | `app/config.py:2124` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_merge_coinbase_spot_universe` | `BRAIN_MERGE_COINBASE_SPOT_UNIVERSE` | True | `app/config.py:779` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_min_trades_for_promotion` | `BRAIN_MIN_TRADES_FOR_PROMOTION` | 30 | `app/config.py:2001` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_mine_patterns_max_tickers` | `BRAIN_MINE_PATTERNS_MAX_TICKERS` | 1000 | `app/config.py:789` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_minimum_net_expectancy_to_trade` | `CHILI_BRAIN_MINIMUM_NET_EXPECTANCY_TO_TRADE` | 0.0 | `app/config.py:2143` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_mining_purged_cpcv_enabled` | `BRAIN_MINING_PURGED_CPCV_ENABLED` | True | `app/config.py:791` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_mp_child_database_max_overflow` | `BRAIN_MP_CHILD_DATABASE_MAX_OVERFLOW` | 2 | `app/config.py:232` | Infrastructure / connectivity / tuning bucket (`'database_'`). | — | — |
| `brain_mp_child_database_pool_size` | `BRAIN_MP_CHILD_DATABASE_POOL_SIZE` | 1 | `app/config.py:231` | Infrastructure / connectivity / tuning bucket (`'database_'`). | — | — |
| `brain_net_edge_cache_ttl_s` | `BRAIN_NET_EDGE_CACHE_TTL_S` | 300 | `app/config.py:268` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_net_edge_min_samples` | `BRAIN_NET_EDGE_MIN_SAMPLES` | 50 | `app/config.py:267` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_net_edge_ops_log_enabled` | `BRAIN_NET_EDGE_OPS_LOG_ENABLED` | True | `app/config.py:266` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_net_edge_ranker_mode` | `BRAIN_NET_EDGE_RANKER_MODE` | 'shadow' | `app/config.py:265` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_NET_EDGE_RANKER_ROLLOUT.md |
| `brain_net_edge_shadow_sample_pct` | `BRAIN_NET_EDGE_SHADOW_SAMPLE_PCT` | 1.0 | `app/config.py:269` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_bootstrap_ci_min_wr` | `BRAIN_OOS_BOOTSTRAP_CI_MIN_WR` | 0.42 | `app/config.py:2011` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_bootstrap_iterations` | `BRAIN_OOS_BOOTSTRAP_ITERATIONS` | 500 | `app/config.py:2009` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_holdout_fraction` | `BRAIN_OOS_HOLDOUT_FRACTION` | 0.25 | `app/config.py:1959` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_max_is_oos_gap_pct` | `BRAIN_OOS_MAX_IS_OOS_GAP_PCT` | 38.0 | `app/config.py:1962` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_aggregate_trades` | `BRAIN_OOS_MIN_AGGREGATE_TRADES` | 15 | `app/config.py:1964` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_evaluated_tickers` | `BRAIN_OOS_MIN_EVALUATED_TICKERS` | 3 | `app/config.py:1963` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_expectancy_pct` | `BRAIN_OOS_MIN_EXPECTANCY_PCT` | None | `app/config.py:2002` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_oos_trades_crypto` | `BRAIN_OOS_MIN_OOS_TRADES_CRYPTO` | None | `app/config.py:1971` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_oos_trades_high_vol_family` | `BRAIN_OOS_MIN_OOS_TRADES_HIGH_VOL_FAMILY` | None | `app/config.py:1972` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_oos_trades_short_tf` | `BRAIN_OOS_MIN_OOS_TRADES_SHORT_TF` | None | `app/config.py:1970` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_profit_factor` | `BRAIN_OOS_MIN_PROFIT_FACTOR` | None | `app/config.py:2003` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_win_rate_pct` | `BRAIN_OOS_MIN_WIN_RATE_PCT` | 42.0 | `app/config.py:1961` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_win_rate_pct_crypto` | `BRAIN_OOS_MIN_WIN_RATE_PCT_CRYPTO` | None | `app/config.py:1968` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_win_rate_pct_high_vol_family` | `BRAIN_OOS_MIN_WIN_RATE_PCT_HIGH_VOL_FAMILY` | None | `app/config.py:1969` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_min_win_rate_pct_short_tf` | `BRAIN_OOS_MIN_WIN_RATE_PCT_SHORT_TF` | None | `app/config.py:1967` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_oos_require_robustness_wr_above_gate` | `BRAIN_OOS_REQUIRE_ROBUSTNESS_WR_ABOVE_GATE` | False | `app/config.py:2007` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_oos_robustness_extra_fractions` | `BRAIN_OOS_ROBUSTNESS_EXTRA_FRACTIONS` | '' | `app/config.py:2005` | String / URL / identifier; infrastructure. | — | — |
| `brain_ops_health_enabled` | `BRAIN_OPS_HEALTH_ENABLED` | True | `app/config.py:435` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_ops_health_lookback_days` | `BRAIN_OPS_HEALTH_LOOKBACK_DAYS` | 14 | `app/config.py:436` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_paper_book_on_promotion` | `BRAIN_PAPER_BOOK_ON_PROMOTION` | False | `app/config.py:2101` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_paper_deployment_enforcement` | `CHILI_BRAIN_PAPER_DEPLOYMENT_ENFORCEMENT` | True | `app/config.py:2158` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_parameter_stability_enabled` | `BRAIN_PARAMETER_STABILITY_ENABLED` | True | `app/config.py:2027` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_parameter_stability_max_variant_evals` | `BRAIN_PARAMETER_STABILITY_MAX_VARIANT_EVALS` | 6 | `app/config.py:2030` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_parameter_stability_neighbor_abs_floor` | `BRAIN_PARAMETER_STABILITY_NEIGHBOR_ABS_FLOOR` | 40.0 | `app/config.py:2032` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_parameter_stability_neighbor_rel_tol` | `BRAIN_PARAMETER_STABILITY_NEIGHBOR_REL_TOL` | 0.12 | `app/config.py:2031` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_parameter_stability_seed` | `BRAIN_PARAMETER_STABILITY_SEED` | 123 | `app/config.py:2028` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_parameter_stability_ticker_subset_size` | `BRAIN_PARAMETER_STABILITY_TICKER_SUBSET_SIZE` | 2 | `app/config.py:2029` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_approval_days` | `BRAIN_PATTERN_REGIME_AUTOPILOT_APPROVAL_DAYS` | 30 | `app/config.py:730` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_compare_days` | `BRAIN_PATTERN_REGIME_AUTOPILOT_COMPARE_DAYS` | 10 | `app/config.py:714` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_cron_hour` | `BRAIN_PATTERN_REGIME_AUTOPILOT_CRON_HOUR` | 6 | `app/config.py:706` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_pattern_regime_autopilot_cron_minute` | `BRAIN_PATTERN_REGIME_AUTOPILOT_CRON_MINUTE` | 15 | `app/config.py:707` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_pattern_regime_autopilot_kill` | `BRAIN_PATTERN_REGIME_AUTOPILOT_KILL` | False | `app/config.py:701` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_pattern_regime_autopilot_ks_max_fires_per_day` | `BRAIN_PATTERN_REGIME_AUTOPILOT_KS_MAX_FIRES_PER_DAY` | 1.0 | `app/config.py:728` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_min_decisions` | `BRAIN_PATTERN_REGIME_AUTOPILOT_MIN_DECISIONS` | 100 | `app/config.py:718` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_ops_log_enabled` | `BRAIN_PATTERN_REGIME_AUTOPILOT_OPS_LOG_ENABLED` | True | `app/config.py:702` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_pattern_regime_autopilot_promo_block_max_ratio` | `BRAIN_PATTERN_REGIME_AUTOPILOT_PROMO_BLOCK_MAX_RATIO` | 0.1 | `app/config.py:725` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_shadow_days` | `BRAIN_PATTERN_REGIME_AUTOPILOT_SHADOW_DAYS` | 5 | `app/config.py:713` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_tilt_mult_max` | `BRAIN_PATTERN_REGIME_AUTOPILOT_TILT_MULT_MAX` | 1.25 | `app/config.py:722` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_tilt_mult_min` | `BRAIN_PATTERN_REGIME_AUTOPILOT_TILT_MULT_MIN` | 0.85 | `app/config.py:721` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_autopilot_weekly_cron_dow` | `BRAIN_PATTERN_REGIME_AUTOPILOT_WEEKLY_CRON_DOW` | 'mon' | `app/config.py:710` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_pattern_regime_autopilot_weekly_cron_hour` | `BRAIN_PATTERN_REGIME_AUTOPILOT_WEEKLY_CRON_HOUR` | 9 | `app/config.py:709` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_pattern_regime_killswitch_consecutive_days` | `BRAIN_PATTERN_REGIME_KILLSWITCH_CONSECUTIVE_DAYS` | 3 | `app/config.py:685` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_killswitch_cron_hour` | `BRAIN_PATTERN_REGIME_KILLSWITCH_CRON_HOUR` | 23 | `app/config.py:680` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_pattern_regime_killswitch_cron_minute` | `BRAIN_PATTERN_REGIME_KILLSWITCH_CRON_MINUTE` | 5 | `app/config.py:681` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_pattern_regime_killswitch_lookback_days` | `BRAIN_PATTERN_REGIME_KILLSWITCH_LOOKBACK_DAYS` | 14 | `app/config.py:690` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_killswitch_max_per_pattern_30d` | `BRAIN_PATTERN_REGIME_KILLSWITCH_MAX_PER_PATTERN_30D` | 1 | `app/config.py:689` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_killswitch_mode` | `BRAIN_PATTERN_REGIME_KILLSWITCH_MODE` | 'shadow' | `app/config.py:677` | String / URL / identifier; infrastructure. | — | — |
| `brain_pattern_regime_killswitch_neg_expectancy_threshold` | `BRAIN_PATTERN_REGIME_KILLSWITCH_NEG_EXPECTANCY_THRESHOLD` | -0.005 | `app/config.py:686` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_killswitch_ops_log_enabled` | `BRAIN_PATTERN_REGIME_KILLSWITCH_OPS_LOG_ENABLED` | True | `app/config.py:678` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_pattern_regime_perf_cron_hour` | `BRAIN_PATTERN_REGIME_PERF_CRON_HOUR` | 23 | `app/config.py:625` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_pattern_regime_perf_cron_minute` | `BRAIN_PATTERN_REGIME_PERF_CRON_MINUTE` | 0 | `app/config.py:626` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_pattern_regime_perf_lookback_days` | `BRAIN_PATTERN_REGIME_PERF_LOOKBACK_DAYS` | 14 | `app/config.py:638` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_perf_max_patterns` | `BRAIN_PATTERN_REGIME_PERF_MAX_PATTERNS` | 500 | `app/config.py:636` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_perf_min_trades_per_cell` | `BRAIN_PATTERN_REGIME_PERF_MIN_TRADES_PER_CELL` | 3 | `app/config.py:632` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_perf_mode` | `BRAIN_PATTERN_REGIME_PERF_MODE` | 'shadow' | `app/config.py:622` | String / URL / identifier; infrastructure. | — | — |
| `brain_pattern_regime_perf_ops_log_enabled` | `BRAIN_PATTERN_REGIME_PERF_OPS_LOG_ENABLED` | True | `app/config.py:623` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_pattern_regime_perf_window_days` | `BRAIN_PATTERN_REGIME_PERF_WINDOW_DAYS` | 90 | `app/config.py:628` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_promotion_block_on_negative_dimensions` | `BRAIN_PATTERN_REGIME_PROMOTION_BLOCK_ON_NEGATIVE_DIMENSIONS` | 2 | `app/config.py:672` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_promotion_kill` | `BRAIN_PATTERN_REGIME_PROMOTION_KILL` | False | `app/config.py:667` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_pattern_regime_promotion_min_confident_dimensions` | `BRAIN_PATTERN_REGIME_PROMOTION_MIN_CONFIDENT_DIMENSIONS` | 3 | `app/config.py:669` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_promotion_min_mean_expectancy` | `BRAIN_PATTERN_REGIME_PROMOTION_MIN_MEAN_EXPECTANCY` | 0.0 | `app/config.py:674` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_promotion_mode` | `BRAIN_PATTERN_REGIME_PROMOTION_MODE` | 'shadow' | `app/config.py:665` | String / URL / identifier; infrastructure. | — | — |
| `brain_pattern_regime_promotion_ops_log_enabled` | `BRAIN_PATTERN_REGIME_PROMOTION_OPS_LOG_ENABLED` | True | `app/config.py:666` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_pattern_regime_tilt_kill` | `BRAIN_PATTERN_REGIME_TILT_KILL` | False | `app/config.py:652` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_pattern_regime_tilt_max_multiplier` | `BRAIN_PATTERN_REGIME_TILT_MAX_MULTIPLIER` | 2.0 | `app/config.py:655` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_tilt_max_staleness_days` | `BRAIN_PATTERN_REGIME_TILT_MAX_STALENESS_DAYS` | 5 | `app/config.py:662` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_tilt_min_confident_dimensions` | `BRAIN_PATTERN_REGIME_TILT_MIN_CONFIDENT_DIMENSIONS` | 3 | `app/config.py:659` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_tilt_min_multiplier` | `BRAIN_PATTERN_REGIME_TILT_MIN_MULTIPLIER` | 0.25 | `app/config.py:654` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_pattern_regime_tilt_mode` | `BRAIN_PATTERN_REGIME_TILT_MODE` | 'shadow' | `app/config.py:650` | String / URL / identifier; infrastructure. | — | — |
| `brain_pattern_regime_tilt_ops_log_enabled` | `BRAIN_PATTERN_REGIME_TILT_OPS_LOG_ENABLED` | True | `app/config.py:651` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_peer_candidate_sessions_max` | `CHILI_BRAIN_PEER_CANDIDATE_SESSIONS_MAX` | 4 | `app/config.py:2169` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_phase2_hygiene_nudge_enabled` | `BRAIN_PHASE2_HYGIENE_NUDGE_ENABLED` | True | `app/config.py:2033` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_pit_audit_mode` | `BRAIN_PIT_AUDIT_MODE` | 'shadow' | `app/config.py:294` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_PIT_HYGIENE_ROLLOUT.md |
| `brain_pit_audit_ops_log_enabled` | `BRAIN_PIT_AUDIT_OPS_LOG_ENABLED` | True | `app/config.py:295` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_position_sizer_crypto_bucket_cap_pct` | `BRAIN_POSITION_SIZER_CRYPTO_BUCKET_CAP_PCT` | 10.0 | `app/config.py:356` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_position_sizer_equity_bucket_cap_pct` | `BRAIN_POSITION_SIZER_EQUITY_BUCKET_CAP_PCT` | 15.0 | `app/config.py:355` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_position_sizer_kelly_scale` | `BRAIN_POSITION_SIZER_KELLY_SCALE` | 0.25 | `app/config.py:358` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_position_sizer_max_risk_pct` | `BRAIN_POSITION_SIZER_MAX_RISK_PCT` | 2.0 | `app/config.py:359` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_position_sizer_mode` | `BRAIN_POSITION_SIZER_MODE` | 'shadow' | `app/config.py:353` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_POSITION_SIZER_ROLLOUT.md |
| `brain_position_sizer_ops_log_enabled` | `BRAIN_POSITION_SIZER_OPS_LOG_ENABLED` | True | `app/config.py:354` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_position_sizer_single_ticker_cap_pct` | `BRAIN_POSITION_SIZER_SINGLE_TICKER_CAP_PCT` | 7.5 | `app/config.py:357` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_prediction_io_workers` | `BRAIN_PREDICTION_IO_WORKERS` | None | `app/config.py:258` | Prediction-mirror tuning / IO — infrastructure or phased auxiliary. | — | — |
| `brain_prediction_mirror_write_dedicated` | `BRAIN_PREDICTION_MIRROR_WRITE_DEDICATED` | False | `app/config.py:221` | Prediction-mirror tuning / IO — infrastructure or phased auxiliary. | — | — |
| `brain_prediction_read_max_age_seconds` | `BRAIN_PREDICTION_READ_MAX_AGE_SECONDS` | 900 | `app/config.py:212` | Prediction-mirror tuning / IO — infrastructure or phased auxiliary. | — | — |
| `brain_prescreen_internal_max_per_kind` | `BRAIN_PRESCREEN_INTERNAL_MAX_PER_KIND` | 40 | `app/config.py:2042` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_prescreen_max_total` | `BRAIN_PRESCREEN_MAX_TOTAL` | 3000 | `app/config.py:2043` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_prescreen_scheduler_enabled` | `BRAIN_PRESCREEN_SCHEDULER_ENABLED` | True | `app/config.py:2039` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_promotion_metric_mode` | `BRAIN_PROMOTION_METRIC_MODE` | 'accuracy' | `app/config.py:309` | String / URL / identifier; infrastructure. | — | — |
| `brain_queue_backtest_executor` | `BRAIN_QUEUE_BACKTEST_EXECUTOR` | 'threads' | `app/config.py:229` | Infrastructure / connectivity / tuning bucket (`'queue_backtest'`). | — | — |
| `brain_queue_batch_size` | `BRAIN_QUEUE_BATCH_SIZE` | 80 | `app/config.py:234` | Infrastructure / connectivity / tuning bucket (`'batch_size'`). | — | — |
| `brain_queue_exploration_enabled` | `BRAIN_QUEUE_EXPLORATION_ENABLED` | True | `app/config.py:752` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_queue_exploration_max` | `BRAIN_QUEUE_EXPLORATION_MAX` | 40 | `app/config.py:753` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_queue_prescreen_enabled` | `BRAIN_QUEUE_PRESCREEN_ENABLED` | True | `app/config.py:2036` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_queue_prescreen_min_win_rate_pct` | `BRAIN_QUEUE_PRESCREEN_MIN_WIN_RATE_PCT` | 45.0 | `app/config.py:2045` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_queue_prescreen_period` | `BRAIN_QUEUE_PRESCREEN_PERIOD` | '3mo' | `app/config.py:2044` | String / URL / identifier; infrastructure. | — | — |
| `brain_queue_prescreen_tickers` | `BRAIN_QUEUE_PRESCREEN_TICKERS` | 6 | `app/config.py:2037` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_queue_priority_stored_refresh` | `BRAIN_QUEUE_PRIORITY_STORED_REFRESH` | True | `app/config.py:2049` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_queue_process_cap` | `BRAIN_QUEUE_PROCESS_CAP` | None | `app/config.py:230` | Infrastructure / connectivity / tuning bucket (`'process_cap'`). | — | — |
| `brain_queue_stored_refresh_max_tickers` | `BRAIN_QUEUE_STORED_REFRESH_MAX_TICKERS` | 40 | `app/config.py:2050` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_queue_stored_stale_days` | `BRAIN_QUEUE_STORED_STALE_DAYS` | 14 | `app/config.py:2052` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_queue_stored_stale_trade_cap` | `BRAIN_QUEUE_STORED_STALE_TRADE_CAP` | 2 | `app/config.py:2051` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_queue_target_tickers` | `BRAIN_QUEUE_TARGET_TICKERS` | 60 | `app/config.py:741` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_recert_queue_include_yellow` | `BRAIN_RECERT_QUEUE_INCLUDE_YELLOW` | False | `app/config.py:418` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_recert_queue_mode` | `BRAIN_RECERT_QUEUE_MODE` | 'shadow' | `app/config.py:416` | String / URL / identifier; infrastructure. | — | — |
| `brain_recert_queue_ops_log_enabled` | `BRAIN_RECERT_QUEUE_OPS_LOG_ENABLED` | True | `app/config.py:417` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_regime_mining_enabled` | `BRAIN_REGIME_MINING_ENABLED` | True | `app/config.py:825` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_research_integrity_enabled` | `BRAIN_RESEARCH_INTEGRITY_ENABLED` | True | `app/config.py:756` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_research_integrity_max_check_bars` | `BRAIN_RESEARCH_INTEGRITY_MAX_CHECK_BARS` | 48 | `app/config.py:758` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_research_integrity_strict` | `BRAIN_RESEARCH_INTEGRITY_STRICT` | True | `app/config.py:757` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_retention_alert_days` | `BRAIN_RETENTION_ALERT_DAYS` | 90 | `app/config.py:2111` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_backtest_days` | `BRAIN_RETENTION_BACKTEST_DAYS` | 180 | `app/config.py:2112` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_batch_job_days` | `BRAIN_RETENTION_BATCH_JOB_DAYS` | 90 | `app/config.py:2109` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_breakout_alert_days` | `BRAIN_RETENTION_BREAKOUT_ALERT_DAYS` | 180 | `app/config.py:2121` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_cycle_run_days` | `BRAIN_RETENTION_CYCLE_RUN_DAYS` | 180 | `app/config.py:2114` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_event_days` | `BRAIN_RETENTION_EVENT_DAYS` | 120 | `app/config.py:2110` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_hypothesis_days` | `BRAIN_RETENTION_HYPOTHESIS_DAYS` | 180 | `app/config.py:2120` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_integration_event_days` | `BRAIN_RETENTION_INTEGRATION_EVENT_DAYS` | 90 | `app/config.py:2115` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_paper_trade_days` | `BRAIN_RETENTION_PAPER_TRADE_DAYS` | 180 | `app/config.py:2119` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_pattern_trade_days` | `BRAIN_RETENTION_PATTERN_TRADE_DAYS` | 365 | `app/config.py:2117` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_prediction_days` | `BRAIN_RETENTION_PREDICTION_DAYS` | 30 | `app/config.py:2113` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_prescreen_days` | `BRAIN_RETENTION_PRESCREEN_DAYS` | 90 | `app/config.py:2116` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_proposal_days` | `BRAIN_RETENTION_PROPOSAL_DAYS` | 90 | `app/config.py:2118` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retention_snapshot_days` | `BRAIN_RETENTION_SNAPSHOT_DAYS` | 180 | `app/config.py:2108` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_retest_interval_days` | `BRAIN_RETEST_INTERVAL_DAYS` | 7 | `app/config.py:750` | Infrastructure / connectivity / tuning bucket (`'interval_'`). | — | — |
| `brain_risk_cooldown_hours` | `BRAIN_RISK_COOLDOWN_HOURS` | 24 | `app/config.py:383` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_dial_ceiling` | `BRAIN_RISK_DIAL_CEILING` | 1.5 | `app/config.py:373` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_dial_default_cautious` | `BRAIN_RISK_DIAL_DEFAULT_CAUTIOUS` | 0.7 | `app/config.py:369` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_dial_default_risk_off` | `BRAIN_RISK_DIAL_DEFAULT_RISK_OFF` | 0.3 | `app/config.py:370` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_dial_default_risk_on` | `BRAIN_RISK_DIAL_DEFAULT_RISK_ON` | 1.0 | `app/config.py:368` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_dial_drawdown_floor` | `BRAIN_RISK_DIAL_DRAWDOWN_FLOOR` | 0.5 | `app/config.py:371` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_dial_drawdown_trigger_pct` | `BRAIN_RISK_DIAL_DRAWDOWN_TRIGGER_PCT` | 10.0 | `app/config.py:372` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_dial_mode` | `BRAIN_RISK_DIAL_MODE` | 'shadow' | `app/config.py:366` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_RISK_DIAL_ROLLOUT.md |
| `brain_risk_dial_ops_log_enabled` | `BRAIN_RISK_DIAL_OPS_LOG_ENABLED` | True | `app/config.py:367` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_risk_max_30d_dd_pct` | `BRAIN_RISK_MAX_30D_DD_PCT` | 8.0 | `app/config.py:381` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_max_5d_dd_pct` | `BRAIN_RISK_MAX_5D_DD_PCT` | 3.0 | `app/config.py:380` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_max_consec_losses` | `BRAIN_RISK_MAX_CONSEC_LOSSES` | 5 | `app/config.py:382` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_max_crypto` | `BRAIN_RISK_MAX_CRYPTO` | 5 | `app/config.py:390` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_max_heat_pct` | `BRAIN_RISK_MAX_HEAT_PCT` | 6.0 | `app/config.py:392` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_max_positions` | `BRAIN_RISK_MAX_POSITIONS` | 10 | `app/config.py:389` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_max_risk_per_trade_pct` | `BRAIN_RISK_MAX_RISK_PER_TRADE_PCT` | 1.0 | `app/config.py:393` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_max_same_ticker` | `BRAIN_RISK_MAX_SAME_TICKER` | 2 | `app/config.py:394` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_risk_max_stocks` | `BRAIN_RISK_MAX_STOCKS` | 8 | `app/config.py:391` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_scan_include_full_crypto_universe` | `BRAIN_SCAN_INCLUDE_FULL_CRYPTO_UNIVERSE` | True | `app/config.py:786` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_secondary_miners_on_cycle` | `BRAIN_SECONDARY_MINERS_ON_CYCLE` | True | `app/config.py:745` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_selection_bias_enabled` | `BRAIN_SELECTION_BIAS_ENABLED` | True | `app/config.py:2026` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_service_url` | `BRAIN_SERVICE_URL` | '' | `app/config.py:888` | Infrastructure / connectivity / tuning bucket (`'_url'`). | — | — |
| `brain_smart_bt_max_workers` | `BRAIN_SMART_BT_MAX_WORKERS` | 28 | `app/config.py:250` | Infrastructure / connectivity / tuning bucket (`'workers'`). | — | — |
| `brain_smart_bt_max_workers_in_process` | `BRAIN_SMART_BT_MAX_WORKERS_IN_PROCESS` | 8 | `app/config.py:233` | Infrastructure / connectivity / tuning bucket (`'workers'`). | — | — |
| `brain_snapshot_backfill_years` | `BRAIN_SNAPSHOT_BACKFILL_YEARS` | 10 | `app/config.py:773` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_snapshot_io_workers` | `BRAIN_SNAPSHOT_IO_WORKERS` | None | `app/config.py:257` | Infrastructure / connectivity / tuning bucket (`'workers'`). | — | — |
| `brain_snapshot_learned_v1_enabled` | `BRAIN_SNAPSHOT_LEARNED_V1_ENABLED` | True | `app/config.py:827` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_snapshot_top_tickers` | `BRAIN_SNAPSHOT_TOP_TICKERS` | 1000 | `app/config.py:769` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_ac1_mean_revert` | `BRAIN_TICKER_REGIME_AC1_MEAN_REVERT` | -0.05 | `app/config.py:517` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_ac1_trend` | `BRAIN_TICKER_REGIME_AC1_TREND` | 0.05 | `app/config.py:516` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_adx_trend` | `BRAIN_TICKER_REGIME_ADX_TREND` | 20.0 | `app/config.py:522` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_atr_period` | `BRAIN_TICKER_REGIME_ATR_PERIOD` | 14 | `app/config.py:523` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_cron_hour` | `BRAIN_TICKER_REGIME_CRON_HOUR` | 7 | `app/config.py:504` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_ticker_regime_cron_minute` | `BRAIN_TICKER_REGIME_CRON_MINUTE` | 15 | `app/config.py:505` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_ticker_regime_hurst_mean_revert` | `BRAIN_TICKER_REGIME_HURST_MEAN_REVERT` | 0.45 | `app/config.py:519` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_hurst_trend` | `BRAIN_TICKER_REGIME_HURST_TREND` | 0.55 | `app/config.py:518` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_lookback_days` | `BRAIN_TICKER_REGIME_LOOKBACK_DAYS` | 7 | `app/config.py:530` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_max_tickers` | `BRAIN_TICKER_REGIME_MAX_TICKERS` | 250 | `app/config.py:528` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_min_bars` | `BRAIN_TICKER_REGIME_MIN_BARS` | 40 | `app/config.py:508` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_min_coverage_score` | `BRAIN_TICKER_REGIME_MIN_COVERAGE_SCORE` | 0.5 | `app/config.py:514` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_mode` | `BRAIN_TICKER_REGIME_MODE` | 'shadow' | `app/config.py:502` | String / URL / identifier; infrastructure. | — | — |
| `brain_ticker_regime_ops_log_enabled` | `BRAIN_TICKER_REGIME_OPS_LOG_ENABLED` | True | `app/config.py:503` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_ticker_regime_vr_mean_revert` | `BRAIN_TICKER_REGIME_VR_MEAN_REVERT` | 0.95 | `app/config.py:521` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_ticker_regime_vr_trend` | `BRAIN_TICKER_REGIME_VR_TREND` | 1.05 | `app/config.py:520` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_tradeable_limit` | `BRAIN_TRADEABLE_LIMIT` | 20 | `app/config.py:832` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_tradeable_min_oos_trades` | `BRAIN_TRADEABLE_MIN_OOS_TRADES` | 5 | `app/config.py:831` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_tradeable_min_oos_wr` | `BRAIN_TRADEABLE_MIN_OOS_WR` | 50.0 | `app/config.py:830` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_triple_barrier_max_bars` | `BRAIN_TRIPLE_BARRIER_MAX_BARS` | 5 | `app/config.py:303` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_triple_barrier_mode` | `BRAIN_TRIPLE_BARRIER_MODE` | 'shadow' | `app/config.py:300` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_TRIPLE_BARRIER_ROLLOUT.md |
| `brain_triple_barrier_ops_log_enabled` | `BRAIN_TRIPLE_BARRIER_OPS_LOG_ENABLED` | True | `app/config.py:304` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_triple_barrier_sl_pct` | `BRAIN_TRIPLE_BARRIER_SL_PCT` | 0.01 | `app/config.py:302` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_triple_barrier_tp_pct` | `BRAIN_TRIPLE_BARRIER_TP_PCT` | 0.015 | `app/config.py:301` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_use_gpu_ml` | `BRAIN_USE_GPU_ML` | False | `app/config.py:742` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `brain_v1_wake_secret` | `BRAIN_V1_WAKE_SECRET` | '' | `app/config.py:885` | Infrastructure / connectivity / tuning bucket (`'secret'`). | — | — |
| `brain_venue_truth_mode` | `BRAIN_VENUE_TRUTH_MODE` | 'shadow' | `app/config.py:322` | String / URL / identifier; infrastructure. | — | docs/TRADING_BRAIN_EXECUTION_REALISM_ROLLOUT.md |
| `brain_venue_truth_ops_log_enabled` | `BRAIN_VENUE_TRUTH_OPS_LOG_ENABLED` | True | `app/config.py:323` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_vol_dispersion_corr_high` | `BRAIN_VOL_DISPERSION_CORR_HIGH` | 0.65 | `app/config.py:570` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_corr_low` | `BRAIN_VOL_DISPERSION_CORR_LOW` | 0.35 | `app/config.py:569` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_corr_sample_size` | `BRAIN_VOL_DISPERSION_CORR_SAMPLE_SIZE` | 30 | `app/config.py:557` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_cron_hour` | `BRAIN_VOL_DISPERSION_CRON_HOUR` | 7 | `app/config.py:544` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_vol_dispersion_cron_minute` | `BRAIN_VOL_DISPERSION_CRON_MINUTE` | 30 | `app/config.py:545` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `brain_vol_dispersion_cs_std_high` | `BRAIN_VOL_DISPERSION_CS_STD_HIGH` | 0.025 | `app/config.py:567` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_cs_std_low` | `BRAIN_VOL_DISPERSION_CS_STD_LOW` | 0.012 | `app/config.py:566` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_lookback_days` | `BRAIN_VOL_DISPERSION_LOOKBACK_DAYS` | 14 | `app/config.py:572` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_min_bars` | `BRAIN_VOL_DISPERSION_MIN_BARS` | 60 | `app/config.py:548` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_min_coverage_score` | `BRAIN_VOL_DISPERSION_MIN_COVERAGE_SCORE` | 0.5 | `app/config.py:553` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_mode` | `BRAIN_VOL_DISPERSION_MODE` | 'shadow' | `app/config.py:542` | String / URL / identifier; infrastructure. | — | — |
| `brain_vol_dispersion_ops_log_enabled` | `BRAIN_VOL_DISPERSION_OPS_LOG_ENABLED` | True | `app/config.py:543` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_vol_dispersion_realized_vol_high` | `BRAIN_VOL_DISPERSION_REALIZED_VOL_HIGH` | 0.3 | `app/config.py:564` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_realized_vol_low` | `BRAIN_VOL_DISPERSION_REALIZED_VOL_LOW` | 0.12 | `app/config.py:563` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_universe_cap` | `BRAIN_VOL_DISPERSION_UNIVERSE_CAP` | 60 | `app/config.py:556` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_vixy_high` | `BRAIN_VOL_DISPERSION_VIXY_HIGH` | 22.0 | `app/config.py:560` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_vixy_low` | `BRAIN_VOL_DISPERSION_VIXY_LOW` | 14.0 | `app/config.py:559` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_vol_dispersion_vixy_spike` | `BRAIN_VOL_DISPERSION_VIXY_SPIKE` | 30.0 | `app/config.py:561` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_work_dispatch_batch_size` | `BRAIN_WORK_DISPATCH_BATCH_SIZE` | 8 | `app/config.py:237` | Infrastructure / connectivity / tuning bucket (`'batch_size'`). | — | — |
| `brain_work_exec_feedback_batch_size` | `BRAIN_WORK_EXEC_FEEDBACK_BATCH_SIZE` | 3 | `app/config.py:246` | Infrastructure / connectivity / tuning bucket (`'batch_size'`). | — | — |
| `brain_work_exec_feedback_debounce_seconds` | `BRAIN_WORK_EXEC_FEEDBACK_DEBOUNCE_SECONDS` | 45 | `app/config.py:247` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_work_lease_seconds` | `BRAIN_WORK_LEASE_SECONDS` | 900 | `app/config.py:238` | Infrastructure / connectivity / tuning bucket (`'lease_seconds'`). | — | — |
| `brain_work_ledger_enabled` | `BRAIN_WORK_LEDGER_ENABLED` | True | `app/config.py:236` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_work_max_attempts_default` | `BRAIN_WORK_MAX_ATTEMPTS_DEFAULT` | 5 | `app/config.py:239` | Numeric tuning / limit; not a mode flag. | — | — |
| `brain_work_retry_base_seconds` | `BRAIN_WORK_RETRY_BASE_SECONDS` | 30 | `app/config.py:240` | Infrastructure / connectivity / tuning bucket (`'retry_'`). | — | — |
| `brain_work_retry_multiplier` | `BRAIN_WORK_RETRY_MULTIPLIER` | 2 | `app/config.py:241` | Infrastructure / connectivity / tuning bucket (`'retry_'`). | — | — |
| `brain_work_snapshots_outcome_enabled` | `BRAIN_WORK_SNAPSHOTS_OUTCOME_ENABLED` | True | `app/config.py:249` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `brain_worker_compose_project` | `BRAIN_WORKER_COMPOSE_PROJECT` | '' | `app/config.py:919` | String / URL / identifier; infrastructure. | — | — |
| `brain_worker_compose_service` | `BRAIN_WORKER_COMPOSE_SERVICE` | 'brain-worker' | `app/config.py:917` | String / URL / identifier; infrastructure. | — | — |
| `chili_auto_execute_stops` | `CHILI_AUTO_EXECUTE_STOPS` | False | `app/config.py:1711` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_autopilot_primary` | `CHILI_AUTOPILOT_PRIMARY` | 'momentum_neural' | `app/config.py:1447` | String / URL / identifier; infrastructure. | — | — |
| `chili_autopilot_strict_primary` | `CHILI_AUTOPILOT_STRICT_PRIMARY` | False | `app/config.py:1455` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_coinbase_market_data_max_age_sec` | `CHILI_COINBASE_MARKET_DATA_MAX_AGE_SEC` | 15.0 | `app/config.py:1012` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_coinbase_strict_freshness` | `CHILI_COINBASE_STRICT_FRESHNESS` | True | `app/config.py:1008` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `chili_cpcv_embargo_frac` | `CHILI_CPCV_EMBARGO_FRAC` | 0.02 | `app/config.py:800` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_cpcv_full_confidence_min_trades` | `CHILI_CPCV_FULL_CONFIDENCE_MIN_TRADES` | 30 | `app/config.py:806` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_cpcv_max_labeled_rows` | `CHILI_CPCV_MAX_LABELED_ROWS` | 0 | `app/config.py:797` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_cpcv_min_trades` | `CHILI_CPCV_MIN_TRADES` | 15 | `app/config.py:802` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_cpcv_n_paths_full_min` | `CHILI_CPCV_N_PATHS_FULL_MIN` | 50 | `app/config.py:809` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_cpcv_n_paths_provisional_min` | `CHILI_CPCV_N_PATHS_PROVISIONAL_MIN` | 20 | `app/config.py:808` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_cpcv_purge_frac` | `CHILI_CPCV_PURGE_FRAC` | 0.05 | `app/config.py:799` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_cpcv_target_paths_max` | `CHILI_CPCV_TARGET_PATHS_MAX` | 100 | `app/config.py:804` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_feature_parity_alert_on_warn` | `CHILI_FEATURE_PARITY_ALERT_ON_WARN` | True | `app/config.py:1665` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `chili_feature_parity_critical_mismatch_count` | `CHILI_FEATURE_PARITY_CRITICAL_MISMATCH_COUNT` | 3 | `app/config.py:1656` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_feature_parity_epsilon_abs` | `CHILI_FEATURE_PARITY_EPSILON_ABS` | 1e-06 | `app/config.py:1640` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_feature_parity_epsilon_rel` | `CHILI_FEATURE_PARITY_EPSILON_REL` | 0.005 | `app/config.py:1648` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_feature_parity_mode` | `CHILI_FEATURE_PARITY_MODE` | 'soft' | `app/config.py:1634` | String / URL / identifier; infrastructure. | — | — |
| `chili_global_max_daily_loss_pct_of_equity` | `CHILI_GLOBAL_MAX_DAILY_LOSS_PCT_OF_EQUITY` | 0.02 | `app/config.py:1162` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_global_max_daily_loss_usd` | `CHILI_GLOBAL_MAX_DAILY_LOSS_USD` | 300.0 | `app/config.py:1156` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_modules` | `CHILI_MODULES` | 'planner,intercom,voice,projects' | `app/config.py:112` | Infrastructure / connectivity / tuning bucket (`'modules'`). | — | — |
| `chili_momentum_ab_test_on_refinement` | `CHILI_MOMENTUM_AB_TEST_ON_REFINEMENT` | False | `app/config.py:937` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_momentum_family_regime_prefilter_enabled` | `CHILI_MOMENTUM_FAMILY_REGIME_PREFILTER_ENABLED` | False | `app/config.py:947` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_momentum_risk_auto_expire_pending_live_arm_seconds` | `CHILI_MOMENTUM_RISK_AUTO_EXPIRE_PENDING_LIVE_ARM_SECONDS` | 900.0 | `app/config.py:1129` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_block_paper_when_kill_switch` | `CHILI_MOMENTUM_RISK_BLOCK_PAPER_WHEN_KILL_SWITCH` | False | `app/config.py:1125` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_momentum_risk_cooldown_after_cancel_seconds` | `CHILI_MOMENTUM_RISK_COOLDOWN_AFTER_CANCEL_SECONDS` | 60 | `app/config.py:1094` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_cooldown_after_stopout_seconds` | `CHILI_MOMENTUM_RISK_COOLDOWN_AFTER_STOPOUT_SECONDS` | 300 | `app/config.py:1089` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_disable_live_if_governance_inhibit` | `CHILI_MOMENTUM_RISK_DISABLE_LIVE_IF_GOVERNANCE_INHIBIT` | True | `app/config.py:1121` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `chili_momentum_risk_max_concurrent_live_sessions` | `CHILI_MOMENTUM_MAX_CONCURRENT_LIVE_SESSIONS, CHILI_MOMENTUM_RISK_MAX_CONCURRENT_LIVE_SESSIONS` | 1 | `app/config.py:1041` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_concurrent_positions` | `CHILI_MOMENTUM_RISK_MAX_CONCURRENT_POSITIONS` | 3 | `app/config.py:1047` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_concurrent_sessions` | `CHILI_MOMENTUM_RISK_MAX_CONCURRENT_SESSIONS` | 6 | `app/config.py:1035` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_daily_loss_usd` | `CHILI_MOMENTUM_MAX_DAILY_LOSS_USD, CHILI_MOMENTUM_RISK_MAX_DAILY_LOSS_USD` | 250.0 | `app/config.py:1025` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_estimated_slippage_bps` | `CHILI_MOMENTUM_MAX_ESTIMATED_SLIPPAGE_BPS, CHILI_MOMENTUM_RISK_MAX_ESTIMATED_SLIPPAGE_BPS` | 18.0 | `app/config.py:1073` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_fee_to_target_ratio` | `CHILI_MOMENTUM_RISK_MAX_FEE_TO_TARGET_RATIO` | 0.35 | `app/config.py:1078` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_hold_seconds` | `CHILI_MOMENTUM_MAX_HOLD_SECONDS, CHILI_MOMENTUM_RISK_MAX_HOLD_SECONDS` | 86400 | `app/config.py:1084` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_loss_per_trade_usd` | `CHILI_MOMENTUM_MAX_LOSS_PER_TRADE_USD, CHILI_MOMENTUM_RISK_MAX_LOSS_PER_TRADE_USD` | 50.0 | `app/config.py:1030` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_notional_per_trade_usd` | `CHILI_MOMENTUM_RISK_MAX_NOTIONAL_PER_TRADE_USD` | 500.0 | `app/config.py:1053` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_position_size_base` | `CHILI_MOMENTUM_RISK_MAX_POSITION_SIZE_BASE` | 1000000.0 | `app/config.py:1058` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_spread_bps_live` | `CHILI_MOMENTUM_MAX_SPREAD_BPS, CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE` | 12.0 | `app/config.py:1068` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_max_spread_bps_paper` | `CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_PAPER` | 28.0 | `app/config.py:1063` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_require_fresh_viability` | `CHILI_MOMENTUM_RISK_REQUIRE_FRESH_VIABILITY` | True | `app/config.py:1113` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `chili_momentum_risk_require_live_eligible` | `CHILI_MOMENTUM_RISK_REQUIRE_LIVE_ELIGIBLE` | True | `app/config.py:1109` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `chili_momentum_risk_require_strict_coinbase_freshness` | `CHILI_MOMENTUM_RISK_REQUIRE_STRICT_COINBASE_FRESHNESS` | False | `app/config.py:1117` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_momentum_risk_stale_market_data_max_age_sec` | `CHILI_MOMENTUM_RISK_STALE_MARKET_DATA_MAX_AGE_SEC` | 30.0 | `app/config.py:1104` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_momentum_risk_viability_max_age_seconds` | `CHILI_MOMENTUM_RISK_VIABILITY_MAX_AGE_SECONDS` | 600.0 | `app/config.py:1099` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_regime_classifier_n_iter` | `CHILI_REGIME_CLASSIFIER_N_ITER` | 200 | `app/config.py:820` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_regime_classifier_random_state` | `CHILI_REGIME_CLASSIFIER_RANDOM_STATE` | 42 | `app/config.py:819` | Infrastructure / connectivity / tuning bucket (`'regime_classifier_random'`). | — | — |
| `chili_regime_classifier_weekly_cron_dow` | `CHILI_REGIME_CLASSIFIER_WEEKLY_CRON_DOW` | 'sun' | `app/config.py:821` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `chili_regime_classifier_weekly_cron_hour` | `CHILI_REGIME_CLASSIFIER_WEEKLY_CRON_HOUR` | 4 | `app/config.py:822` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `chili_regime_classifier_weekly_cron_minute` | `CHILI_REGIME_CLASSIFIER_WEEKLY_CRON_MINUTE` | 15 | `app/config.py:823` | Infrastructure / connectivity / tuning bucket (`'cron_'`). | — | — |
| `chili_scheduler_role` | `CHILI_SCHEDULER_ROLE` | 'all' | `app/config.py:905` | Infrastructure / connectivity / tuning bucket (`'scheduler_role'`). | — | — |
| `chili_scheduler_runs_externally` | `CHILI_SCHEDULER_RUNS_EXTERNALLY` | False | `app/config.py:911` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_venue_health_ack_to_fill_p95_ms` | `CHILI_VENUE_HEALTH_ACK_TO_FILL_P95_MS` | 5000 | `app/config.py:1516` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_venue_health_auto_switch_to_paper` | `CHILI_VENUE_HEALTH_AUTO_SWITCH_TO_PAPER` | False | `app/config.py:1542` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_venue_health_enabled` | `CHILI_VENUE_HEALTH_ENABLED` | False | `app/config.py:1491` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `chili_venue_health_error_rate_pct` | `CHILI_VENUE_HEALTH_ERROR_RATE_PCT` | 0.1 | `app/config.py:1532` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_venue_health_min_samples` | `CHILI_VENUE_HEALTH_MIN_SAMPLES` | 5 | `app/config.py:1507` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_venue_health_submit_to_ack_p95_ms` | `CHILI_VENUE_HEALTH_SUBMIT_TO_ACK_P95_MS` | 3000 | `app/config.py:1523` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_venue_health_window_sec` | `CHILI_VENUE_HEALTH_WINDOW_SEC` | 300 | `app/config.py:1498` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_venue_idempotency_ttl_hours_crypto` | `CHILI_VENUE_IDEMPOTENCY_TTL_HOURS_CRYPTO` | 48.0 | `app/config.py:1140` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_venue_idempotency_ttl_hours_equities` | `CHILI_VENUE_IDEMPOTENCY_TTL_HOURS_EQUITIES` | 168.0 | `app/config.py:1146` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_walk_forward_embargo_days` | `CHILI_WALK_FORWARD_EMBARGO_DAYS` | 2 | `app/config.py:1585` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_walk_forward_min_fold_win_rate` | `CHILI_WALK_FORWARD_MIN_FOLD_WIN_RATE` | 0.45 | `app/config.py:1603` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_walk_forward_min_folds` | `CHILI_WALK_FORWARD_MIN_FOLDS` | 3 | `app/config.py:1594` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_walk_forward_min_pass_fraction` | `CHILI_WALK_FORWARD_MIN_PASS_FRACTION` | 0.6 | `app/config.py:1612` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_walk_forward_step_days` | `CHILI_WALK_FORWARD_STEP_DAYS` | 30 | `app/config.py:1575` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_walk_forward_test_days` | `CHILI_WALK_FORWARD_TEST_DAYS` | 30 | `app/config.py:1567` | Numeric tuning / limit; not a mode flag. | — | — |
| `chili_walk_forward_train_days` | `CHILI_WALK_FORWARD_TRAIN_DAYS` | 180 | `app/config.py:1559` | Numeric tuning / limit; not a mode flag. | — | — |
| `code_brain_interval_hours` | `CODE_BRAIN_INTERVAL_HOURS` | 4 | `app/config.py:843` | Infrastructure / connectivity / tuning bucket (`'interval_'`). | — | — |
| `code_brain_max_files` | `CODE_BRAIN_MAX_FILES` | 5000 | `app/config.py:844` | Numeric tuning / limit; not a mode flag. | — | — |
| `code_brain_repos` | `CODE_BRAIN_REPOS` | '' | `app/config.py:842` | String / URL / identifier; infrastructure. | — | — |
| `coding_validation_step_timeout_seconds` | `CODING_VALIDATION_STEP_TIMEOUT_SECONDS` | 120 | `app/config.py:862` | Infrastructure / connectivity / tuning bucket (`'timeout'`). | — | — |
| `coinbase_api_key` | `COINBASE_API_KEY` | '' | `app/config.py:158` | Infrastructure / connectivity / tuning bucket (`'api_key'`). | — | — |
| `coinbase_api_secret` | `COINBASE_API_SECRET` | '' | `app/config.py:159` | Infrastructure / connectivity / tuning bucket (`'secret'`). | — | — |
| `database_max_overflow` | `DATABASE_MAX_OVERFLOW` | 55 | `app/config.py:880` | Infrastructure / connectivity / tuning bucket (`'database_'`). | — | — |
| `database_pool_size` | `DATABASE_POOL_SIZE` | 25 | `app/config.py:879` | Infrastructure / connectivity / tuning bucket (`'database_'`). | — | — |
| `database_url` | `DATABASE_URL` | None | `app/config.py:871` | Infrastructure / connectivity / tuning bucket (`'database_'`). | — | — |
| `desktop_refinement_enabled` | `DESKTOP_REFINEMENT_ENABLED` | True | `app/config.py:122` | Infrastructure / connectivity / tuning bucket (`'desktop_refinement'`). | — | — |
| `discord_webhook_url` | `DISCORD_WEBHOOK_URL` | '' | `app/config.py:142` | Infrastructure / connectivity / tuning bucket (`'_url'`). | — | — |
| `email_password` | `EMAIL_PASSWORD` | '' | `app/config.py:96` | Infrastructure / connectivity / tuning bucket (`'password'`). | — | — |
| `email_user` | `EMAIL_USER` | '' | `app/config.py:95` | Infrastructure / connectivity / tuning bucket (`'email_user'`). | — | — |
| `google_client_id` | `GOOGLE_CLIENT_ID` | '' | `app/config.py:149` | Infrastructure / connectivity / tuning bucket (`'google_client'`). | — | — |
| `google_client_secret` | `GOOGLE_CLIENT_SECRET` | '' | `app/config.py:150` | Infrastructure / connectivity / tuning bucket (`'secret'`). | — | — |
| `learning_cycle_report_llm_every_n` | `LEARNING_CYCLE_REPORT_LLM_EVERY_N` | 0 | `app/config.py:747` | Numeric tuning / limit; not a mode flag. | — | — |
| `learning_cycle_stale_seconds` | `LEARNING_CYCLE_STALE_SECONDS` | 10800 | `app/config.py:186` | Infrastructure / connectivity / tuning bucket (`'learning_cycle_stale'`). | — | — |
| `learning_interval_hours` | `LEARNING_INTERVAL_HOURS` | 1 | `app/config.py:183` | Infrastructure / connectivity / tuning bucket (`'interval_'`). | — | — |
| `llm_api_key` | `LLM_API_KEY` | '' | `app/config.py:62` | Infrastructure / connectivity / tuning bucket (`'api_key'`). | — | — |
| `llm_base_url` | `LLM_BASE_URL` | 'https://api.groq.com/openai/v1' | `app/config.py:66` | Infrastructure / connectivity / tuning bucket (`'_url'`). | — | — |
| `llm_cache_max_entries` | `LLM_CACHE_MAX_ENTRIES` | 256 | `app/config.py:83` | Numeric tuning / limit; not a mode flag. | — | — |
| `llm_cache_ttl_seconds` | `LLM_CACHE_TTL_SECONDS` | 600 | `app/config.py:84` | Numeric tuning / limit; not a mode flag. | — | — |
| `llm_free_tier_first` | `LLM_FREE_TIER_FIRST` | True | `app/config.py:79` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `llm_model` | `LLM_MODEL` | 'llama-3.3-70b-versatile' | `app/config.py:65` | String / URL / identifier; infrastructure. | — | — |
| `market_data_allow_provider_fallback` | `MARKET_DATA_ALLOW_PROVIDER_FALLBACK` | True | `app/config.py:180` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `massive_api_key` | `MASSIVE_API_KEY` | '' | `app/config.py:162` | Infrastructure / connectivity / tuning bucket (`'api_key'`). | — | — |
| `massive_base_url` | `MASSIVE_BASE_URL` | 'https://api.massive.com' | `app/config.py:163` | Infrastructure / connectivity / tuning bucket (`'_url'`). | — | — |
| `massive_http_pool_connections` | `MASSIVE_HTTP_POOL_CONNECTIONS` | 128 | `app/config.py:169` | Infrastructure / connectivity / tuning bucket (`'pool_'`). | — | — |
| `massive_http_pool_maxsize` | `MASSIVE_HTTP_POOL_MAXSIZE` | 512 | `app/config.py:170` | Infrastructure / connectivity / tuning bucket (`'pool_'`). | — | — |
| `massive_max_rps` | `MASSIVE_MAX_RPS` | 100 | `app/config.py:166` | Infrastructure / connectivity / tuning bucket (`'massive_'`). | — | — |
| `massive_use_websocket` | `MASSIVE_USE_WEBSOCKET` | True | `app/config.py:165` | Infrastructure / connectivity / tuning bucket (`'massive_'`). | — | — |
| `massive_ws_url` | `MASSIVE_WS_URL` | 'wss://socket.massive.com' | `app/config.py:164` | Infrastructure / connectivity / tuning bucket (`'_url'`). | — | — |
| `module_registry_url` | `MODULE_REGISTRY_URL` | '' | `app/config.py:108` | Infrastructure / connectivity / tuning bucket (`'_url'`). | — | — |
| `ollama_host` | `OLLAMA_HOST` | 'http://127.0.0.1:11434' | `app/config.py:56` | Infrastructure / connectivity / tuning bucket (`'_host'`). | — | — |
| `ollama_model` | `OLLAMA_MODEL` | 'phi4-mini' | `app/config.py:57` | String / URL / identifier; infrastructure. | — | — |
| `ollama_vision_model` | `OLLAMA_VISION_MODEL` | 'llama3.2-vision' | `app/config.py:59` | String / URL / identifier; infrastructure. | — | — |
| `openai_api_key` | `OPENAI_API_KEY` | '' | `app/config.py:64` | Infrastructure / connectivity / tuning bucket (`'api_key'`). | — | — |
| `openai_daily_token_limit` | `OPENAI_DAILY_TOKEN_LIMIT` | 0 | `app/config.py:88` | Infrastructure / connectivity / tuning bucket (`'token'`). | — | — |
| `openai_vision_model` | `OPENAI_VISION_MODEL` | 'gpt-4o-mini' | `app/config.py:92` | String / URL / identifier; infrastructure. | — | — |
| `opportunity_board_max_prescreener_fallback` | `OPPORTUNITY_BOARD_MAX_PRESCREENER_FALLBACK` | 8 | `app/config.py:2212` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_board_max_scanner_fallback` | `OPPORTUNITY_BOARD_MAX_SCANNER_FALLBACK` | 6 | `app/config.py:2211` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_board_max_ticker_scores_per_request` | `OPPORTUNITY_BOARD_MAX_TICKER_SCORES_PER_REQUEST` | 360 | `app/config.py:2210` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_board_max_tickers_per_pattern` | `OPPORTUNITY_BOARD_MAX_TICKERS_PER_PATTERN` | 10 | `app/config.py:2209` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_board_max_universe_cap` | `OPPORTUNITY_BOARD_MAX_UNIVERSE_CAP` | 80 | `app/config.py:2208` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_board_scanner_fallback_min_score_b` | `OPPORTUNITY_BOARD_SCANNER_FALLBACK_MIN_SCORE_B` | 6.5 | `app/config.py:2213` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_board_stale_seconds` | `OPPORTUNITY_BOARD_STALE_SECONDS` | 180 | `app/config.py:2206` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_max_tier_a` | `OPPORTUNITY_MAX_TIER_A` | 3 | `app/config.py:2225` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_max_tier_b` | `OPPORTUNITY_MAX_TIER_B` | 5 | `app/config.py:2226` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_max_tier_c` | `OPPORTUNITY_MAX_TIER_C` | 8 | `app/config.py:2227` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_max_tier_d` | `OPPORTUNITY_MAX_TIER_D` | 12 | `app/config.py:2228` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_tier_a_min_composite` | `OPPORTUNITY_TIER_A_MIN_COMPOSITE` | 0.48 | `app/config.py:2219` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_tier_a_min_coverage` | `OPPORTUNITY_TIER_A_MIN_COVERAGE` | 0.5 | `app/config.py:2220` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_tier_b_max_eta_hours` | `OPPORTUNITY_TIER_B_MAX_ETA_HOURS` | 4.0 | `app/config.py:2223` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_tier_b_min_composite` | `OPPORTUNITY_TIER_B_MIN_COMPOSITE` | 0.38 | `app/config.py:2221` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_tier_b_min_coverage` | `OPPORTUNITY_TIER_B_MIN_COVERAGE` | 0.35 | `app/config.py:2222` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_tier_c_min_composite` | `OPPORTUNITY_TIER_C_MIN_COMPOSITE` | 0.28 | `app/config.py:2224` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_weight_coverage` | `OPPORTUNITY_WEIGHT_COVERAGE` | 0.22 | `app/config.py:2230` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_weight_eta` | `OPPORTUNITY_WEIGHT_ETA` | 0.15 | `app/config.py:2233` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_weight_pattern_quality` | `OPPORTUNITY_WEIGHT_PATTERN_QUALITY` | 0.22 | `app/config.py:2231` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_weight_readiness` | `OPPORTUNITY_WEIGHT_READINESS` | 0.28 | `app/config.py:2229` | Numeric tuning / limit; not a mode flag. | — | — |
| `opportunity_weight_risk_reward` | `OPPORTUNITY_WEIGHT_RISK_REWARD` | 0.13 | `app/config.py:2232` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_alert_enabled` | `PATTERN_IMMINENT_ALERT_ENABLED` | True | `app/config.py:2177` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `pattern_imminent_allow_evaluable_shortcut` | `PATTERN_IMMINENT_ALLOW_EVALUABLE_SHORTCUT` | True | `app/config.py:2192` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `pattern_imminent_cooldown_hours` | `PATTERN_IMMINENT_COOLDOWN_HOURS` | 3.0 | `app/config.py:2184` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_debug_dry_run` | `PATTERN_IMMINENT_DEBUG_DRY_RUN` | False | `app/config.py:2197` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `pattern_imminent_eta_scale_k` | `PATTERN_IMMINENT_ETA_SCALE_K` | 1.5 | `app/config.py:2188` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_evaluable_ratio_floor` | `PATTERN_IMMINENT_EVALUABLE_RATIO_FLOOR` | 0.35 | `app/config.py:2187` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_max_eta_hours` | `PATTERN_IMMINENT_MAX_ETA_HOURS` | 4.0 | `app/config.py:2178` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_max_per_pattern_per_run` | `PATTERN_IMMINENT_MAX_PER_PATTERN_PER_RUN` | 3 | `app/config.py:2194` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_max_per_run` | `PATTERN_IMMINENT_MAX_PER_RUN` | 12 | `app/config.py:2183` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_max_per_ticker_per_run` | `PATTERN_IMMINENT_MAX_PER_TICKER_PER_RUN` | 2 | `app/config.py:2193` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_max_prediction_tickers` | `PATTERN_IMMINENT_MAX_PREDICTION_TICKERS` | 40 | `app/config.py:2202` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_max_prescreener_tickers` | `PATTERN_IMMINENT_MAX_PRESCREENER_TICKERS` | 80 | `app/config.py:2201` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_max_scanner_tickers` | `PATTERN_IMMINENT_MAX_SCANNER_TICKERS` | 50 | `app/config.py:2203` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_max_tickers_per_run` | `PATTERN_IMMINENT_MAX_TICKERS_PER_RUN` | 160 | `app/config.py:2185` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_min_composite_main` | `PATTERN_IMMINENT_MIN_COMPOSITE_MAIN` | 0.42 | `app/config.py:2191` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_min_feature_coverage_main` | `PATTERN_IMMINENT_MIN_FEATURE_COVERAGE_MAIN` | 0.45 | `app/config.py:2190` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_min_readiness` | `PATTERN_IMMINENT_MIN_READINESS` | 0.58 | `app/config.py:2181` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_readiness_cap` | `PATTERN_IMMINENT_READINESS_CAP` | 0.995 | `app/config.py:2182` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_research_mode` | `PATTERN_IMMINENT_RESEARCH_MODE` | False | `app/config.py:2195` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `pattern_imminent_research_nearmiss_log` | `PATTERN_IMMINENT_RESEARCH_NEARMISS_LOG` | False | `app/config.py:2196` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `pattern_imminent_scope_tickers_cap` | `PATTERN_IMMINENT_SCOPE_TICKERS_CAP` | 32 | `app/config.py:2186` | Numeric tuning / limit; not a mode flag. | — | — |
| `pattern_imminent_use_predictions_universe` | `PATTERN_IMMINENT_USE_PREDICTIONS_UNIVERSE` | True | `app/config.py:2199` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `pattern_imminent_use_prescreener_universe` | `PATTERN_IMMINENT_USE_PRESCREENER_UNIVERSE` | True | `app/config.py:2198` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `pattern_imminent_use_scanner_universe` | `PATTERN_IMMINENT_USE_SCANNER_UNIVERSE` | True | `app/config.py:2200` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `pick_warn_drift_pct` | `PICK_WARN_DRIFT_PCT` | 10.0 | `app/config.py:867` | Infrastructure / connectivity / tuning bucket (`'pick_warn'`). | — | — |
| `polygon_api_key` | `POLYGON_API_KEY` | '' | `app/config.py:173` | Infrastructure / connectivity / tuning bucket (`'api_key'`). | — | — |
| `polygon_base_url` | `POLYGON_BASE_URL` | 'https://api.polygon.io' | `app/config.py:174` | Infrastructure / connectivity / tuning bucket (`'_url'`). | — | — |
| `polygon_max_rps` | `POLYGON_MAX_RPS` | 5 | `app/config.py:176` | Infrastructure / connectivity / tuning bucket (`'polygon_'`). | — | — |
| `premium_api_key` | `PREMIUM_API_KEY` | '' | `app/config.py:70` | Infrastructure / connectivity / tuning bucket (`'api_key'`). | — | — |
| `premium_base_url` | `PREMIUM_BASE_URL` | 'https://generativelanguage.googleapis.com/v1beta/openai/' | `app/config.py:72` | Infrastructure / connectivity / tuning bucket (`'_url'`). | — | — |
| `premium_daily_token_limit` | `PREMIUM_DAILY_TOKEN_LIMIT` | 0 | `app/config.py:89` | Infrastructure / connectivity / tuning bucket (`'token'`). | — | — |
| `premium_model` | `PREMIUM_MODEL` | 'gemini-2.0-flash' | `app/config.py:71` | String / URL / identifier; infrastructure. | — | — |
| `project_brain_auto_cycle_minutes` | `PROJECT_BRAIN_AUTO_CYCLE_MINUTES` | 60 | `app/config.py:855` | Numeric tuning / limit; not a mode flag. | — | — |
| `project_brain_chat_context_enabled` | `PROJECT_BRAIN_CHAT_CONTEXT_ENABLED` | False | `app/config.py:859` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `project_brain_enabled` | `PROJECT_BRAIN_ENABLED` | True | `app/config.py:854` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `project_brain_max_web_searches` | `PROJECT_BRAIN_MAX_WEB_SEARCHES` | 5 | `app/config.py:856` | Numeric tuning / limit; not a mode flag. | — | — |
| `project_brain_scheduler_enabled` | `PROJECT_BRAIN_SCHEDULER_ENABLED` | False | `app/config.py:858` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `project_domain_enabled` | `PROJECT_DOMAIN_ENABLED` | True | `app/config.py:119` | Infrastructure / connectivity / tuning bucket (`'project_domain'`). | — | — |
| `proposal_warn_age_min` | `PROPOSAL_WARN_AGE_MIN` | 60 | `app/config.py:866` | Infrastructure / connectivity / tuning bucket (`'proposal_warn'`). | — | — |
| `reasoning_enabled` | `REASONING_ENABLED` | True | `app/config.py:849` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `reasoning_insight_chat_enabled` | `REASONING_INSIGHT_CHAT_ENABLED` | True | `app/config.py:850` | Boolean default-on (product/research infra); not a Q1.T8 diagnostic flip target. | — | — |
| `reasoning_interval_hours` | `REASONING_INTERVAL_HOURS` | 6 | `app/config.py:847` | Infrastructure / connectivity / tuning bucket (`'interval_'`). | — | — |
| `reasoning_max_goals` | `REASONING_MAX_GOALS` | 5 | `app/config.py:851` | Numeric tuning / limit; not a mode flag. | — | — |
| `reasoning_max_web_searches` | `REASONING_MAX_WEB_SEARCHES` | 10 | `app/config.py:848` | Numeric tuning / limit; not a mode flag. | — | — |
| `robinhood_password` | `ROBINHOOD_PASSWORD` | '' | `app/config.py:156` | Infrastructure / connectivity / tuning bucket (`'password'`). | — | — |
| `robinhood_totp_secret` | `ROBINHOOD_TOTP_SECRET` | '' | `app/config.py:157` | Infrastructure / connectivity / tuning bucket (`'secret'`). | — | — |
| `robinhood_username` | `ROBINHOOD_USERNAME` | '' | `app/config.py:155` | Infrastructure / connectivity / tuning bucket (`'robinhood_'`). | — | — |
| `session_secret` | `SESSION_SECRET` | 'chili-session-change-me' | `app/config.py:151` | Infrastructure / connectivity / tuning bucket (`'session_'`). | — | — |
| `sms_carrier` | `SMS_CARRIER` | 'verizon' | `app/config.py:129` | Infrastructure / connectivity / tuning bucket (`'sms_'`). | — | — |
| `sms_phone` | `SMS_PHONE` | '' | `app/config.py:128` | Infrastructure / connectivity / tuning bucket (`'sms_'`). | — | — |
| `smtp_host` | `SMTP_HOST` | 'smtp.gmail.com' | `app/config.py:97` | Infrastructure / connectivity / tuning bucket (`'smtp_'`). | — | — |
| `smtp_port` | `SMTP_PORT` | 587 | `app/config.py:98` | Infrastructure / connectivity / tuning bucket (`'smtp_'`). | — | — |
| `staging_database_url` | `STAGING_DATABASE_URL, staging_database_url` | '' | `app/config.py:873` | Infrastructure / connectivity / tuning bucket (`'database_'`). | — | — |
| `telegram_bot_token` | `TELEGRAM_BOT_TOKEN` | '' | `app/config.py:138` | Infrastructure / connectivity / tuning bucket (`'token'`). | — | — |
| `telegram_chat_id` | `TELEGRAM_CHAT_ID` | '' | `app/config.py:139` | Infrastructure / connectivity / tuning bucket (`'telegram_'`). | — | — |
| `top_picks_warn_age_min` | `TOP_PICKS_WARN_AGE_MIN` | 15 | `app/config.py:865` | Infrastructure / connectivity / tuning bucket (`'top_picks_warn'`). | — | — |
| `trading_inspect_bearer_secret` | `CHILI_TRADING_INSPECT_SECRET, TRADING_INSPECT_BEARER_SECRET` | '' | `app/config.py:2215` | Infrastructure / connectivity / tuning bucket (`'secret'`). | — | — |
| `twilio_account_sid` | `TWILIO_ACCOUNT_SID` | '' | `app/config.py:133` | Infrastructure / connectivity / tuning bucket (`'twilio_'`). | — | — |
| `twilio_auth_token` | `TWILIO_AUTH_TOKEN` | '' | `app/config.py:134` | Infrastructure / connectivity / tuning bucket (`'token'`). | — | — |
| `twilio_phone_number` | `TWILIO_PHONE_NUMBER` | '' | `app/config.py:135` | Infrastructure / connectivity / tuning bucket (`'twilio_'`). | — | — |
| `use_polygon` | `USE_POLYGON` | False | `app/config.py:175` | Boolean default-off feature toggle; not in Q1.T8 diagnostic flip set — enable per feature rollout doc. | — | — |
| `vapid_contact_email` | `VAPID_CONTACT_EMAIL` | '' | `app/config.py:146` | Infrastructure / connectivity / tuning bucket (`'vapid'`). | — | — |
| `vapid_private_key` | `VAPID_PRIVATE_KEY` | '' | `app/config.py:145` | Infrastructure / connectivity / tuning bucket (`'vapid'`). | — | — |
| `weather_location` | `WEATHER_LOCATION` | '' | `app/config.py:101` | Infrastructure / connectivity / tuning bucket (`'weather_'`). | — | — |
| `wellness_model` | `WELLNESS_MODEL` | 'phi4-mini' | `app/config.py:58` | String / URL / identifier; infrastructure. | — | — |
| `zerox_api_key` | `ZEROX_API_KEY` | '' | `app/config.py:125` | Infrastructure / connectivity / tuning bucket (`'api_key'`). | — | — |

## Category 4 — Unknown / investigate

*(Empty after Q1.T8 follow-up investigation — see resolution below.)*

### Resolved: `brain_miner_scanpattern_bridge_enabled`

Investigation `app/services/trading/learning.py:4051-4098` and `:4283-4293`:

When ON, the intraday compression miner (`mine_intraday_patterns`) calls
`_bridge_compression_scanpattern_from_miner` after a successful discovery,
which enqueues exactly one `ScanPattern` row (`Brain miner: BB squeeze prescreen
(15m)`) in `prescreen` queue tier with `lifecycle_stage='candidate'` and
`promotion_status='pending_oos'`. The bridge is idempotent (no-op if the
named pattern already exists) and creates at most one row per cycle.

The created pattern still has to pass every downstream gate (OOS backtest,
ensemble promotion check, CPCV gate when enabled, kill switch, drawdown
breaker). The flag does NOT bypass safety; it only feeds the candidate funnel.

**Reclassification:** Category 1 (diagnostic-safe-to-flip).

**Recommended default:** `True`. Flipping ON only adds one candidate pattern
per intraday compression discovery; flipping OFF means the discovery is logged
but not enqueued, so a potentially-edge-bearing pattern never gets evaluated
through the OOS pipeline. There is no operational reason to keep it OFF.

Q1.T8 follow-up commit (this file + `app/config.py` default flip) lands the
re-classification.

## Counts (updated)
- **Category 1:** 5  *(was 4 + brain_miner_scanpattern_bridge_enabled)*
- **Category 2:** 91
- **Category 3:** 627
- **Category 4:** 0  *(was 1)*
- **Total `Settings` fields:** 723

*Generated by `scripts/build_feature_flag_audit.py`; regenerate after large config changes.*
