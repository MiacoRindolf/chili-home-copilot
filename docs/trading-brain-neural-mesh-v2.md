# Trading Brain neural mesh (v2)

## Overview

The trading brain adds an **optional**, **Postgres-backed** event-driven mesh alongside the legacy `run_learning_cycle` pipeline. The legacy graph (`learning_cycle_architecture` → `brain_network_graph`) and `/api/brain/trading/network-graph` remain stable.

No Kafka, Redis, NATS, Celery, or extra containers are required for v1: activation events live in `brain_activation_events` and are processed by the existing `brain-worker` process (or a dedicated `activation-loop` mode).

## Feature flags

| Env | Meaning |
|-----|---------|
| `TRADING_BRAIN_NEURAL_MESH_ENABLED=1` | Enable mesh publishers, worker batch, `/api/trading/brain/graph*` neural mode, UI toggle. |
| `TRADING_BRAIN_GRAPH_MODE=legacy\|neural` | Default desk mode when mesh is enabled (server-side `effective_graph_mode()`). |

## Worker modes (`scripts/brain_worker.py`)

| Mode | Behavior |
|------|----------|
| `lean-cycle` (default) | Full learning cycle + subtasks; after each successful cycle, runs a short activation batch if `TRADING_BRAIN_NEURAL_MESH_ENABLED=1`. |
| `activation-loop` | Neural queue + decay only (soak / dev). Sleep interval uses `--interval` as **seconds** (capped). |
| `mining` | Repeated `mine_patterns` (Compose `mining-worker`). |
| `backtest` | Repeated fast-backtest subtask (queue drain). |
| `fast-scan` | Repeated `run_pattern_imminent_scan` (Compose `fast-scan-worker`). |

Previously, `mining` / `backtest` / `fast-scan` were accepted by argparse but still ran `lean-cycle`; that is fixed.

## Database tables (migration `086_trading_brain_neural_mesh`)

- `brain_graph_nodes`, `brain_graph_edges` — topology + thresholds.
- `brain_node_states` — activation, confidence, staleness (truncated each test; topology kept).
- `brain_activation_events` — queue (`pending` → `processing` → `done`/`dead`).
- `brain_fire_log` — append-only fires.
- `brain_graph_snapshots`, `brain_graph_metrics` — optional audit / keyed metrics.

## Queue + propagation

1. Publishers `INSERT` rows with `status='pending'` (scheduler after market snapshots, end of successful learning cycle, or `POST /api/trading/brain/graph/publish`).
2. Worker/API batch claims rows with `FOR UPDATE SKIP LOCKED`, marks `processing`, applies **one hop** of outbound edges, updates `brain_node_states`, may append `brain_fire_log`, enqueues downstream events with bounded depth.
3. Gates are **structured JSON only** (`gate_config`), no arbitrary expressions.
4. **Excitatory** edges add activation; **inhibitory** edges subtract (see seeded `nm_contradiction` → `nm_action_signals`).
5. **Decay** reduces confidence over time using half-life on `staleness_at` / `updated_at` (global tick in batch).

## HTTP API (trading router)

- `GET /api/trading/brain/graph/config` — flags + `effective_graph_mode`.
- `GET /api/trading/brain/graph?mode=legacy|neural` — legacy delegates to existing graph builder; neural uses DB projection.
- `GET /api/trading/brain/graph/nodes/{id}`, `GET /api/trading/brain/graph/edges/{id}`.
- `GET /api/trading/brain/graph/activations`, `GET /api/trading/brain/graph/metrics`.
- `POST /api/trading/brain/graph/publish` — enqueue (non-guest).
- `POST /api/trading/brain/graph/propagate?dry_run=1` — savepoint rollback simulation.

## UI

Trading Brain → Network: **Legacy pipeline** vs **Neural mesh** (localStorage `chili_tbn_graph_mode`). Neural view uses ring layout, excitatory/inhibitory styling, optional **Live** polling of activations for edge pulses.

## Manual validation

1. Apply migrations; set `TRADING_BRAIN_NEURAL_MESH_ENABLED=1`.
2. Open `/brain` → Trading → Network → Neural mesh; confirm graph loads.
3. `POST /api/trading/brain/graph/publish` with `source_node_id: "nm_snap_daily"`, `signal_type` in JSON body via `signal_type` field.
4. Run worker `lean-cycle` or `activation-loop`; watch `brain_activation_events` drain and `brain_fire_log` grow.
5. Toggle Legacy and confirm pipeline graph unchanged.

## Known limitations (v1)

- Thin canonical spine (~20 nodes); not every `run_learning_cycle` step is mirrored.
- `POST .../propagate` without `dry_run` runs a real batch (admin/debug).
- Metrics flush is best-effort every ~45s of batch activity.

## Diagrams

See [docs/diagrams/trading-brain-neural-mesh-v2.mmd](diagrams/trading-brain-neural-mesh-v2.mmd) and [docs/diagrams/trading-brain-neural-mesh-v2.svg](diagrams/trading-brain-neural-mesh-v2.svg).
