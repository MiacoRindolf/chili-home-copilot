# CHILI refactor — Phase 0 inventory & audit

Generated as part of the **CHILI total refactor** plan. Use this as the baseline for extraction work and ADRs.

## Executive summary

- **~316** registered HTTP/WebSocket routes (see `scripts/_dump_routes.py` to regenerate).
- **Largest hotspots:** `app/services/trading/learning.py` (~5.5k lines), `scanner.py` (~3.8k), `routers/trading_sub/ai.py` (~1.5k). Trading + brain worker paths dominate complexity.
- **Compat / duplication:** Native Brain UI uses `/api/brain/trading/worker/*` and `/api/trading/brain/worker/*`; external SPAs may use `/api/v1/brain-*` ([`app/routers/brain_v1_compat.py`](../app/routers/brain_v1_compat.py)). Consolidation planned in Phase 3.
- **Scheduler:** Full `run_learning_cycle` in APScheduler is **disabled**; [`scripts/brain_worker.py`](../scripts/brain_worker.py) owns the continuous cycle. Other jobs (code/reasoning/project brain, breakouts, broker) remain in [`app/services/trading_scheduler.py`](../app/services/trading_scheduler.py).
- **Strangler implementation:** [`chili-brain/`](../chili-brain/) HTTP service delegates to existing `app.*` imports until logic is physically moved.

## HTTP surface (routers)

| Router module | Role |
|---------------|------|
| `app/main.py` | App assembly, middleware, module routers |
| `app/routers/auth.py` | Google OAuth, logout |
| `app/routers/chat.py` | Chat API, streaming |
| `app/routers/brain.py` | Cross-domain brain pages + trading/code/reasoning/project APIs |
| `app/routers/brain_v1_compat.py` | External SPA compatibility (`/api/v1/brain-*`, `/ws`) |
| `app/routers/trading.py` | Trading + includes `trading_sub/ai`, `broker`, `web3` |
| `app/routers/trading_sub/ai.py` | Trading brain AI, worker control, patterns, learning triggers |
| `app/routers/pages.py` | HTML pages |
| `app/routers/mobile.py` | `/api/mobile/*` |
| `app/routers/marketplace.py` | Module marketplace |
| `app/routers/admin.py` | Admin |
| `app/routers/health_routes.py` | Health |
| `app/routers/planner.py` | Planner |
| `app/routers/projects.py` | Projects |
| `app/routers/intercom.py` | Intercom + WS |
| `app/routers/voice.py` | Voice |

## Large files (line counts, approximate)

| Path | Lines (approx) | Notes |
|------|----------------|--------|
| `app/services/trading/learning.py` | 5.5k | Learning cycle orchestrator, backtest queue, evolution hooks |
| `app/services/trading/scanner.py` | 3.8k | Market scan, scoring |
| `app/routers/trading_sub/ai.py` | 1.5k | Many JSON endpoints |
| `app/services/trading_scheduler.py` | 1k+ | APScheduler jobs |

## Coupling & cycles (high level)

- **`learning.py`** imports scanner, prescreener, backtest engine, pattern evolution, journal, ML, etc. — **hub** module; any split must cut interfaces first.
- **Routers → `trading_service` / `learning` / `scanner`** — thin routers are rare; many call services directly.
- **`brain_worker.py`** → `run_learning_cycle` in-process; optional **remote** path via `CHILI_USE_BRAIN_SERVICE` + [`app/services/brain_client.py`](../app/services/brain_client.py).

## Background execution model (current)

| Mechanism | What runs |
|-----------|-----------|
| `scripts/brain_worker.py` | Full `run_learning_cycle` loop + file-based wake/stop/pause |
| `trading_scheduler.py` | Code/reasoning/project cycles, breakouts, broker sync, price monitor — **not** full trading learning (commented out) |
| Thread pools | Inside `learning.py` for parallel backtests / snapshots |

**Target model (from ADRs):** Brain HTTP service + worker container(s); CHILI optionally delegates learning cycle via HTTP when configured.

## Redundancy & questionable areas (candidates for cleanup)

| Area | Observation | Suggested action |
|------|-------------|------------------|
| Worker control | Paths under `/api/brain/trading/worker/*` vs `/api/trading/brain/worker/*` | Consolidate + migration guide |
| `brain_v1_compat` vs native | Overlap with worker wake | Keep compat until clients migrate; document |
| Backtest | `backtest_service.py` vs `backtest_engine.py` vs queue | Document ownership; reduce duplicate entry points in later phase |
| Debug | (removed) | `debug_agent_log` NDJSON sink removed; use app logger if needed |
| Config | Single large `config.py` | Split per domain after extraction |

## Move / keep / delete (initial)

| Component | Phase 0 disposition |
|-----------|---------------------|
| Trading learning core | **Keep** in monorepo; **delegate** via `chili-brain` strangler |
| `brain_worker` | **Keep** script; optional HTTP delegation |
| Code/reasoning/project services | **Keep**; placeholder HTTP surface on Brain service (`501` planned) |
| `brain_v1_compat` | **Keep** until external clients switch |
| Marketplace loader | **Keep**; formal contract in `docs/MARKETPLACE_MODULE_CONTRACT.md` |

## Regenerating route list

```powershell
conda activate chili-env
cd c:\dev\chili-home-copilot
python scripts/_dump_routes.py > routes_dump.txt
```

## References

- [ADR 001: Brain service boundaries](adr/001-brain-service-boundaries.md)
- [ADR 002: CHILI ↔ Brain auth](adr/002-chili-brain-auth.md)
- [ADR 003: Shared database & models](adr/003-shared-database-models.md)
- [Migration: Brain service](MIGRATION_BRAIN_SERVICE.md)
- [Docker full stack](DOCKER_FULL_STACK.md)
