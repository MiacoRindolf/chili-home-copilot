---
title: LC-shrink — validation reset + scan/status contract
status: completed
updated: 2026-04-13
---

# LC-shrink slice — validation reset (commit `31ca070`)

## Binding assumption (replaces old Phase 0)

| Old (obsolete) | New (binding) |
|----------------|---------------|
| Phase 0 compared `release.git_commit` to repo SHA | **Never.** No SHA / fingerprint deploy gate in app JSON. |
| Missing or stale `git_commit` = failed gate | **`release` is intentionally `{}`** everywhere it appears — **correct and expected**. |
| Redeploy “truth” = matching SHAs | Deploy truth = **host/platform** (image digest, PaaS deployment id, logs). API validates **shape + behavior** only. |

## Phase 0 (runtime / regression) — use this checklist

1. **`GET /api/trading/scan/status`** returns **`ok: true`** (happy path).
2. **Top-level key order:** `ok`, `brain_runtime`, `prescreen`, `work_ledger`, `release`, `scheduler`, `scan`, `learning` (`learning` last).
3. **`brain_runtime`:** `work_ledger`, `release`, `scheduler`, `scan`, `learning_summary`, `activity_signals`, `compatibility_mirror_keys`, `compatibility_mirror_note`.
4. **`brain_runtime.release` and top-level `release`:** both **`{}`** (empty object, no `git_commit`).
5. **Mirrors:** top-level `work_ledger`, `scheduler`, `scan` **deep-equal** the same fields inside `brain_runtime`.
6. **`learning.status_role`:** **`reconcile_compatibility`**.
7. **`activity_signals`:** exactly **`reconcile_active`**, **`ledger_busy`**, **`retry_or_dead_attention`**, **`outcome_head_id`** (minimal slice).
8. **`learning_summary`:** includes **`status_role`** (`reconcile_compatibility`) and **`tickers_processed`**; operator UI should prefer **`brain_runtime`** for runtime strip (graph overlay may still use top-level **`learning`** for mesh/step fields).
9. **`work_ledger`:** structure and enabled flag consistent with ledger service (environment-specific).
10. **Brain desk (manual / browser):** reconcile-first copy where implemented, **`details` collapsed when idle**, optional **`window.__CHILI_LEDGER_OUTCOME_REFRESH`** default **off**, debounced refresh only when opted in.

**Automated gate:** `pytest tests/test_scan_status_brain_runtime.py -v`.

## LC-shrink implementation slice (approved scope) — status

Implemented on `main` (API + UI + tests + worker copy + docs):

- [x] `learning` last in `api_scan_status` payload; encode-error path aligned.
- [x] `brain_runtime.activity_signals` (four fields only).
- [x] `release == {}` (post-`31ca070`).
- [x] Brain template: reconcile wording, ledger debounce flag, `scanStatusBrainRuntime` / `activity_signals`.
- [x] `scripts/brain_worker.py` iteration/reconcile wording (no behavior change).
- [x] `docs/TRADING_BRAIN_WORK_LEDGER.md` aligned with empty `release`.
- [x] **Non-goals honored:** no `brain.py` / `trading.py` copy cleanup unless blocking; no mutex changes to `get_learning_status()["running"]`.
- [x] Operator-facing copy: worker status **Iterations** (was Cycles), queue idle line uses **next iteration**, cycle-report empty state, intro line **cycle AI reports**, comments aligned with reconcile/iteration model.
- [x] **`brain_runtime` sufficient for operator/runtime strip:** `learning_summary` adds `status_role` + `tickers_processed`; `pollLearningStatus` merges `learning_summary` over top-level `learning`; graph overlay still uses full `learning` only.

## Copy-paste: reset Cursor validation (next session)

> **Validation reset:** `brain_runtime.release` and top-level `release` are **always `{}`** (commit `31ca070`). Do **not** use `release.git_commit` or match API JSON to `git rev-parse HEAD`. Phase 0 = `scan/status` **shape**, **mirrors**, **`learning.status_role`**, **`activity_signals`**, **work_ledger**, and **Brain UI** behavior — see `.cursor/rules/chili-scan-status-deploy-validation.mdc` and `tests/test_scan_status_brain_runtime.py`.

## Next phase (out of this plan)

**Mirror removal & consumer cleanup:** [`.cursor/plans/scan_status_mirror_removal_readiness.plan.md`](scan_status_mirror_removal_readiness.plan.md) — preserves **top-level `learning`** for graph/mutex snapshot.

Other ideas (separate freeze): further LC demotion, additional `activity_signals` — only if explicitly approved.
