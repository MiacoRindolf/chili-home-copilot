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

## Live runner (**broker orders** — dangerous)

The current Alpaca adapter is constrained to paper-account, US-equity, long-only. The
automation mode is still named `live` because it drives a real broker order lifecycle;
paper-account fills are not simulated. Keep the runner off during recertification.

Production ownership is exclusive: the event loop is the canonical driver and the
legacy APScheduler batch must stay off. The event loop cannot run without the price bus.
Never enable both drivers to try to clear a readiness warning; that configuration is
rejected and leaves no session owner.

```powershell
# Do not apply this block until the recertification rollout gate authorizes it.
$env:CHILI_SCHEDULER_ROLE = "momentum_exec_only"
$env:CHILI_MOMENTUM_LIVE_RUNNER_ENABLED = "1"
$env:CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED = "1"
$env:CHILI_AUTOPILOT_PRICE_BUS_ENABLED = "1"
$env:CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED = "1"
$env:CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED = "1"
$env:CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD = "iqfeed-l1-quote-provenance-v2+sha256:dc0185e65439364c"

# Required exclusivity: legacy/batch driver and dev HTTP tick remain off.
$env:CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED = "0"
$env:CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED = "0"
$env:CHILI_MOMENTUM_LIVE_RUNNER_DEV_TICK_ENABLED = "0"
```

The loop writes a durable cross-process heartbeat. The cockpit must show
`driver_mode=event_loop` with a fresh `live_loop_heartbeat_utc`; a missing, malformed,
overlapping-owner, future, or stale heartbeat is a blocker. The process-health check also
requires `CHILI_SCHEDULER_ROLE=momentum_exec_only`, the price bus, the loop flag, and the
legacy scheduler flag off. It also rejects an empty or different IQFeed authority pin;
the configured value must match the exact reviewed bridge source build:

```powershell
python scripts/verify_momentum_exec_process_health.py
```

**Production**: keep all live-runner flags off unless the broker-truth recertification and
paper soak authorize a rollout. Use governance / kill switch as documented in risk policy.

## Feedback / evolution ingest

```powershell
$env:CHILI_MOMENTUM_NEURAL_FEEDBACK_ENABLED = "1"
```

## Trading UI

- Optional automation HUD on `/trading`: `CHILI_TRADING_AUTOMATION_HUD_ENABLED` (default true).

## Execution family registry

- Implemented venue families include Coinbase and Robinhood spot paths plus the recertified
  subset of `alpaca_spot`. Alpaca is currently paper-only, equity-only, and long-only;
  `alpaca_short`, Alpaca crypto, and live-money Alpaca posture are quarantined before broker
  transport.
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
| Live event loop | **Off** by default; treat as a broker-order bot |
| Legacy live scheduler | **Off** in event-loop mode; enabling both drivers is invalid |
