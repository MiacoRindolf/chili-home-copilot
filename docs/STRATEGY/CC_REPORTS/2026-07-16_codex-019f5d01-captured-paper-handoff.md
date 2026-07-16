# HANDOFF — Codex thread 019f5d01 → captured-paper Alpaca PAPER activation path

**Date:** 2026-07-16 · **Author:** Claude Code (reconstruction + handoff session)
**Status:** AUTHORITATIVE handoff for the interrupted Codex thread `019f5d01-6917-7413-b84c-ed4a027b0576`

---

## 0. TL;DR

The 3-day Codex thread (2026-07-13 → 07-16) that built the **frozen shortest-safe path to Alpaca PAPER (fake-money) trading** died when Codex credits ran out at 07-16 ~07:34 PT. **Its work is safe**: committed and pushed on branch **`codex/ross-replay-validation`** (worktree `D:\dev\chili-home-copilot-codex-broker`, HEAD `2d3ca10`), based on the current `origin/main` tip (`8bf2895`, 07-12). Every item that was in flight at cutoff was landed by a follow-up Claude session and is verified present in the tree. **What remains is not code — it is the evidence + activation run** (sealed generation → probes → preflight → host cutover → no-order smoke → activate), with two explicit operator-approval checkpoints.

---

## 1. Lineage & goal

**Thread goal (verbatim):** "Continue improving and validating CHILI offline against recorded live market data and Ross Cameron trade recaps, implementing only evidence-supported changes until CHILI can reproduce the transferable profitable setups with broker-fidelity safeguards; keep paper/live order execution off until explicit activation approval and recertification gates pass."

**USER PRIORITY OVERRIDE (07-16 00:22 UTC)** froze scope to the shortest safe path to ALPACA PAPER:
1. Enumerate the exact remaining paper-start blockers; freeze scope to them.
2. Install the real captured-paper context and intended first-dip/strategy policy in the ACTUAL runner invocation, with replay/paper adaptive-policy parity — no permanent dark flag, no paper-only weak strategy, no $50/$250/one-symbol magic caps.
3. Finish the correct-host, SHA-bound launcher/manifest and cut over the four legacy-rooted IQFeed tasks/processes only through validated rollback-safe preflight; never touch live-cash execution, never start Alpaca live.
4. Prove the selected broker UUID/scope is Alpaca PAPER, then run account identity, stale-data, order ownership/idempotency, reconciliation, indeterminate-submit/late-fill, kill-switch, and append-only fill-settlement preflights.
5. Run only the focused integrated regressions invalidated by these changes plus a no-order smoke, then activate fake-money Alpaca paper when those operational gates pass.

**PARKED (required backlog, resumes AFTER paper starts):** counterfactual/OOS authority, Ross validator (external audit verdict: CERTIFIABLE = 0 on historical data — structurally impossible retroactively), promotion-certification, exact-print allocator (left at a safe compile/test checkpoint), other post-paper hardening.

**Who did what:**
- Codex thread `019f5d01` (orchestrator, ~287 subagents) — built everything below, working in `D:\dev\chili-home-copilot-codex-broker`.
- Claude session "Codex chat local setup" — after the credit cutoff, committed the WIP snapshot (`8aa4df1`) and closed the in-flight items (`c3f263f`, `8e5a5a2`, `2d3ca10`); also recovered Docker Desktop on this box.
- Claude session "Ross-vs-CHILI replay audit" (read-only) — produced the external audit packages in `C:\chili-claude-audit`.
- This session — reconstruction, this handoff, and the completion run.

---

## 2. Where the code is

| Item | Value |
|---|---|
| Worktree | `D:\dev\chili-home-copilot-codex-broker` |
| Branch | `codex/ross-replay-validation` (pushed; remote ref = local HEAD) |
| HEAD | `2d3ca10` |
| Base (merge-base w/ origin/main) | `8bf2895` = origin/main tip (07-12; main has not moved since) |
| Scale | 330 files, +326,776 / −8,568 (212 added / 118 modified) |

