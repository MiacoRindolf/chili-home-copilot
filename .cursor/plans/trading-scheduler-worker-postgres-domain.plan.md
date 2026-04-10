---
name: ""
overview: ""
todos: []
isProject: false
---

# Trading scheduler worker, Postgres (brain_batch_jobs + payloads), Jobs domain (revised)

## 1) Audit: reuse existing Postgres batch table

**Use the existing model and table** — `[BrainBatchJob](app/models/trading.py)` / `brain_batch_jobs` — and the helpers in `[app/services/trading/brain_batch_job_log.py](app/services/trading/brain_batch_job_log.py)` (`brain_batch_job_begin`, `brain_batch_job_finish`).

- **Do not introduce a parallel `scheduler_job_runs` table** for the same purpose. Standardize heavy trading scan jobs (crypto breakout, pattern imminent, stock breakout, momentum, etc.) on `**brain_batch_job_begin` / `brain_batch_job_finish`** with distinct `job_type` strings (align names with APScheduler job ids where helpful).
- **Correlation id:** use the existing row `**id`** (UUID returned by `brain_batch_job_begin`) as the per-run correlation id.
- **Metrics:** store structured metrics in `**meta_json`** (e.g. `tickers_scanned`, `duration_s`, `score_buckets`, `errors_count`). If full scan **payloads** (large result lists) do not fit cleanly in `meta_json`, add a **single migration** extending the same table (e.g. optional `**payload_json` JSONB** on `brain_batch_jobs`) *or* a **1:1 child table** `brain_batch_job_payloads(job_id FK → brain_batch_jobs.id)` — preference: keep one logical “run” row in `brain_batch_jobs` and attach payload in an extra column or child row so queries stay simple.

Today only some paths (e.g. daily market scan) use this; the plan extends it to **every** heavy scheduler run worth auditing.

## 2) No process-local cache for shared scan results — Postgres only

- **Remove reliance on in-memory globals** as the source of truth for data that must be visible across **scheduler worker** vs **web** processes.
- **Writers** (worker or web when triggering a scan): persist outcomes via `**brain_batch_jobs` (+ payload column/table as above)**.
- **Readers** (trading APIs, AI context, UI): **load latest completed run** for the relevant `job_type` (and optionally `status='ok'`) from Postgres — **no** “read memory first, then DB” fallback once rollout is complete; optional tiny request-scoped reuse is implementation detail, not a cache layer.

This replaces the earlier “file cache” / “merge with globals” idea entirely.

## 3) Dedicated scheduler worker process

- `**chili-scheduler-worker`** (same image): entrypoint e.g. `python scripts/scheduler_worker.py`. Registers APScheduler **heavy / interval market scans** only.
- **Web (`chili`)**: `start_scheduler(role=web|worker)` — web omits heavy jobs (or configurable).
- **Heartbeat (optional):** worker writes a row or updates `**meta_json` on a synthetic job_type** (e.g. `scheduler_worker_heartbeat`) on `brain_batch_jobs`, or a tiny dedicated row pattern — so **Jobs** UI can show “worker alive as of …”.

## 4) Jobs in the CHILI app — two pages

Naming: **Jobs** as the domain.

1. **Jobs — metrics & history (client-facing page)**
  - For **household / paired users** (normal CHILI client): dashboards and **metrics** derived from `brain_batch_jobs` — run counts, success rate, duration trends, last run per `job_type`, links to error snippets.  
  - This is the “show me how the brain batch work is doing” experience without requiring log access.
2. **Jobs — manage / control (second page)**
  - **Operational management** inside CHILI: e.g. trigger a safe one-off run (where allowed), view detailed run list with filters (job type, status, time range), worker heartbeat, future toggles (enable/disable job classes via flags/DB).  
  - **Access control:** likely **admin or power user** initially; exact role can be tightened in implementation.

Both pages read/write **Postgres** (`brain_batch_jobs`); management actions go through authenticated API routes (no separate microservice in Phase 1).

## 5) Cross-domain coordination and future “AI-powered Jobs”

- **Phase 1:** **DB as single pane of glass** — Code Brain / Reasoning Brain / Project Brain keep their own schedulers or intervals, but **any job that should appear in Jobs** must **open/close a `brain_batch_jobs` row** (or a documented equivalent) with consistent `job_type` prefixes (e.g. `code_brain_*`, `reasoning_*`).
- **Later (explicit product goal):** evolve Jobs into an **AI-assisted Jobs domain** — smarter summaries, anomaly explanations, suggested actions, and **internal APIs** such as `POST /api/internal/jobs/enqueue` or **lease/claim** patterns for orchestration. **Defer** that complexity until Phase 1 metrics + worker split are stable; document as roadmap in the same plan file.

Aligned with **task-first**: Phase 1 is **visibility + persistence + worker placement**, not a second generic orchestrator.

---

## Phased delivery

### Phase 1 — Reliability + Postgres-only reads

- Extend `[brain_batch_job_log](app/services/trading/brain_batch_job_log.py)` usage in `[trading_scheduler.py](app/services/trading/trading_scheduler.py)` for crypto breakout, pattern imminent, stock breakout, momentum (wrap each job body).
- Migration only if needed: `**payload_json`** on `brain_batch_jobs` or `brain_batch_job_payloads` — not a duplicate audit table.
- Refactor `[scanner.py](app/services/trading/scanner.py)` (or thin module): **persist** scan results to DB on completion; `**get_crypto_breakout_cache` / `get_breakout_cache`** (and callers) **read from DB** (latest ok run for type); remove long-term dependence on globals for cross-process data.
- `start_scheduler(role=...)` + `scripts/scheduler_worker.py` + `docker-compose` service + deploy notes.

### Phase 2 — Jobs UI (two pages)

- Router + templates: **Jobs (metrics)** and **Jobs (manage)**; APIs listing/filtering `brain_batch_jobs`, aggregates for charts/tables; optional heartbeat display.
- Nav placement: TBD (e.g. main nav “Jobs” or under Brain/Admin for manage).

### Phase 3 — Broader reporting

- Convention doc: new scheduled work → register `job_type` + begin/finish.
- Optionally backfill Code/Reasoning scheduler hooks to write `brain_batch_jobs`.

### Future — AI-powered Jobs domain

- Enqueue/lease APIs, LLM summaries over run history, proactive recommendations — **after** Phase 1–2 are production-stable.

---

## Non-goals (this pass)

- Replacing APScheduler with Celery/Temporal.
- Redis or on-disk JSON as the **source of truth** for scan results (Postgres only for shared data).
- A full workflow DAG engine before Phase 1 ships.

---

## Open implementation choices

- **Payload shape:** `meta_json` only vs new `**payload_json`** / child table — decide from typical payload size for 250-crypto scan results.
- **Who can open Jobs (manage):** admin-only vs any paired user for triggers.

