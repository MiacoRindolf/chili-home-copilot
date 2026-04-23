# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

CHILI is a local-first household assistant whose most sophisticated subsystem is an autonomous trading brain. Models recommend/decide inside typed, policy-bound envelopes; deterministic systems validate, constrain, execute, audit, and veto. Live trades touch real brokers (Robinhood, Coinbase), so correctness and safety rules are load-bearing, not cosmetic.

## Environment & runtime

- Python **3.11**; conda env **`chili-env`**. Use `conda activate chili-env` or `conda run -n chili-env …` for Python invocations and pip installs. Do not install into base.
- Platform is Windows (win32), bash shell. Helper scripts are PowerShell (`.ps1`).
- No ruff/black/mypy configured — do not add lint gates unprompted.

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

```bash
docker compose up chili           # main FastAPI service (HTTPS, 8000)
docker compose up brain-worker    # neural learning cycle (every 5s)
docker compose up scheduler-worker
docker compose --profile workers up    # mining/backtest/fast-scan workers
```

Postgres is mapped to **5433** (not 5432). Ollama on 11434.

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
- `models/` — SQLAlchemy ORM split by domain (`trading.py`, `chat.py`, `household.py`, `planner.py`, `code_brain.py`, `core.py`)
- `routers/` — API surfaces: `chat`, `trading` (+ `trading_sub/`), `brain`, `brain_project`, `planner_coding`, `admin`, `pages`, `health_routes`
- `services/trading/` — the bulk of the trading brain (~60 files)
- `services/trading/venue/` — broker adapters: `coinbase_spot.py`, `robinhood_spot.py`

### Trading pipeline (signal → fill)

1. **Mine**: `services/trading/learning.py` runs the learning cycle (13 steps) — mines patterns from `trading_snapshots`, backtests, evolves.
2. **Decide**: `services/trading/auto_trader.py` consumes pattern-imminent alerts, applies rule gates, LLM revalidation, scale-in logic.
3. **Execute**: `venue/robinhood_spot.py` or `venue/coinbase_spot.py` places the order; `bracket_intent_writer.py` records stop/target intent.
4. **Reconcile**: `bracket_reconciler.py` + broker-sync (every 2min) reconciles DB against broker truth.
5. **Audit**: `execution_audit.py` logs expected-vs-realized cost gaps to `trading_venue_truth_log` (shadow mode).

### Authority model ("truth")

- **Broker APIs are authoritative for live fills** (Robinhood / Coinbase). CHILI mirrors.
- **`Trade` rows are authoritative for decision metadata** (entry reason, pattern, rule gates).
- **`trading_venue_truth_log` is shadow** — cost-drift audit; the `live-trading-truth-repair` branch hardens reconciliation when broker sync finds mismatches (dedupe, missing stops, stale positions).

### Prediction mirror (`app/trading_brain/`)

Phased (2 through 8) migration to persist predictions authoritatively in DB. **The authority contract is frozen** — phases 3–8 are hardened. Changing authority or log format requires a new phase with design + tests + soak + rollout doc; do not erode via side edits.

**Release blocker**: Do not ship if any `[chili_prediction_ops]` log line has `read=auth_mirror` AND `explicit_api_tickers=false` together. Authoritative mirror reads are only valid for explicit, non-empty ticker intent.

## Hard rules (violating these breaks prod)

These come from `.cursor/rules/` and are non-negotiable:

1. **Kill switch before any automated trade.** `ensemble_promotion_check` must pass before a pattern goes live. See [docs/KILL_SWITCH_RUNBOOK.md](docs/KILL_SWITCH_RUNBOOK.md) for activation / reset / audit procedures.
2. **Drawdown breaker before sizing.** If it trips, trades are blocked until manual reset. See [docs/DRAWDOWN_BREAKER_RUNBOOK.md](docs/DRAWDOWN_BREAKER_RUNBOOK.md) for the incident playbook.
3. **Data-first, code-second.** When symptoms look like wrong FKs / contaminated linkage, fix the DB + add a migration. Do **not** paper over it with a router/service filter — that hides corruption from other consumers.
4. **Tests must use a `_test`-suffixed DB.** The guard in `conftest.py` is there because fixtures TRUNCATE. Do not bypass it.
5. **Prediction mirror authority is frozen** (see above). See [docs/PHASE_ROLLBACK_RUNBOOK.md](docs/PHASE_ROLLBACK_RUNBOOK.md) for rollback procedures when a phase flag misbehaves (rollback only — forward migrations need a new phase).
6. **Migrations are sequential and idempotent.** Check the last `_migration_NNN_` number before adding; never reuse IDs. Enforced at app startup by `_assert_migration_ids_unique` in `app/migrations.py`; run `.\scripts\verify-migration-ids.ps1` to check ahead of merge.

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
