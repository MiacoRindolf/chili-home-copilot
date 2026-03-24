# `app/trading_brain` — Phase 1 scaffolding + Phase 2 mirror

## Scope

- **Models:** SQLAlchemy tables in [`app/models/trading_brain_phase1.py`](../models/trading_brain_phase1.py). Migrations `048`–`051` create `brain_*` tables (including Phase 4 prediction mirror).
- **Phase 1:** Protocol ports, Pydantic schemas, stage catalog ([`stage_catalog.py`](stage_catalog.py)).
- **Phase 2 (Part M):** Working SQLAlchemy repositories ([`infrastructure/repositories/cycle_sqlalchemy.py`](infrastructure/repositories/cycle_sqlalchemy.py), [`lease_sqlalchemy.py`](infrastructure/lease_sqlalchemy.py), [`integration_sqlalchemy.py`](infrastructure/integration_sqlalchemy.py)), DB status reader ([`infrastructure/learning_status_sqlalchemy.py`](infrastructure/learning_status_sqlalchemy.py)), factories + shadow helpers ([`wiring.py`](wiring.py)). Legacy [`run_learning_cycle`](../services/trading/learning.py) / `_learning_status` remain **authoritative**.

## Phase 2 feature flags ([`app/config.py`](../config.py))

| Flag | Default | Purpose |
|------|---------|---------|
| `brain_cycle_shadow_write_enabled` | `False` | Mirror `brain_learning_cycle_run` + `brain_stage_job` during `run_learning_cycle`. |
| `brain_status_dual_read_enabled` | `False` | In `get_learning_status()`, compare legacy dict vs latest DB mirror and **log** mismatches (WARNING). |
| `brain_lease_shadow_write_enabled` | `False` | Best-effort `brain_cycle_lease` refresh during a shadow cycle (observability only; **no gating**). |
| `brain_cycle_lease_enforcement_enabled` | `False` | **Phase 3:** single-flight **admission** via `brain_cycle_lease` using **dedicated** DB sessions (independent of shadow flags). |
| `brain_prediction_dual_write_enabled` | `False` | **Phase 4:** append-only mirror of legacy `get_current_predictions` via **dedicated** session; routers unchanged; **not** read-authoritative. |
| `brain_prediction_read_compare_enabled` | `False` | **Phase 5:** compare legacy vs latest mirror (same `universe_fingerprint`, `ORDER BY id DESC`); **mirror miss = DEBUG**; **parity mismatch = WARNING**; **success = no log**. |
| `brain_prediction_read_authoritative_enabled` | `False` | **Phase 5:** return mirror-hydrated rows **only** when **explicit API tickers** were used, snapshot **fresh** (`max_age_seconds`, default 900), and **full parity** passes; else legacy. **`tickers=None` never authoritative.** |
| `brain_prediction_read_max_age_seconds` | `900` | Staleness cutoff for **authoritative** mirror reads only. |
| `brain_prediction_ops_log_enabled` | `False` | **Phase 6:** emit **one** bounded `INFO` line per `_get_current_predictions_impl` with prefix **`[chili_prediction_ops]`** (dual-write + read enums + `fp16`/`snapshot_id`/`line_count`; **no** ticker lists). When **off**, existing Phase 4/5 **WARNING** behavior is unchanged. |

When **`brain_cycle_lease_enforcement_enabled`** is on, Phase 2 **shadow** lease mirror acquire/refresh/release on the **main** learning `Session` is **skipped** so only the dedicated-session enforcement path touches the lease row during a cycle.

## Phase 3 — lease enforcement (Part N)

- **Admission only:** acquire before `run_learning_cycle` sets `_learning_status["running"] = True`; optional **refresh** after each successful `_commit_step` (soft+log on failure); **release** in `finally` after `brain_shadow_finally`.
- **Dedicated sessions:** all enforcement lease I/O uses a short-lived `SessionLocal()` in [`infrastructure/lease_dedicated_session.py`](infrastructure/lease_dedicated_session.py), not the caller’s learning session.
- **Legacy authoritative:** `get_learning_status()` is unchanged for display; lease does **not** replace UI status reads.
- **Denied start:** second holder gets `{"ok": False, "reason": "Learning cycle lease already held"}` and a structured WARNING with peer holder / expiry when discoverable.
- **Rollback:** set `brain_cycle_lease_enforcement_enabled` to `False`.

