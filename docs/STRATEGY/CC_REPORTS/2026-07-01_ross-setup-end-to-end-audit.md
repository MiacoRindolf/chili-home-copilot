# Ross Setup End-to-End Audit - Momentum Lane

Date: 2026-07-01
Worker: Sub-agent D
Repo basis: current working tree on `chili/momentum-concurrency-basis-independent` at `395f4ef`

## 2026-07-01 Current Addendum

Scope correction: this addendum is the active momentum-lane/setup audit, not a live-trade monitor. Live execution was paused only because the worker restarted and placed TC/LHAI while this audit was in progress. That placeholder state is no longer the desired runtime state: Ross equity momentum must run from a real `momentum_exec_only` worker with live runner, auto-arm, and the hard Ross universe gate enabled. If the canonical ad-hoc name is occupied by a placeholder, do not treat it as live coverage; replace it with a real worker or run exactly one real replacement worker and verify heartbeat, IQFeed listener, and Ross-universe admission logs.

2026-07-03 current-runtime correction: authoritative runtime is now canonical `chili-clean-recovery-momentum-exec` on `chili-app:codex-ross-hygiene-catalyst120-20260703-1035` (`sha256:be3832a22b205ee43510a895a272eb8f4ba07742524b26398b740bf463fdd66f`), healthy with live runner/event loop/auto-arm/Ross universe enabled and scheduler/batch fallback disabled. Runtime smoke proved the pre-entry expiry bridge is loaded, Ross pre-submit universe guard is loaded, generic catalyst rank stays `0`, `no_fill` is not a real-entry outcome, OFI reader contracts are present, A-setup notional floors respect `red_intraday` as a hard reducer, and the news-catalyst freshness contract is restored to 120 minutes instead of the accidental 30-minute de-boost. Focused validation passed the DB-backed expiry/starvation runner slice (`3 passed`), Replay v3 scheduler/PnL/export/CLI slice (`60 passed`), restored-helper/dark-flag/runtime slice (`115 passed` lineage plus the 2026-07-03 catalyst120 focused verifier), feature-flag suite (`18 passed`), and strict runtime verifier (`momentum_worker_runtime_ok`). Read-only live replay since worker start now returns `ok=true` with zero current session rows; empty windows no longer become fake setup-trace failures, while strict current-evidence, lifecycle, scheduler-priority, and PnL-min/max claims remain fail-closed until actual selected sessions/outcomes/labels exist. Scheduler service history may still mention stale `chili-app:main-clean-d718991` / `main-clean-7f6eff4`, but it is not the live order-capable canonical worker; the stale `chili-clean-recovery-scheduler` is stopped with restart policy disabled, and do not rotate it casually while another agent may be active.

2026-07-03 catalyst-deboost correction: re-audit found one non-neutral change hidden inside the restored-helper/feature-registry work: `NEWS_CATALYST_MAX_AGE_MIN` and `chili_momentum_news_catalyst_max_age_min` had been narrowed from 120 minutes to 30 minutes. That could suppress valid Ross news/catalyst context and is not a compatibility primitive. Restored both defaults to 120, added tests proving the Settings row matches the module contract, hardened `verify_momentum_worker_runtime.py` so future images fail if catalyst freshness regresses, built `chili-app:codex-ross-hygiene-catalyst120-20260703-1035`, and reloaded only the canonical worker through Compose. In-container smoke reports `catalyst_const=120`, `settings_catalyst_age=120.0`, `catalyst_rank_neutral=0`, `no_fill_real_entry=False`, and `ofi_reader_callable=True`. Full runtime verifier returned `momentum_worker_runtime_ok`; Ross readiness remains green-with-warning only for market-holiday stale tape.

2026-07-03 Replay PnL-boundary guard: added a focused regression for the exact PnL certification transition. Replay PnL/min-max stays false when the timeline has selected sessions and complete broker outcomes but lacks market-path counterfactual opportunity labels; it becomes true only with multi-snapshot scheduler evidence, complete selected outcomes, and complete missed/taken labels. This prevents Replay from becoming another dark flag after evidence is complete, while still preventing overclaims from expected PnL or fills alone. Validation passed Replay v3 sizing/scheduler/PnL (`15 passed`), audit CLI (`28 passed`), and compile checks.

2026-07-03 candidate image note: built and smoked `chili-app:codex-audit-boundary-20260703-0630` (`sha256:b8df1376de1d57f289df8bcc50b238ef3f3087fd582eb3e35315145ff479621e`) to package local audit/replay/report hardening. It is **not** the running live worker and was **not** deployed; canonical live runtime now stays on `chili-app:codex-ross-hygiene-catalyst120-20260703-1035`. Candidate smoke covered `_settings` fallback scanning, empty-window setup trace behavior, PnL-minmax proof-leg transition, restored helper contracts, and in-image compile. Host runtime verifier still passed against the running worker. Do not claim candidate image live-loaded until `.env` is intentionally pinned and exactly one canonical worker is recreated through Compose.

**Current verdict:** code path and live worker are **MECHANICALLY READY / ROSS CERTIFICATION PARTIAL**, but live PnL and Ross-complete certification are **NOT READY**. The authoritative current canonical worker is `chili-clean-recovery-momentum-exec` on `chili-app:codex-ross-hygiene-catalyst120-20260703-1035` (`sha256:be3832a22b205ee43510a895a272eb8f4ba07742524b26398b740bf463fdd66f`), running healthy with live runner/event loop/auto-arm/Ross universe enabled, scheduled live runner disabled, auto-arm scheduler/fallback disabled, and batch fallback disabled. Strict runtime verification completed and returned `momentum_worker_runtime_ok`; read-only replay/setup trace verification is clean for the empty current window and still fail-closed for lifecycle/PnL claims. Earlier `codex-replay-label-*`, `main-clean-codex-regression-restore-*`, `rosslane-*`, `envguard*`, boundary-telemetry, budget-telemetry, Ross-pillar-telemetry, replay-cert, event-replay-snap, expirybridge, regression-restore, and `codex-ross-hygiene-contracts-redintraday-*` images in this report are lineage only, not current runtime truth. The verifier also fails if the canonical worker had a recent external `kill`/`die`/`restart` inside its Docker healthcheck-derived stabilization window, so a host-side bounce cannot be hidden behind a healthy auto-restart; the restored-helper smoke also fails on catalyst freshness drift. This upgrades runtime guard evidence to mechanically ready, but it still does not upgrade the lane to Ross-complete or PnL-certified because Replay source labels remain fail-closed. Do not recreate placeholder containers under the canonical momentum-exec name, and do not rotate workers just to satisfy stale audit text.

2026-07-03 dropped-binding correction: re-audit found the canonical worker was healthy but still importing `chili_momentum_pullback_entry_interval=5m` and `chili_momentum_early_premarket_min_movers=3`, contradicting the 2026-07-02 master-plan claim that Wave 0 was live at `1m` and `1`. This is a real premarket capture binding gap, not a Ross/PnL certification issue. Patched source defaults to `1m` and `1`, added explicit momentum-exec compose bindings, and made both keys load-bearing in `verify_momentum_worker_runtime.py` so future compose/image rebuilds fail if they are dropped. Built `chili-app:codex-momentum-bindings-20260703-ptfix` (`sha256:98f8089d1a83ad3515254111fc10cdb18963a52a74d7b817601bbfe674e5c26c`), isolated-smoked the missing Settings fields, and after `--require-no-active-like-sessions` preflight passed, archived the out-of-band `main-clean-855f24c` container and recreated the canonical worker through the compose `momentum-exec-worker` service. Post-reload runtime reads `pullback_entry_interval=1m`, `early_premarket_min_movers=1`, live runner/auto-arm/event loop ON, scheduler/batch fallback OFF, Ross transcript marker path present, and RH Agentic readiness reports `broker_ready_for_live=true`, `execution_ready=true`, `runnable_live_now=true`. Validation passed focused compose/runtime guard tests, compile, semantic restored-helper tests, in-container process health, reload preflight, full runtime verifier (`momentum_worker_runtime_ok`), and read-only replay/setup trace (`ok=true`, `trace_coverage_ok=true`, `setup_trace_findings=0`). No manual orders were placed; recent log scan found no new submit/fill lines after reload. PnL/min-max remains fail-closed because selected sessions, broker outcomes, and counterfactual opportunity labels are still absent.

2026-07-03 binding-regression guard hardening: the runtime verifier now has an effective-value stage, `premarket_binding_config`, that reads live Settings from the canonical worker and fails on stale `pullback_entry_interval` or `early_premarket_min_movers` values, not merely on missing compose keys. Regression tests prove the guard accepts `1m/1` and rejects the old `5m/3` state (`premarket_binding_failed:*`). Focused tests, compile, and full runtime verifier passed against the current worker.

2026-07-02 18:52 PT historical runtime supersession: at that point the canonical worker had been moved to `chili-clean-recovery-momentum-exec` on `chili-app:codex-replay-label-20260702-1835` (`sha256:b3f14efc9217f4292ab75f09904ffd98c4b50d4b1626eb1e83f01d1a410b84cf`). That is now lineage only; the authoritative current runtime is the 2026-07-03 boundary at the top of this report. The older `main-clean-codex-regression-restore-*`, `rosslane-*`, `envguard*`, boundary-telemetry, budget-telemetry, Ross-pillar-telemetry, replay-cert, and event-replay-snap image references in this report are deployment lineage, pending-artifact history, or stale pre-supersession observations, not the current canonical runtime. Runtime guard returned `momentum_worker_runtime_ok`, reload preflight returned `momentum_worker_reload_preflight_ok`, in-container readiness was green (`broker_ready_for_live=true`, `execution_ready=true`, `runnable_live_now=true`), boundary-risk telemetry was normalized with stable reasons and failed-check ids, daily trade-count budget telemetry included explicit Ross A+ overflow refusal reasons, Ross-pillar waits emitted `failed_pillars` plus `pillar_pass` diagnostics, and Replay certification emitted structured `evidence_status` plus `missing_evidence`, event-loop `live_replay_event_snapshot` timeline steps, and explicit opportunity-label/PnL-minmax guardrails. The visual/source boundary remained fail-closed with zero source-before-opportunity certifying rows. Status remained **READY / MECHANICALLY DEPLOYED; NOT READY for Ross-complete or positive-EV certification**.

2026-07-02 Ross-pillar wait follow-up: audited the dominant `ross_pillars_not_explosive` wait class instead of treating it as a vague dark flag. Current sampled rows were mostly failing objective Ross starter pillars: change below the configured explosive threshold, RVOL missing/low, missing Ross-supported source context, or no catalyst/direct-trade/daily-break context. Patched `tick_scalp.py` telemetry only: refusals now explain `failed_pillars` and per-pillar pass state (`pillar_pass`) while preserving the same fail-closed entry decision. Validation passed focused Ross evidence tests, compile, setup-trace/replay CLI tests, isolated image smoke, strict runtime guard, and post-deploy read-only replay (`ok=true`, `setup_trace_findings=0`, `trace_coverage_ok=true`, `selected_count=0`, `broker_outcome_count=0`). This is diagnosis-quality telemetry, not an entry relaxation or PnL certification.

