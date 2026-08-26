# Dark-flag census — 2026-08-26

Bawat numero rito ay galing sa AST ng `app/config.py`, hindi sa hula.

| | bilang |
|---|---|
| kabuuang `bool` na flag sa `Settings` | **748** |
| naka-ON (`default=True`) | 429 |
| **DARK** (`default=False`) | **119** |
| dark sa MAINIT na landas (momentum/entry/exit/arm/runner) | **69** |
| └ buhay LAMANG dahil binubuksan ng env ng operator | 5 |
| └ **TALAGANG patay** | **64** |

## ⚠️ Ang mapanlinlang: DARK sa code, buhay sa env

Ang sinumang magbabasa ng repo ay maghihinuhang patay ang mga ito. Buhay lang sila
dahil sa `WINDOW_ENV` ng operator sa `timeshare_supervisor.py`.

- `chili_momentum_legacy_alpaca_dispatch_enabled` (config.py:6809)
- `chili_momentum_live_runner_enabled` (config.py:9018)
- `chili_momentum_live_runner_scheduler_enabled` (config.py:9022)
- `chili_momentum_auto_arm_equity_only` (config.py:10613)
- `chili_momentum_equity_execution_via_alpaca_paper` (config.py:11160)

> Kasama rito ang **`chili_momentum_live_runner_enabled`** — ang live runner MISMO ay
> ipinapadala nang `default=False`.

## Ang 64 na talagang patay sa mainit na landas

