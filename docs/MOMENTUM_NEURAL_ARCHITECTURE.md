# Neural-native momentum automation — architecture (closeout)

This document describes the **Coinbase spot + neural mesh** momentum system. It is **not** owned by the legacy learning-cycle.

## 1. Neural ownership

- **Intelligence**: `BrainGraphNode` / `BrainNodeState`, activation queue, `momentum_neural/*` (viability, evolution, feedback).
- **Learning-cycle** (`learning.py`, `learning_cycle_architecture.py`): legacy; **do not extend** for momentum semantics.

## 2. Surface split

- **Brain** (`/brain`, neural graph): momentum intel, viability pool, evolution/feedback visibility (read-model).
- **Trading** (`/trading`, `/trading/autopilot`, legacy `/trading/automation`): operator UX — viable strategies, paper/live arm, bounded runtime monitoring, and Autopilot inspection.

## 3. Strategy vs execution

- **`strategy_family`**: `MomentumStrategyVariant.family` — *what* logic (e.g. momentum variant).
- **`execution_family`**: column on variant, session, outcome — *how/where* orders route.
- **Implemented**: `coinbase_spot` only. Other IDs in `execution_family_registry` are **documented stubs** (no arbitrage, no multi-venue execution).

## 4. Execution layer

- **Venue adapter**: `venue/coinbase_spot.py` (`VenueAdapter` protocol). Readiness flows into viability metadata.
- **Live runner**: resolves adapter via `execution_family_registry`; real orders only when flags allow and family is implemented.

## 5. Paper vs live

- **Separate** FSMs, snapshots (`momentum_paper_execution` / `momentum_live_execution`), and outcome **`mode`**.
- Feedback and brain summaries keep **paper and live** distinct (no silent blending).

## 6. Risk and governance

- **Pre-runner**: `risk_evaluator` + config-backed `MomentumAutomationRiskPolicy`.
- **Kill switch** and policy flags can block live (and optionally paper).
- **Frozen** risk snapshot on session admission; runners must not overwrite audit keys.

## 7. Feedback loop (Phase 9)

- Terminal sessions → `MomentumAutomationOutcome` → `ingest_session_outcome` / evolution trace / capped viability nudges.
- Gated by `CHILI_MOMENTUM_NEURAL_FEEDBACK_ENABLED` and durable table presence (migration **091**).

## 8. Brain desk (Phase 10)

- Graph projection includes `momentum_desk` previews and panel metadata; optional REST under `/api/trading/brain/momentum/*`.

## 9. Migrations (order)

| Version | Purpose |
|--------|---------|
| **089** | Neural mesh nodes/edges for momentum hub/pool/evolution |
| **090** | Variants, viability, automation sessions/events |
| **091** | `momentum_automation_outcomes` for feedback |

## 10. Current limitations

- No cross-exchange arbitrage, triangular arb, or basis execution.
- No WebSocket OMS requirement; live path is **market-order-biased** and Coinbase-centric.
- Session PnL/state live in **snapshots + events**, not a full broker ledger reconciliation inside this feature.
- **DB tests** need reachable Postgres (`TEST_DATABASE_URL`).

## 11. Future seams

- New **execution families** need: adapter, registry entry, risk story, and explicit operator gates — not a silent column change alone.