2026-07-02 regression re-audit: rechecked Claude's "deleted features" concern after the latest deployment. The restored helpers are still not redundant with the Ross/event-driven fixes: OFI/ladder/target-print readers feed entry, add, hold, and exit flow logic; `is_real_entry_outcome` protects trade-budget and streak math from no-fill/cancel/pre-entry rows; and catalyst grading remains an additive sizing/context contract. A narrowed static scan found no remaining hidden `getattr(settings, "chili_momentum_*_enabled", False)` sites in scoped Python app/scripts/tests paths, and focused guard suites passed for dynamic import contracts, first-class feature flags, Compose/load-bearing env rows, and restored-helper runtime smoke. Current interpretation: restoring these contracts reduces rebuild regression risk; it does not loosen Ross universe, stale-entry, structural-stop, broker-auth, or daily-loss hard gates.

2026-07-03 regression hygiene recheck: operator explicitly rejected "quick neutralize everything" fixes. Rechecked the current worktree and runtime with that standard. `is_real_entry_outcome` remains fail-closed for no-fill/cancel/pre-entry outcomes, OFI/L2 helpers return `None` on absent/stale/thin data, and OFI-required entry gates still reject missing OFI/L2 instead of treating it as confirmation. Catalyst grading is sizing/context only: weak/fake dominates strong, missing feeds produce no boost, and the live worker has `CHILI_MOMENTUM_CATALYST_CONVICTION_ENABLED=false` unless explicitly enabled. Static feature audit found `missing_settings_count=0`, `missing_default_false_controls=0`, 106 operator-visible feature rows, and no soak wording in disabled reasons/proof gates. Runtime verifier passed against `chili-app:codex-momentum-bindings-20260703-ptfix`, and canonical env inspection confirmed the previously dropped load-bearing add/OFI/catalyst controls are present. Verdict for this concern: restoring OFI/outcome/catalyst contracts is not redundant, but the acceptable shape is compatibility plus explicit fail-closed/soft-only tests, not semantic shortcuts.

2026-07-03 catalyst shortcut correction: tightened the restored catalyst contract so generic `all_catalyst_symbols()` membership remains a viability-selection tilt only and does not become an inferred conviction-sizing rank. `catalyst_grade_rank()` now returns size-up rank only for explicit strong catalyst evidence, with weak/fake evidence overriding to rank 0 and all missing/generic evidence staying neutral. Focused validation passed `tests/test_news_catalyst.py`, restored-helper behavior tests, pipeline live-reader tests, feature-flag/runtime guard tests, compile, and the live runtime verifier (`momentum_worker_runtime_ok`). This is a semantic cleanup to avoid future hidden size drift, not an entry relaxation.

2026-07-03 JEM arm-lifecycle diagnostic: audited the user's `$3.86` VWAP-reclaim screenshot against live DB/tape windows. The matching tape is June 30, 2026 `19:35-19:55Z` (JEM prints `$3.33-$4.19`, average about `$3.84`, fresh IQFeed/NBBO), but sessions `9970`, `9988`, and `10036` had only `live_arm_requested -> live_arm_expired`: no `live_arm_confirmed`, no `live_runner_started`, no setup wait, and no entry candidate. Added read-only `arm_lifecycle` evidence to `run_live_replay_audit` and the CLI summary so future Replay audits separate unconfirmed-arm lifecycle gaps from setup-gate refusals. Focused regression proves an arm-requested/never-confirmed/expired session is reported as `arm_requested_without_confirm`; live audit of those JEM sessions now shows `arm_requested_count=3`, `arm_confirmed_count=0`, `runner_started_count=0`, `expired_without_runner_count=3`. The later confirmed JEM sessions show confirmed runners and should be analyzed under their actual backside/pullback/VWAP/risk reasons, not conflated with the June 30 arm gap.

2026-07-02 Replay certification hardening: `run_live_replay_audit` now emits structured `certification.evidence_status` and `certification.missing_evidence`, not only prose blockers. This makes the remaining Replay/PnL gap machine-readable: current live audit has live session rows, but lacks a multi-snapshot scheduler timeline, replay-selected sessions, scheduler pressure/delayed-selection evidence, adapter-unavailable same-step selection evidence, market-path counterfactual opportunity labels, and complete missed-vs-taken outcome labels. Focused validation passed 23 replay/audit tests plus compile, and the live read-only audit remained `ok=true` with `setup_trace_findings=0` and `trace_coverage_ok=true`. This is a Replay harness improvement, not a trading-behavior change.

2026-07-02 event-loop Replay snapshot bridge: the live lane is event-loop driven, so Replay should not rely only on scheduler-batch snapshot events. Added best-effort `live_replay_event_snapshot` emission from `LiveRunnerLoop` after a session tick, and taught the Replay export path to count those events as timeline snapshots. This does not alter entry, risk, sizing, order, or capacity behavior. Validation passed focused Replay export/emitter tests, runtime verifier smoke, compile, image smoke, controlled reload preflight, and post-deploy read-only Replay. The current post-deploy audit now has `scheduler_snapshot_steps=200`, but still has `selected_count=0`, `broker_outcome_count=0`, 0 entry fills, 0 adds, and 0 exits; lifecycle and PnL certification therefore remain fail-closed.

2026-07-02 opportunity-label Replay bridge: added explicit market-path opportunity label extraction to live Replay export/audit. The audit accepts only persisted counterfactual labels (`opportunity_label_summary`, `counterfactual_opportunity_label`, or equivalent explicit label payloads); it does not turn live fills, expected PnL, or source mentions into labels. PnL/min-max certification now requires three proof legs together: multi-snapshot timeline, complete broker outcomes for replay-selected sessions, and complete label-ready missed/taken opportunity rows. Focused tests prove the positive path and the fail-closed noncertified path. Current live audit still has no opportunity labels and no selected broker outcomes, so this is a certification-harness upgrade, not a live trading behavior change.

2026-07-02 current read-only setup trace audit: repeated `audit_momentum_live_replay.py --since-canonical-worker-start --limit 200 --setup-trace-limit 500 --summary-only --require-setup-trace-coverage` runs returned `ok=true`, `setup_trace_findings=0`, `trace_coverage_ok=true`, `window_completeness_ok=true`, and `lifecycle_order_ok=true` on the current runtime image. The current rolling window has 52 session rows, 500 setup-trace events, and roughly two dozen setup traces, dominated by `tick_first_pullback_scalp` waits such as `ross_pillars_not_explosive` and `tick_pullback_too_deep`. This is a clean setup-trace coverage result, but lifecycle/PnL certification remains not ready because this current-worker window has 0 entry fills, 0 trailing arms, 0 add fills, 0 exit fills, 0 selected scheduler rows, 0 broker outcomes, and no market-path counterfactual opportunity labels.

2026-07-02 blocker-quality follow-up: sampled current post-final-image `live_blocked_by_risk` and wait telemetry instead of relying only on counts. Recent refusals were dominated by explicit `wide_bbo_spread` blocks and Ross/profile/freshness blocks (`Viability snapshot stale`, `Not live-eligible per neural viability`, `Ross equity lane blocks faded/thin small-cap candidate below profile`, and `sub-dollar/non-profile equity candidate`). That classification is consistent with fail-closed Ross/safety behavior, not the restored OFI/outcome/catalyst regression. However, boundary-risk telemetry had a real audit gap: the live event emitted only raw `errors` plus `severity`, so replay/readiness could not group reasons without parsing free text. Patched `live_runner.py` with `_boundary_risk_block_payload`, preserving the same refusal while emitting stable `reason` and `failed_check_ids` fields such as `viability_snapshot_stale`, `ross_profile_below_profile`, `ross_profile_sub_dollar_or_non_profile`, `neural_viability_not_live_eligible`, or structured `boundary_viability_freshness_and_live_eligible`. Validation passed exact telemetry tests (`6 passed`), replay/report/setup audit tests (`64 passed`), compile check, isolated image smoke, and full runtime guard. Built and loaded `chili-app:codex-boundary-telemetry-20260702-1445` (`sha256:f47b6a739d0371050696daf3546808c9655c68c7d6b7a90cb09ed431eb5c7316`) after `--require-no-active-like-sessions` reload preflight passed; `.env` now pins `CHILI_MOMENTUM_EXEC_IMAGE` to this image. Strict runtime guard first failed correctly inside the controlled recreate quiet window, then passed after the health-derived quiet window. Post-deploy read-only replay returned `ok=true`, `setup_trace_findings=0`, `trace_coverage_ok=true`, and live DB now shows normalized boundary reasons plus `failed_check_ids` on fresh events. One LHSW tick-first-pullback candidate was blocked by daily trade count budget (`used=11`, `effective_ceiling=5`) after viability remained below floor (`0.44 < 0.57`) and bid-prop later showed deteriorating book/spread blowout; current evidence classifies that refusal as fail-closed, not a restored-feature regression. Lifecycle/PnL certification remains not ready: no entry fills/adds/exits in the post-deploy window, and Replay still has only single-snapshot scheduler/PnL evidence.

2026-07-02 daily-budget follow-up: audited the LHSW `daily_trade_count_budget_reached` blocker end-to-end. The count was not inflated by no-fill/cancel/risk-block rows: today's outcome table had 22 rows, 11 real-entry outcomes and 11 `cancelled_pre_entry` rows, and `_count_real_entries_today` uses `is_real_entry_outcome` to count only the real entries. The block reason was therefore real overtrading discipline, not a restored-helper regression. Patched `daily_trade_count_budget_decision` to add `ross_a_plus_overflow_reason` so future payloads explain why overflow was refused (`ross_universe_not_proven`, `stale_pre_submit`, or `overflow_ceiling_reached`) instead of only saying `ross_a_plus_overflow_allowed=false`. The allow/block math did not change: proven Ross tick/tape A+ entries can overflow the normal adaptive ceiling inside the documented max-multiple band; even Ross A+ is blocked once the hard overflow ceiling is reached. Validation passed daily budget tests (`4 passed`), `is_real_entry_outcome` focused test, live-runner budget/boundary tests (`11 passed`), compile, isolated image smoke, strict runtime guard, and post-deploy replay (`ok=true`, `setup_trace_findings=0`, `trace_coverage_ok=true`). Built and loaded `chili-app:codex-budget-telemetry-20260702-1515` (`sha256:d1f39ad2aa482e2e7af4699e8c80b793e89f8e9f24b7ebb46c2452ac1f7480bf`) after zero-active reload preflight passed.

**Replay boundary:** Replay v3 now has a scheduler-batch model for multi-session slot starvation, venue availability, order-call/risk budget accounting, missed expected-PnL attribution, and a DB-shaped row adapter that derives quality, queue age, expiry urgency, tick-arm state, and expected PnL from `TradingAutomationSession`-style snapshots. This is enough to regression-test adapter-unavailable starvation using live-session-shaped inputs. It is still not enough to claim live scheduler PnL min/max because it does not yet drive the real DB-backed runner loop, market-data/fill replay, or delayed-opportunity attribution from historical sessions.