## Phase 4 — prediction mirror (dual-write only)

- **Objective:** Persist each non-empty legacy prediction result as **`brain_prediction_snapshot`** + **`brain_prediction_line`** rows (append-only, no dedupe).
- **Hook (single choke point):** end of `_get_current_predictions_impl` in [`app/services/trading/learning.py`](../services/trading/learning.py), **after** sorting `results`, **immediately before** `return results`. **Skipped** when `results == []` (no empty snapshots).
- **Write path:** [`infrastructure/prediction_mirror_session.py`](infrastructure/prediction_mirror_session.py) opens **`SessionLocal()`**, commits mirror inserts, closes; failures **log WARNING** and **do not** affect the returned legacy list.
- **Snapshot identity:** `id` (BIGSERIAL) is `snapshot_id`; **`as_of_ts`** = UTC seal time at insert; **`universe_fingerprint`** = SHA-256 of sorted uppercased tickers in the effective batch (`ticker_batch`); **`correlation_id`** = new UUID per write.
- **Read path:** **No** router or product code reads these tables in Phase 4; **`get_current_predictions`** return shape unchanged.
- **Parity (tests):** `ticker`, `sort_rank`, `score`, `confidence`, `direction`, `price`, `meta_ml_probability`, `vix_regime`, `signals`, `matched_patterns`, `suggested_stop`, `suggested_target`, `risk_reward`, `position_size_pct`.
- **Rollback:** set `brain_prediction_dual_write_enabled` to `False`.

## Phase 5 — mirror read compare + candidate-authoritative (explicit tickers only)

- **Integration:** [`infrastructure/prediction_read_phase5.py`](infrastructure/prediction_read_phase5.py) runs at end of `_get_current_predictions_impl` after Phase 4 dual-write; **request-local**; **does not** mutate `_pred_cache` or background SWR.
- **Selection:** `universe_fingerprint = prediction_universe_fingerprint(ticker_batch)`; latest snapshot **`ORDER BY id DESC LIMIT 1`**.
- **Parity:** index-aligned rows; ints/`confidence` exact; floats `math.isclose(rel_tol=1e-9, abs_tol=1e-6)`; `signals` list of stripped strings; `matched_patterns` as **set** of `json.dumps(..., sort_keys=True)` per dict.
- **API shape:** **no** new response fields in Phase 5; **routers unchanged**.
- **Rollback:** set `brain_prediction_read_compare_enabled` and `brain_prediction_read_authoritative_enabled` to `False`.

## Phase 6 — prediction ops log (observability only)

- **Switch:** `brain_prediction_ops_log_enabled` is the **only** gate for the new **`INFO`** line; no router or API changes.
- **Contract (one line):** `[chili_prediction_ops] dual_write=<na|ok|skip_empty|fail> read=<na|compare_ok|compare_miss|compare_mismatch|auth_mirror|fallback_miss|fallback_empty|fallback_stale|fallback_parity|fallback_ineligible|error> explicit_api_tickers=<true|false> fp16=<16-hex|none> snapshot_id=<int|none> line_count=<int|none>`.
- **Dual-write outcome:** inferred in [`app/services/trading/learning.py`](../services/trading/learning.py) from whether dual-write ran, was skipped for empty results, or raised (legacy list still returned).
- **Read outcome:** returned as `PredictionReadOpsMeta` from [`infrastructure/prediction_read_phase5.py`](infrastructure/prediction_read_phase5.py) (`phase5_apply_prediction_read` → `(results, meta)`).
- **Formatter:** [`infrastructure/prediction_ops_log.py`](infrastructure/prediction_ops_log.py).
- **Rollback:** set `brain_prediction_ops_log_enabled` to `False`.

## Phase 7 — authority hardening (explicit vs implicit choke point)

- **Objective:** Prevent **accidental** candidate-authoritative mirror reads. **Not** an authority-expansion phase (no goal to increase `auth_mirror` usage).
- **Primary enforcement:** [`get_current_predictions`](../services/trading/learning.py) and [`_get_current_predictions_impl`](../services/trading/learning.py):
  - **`tickers=None`**, **empty `tickers` list**, cache/SWR refresh, and any path that builds an inferred universe → **`explicit_api_tickers=False`** (mirror never candidate-authoritative for that request).
  - **Non-empty** explicit ticker list from the API → **`explicit_api_tickers=True`** (Phase 5 authoritative rules may apply when flags allow).
