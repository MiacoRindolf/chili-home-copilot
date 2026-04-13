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

## Remaining (explicit freeze before execution)

1. **External consumers:** audit forks, mobile, scripts, and third-party callers for top-level `work_ledger` / `release` / `scheduler` / `scan` only (no `brain_runtime`).
2. **API change (future PR):** remove duplicate keys from JSON **or** gate with `?compat_mirrors=1` (default off) after audit; update `tests/test_scan_status_brain_runtime.py` and encode-error contract.
3. **`learning` migration (later):** optional nested `brain_runtime.learning_full` + graph switch — **separate** from mirror removal; do not block mirror removal on this.

## Definition of done (mirror-removal PR)

- No in-repo references require top-level mirrors on success responses.
- Tests and docs match the new shape; Brain desk + graph overlay manually smoke-tested.
- Rollback: restore mirrors or flip compat query default.
