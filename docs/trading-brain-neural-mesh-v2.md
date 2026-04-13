# Trading Brain neural mesh (v2)

## Overview

The trading brain uses a **Postgres-backed** neural mesh as the **only** Trading Brain graph mode: topology in `brain_graph_nodes` / `brain_graph_edges`, runtime activations in `brain_activation_events`, and the desk reads `build_neural_graph_projection`. `run_learning_cycle` commits each architecture step, then calls `notify_learning_cycle_step_committed` so `nm_lc_*` step nodes enqueue real propagation events (plus `publish_learning_cycle_completed` at the end).

Legacy pipeline JSON and `brain_network_graph.py` are **removed**; compat routes (`/api/brain/trading/network-graph`, `/api/trading/brain/graph`) return the same neural projection.

No Kafka, Redis, NATS, Celery, or extra containers are required for v1: activation events are processed by the existing `brain-worker` process (or a dedicated `activation-loop` mode).

## Feature flags

| Env | Meaning |
|-----|---------|
| `TRADING_BRAIN_NEURAL_MESH_ENABLED` | Still read in some modules; mesh is **always on** in `brain_neural_mesh.schema.mesh_enabled()` for unified graph behavior. |
| `TRADING_BRAIN_GRAPH_MODE` | **Neural only** — `effective_graph_mode()` returns `"neural"`. |

## Worker modes (`scripts/brain_worker.py`)

| Mode | Behavior |
|------|----------|
| `lean-cycle` (default) | Full learning cycle + subtasks; after each successful cycle, runs a short activation batch. |
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

- `GET /api/trading/brain/graph/config` — flags + `effective_graph_mode` (always neural).
- `GET /api/trading/brain/graph` — neural DB projection (`build_neural_graph_projection`).
- `GET /api/trading/brain/graph/nodes/{id}`, `GET /api/trading/brain/graph/edges/{id}`.
- `GET /api/trading/brain/graph/activations`, `GET /api/trading/brain/graph/metrics`.
- `POST /api/trading/brain/graph/publish` — enqueue (non-guest).
- `POST /api/trading/brain/graph/propagate?dry_run=1` — savepoint rollback simulation.

## UI

Trading Brain → Network: **Neural mesh** (ring layout, excitatory/inhibitory edges, optional **Live** activation pulses). While a learning cycle is running, `/api/trading/scan/status` includes `mesh_step_node_id` / `mesh_cluster_node_id` so the desk can highlight the active `nm_lc_*` step and cluster.

## Manual validation

1. Apply migrations (including LC mesh and causal edge migrations).
2. Open `/brain` → Trading → Network; confirm graph loads.
3. `POST /api/trading/brain/graph/publish` with `source_node_id: "nm_snap_daily"` (debug).
4. Run worker `lean-cycle` or `activation-loop`; watch `brain_activation_events` for `cause=learning_step_completed` during cycles.
5. During a cycle, confirm the mesh highlights the current step node when polling status.

## Known limitations (v1)

- Thin canonical spine (~20 nodes); not every `run_learning_cycle` step is mirrored.
- `POST .../propagate` without `dry_run` runs a real batch (admin/debug).
- Metrics flush is best-effort every ~45s of batch activity.

## Diagrams

See [docs/diagrams/trading-brain-neural-mesh-v2.mmd](diagrams/trading-brain-neural-mesh-v2.mmd) and [docs/diagrams/trading-brain-neural-mesh-v2.svg](diagrams/trading-brain-neural-mesh-v2.svg).
