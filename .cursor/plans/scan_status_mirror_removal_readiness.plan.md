---
title: scan/status — consumer cleanup & mirror-removal readiness
status: active
updated: 2026-04-13
---

# Next phase: consumer cleanup & mirror-removal readiness

**Preserved (do not collapse with mirrors):** top-level **`learning`** — neural graph overlay (`tbnUpdateNodeStatuses`) and any consumer needing the **full reconcile / mutex-adjacent snapshot** (mesh/step ids, indices, funnel, etc.).

## Done in this slice (repo)

- [x] **`brain.html`**: `scanStatusBrainRuntime()` uses **`brain_runtime` only** when `encode_error` is absent and `brain_runtime.work_ledger` is present; flat mirrors only for encode-error / legacy edge cases.
- [x] **`api_scan_status`**: `compatibility_mirror_note` documents deprecation and that **`learning` is out of scope** for mirror removal.
- [x] **`docs/TRADING_BRAIN_WORK_LEDGER.md`**: mirror removal checklist + consumer rules.
- [x] **`chili-scan-status-deploy-validation.mdc`**: mirror-removal guidance; points here.
- [x] **`scripts/prove_execution_feedback_ledger.py`**: verify hint uses `brain_runtime.work_ledger`.

## Done — API mirror removal (happy path)

- [x] **`api_scan_status`:** success responses omit root `work_ledger` / `release` / `scheduler` / `scan`; **`encode_error`** payload unchanged (frozen flat keys).
- [x] **`tests/test_scan_status_brain_runtime.py`:** asserts mirror-free happy path.
- [x] **Docs / rules:** `TRADING_BRAIN_WORK_LEDGER.md`, `chili-scan-status-deploy-validation.mdc`, `lc_shrink_validation_reset.plan.md` aligned.

## Remaining (optional / separate)

1. **External consumers:** out-of-repo callers still using root mirror keys must migrate to `brain_runtime` (breaking change for them).
2. **`learning` migration:** optional nested `brain_runtime.learning_full` + graph switch — **separate** program.

## Definition of done (mirror-removal PR) — met

- No in-repo references require top-level mirrors on success responses.
- Tests and docs match the new shape; Brain desk + graph overlay manually smoke-tested.
- Rollback: redeploy prior image (or restore previous `api_scan_status` payload in git).