- **Empty list caveat:** `_build_prediction_tickers(db, [])` falls through to the **implicit** universe; Phase 7 ensures that case is **not** paired with `explicit_api_tickers=True`.
- **Release blocker (hard):** Any **`[chili_prediction_ops]`** log line containing **`read=auth_mirror`** and **`explicit_api_tickers=false`** → **do not ship**; fix and re-run validation.
- **Validation:** Phase 7 is **not** complete on unit tests alone — replay Docker/runtime soak with ops log enabled and confirm **zero** lines matching the blocker pattern, e.g. PowerShell:
  - `(docker compose logs chili --since 30m 2>&1 | Select-String "chili_prediction_ops") | Where-Object { $_.Line -match "read=auth_mirror" -and $_.Line -match "explicit_api_tickers=false" }` → must be **empty**.

## Phase 8 — operational rollout + guardrails (docs/ops only)

- **Objective:** Repeatable **per-environment** enablement of prediction mirror flags, **minimal soak**, and the **frozen release-blocking** check above — **no** authority code changes in Phase 8.
- **Rollout doc:** [`docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md`](../../docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md) (flag order, soak, rollback, grep).
- **Script:** [`scripts/check_chili_prediction_ops_release_blocker.ps1`](../../scripts/check_chili_prediction_ops_release_blocker.ps1) — pipe `docker compose logs` or use `-Path` on a saved log file.

## Rollback

- Turn Phase 2/3/4/5/6 flags **off** (immediate).
- **Disposable dev DB:** truncate `brain_stage_job` then `brain_learning_cycle_run` (respect FKs), or revert the PR.

## Final shadow commit (`brain_shadow_finally`)

When `brain_cycle_shadow_write_enabled` is on, `brain_shadow_finally` may call `Session.commit()` **after** flushing ORM updates to `brain_learning_cycle_run`, `brain_stage_job`, and (if enabled) `brain_cycle_lease`.

**Transaction boundary**

- That commit ends the **current** SQLAlchemy transaction on the **same** `Session` passed into `run_learning_cycle` (typically the request/worker session).
- It runs **once per cycle** in `finally`, after legacy `_learning_status` is already set to idle (legacy behavior is unchanged).

**Scope of effect**

- Intended effect is persisting **brain mirror rows only** (tables under the Phase 1/2 `brain_*` models). No separate “brain-only” connection or sub-transaction is used in Phase 2.

**Why legacy stays authoritative**

- `_learning_status`, early-exit rules, and all product-facing return shapes are unchanged; the commit does not re-read brain tables to drive decisions.

**Failure mode avoided**

- Without a finalize commit, mirror rows updated only in memory could be **lost** if the caller never committed again after the last `_commit_step()` (notably after `steps_completed` is set to `25` with a final `_commit_step()`).

**Risk introduced**

- Any other **pending** dirty objects attached to the same `Session` at `finally` time would be committed together with the mirror (shared-session model). Mitigation: learning cycle steps already commit frequently; keep non-brain work out of the same session between steps if you need stricter isolation (future: nested transaction / dedicated session for mirror only).

**Guard (Phase 2.5)**

- The finalize `commit()` runs only if the session had pending ORM state (`new` / `dirty` / `deleted`) **before** `flush()`, so a no-op finalize does not issue an empty commit.

## Non-goals (Phase 2 scope)

Phase 2 did not add lease **enforcement** (that is Phase 3). No router edits, no prediction path changes, no event consumers, no authoritative brain reads for product UI.

## Known parity debt: `total_steps`

- **Legacy:** `_learning_status` is initialized with `total_steps: 14` (idle template). During a cycle, `run_learning_cycle` sets `total_steps` to **25**.
- **Brain mirror / `LearningStatusDTO`:** `total_steps` defaults to [`TOTAL_STAGES`](stage_catalog.py) (**25**) from the stage catalog.
- **Why it is expected:** Phase 2 deliberately did not change legacy idle defaults or API return shape; dual-read logs call out mismatches when `brain_status_dual_read_enabled` is on.
- **Later resolution:** Blueprint **Part M / Phase 3** options include promoting DB-backed fields behind flags or aligning idle init once product agrees — not Phase 2.

## Canonical source

[`.cursor/plans/trading-brain-canonical-blueprint.md`](../../.cursor/plans/trading-brain-canonical-blueprint.md) — **Part M (Phase 2)**.
