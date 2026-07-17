<!-- Auto-saved mula sa dark-flag audit workflow wf_2c8fc458-9b7 (8 agents, 104 flags), 2026-07-16 gabi. Ang 37 TURN_ON_SAFE ay in-apply bilang config.py defaults sa kasamang commit; ang dip-family VALIDATE_FIRST ay nanatiling OFF (DIP-ON A/B ngayong gabi: 3 windows walang pagbabago, 1 lumala — hindi pa proven). -->

# Dark-Flag Audit Synthesis — 104 flags (2026-07-16)

**Bilang:** TURN_ON_SAFE = 37 · KEEP_OFF_EVIDENCE = 23 · VALIDATE_FIRST = 32 · POSTURE_ENV_DRIVEN = 12 (kabuuang 104 ✓)

---

## 1. TURN_ON_SAFE — ranked ayon sa inaasahang epekto sa Ross winner capture

### Tier 1 — Direktang capture: additive entries + conversion unblockers (pinakamataas)
1. **chili_momentum_pullback_raw_break_when_explosive** — natitirang strand ng conversion bundle; tumatama mismo sa #1 bottleneck (SILO 95s late, CLRO/VRAX zero-fills); ang kapatid na gate ay DEFAULT TRUE at live-proven.
2. **chili_momentum_bull_flag_entry_enabled** — ang pinaka-core na Ross setup (SS101 #012) na wala sa live; guard-complete, per-trigger rollback.
3. **chili_momentum_wedge_break_entry_enabled** — reference-grade guarded entry; kapatid na cup-and-handle ay hardened + ON na since 06-27.
4. **chili_momentum_bid_prop_explosive_exempt** — dokumentadong false-blocks (ILLR/WEN/BB blocked then ran); master explosive_recalibration ON na since 07-07.
5. **chili_momentum_entry_flow_veto_explosive_exempt** — inaatake ang re-entry lockout winner-killer (VRAX 212 blocks habang 8→13); AND-leg intact kaya ang PLSM-class ay vetoed pa rin.
6. **chili_momentum_event_based_abandonment_enabled** — pinapanatiling watched ang day's leader (IVF +66% na-reap; CLRO afternoon starvation class); instant fade-reap + hard ceiling.
7. **chili_momentum_pyramid_skip_viability_recheck** — inaalis ang flicker-refusal ng adds sa HELD winners (3x live refusal dokumentado); napaka-narrow override, hard blocks intact.

### Tier 2 — Selection ng winners + sizing ng winners
8. **chili_momentum_news_catalyst_weight_enabled** — 4th Ross pillar; top-3 profit lever; absurd na ang sizing sa parehong feed ay ON pero ang selection pillar OFF.
9. **chili_momentum_catalyst_conviction_enabled** — bounded 1.5x size-up sa strong-catalyst names; #849/#850 doctrine (+87% wk). ⚠️ i-verify muna ang exec-container binding (may binding contradiction, tingnan §5).
10. **chili_momentum_dip_velocity_conviction_enabled** — size-by-dip-quality [1.0,1.25]; itinutulak ang capital sa tamang entry shape (dips = #1 gap sa 07-16 scorecard). Halos inert hangga't OFF ang dip family — i-flip kasabay ng dip-family activation post-A/B.
11. **chili_momentum_news_pr_cadence_enabled** — pares ng news pillar (inert kung OFF iyon); premarket PR-window lean-in.
12. **chili_momentum_absorption_snap_entry_enabled** — additive L2 entry na may buong chase-guards; ⚠️ mababa ang realized value hangga't stale ang equity L2 (fail-closed → inert).
13. **chili_momentum_bottom_reversal_entry_enabled** — SS101 #019 counter-trend; guarded + segmentable; kapatid na flush_dip live na.
14. **chili_momentum_halt_resumption_direction_enabled** — ±15% bounded size tilt sa halt-resume conviction.
15. **chili_momentum_second_leg_preference_enabled** — makitid na multi-fire tie-break papunta sa based leg (anti-local-top).

### Tier 3 — Env-drift restores + selection hygiene (proven posture, ibalik lang)
16. **chili_momentum_move_exhaustion_abandon_enabled** — PLSM-chase fix, deliberately enabled 06-27; nalaglag sa exec env (drift).
17. **chili_momentum_no_asetup_sit_cash_enabled** — A+-only discipline sa dead regime; enabled 06-27, drift.
18. **chili_momentum_adaptive_spread_cost_veto_enabled** — tumutugon sa proven ~4.6%-spread loss mode; enabled 06-27, drift.
19. **chili_momentum_a_setup_quality_floor_enabled** — operator-blessed 06-27 floor; nalaglag sa 07-06 armfix; mababang capture cost (hindi selection ang bottleneck).
20. **chili_momentum_no_signal_derank_enabled** — GALT-class fix (no-score name entered over real movers); scored names never touched.
21. **chili_momentum_float_persistence_enabled** — data-integrity backfill; pinoprotektahan ang quality floor sa flicker-to-None false rejections.
22. **chili_momentum_price_sweetspot_tilt_enabled** — Ross $3-10 doctrine, additive re-rank lang.
23. **chili_momentum_overhead_supply_tilt_enabled** — trapped-supply de-weight, never-block pillar.
24. **chili_momentum_daily_200ema_room_enabled** — room-above-200MA tilt; 3 kapatid na tilts default TRUE na.

### Tier 4 — Protective/quality gates (capture-neutral pero tama)
25. **chili_momentum_false_halt_avoid_enabled** — quality gate ng halt-resume dip (UBXG loss class); weak resume = no-fire lang.
26. **chili_momentum_round_number_entry_timing_enabled** — makitid na defer bago round-number overhead; re-enters on hold.
27. **chili_momentum_wick_reclaim_slow_recovery_gate_enabled** — Ross-faithfulness tightening sa live nang wick-reclaim; >=4-bar trickles lang ang saklaw.
28. **chili_momentum_halt_down_cascade_liquidate_enabled** — disaster protection sa halt-ladder trap (ZJYL/HKD class); exit-only.
29. **chili_momentum_overnight_dark_flatten_enabled** — GMM-class protection; halos inert habang walang overnight positions.
30. **chili_momentum_hard_no_trade_regime_enabled** — literal na no-op sa current defaults (empty event list); vacuous ang "flip" (tingnan §5).

### Tier 5 — Telemetry/infra (mababa pero libre)
31. **chili_momentum_exit_event_driven_enabled** — latency accelerant sa deployed exit logic; dispatch hint lang, isang buwan nang may counterfactual.
32. **chili_bracket_watchdog_enabled** — detection layer ng GMM-orphan/missing-stop class (#900); alert-only, anti-storm bounds.
33. **chili_momentum_big_buyer_bid_starter_enabled** — annotation-only conviction telemetry (pakain sa meta-label features).
34. **chili_momentum_overnight_tape_enabled** — WRITE-before-READ overnight data accumulation; whitelist-bounded (bantayan ang retention).
35. **chili_momentum_replay_capture_features** — replay-only; direktang pakain sa meta-label dataset (#1 lever).
36. **chili_momentum_process_score_enabled** — read-only journaling.
37. **chili_momentum_challenge_metrics_enabled** — read-only journaling.

---

## 2. KEEP_OFF_EVIDENCE — isang-linyang dahilan bawat isa

| Flag | Dahilan |
|---|---|
| chili_auto_execute_stops | Dead code na walang caller; kung ma-rewire, magre-race sa autotrader exits → i-DELETE, huwag i-flip. |
| chili_mesh_plasticity_dry_run | BALIKTAD ang polarity — ang ON ay tahimik na PAPATAYIN ang live mesh learning (dark regression). |
| chili_micro_log_equity_enabled | Walang consumer code; no-op na magbibigay ng ilusyon ng equity-L2 data collection. |
| chili_momentum_backside_veto_enabled | E1: replay-KILLED 06-25 (net-negative, over-vetoed reclaim winners); may adaptive successor na LIVE. |
| chili_momentum_bail_on_no_confirmation_enabled | Winner-killer: papasok sa 8-20s dip-test window na mismong pinoprotektahan ng lock-in fix (FCUV lesson); hindi kino-consult ang lock-in. |
| chili_momentum_exit_ladder_live | 07-06 flip ROLLED BACK (#855; 201/219 cancels, 1054/1054 stale L2); superseded na ng MFE-partial #881. |
| chili_momentum_family_regime_prefilter_enabled | Bucket data ay empty/contaminated (940/942 snapshots) + walang sha segmentation → veto base sa basura. |
| chili_momentum_fatigue_derate_enabled | Kapareho ng exp_mult-0.5 death-spiral (CLRO 07-07 sizing inversion); catalyst names win ANY hour. |
| chili_momentum_first_dip_reclaim_enabled | Sinadyang inert (provenance-only); ang policy_mode ang tunay na authority. |
| chili_momentum_hard_no_trade_midday_enabled | Blunt 4-oras entry block; ang adaptive midday de-weight ay LIVE na, at catalyst winners win any hour. |
| chili_momentum_instant_bid_above_fill_confirm_enabled | Fixed 25bps vs ~460bps na totoong spreads → halos sistematikong 6-12s bail ng normal fills. |
| chili_momentum_instant_bid_below_fill_cut_enabled | Parehong 25bps magic-number flaw; magfa-fire sa ordinaryong spread, hindi sa tunay na collapse. |
| chili_momentum_order_chunking_enabled | Nagpaparami ng orphan surface habang sariwa ang GMM −$18k (#900) + may orphan-reconciler landmine bago activation. |
| chili_momentum_overnight_trading_enabled | Walang broker stop overnight + proven blind-window infra + walang feed-liveness gate; ⚠️ SCRUB ang stale =1 sa scheduler env. |
| chili_momentum_per_symbol_fatigue_enabled | Deliberate 06-26 disable; lumalaban sa proven leader-re-attempt need (JEM captured pagkatapos alisin ang lockout). |
| chili_momentum_risk_require_strict_coinbase_freshness | Strict freshness = proven lane-killer class (07-09 frozen-freshness outage). |
| chili_momentum_rulebreak_nextday_lockout_enabled | Mismong mekanismo ng 06-30 zero-entry outage; landmine tinanggal sa A1 (07-03); verified-posture doc naglilista ng False. |
| chili_momentum_scale_grid_enabled | Fixed 1R/2R grid na nagbebenta ng 75% by 2R sa lane na 7-17R ang winners; superseded ng MFE targets. |
| chili_momentum_stopout_cooldown_timer_enabled | INVERTED: ang ON ay NAGBABALIK ng legacy timer na replay-proven mas masama (CELZ −$108 vs +$229). |
| chili_momentum_timeofday_schedule_enabled | Superseded ng adaptive per-hour risk curve (ON na); magdodoble ng time-of-day sa size + pangalawang fade suppressor sa parehong signal. |
| chili_momentum_win_cycle_fatigue_enabled | Pinuputol ang right tail sa hot-streak days — ang mismong days na bubuhat ng $1k/day; E-batch sibling ay replay-killed. |
| chili_per_broker_count_manual_as_rh | Deliberate semantics; ang ON ay magpapakain ng manual trades sa automated budget → frozen-lane class ulit (#727). |
| chili_tenbeat_candle_equity_enabled | Data-gated: walang 2-3s equity mid source at hindi wired ang equity branch; no-op na magsisinungaling sa posture. |

---

## 3. VALIDATE_FIRST — anong validation ang kulang

### A. Naghihintay sa dip-family A/B replay NGAYONG GABI (in-flight — huwag galawin hangga't walang verdict)
- **chili_momentum_entry_tight_false_break_reclaim_enabled** — A/B verdict mismo.
- **chili_momentum_ask_thins_dip_entry_enabled** — A/B verdict.
- **chili_momentum_sub_vwap_trap_entry_enabled** — A/B verdict (mid-A/B flip = contamination).
- **chili_momentum_pulling_away_roc_entry_enabled** — A/B verdict.
- **chili_momentum_premarket_pivot_macd_entry_enabled** — A/B verdict; restore agad sa exec kung green.
- **chili_momentum_first_dip_reclaim_policy_mode** — 'candidate' = evidence vehicle; 'promoted' ay nakasara hangga't walang hash-bound OOS receipt.
- **chili_momentum_dip_buy_rth_only_enabled** — A/B na may/walang RTH gate; may KONTRA-ebidensya (Ross dips ay premarket per 07-16 scorecard).
- **chili_momentum_red_candle_entry_block_enabled** — idagdag sa dip-family matrix; dip bounces ay nag-fi-fire habang RED pa ang bar.

### B. Naka-log NA ang counterfactual — basahin lang (pinakamurang validation)
- **chili_momentum_exit_candle_confirm_live** — isang buwan ng candle_would_suppress events; i-promote kung early-sells sila (posibleng winner-killer fix). MATAAS na prayoridad.
- **chili_momentum_exit_ofi_lock_partial_enabled** — basahin ang would-fire counterfactual + i-map ang overlap sa live MFE-partial (double-scale risk).
- **chili_momentum_exit_ofi_hidden_seller_enabled** — manipis ang crypto soak (lane patay 07-03→07-09); patunayan sa counterfactual na hindi pinuputol ang 7-17R runners.

### C. Kailangan ng replay A/B (hermetic / 5-day scorecard windows)
- **chili_momentum_break_candle_adaptive_close_pos_enabled** — sariling Field mandate: A/B sa recorded-fills replay (malakas ang prior: 53% ng blocks tumakbo +3%).
- **chili_momentum_entry_extension_rvol_boost_enabled** — two-sided: extension tops = #1 loss shape VS 200+ VRAX re-entry blocks; A/B ang aareglo.
- **chili_momentum_explosive_floor_enabled** — F1 fix nasa code na; E1-sibling lesson = hard gates ay replay-proven muna (ano ang na-block sa dull days?).
- **chili_momentum_candle_quality_multitf_veto_enabled** — patunayang net-positive ang mga vetoed (E1 + over-reject class history).
- **chili_momentum_halt_chain_risk_gate_enabled** — replay sa halt-chain days; baka putulin ang vertical tail (fill-on-verticals lesson).
- **chili_momentum_ma_vwap_pullback_enabled** — FSM replay sa grind class (verticals >> grinds finding + budget starvation risk).
- **chili_momentum_measured_move_exit_enabled** — A/B vs CURRENT 07-09 exit stack (noise-floor clamp + MFE-partial interaction; tighten mechanism sa winner-killer zone).
- **chili_momentum_smart_hold_enabled** — hermetic A/B vs lock-in/noise-floor pair (proven interaction-sensitive zone: clamp alone WORSE).
- **chili_momentum_sub5min_scalp_bailout_enabled** — hermetic A/B + spot-check na tama ang cadence_cls labels (G4 cadence bug history).
- **chili_momentum_pyramid_discrete_add_enabled** — A/B sa add-capture ng verticals (adds were STARVED; baka wala itong discrete higher-low mid-move).
- **chili_momentum_order_burst_candle_guard_enabled** — A/B sa top-of-hour entries; direktang salungat sa PR-cadence lean-in (§5).
- **chili_momentum_float_overrotation_fix_enabled** — scorer replay sa 07-09..07-15; i-verify na hindi nawawala sa top-3 ang dokumentadong winners.
- **chili_momentum_second_day_context_enabled** — offline before/after re-rank sa recorded selection days (guardrail: don't rewrite the proven scorer; day-3 derate risk).

### D. Binding/feed verification muna
- **chili_momentum_entry_l2_veto_enabled** — i-verify ang fresh L2 sa EXEC worker (1054/1054 stale = inert flag); saka A/B.
- **chili_momentum_l2_confirm_enabled** — pareho: fresh-L2 binding check muna, tapos A/B (dagdag deferrer sa conversion-bottlenecked lane).

### E. Soak / staged rollout (dokumentadong sariling gate)
- **chili_momentum_broker_truth_reconciliation_enabled** — ang pag-ON ANG soak-start (WRITE, additive); kailangan ng deliberate follow-through: inspect divergence + match-rate.
- **chili_momentum_broker_truth_label_enabled** — READ flip LANG pagkatapos ng WRITE soak inspection; gate-input change → deploy-when-flat.
- **chili_momentum_short_enabled** — tapusin ang P1 + P2 paper soak (operator-approved staged rollout sa design doc; squeeze-into-halt-up kill scenario).
- **chili_momentum_anticipation_starter_enabled** — patakbuhin ang R1 dedupe/orphan suite laban sa alpaca-paper path PAGKATAPOS ng cutover soak (multi-leg + bagong adapter + orphan landmine = masamang timing ngayon).

### F. May purpose-built validation script na — patakbuhin lang
- **chili_pattern_dd_breaker_enabled** — walk-forward (walkforward_monthly_dd_breaker.py) vs 2026-04-22 trough; iyan mismo ang dokumentadong turn-on condition.
- **chili_pattern_evidence_auto_demote** — patakbuhin ang dry_run flag + basahin ang report + evidence backfill; kung hindi, mass-demote ng promoted set = biglang trading halt.

---

## 4. POSTURE_ENV_DRIVEN — deployment toggles, hindi problema

| Flag | Effective posture |
|---|---|
| chili_alpaca_enabled | ON via env sa parehong workers; code default OFF ang tama (keys+UUID pin dependency). |
| chili_autopilot_price_bus_enabled | ON sa lahat ng live services; ang panganib ay pag-OFF (07-06 outage class). |
| chili_autotrader_broker_equity_cache_enabled | ON na matagal sa parehong containers, walang insidente. |
| chili_autotrader_synergy_enabled | Compose default true sa autotrader-worker; moot (worker hindi tumatakbo). |
| chili_code_frontier_enabled | Sadyang SPLIT (=1 exec, =0 scheduler); spend decision, hindi safety gate. |
| chili_coinbase_ws_enabled | ON sa lahat; pag-OFF = frozen-freshness lane-killer (dokumentado 07-09). |
| chili_momentum_add_into_halt_enabled | Deliberately ON live since 06-27 directive; code OFF = kill-switch pattern. |
| chili_momentum_auto_arm_equity_only | Manifest-driven (captured-paper scripts set TRUE); code default False ang tama. |
| chili_momentum_crypto_live_arm_enabled | .env=true na per 07-09 unblock; code default False = tamang env-less fallback. |
| chili_momentum_replay_full_pipeline_enabled | Per-run replay tool mode, hindi dark flag. |
| chili_momentum_replay_tick_entry_enabled | Per-run replay resolution mode. |
| chili_scheduler_runs_externally | Dapat mag-mirror ng topology; compose-driven, hindi fliflip. |

---

## 5. Mga kontradiksyon at kahina-hinalang klasipikasyon 🚩

1. **PR-cadence vs order-burst guard — direktang salungatan.** news_pr_cadence (TURN_ON_SAFE) ay LEAN-IN sa top-of-hour PR windows; order_burst_candle_guard (VALIDATE) ay DEFER sa parehong windows. Huwag i-ON pareho nang walang A/B verdict sa guard; kung i-ON ang cadence ngayon, ang guard A/B baseline ay dapat i-note na may cadence ON.
2. **catalyst_conviction binding contradiction.** Sariling entry: "hindi ma-compute ang in-container binding"; pero ang news_catalyst_weight entry ay nag-claim na PRESENT ang CHILI_MOMENTUM_CATALYST_CONVICTION_ENABLED sa _mexec_armfix.env. Isa lang ang totoo — i-docker-inspect ang exec worker bago ito ituring na bagong flip (report-binding rule).
3. **"Safe dahil inert" ≠ may capture value.** absorption_snap_entry at big_buyer_bid_starter ay TURN_ON_SAFE partly dahil fail-closed/annotation-only sila sa stale equity L2 — ang PAREHONG staleness evidence (1054/1054) ang ginamit para i-VALIDATE ang l2_confirm/entry_l2_veto. Consistent sa risk, pero ang expected capture ng L2-dependent flips ay ~zero hangga't hindi naaayos ang L2 freshness; huwag i-count bilang capture wins.
4. **hard_no_trade_regime "TURN_ON_SAFE" ay vacuous.** No-op sa empty defaults — ang flip ay walang binabago; ang tunay na risk ay darating kapag nilagyan na ng event times (timezone/parsing check kailangan noon). Posture-enabler ito sa katotohanan, hindi capture flip.
5. **Halt family tension.** add_into_halt ay LIVE ON (posture) habang halt_chain_risk_gate (block sa 3rd+ halt) ay unvalidated — kasalukuyang posture: nag-a-ADD tayo into halts nang walang chain gate. Hindi mali, pero ang halt_chain A/B ay dapat sumakop sa interaction ng dalawa.
6. **Hindi lahat ng 06-27-enabled ay dapat blanket-restore.** Ang env-drift story ay totoo para sa restore bundle (§Tier 3), PERO dalawang dating-enabled flags ang lehitimong VALIDATE na ngayon dahil nagbago ang paligid: measured_move_exit (07-09 exit-stack retune post-dates ito) at order_burst_candle_guard (conversion evidence post-dates ito). Ang tamang aksyon ay selective restore, hindi buong 06-27 env replay.
7. **Systemic finding — ang 07-06 _mexec_armfix.env re-baseline ang naglaglag ng ~10+ dating-ON flags sa exec worker.** Ito ang pinakamalaking single dahilan ng "dark" state ngayon. Kailangan: (a) isang env-restore commit, (b) in-container binding verification pagkatapos i-deploy, (c) env-file diff guard sa deploy script para hindi na maulit (kapatid ng duplicate-DATABASE_URL lesson).
8. **Stale docs na landmine.** mesh_plasticity docstring ay nagsasabing dry-run default ON gayong live-mutating na — ayusin para walang ma-mis-flip. Scheduler env may stale CHILI_MOMENTUM_OVERNIGHT_TRADING_ENABLED=1 — i-scrub sa 0.
9. **Dead-flag cleanup class (i-delete, hindi i-audit paulit-ulit):** chili_auto_execute_stops (+ ang function), chili_micro_log_equity_enabled, chili_tenbeat_candle_equity_enabled, chili_momentum_first_dip_reclaim_enabled — apat na flags na walang functional effect na nagpaparumi ng audit surface.
10. **Timing caution ngayong gabi.** May dip-family A/B replays na tumatakbo sa branch na ito — ang live env flips ay hindi gumagalaw sa hermetic replay sinks, pero ang sub_vwap audit warning (mid-A/B contamination) ay nangangahulugang: huwag i-flip ang anumang dip-family trigger bago ang verdict, at i-land ang ibang flips PAGKATAPOS matapos ang replay batch para malinis ang attribution.

---

## TONIGHT FLIP SHORTLIST

Mga TURN_ON_SAFE entry/veto flags na pinaka-makakatulong sa Ross capture at ligtas i-default-ON agad. **Sequencing:** i-flip PAGKATAPOS matapos ang tonight's replay batch; i-verify ang in-container bindings sa momentum-exec pagkatapos i-deploy; per-trigger events ang rollback instrument.

| # | Flag | Bakit ngayon na |
|---|---|---|
| 1 | chili_momentum_pullback_raw_break_when_explosive | Conversion bottleneck fix strand; gate live-proven ng default-TRUE sibling; tape-required fail-closed. |
| 2 | chili_momentum_bid_prop_explosive_exempt | Dokumentadong false-blocks sa runners (ILLR/WEN/BB); master ON na, sub-flag lang naiwan. |
| 3 | chili_momentum_entry_flow_veto_explosive_exempt | Re-entry lockout class (VRAX 212 blocks); PLSM AND-leg intact = bounded. |
| 4 | chili_momentum_bull_flag_entry_enabled | Core Ross setup na wala sa live; guard-complete; segmentable per-trigger. |
| 5 | chili_momentum_wedge_break_entry_enabled | Reference guard implementation; sibling cup-and-handle proven ON since 06-27. |
| 6 | chili_momentum_event_based_abandonment_enabled | Pinapanatiling buhay ang leader watch (IVF/CLRO lesson); instant fade-reap + ceiling. |
| 7 | chili_momentum_pyramid_skip_viability_recheck | Un-starves adds sa held winners (makasakay); narrow override, hard blocks intact. |
| 8 | chili_momentum_false_halt_avoid_enabled | Quality gate ng 07-16 fix item (d) halt-resume dip; no-fire lang sa weak resume. |
| 9 | chili_momentum_bottom_reversal_entry_enabled | Additive guarded counter-trend entry; reversible sa isang env flip. |
| 10 | **ENV-DRIFT RESTORE BUNDLE** (isang commit): a_setup_quality_floor, absorption_snap_entry, adaptive_spread_cost_veto, move_exhaustion_abandon, no_asetup_sit_cash | Hindi bagong flips — pagbabalik ng operator-blessed 06-27 posture na nalaglag sa 07-06 armfix. EXCLUDE: measured_move_exit at order_burst_candle_guard (VALIDATE muna, §5.6). |

**Kasabay ng parehong deploy (selection/sizing, hindi entry/veto pero capture-critical):** news_catalyst_weight + news_pr_cadence (pares), catalyst_conviction (pagkatapos ma-verify ang binding, §5.2).

**Huwag isama ngayong gabi:** lahat ng dip-family triggers (ask_thins, sub_vwap_trap, pulling_away_roc, premarket_pivot_macd, tight_false_break_reclaim, red_candle_block, dip_buy_rth_only) — in-flight ang A/B, oras na lang ang verdict; at anticipation_starter/order_chunking — hintayin ang alpaca-paper cutover + orphan-reconciler hardening.