| flag | linya | description |
|---|---|---|
| `chili_autotrader_options_exit_monitor_enabled` | 13727 | (walang description) |
| `chili_drift_escalation_enabled` | 12317 | (walang description) |
| `chili_momentum_ab_test_on_refinement` | 2176 | (walang description) |
| `chili_momentum_add_into_halt_enabled` | 10307 | GAP 6 (RISKIEST — KEEP OFF UNTIL SOAKED): permit a SMALL pyramid ADD while the name is HALTED LIMIT-UP, gated by EVERY extra condition (fail- [...] |
| `chili_momentum_alpaca_orphan_reconcile_standalone_enabled` | 4844 | (walang description) |
| `chili_momentum_alpaca_size_use_buying_power` | 4764 | (walang description) |
| `chili_momentum_anticipation_starter_enabled` | 2513 | ANTICIPATION STARTER (probe-then-add): split the live entry into a small PROBE leg on the pivot break, then ADD the remainder after the position [...] |
| `chili_momentum_ask_thins_dip_entry_enabled` | 10143 | LOCATE #2 ASK-THINS-TO-ZERO DIP: an L2 ask-DEPLETION dip-bottom long. After a real dip (an ATR-scaled retrace that holds a structural higher- [...] |
| `chili_momentum_backside_veto_enabled` | 5043 | E1: veto an entry when the SESSION-anchored front_side_state reads backside (post-peak/declining lifecycle). Fail-open on unknown/thin data. [...] |
| `chili_momentum_bail_on_no_confirmation_enabled` | 8055 | Early bailout when a breakout shows no confirming strength inside the bounded confirmation window. DEFAULT OFF since 2026-07-31 (L2a golden- [...] |
| `chili_momentum_break_candle_adaptive_close_pos_enabled` | 6360 | Adaptive break-candle close-pos: when ON, an explosive break (RVOL >= chili_momentum_explosive_floor_rvol) relaxes the close-pos requirement [...] |
| `chili_momentum_broker_truth_label_enabled` | 3141 | Kill-switch: True => learning consumers read the broker-true label via authoritative_label_for_outcome (reconciled rows use broker PnL/bps; [...] |
| `chili_momentum_broker_truth_reconciliation_enabled` | 3136 | Kill-switch: True => the reconcile pass writes the authoritative broker-truth label columns on momentum_automation_outcomes (additive, never [...] |
| `chili_momentum_candle_quality_multitf_veto_enabled` | 9943 | Master enable for BOTH the doji veto and the HTF-against (multi-TF alignment) veto in pullback_break_confirmation. false = byte-identical (both [...] |
| `chili_momentum_crypto_execution_via_alpaca_paper` | 12696 | (walang description) |
| `chili_momentum_crypto_live_arm_enabled` | 9777 | (walang description) |
| `chili_momentum_dip_buy_rth_only_enabled` | 10270 | GAP 4 (bug fix): the flush-dip / deep-reclaim DIP-BUY only works 09:30-16:00 ET because stops fire then (there are NO stops premarket, so a [...] |
| `chili_momentum_entry_execution_bbo_ceiling_defer_enabled` | 9160 | Legacy behaviour: DEFER the entry when the execution-venue ask sits above the planned limit. OFF (default) places at the PLANNED limit instead [...] |
| `chili_momentum_entry_extension_rvol_boost_enabled` | 7997 | When ON (and master ON), BOOST the entry-extension cap for high-RVOL outlier squeezes by min(boost_max, boost_per * max(0, rvol - rvol_floor)). [...] |
| `chili_momentum_entry_l2_veto_enabled` | 9841 | Gate 3 (dip-buy quality): enable the L2 hidden-seller / big-seller entry veto (reuses read_ladder_distribution + OFI/micro). FAIL-OPEN on any [...] |
| `chili_momentum_entry_macd_open_strict` | 9799 | Gate 1 (dip-buy quality): inside the existing require_macd_bullish veto, require the MACD LINE strictly above SIGNAL instead of the lenient [...] |
| `chili_momentum_entry_tight_false_break_reclaim_enabled` | 7849 | GAP-B: enable the TIGHT-MOMENTUM false-breakout-reversal / VWAP-reclaim entry trigger family. OFF (default) ⇒ detector disabled before any [...] |
| `chili_momentum_exit_candle_confirm_live` | 7465 | Gate the lock's FLOW confluence on the 1m candle. Default OFF = observe-first: the LIVE lock decision is byte-identical and only the would- [...] |
| `chili_momentum_explosive_floor_enabled` | 5222 | E3: hard entry gate — an equity name must clear absolute RVOL + day-change floors (on top of percentile rank) to be live-eligible. [...] |
| `chili_momentum_family_regime_prefilter_enabled` | 2202 | (walang description) |
| `chili_momentum_first_dip_reclaim_enabled` | 2673 | Deprecated compatibility input retained for provenance only. It never changes the effective lifecycle mode, cannot activate candidate/promoted [...] |
| `chili_momentum_float_overrotation_fix_enabled` | 2279 | Ross SS101 EXHAUSTION fix: the deployed float-rotation sub-score rewards higher projected rotation MONOTONICALLY, but EXCESSIVE rotation (over- [...] |
| `chili_momentum_halt_chain_risk_gate_enabled` | 3497 | GAP 1: when ON, track a PER-SYMBOL consecutive halt-UP count; once it reaches chili_momentum_halt_chain_block_count, BLOCK the halt-resume-dip [...] |
| `chili_momentum_hard_no_trade_midday_enabled` | 7103 | Optional HARD midday no-NEW-ENTRY window (reuses the SAME 10:30-14:30 ET in_midday_lull band as the soft de-weight). Default OFF: the soft de- [...] |
| `chili_momentum_instant_bid_above_fill_confirm_enabled` | 10231 | LOCATE #7 INSTANT BID-ABOVE-FILL CONFIRM: the positive MIRROR of instant_bid_below_fill_cut. In the first [...] |
| `chili_momentum_instant_bid_below_fill_cut_enabled` | 8082 | GAP2: within the instant-cut window after entry, cut fast if the live bid has dropped below the fill price by more than the noise margin (entry [...] |
| `chili_momentum_l2_confirm_enabled` | 9981 | Phase-1 L2 entry CONFIRMER (DEFER-only): after the chart trigger + both existing vetoes pass, require the executed tape to confirm thrust [...] |
| `chili_momentum_live_runner_dev_tick_enabled` | 9083 | (walang description) |
| `chili_momentum_live_runner_loop_enabled` | 9029 | (walang description) |
| `chili_momentum_ma_vwap_pullback_enabled` | 5529 | SS101 #014: MOVING-AVERAGE / VWAP PULLBACK — after an impulse (3+ green candles) the name pulls back 2+ bars into a SIDEWAYS consolidation [...] |
| `chili_momentum_measured_move_exit_enabled` | 6866 | Kill-switch for the measured-move scale target + double-top exhaustion tighten (winner-management). false = inert no-op; the runner trails byte- [...] |
| `chili_momentum_order_burst_candle_guard_enabled` | 10254 | LOCATE #9 8AM BURST GUARD: a narrow time-windowed DISTRUST of the top-of-hour burst candle (esp. 08:00 ET) — within [...] |
| `chili_momentum_order_chunking_enabled` | 2530 | ORDER CHUNKING: a venue-adapter WRAPPER that splits a parent place_limit_order_gtc into N equal blocks for queue priority, each with a fresh [...] |
| `chili_momentum_overnight_trading_enabled` | 6644 | Tier 2 MASTER gate: allow overnight/24h arming+entry for 24h-ELIGIBLE, 24h-LIQUID names (RH all_day_hours routing). DEFAULT FALSE (higher risk: [...] |
| `chili_momentum_paper_runner_dev_tick_enabled` | 9012 | (walang description) |
| `chili_momentum_paper_runner_enabled` | 9004 | (walang description) |
| `chili_momentum_paper_runner_scheduler_enabled` | 9008 | (walang description) |
| `chili_momentum_per_symbol_fatigue_enabled` | 7038 | Kill-switch for per-symbol attempt fatigue (P2, entries-only). false = no per-symbol attempt count, no YELLOW down-size, no RED veto (byte-identical). |
| `chili_momentum_premarket_pivot_macd_entry_enabled` | 10220 | LOCATE #6 PREMARKET PIVOT + MACD: a premarket gap-and-go pivot break — price breaks a premarket pivot (the premarket swing high) WITH a fresh [...] |
| `chili_momentum_pulling_away_roc_entry_enabled` | 10209 | LOCATE #5 PULLING-AWAY ROC: a ROC-INFLECTION breakout — price tapped a multi-tap resistance >= chili_momentum_pulling_away_min_taps times (a [...] |
| `chili_momentum_pyramid_discrete_add_enabled` | 10394 | GAP 3 (RISK): require a FRESH DISCRETE entry sub-pattern (a new higher-low bounce off the rising EMA/VWAP) for a pyramid ADD, on top of the [...] |
| `chili_momentum_red_candle_entry_block_enabled` | 10265 | LOCATE #10 RED-CANDLE ENTRY BLOCK: do NOT fire a fresh entry while the CURRENT 1m (entry-interval) bar is RED (close < open) — Ross never buys [...] |
| `chili_momentum_replay_engine_on` | 6193 | (walang description) |
| `chili_momentum_replay_fidelity_v2` | 6180 | (walang description) |
| `chili_momentum_replay_full_pipeline_enabled` | 10937 | Replay armed_source='full_pipeline': re-run the real as-of selection pipeline (build_equity_universe re-screen → re-score → re-arm) from raw [...] |
| `chili_momentum_replay_prints_fill_enabled` | 6231 | (walang description) |
| `chili_momentum_replay_recorded_fills_enabled` | 6211 | (walang description) |
| `chili_momentum_replay_tick_entry_enabled` | 10927 | Replay every densified tick in the entry window (true sub-minute resolution where WS ticks exist; byte-identical where only 1-min snapshots [...] |
| `chili_momentum_risk_block_paper_when_kill_switch` | 8553 | (walang description) |
| `chili_momentum_risk_require_strict_coinbase_freshness` | 8545 | (walang description) |
| `chili_momentum_rulebreak_nextday_lockout_enabled` | 10333 | GAP 1 (RISK): when a discipline rule is broken today (daily-loss breach / trade-count budget exceeded / max-loss circuit fire), block LIVE [...] |
| `chili_momentum_second_day_context_enabled` | 3451 | Kill-switch for the P1 second-day/multi-day continuation selection tilt (equities). false = no day-2 boost, no day-3+ derate; the run/level [...] |
| `chili_momentum_short_enabled` | 11033 | (walang description) |
| `chili_momentum_short_lane_enabled` | 11171 | Master gate for the Alpaca SHORT lane. Default OFF (paper-first, un-soaked, no triggers wired yet). OFF ⇒ byte-identical long-only lane. |
| `chili_momentum_smart_hold_enabled` | 7770 | GAP-A: replace the fixed 0.001 fast-bail wick buffer with a vol-adaptive hold band + flow/volume/time-floor CUT gating, governing only the first [...] |
| `chili_momentum_stopout_cooldown_timer_enabled` | 8392 | 2026-07-04: when FALSE (default) the fixed wall-clock stop-out re-entry timer is REMOVED — the session recycles to WATCHING immediately and re- [...] |
| `chili_momentum_sub5min_scalp_bailout_enabled` | 10132 | LOCATE #1 SUB-5MIN SCALP BAILOUT: a scalp-family fast time-stop. When the deployed cadence classifier (_classify_cadence) reports a SLOW_CHOPPER [...] |
| `chili_momentum_timeofday_schedule_enabled` | 9535 | Kill-switch for the time-of-day schedule (prime-window size lever + fade-driven late-day NEW-entry cutoff), NEW-INITIATION ONLY. OFF (default) [...] |
| `chili_momentum_win_cycle_fatigue_enabled` | 6892 | Kill-switch for win-cycle fatigue (E2, entries-only). false = no win count, no YELLOW down-size, no RED halt (byte-identical). |

## Bakit ito mahalaga

Tatlong beses ngayong 2026-08-26 ang isang dark flag ang naging harang:

1. `exit_ladder_live` + 2 kapatid — naka-OFF na naghihintay ng A/B na **hindi kailanman darating**
   (ZERO counterfactual event sa 30 araw dahil walang trade na maoobserbahan). Capture ratio 18.5%.
   Binuksan sa PR #1185.
2. `event_based_abandonment_enabled` — ang **description** ay nagsasabing "OFF (default)"
   samantalang ang default ay `True`. Naling-lang ako mismo nito.
3. `broker_truth_reconciliation_enabled` — patay, at ito sana ang naglinis ng saradong
   posisyon ng CDTG. Ang stale na hilerang iyon ang nag-defer ng **15 entry** ngayong hapon.

## Ang aral

Ang "ship dark, promote after A/B" ay tunog maingat pero **hindi ito nagsasara**:
ang A/B ay nangangailangan ng trade, at ang trade ay hinaharangan ng flag. Ang flag na
naka-OFF nang walang **nakasulat na petsa ng pagsusuri** ay hindi maingat — ito ay
nakalimutan. Kung hindi ito maipapasa sa loob ng isang linggo, dapat itong **ON o
TANGGALIN**.
