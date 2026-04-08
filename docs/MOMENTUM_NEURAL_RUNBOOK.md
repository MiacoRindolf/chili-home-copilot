# Neural momentum — operator / developer runbook

Copy/paste oriented. **Defaults are conservative**: paper/live runners and their schedulers are **off** unless you enable them.

## Prerequisites

- **PostgreSQL** with app migrations applied (`run_migrations` / app startup per your deploy).
- Migrations **089**, **090**, **091** applied (momentum mesh + persistence + outcomes).

## Mesh + neural momentum

```powershell
# Enable neural mesh (desk + graph projection)
$env:TRADING_BRAIN_NEURAL_MESH_ENABLED = "1"

# Optional: default graph mode (when mesh on)
$env:TRADING_BRAIN_GRAPH_MODE = "neural"
```

Momentum ticks require mesh + neural flag:

- `CHILI_MOMENTUM_NEURAL_ENABLED` (default `true` in Settings — set `0`/`false` to disable ticks)

## Coinbase adapter (readiness / live)

```powershell
$env:CHILI_COINBASE_SPOT_ADAPTER_ENABLED = "1"
# Optional strictness
$env:CHILI_COINBASE_STRICT_FRESHNESS = "1"
$env:CHILI_COINBASE_MARKET_DATA_MAX_AGE_SEC = "15"
```

Live orders need **SDK + API keys** per existing Coinbase integration; adapter `is_enabled()` must be true.

## Paper runner (simulated)

```powershell
$env:CHILI_MOMENTUM_PAPER_RUNNER_ENABLED = "1"
# Optional: scheduler batch (app trading_scheduler web-light profile)
$env:CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_ENABLED = "1"
$env:CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_INTERVAL_MINUTES = "3"
# Dev-only: HTTP tick endpoint (paired user)
$env:CHILI_MOMENTUM_PAPER_RUNNER_DEV_TICK_ENABLED = "1"
```

## Live runner (**real orders** — dangerous)

```powershell
$env:CHILI_MOMENTUM_LIVE_RUNNER_ENABLED = "1"
$env:CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED = "1"
$env:CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_INTERVAL_MINUTES = "2"
$env:CHILI_MOMENTUM_LIVE_RUNNER_DEV_TICK_ENABLED = "1"
```

**Production**: keep `CHILI_MOMENTUM_LIVE_RUNNER_ENABLED` and scheduler flags **off** unless you explicitly accept real trading. Use governance / kill switch as documented in risk policy.

## Feedback / evolution ingest

```powershell
$env:CHILI_MOMENTUM_NEURAL_FEEDBACK_ENABLED = "1"
```

## Trading UI

- Optional automation HUD on `/trading`: `CHILI_TRADING_AUTOMATION_HUD_ENABLED` (default true).

## Execution family registry

- Only **`coinbase_spot`** is implemented. Others return safe errors or `execution_family_not_implemented` from runners/APIs.
- Read-only list: `GET /api/trading/momentum/execution-families`

## pytest (from repo root, conda env `chili-env`)

**Fast (no DB):**

```powershell
conda activate chili-env
python -m pytest tests/test_execution_family_registry_phase11.py tests/test_momentum_neural_settings_closeout.py -v --tb=short
```

**Momentum area (needs Postgres on `TEST_DATABASE_URL`):**

```powershell
python -m pytest tests/test_momentum_neural.py tests/test_momentum_neural_persistence.py tests/test_momentum_operator_api.py tests/test_momentum_automation_api.py tests/test_momentum_risk_phase6.py tests/test_momentum_paper_runner.py tests/test_momentum_live_runner.py tests/test_momentum_feedback_phase9.py tests/test_brain_momentum_desk_phase10.py -v --tb=short
```

See [MOMENTUM_NEURAL_TEST_MATRIX.md](MOMENTUM_NEURAL_TEST_MATRIX.md) for phase mapping.

## Safe vs unsafe

| Dev / lab | Prod |
|-----------|------|
| Paper runner on | Prefer off unless you understand simulation limits |
| Live runner dev tick | **Avoid** on shared/prod hosts |
| Live scheduler | **Off** by default; treat as trading bot |
