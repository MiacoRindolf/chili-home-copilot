# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⭐ FIRST: read the strategy protocol

This project uses a **Cowork-directed workflow**. A separate Cowork session writes plans and the operator runs `claude` to execute them.

**On every Claude Code session start, after reading this file, ALSO read in order:**

1. `docs/STRATEGY/PROTOCOL.md` — defines the loop, file conventions, and hard rules
2. `docs/STRATEGY/CURRENT_PLAN.md` — the active initiative
3. `docs/STRATEGY/NEXT_TASK.md` — the specific task for this run

If `NEXT_TASK.md` has `STATUS: PENDING`, plan briefly, confirm scope with the operator if anything's ambiguous, then execute. When done, write `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_<slug>.md`, mark `NEXT_TASK.md` as `STATUS: DONE`, and commit.

If `NEXT_TASK.md` has `STATUS: DONE`, the operator hasn't queued a new task yet — say so and ask what they need.

## Project overview

CHILI is a local-first household assistant whose most sophisticated subsystem is an autonomous trading brain. Models recommend/decide inside typed, policy-bound envelopes; deterministic systems validate, constrain, execute, audit, and veto. Live trades touch real brokers (Robinhood, Coinbase), so correctness and safety rules are load-bearing, not cosmetic.

## Environment & runtime

- Python **3.11**; conda env **`chili-env`**. Use `conda activate chili-env` or `conda run -n chili-env …` for Python invocations and pip installs. Do not install into base.
- Platform is Windows (win32), bash shell. Helper scripts are PowerShell (`.ps1`).
- No ruff/black/mypy configured — do not add lint gates unprompted.
- If pytest fails to collect with `'Package' object has no attribute 'obj'`, the env's `pytest-asyncio` is older than the floor in `requirements.txt:92`. Recreate or upgrade: `conda run -n chili-env pip install -r requirements.txt --upgrade`.

## Common commands

### Run the app (local)

```powershell
# HTTPS (recommended) — frees port 8000, starts uvicorn with SSL
.\scripts\start-https.ps1

# HTTP dev mode — auto-finds free port among 8000/8010/8020
.\scripts\start-dev.ps1

# Override port
$env:CHILI_PORT='8010'; .\scripts\start-https.ps1
```

FastAPI app is `app.main:app`. Default URL: `https://localhost:8000/chat`. Certs in `certs/localhost.pem` + `certs/localhost.key`.

### Run the app (Docker)