2026-07-02 premarket regression restore: Claude's warning about fork regressions was partly correct. The current worktree had live/risk imports for `_live_ofi_microprice`, `_live_book_imbalance`, `_live_trade_flow`, `_live_flow_slope`, `_live_realized_vol`, `is_real_entry_outcome`, and `catalyst_grade_rank`, but the provider/helper definitions were missing. These are not redundant with the recent event-driven/replay fixes: they are compatibility primitives used by entry features, live entry vetoes, add confirmation, smart-hold/exit, run-R/streak math, and catalyst-conviction sizing. Restored them as fail-neutral helpers: stale/missing L2 or prints return `None`, never a hard block; never-entered outcomes are filtered out of realized-entry math; weak/fake catalyst evidence suppresses size-up rather than vetoing entry. Added `tests/test_momentum_dynamic_import_contracts.py` so future fork/image rebuilds cannot delete helpers still imported by live branches.

2026-07-02 hidden flag follow-up: the "7 dropped env flags" warning was also real, and the broader app-wide scan found 24 total hidden `chili_momentum_*_enabled` controls that were used through `getattr` but absent from `Settings` and/or readiness. The original 7 were `chili_momentum_replay_regression_enabled`, `chili_momentum_universe_uncapped_enabled`, `chili_momentum_tape_delta_ignite_enabled`, `chili_momentum_attention_leadership_enabled`, `chili_momentum_premarket_gap_full_universe_enabled`, `chili_momentum_live_eligible_recency_grace_enabled`, and the associated recency seconds knob. The additional 17 were entry-gate/setup controls in `entry_gates.py`: ABCD, ORB, VWAP reclaim, red-to-green, double-bottom, first-pullback, deep reclaim, flush dip-buy, wick reclaim, backside VWAP reclaim, raw explosive break, explosive floor, flow veto, extension veto, backside veto, red-volume exhaustion veto, and deep-reclaim dip-buy. These were first-classed with defaults preserving current behavior and registered in operator-visible feature readiness. New regression `test_no_app_momentum_enabled_getattr_is_absent_from_settings` now fails if another app-wide momentum enabled flag is used without a `Settings` field. Current fallback audit reports `missing_settings_count=0`, `missing_fallback_sites=0`, and `missing_default_false_controls=0`; runtime parity is now covered by the current canonical image and runtime verifier named in the opening verdict. Strict Replay still fails closed with missing opportunity labels and PnL-minmax evidence, so this restore is runtime-safety/mechanical readiness, not a PnL certification.

2026-07-02 verifier safety follow-up: running the full runtime verifier against the live worker exposed a verifier-side live-ops hazard: the import-heavy Ross symbol-resolution smoke returned 137 and the canonical worker restarted under `unless-stopped`. Patched `scripts/verify_momentum_worker_runtime.py` with `--smoke-image` so import-heavy contract checks can run in an isolated disposable image while the live worker checks remain read-only/lightweight. `tests/test_verify_momentum_worker_runtime.py` passed 54 tests, compile passed, and `python scripts/verify_momentum_worker_runtime.py --smoke-image chili-app:main-clean-codex-regression-restore-20260702-044118` returned `momentum_worker_runtime_ok`. Read-only Docker state after the fix showed exactly one canonical momentum worker, healthy, still on `chili-app:main-clean-codex-rosslane-turnover-20260701-161914`. Therefore runtime is mechanically healthy, but the latest regression-restore image is not yet the running worker.

2026-07-02 deploy-readiness guard follow-up: added `--expected-image` to `scripts/verify_momentum_worker_runtime.py`. This makes image drift a first-class preflight failure instead of relying only on compose/running-image equality. The guard first failed correctly while the older `rosslane-turnover` worker was still running; after the final coordinated reload it passed against `chili-app:main-clean-codex-regression-restore-20260702-060231`. `.codex_last_image_tag` and `.env` now point at that final image so future Compose recreates do not roll back.

2026-07-02 deterministic replay/visual proof follow-up: expanded the non-live proof set for the active setup audit. Setup-trace/replay/batch/anticipation group passed 87 tests; Ross event/feed/live-loop hygiene passed 25 tests; audit/live-replay CLI and Ross incident audit passed 25 and 23 tests respectively. Ross visual evidence audit initially failed because local evidence folders had `video.mp4`, timestamped transcript, and hundreds of frames but no duplicate `transcript_flat.txt`; patched `audit_ross_visual_evidence.py` so timestamped transcript text is sufficient while video/keyframes remain mandatory. Visual audit now passes 7 tests. `tests/test_live_replay_export.py` passed all 9 tests in one long DB-backed run (7m53s), proving live-row export, setup attribution, venue-unavailable inference, read-only audit summaries, and adapter-unavailable starvation evidence; this is still a replay/telemetry certification, not live PnL min/max certification.

2026-07-02 live-load follow-up: after read-only preflight found exactly one canonical worker and `active_like_count 0`, reloaded only `momentum-exec-worker` to the regression-restore image and updated `.env` `CHILI_MOMENTUM_EXEC_IMAGE` so future Compose recreates do not roll back to `rosslane-turnover`. A concurrent/local source change exposed one more hidden flag, `chili_momentum_independent_smallcap_a_plus_enabled`; first-classed it with default `true` to preserve existing behavior, registered it in readiness, rebuilt, and reloaded again with zero active-like sessions. Final live image: `chili-app:main-clean-codex-regression-restore-20260702-060231` (`sha256:9b53b801b43a6998c352dc2cce68181dd38ba8f0577ef7da878134af53ea3d7e`). Post-reload Docker state: exactly one `chili-clean-recovery-momentum-exec`, final image, healthy, `restart=unless-stopped`. Strict post-deploy verifier passed: `python scripts/verify_momentum_worker_runtime.py --smoke-image chili-app:main-clean-codex-regression-restore-20260702-060231 --expected-image chili-app:main-clean-codex-regression-restore-20260702-060231` returned `momentum_worker_runtime_ok`; in-container process health returned `momentum_exec_process_health_ok`; post-reload active-like live sessions remained 0. Robinhood Agentic readiness probe reported `robinhood_agentic_mcp_adapter_enabled=true`, token bundle present/routable, `execution_ready=true`, `broker_ready_for_live=true`, and `runnable_live_now=true`. The runtime verifier now routes import-heavy symbol-resolution and starter-alias smokes through `--smoke-image` so live preflight stays lightweight. No manual orders were placed.

2026-07-02 lifecycle guard follow-up: after reload, Docker events exposed two host-side bounces of the canonical worker: `kill signal=15`, then `kill signal=9`, `die exit=137`, and auto-restart under `unless-stopped`. This was not a Python strategy exception, but it is a real live-readiness risk if another agent/controller is rotating the worker. Patched `scripts/verify_momentum_worker_runtime.py` so strict preflight derives a quiet window from the container healthcheck (`max(start_period, interval * retries)`) and fails on recent `kill`/`stop`/`die`/`restart`/`oom` events during that window. This is a deterministic hard safety guard, not market soak. Also tightened source freshness to long-running worker modules only; timestamp churn in external monitor/readiness scripts such as `ross_live_monitor_snapshot.py` must not force a worker restart or become a dark flag. Validation: `tests/test_verify_momentum_worker_runtime.py` passed 59 tests, compile passed, the lifecycle guard initially failed correctly while the bounce was recent, then strict runtime verification passed after the health-derived quiet window cleared.

2026-07-02 premarket telemetry/regression follow-up: Claude's reported "deleted features" are not redundant with the Ross/event-driven fixes. `_live_ofi_microprice`, `read_ladder_distribution`, `read_target_level_trade_prints`, `is_real_entry_outcome`, and `catalyst_grade_rank` remain live dependencies for entry features, L2/tape confirmation, add/hold/exit logic, realized-entry feedback, and catalyst-conviction sizing. The then-running `main-clean-codex-regression-restore` image smoke-verified those helpers in-container; the current canonical image named in the opening verdict contains the same restored-helper contract plus later Replay label guardrails. The separate "7 dropped env flags" class is covered by a regression that treats `CHILI_MOMENTUM_EXEC_*` as Compose launcher proxies and requires other `CHILI_MOMENTUM_*` envs to map to first-class `Settings`; focused flag tests passed. A new live audit then exposed an alias-telemetry drift: `live_entry_pre_candidate_ross_shape_block` / A-floor skipped rows could carry raw `trigger_reason` without canonical setup coverage, making covered aliases such as `abcd_break_tick_ok` and `tick_first_pullback_scalp` appear missing from structural-stop/A-floor coverage. Patched `live_runner.py` so pre-candidate tick-revalidation blocks emit `setup_coverage=structural_a_setup`, patched sizing-floor skip events to merge `_entry_trace_event_payload`, and patched `setup_trace_audit.py` to infer coverage from `setup_coverage` while still requiring real stop levels on actual candidate/order events. Validation passed: 23 setup-trace/live-runner alias tests, 33 restored-helper/feature-flag tests, 91 Replay v3/live-replay/audit tests, compile smoke, isolated in-image helper smoke, and runtime guard with new image as `--smoke-image`. Built but did not live-reload `chili-app:main-clean-codex-regression-restore-telemetry-20260702-0748` (`sha256:bb692cad2c2566ef6a5a1f1ae33f11c288af5e9fc1090045aa8e3ac96f713cdc`) because the canonical worker had 29 active `watching_live` sessions. This paragraph is historical lineage; current runtime truth is the opening verdict and supersession note.

2026-07-02 07:53 PT historical status check: canonical worker remained single and healthy on `chili-app:main-clean-codex-regression-restore-20260702-060231`; read-only DB preflight found 30 fresh active-like `watching_live` sessions, so the telemetry image was intentionally left pending and documented in `docs/STRATEGY/QUEUED/f-momentum-telemetry-image-pending-reload.md`. Strict runtime guard passed again with live image as `--expected-image` and pending telemetry image as `--smoke-image`. That earlier read-only live replay audit reported clean setup trace coverage and `pnl_minmax_claim_ready=false`; its scheduler-priority claim semantics are superseded by the current stricter evidence rule, which requires observed scheduler pressure or delayed selection rather than a single-snapshot shape alone. This is historical lineage, not current scheduler/PnL certification.

2026-07-02 07:57 PT status check: canonical worker remained single and healthy on the same running image, and active-like live sessions increased to 31 fresh `watching_live` rows. `.env` and `.codex_last_image_tag` still point to the running verified image, not the pending telemetry image, so an accidental Compose recreate will not silently switch images before the zero-active preflight. Runtime/feature guard tests passed 61 tests, and read-only live replay audit again showed `setup_trace_findings=0`, `trace_coverage_ok=true`, and `pnl_minmax_claim_ready=false` for the expected Replay boundary.

