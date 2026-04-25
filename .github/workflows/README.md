# GitHub Actions workflows

## CI (`ci.yml`)

### What runs and when

- **Trigger:** every `push` to `main` and every `pull_request`.
- **Runner:** `ubuntu-latest`.
- **Python:** 3.11 (pip install from `requirements.txt`; no conda).
- **Database:** PostgreSQL **16** service container with `POSTGRES_USER=chili`, `POSTGRES_PASSWORD=chili`, `POSTGRES_DB=chili_test`, port **5432**, health check via `pg_isready`.
- **Job env (required):** `TEST_DATABASE_URL` and `DATABASE_URL` both set to `postgresql://chili:chili@localhost:5432/chili_test`. `CHILI_PYTEST` / `CHILI_SCHEDULER_ROLE` are **not** set in CI; `tests/conftest.py` sets them at import time.
- **Steps:** checkout → setup-python → `pip install -r requirements.txt` → wait until port 5432 accepts connections → `run_migrations(engine)` via one-line `python -c` → `bash scripts/verify-migration-ids.sh` → `pytest tests/ -v --tb=short -rs`.

### Reproduce a CI failure locally

Use a dedicated Postgres 16 database named with a `_test` suffix (conftest enforces this for pytest). Example with Docker:

```bash
docker run -d --name chili-pg-ci -e POSTGRES_USER=chili -e POSTGRES_PASSWORD=chili \
  -e POSTGRES_DB=chili_test -p 5432:5432 postgres:16
```

Then from the **repo root**, with the same Python env you use for development:

**Plain pip / venv (CI-equivalent):**

```bash
export TEST_DATABASE_URL=postgresql://chili:chili@localhost:5432/chili_test
export DATABASE_URL=postgresql://chili:chili@localhost:5432/chili_test
pip install -r requirements.txt
python -c "from app.db import engine; from app.migrations import run_migrations; run_migrations(engine)"
bash scripts/verify-migration-ids.sh
pytest tests/ -v --tb=short -rs
```

**Conda (local dev, WR8) — same steps inside the env:**

```bash
export TEST_DATABASE_URL=postgresql://chili:chili@localhost:5432/chili_test
export DATABASE_URL=postgresql://chili:chili@localhost:5432/chili_test
conda run -n chili-env pip install -r requirements.txt
conda run -n chili-env python -c "from app.db import engine; from app.migrations import run_migrations; run_migrations(engine)"
conda run -n chili-env bash scripts/verify-migration-ids.sh
conda run -n chili-env pytest tests/ -v --tb=short -rs
```

**Verify migration IDs only (after `pip install` / conda env has deps):**

```bash
bash scripts/verify-migration-ids.sh
# or
conda run -n chili-env bash scripts/verify-migration-ids.sh
```

---

## Known `pytest.skip` inventory

> **Note**: First T7 CI run reported 0 skipped, suggesting the conditions
> that trigger these pytest.skip calls behave differently in fresh-CI Postgres
> vs. dev-DB environments. The 27 entries below are the static call sites
> in `tests/`; whether each fires in CI depends on data state at test time.
> To be reconciled in the test-environment hardening ticket.

Generated from repo root with `grep -rn "pytest.skip" tests/` (equivalent: search for `pytest.skip(` under `tests/`). **Total: 27** call sites (the prior audit expected ~27; this inventory matches).

| file:line | skip reason |
|-----------|-------------|
| `tests/test_auto_trader_safety.py:62` | `advisory locks are Postgres-only` |
| `tests/test_auto_trader_safety.py:78` | `advisory locks are Postgres-only` |
| `tests/test_brain_momentum_desk_phase10.py:25` | `no mesh nodes` |
| `tests/test_brain_momentum_desk_phase10.py:54` | `momentum hub node not seeded` |
| `tests/test_brain_neural_mesh.py:59` | `migration 086 neural mesh seed not present` |
| `tests/test_brain_neural_mesh.py:81` | `migration 086 neural mesh seed not present` |
| `tests/test_brain_neural_mesh.py:93` | `no mesh nodes` |
| `tests/test_mesh_plasticity.py:123` | `neural mesh not seeded` |
| `tests/test_mesh_plasticity.py:208` | `mesh not seeded` |
| `tests/test_mesh_plasticity.py:237` | `mesh seed missing` |
| `tests/test_momentum_feedback_phase9.py:105` | `momentum_automation_outcomes table not present (run migrations)` |
| `tests/test_momentum_feedback_phase9.py:147` | `momentum_automation_outcomes table not present` |
| `tests/test_neural_graph_layout.py:79` | `no mesh nodes` |
| `tests/test_operational_clusters.py:76` | `neural mesh not seeded in this test DB` |
| `tests/test_operational_clusters.py:89` | `neural mesh not seeded in this test DB` |
| `tests/test_operational_clusters.py:108` | `neural mesh not seeded in this test DB` |
| `tests/test_operational_clusters.py:117` | `migration 152 structural edges not present (no members in this DB)` |
| `tests/test_operational_clusters.py:128` | `neural mesh not seeded in this test DB` |
| `tests/test_phase_e_observer_momentum_bridge.py:14` | `neural mesh not seeded` |
| `tests/test_phase_e_observer_momentum_bridge.py:25` | `neural mesh not seeded` |
| `tests/test_phase_e_observer_momentum_bridge.py:39` | `neural mesh not seeded` |
| `tests/test_portfolio_exit_mesh.py:136` | `neural mesh not seeded` |
| `tests/test_portfolio_exit_mesh.py:153` | `neural mesh not seeded` |
| `tests/test_portfolio_exit_mesh.py:172` | `neural mesh not seeded` |
| `tests/test_reconciliation_concurrent_sweeps.py:79` | `concurrency test is Postgres-only` |
| `tests/test_reconciliation_concurrent_sweeps.py:142` | `concurrency test is Postgres-only` |
| `tests/test_spine_feedback_edges.py:21` | `neural mesh not seeded` |

## Known CI failures (baseline as of f8226dd)

First run of T7 CI on a fresh Postgres 16 container produced 2607 passed,
84 failed, 18 errors, 0 skipped (run 24935569745). The failures cluster
into the following groups, which represent test-environment hardening
work scoped OUT of T7:

- **LLM/conversation tests** (openai_routing, conversations, voice, projects,
  test_api LLM offline tests): expect specific offline/mock states that
  CI's ambient environment doesn't provide. Owner: separate ticket.

- **Broker adapter tests** (autotrader_*, robinhood_*, momentum_*): assume
  adapter doubles that aren't loaded by conftest in a fresh CI env.
  Owner: separate ticket.

- **trade_assign_pattern, trades_sync, test_trading.py CRUD tests**:
  fixture-state assumptions (~30 tests in test_trading.py expect ambient
  user/device data that local dev DBs accumulate but CI does not).
  Owner: separate ticket.

- **Signal-to-reconcile e2e, brain_network_graph seed tests, workflow_state
  tests**: misc. Need individual investigation.

Until those tickets land, T7 CI runs as a regression baseline: new failures
on PRs above this 84-failure floor are real regressions; same-set failures
are pre-existing.
