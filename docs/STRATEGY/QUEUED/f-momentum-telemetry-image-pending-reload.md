# Momentum Telemetry Image Pending Reload

Date: 2026-07-02

Status: built and smoked, not live-loaded.

Superseding pending image:

- `chili-app:main-clean-codex-regression-restore-replay-snap-20260702-0812`
- digest `sha256:35bc671370a3ca4f5a1ad32028c593e9b4fe30cea56bf2c03775adb53c475d82`

Prior superseded image:

- `chili-app:main-clean-codex-regression-restore-telemetry-20260702-0748`
- digest `sha256:bb692cad2c2566ef6a5a1f1ae33f11c288af5e9fc1090045aa8e3ac96f713cdc`

Why pending:

- Superseding pending image built at 2026-07-02 08:45 PT:
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-20260702-0845`.
  Digest: `sha256:8e49831003311e835b8cf2fe10d8f49ea3b6d5aa1f71e3422240154618d030dd`.
  This replaces the earlier pending replay-snapshot image for any future reload attempt because it
  also includes the runtime verifier replay-snapshot smoke and zero-active-session reload
  preflight hardening.
- Superseding compatibility image built after the 08:45 PT image:
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-20260702-0900`.
  Digest: `sha256:de0078f3ceaf97b3e6d985ef9d32140675a9f841aa4128f1dc9039829101ffa8`.
  Use this newer tag for the next pending reload attempt. It adds no strategy behavior change; it
  only re-exports the already-restored OFI/L2 readers, outcome filter, and catalyst rank helper
  from `live_runner` so future smoke/import checks catch both native and runner-facing contracts.
- Latest superseding pending image built after the compatibility image:
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-20260702-0915`.
  Digest: `sha256:e083863a44d491a8378e0737dd304e698159d64fa4ac94381961b84990b395a3`.
  Use this latest tag for the next coordinated reload. It includes the restored-contract
  compatibility exports plus a lightweight `--active-session-preflight-only` runtime verifier path
  for reload/no-reload decisions during active watches.
- Latest superseding audit image built after the 09:15 PT image:
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-20260702-0925`.
  Digest: `sha256:7a8b19c407337b87a64162332beb600dfee2605fead6ff7e839d9bae8fa4dc85`.
  Use this tag for the next coordinated reload. It adds scheduler replay evidence reason counts:
  `decision_reason_counts`, `free_skip_reason_counts`, and `capacity_consuming_reason_counts`.
- Latest superseding trace image built after the 09:25 PT image:
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-20260702-0940`.
  Digest: `sha256:7bc897149fc7913e2906a66e37c2ee4cb33251432282a23ccccee2c2a1861516`.
  Use this newest tag for the next coordinated reload. It fixes future
  `live_entry_tick_scalp_wait` telemetry so tick first-pullback waits carry canonical
  `setup_trace` (`setup_alias=tick_first_pullback_scalp`, wait reason, tick micro-frame, and
  pullback levels when available), and it makes anonymous wait events fail audit explicitly.
- Latest superseding reload-preflight image built after the 09:40 PT image:
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-reloadpf-20260702-0950`.
  Digest: `sha256:da809bb492563fd3ee473c8fa90f206cbf3d94d5280e9afc18677e62dc6ba334`.
  Use this newest tag for the next coordinated reload. It adds
  `python scripts/verify_momentum_worker_runtime.py --reload-preflight-only`, a cheap reload guard
  that checks canonical worker uniqueness/placeholder state, lifecycle quiet window, optional
  expected image alignment, and zero active-like live sessions without running import-heavy smoke
  stages inside the active worker.