**Commits (oldest first):**
1. `a4ef7a0` fix(momentum): enforce broker truth and idempotent order lifecycle
2. `af46c64` docs(audit): record broker-truth recertification
3. `8aa4df1` **WIP snapshot: captured-paper Alpaca activation path (Codex session 019f5d01, credits exhausted mid-slice)** — the ~45k-line snapshot
4. `c3f263f` host cutover: close wrapper-v3 P1s 1–4 + builder ACL fail-closed (tests: 17 builder + 25 collector + 63 cutover green)
5. `8e5a5a2` host cutover: close wrapper-v3 P1 scheduler semantics + P2 fences (RESTORE_PLAN_SCHEMA → v4; tests: 62 + 30 + siblings green)
6. `2d3ca10` host cutover: fix stale v3 restore-plan references to v4 (996 captured-paper tests collect clean)

**Subsystem → module map** (all under `app/services/trading/momentum_neural/` unless noted; 54 new modules):

| Subsystem | Modules |
|---|---|
| Replay capture host / provenance | `replay_capture_contract.py`, `replay_capture_runtime.py`, `replay_provenance.py`, `replay_errors.py`, `live_replay_capture.py`, `legacy_replay_ohlcv.py`, `ross_replay_benchmark.py` |
| IQFeed L1/L2 capture | `iqfeed_l1_capture.py`, `iqfeed_l2_capture.py`, `captured_paper_iqfeed_trigger.py` |
| Captured-paper admission/dispatch/selection | `captured_paper_admission.py`, `captured_paper_initial_admission.py`, `captured_paper_dispatcher.py`, `captured_paper_selection.py`, `captured_paper_entry_intent.py`, `captured_paper_positive_acceptance.py` |
| Owner fence / PREOWNER→PENDING_OWNER | `captured_paper_pending_owner.py`, `captured_paper_preowner_promotion.py`, `captured_paper_service_fence.py`, `captured_paper_phase_one_handoff.py` |
| Transport coordinator + outbox (C3) | `captured_paper_outbox.py`, `captured_paper_transport_coordinator.py`, `captured_paper_transport_worker.py`, `captured_paper_post_commit_worker.py` |
| Fill capture/watch | `captured_paper_fill_capture.py`, `captured_paper_fill_watch.py` |
| Financial breaker seam | `captured_paper_financial_breaker.py` |
| Supervisor / stranded-authority recovery | `captured_paper_service_supervisor.py`, `captured_paper_restart_inventory.py`, `captured_paper_initial_recovery.py` |
| Production material providers | `captured_paper_production_material.py`, `captured_paper_production_provider.py`, `captured_paper_initial_provider.py`, `captured_paper_initial_controller.py`, `captured_paper_initial_candidate_reader.py` |
| Captured Alpaca adapter | `captured_alpaca_paper_adapter.py` |
| Alpaca broker-truth | `alpaca_paper_identity.py`, `alpaca_paper_account_receipt.py`, `alpaca_bp_census_capability.py`, `alpaca_buying_power_reflection.py`, `alpaca_cycle_settlement.py`, `alpaca_fill_activity.py`, `alpaca_fill_read_capability.py`, `alpaca_orphan_claims.py`, `venue/account_identity.py` |
| Adaptive risk reservation | `adaptive_risk_policy.py`, `adaptive_risk_account_lock.py`, `adaptive_risk_request_builder.py`, `adaptive_risk_reservation.py`, `adaptive_risk_runtime_contract.py`, `captured_adaptive_risk_source.py` |
| First-dip policy (baseline vs candidate) | `first_dip_tape_decision.py`, `first_dip_tape_policy.py` |
| Misc | `paired_oos_scoreboard.py`, `optional_db_read.py` |