2026-07-02 08:18 PT replay-snapshot follow-up: closed the next Replay v3 evidence gap locally. `plan_live_runner_batch_sessions` now emits best-effort `live_replay_scheduler_snapshot` telemetry behind first-class advisory flag `chili_momentum_live_runner_replay_snapshot_enabled=true`. The payload records all considered candidate rows, selected IDs, free prefilter results, venue health, and order/risk budgets derived from the actual batch `work_limit`; it does not alter ordering, broker calls, capacity accounting, or order routing. Serial validation passed 62 tests across live replay export, Replay v3 sizing/PnL, audit CLI, feature flags, and batch planner regressions; compile and in-image import smoke passed. Built superseding pending image `chili-app:main-clean-codex-regression-restore-replay-snap-20260702-0812` (`sha256:35bc671370a3ca4f5a1ad32028c593e9b4fe30cea56bf2c03775adb53c475d82`). It is not live-loaded because read-only preflight found 32 active `watching_live` sessions. During smoke, strict runtime guard caught a fresh external kill/restart of the canonical worker at 2026-07-02 15:16:41-15:16:42 UTC; the worker auto-restarted healthy on the same running image, and the guard passed again after the health-derived quiet window. This confirms the lifecycle guard is doing its job: fresh lifecycle events are a real deterministic preflight failure until the health-derived quiet window clears, not a passive-observation requirement.

2026-07-02 08:29 PT verifier follow-up: hardened `scripts/verify_momentum_worker_runtime.py` with a `replay_scheduler_snapshot_smoke` stage so future image rebuilds cannot drop the Replay v3 scheduler snapshot bridge silently. With `--smoke-image`, the guard validates the pending image has the first-class flag, callable snapshot emitter, venue-state helper, event type, and source markers. `tests/test_verify_momentum_worker_runtime.py` passed 61 tests. After another external restart at 2026-07-02 15:24:56-15:24:57 UTC, strict guard later failed `source_reload_freshness` because local `live_runner.py` is newer than the still-running image; the same guard passed with only source freshness skipped. Current interpretation: the live worker is otherwise mechanically healthy, but full strict deploy-readiness is intentionally not green until the pending image is loaded or the source-freshness caveat is accepted for the no-reload period.

2026-07-02 08:38 PT reload-preflight follow-up: hardened `scripts/verify_momentum_worker_runtime.py` again with optional `--require-no-active-like-sessions`. This makes the reload rule executable: before rotating the canonical worker, the guard must query live runnable/holding states and fail if any active watchers or positions exist. Focused verifier tests now pass 63 tests. Targeted preflight failed correctly with `active_like_sessions_present:count=32`, so the pending replay-snapshot image remains not live-loaded. A repeated external kill/restart occurred around 2026-07-02 15:36:11-15:36:13 UTC during a full guard run; worker auto-restarted healthy on the same running image. Avoid repeated full strict guard runs while active watchers remain unless needed; use targeted zero-active preflight and require lifecycle quiet before reload.

| Segment | Current Status | Evidence / Anchor | Verdict |
|---|---|---|---|
| Ross universe source | Equity lane no longer treats generic broad `MomentumSymbolViability.live_eligible` as authoritative. Auto-arm and risk boundary require Ross small-cap profile/evidence. A final pre-submit recheck now exists before broker place. | `risk_evaluator.py` `_ross_lane_universe_check`; `live_runner.py` `_pre_submit_ross_universe_block`; tests `test_ross_equity_lane_*`, `test_pre_submit_ross_universe_block_*`. | PARTIAL: code green; runtime guard must pass before live-complete claims. |
| Tick/tape wait -> arming | Tick-armable waits now include break/retest/reclaim/VWAP/MA/bull-flag/Ross starter wait reasons, and live runner stores `watch_break_level` when a valid level exists. | `entry_gates.py` `TICK_ARMED_WAIT_REASONS`; `live_runner.py` watch-level handling; `test_ross_starter_wait_*`, `test_momentum_tick_scalp.py`. | PARTIAL/WORKING in unit scope. |
| Micro/event-driven frame | Adaptive micro frame exists with `micro_frame_used` and explicit `fallback_reason`. TC/LHAI live evidence showed `micro_frame_used=5m`, `fallback_reason=disabled` because `chili_momentum_micro_pullback_primary_enabled=True` did not activate the dense-frame selector when `chili_momentum_micropull_enabled=False`. Current worktree derives micro-frame enablement from the active equity setup, so primary micro pullback can use IQFeed 15s bars and fall back only for `thin_ticks`/`stale_nbbo`/`no_iqfeed`. | `live_runner.py` `_select_entry_trigger_frame`; tests `test_micro_frame_selector_uses_setup_derived_micro_when_primary_enabled`, `test_micro_frame_selector_falls_back_with_telemetry_when_setup_micro_thin`. | FIXED in code; needs runtime/replay proof for each incident class before Ross-complete claims. |
| Entry candidate telemetry | `live_entry_candidate_detected`, pending-place, rewatch, submit, and fill-side events carry the old flat fields plus a canonical nested `setup_trace` envelope: setup alias, trigger reason, source wait, structural levels, stop model, micro frame, structural-stop coverage, A-floor coverage, and sizing-floor summary when evaluated. This closes the "only wait reason, no structure" telemetry gap for JEM/GVH/CUPR-style audits. | `live_runner.py` `_entry_trace_event_payload`, `_entry_candidate_event_payload`; tests `test_entry_trace_payload_carries_canonical_setup_envelope`, `test_entry_candidate_payload_marks_uncovered_alias_in_trace`; current read-only replay/setup audit. | RUNTIME-LOADED / TRACE-COVERED: current worker audit has `setup_trace_findings=0` and `trace_coverage_ok=true`; full lifecycle/PnL proof still needs actual entry/add/exit rows. |
| A-setup alias coverage | A-floor allowlist now includes Ross aliases: pullback, first/micro pullback, VWAP, HOD/flat-top/blue-sky, ORB, bull flag, false break, deep reclaim, ABCD, double bottom, cup/handle, raw explosive, tape hold. Floor math uses the geometric mean of active soft shrinkers rather than a fixed half-size override; hard blockers still win. | `risk_policy.py` `_A_SETUP_SIZE_FLOOR_TRIGGER_REASONS`, `_combined_soft_floor_fraction`, `apply_a_setup_combined_size_floor`. | PARTIAL: math is better; live sizing outcomes still need replay/live parity. |
| Structural stop coverage | Structural-stop allowlist covers the broad Ross alias family, including `abcd_break_tick_ok`, and candidate telemetry flags `setup_structural_stop_covered`. | `live_runner.py` `_STRUCTURAL_STOP_TRIGGER_REASONS`, `_entry_trigger_setup_coverage`. | PARTIAL/WORKING in code; needs full lifecycle replay. |
| Stale pre-submit latency | TC showed `entry_pre_submit_internal_latency_s=10.893 > max 6.0`. Current worktree blocks/re-watches stale pending-place paths instead of refreshing the clock and continuing to submit. | `live_runner.py` `_pre_submit_stale_path_block`; `test_pre_submit_stale_path_blocks_internal_latency_without_refreshing_clock`; final strict verifier with expected image passed after source freshness caught and cleared one post-build timestamp drift. | FIXED in code and runtime-loaded; still needs incident-class EV attribution, not market soak. |
| Batch starvation | Adapter-unavailable / wrong-venue / terminal pre-entry rows no longer consume useful batch capacity; later eligible equity rows can still be selected within the same limit. This is a prefilter/slot-accounting fix, not a blind limit raise. Replay v3 now includes pure scheduler-batch regression plus DB-shaped row conversion for this class. | `live_runner.py` `plan_live_runner_batch_sessions`, `_BATCH_FREE_SKIP_REASONS`; `replay_v3.py` `replay_scheduler_batch`, `replay_scheduler_candidates_from_live_rows`; tests `test_batch_prefilter_adapter_unavailable_does_not_starve_later_equity`, `test_scheduler_replay_prefilters_unavailable_venue_without_starving_equity`, `test_scheduler_replay_live_rows_do_not_starve_equity_behind_unavailable_adapter`. | FIXED in code; real runner-loop/PnL replay still partial. |
| Add/remainder path | Early-arm/trailing fixes exist in prior handoff, and anticipation/remainder tests exist. TC/LHAI exposed lifecycle/idempotency issues around restart and slow scheduler path; current worktree now finishes/recycles cooldown rows before stale entry logic and clears prior order lifecycle on same-session recycle. Setup-trace audit now has ordered lifecycle certification for anticipation remainder and runner adds: entry, add submit, add fill/no-fill, partial exit, trailing arm, and final exit are summarized per session with non-blocking issue counts. Positive-EV certification still requires post-deploy telemetry carrying those stages, but observation windows are advisory telemetry, not enablement blockers. | `2026-06-30_momentum-lockout-premarket-pullback-handoff.md`; `setup_trace_audit.py`; `tests/test_momentum_live_anticipation_remainder.py`; `test_live_cooldown_finishes_instead_of_same_session_recycle`, `test_live_cooldown_recycle_clears_prior_entry_lifecycle`, `test_setup_trace_audit_certifies_ordered_anticipation_remainder_lifecycle`, `test_setup_trace_audit_certifies_runner_add_only_after_trailing_arm`. | PARTIAL/DETERMINISTICALLY AUDITABLE: lifecycle bug fixed locally; ordered replay/audit proof exists; fresh telemetry still needed for EV attribution. |
| Event-driven admission vs 10s auto-arm | Ross event admission and the live runner loop are the primary path: IQFeed/WS events can admit a proven Ross small-cap through `begin_live_arm -> confirm_live_arm` and immediately tick the new session. The hidden trap was deploy config: scheduler auto-arm had previously recreated the 10s path. Current read-only Docker inspect shows scheduler live runner, auto-arm scheduler, auto-arm fallback, and batch fallback env flags are false on the canonical worker; `verify_momentum_worker_runtime.py` now completes and returns `momentum_worker_runtime_ok`. | `ross_event_admission.py` `admit_ross_event`; `live_runner_loop.py` `_handle_iqfeed_notify_payload`; `config.py` `chili_momentum_auto_arm_live_scheduler_enabled default=False`; `docker-compose.yml` `CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED:-false`; tests `tests/test_ross_event_admission.py`, `tests/test_momentum_auto_arm.py`; compose config render; 2026-07-02 read-only Docker inspect and runtime verifier. | MECHANICALLY READY; full event-driven/live PnL proof still partial. |
| Live restart/orchestration safety | External Docker starts previously restarted the old live worker after manual stops and even flipped restart policy back to `unless-stopped`. The disabled placeholder that occupied `chili-clean-recovery-momentum-exec` was a safety mistake and is now quarantined by ops; the real Ross hard-gate worker is canonical again. `.env` live momentum defaults remain false for generic compose paths; `docker-compose.yml` no longer hardcodes live runner defaults to `1`; a dedicated opt-in `momentum-exec-worker` compose service exists under profile `live-momentum`. Runtime preflight now fails if Docker events show recent external kill/restart inside the healthcheck-derived stabilization window. | Side-thread inspect: placeholder renamed to `chili-clean-recovery-momentum-exec-placeholder-quarantined-20260701-1145`; real worker renamed back to `chili-clean-recovery-momentum-exec`; `docker-compose.yml` live runner defaults `${...:-0}` and service `momentum-exec-worker`; `verify_momentum_worker_runtime.py` `evaluate_worker_lifecycle_quiet`; tests `test_worker_lifecycle_quiet_*`; 2026-07-02 strict verifier passed after quiet window. | MECHANICALLY READY WITH GUARD; any fresh kill/restart makes preflight fail until health-derived quiet window clears. |
| RH Agentic readiness | Operator readiness now treats `robinhood_agentic_mcp` as its own Robinhood/Agentic rail instead of falling through to Coinbase readiness. `.env` carries `CHILI_EQUITY_EXECUTION_RAIL`, Agentic account pin, token-file path, and `CHILI_MOMENTUM_EXEC_IMAGE`. A no-order Docker probe with `chili_rh_agentic_secrets:/app/secrets:ro` returned `RobinhoodAgenticMcpAdapter.is_enabled=True`; current readiness probe reports adapter enabled, token bundle present/routable, `execution_ready=true`, `broker_ready_for_live=true`, and `runnable_live_now=true`. | `operator_readiness.py` `_agentic_mcp_adapter_enabled`; `tests/test_equity_broker_readiness.py`; one-off read-only adapter probe; 2026-07-02 read-only Docker inspect, readiness probe, and runtime verifier. | MECHANICALLY READY; recheck before any future container rotation or broker incident. |