- Latest superseding reload-preflight v2 image built after the 09:50 PT image:
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-audit-trace-reloadpf2-20260702-1000`.
  Digest: `sha256:1e55ac98d928aaf889aa30d4aaf3e69222abc3033e05bb1b5bb683948812e476`.
  Use this newest tag for the next coordinated reload. It reorders `--reload-preflight-only` so
  zero-active-session check runs before lifecycle event scanning; active watches now fail fast
  without hitting the slower Docker event window.
- The canonical live worker is already running as `chili-clean-recovery-momentum-exec` on
  `chili-app:main-clean-codex-regression-restore-20260702-060231`.
- Read-only preflight at 2026-07-02 14:53 UTC found 30 fresh active-like `watching_live`
  sessions, so reloading the worker would rotate an active live watchlist.
- Read-only preflight at 2026-07-02 14:57 UTC found 31 fresh active-like `watching_live`
  sessions. The queue was still active; do not reload yet.
- Read-only preflight at 2026-07-02 15:14 and 15:18 UTC found 32 fresh active-like
  `watching_live` sessions. The queue was still active; do not reload yet.
- Read-only preflight at 2026-07-02 15:47 UTC still found 32 active-like `watching_live`
  sessions. Sample symbols: `TENX`, `NBIZ`, `CLRO`, `CONL`, `ILLR`. The pending image was
  intentionally not live-loaded.
- `.env` and `.codex_last_image_tag` intentionally remain pinned to
  `chili-app:main-clean-codex-regression-restore-20260702-060231`, so a Compose recreate does
  not silently switch to this pending image before the active-session preflight is clear.
- This image contains telemetry/audit coverage fixes, not a required entry-safety hotfix.

What the image fixes:

- Restored-helper regression protections remain intact for OFI/L2 readers, real-entry outcome
  filtering, and catalyst grade ranking.
- Pre-candidate Ross shape blocks now report `setup_coverage=structural_a_setup` for covered
  aliases such as `abcd_break_tick_ok`.
- A-setup size/notional floor skip events now merge canonical setup trace payloads.
- Setup-trace audit infers coverage from `setup_coverage` while still requiring real structural
  stop levels on actual candidate/order events.
- Live batch planner now emits best-effort `live_replay_scheduler_snapshot` telemetry when
  `chili_momentum_live_runner_replay_snapshot_enabled=true` (default true). Snapshot rows include
  all considered candidates, selected IDs, free prefilter reasons, venue health, and budgets
  derived from the actual batch `work_limit`. This is Replay v3 evidence only; it does not change
  candidate ordering, broker calls, or order/risk capacity.

Validation already passed:

- `tests/test_momentum_setup_trace_audit.py` plus focused live-runner alias tests: 23 passed.
- Restored-helper / feature-flag suite: 33 passed.
- Replay v3 / live replay / audit suite: 91 passed.
- Compile smoke in the image passed.
- In-image import/behavior smoke passed with a dummy PostgreSQL-shaped URL.
- Runtime guard passed with the live image as `--expected-image` and the pending image as
  `--smoke-image`.
- Runtime/feature guard suite passed 61 tests after the 31-watcher preflight.
- Read-only live replay audit remained setup-trace clean:
  `setup_trace_findings=0`, `trace_coverage_ok=true`, `input_shape=single_snapshot_batch`.
  PnL min/max remained not claim-ready because historical scheduler snapshots and complete
  missed/taken outcomes are still unavailable.
- Replay snapshot emission validation passed serially: 62 tests across live replay export,
  Replay v3 sizing/PnL, audit CLI, feature flags, and batch planner regressions.
- In-image compile and import smoke passed for
  `chili-app:main-clean-codex-regression-restore-replay-snap-20260702-0812`.
- Runtime guard passed with the running live image as `--expected-image` and the replay-snapshot
  pending image as `--smoke-image`.
- During smoke, the runtime guard caught a fresh external kill/restart of the canonical worker
  at 2026-07-02 15:16:41-15:16:42 UTC. The worker auto-restarted healthy on the same running
  image, and strict guard passed again after the health-derived quiet window. Treat any future
  fresh lifecycle event the same way: do not reload until the guard is green.
- Runtime verifier was then hardened with a `replay_scheduler_snapshot_smoke` stage. With
  `--smoke-image`, this validates the pending image has the first-class replay snapshot flag,
  callable emitter, venue-state helper, snapshot event type, and source markers. Focused verifier
  tests passed 61.
- A later strict guard run correctly failed `source_reload_freshness` because local
  `live_runner.py` is newer than the running worker image. This is expected while this pending
  image is not live-loaded. The same guard passed with only `--skip-source-reload-freshness`,
  proving the current worker is otherwise single/healthy/aligned. Do not claim full strict
  deploy-readiness until either the pending image is loaded or the source freshness caveat is
  explicitly accepted for a no-reload window.
- Runtime verifier now also has optional `--require-no-active-like-sessions` reload preflight.
  It queries the canonical worker DB for live runnable/holding states and fails before any reload
  attempt if watchers/positions exist. Current preflight failed as intended with
  `active_like_sessions_present:count=32`; sample symbols included `VRXA`, `BEZ`, `IPW`, `CCTG`,
  and `JFB`.
- Superseding image validation for
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-20260702-0845`:
  build succeeded; in-image compile smoke passed for config/live-runner/live-replay/replay-v3 and
  runtime verifier modules; in-image import smoke confirmed the first-class replay snapshot flag,
  snapshot emitter, venue-state helper, verifier snapshot smoke, and zero-active preflight are
  callable; focused runtime/batch tests passed (`64 passed`).