**DB migrations: 34 new, IDs 315–348** (`app/migrations.py` +7,183 lines; `app/models/trading.py` +2,110). Main's last ID is 314. ⚠️ **Hard Rule 6**: run `.\scripts\verify-migration-ids.ps1` before any merge; merge this branch promptly (or coordinate) so no other branch claims the 315+ range. Audit gap G1 (below) targets `_328→_335` and `_338→_344`.

**Tests:** 122 new test files (~1,713 `def test_` functions), 62 modified. `tests/conftest.py` extended (18 new tables in targeted cleanup + `adaptive_risk_` prefix). Requires `TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test`; **one pytest at a time** against chili_test (fixtures truncate). Baseline: full captured-paper surface (996 tests) collects clean at `2d3ca10`.

**Docs in-tree:** `docs/DESIGN/PRODUCTION_REPLAY_CAPTURE_V1.md` (capture contract + "Known blockers" ledger), `docs/DESIGN/ADAPTIVE_RISK_REPLAY_PAPER_PARITY.md` (schema-v2 resolver + "Audited legacy migration blockers" + 8-step migration gate), `docs/DESIGN/IQFEED_L1_CAUSAL_PROVENANCE.md`, `docs/AUDITS/2026-07-14_chili_broker_truth_recertification_sanitized.md`.

---

## 3. Frozen-blocker status (the 5 override steps)

| # | Step | Status | Evidence |
|---|---|---|---|
| 1 | Enumerate blockers, freeze scope | ✅ DONE | Blocker list issued 07-16 00:22; this doc supersedes it |
| 2 | Captured-paper context + first-dip candidate policy in the real runner invocation | ✅ BUILT, ⏳ unproven end-to-end | C3 zero-POST suites 76/76; service handshake 32/32; PREOWNER→PENDING_OWNER atomic promotion green (6 real-PG failure scenarios); strict exact-print trigger 23/23; stranded-authority recovery 26/26; sealed PAPER user-ID binding; event controller (Q notify → hot capture → attestation → candidate → PREOWNER → PENDING_OWNER) green |
| 3 | SHA-bound launcher/manifest + 4-task IQFeed host cutover | ✅ BUILT + validated, ⏳ **Apply not executed** | wrapper-v3 78/78 → P1/P2 closures (17+25+63, then 62+30); v3 host snapshot validated (`host_mutation_count=0`); v4 restore-plan schema; read-only collector 6/6, fails closed on drift |
| 4 | Prove Alpaca PAPER UUID + preflight battery | ✅ UUID fetched/ACTIVE/bound; ⏳ battery not run end-to-end | `paper=True` read-only account lookup done 07-16; isolated env hash valid; ACL-sealed secret |
| 5 | Focused regressions + no-order smoke → activate | ⏳ NOT REACHED | — |

**C3 invariant (non-negotiable):** capture/admission/reservation authority invalidated after the fence ⇒ **zero broker POST**, reconciliation-only. Encoded and green in fake- and real-DB paths. Any POST during the no-order smoke = C3-class violation = full stop.

**In-flight items at the moment of cutoff — all landed and verified in-tree:**
- Windows ACL seal + fail-closed builder → `scripts/build_captured_paper_runtime_env.py:568+` (`_enforce_private_output_acl`; protected non-inherited DACL; fail-closed when win32security unavailable; re-sealed on every publish)
- Account-UUID → installer-argument binding + receipt-validation-before-publish → same file `:444/:731/:741–767` (`--expected-account-id`, `.pending` validation before `_publish_no_overwrite`)
- Wrapper "semantic bypass" → `scripts/captured_paper_host_cutover.py:1618+` (PowerShell leading-apostrophe = string literal, `#Requires` = executable directive → both reject)
- v3→v4 restore-plan consistency → `RESTORE_PLAN_SCHEMA v4` (`:75`, parser rejects non-v4 `:2593`; remaining `rollback_snapshot.v3` strings are a different, legitimately-v3 schema)

---

## 4. Activation runbook (what remains)

All CLIs are stdlib-only, offline, fail-closed, hash-pin-chained, in `scripts/` of the codex-broker worktree. **Receipts expire in 30–60s** — run each evidence→consumption chain contiguously; on `PROBE_TTL_CONTRACT_DRIFT`, regenerate rather than retry.

