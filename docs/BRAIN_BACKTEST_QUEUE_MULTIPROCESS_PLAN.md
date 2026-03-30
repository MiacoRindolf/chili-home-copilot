# Plan: Multi-process pattern queue backtests + DB headroom

**Status:** Implemented (opt-in). Set `BRAIN_QUEUE_BACKTEST_EXECUTOR=process` on `brain-worker`; see `.env.example` and `docker-compose.yml` (`max_connections=350`).

## Objective

Speed up the **“Backtesting patterns from queue”** step by running **one OS process per queued pattern** (or a bounded process pool), so **CPU-bound** `backtesting.py` / Python work escapes the **GIL** and can use **multiple physical/logical cores**.

**Non-goals (this phase)**

- Multiple `brain-worker` Compose replicas (still blocked by `data/brain_worker.lock` on shared volume unless redesigned).
- Process-per-ticker parallelism (too many connections and processes; revisit only if pattern-level scaling plateaus).
- Changing promotion logic or backtest semantics—only **execution topology** and **resource limits**.

---

## Current state (baseline)

| Piece | Behavior |
|--------|-----------|
| Queue step | `_auto_backtest_from_queue` in `app/services/trading/learning.py` uses `ThreadPoolExecutor` with `brain_backtest_parallel` (default 18), capped by `len(pattern_ids)` and optional `brain_max_cpu_pct`. |
| Per pattern | `_backtest_one_pattern_from_queue` opens its **own** `SessionLocal()` (good for multiprocessing). |
| Per pattern tickers | `smart_backtest_insight` uses another `ThreadPoolExecutor` (`_bt_workers()` / `brain_smart_bt_max_workers`). |
| CPU | Threads + GIL → often **~1 core** effective for CPU-heavy backtests **per process**. |
| Docker `brain-worker` | `DATABASE_POOL_SIZE=8`, `DATABASE_MAX_OVERFLOW=12` (max **20** conns **per process** that imports `app.db`). |
| Postgres (Compose) | `max_connections=200` on `postgres` service. |
| Single worker | `scripts/brain_worker.py` holds `data/brain_worker.lock` on shared `chili_data`. |

---

## Target architecture

1. **Configurable backend** for the queue batch: `threads` (default, backward compatible) vs `process` (opt-in via settings/env).
2. **Process pool** (`concurrent.futures.ProcessPoolExecutor`) with `max_workers = min(brain_backtest_parallel, len(pattern_ids), brain_queue_process_cap)` (new cap to match hardware/DB budget).
3. **Top-level picklable worker** (required on **Windows spawn**): e.g. `app.services.trading.backtest_queue_worker.run_one_pattern_job(pattern_id, user_id)` that only imports heavy app code inside the function or uses a small `initializer` to set env and logging.
4. **Child process DB pools must be tiny**: each spawned process currently would create an engine with the **same** `database_pool_size` / `max_overflow` as the parent unless overridden—this is the main **footgun** (see below).
5. **Reduce inner thread count** when `process` mode is on: set `brain_smart_bt_max_workers` lower in children (or global when process mode) to avoid **process × thread** explosion.

---

## Releasing / sizing DB constraints

### 1) Math: connections required

Let:

- **P** = process pool size (patterns run in parallel).
- **C_parent** = parent process peak connections (orchestrator + any incidental sessions).
- **C_child** = per-child peak connections (one `_backtest_one_pattern_from_queue` holds one session for most of the work, but commits and nested code may spike briefly—assume **1–3** safe engineering margin per child if pool allows it).

**Worst case (naïve):** each process uses default SQLAlchemy pool `pool_size + max_overflow`.  
Compose brain-worker today: **20 per process**.  
If **P = 16** → up to **16 × 20 = 320** connections from workers alone → **exceeds** `max_connections=200` and starves `chili` / `brain` / admin.

**Required policy**

- **Child processes:** force **small pool**, e.g. `pool_size=1`, `max_overflow=2` (max **3** conns per child) via:
  - **Env only in pool initializer** (`os.environ["DATABASE_POOL_SIZE"]="1"`, etc.) **before** `SessionLocal` / engine first use, **or**
  - Dedicated settings: `brain_mp_child_database_pool_size`, `brain_mp_child_database_max_overflow`, **or**
  - `NullPool` for children (one connection per checkout, no pool—simplest accounting, slightly more connect overhead).

- **Parent brain-worker process:** can keep a **moderate** pool (e.g. 4+8) since it mostly dispatches.

**Budget example (target)**

| Role | pool_size | overflow | max conns/process | count | subtotal |
|------|-----------|----------|-------------------|-------|----------|
| Parent | 4 | 8 | 12 | 1 | 12 |
| Child | 1 | 2 | 3 | 16 | 48 |
| **Other services** | … | … | | | reserve **40–80** |
| **Headroom** | | | | | **≥ 20** |

For **P=16**, children **48** + parent **12** + other **60** ≈ **120** → fits **200** with margin.  
**P=24** → children **72** + overhead → still fits if “other” is controlled.

### 2) Postgres `max_connections`

- **Compose:** bump `postgres` `command` from `200` to **`300`** or **`400`** if you intend **P≥20** or run **chili + brain + worker + many children** concurrently.
- **Hosted Postgres:** apply the same increase in provider settings; watch memory (each connection uses RAM).

### 3) Docker Compose / `.env` for `brain-worker`

