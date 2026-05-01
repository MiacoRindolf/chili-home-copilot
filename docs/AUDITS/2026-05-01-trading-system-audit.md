# CHILI Trading System — Comprehensive Audit
**Date**: 2026-05-01
**Trigger**: Robinhood SELL_STOP rejection storm + cancellation cycle revealed several layers of accumulated tech debt.
**Status of working positions**: Pre-incident state restored. Writer disabled. No further automated placements until this audit's recommendations are accepted.

---

## TL;DR

I found **four classes** of problems. Two are bugs that lose real money or invalidate broker submissions today. One is structural and the reason every fix this week kept generating new bugs. One is bloat that makes the system hard to reason about.

| Class | Findings | What it costs you |
| --- | --- | --- |
| **Precision / tick-size** | 20 | Crypto stops silently truncated 1–2%. Equity stops with 4-decimal brain output get post-rounded into `invalid` Robinhood states. P&L rounded at storage so audit history is wrong. |
| **Bracket-lifecycle architecture** | 28 | A single bracket_intent row has 5 independent readers/writers with no locking. Two subsystems (`bracket_writer_g2` and `live_exit_engine`) can fight over the same SELL slot. Every patch this week was symptomatic — the underlying race was always there. |
| **Hardcoded magic numbers** | 60 | Three duplicated policy values across files (drift risk). Multiple `or 0.5` fallbacks for missing measurements, which violate your own stated rule. Stop multiplier inconsistency: 0.97 in one file, 0.95 in another. |
| **Stale FIX/Round/flag scaffolding** | 78 markers, ~9 dead | One `bridge` (FIX 31) explicitly waiting to be deleted. 8 env-var kill-switches that haven't been flipped in production for weeks. Safe deletions. |

**No contradiction was found between active fixes** — they accumulated, didn't conflict. The pattern of bug-after-bug-after-bug this week was caused by symptom-patching when the real problem was the bracket-lifecycle architecture.

Full per-finding detail in the four agent reports referenced at the end.

---

## Phase A — DONE

✅ Restored: AIDX 150 × $6.08, CCCC 150 × $3.30, CRDL 200 × $1.92, TLS 100 × $5.13, VFS 50 × $5.46. All 5 limit-sells confirmed resting at the broker.
✅ Cancelled the 4 still-resting bogus SELL_STOPs (CCCC/CRDL/TLS/VFS at the rounded-to-$0.01 prices that Robinhood was about to flag as invalid). AIDX's had already self-cancelled.
✅ Writer kill-switch ON (`CHILI_BRACKET_WRITER_G2_PLACE_MISSING_STOP=0`). No automated SELL_STOP placements happen until the architectural fix below is in.

---

## CRITICAL #1 — Crypto precision is silently destroyed at the broker boundary

`app/services/broker_service.py` rounds every crypto submission to 2 decimals:

| Line | Function | What gets destroyed |
| --- | --- | --- |
| 2290 | `place_sell_stop_loss_order` | crypto stop price (DOGE-USD 0.10984 → 0.11, that's a 1.4% destructive shift) |
| 2423 | `place_crypto_buy_order` | crypto buy limit price |
| 2501 | `place_crypto_sell_order` | crypto sell limit price |
| 2083 | `place_buy_order` (equity) | also rounds — fractional equity gets truncated |
| 2171 | `place_sell_order` (equity) | same |
| 2728/2828/2932/2940 | options buy/sell limit | options premium rounded to 2 (usually OK but no granularity check) |

**Why it's CRITICAL:** the brain decides a stop at 0.10984 based on real ATR; the broker gets told the stop is at 0.11. That's not the brain's decision anymore. For a crypto position with $10K notional that's $140 of silent slippage per round trip, before the trade even runs.

**The reason your "stop is invalid" notifications appeared today specifically:** equity stops the brain produced with 4 decimals (CCCC 2.5898, CRDL 1.3326, TLS 4.0158, VFS 4.1766) got rounded to 2 at submission, then Robinhood's downstream validator flagged the rounded values as invalid because of how the stop-trigger price interacts with their "marketable" check. AIDX (1.84, already 2-decimal from the brain) and ELTX (11.0584 → 11.06, accepted) didn't trip this code path.

**Fix shape:** introduce `_venue_tick_size(ticker)` in `broker_service.py`. Equity = 2 decimals if price ≥ $1, 4 decimals if < $1 (NMS Rule 612). Crypto = 8 decimals always. Options = 2 decimals if premium ≥ $3, else $0.05 increments. Replace every `round(*, 2)` in submission code with `_normalize_to_tick(price, ticker)`. Mirror the change in `bracket_intent.py:310` so the brain stores values that are already tick-aligned.

**Estimated effort:** ~1 day. ~20 call sites, 1 helper, tests.

---

## CRITICAL #2 — Bracket lifecycle has no single owner

This is the underlying reason every fix this week generated a new bug.

### Today's bracket_intent has FIVE independent writers:

1. `auto_trader.py` (creates the row at entry — sets initial stop_price, target_price)
2. `stop_engine.py:611-617` (calls `upsert_bracket_intent` periodically — recomputes stop)
3. `bracket_intent_writer.py:208` (mutates intent_state on shadow→authoritative transition)
4. `bracket_writer_g2.py` (places broker orders that should logically update the row, but doesn't)
5. `bracket_reconciler.py` (reads + classifies; technically read-only but its output drives writes)

**No locking, no transaction isolation, no explicit state machine.** The reason `intent_state='shadow_logged'` rows exist next to `intent_state='intent'` rows is exactly this — different writers see different worlds.

### TWO subsystems can place SELL orders for the same position:

- `bracket_writer_g2.place_missing_stop` (the one I was patching all day)
- `live_exit_engine` (separate path that places SELL market orders when a target is hit)

These don't coordinate. When a target hits and `live_exit_engine` sells, the next bracket reconciliation sweep sees no resting stop and tries to place one against shares that are already exiting. **This is the exact pattern that produced FIX 51 → FIX 55 → FIX 57 today.**

### Authority confusion at the writer:

The brain decides stop_price at entry. The reconciliation sweep classifies "missing_stop". The writer enforces the stored stop_price unchanged — but it doesn't know if the position is being actively exited via `live_exit_engine`, doesn't know if the existing limit-sell is a take-profit ceiling vs a stale order, doesn't know if Robinhood will reject the placement before the sweep tries again.

**Recommended architecture (single-owner):**

| Resource | Single owner | Everyone else's role |
| --- | --- | --- |
| `bracket_intent` row | `bracket_intent_writer` (only module that writes) | All other code reads. Mutations go through a `transition()` helper with explicit state checks. |
| Broker SELL orders for an open position | `bracket_writer_g2` | `live_exit_engine` either merges into G.2 or registers as a "delegate" with G.2 holding the lock. |
| `intent_state` transitions | A small state machine (`intent → confirmed_at_broker → exiting → closed`) with rejected illegal transitions | Every read includes the state, never just the price. |
| `trading_execution_events` | Centralized bus (existing, just enforce) | Every action that touches a broker order must record an event. No event = no action. |

**Estimated effort:** 1 week if done deliberately. Cannot be quick-fixed — that's literally what produced this week's mess. Done right, it deletes more code than it adds (the various cooldown helpers, the kill-switches, FIX 51-57 most of FIX 55-57 collapse into "the writer owns the slot, no one else can touch it").

---

## HIGH — P&L stored rounded; reconciler drift is float-fragile

| Line | Issue |
| --- | --- |
| `broker_service.py:1213` | `average_buy_price = round(avg_cost, 4)` — broker reports 8 decimals, we store 4, reconciler classifies `price_drift` against an artifact |
| `broker_service.py:1714/1716/1833/1835/1888/1890` (six P&L call sites) | `trade.pnl = round((entry - exit) * qty, 2)` — penny-level rounding that loses sub-$0.01 P&L on fractional/crypto positions |
| `bracket_reconciler.py:100/226` | `if expected == 0`, `if broker_q == 0.0` — exact float equality, fails when value is 1e-15 |

**Why it matters:** today's reconciler classifies AIDX/CCCC/etc as `missing_stop` based on these comparisons. A floating-point edge case that says `broker_q != 0.0` when it's effectively zero will route the trade through the wrong classifier branch. Subtle, real, hard to repro.

**Fix shape:** store all prices/qtys at full precision (DECIMAL or 8-decimal float) in DB. Round only at display/audit boundaries. Replace every `==` on a float with `abs(a-b) < epsilon`, `epsilon` keyed off the venue tick size.

**Estimated effort:** ~half day. Pure cleanup, no semantics change.

---

## HIGH — Magic numbers + duplicated policy

The audit found **60 hardcoded constants** in financial-decision paths. The 15 CRITICAL ones cluster around three patterns:

### Pattern 1: Fallback values that violate your "no hardcoded fallbacks" rule

```python
# alerts.py:332, 693, 926
buying_power = broker_account.get("buying_power") or 10000.0
# stop_engine.py:301-307
if atr is None:
    return entry * 0.92, entry * 1.15  # 8% stop, 15% target
# live_exit_engine.py:43-45
DEFAULT_STOP_PCT = 0.03
DEFAULT_RISK_PCT = 0.03
```

You wrote a memory: "never `or 0.5` magic constants for missing measurements; compute dynamically or propagate None." **These violate that rule.** When the broker is disconnected and we use `or 10000.0`, we silently lie to the brain about the user's actual capital. The brain then sizes positions against a number that's wrong.

### Pattern 2: Stop multiplier inconsistency

| File | Multiplier |
| --- | --- |
| `contracts/signal_emit.py:212` | `0.97` (3% stop) |
| `live_exit_engine.py:43` | `0.97` (3% stop) |
| `alerts.py:1291-1292` | `0.95` (5% stop) |

Three files, three places, two different default risk policies. Whichever one fires depends on which code path you came in through. **You don't have one risk policy — you have three.**

### Pattern 3: Promotion thresholds without governance

```python
# learning.py:4007
if win_rate >= 0.55 and avg_return >= 0.02:
    promote()
```

Hardcoded 55% win-rate and 2% avg-return. No setting, no audit trail of who decided those values, no test that would catch a typo (0.55 → 0.5 silently changes pattern selection).

**Fix shape:** consolidate into a single `policy.py` (or use existing `Settings` more aggressively). Each value gets:
- A canonical name
- An env-var override
- A docstring explaining why that number
- A test that fails if the value is changed without updating the docstring

**Estimated effort:** ~2 days for the 15 CRITICAL ones. The 45 MEDIUM/LOW can come later or be batched.

---

## MEDIUM — Stale scaffolding + dead toggles

**Concrete deletes available right now (no risk):**

| Item | What it does | Why safe to delete |
| --- | --- | --- |
| FIX 31 (`reference_fix31_is_a_bridge.md`) | Cold-start carve-out forcing full `run_learning_cycle` on every restart | Phase 2 handlers cover this. Memory note explicitly says "user expects this gone." |
| `CHILI_DISPATCH_ENABLED=0` default | Disables the autonomous coding loop | Hasn't been ON in this branch's history. |
| Several `chili_*_enabled` settings that default OFF and have no operator-facing reason to flip | Toggles that no one uses | If a flag has been in one position for >2 weeks of production with no flip-event, it's dead config. |
| Phase G "shadow_logged" intent_state path | Pre-G.2 read-only mode | Phase G.2 is authoritative now. Shadow paths still execute on every sweep but no one consumes the output. |
| Multiple cooldown helpers (`_intent_reject_cooldown`, `_intent_post_place_cooldown`, `_intent_placement_count`) added by today's FIX 52/53/56 | Workarounds for the bracket-lifecycle race | If we fix CRITICAL #2 properly, all of these collapse into the writer's state machine. ~150 lines deletable. |

**Estimated effort:** ~1 day to verify and delete each.

---

## RECOMMENDED EXECUTION ORDER

I'm proposing this sequence. You sign off before each phase.

### Phase 1 — Tick-size correctness (1 day, blocks broker re-enable)
- Add `_venue_tick_size(ticker)` and `_normalize_to_tick(price, ticker)`
- Replace 20 `round(*, 2)` call sites
- Mirror in `bracket_intent.py:310` so brain stops are tick-aligned at storage time
- Add unit tests for: equity ≥$1, equity <$1, crypto, options
- After this, re-enable `CHILI_BRACKET_WRITER_G2_PLACE_MISSING_STOP=1` and verify the rejected stops would now go through

### Phase 2 — Float comparison + storage precision (½ day)
- Tolerance-based comparisons in `bracket_reconciler.py`
- Stop rounding P&L and avg_cost at storage; round only at audit-output boundary

### Phase 3 — Single-owner bracket lifecycle (1 week, biggest impact)
- Move every `bracket_intent` write through `bracket_intent_writer` only. Add a `transition()` helper with explicit state-machine guards.
- Either merge `live_exit_engine` SELL placement into `bracket_writer_g2` OR formalize a `slot_lock` so only one of them can place at a time per (trade, ticker).
- Delete the cooldown helpers from FIX 52/53/56 — replaced by the state machine.
- Delete FIX 31 bridge.

### Phase 4 — Magic-number consolidation (2 days)
- 15 CRITICAL constants → `policy.py` with env-var overrides, docstrings, tests
- Stop multiplier reconciliation: pick ONE risk policy (probably the 3% one — that matches `live_exit_engine` and `signal_emit`), retire the 5% in `alerts.py`
- Fallback fixes: every `or 10000.0` becomes `propagate None and refuse to size`

### Phase 5 — Dead-code sweep (1 day)
- Delete the 8 stale flags
- Delete shadow_logged code path in G phase
- Verify nothing reads it; remove

### Phase 6 — Future-proofing (1 day, structural)
- Add CI guard: `grep -rn 'round(*, 2)' app/` fails CI unless wrapped in `_normalize_to_tick`
- Add CI guard: every new constant in `app/services/trading/*.py` must come from `Settings` or have a `# inline-policy:` comment with rationale
- Add a "broker submission boundary" lint rule: anything that calls `rh.orders.*` directly outside of `broker_service.py` is a bug

### Total: ~2 weeks of focused work

---

## What I am NOT recommending

- **Don't rewrite the brain.** The brain's decisions look sound. It's the layers between brain → broker that are leaky.
- **Don't replace robin_stocks.** It's a thin wrapper; the precision bugs are CHILI's, not theirs.
- **Don't introduce a new framework.** The codebase doesn't need one — it needs single-owner discipline applied to the few resources that have multiple writers.

---

## Decisions needed from you

1. **Approve Phase 1 (tick-size fix)?** I can ship within a day. After that, the writer can be re-enabled safely. **(yes/no/not yet)**
2. **Approve Phase 3 (single-owner lifecycle)?** This is the big one. ~1 week, deletes more code than it adds, but requires you to be OK with me restructuring the bracket subsystem. **(yes/no/discuss)**
3. **Pick the canonical stop policy**: 3% (matches `live_exit_engine`/`signal_emit`) or 5% (`alerts.py`)? **(I recommend 3%)**
4. **Re-enable the writer after Phase 1, or wait until after Phase 3?** Phase 1 alone fixes today's symptoms. Phase 3 fixes the underlying disease. **(I recommend wait until Phase 3 — your positions are protected by manual limits right now.)**

I'll wait for your call on these before touching code.

---

## Appendix — Source audit reports

These four reports back this synthesis. Spawned today as parallel sub-agent investigations.

1. **Precision / tick-size** — 20 findings. (4 CRITICAL, 11 HIGH, 2 MEDIUM, 3 LOW)
2. **Bracket lifecycle** — 28 findings, full architectural recommendation, alternative single-owner design.
3. **Magic numbers** — 60 findings across 15 CRITICAL / 13 HIGH / 20 MEDIUM / 12 LOW.
4. **FIX/Round comments + dead code** — 78 markers audited, 9 confirmed safely deletable.

If you want any of the four full reports surfaced (they're verbose — 100+ pages combined), let me know which and I'll paste it.