Current blockers before Ross-complete / PnL-certified status:

1. Keep exactly one real canonical momentum worker. A non-trading placeholder is not live coverage, and duplicate workers are a hard safety blocker.
2. Keep the full runtime guard/preflight green before using live runtime as proof. The guard now includes image alignment, source freshness for long-running worker modules, and health-derived lifecycle quiet; this is still mechanical readiness, not PnL proof.
3. Prove event-driven micro path on actual incident classes: if IQFeed is healthy, use tick/5s/10s/15s; emit explicit fallback when not.
4. Keep strict Replay v3 source-label guards: current `JEM CANF DXF TC LHAI` replay remains fail-closed with `label_ready_symbol_count=0`, so PnL min/max is not ready.
5. Use the ordered add/remainder and exit-management lifecycle audit on post-deploy rows for EV attribution; current deterministic tests prove lifecycle accounting and regressions, not positive EV.

Focused validation passed in the current worktree:

- `test_pre_submit_stale_path_blocks_internal_latency_without_refreshing_clock`
- `test_pre_submit_ross_universe_block_reuses_final_risk_boundary`
- `test_pre_submit_ross_universe_block_allows_verified_smallcap`
- `test_unavailable_adapter_blocks_preentry_but_not_held_position`
- `test_batch_prefilter_adapter_unavailable_does_not_starve_later_equity`
- `test_live_cooldown_recycle_clears_prior_entry_lifecycle`
- `test_live_cooldown_finishes_instead_of_same_session_recycle`
- `test_setup_trace_audit_certifies_ordered_anticipation_remainder_lifecycle`
- `test_setup_trace_audit_certifies_runner_add_only_after_trailing_arm`
- `test_setup_trace_audit_reports_runner_add_before_trailing_without_failing_trace_ok`
- `test_entry_trace_payload_carries_canonical_setup_envelope`
- `test_entry_candidate_payload_marks_uncovered_alias_in_trace`
- `test_micro_frame_selector_uses_setup_derived_micro_when_primary_enabled`
- `test_micro_frame_selector_falls_back_with_telemetry_when_setup_micro_thin`
- `test_scheduler_replay_prefilters_unavailable_venue_without_starving_equity`
- `test_scheduler_replay_priority_and_budget_missed_pnl_are_explicit`
- `test_scheduler_replay_builds_candidates_from_live_session_rows`
- `test_scheduler_replay_live_rows_do_not_starve_equity_behind_unavailable_adapter`
- `tests/test_equity_broker_readiness.py`
- `tests/test_momentum_tick_scalp.py`, `tests/test_ross_breakout_starter.py`, `tests/test_replay_v3_sizing_pnl.py`

Fresh non-live image smoke:

- Built `chili-app:main-clean-codex-setup-audit-20260701-1235` (`sha256:9dd9784506f3c2fe9798b4805b74ec38bd010bdd470b679bc6565405c33bd926`).
- Compile-only Docker smoke passed for `config.py`, `live_runner.py`, `live_runner_loop.py`, `ross_event_admission.py`, `replay_v3.py`, and `operator_readiness.py`.

## 2026-07-01 Codex Correction

The older body below is preserved as historical audit context, but the current-state facts above supersede it where they conflict:

- The canonical worker is no longer intentionally held by a placeholder; side-thread ops quarantined the placeholder and restored the real Ross hard-gate worker to `chili-clean-recovery-momentum-exec`.
- Historical 2026-07-02 Docker checks on `chili-app:main-clean-codex-regression-restore-20260702-060231` are retained below as lineage only. The authoritative current runtime is the 2026-07-03 boundary at the top of this report: `chili-app:codex-ross-hygiene-catalyst120-20260703-1035` with runtime verifier `momentum_worker_runtime_ok`.
- Tick/event-driven architecture is mechanically configured but not Ross-complete live proof until incident-class Replay/telemetry checks pass.
- Setup telemetry is no longer missing a canonical envelope: the current worktree emits nested `setup_trace` on live runner events and has focused tests for covered and uncovered aliases.
- Replay v3 can now prove adapter-unavailable scheduler starvation regressions from pure candidates and DB-shaped live-session rows, but it still cannot prove DB-backed scheduler PnL min/max until multi-session runner-loop replay is wired to real historical sessions/fills.

## Scope And Evidence Rules

This audit checks whether Ross-style momentum setups are present end to end:

`detect -> arm -> enter -> size -> add -> hold -> exit`

Evidence is local repo code, local tests, and existing strategy docs. I did not edit production code. I did not run the test suite during this audit. The working tree is dirty, so line anchors below describe the current working tree, not a clean HEAD or a proven deployed container.

Ross video/source rule: transcript text is discovery/index evidence only. For any important conclusion that a Ross setup should or should not have been traded, capture and review the relevant extracted video/chart frames or keyframes around the timestamp. The reviewed frame evidence must include, when visible, chart drawings, VWAP/HOD/pullback levels, candles, tape/L2 annotations, and screen-indicated context. Do not certify trade/no-trade correctness from transcript mentions alone.

Status terms for the retained historical matrix:

- `deployed working`: wired through the local runner and covered by meaningful tests.
- `deployed but unproven`: source/test pieces exist, but no full setup-specific live lifecycle proof.
- `deployed but bugged`: source/docs show the path exists, but evidence says it misses, starves, or fails operationally.
- `config-disabled`: present but guarded/off by default or not shown enabled locally.
- `missing`: not found in the local momentum runner lifecycle.

Current-state supersession note: the lifecycle matrix below was a 2026-07-01 local-source snapshot created before the later Ross-lane merges, runtime reloads, and certification report updates. Rows that say ABCD, bull flag, tape-hold, absorption, wedge, premarket pivot, or related setup aliases were "missing locally" are historical findings, not current truth. Use the `## Current Runtime Boundary`, `## Named Incident Coverage Matrix`, and `docs/STRATEGY/CC_REPORTS/2026-07-01_ross-warrior-playbook-certification.md` for current deployed status. The stale matrix remains useful only as a record of the drift class that caused earlier incidents.

## Executive Verdict

Historical 2026-07-01 local snapshot: CHILI then had one strong generic Ross lane, a shallow pullback/high-break trigger (`pullback_break_ok`) that could be armed, entered, sized with structural/vol-floored risk-first sizing, held, scaled out, trailed, and exited. That covered the skeleton for first-pullback / MA-VWAP pullback / bull-flag-like entries.

Current 2026-07-03 status is stronger than that historical snapshot: multiple named Ross handlers and aliases are now default-on/current-code verified in the certification report, the canonical worker emits setup trace envelopes, and runtime/feature-flag checks report no hidden fallback-false controls. The remaining gap is not "only one generic lane"; it is full current-window lifecycle and PnL attribution for actual entry/add/exit sequences, plus source-before-opportunity visual evidence for Ross trade/no-trade certification.

The largest operational gap is still named setup traceability plus size/add/exit proof under fresh current-runtime telemetry. Existing strategy docs record historical audited windows with zero anticipation remainder submissions/fills; those windows cannot certify current add behavior, and empty current windows must not be treated as hidden enablement blockers.

## Top P0 Findings

1. **Setup identity is partially repaired, not fully certified.** Local runner events now carry a nested `setup_trace` envelope, but named setup detectors such as `ma_vwap_pullback`, `bull_flag`, and `abcd_break_tick_ok` still need full lifecycle replay/live proof instead of relying on allowlist coverage alone.
2. **Add/remainder is present but still not proven in a full live lifecycle.** Source has `_anticipation_starter_plan`, `_handle_anticipation_remainder`, `live_anticipation_probe_sized`, `live_anticipation_remainder_submitted`, and `live_anticipation_remainder_filled`; tests cover the helper path. The queued live evidence still says recent production behavior was probe-only starvation.
3. **Deployment docs and local source disagree materially.** Docs cite much larger `entry_gates.py` / `live_runner.py` line numbers and functions such as `ross_abcd_confirmation`, `halt_resume_dip_trigger`, `pullback_add`, and `flag_breakout_add`. The local `entry_gates.py` is 797 lines and does not contain those local entry functions.
4. **Scanner/pattern coverage is not trade coverage.** ORB, HOD, VWAP reclaim, tape burst, and first-pullback patterns exist in generic scanner/pattern modules, but most are not consumed as setup-specific momentum live entries.

## Lifecycle Matrix