⚠️ **Check `docker ps` first.** The live stack runs under `chili-clean-recovery-*` names, NOT
the compose service names — `docker compose up chili` starts a SECOND stack beside it. See
[Runtime topology](#runtime-topology-what-is-actually-running).

```bash
docker compose up chili           # main FastAPI service (HTTPS, 8000)
docker compose up brain-worker    # neural learning cycle (every 5s)
docker compose up scheduler-worker
docker compose --profile workers up    # mining/backtest/fast-scan workers
```

Postgres is mapped to **5433** (not 5432). Ollama on 11434.

⚠️ Rebuilding a container does not update the trading lane — that runs on the HOST from
`E:\dev\wt-window2`. Rebuilding the lane is a `git merge --ff-only` there plus a restart.

### Tests

`tests/conftest.py` **hard-fails** if `TEST_DATABASE_URL` is unset or its DB name doesn't end in `_test`. This guard exists because the fixture truncates tables — running it against live `chili` would wipe production data. For **production-shaped** data (CPCV dry-runs, regime rehearsal, etc.), use **`chili_staging`** (refreshed from `chili` per [`docs/STAGING_DATABASE.md`](docs/STAGING_DATABASE.md)) — not `chili_test`.

```bash
set TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
pytest tests/ -v                                      # all tests
pytest tests/test_entry_feature_parity.py -v          # single file
pytest tests/test_api.py -v -k "test_specific_name"   # single test
```

Fixtures: `db` (truncated per-test), `client` (guest), `paired_client` (seeded user + device).

### Migrations

Custom, **not Alembic**. Located in `app/migrations.py`, auto-run at app startup via `run_migrations(engine)` (skipped when `CHILI_PYTEST=1`). Add a new `_migration_NNN_*()` function, register in the `MIGRATIONS` list, never reuse IDs, make them idempotent (check for table/column existence before ALTER).

## Architecture

### Top-level layout

- `app/` — FastAPI backend (routers, services, models, templates, static)
- `app/trading_brain/` — phased migration framework for prediction dual-write → authoritative mirror
- `chili_mobile/` — Flutter mobile app
- `chili-brain/` — standalone brain module
- `scripts/` — start scripts, brain worker, scheduler worker, port utils
- `tests/` — pytest suite (requires `_test`-suffixed Postgres DB)
- `docs/` — architecture, strategy, runbooks
- `.cursor/rules/*.mdc` — authoritative architecture/process rules

### app/ structure

- `main.py` — FastAPI setup, migrations, scheduler wiring
- `config.py` — pydantic Settings
- `deps.py` — `get_identity_ctx(request, db)` resolves user via `chili_device_token` cookie (never trust the client)
- `migrations.py` — versioned DB migrations
- `models/` — 17 files, SQLAlchemy ORM split by domain (`trading.py`, `chat.py`, `household.py`, `planner.py`, `code_brain.py`, `core.py`, `projects.py`, `marketplace.py`, `intercom.py`, `reasoning_brain.py`, `project_brain.py`, …)
- `routers/` — 22 files. API surfaces: `chat` (+ `chat_streaming`), `trading` (+ `trading_sub/`), `brain` (+ `brain_project`, `brain_v1_compat`), `planner` / `planner_coding`, `admin`, `auth`, `pages`, `projects`, `marketplace`, `jobs`, `voice`, `intercom`, `dev_terminal`, `dispatch_status`, `health_routes`, `code_brain_status`, `context_brain_status`
- `services/trading/` — the bulk of the trading brain (**267 files**, plus **155 in `momentum_neural/`** — `live_runner.py` alone is ~42k lines and holds the entry/exit FSM)
- `services/trading/venue/` — 19 files. **`alpaca_spot.py` is the PAPER lane's execution venue and the one the momentum lane actually trades through** (`execution_family="alpaca_spot"`, `account_scope="alpaca:paper"`). Also `coinbase_spot.py` (crypto), `robinhood_spot.py` / `robinhood_mcp.py` (equity live), plus `factory.py`, `order_state_machine.py`, `idempotency_store.py`, `account_identity.py`, `venue_health.py`.

### Trading pipeline (signal → fill)

1. **Mine**: `services/trading/learning.py` runs the learning cycle (13 steps) — mines patterns from `trading_snapshots`, backtests, evolves.
2. **Decide**: `services/trading/auto_trader.py` consumes pattern-imminent alerts, applies rule gates, LLM revalidation, scale-in logic.
3. **Execute**: for the PAPER momentum lane this is `venue/alpaca_spot.py` (limit + `time_in_force="day"` + `extended_hours=True` outside RTH — Alpaca rejects market and stop orders in extended hours). `venue/robinhood_spot.py` / `venue/coinbase_spot.py` serve the live equity / crypto rails. `bracket_intent_writer.py` records stop/target intent.
4. **Reconcile**: `bracket_reconciler.py` + broker-sync (every 2min) reconciles DB against broker truth.
5. **Audit**: `execution_audit.py` logs expected-vs-realized cost gaps to `trading_venue_truth_log` (shadow mode).

### Authority model ("truth")

- **Broker APIs are authoritative for live fills** (Robinhood / Coinbase). CHILI mirrors.
- **`Trade` rows are authoritative for decision metadata** (entry reason, pattern, rule gates).
- **`trading_venue_truth_log` is shadow** — cost-drift audit; the `live-trading-truth-repair` branch hardens reconciliation when broker sync finds mismatches (dedupe, missing stops, stale positions).

### Prediction mirror (`app/trading_brain/`)

Phased (2 through 8) migration to persist predictions authoritatively in DB. **The authority contract is frozen** — phases 3–8 are hardened. Changing authority or log format requires a new phase with design + tests + soak + rollout doc; do not erode via side edits.

**Release blocker**: Do not ship if any `[chili_prediction_ops]` log line has `read=auth_mirror` AND `explicit_api_tickers=false` together. Authoritative mirror reads are only valid for explicit, non-empty ticker intent.

## Runtime topology (what is ACTUALLY running)

⚠️ **The compose service names are not the running container names.** `docker compose up chili`
would start a SECOND stack alongside the live one. Read `docker ps` first.

| running container | compose service | role |
|---|---|---|
| `chili-clean-recovery-web` | `chili` | FastAPI, `CHILI_SCHEDULER_ROLE=none` |
| `chili-clean-recovery-scheduler` | `scheduler-worker` | `CHILI_SCHEDULER_ROLE=rnd_only` |
| `chili-clean-recovery-brain` | `brain` | pinned image |
| `chili-home-copilot-postgres-1` | `postgres` | port **5433** |
| `chili-home-copilot-ollama-1` | `ollama` | 11434 |
| `chili-cloudflare-origin-bridge` | — | edge |

**Dead and NOT restarted** (both `Exited (137)`, 7+ weeks): `chili-clean-recovery-broker-sync`
and `chili-clean-recovery-autotrader`. Consequences that bite in practice:
- nothing reconciles a closed position back out of `trading_automation_sessions.risk_snapshot_json`,
  so a stale persisted `position` can block every later entry via the serial-recertification
  guard in `alpaca_orphan_claims.py` (`account_position_exposure_present`);
- `run_crypto_exit_pass` does not run, so crypto software stops are not evaluated.

### The trading lane runs on the HOST, not in a container

The momentum PAPER lane is a host `uvicorn app.main:app` on **port 8010** with
`CHILI_SCHEDULER_ROLE=momentum_exec_only`, started by
`project_ws/AgentOps/timeshare/timeshare_supervisor.py` (untracked operator tooling).

- **Deploy tree** (what the lane executes): `E:\dev\wt-window2` — `git merge --ff-only origin/main`
  there, then restart the lane. A merge to `main` does NOT reach it by itself.
- **Containers are baked images** and need a rebuild + manual swap; they lag `main`.
- The supervisor holds a PG advisory lock and its ACCEPT gate is **fail-closed**: it refuses to
  start unless the lease is held, all six prestart counters are 0, the broker census is clean
  (**account FLAT — no adopt path**), and the producer census is clean.
- ⚠️ **The flat check reads `risk_snapshot_json->'momentum_live_execution'->'position'`** —
  NOT `live_exec` (a similarly-named key that does not exist; a check against it is
  vacuously "flat"). Measured 2026-08-27: every flat gate that day used the wrong key and
  passed by luck (the broker census at ACCEPT was the real proof). The broker census in the
  ACCEPT receipt is the authority; the session-side check is the pre-check.
- ⚠️ **Do not kill the lane while it holds a position.** `_preshutdown_flatten` runs only on a
  NORMAL shutdown; the fail-closed ACCEPT returns before it, so a force-kill leaves the position
  unmanaged AND the lane unable to restart until the account is flat again.
- The scheduler jobs take **~4 minutes** after startup before the first arm.

### Market-data bridges (host, alongside the lane)

`scripts/iqfeed_trade_bridge.py` (trade prints) and `scripts/iqfeed_depth_bridge.py` (L2).
⚠️ At the RTH open the trade bridge ingests 30–49k rows/min and the frontier falls behind
real time. **Measure the FRONTIER (`max(observed_at)`), never a relative window** — a
`WHERE observed_at > now() - interval '60 seconds'` count reads ZERO whenever the pipeline is
behind, which looks like a dead feed and is not. Anything that judges "silence" must compare
against the tape frontier, not the wall clock.

## Hard rules (violating these breaks prod)

These come from `.cursor/rules/` and are non-negotiable:

1. **Kill switch before any automated trade.** `ensemble_promotion_check` must pass before a pattern goes live. See [docs/KILL_SWITCH_RUNBOOK.md](docs/KILL_SWITCH_RUNBOOK.md) for activation / reset / audit procedures.
2. **Drawdown breaker before sizing.** If it trips, trades are blocked until manual reset. See [docs/DRAWDOWN_BREAKER_RUNBOOK.md](docs/DRAWDOWN_BREAKER_RUNBOOK.md) for the incident playbook.
3. **Data-first, code-second.** When symptoms look like wrong FKs / contaminated linkage, fix the DB + add a migration. Do **not** paper over it with a router/service filter — that hides corruption from other consumers.
4. **Tests must use a `_test`-suffixed DB.** The guard in `conftest.py` is there because fixtures TRUNCATE. Do not bypass it.
5. **Prediction mirror authority is frozen** (see above). See [docs/PHASE_ROLLBACK_RUNBOOK.md](docs/PHASE_ROLLBACK_RUNBOOK.md) for rollback procedures when a phase flag misbehaves (rollback only — forward migrations need a new phase).
6. **Migrations are sequential and idempotent.** Check the last `_migration_NNN_` number before adding (**highest is 371 as of 2026-08-26**); never reuse IDs. Enforced at app startup by `_assert_migration_ids_unique` in `app/migrations.py`; run `.\scripts\verify-migration-ids.ps1` to check ahead of merge.

## Workflow rules

- **Run, don't delegate.** If a script/test/docker/DB command can run in this environment, run it and report exit code + output. Don't say "you should run X".
- **One logical change at a time.** Make it, test it, then proceed. Don't stack fixes.
- **Restart the server between changes.** Kill the existing process, start clean.
- **Parity testing for dual code paths.** Feed identical input to both (backtest vs live); assert equal output at each step. See `tests/test_entry_feature_parity.py`.
- **Flag conflicts in frozen scopes, don't veto.** The only authority contract that is truly frozen is the prediction mirror (Hard Rule 5). For everything else — rollout plans, feature-flag ramps, phased migrations — flag the conflict in one sentence, ask if unclear, then proceed with the user's explicit authorization. Don't treat internal rollout docs as hard gates; they exist as defaults, not vetoes. If the user says "flip it," flip it.

## Conventions

- **Imports, 4 sections**: stdlib → third-party → relative app → relative service. `Optional` is only for FastAPI `Query`/`Form` defaults.
- **Loggers**: `logger = logging.getLogger(__name__)`; prefix messages with `[module_name]`.
- **Pydantic**: `ConfigDict(extra="forbid")` on planner schemas to reject LLM hallucinations; `Field(min_length=1)` on required strings.
- **Inline request models** in trading/brain routers use `_`-prefixed names to avoid namespace clutter.
- **Identity**: always resolve via `get_identity_ctx(request, db)` — never accept user IDs from the client body.
- **Templates**: `request.app.state.templates`. **SSE**: `StreamingResponse(gen(), media_type="text/event-stream")`.
- **LLM fallback chain**: Ollama → NLU parser → Groq/Gemini → offline message. Every LLM-dependent path needs a fallback.
- **Market data priority**: Massive.com → Polygon.io → yfinance → CoinGecko.
- **Concurrency**: thread pools scale as `min(80, max(24, os.cpu_count() * 3))`. Caches must have hard max size + TTL.

## Key env vars

- `TEST_DATABASE_URL` — pytest; must end in `_test`
- `DATABASE_URL` — Postgres connection for app
- `STAGING_DATABASE_URL` — optional; `chili_staging` (prod-shaped copy for operator scripts; see `docs/STAGING_DATABASE.md`)
- `CHILI_TLS` — `1` for HTTPS (default in Docker)
- `CHILI_PORT` — override default 8000
- `CHILI_PYTEST` — `1` skips migrations on startup (set by conftest)
- `CHILI_SCHEDULER_ROLE` — `none` in app container, `all` in scheduler-worker