- Reload preflight against the superseding image failed closed only on
  `active_like_sessions_present:count=32`, which is the intended no-rotation behavior while the
  live watchlist is active.
- Compatibility image validation for
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-20260702-0900`:
  build succeeded; in-image compile smoke passed; in-image import smoke confirmed both native
  contracts (`pipeline`, `outcome_labels`, `catalyst`) and runner compatibility exports; focused
  tests passed (`67 passed`).
- Preflight image validation for
  `chili-app:main-clean-codex-regression-restore-replay-snap-guard-compat-preflight-20260702-0915`:
  build succeeded; digest captured; in-image compile smoke passed; in-image import smoke confirmed
  runner compatibility exports, native restored contracts, and the lightweight preflight CLI marker;
  focused verifier/import tests passed (`68 passed`).
- The new lightweight reload check is:
  `python scripts/verify_momentum_worker_runtime.py --active-session-preflight-only`.
  It runs only the active live-session DB query and exits. At 2026-07-02 15:59 UTC it failed closed
  with `active_like_sessions_present:count=32`; sample symbols included `YRD`, `IREZ`, `AHMA`,
  `AVXX`, and `CETX`. The canonical worker remained running on the same live image after this
  check.
- At 2026-07-02 16:08 UTC the same lightweight preflight still failed closed with
  `active_like_sessions_present:count=32`; sample symbols included `SDEV`, `CCUP`, `VRXA`, `IREZ`,
  and `PPCB`. The canonical worker remained running on
  `chili-app:main-clean-codex-regression-restore-20260702-060231`.
- Current source/test audit also verifies the hidden momentum flag class is closed:
  `tests/test_momentum_feature_flags.py`, `tests/test_verify_momentum_worker_runtime.py`, and
  `tests/test_momentum_dynamic_import_contracts.py` passed together (`80 passed`). The settings
  fallback audit found `0` missing Settings fields and `0` missing default-false controls.
- Audit image validation:
  `tests/test_live_replay_export.py::test_live_replay_audit_certifies_adapter_unavailable_starvation_evidence`,
  `tests/test_live_replay_export.py::test_live_replay_audit_reports_pre_entry_terminal_free_skip_reason`,
  `tests/test_audit_momentum_live_replay_cli.py`, and `tests/test_momentum_setup_trace_audit.py`
  passed together (`45 passed`). In-image compile smoke passed; in-image replay evidence smoke
  confirmed `free_skip_reason_counts={'pre_entry_terminal': 1}` and
  `capacity_consuming_reason_counts={'selected': 1}` on a synthetic timeline.
- Current-window live replay audit after the 16:09 UTC worker restart has `session_rows=32`,
  `setup_trace_traces_seen=0`, `setup_trace_findings=0`, and therefore correctly fails
  current-window trace/lifecycle certification with `no_setup_trace_events`. Scheduler evidence is
  now explicit: `decision_reason_counts={'pre_entry_terminal': 32}`,
  `free_skip_reason_counts={'pre_entry_terminal': 32}`, and no capacity-consuming skip reasons.
- Broad audit after enabling stricter telemetry validation correctly fails historical trace
  coverage with `wait_event_missing_setup_trace=614` on anonymous
  `live_entry_tick_scalp_wait` rows. This is a real telemetry gap in the currently running image,
  not a trading-order change. The newest pending image fixes the future emit path and keeps the
  audit fail-closed for any remaining anonymous wait telemetry.
- Trace image validation: focused setup/audit/live-runner tests passed (`46 passed`); in-image
  compile smoke passed; in-image smoke confirmed
  `tick_first_pullback_scalp` wait trace emits `source_wait_tick_armed=True` and
  `source_wait_has_pullback_levels=True`, while anonymous wait payloads are reported as
  `wait_event_missing_setup_trace`.
- Reload-preflight image validation: verifier tests passed (`68 passed`); combined focused suite
  passed (`126 passed` before this reload-preflight patch); in-image compile smoke passed; in-image
  smoke confirmed both `--reload-preflight-only` and `--active-session-preflight-only` markers.
- Live `--reload-preflight-only --expected-image chili-app:main-clean-codex-regression-restore-20260702-060231`
  ran without heavy import/config stages and failed closed on active sessions:
  `active_like_sessions_present:count=32`. The canonical worker remained running on the same
  current image.
- Reload-preflight v2 validation: verifier tests passed (`68 passed`); live reload preflight failed
  closed on active sessions without lifecycle timeout; in-image compile smoke passed; in-image smoke
  confirmed `--reload-preflight-only` exists and active-session preflight runs before lifecycle
  event scanning.
- Do not run the full runtime verifier repeatedly while active watches exist. A later full verifier
  attempt returned `runtime_guard_stage_failed:ross_event_admission_config:returncode=137` and the
  canonical worker restarted externally/under orchestration, then came back on the same running
  image. During active watches, use direct read-only checks plus the zero-active preflight instead
  of rotating or stress-checking the worker.
- Another external kill/restart occurred around 2026-07-02 15:36:11-15:36:13 UTC during a full
  verifier run. Because this pattern repeated, avoid repeated full strict guard runs during active
  watches unless necessary; use the targeted zero-active preflight and wait for lifecycle quiet
  before any reload.
- Superseding regression/env-guard image:
  `chili-app:codex-envguard6-triggertrace-20260702-1120`.
  Digest: `sha256:fc4248e33b17ebc7527930b1790fa84b4e52ca62b6e925914cd03a0a4cfd5416`.
  The claimed dropped helper class is not redundant and is now explicitly covered. Read-only
  in-container import smoke on the running canonical worker confirmed `live_runner`, `pipeline`,
  `outcome_labels`, and `catalyst` expose the OFI readers, `is_real_entry_outcome`, and
  `catalyst_grade_rank` contracts. The runtime guard now also enforces the full event-driven
  live-lane env contract: live runner, event loop, IQFeed tape/notify/poll fallback, Ross event
  admission, auto-arm, and Ross universe must be on; scheduler and auto-arm scheduler fallbacks
  must be off. Compose-service verification now enforces the same event-driven env contract, so a
  dropped Compose proxy/default is caught before runtime. Focused guard/import tests passed
  (`89 passed`). The new image was built and smoked: compile passed, helper import smoke passed,
  and in-image verifier smoke accepted a healthy env while rejecting disabled IQFeed/Ross-event env
  with explicit reasons. The image is now deployed on the canonical
  `chili-clean-recovery-momentum-exec` worker via the `momentum-exec-worker` Compose service. The
  live worker bind-mounts `./app` and `./scripts`; therefore image smoke
  is necessary but not sufficient for runtime truth. The reload-only preflight now also runs
  mounted-source freshness and rendered Compose env-contract checks after zero-active succeeds and
  before lifecycle quiet. It also separates passive watch rows from true reload-blocking live risk:
  `watching_live` rows alone no longer block a reload, while `live_pending_entry` and holding
  states still fail closed. In-image verifier smoke confirmed `32` passive watches pass and a
  pending-entry blocker fails. Post-reload verification against the canonical worker returned
  `momentum_worker_reload_preflight_ok`; restored helper import smoke passed; RH Agentic readiness
  returned `broker_ready_for_live=true`, `execution_ready=true`, and `runnable_live_now=true`.
  Trigger-wait telemetry was then fixed and deployed: `tick_first_pullback_scalp` wait traces infer
  structural stop from pullback low, pre-structure `ross_pillars_not_explosive` waits are not
  falsely required to have a stop before pullback levels exist, and `live_entry_trigger_wait`
  carries a `micro_pullback_trigger_wait` setup trace. Fresh live audit passed with
  `setup_trace_findings=0`; focused tests passed (`144 passed`).

Reload rule:

- Do not rotate the canonical worker while reload-blocking live risk exists (`live_pending_entry`
  or position-holding states). Passive `watching_live` rows are not broker/position risk and should
  not be treated as a dark-flag blocker.
- Current canonical worker is already on the image above. For any future reload, coordinate a
  minimal reload only:
  one canonical worker, no placeholder, no duplicate worker, expected image aligned, RH Agentic
  readiness true, runtime guard green, no manual orders.