| Setup | Entry aliases / reasons observed | Code anchors | Lifecycle state | Blockers / dark flags | Telemetry | Tests / replay coverage | Concrete next fix |
|---|---|---|---|---|---|---|---|
| VWAP/MA pullback and reclaim | Local runner: `pullback_break_ok`, fallback `momentum_ok_*`; docs mention `ma_vwap_pullback`; local pure reclaim setup: `vwap_reclaim`; scorer reason: `vwap_reclaim_from_below` | `app/services/trading/momentum_neural/entry_gates.py:426`, `:667`, `:760`; `live_runner.py:2496`, `:2555`, `:2930`; `paper_runner.py:745`; `ross_momentum.py:378`, `:459`; `tight_false_break_entry.py:171`, `:196` | Detect/arm/enter/size/hold/exit: deployed working for generic pullback-break. Add: deployed but unproven/bugged. Distinct VWAP-reclaim entry alias: missing from runner. Overall: deployed but unproven. | `ma_vwap_pullback` appears in queued live evidence, not local code. `vwap_reclaim` pure gate is not wired to live/paper entry. Generic live events now carry `setup_trace`, but the distinct VWAP/MA alias still needs runner wiring. | `setup_trace`, `entry_trigger_reason`, `live_entry_candidate_detected`, `live_entry_submitted`, `live_anticipation_*`, `live_partial_exit`, `live_trail_ratchet`, `live_exit_filled`; still missing distinct VWAP setup alias. | `tests/test_pullback_break.py`, `tests/test_momentum_auto_arm.py`, `tests/test_momentum_live_anticipation_remainder.py`, `tests/test_momentum_asymmetric_exit.py`, `tests/test_tight_false_break_entry.py` pure only. | Normalize `ma_vwap_pullback` / `vwap_reclaim` into the setup alias taxonomy and wire `evaluate_tight_false_break_entry` as an alternate entry trigger with live/paper parity tests. |
| First pullback / micro pullback | `pullback_break_ok`, internal `raw_break`, `break_retest`; taxonomy `micro_pullback_continuation` | `entry_gates.py:269`, `:323`, `:426`, `:667`; `auto_arm.py:258`, `:342`, `:557`; `variants.py:29`; `strategy_params.py:44`; `live_runner.py:2519`, `:2531` | Generic first-pullback: deployed working through enter/size/hold/exit. Micro-specific setup label/add: deployed but unproven. Add: deployed but unproven/bugged. | No distinct `micro_pullback` live trigger or `live_micro_pullback_reentry_submitted` in local source. Deployment note reports 0 micro-reentry/add events in audited window. | Same generic pullback telemetry plus `live_anticipation_*`; no micro setup alias. | Pullback unit coverage and auto-arm selection coverage. Replay v3 models adaptive add policy but is fixture-driven. | Add setup alias selection before entry, then one live-runner test that flows `pullback_break_ok -> starter -> confirmed remainder -> scale-out/trail`. |
| Bull flag | Local alias not present; maps operationally to `pullback_break_ok`; generic parser/backfill maps `"bull flag"` to `compression_expansion` | `entry_gates.py:426`, `:667`; `pattern_family_backfill.py:65`; `live_runner.py:2555`, `:3248` | Enter/size/hold/exit: deployed working only as generic pullback-break. Named bull-flag setup and flag add: deployed but unproven/bugged. | No local `bull_flag` or `flag_breakout_add` runner path found. Docs say flag add was enabled but had 0 live events before a later claimed deploy. | Generic pullback events; no `bull_flag` event or setup trace. | Pullback-break tests indirectly cover the shape; no bull-flag-specific lifecycle test. | Add a `bull_flag` alias when pullback-break compression/volume conditions match, then prove the lifecycle and flag-add/remainder event in a deterministic runner test. |
| ABCD | Requested/mentioned alias `abcd_break_tick_ok`; docs mention `ross_abcd_confirmation`; neither found in local momentum runner | Docs only: `docs/STRATEGY/CC_REPORTS/2026-06-26_warrior-courses-reaudit.md:30`, `:187` | Missing locally for detect/arm/enter/size/add/hold/exit. | Major source/deploy drift: docs cite `entry_gates.py:3469`, but local `entry_gates.py` is 797 lines. | None local. | No local ABCD momentum lifecycle tests found. | Either port the deployed ABCD gate into local source with tests, or mark the docs stale and remove ABCD from claimed production coverage. |
| ORB / HOD / flat-top / blue-sky breaks | Generic scanner/pattern aliases: `bo_orb_break_above`, `1m Opening Range Break + Volume`, `5m HOD Reclaim with Volume`; local exit reason `breakout_failed_fast_bail` for flat-top hold failure | `pattern_engine.py:113`, `:318`; `scanner.py:590`, `:3541`, `:3562`; `entry_gates.py:670`; `live_runner.py:3248` | Detection/scanner: deployed working. Momentum entry lifecycle: missing/unproven. Flat-top hold failure exit: deployed working for pullback-break entries. | ORB/HOD/blue-sky are not wired as momentum entry aliases in local runner. Flat-top is exit/bailout logic, not entry detection. | Scanner signals and generic runner events; no setup-level ORB/HOD/blue-sky trace. | Scanner/bridge tests exist around ORB filtering; no end-to-end momentum runner test for ORB/HOD/blue-sky entry. | Convert scanner/pattern signals into explicit momentum entry candidates or keep them documented as selection-only, then add one ORB/HOD runner test. |
| Tape-hold early entry | Generic pattern `1m Tape-Speed Burst`; live add confirmation leg `tape_or_ofi_confirm` | `pattern_engine.py:159`; `live_runner.py:1408`, `:1415`, `:1499`, `:1549` | Missing as early entry. Deployed but unproven as add/remainder confirmer. | Tape is used as an optional confirmation leg, not a first-class entry trigger. No `tape_hold` alias or test proving early entry. | Confirmation legs appear in `live_anticipation_probe_sized` / `live_anticipation_remainder_*` payloads when available. | `tests/test_momentum_live_anticipation_remainder.py` covers confirmation submission without needing green bid; not a tape-hold entry test. | Add a fail-closed `tape_hold_early_entry` detector or explicitly classify tape as confirmation-only; add telemetry for which leg fired. |
| Halt resume dip | Docs mention `halt_resume_dip_trigger`; local momentum runner/gates do not contain it | Docs only: `docs/STRATEGY/CC_REPORTS/2026-06-26_warrior-courses-reaudit.md:30`, `:190` | Missing locally for detect/arm/enter/size/add/hold/exit. | Source/deploy drift. Local grep finds halt language in docs/speculative text, not a momentum entry trigger. | None local. | No local momentum lifecycle tests found. | Reconcile whether this exists in deployed image. If yes, backport with tests; if no, mark halt-resume dip unsupported. |
| Deep reclaim / false break reclaim | Pure module aliases: `false_break_reversal`, `vwap_reclaim`; success `tight_entry_ok` | `tight_false_break_entry.py:154`, `:171`, `:196`, `:281`; `tests/test_tight_false_break_entry.py:176`, `:183`; `scanner.py:2195`, `:2339` | Pure detector: deployed working. Live/paper entry lifecycle: missing. Overall: deployed but unproven / not runner-wired. | `ENTRY_TIGHT_FALSE_BREAK_RECLAIM` appears in deployment flags doc, but no local runner import/call. Deep reclaim as a distinct live setup is not present. | Pure decision debug only; no live setup event. | Strong pure tests for geometry/guards. No runner parity test. | Wire the pure gate behind a config flag in live + paper entry-gates, emit `setup_trace.setup= false_break_reversal|vwap_reclaim`, then test enter/size/add/exit parity. |

## Shared Lifecycle Evidence

Working generic pieces:

- Ross universe/scoring and freshness: `ross_momentum.py:169`, `:283`, `:378`; pipeline carries `ross_scores` at `pipeline.py:94`; viability tilts with Ross score at `viability.py:278`.
- Auto-arm probes entry triggers: `_entry_trigger_fires` at `auto_arm.py:258`; `run_auto_arm_pass` at `auto_arm.py:342`; fresh-watch path at `auto_arm.py:557`.
- Live entry gate: `live_runner.py:2496-2580` calls shared `momentum_pullback_trigger` first, then `momentum_volume_confirmation`.
- Risk-first sizing: `live_runner.py:2910-2956` uses `effective_stop_atr_pct`, `structural_or_vol_floored_atr_pct`, and `compute_risk_first_quantity`.
- Anticipation add: `_anticipation_starter_plan` at `live_runner.py:1427`; `_handle_anticipation_remainder` at `live_runner.py:1628`; submit/fill events at `live_runner.py:1793` and `:1609`.
- Hold/exit: breakout-or-bailout at `live_runner.py:3248`; max hold at `:3355`; runner trail at `:3409`; first target scale-out at `:3519`; final/cooldown flow through `:3646`.
- Paper parity for entry gate and structural stop: `paper_runner.py:745-778`.
- Shared scale-out/trail helpers: `paper_execution.py:33`, `:64`, `:155`, `:200`, `:216`, `:240`, `:280`.

Coverage highlights:

- `tests/test_pullback_break.py` proves shallow pullback, retest, runaway, deep-pullback rejection, and breakout-failed-to-hold helper behavior.
- `tests/test_momentum_auto_arm.py` proves auto-arm selection around `pullback_break_ok` and fresh-watch behavior.
- `tests/test_momentum_live_anticipation_remainder.py` proves adaptive starter sizing, explicit wait telemetry, no duplicate in-flight order, terminal-no-fill retry, and remainder submission without requiring green bid over average entry.
- `tests/test_momentum_asymmetric_exit.py` proves live and paper scale-out to breakeven, runner trail, and `trail_stop` exit.
- `replay_v3.py` is useful for policy comparison but explicitly fixture-driven: it does not replay live scheduler ticks, broker fills, RVOL math, or market-data gates.

## Recommended Next Fixes

1. Add a canonical setup trace envelope to live and paper entries: `setup_alias`, `detector`, `trigger_reason`, `structural_level`, `stop_model`, `sizing_model`, `add_policy`, `hold_policy`, `exit_reason`.
2. Promote the generic pullback gate into named aliases when conditions are met: `ma_vwap_pullback`, `first_pullback`, `micro_pullback`, `bull_flag`.
3. Wire tight false-break / VWAP reclaim into the same live + paper entry pipeline or remove the deployment flag from claims.
4. Resolve source/deploy drift before claiming ABCD, HOD/flat-top/blue-sky, halt-resume dip, pullback add, or flag-breakout add as deployed.
5. Add one full live-runner deterministic test that proves `entry -> starter -> remainder submitted/fill -> hold -> scale-out -> trail -> exit` with setup trace present.

## 2026-07-02 Premarket Regression Restore

Claude's warning about fork regressions was valid. These helpers were not redundant with the recent Ross hard-gate/setup fixes: live entry, add, exit, and sizing code still lazy-imported them, so deleting the exports made those features silently ineffective.

Restored locally as fail-neutral contracts:

- `ross_momentum`: `compute_is_ssr`, `ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED`, `ROSS_ELIGIBILITY_*`, squeeze entry/exit helpers, squeeze fuel signal, front-side strength/size tilt.
- `candles`: `_ema`, `is_bounce_curl_candle`, `bounce_curl_from_df`, `macd_hist_rollover_from_df`.
- `auto_arm`: 24h eligibility/whitelist helpers, agentic reject recorder/detector, entry reject cooldown hook, and fatigue/hot-cold/prime-window size multiplier contracts. The size multipliers are neutral until backed by a reviewed adaptive model; this prevents new dark-blocking while preserving the import contract.
- `short_mechanics`: fail-neutral provider adapter returning no borrow mechanics until a real provider implementation is wired.

Validation:

- Relative import contract sweep: 774 momentum-neural relative imports checked, 0 missing.
- Focused tests: `tests/test_momentum_relative_import_contracts.py`, `tests/test_momentum_dynamic_import_contracts.py`, `tests/test_momentum_pipeline_live_readers.py`, `tests/test_news_catalyst.py`, and `tests/test_momentum_feedback_phase9.py::test_is_real_entry_outcome_filters_never_entered_rows` passed (`13 passed`).
- Compile smoke passed for touched modules/tests.
- Additional live-runner validation found and fixed an exit-poll telemetry regression: RH raw `state=filled` now returns and records `broker_order_status=filled` on the confirmed full-fill path instead of losing broker truth on the early return.
- Targeted broader tests passed after the restore: auto-arm (`44 passed`), anticipation/batch (`15 passed`), runtime/IQFeed loop (`56 passed`), Ross admission/feed/live-eligible hygiene (`22 passed`), restored import/behavior contracts (`11 passed`), `test_live_exit_poll_raw_robinhood_filled_then_flattens`, `test_batch_prefilter_adapter_unavailable_does_not_starve_later_equity`, and `test_wide_live_bbo_blocks_market_entry_without_error`. Avoid running DB-backed live-runner tests in parallel; parallel truncates produced PostgreSQL deadlocks unrelated to the strategy code.
- New behavior regression coverage: `tests/test_momentum_restored_helper_behaviors.py` proves restored helpers are fail-neutral on missing data, squeeze size-up requires all confirming legs and stays bounded, front-side tilt is size-down-only/neutral on stale tape, candle curl/MACD helpers fail safe, and auto-arm restored hooks expose 24h eligibility + agentic reject state without introducing size blockers.
- Docker image built: `chili-app:main-clean-codex-setup-audit-20260702-1135`, manifest digest `sha256:bc9ce4559fcecb12075bc38b08c7c6c0b90ad08896bdb7d27b600ee0c1e0cdfb`.
- In-image compile smoke passed for restored momentum modules, live runner, entry gates, risk policy, and runtime verifier. The image excludes tests, so test compile remains a local-worktree validation.
- In-image restored-contract import smoke passed with a dummy `DATABASE_URL`.
- Runtime guard before live reload reports source freshness if the canonical worker is older than the repaired source; this is an expected deploy/reload caveat, not a code-test failure.
- Strict Replay v3 for `JEM CANF DXF TC LHAI` still fails closed with `label_ready_symbol_count=0` and `pnl_minmax_label_ready=false`; this is expected because chart/source labels are not certified and replay includes `counterfactual_notional_uncapped_no_broker_state`. Do not claim scheduler/PnL min-max readiness from this run.

## 2026-07-02 08:45 PT Superseding Pending Image

Built superseding pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-20260702-0845`
- Digest: `sha256:8e49831003311e835b8cf2fe10d8f49ea3b6d5aa1f71e3422240154618d030dd`

This image supersedes the earlier pending replay-snapshot tag because it includes the latest runtime
verifier hardening as well as the restored helper contracts, setup-trace telemetry repair, and live
batch replay-snapshot emission.

Validation:

- Focused runtime verifier + batch starvation regression suite passed: `64 passed`.
- In-image compile smoke passed for config, live runner, live replay export/audit, Replay v3, and
  runtime verifier.
- In-image import smoke confirmed the replay snapshot flag is first-class/default-on and the
  snapshot emitter, venue-state helper, replay-snapshot smoke, and zero-active reload preflight are
  callable.
- Reload preflight failed closed on `active_like_sessions_present:count=32` with active
  `watching_live` rows, so the image was not live-loaded. This is intentional: do not rotate the
  canonical worker until the active-like count is zero and broker/live-worker preflight is clean.

Verdict for this image: code/build/smoke READY as a pending reload artifact; live reload NOT READY
while active-like sessions remain.

## 2026-07-02 09:00 PT Compatibility Supersede

Built newer pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-20260702-0900`
- Digest: `sha256:de0078f3ceaf97b3e6d985ef9d32140675a9f841aa4128f1dc9039829101ffa8`

Reason: the restored OFI/L2 readers, real-entry outcome filter, and catalyst rank helper were
present in their native modules (`pipeline`, `outcome_labels`, `catalyst`), including in the
currently running worker image, but they were not runner-facing compatibility exports. The newer
image exposes those helpers from `live_runner` as well. This is a compatibility/smoke hardening
patch, not a strategy behavior change.

Validation:

- Native running-worker contracts checked directly: `pipeline` OFI/L2/target-print readers present,
  `outcome_labels.is_real_entry_outcome(cancelled_pre_entry)=False`,
  `outcome_labels.is_real_entry_outcome(stop_loss)=True`, catalyst weak/fake rank returns `0`.
- Local contract tests plus pipeline/outcome/catalyst tests passed: `6 passed`.
- Focused runtime verifier, batch-starvation, and dynamic import contracts passed: `67 passed`.
- In-image compile smoke passed.
- In-image import smoke confirmed native contracts and `live_runner` compatibility exports.

Caveat: a full runtime verifier attempt during active watches returned
`runtime_guard_stage_failed:ross_event_admission_config:returncode=137` and the canonical worker
restarted under orchestration on the same running image. Avoid repeated full verifier runs while
active `watching_live` rows exist; use direct read-only checks and only reload when the
zero-active-session preflight is clean.

Verdict for this image: latest pending artifact READY for a coordinated reload after active-like
sessions clear; do not reload during the current active watchlist.

## 2026-07-02 09:15 PT Lightweight Preflight Supersede

Built latest pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-20260702-0915`
- Digest: `sha256:e083863a44d491a8378e0737dd304e698159d64fa4ac94381961b84990b395a3`

Reason: the full runtime verifier can run heavy import/config smoke stages and has repeatedly
coincided with/revealed worker restarts during active watches. This image keeps the restored
contracts and compatibility exports, and adds a cheap reload-only CLI:

`python scripts/verify_momentum_worker_runtime.py --active-session-preflight-only`

That command runs only the active runnable/holding session DB query and exits. It is the right
reload/no-reload check near market open; the full verifier remains useful for quiet windows.

Validation:

- Verifier/import tests passed: `68 passed`.
- Compile smoke passed for verifier, live runner, replay/export modules, and config.
- In-image smoke confirmed native restored contracts, runner compatibility exports, and the
  lightweight preflight CLI marker.
- Live lightweight preflight failed closed with `active_like_sessions_present:count=32`; the
  canonical worker stayed running on the current live image.

Verdict: latest pending artifact READY for a coordinated reload only after active-like sessions are
zero. Current live reload remains NOT READY while active watches exist.

## 2026-07-02 09:25 PT Replay Evidence Reason Counts

Built latest pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-20260702-0925`
- Digest: `sha256:7a8b19c407337b87a64162332beb600dfee2605fead6ff7e839d9bae8fa4dc85`

Reason: the live replay audit previously reported aggregate `free_skip_count` without showing
which reasons were free versus capacity-consuming. That made adapter-unavailable/batch-starvation
claims harder to prove or refute. The audit now reports:

- `decision_reason_counts`
- `free_skip_reason_counts`
- `capacity_consuming_reason_counts`

Validation:

- Replay/audit evidence tests passed: `45 passed`.
- In-image compile smoke passed.
- In-image synthetic timeline smoke produced `free_skip_reason_counts={'pre_entry_terminal': 1}`
  and `capacity_consuming_reason_counts={'selected': 1}`.

Current live audit boundary after the 16:09 UTC worker restart:

- `session_rows=32`
- `setup_trace_events_seen=500`
- `setup_trace_traces_seen=0`
- `setup_trace_findings=0`
- current-window trace certification fails with `no_setup_trace_events`
- lifecycle certification remains not ready: complete lifecycle count `0`
- scheduler evidence is explicit: `decision_reason_counts={'pre_entry_terminal': 32}`,
  `free_skip_reason_counts={'pre_entry_terminal': 32}`, and no capacity-consuming skip reasons

Interpretation: the current window does not prove a setup/lifecycle failure; it lacks post-restart
setup-trace events. It also does not show adapter-unavailable starvation in the current window.

## 2026-07-02 09:40 PT Tick-Wait Setup Trace Repair

Built latest pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-20260702-0940`
- Digest: `sha256:7bc897149fc7913e2906a66e37c2ee4cb33251432282a23ccccee2c2a1861516`

Reason: stricter audit showed a real telemetry gap in the currently running image. Historical
`live_entry_tick_scalp_wait` events carried debug fields such as `pullback_low`, but no canonical
`setup_trace`, no `setup_alias`, and no wait/level/tick-arm proof. That made the prior audit too
optimistic: it could count anonymous payloads as traces without proving setup identity.

Fix:

- Future `live_entry_tick_scalp_wait` emits canonical setup trace:
  `setup_alias=tick_first_pullback_scalp`, `source_wait_reason`, tick micro-frame, and pullback
  high/low when available.
- The setup trace audit no longer treats raw level fields alone as a valid trace.
- Anonymous `live_entry_tick_scalp_wait` / `live_entry_trigger_wait` events now fail explicitly as
  `wait_event_missing_setup_trace`.
- CLI certification failures now compact duplicate reasons, e.g.
  `wait_event_missing_setup_trace=614`.

Validation:

- Focused setup/audit/live-runner tests passed: `46 passed`.
- In-image compile smoke passed.
- In-image smoke confirmed tick first-pullback wait trace emits
  `source_wait_tick_armed=True` and `source_wait_has_pullback_levels=True`.

Current broad DB audit after tightening:

- `setup_trace_findings=614`
- `setup_trace_finding_reasons={'wait_event_missing_setup_trace': 614}`
- samples are `live_entry_tick_scalp_wait`
- lifecycle still not certifiable because there are no entry-fill/add/exit stages in the audited
  setup-trace chain

Interpretation: this is now correctly classified as a telemetry blocker in the currently running
image. The pending image fixes future telemetry, but historical anonymous rows remain uncertified.

## 2026-07-02 09:50 PT Reload Preflight Hardening

Built latest pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-reloadpf-20260702-0950`
- Digest: `sha256:da809bb492563fd3ee473c8fa90f206cbf3d94d5280e9afc18677e62dc6ba334`

Reason: full runtime verification is too heavy for active-watch reload decisions. It imports and
smokes several strategy modules and has repeatedly coincided with/revealed worker restarts. The
reload decision needs a cheap fail-closed guard, not a full certification sweep.

New command:

`python scripts/verify_momentum_worker_runtime.py --reload-preflight-only --expected-image <current-running-image>`

This runs only:

- canonical worker uniqueness / placeholder / duplicate-live-worker check
- lifecycle quiet-window check
- optional expected running image alignment
- zero active-like live-session check

It skips transcript/config/source/replay/import-heavy smoke stages. Use full verifier only during a
quiet window.

Validation:

- Verifier tests passed: `68 passed`.
- In-image compile smoke passed.
- In-image smoke confirmed `--reload-preflight-only` and `--active-session-preflight-only`.
- Live reload preflight against the current running image failed closed on
  `active_like_sessions_present:count=32`, with no live reload/rotation.