- Increase **only if** parent pool grows or P grows; **children** use the **small pool** from code/env initializer, **not** the current `8+12` defaults.
- Document required pairs:
  - `BRAIN_QUEUE_BACKTEST_EXECUTOR=process` (name TBD in implementation)
  - `BRAIN_BACKTEST_PARALLEL=16` (example)
  - `BRAIN_QUEUE_PROCESS_CAP=16` (hard ceiling)
  - Child pool: `BRAIN_MP_CHILD_DATABASE_POOL_SIZE=1`, `BRAIN_MP_CHILD_DATABASE_MAX_OVERFLOW=2` (example env names)

### 4) Other services (`chili`, `brain`)

- They keep their own pools; **total** must stay under `max_connections`.
- If you raise worker parallelism, **avoid** simultaneously raising `chili` pool without recalculating totals.

### 5) Provider / Massive / Polygon RPS

- Not DB, but a **throughput constraint**: **P × tickers × fetches** can trigger **429s**. Plan should include optional **`brain_queue_process_provider_rps_guard`** (reuse existing governors if any) or conservative default **P** on first ship.

---

## Implementation phases

### Phase A — Design flags & child pool (no process pool yet)

- Add settings + env aliases for executor mode and child pool sizes.
- In `app/db.py` or a one-time **child bootstrap** module: if `CHILI_MP_BACKTEST_CHILD=1` (or equivalent), create engine with **NullPool** or **(1,2)** pool—**before** any `SessionLocal()` in that process.
- Unit test: import `db` in subprocess with env set → assert pool class / params (if introspectable) or connection count under load test.

### Phase B — Picklable worker + ProcessPoolExecutor

- Extract **pure** `run_one_pattern_job(pattern_id: int, user_id: int | None) -> tuple[int, int]` at **module top level** (same return shape as today).
- Inside: set child marker env, configure logging, `SessionLocal`, call existing body of `_backtest_one_pattern_from_queue` (refactor to shared function to avoid duplication).
- Replace inner `ThreadPoolExecutor` default workers when in MP child (config).
- Wire `_auto_backtest_from_queue`: if `process` mode, `ProcessPoolExecutor` + `as_completed`; else existing threads.
- **Windows:** test on **spawn** (default on Windows); **Linux Docker** also uses spawn for safety unless you explicitly prefer fork with documented risks.

### Phase C — Ops: Compose + docs

- Update `docker-compose.yml` comments and suggested `postgres` `max_connections`.
- Update `.env.example` with **DB budgeting** subsection and example safe values for **P=8** and **P=16**.
- Add `docs/DOCKER_FULL_STACK.md` (or this doc’s “Operations” section) **checklist**: after changing P, verify `SELECT count(*) FROM pg_stat_activity;` under load.

### Phase D — Verification

- **Functional:** run `brain_worker.py --once` (or learning cycle) with `BRAIN_BACKTEST_PARALLEL=4`, process mode, small queue—same row counts / `mark_pattern_tested` behavior as thread mode.
- **Performance:** log wall time for queue step; expect **multi-core** CPU in Docker stats when process mode is on.
- **DB:** soak test: no `remaining connection slots` errors; `pg_stat_activity` peak < `max_connections - headroom`.

### Phase E — Rollout default

- Ship with **executor default = threads** (no behavior change).
- Document **opt-in** process mode for power users; after soak, consider defaulting to process on **worker only** when `cpu_count >= 8`.

---

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Connection storm from child pools | Mandatory **small child pool** or **NullPool**; formula in runbook. |
| Pickle / import failures on Windows | Top-level worker; minimal globals; test CI or local `pytest` with subprocess. |
| `_shutting_down` ignored in children | Use **timeout** on `future.result(timeout=...)`; on cancel, **terminate pool** only as last resort; document. |
| Double `brain_worker` | Unchanged: single lock; process pool is **intra**-worker only. |
| `save_backtest` / session thread-safety | Each process has its own session; avoid **shared** session across processes (already satisfied). |

---

## Definition of done

- [ ] Opt-in **process** executor for queue backtests with **documented** env knobs.
- [ ] **Child processes** use **bounded** DB pool (or NullPool) so **P × conns** fits under **`max_connections`** with **chili/brain** reserved.
- [ ] **Postgres** `max_connections` guidance updated for Compose (and optional bump in `docker-compose.yml`).
- [ ] **.env.example** + this plan reference **budget table** for P=8 / P=16.
- [ ] Tests: at least one **subprocess** smoke test; no regression in thread mode default.

---

## Rollback

- Set executor env back to **threads** (or remove env).
- Revert Compose `max_connections` only if lowered for unrelated reasons—generally **safe to leave higher**.

---

## Estimated effort

- **Phase A–B (code):** ~2–4 days (including Windows + Docker smoke).
- **Phase C–D (ops + soak):** ~1–2 days.
- **Total:** ~**3–6 days** calendar time depending on soak and DB tuning.

---

## References in repo

- `app/services/trading/learning.py` — `_auto_backtest_from_queue`, `_backtest_one_pattern_from_queue`
- `app/services/trading/backtest_engine.py` — `smart_backtest_insight`, `_bt_workers`
- `app/db.py`, `app/config.py` — engine pool
- `docker-compose.yml` — `brain-worker` env, `postgres` `max_connections`
- `scripts/brain_worker.py` — lock file, cycle driver
