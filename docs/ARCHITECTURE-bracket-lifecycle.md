# Bracket lifecycle — target architecture

Status: **proposed** — Phase 1 (tick-size) shipping; Phase 3 (single-owner restructure) pending.

## What is a bracket?

A "bracketed" position is one with three linked plans: an **entry**, a **stop** (downside cap), and a **target** (upside lock). Brokers like Robinhood don't expose this as one atomic order for stocks — you have to manage the three pieces as separate broker orders that *behave* like a bracket.

CHILI persists the brain's intent in `trading_bracket_intents` (one row per open trade), then various subsystems try to enforce that intent at the broker.

## Authority model — one owner per resource

| Resource | Single owner | Everyone else |
| --- | --- | --- |
| `trading_bracket_intents` row | `bracket_intent_owner` | reads only; mutations request a transition |
| State transitions on the row | `bracket_intent_owner.transition()` (state-machine guarded) | callers pass desired transition; helper rejects illegal ones |
| Broker SELL orders for protective exit | `bracket_executor` (merged `bracket_writer_g2` + `live_exit_engine`) | reads broker state; cannot place |
| Direct calls to `rh.orders.*` | `venue_adapter` | nobody else imports robin_stocks |
| Tick-size / precision | `tick_normalizer` | every price crossing the broker boundary goes through it |
| Audit events | `execution_event_bus` | every action records exactly one event |

## Clusters

### 1. Brain cluster — decides

- `pattern_engine` (scan + score)
- `regime_classifier` (market state)
- `stop_engine` (ATR + regime + lifecycle → stop_price)
- `target_engine` (R:R + regime → target_price)
- `auto_trader` (consumes the four, emits a `BracketSpec` dataclass — **in-memory only, not persisted**)

### 2. Intent cluster — owns persistent state

- `bracket_intent_owner` — only module that writes to `trading_bracket_intents`
  - `create(spec)` — initial row from a `BracketSpec`
  - `transition(intent_id, from, to)` — explicit state-machine guarded
  - States: `intent → confirmed_at_broker → exiting → closed` (illegal jumps rejected)
  - All reads exposed through typed getters (no raw query allowed elsewhere)

### 3. Execution cluster — enforces at broker

- `bracket_executor` — owns the SELL slot for every open trade
  - Replaces today's `bracket_writer_g2` + `live_exit_engine`
  - One internal queue per (trade_id, ticker) — no concurrent placement attempts
  - Routes prices through `tick_normalizer` before any broker call
  - Records every action to `execution_event_bus`
- `tick_normalizer` — venue-aware precision helper
  - Equity ≥ $1 → 2 decimals (NMS Rule 612)
  - Equity < $1 → 4 decimals (NMS Rule 612 sub-dollar)
  - Crypto → 8 decimals
  - Options ≥ $3 premium → 2 decimals; else $0.05 increments
- `venue_adapter` — only module that imports `robin_stocks` / Coinbase SDK
  - Translates `BracketSpec`/`OrderSpec` → broker API call
  - Returns normalized response (no leaky robin_stocks dicts)

### 4. Reconcile + audit cluster — read-only diff

- `bracket_reconciler` — diffs broker truth against `trading_bracket_intents`, emits events
  - **Strictly read-only.** No writes to the intent row, no broker-side mutations.
  - Emits `BracketDriftEvent` to the bus, suggesting a transition
  - The Intent owner subscribes and decides whether to apply
- `execution_event_bus` — append-only event log (existing `trading_execution_events` table, just enforce that everyone uses it)

## State machine

```
                   ┌──────── reject ─────────┐
                   ▼                          │
[ intent ] ──── place broker stop+target ──→ [ confirmed_at_broker ]
                                              │
                                              ▼
                                   [ exiting ] ── fill ──→ [ closed ]
                                              │
                                              ▼
                                   [ amending ] (when reconciler suggests resize/cancel)
```

Illegal transitions (e.g. `intent → closed` without going through `exiting`) are rejected by `transition()`.

## Why this kills the FIX 51-57 bug class

Every fix this week was a workaround for a race that exists *only because two writers can touch the same row*. Once `bracket_intent_owner` is the single writer:

| Today's fix | Why it goes away |
| --- | --- |
| FIX 51 (pre-flight broker-qty check) | `bracket_executor` reads broker state itself before placing. No "two callers disagree" possible. |
| FIX 52 (terminal-reject 1h cooldown) | The state machine rejects re-entry to `intent → confirmed_at_broker` if the previous attempt is still in flight. |
| FIX 53 (post-place 5min cooldown) | Same — the executor's queue serializes attempts; no churn possible. |
| FIX 55 (covered-by-existing-sell skip) | The executor *owns* the SELL slot — there can't be a foreign covering order, because nothing else can place a SELL. |
| FIX 56 (auto-cancel threshold) | If the broker repeatedly cancels, the executor records terminal state on the bus, the intent owner transitions to `terminal_reject` once, no retry. |
| FIX 57 (cancel covering limit before placing) | Same as FIX 55. |

Net deletion: ~600 lines of cooldown/threshold/race-mitigation code, replaced with ~150 lines of state-machine + queue code.

## Why this future-proofs

1. **One owner per resource** is enforceable with a CI rule: `grep` for any module other than `bracket_intent_owner` writing to `trading_bracket_intents` → fail build.
2. **Tick-size at the boundary** is enforceable with a CI rule: `grep` for `round(*, N)` in any file other than `tick_normalizer.py` → fail build.
3. **No raw `rh.orders.*` outside `venue_adapter`** is enforceable the same way.
4. **State transitions through a helper** means new lifecycle states (e.g. `paused`, `manual_override`) compose cleanly without touching every reader.

## Phase 3 implementation order (when approved)

1. Carve out `bracket_intent_owner` from existing `bracket_intent_writer.py`. Add `transition()` with state-machine. Migrate all writers in one PR.
2. Carve out `tick_normalizer.py`. Migrate `broker_service.py` rounding sites in one PR (this is also Phase 1 — it lands first).
3. Carve out `venue_adapter`. Move every `rh.orders.*` call into it. Existing callers go through the adapter.
4. Merge `live_exit_engine` into `bracket_executor` as a target-fill handler. Single SELL-slot owner.
5. Strip writes from `bracket_reconciler`. Convert to event emitter only.
6. Delete FIX 31 bridge, FIX 51-57 cooldown helpers, dead env-var flags.
7. Add CI guards (precision, ownership, no-raw-broker).