Verdict: latest pending image is mechanically ready as a reload artifact, but reload remains NOT
READY while active-like sessions exist.

## 2026-07-02 10:00 PT Reload Preflight V2

Built latest pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-reloadpf2-20260702-1000`
- Digest: `sha256:1e55ac98d928aaf889aa30d4aaf3e69222abc3033e05bb1b5bb683948812e476`

Reason: the first `--reload-preflight-only` path still checked Docker lifecycle events before active
sessions. With active watches present, reload is already impossible, so lifecycle scanning is wasted
and can be slow. V2 checks active sessions before lifecycle quiet; if active rows exist, it fails
fast and does not scan Docker events.

Validation:

- Verifier tests passed: `68 passed`.
- Live reload preflight failed closed on `active_like_sessions_present:count=32` without the prior
  lifecycle timeout.
- In-image compile smoke passed.
- In-image smoke confirmed active-session preflight comes before lifecycle scanning in the
  reload-only path.

Use this latest tag and command for the next reload attempt:

`python scripts/verify_momentum_worker_runtime.py --reload-preflight-only --expected-image chili-app:main-clean-codex-regression-restore-20260702-060231`

## 2026-07-02 Feature Flag / Dark-Flag Audit

Ran the first-class momentum feature flag audit against the current worktree:

- `tests/test_momentum_feature_flags.py`: `12 passed`.
- Exact search for `getattr(settings, "chili_momentum_*_enabled", False)` in momentum code found
  no remaining hits.
- `audit_momentum_settings_fallbacks()` scanned `560` momentum settings fallback sites across
  `406` unique settings.
- Missing Settings fields: `0`.
- Missing fallback sites: `0`.
- Missing default-false controls: `0`.
- Registered operator-visible feature flag rows: `106`.
- Registered rows absent from Settings: `0`.
- Currently disabled operator-visible rows: `71`; these are visible with disabled reasons and proof
  gates, not silent absent defaults.

This closes the hidden default-false flag class for current momentum code: an implemented handler
should no longer disappear merely because a `chili_momentum_*_enabled` setting was absent. The
remaining OFF handlers still require case-by-case proof or explicit operator enablement; they are
not allowed to hide behind passive-observation wording. Current tests enforce that disabled
reasons and proof gates do not use passive observation as an enablement blocker.

## 2026-07-02 Regression Helper / Exec Env Guard

Claude's reported regression class is not redundant: the OFI readers, real-entry outcome filter,
and catalyst rank helper are live contracts. Current code and the running canonical worker now
expose those contracts from both their native modules and the `live_runner` compatibility surface.

Additional guard added: runtime verification now requires the full event-driven momentum lane env
contract, not only `live_runner=true`. The canonical worker must have live runner, event loop,
IQFeed tape/notify/poll fallback, Ross event admission, auto-arm, and Ross universe enabled; live
scheduler, batch fallback, auto-arm scheduler, and auto-arm scheduler fallback must be disabled.

Validation:

- Focused guard/import suite passed: `85 passed`.
- Compile check passed for the runtime verifier and touched tests.
- Read-only in-container import smoke confirmed the running worker exposes:
  `_live_ofi_microprice`, `read_ladder_distribution`, `read_target_level_trade_prints`,
  `is_real_entry_outcome`, and `catalyst_grade_rank`.
- Live reload preflight still fails closed on active sessions
  (`active_like_sessions_present:count=32`), so no reload/rotation was performed.

Superseding pending image built after this guard and the matching Compose-service env guard:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-reloadpf2-envguard2-20260702-1029`
- Digest: `sha256:ed444c1bf25bc144e9973972094f10526125eadd0f3e2fafa279d902b780b59d`

In-image validation passed: compile smoke, helper import smoke, runtime env-contract smoke, and
Compose-service env-contract smoke. The verifier accepted a healthy event-driven lane env and
rejected disabled IQFeed/Ross-event env with explicit reasons.

Runtime caveat: the canonical worker bind-mounts `./app` and `./scripts`, so the image artifact is
not the only source of truth. Any reload/readiness claim must include source-freshness and
mounted-source checks, not only image smoke.

Superseding reload-preflight source-freshness image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-reloadpf2-envguard3-20260702-1037`
- Digest: `sha256:f4cda3af6c966a9f9a570f0c1e317aeb3f5b496fb3902df1e8a6b251a62b239b`

Reload-only preflight now includes mounted-source freshness and rendered Compose env-contract checks
after zero-active succeeds and before Docker lifecycle quiet. Focused tests passed (`87 passed`);
in-image smoke confirmed the new reload-preflight source and Compose stages are present.

## 2026-07-02 10:49 PT Risk-Aware Reload Preflight

The earlier reload blocker treated every runnable live session, including passive `watching_live`
rows, as a hard no-reload condition. That was over-conservative: the FSM explicitly separates
zero-capital watch states from position-holding states. The reload preflight now blocks true
broker/position risk (`live_pending_entry` plus entered/scaling/trailing/bailout states) while
allowing passive `watching_live` rows.

Superseding pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-reloadpf2-envguard4-riskreload-20260702-1049`
- Digest: `sha256:3775d48d7365a275a16c232467143342d79a470e5a0fb8a338611b9195d40a01`

Validation:

- Focused guard/import tests passed: `89 passed`.
- In-image compile smoke passed.
- In-image verifier smoke confirmed `32` passive watches pass and a `live_pending_entry` blocker
  fails.
- Live `--active-session-preflight-only` returned
  `momentum_worker_no_reload_blocking_live_risk`.
- Live `--reload-preflight-only --expected-image chili-app:main-clean-codex-regression-restore-20260702-060231`
  returned `momentum_worker_reload_preflight_ok`.

No reload/rotation was performed by this agent.

## 2026-07-02 10:58 PT Controlled Deploy

After final preflight passed, the canonical `momentum-exec-worker` Compose service was recreated
once onto:

- `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-reloadpf2-envguard4-riskreload-20260702-1049`
- Digest: `sha256:3775d48d7365a275a16c232467143342d79a470e5a0fb8a338611b9195d40a01`

Post-reload validation:

- Canonical worker `chili-clean-recovery-momentum-exec` is healthy on the new image.
- `--reload-preflight-only --expected-image ...envguard4-riskreload-20260702-1049` returned
  `momentum_worker_reload_preflight_ok`.
- Restored helper import smoke passed inside the running container:
  `_live_ofi_microprice`, `read_ladder_distribution`, `read_target_level_trade_prints`,
  `is_real_entry_outcome`, and `catalyst_grade_rank`.
- RH Agentic readiness is green:
  `broker_ready_for_live=true`, `execution_ready=true`, `runnable_live_now=true`,
  `robinhood_agentic_mcp_adapter_reason=agentic_adapter_enabled`.
- Focused guard/import/broker-readiness tests passed: `96 passed`.

No manual orders were placed.

## 2026-07-02 11:20 PT Trigger-Wait Trace Fix

A fresh post-deploy audit found setup-trace coverage still red because wait telemetry had two
separate issues:

- `tick_first_pullback_scalp` waits with pullback lows did not promote the low to
  `structural_stop_price`.
- Generic `live_entry_trigger_wait` rows carried pullback levels but no `setup_trace` envelope.

Fix:

- Structural wait traces now infer `structural_stop_price` from `pullback_low` when the setup alias
  is structural.
- Pre-structure tick waits such as `ross_pillars_not_explosive` are no longer falsely required to
  have a stop level when they explicitly report `source_wait_has_pullback_levels=false`.
- `live_entry_trigger_wait` now emits a structural setup trace under the neutral alias
  `micro_pullback_trigger_wait` when it carries pullback levels.

Deployed image:

- `chili-app:codex-envguard6-triggertrace-20260702-1120`
- Digest: `sha256:fc4248e33b17ebc7527930b1790fa84b4e52ca62b6e925914cd03a0a4cfd5416`

Validation:

- Canonical worker is healthy on the deployed image.
- Runtime reload preflight is green.
- In-container smoke shows `micro_pullback_trigger_wait` produces `setup_coverage=structural_a_setup`
  and the expected structural stop.
- Fresh live audit passed setup-trace coverage: `setup_trace_findings=0`, `ok=true`.
- Focused guard/import/broker/audit tests passed: `144 passed`.

Replay boundary remains: single-snapshot replay still cannot certify scheduler-slot PnL min/max.

## 2026-07-02 11:40 PT Replay Scheduler Boundary Recheck

The alleged helper/env regression class was rechecked against the running canonical worker before
premarket. The restored contracts are not redundant: OFI/microprice, L2 ladder distribution,
target-level prints, real-entry outcome filtering, and catalyst grade rank remain live dependencies
for tape/L2 confirmation, add/hold/exit behavior, realized-entry feedback, and catalyst-conviction
sizing.

Current runtime:

- Canonical worker: `chili-clean-recovery-momentum-exec`
- Image: `chili-app:codex-envguard6-triggertrace-20260702-1120`
- State: healthy
- In-container helper smoke: `_live_ofi_microprice`, `read_ladder_distribution`,
  `read_target_level_trade_prints`, `is_real_entry_outcome`, and `catalyst_grade_rank` are callable
  in their native modules and via the `live_runner` compatibility exports.
- Env contract: event-driven loop, IQFeed tape/notify/poll fallback, Ross event admission,
  Ross universe guard, and auto-arm are on; scheduler/batch fallbacks are off.

Replay/scheduler validation:

- Focused replay/export/CLI tests passed: `80 passed`.
- Compile smoke passed for `live_replay_export.py`, `live_replay_audit.py`, `replay_v3.py`, and
  `audit_momentum_live_replay.py`.
- Replay v3 has a tested multi-snapshot scheduler timeline path. It models venue availability,
  pre-capacity free skips for adapter-unavailable/wrong-venue/terminal rows, per-venue/global
  order-call and risk budgets, delayed-then-selected evidence, and broker-outcome attribution.
- The priority model is self-normalizing: it ranks available candidates by quality percentile,
  queue-age percentile, expiry-urgency percentile, and tick-arm state. There was no blind batch
  limit increase.
- Regression coverage includes adapter-unavailable rows not consuming useful capacity while a later
  Robinhood equity row is selected, terminal pre-entry rows not consuming capacity, and multi-step
  budget-delayed candidates being selected on a later snapshot.

Fresh live audit:

- `ok=true`
- `setup_trace_findings=0`
- `trace_coverage_ok=true`
- `scheduler_snapshot_steps=1`
- `session_rows=300`, all replay decisions in this sampled window were `pre_entry_terminal`

Boundary:

- Current live DB evidence is still a single-snapshot terminal-window sample, so it correctly
  refuses live scheduler-priority and PnL min/max claims.
- This is not a passive-observation requirement and not a reason to disable the live lane. It means only that
  future scheduler/PnL claims require persisted multi-snapshot scheduler events plus complete
  broker/counterfactual labels.