**Preconditions:** Docker/Postgres up (5433); no other pytest against chili_test; worktree clean at the freeze SHA; no commits from freeze until activation (any commit ⇒ regenerate evidence).

1. **Sealed generation:** `python scripts/build_captured_paper_preactivation.py --request … --request-sha256 … --candidate-root … --output-root … --allow-read-root …`
2. **Capture benchmark:** `python scripts/benchmark_replay_capture_runtime.py …` (compares `.bench-paper-current/` vs `.bench-paper-candidate/` output roots — untracked runtime scratch, do not commit)
3. **All-eight probe battery:** `python scripts/run_captured_paper_preactivation_probes.py …` — probes: `runtime_settings, broker_account, database_schema, capture_host_smoke, focused_regressions, lifecycle_preflight, kill_switch, rollback_snapshot`. Gate: 8/8.
4. **Operational preflight (broker GET-only):** `python scripts/alpaca_paper_operational_preflight.py --mode audit …` → 6-check JSON (`topology, broker_get_only, capture_benchmark, capture_seal, adaptive_runtime, historical_replay`). Gate: 6/6, account ACTIVE, UUID = bound PAPER UUID.
5. **Host cutover:** fresh snapshot via `python scripts/collect_captured_paper_host_snapshot.py …` → `python scripts/captured_paper_host_cutover.py --mode ValidateOnly …` (**mandatory fresh, in-path observation — never Apply over a stale ValidateOnly**) → **[OPERATOR APPROVAL #1]** → `--mode Apply` (two-phase permit handshake; 39-field permit schema; hash-chain journal) → verify the 4 legacy IQFeed tasks point at the SHA-bound launcher. `--mode Rollback` is the escape hatch; if Rollback itself fails, freeze everything and get the operator.
6. **Sealed runtime env:** `python scripts/build_captured_paper_runtime_env.py --source-env … --source-sha256 … --output-env … --expected-account-id <PAPER-UUID> --iqfeed-bridge-build … --iqfeed-notify-channel … --allow-read-root … --allow-write-root …`
7. **No-order smoke + finalize:** `python scripts/captured_paper_operator_flow.py …` (publishes the exact NoOrderSmoke command; never invokes it) → `powershell scripts/start-captured-alpaca-paper.ps1 -Mode NoOrderSmoke` → `python scripts/finalize_captured_paper_activation.py --preactivation … --no-order-receipt … --output-root …`. Gate: **zero broker POSTs** in the receipt.
8. **[OPERATOR APPROVAL #2 — REQUIRED] Activate:** `powershell scripts/start-captured-alpaca-paper.ps1 -Mode ActivatePaper`. Present first: freeze SHA, 8/8 probe manifest, 6/6 preflight JSON, Apply journal, sealed env hash, no-order receipt, activation contract hash, G1–G9 verdicts.
9. **Post-activation watch:** policy hashes = pinned provenance (first-dip mode must be `candidate`, no dark-flag substitution); kill-switch reachability re-checked live; zero orders outside admitted candidates; L2-unavailable decisions emit decision-local `COVERAGE_UNAVAILABLE` (no global block, no opportunity consumed).

---

## 5. External audit gap ledger G1–G9 — verdicts

Source: `C:\chili-claude-audit\captured_paper_livehead_evidence_gate_fable5_20260716T080418Z` (aggregate NOT-PASS, DIAGNOSTIC_ONLY). ⚠️ The audit snapshot **predates** commits `8aa4df1..2d3ca10` — each gap must be re-verified against HEAD before being believed. Gating rule: a gap gates activation iff its failure mode is (i) unintended broker POST / C3 violation, (ii) wrong-account or wrong-credential binding, or (iii) silently-wrong schema in the paper order/capture path. Everything else → post-paper backlog.

Triage completed 2026-07-16 (this session). Fixes referenced below are committed on this branch.

| Gap | Claim | Verdict vs HEAD |
|---|---|---|
| G1 (HIGH) | `test_migration_341` stubs the reassert helper to no-op; 16 misleading-greens (recording-fake pattern executes no SQL); forward-repair helpers couple `_328→_335`/`_338→_344` | **FIXED pre-freeze.** New real-DB test `test_migration_341_reassert_executes_full_335_body_on_real_schema` runs the UNSTUBBED migration 341 + full 335 body (constraint re-validation vs existing rows + %ROWTYPE guard CREATE) in a rollback-only tx — PASSES; no hidden schema bug. Full three-shape (old/partial/current) chain test → backlog. |
| G2 (HIGH) | Close-lane same-CID resend relies on broker-side dedup | **GATED → FIXED.** At HEAD the resend-after-indeterminate path was already fail-closed at the sweep (strict CID truth; neutered absent-rotation), but two racing workers could BOTH pass the byte-equal freeze and POST the same CID on FIRST submission. Fix: `consume_orphan_handoff_close_post_permission` (one-shot CAS) wired as the pre-POST gate in `_submit_handoff_close`; acceptance tests with a NON-deduping, NON-retaining fake broker prove exactly 1 POST across racing workers + frozen-request replays (`test_handoff_close_single_post_when_broker_never_dedupes`, `..._frozen_request_consumes_post_permission_once`). |
| G3 (MED) | No durable pagination frontier for fill reads | BACKLOG — restart-mid-pagination real-DB test; restart safety currently rests on immutable fill identity + dedupe. |
| G4 (MED) | Lease-expiry vs version-CAS fencing unpinned | BACKLOG — premise STALE at HEAD: every post-fence stage re-derives wall-clock `valid_until` (min of outbox/action/arm/invocation/breaker/pre-dispatch) under FOR UPDATE (`captured_paper_outbox.py:3452/3606/3786/4030`) + reaper `recover_expired_captured_paper_leases` each cycle. Debt = `test_two_actor_lease_expires_post_fence`. |
| G5 (MED) | Two DEAD_NEVER_EXECUTED test invariants | BACKLOG — helper 1 just needs rename to `test_`; helper 2 is a builder needing a `def test_` wrapper (fill-settlement lock-walk; positive-adoption transport start). |
| G6 (MED) | Managed-exit bracket outside captured-paper file set | BACKLOG — boundary documented; exit-owner coverage ends at the handoff boundary. |
| G7a | ORM completion-marker stricter than migration-344 constraint | BACKLOG — divergence is inert wherever migrations run (migration replaces the ORM-created constraint); debt = ORM-vs-migration constraint-parity test, or relax the ORM branch. |
| G7b | `lease_expires_at` never reaped in-segment | BACKLOG — premise STALE: read in-segment (outbox.py:2821) + two wired reapers (startup stranded-authority recovery `expired_released`; per-cycle outbox reaper). `submit_indeterminate` retention is BY DESIGN (same-CID reconciliation; escalates `reconciliation_health_escalated`). |
| G7c | Fence-to-POST staleness after `authorize_transport_invocation` | BACKLOG — revalidate re-derives reservation/packet/claim/session/arm/opportunity/event-chain under lock pre-POST; host-revocation + operator-pause re-checked in-process immediately before POST. Residual = daily-loss flip INSIDE the breaker receipt validity window; debt = `test_pre_post_financial_breaker_window`. |
| G8 | Bounded PARTIAL crash windows | BACKLOG — recorded in `transaction_and_crash_matrix.json`; recovery preconditions documented. |
| G9 (INFO) | 291 UNRESOLVED_DYNAMIC edges; 2 chunking-stub misleading-greens | Chunking tests **FIXED** (now patch the real settings singleton the code reads; the import-error fail-safe is genuinely covered). Dynamic edges + hermeticity → backlog. |

**NEW finding (this session): 14 stale tests** (8 in `test_adaptive_alpaca_lifecycle.py`, 5 in `test_alpaca_detached_claim_handoff.py::*adaptive*` failing with `adaptive_risk_reservation_unavailable`, +1 in `test_alpaca_deadman_close_handoff.py::test_rth_entry_rejects_stale_premarket_extended_hours_generation_before_place` failing with `builder_missing_capture_binding`). Root cause: they exercise the DEPRECATED `live_runner._ensure_adaptive_alpaca_reservation` entry, and `AdaptiveRiskReservationStore.reserve` now (correctly) requires the `LockedAlpacaPaperAdmissionBundle` for alpaca_paper — only the captured-paper admission path (`captured_paper_admission.py:1358`) constructs it. The old live_runner alpaca-paper reservation entry is intentionally fail-closed at HEAD. **Not an activation gate**: the probe battery's `focused_regressions` runs a PINNED 6-node list untouched by these; deployed prod runs main. Debt: update the 13 tests to the bundle contract or quarantine the deprecated entry + its tests explicitly.

Note: the audit's central **PT-C3-AUTH-AT-INVOCATION UNSAFE** quarantine is **already resolved at HEAD** — corrected C3 semantics (post-fence invalidation ⇒ zero POST) green in fake- and real-DB suites; independently re-verified this session on this machine: `test_captured_paper_outbox.py` + `test_captured_paper_transport_coordinator.py` = **100/100 passed** vs real PostgreSQL.

---

## 6. Landmines (read before touching anything)

1. **Two paper lanes, one Alpaca PAPER account.** The DEPLOYED momentum-exec-worker (main-clean image) runs the *pragmatic* alpaca-paper lane, including the **orphan reconciler (PR #880)** that flattens any long-equity position without a matching non-terminal alpaca session. If the new captured-paper runner activates while that worker runs, the old worker may flatten the new lane's positions. **Before ActivatePaper: pause the old alpaca-paper lane (or verify ownership fencing covers cross-lane claims).**
2. **Compose flags are source-only.** This branch sets `CHILI_MOMENTUM_LIVE_RUNNER_ENABLED`/`_SCHEDULER_ENABLED` → 0 in `docker-compose.yml`, but that edit is **not deployed**. Production containers run pinned main-clean images. Do not redeploy from this branch without an explicit operator decision; verify effective in-container bindings, never config defaults.
3. **Do not merge the old s16 branch wholesale.** `codex/validate-s16-fixture` (old tree `D:\dev\chili-home-copilot`) is June-9-based, 687 commits behind main; its 8 core trading files conflict with the July rewrites (#871–#900). Its uncommitted work is preserved at checkpoint branch **`codex/validate-s16-fixture-checkpoint-20260716`** (commit `8fd9d95`, pushed). Salvage file-by-file (triage plan §8).
4. **Legacy $50/$250/one-exposure clamps.** `docs/DESIGN/ADAPTIVE_RISK_REPLAY_PAPER_PARITY.md` documents that the *legacy* paper rail re-applies these in several modules. The session log claims the *new* captured-paper resolver is equity/structural-risk-based with no execution-surface clamp. **Verify the EFFECTIVE captured-paper path** (AST regression excludes activation-only `50`/`250` literals — run it) before treating parity as true.
5. **Old-tree stash inventory** (19 stashes in the main repo, recorded so `stash clear` can't lose them): `da7b234`, `edf4017`, `96b481f`, `195119f`, `c4fa302`, `658dc62`, `12383f5`, `3fd6cda`, `ffb6bf1`, `6cfee75`, `071ec04`, `fa0f325`, `ef8632e`, `0e84230`, `212a526`, `158f077`, `30e8733`, `566bf67`, `2325b17`.
6. **Not committed but on disk** (excluded from the checkpoint): `logs/` (runtime logs), `project_ws/_worktrees|_mlops_worktrees|_qa_worktrees` (embedded git worktrees — content lives on their own branches), `project_ws/AgentOps/ross_video_evidence/<video-id>/` media (~1.9 GB; the `.md` audit reports ARE committed).
7. **IQFeed host bridge**: the running host bridge is still the WORKTREE build (pg_notify producer). Do not restart it from a main build until the bridge file is ported (standing warning from WORKING_MEMORY).

---

## 7. Parked backlog (resume AFTER paper starts)

- Counterfactual/OOS authority; promotion-certification; Ross validator (external verdict: CERTIFIABLE=0 / DIAGNOSTIC 4 / UNAVAILABLE 3 / UNRESOLVED 5 — constraints-only, never a paper gate).
- Exact-print allocator — parked at a safe compile/test checkpoint.
- `PRODUCTION_REPLAY_CAPTURE_V1.md` "Known blockers": IQFeed exact quote-event clock unavailable; IQFeed handoff is an injection seam, not an installed production bootstrap; equity L2 persists snapshots not raw deltas; Massive lacks a certifying watermark; halt/LULD/SSR inferred. These are **coverage gaps** — handled decision-locally as `COVERAGE_UNAVAILABLE`, never a global paper block.
- `ADAPTIVE_RISK_REPLAY_PAPER_PARITY.md` 8-step migration gate (atomic packet consumption across all economic paths; replace strategy-count gates with aggregate exposure/correlation/liquidity budgets; remove duplicated dollar clamps with scoped AST tests).
- Audit gaps classified "backlog" in §5 once Phase-3 verdicts land.

## 8. Old-tree salvage plan (parallel track; never blocks the paper path)

Preserved at `codex/validate-s16-fixture-checkpoint-20260716` (`8fd9d95`). Triage each area into: **(1) superseded-by-main** (the 8 core files live_runner/entry_gates/risk_policy/paper_execution/config/auto_arm/pipeline/execution_family_registry — do not port; diff for novel logic only), **(2) missing-on-main** (tape recorders `nbbo_tape`/`tape_ws_recorder`/`micro_bars`, 12 `verify_*` runtime verifiers, standalone modules e.g. `tight_false_break_entry` (parked first-dip variant), `feature_flags` registry, replay/counterfactual harnesses — port as clean commits on `salvage/<module>` branches off current main), **(3) scratch** (root `_*.py`/`_*_out.txt`, `scripts/_cx_*` — already preserved, do not port). Mechanical test: `git log --oneline origin/main --since=2026-07-01 -- <path>` — empty main-side July history ⇒ likely bin 2. All salvage merges via PR with operator sign-off.

## 9. External audit package index (`C:\chili-claude-audit`)

All DIAGNOSTIC_ONLY / read-only; none certify activation. Newest last: `source_snapshot_rev3…`, `postfix_delta…`, `rolling_firstdip_redteam…`, `iqfeed_host_readiness…`, `capture_to_alpaca_paper_redteam…`, `alpaca_entry_state_model…`, `alpaca_entry_model_v2_redteam…` (verdict: reproducible but INVALID as replacement/proof), `ross_coverage_master_crosscheck{,_v2}…`, `ross_source_binding_contract…`, `capture_order_lifecycle_fault_model…`, `capture_order_policy_oracle_v1/v2…`, `captured_paper_implementation_closure_fable5_20260716T062223Z` (binding PASS, C1–C20 + W1–W8), `captured_paper_livehead_evidence_gate_fable5_20260716T080418Z` (**latest**; aggregate NOT-PASS; G1–G9). Each has a `.zip` + `.zip.sha256`.

## 10. Coordination

- Claude session "Codex chat local setup" is active on this box (Docker recovery; author of the post-cutoff commits). Ownership split: infra recovery = that session; codex-broker completion run = this session; salvage = follow-up/parallel.
- One pytest at a time against `chili_test`; check for other runners before every suite.
- Original Codex session log (full fidelity): `C:\Users\rindo\.codex\sessions\2026\07\13\rollout-2026-07-13T12-43-26-019f5d01-6917-7413-b84c-ed4a027b0576.jsonl` (293 MB, 90.8k lines).
