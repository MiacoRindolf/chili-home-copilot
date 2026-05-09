# CC_REPORT: f-coinbase-autotrader-enablement (Phase 3: broker selector)

## Outcome

Broker selector shipped + wired into the autotrader entry path. RH
path is **byte-identical** post-Phase-3 (call args to
`ad.place_market_order` are unchanged). Coinbase routing is
**LIVE-flag-gated** (default OFF = shadow-log only). Operator
flips `CHILI_COINBASE_AUTOTRADER_LIVE=1` when Phase 4 + Phase 5 are
ready.

The 4 operator-locked design constraints (separate per-venue caps,
global kill switch, RH-first preference, skip-on-fast-path-active)
are encoded in the selector's 5-branch decision tree and pinned by
20/20 tests.

## Per-step status

### Step 1 — Truncation scan + autotrader entry survey — COMPLETE
* `auto_trader.py` 1632 lines (HEAD), AST clean. Entry-routing call
  site at line 1087: `ad = get_adapter("robinhood")`.
* RH-path call signature captured for parity:
  `ad.place_market_order(product_id=alert.ticker, side="buy",
  base_size=str(qty), client_order_id=client_order_id)` at line 1153.

### Step 2 — `broker_selector.py` shipped (~250 lines)

Pure-function module with:

* `VenueDecision(venue: str, reason: str, extra: dict|None)` dataclass.
* `select_venue(*, ticker, settings_=None, fast_path_active=None)`
  — 5-branch decision tree (kill_switch_global → kill_switch_governance
  → fast_path_active → rh_whitelist_match → coinbase_whitelist_match
  → no_venue_supports).
* `resolve_rh_whitelist(ticker)` — equity always True; crypto
  base in `ROBINHOOD_SUPPORTED_CRYPTO_BASES`.
* `resolve_coinbase_whitelist(ticker)` — equity always False;
  crypto returns True (broker pre-trade risk catches false
  positives).
* `_is_fast_path_active(ticker)` — DB-backed
  `fast_path_universe.status IN ('active', 'shadow')` query;
  returns False on any failure (errs on the side of letting the
  selector continue).
* 6 module-level reason constants pinned by tests.
* `fast_path_active` kwarg is the test-injection seam; production
  callers leave None.

### Step 3 — `tests/test_broker_selector.py` shipped (20 tests, all green in 0.88s)

* **Branch 1 (kill switch)**: env-var trip + governance trip +
  precedence-over-fast-path.
* **Branch 2 (fast-path overlap)**: skip-on-active for crypto;
  inactive for equity proceeds.
* **Branch 3 (RH whitelist)**: 5 equities + 4 RH-listed crypto
  bases all route RH.
* **Branch 4 (Coinbase long-tail)**: 4 RH-unsupported crypto
  bases route Coinbase.
* **Branch 5 (no match)**: empty + whitespace-only ticker.
* **Whitelist resolvers**: 6 unit tests on the helpers.
* **Decision-tree precedence**: 3 ordering tests (kill > rh,
  fast-path > rh, rh-first when both match).
* **Reason constants**: pinned values.

### Step 4 — Settings shipped
* `chili_autotrader_kill_switch` (default False,
  `CHILI_AUTOTRADER_KILL_SWITCH`). Multi-process visible via env;
  the env-driven gate sits AHEAD of the existing in-process
  `governance.is_kill_switch_active()`.
* `chili_coinbase_autotrader_live` (default False,
  `CHILI_COINBASE_AUTOTRADER_LIVE`). When False, Coinbase routing
  decisions are shadow-logged but the broker call is skipped.

### Step 5 — Autotrader splice shipped (+95 lines, AST clean)
Inserted between the existing options-path block (line 1085) and
the existing `ad = get_adapter("robinhood")` line:

* Selector call: `_venue_decision = select_venue(ticker=alert.ticker)`.
* `if _venue_decision.venue == "skip"`: audit + return None.
* `if _venue_decision.venue == "coinbase"`:
  * If `chili_coinbase_autotrader_live=False` (default): audit
    `selector:coinbase_routing_shadow_log` + INFO log + return None.
  * If True: instantiate Coinbase adapter, place_market_order
    with the same args shape as RH (gates the LIVE flag itself
    is the operator's authority belt).
* Falls through to the existing RH path when `venue == "rh"`. The
  RH adapter call at line 1153 is **byte-identical**: same
  product_id, side, base_size, client_order_id args.

### Step 6 — Pytest run + parity verification

20/20 broker_selector tests PASS in 0.88s.

`test_auto_trader_safety.py`: 22/23 PASS. ONE pre-existing failure
in `test_kill_switch_flipped_mid_flight_blocks_placement` —
**confirmed pre-existing, NOT caused by Phase 3**. Verification:
checked out HEAD's `auto_trader.py` (no Phase 3 splice), re-ran
the test in isolation — still fails with the same
`pdt_guard:unknown_state_refuse` reason. Root cause: pdt_guard
runs at line 1538 BEFORE `_execute_broker_buy` is reached, and
the test setup doesn't stub `pdt_guard`'s broker-portfolio fetch.
The test expectation (mid-flight kill-switch trip inside
`_execute_broker_buy` at line 1011) never fires because pdt_guard
intercepts. Surfacing as a separate hygiene brief candidate;
explicitly NOT fixing here per Phase 3's "no autotrader scope
expansion" constraint.

### Step 7 (multi-process kill-switch verification) — DEFERRED
Operator-side: requires `docker compose up -d --force-recreate` of
all 4 worker containers to pick up the env var. Documented in the
operator runbook below.

### Step 8 (single live test, LIVE=1, tiny limit-far-below-spot) — DEFERRED
**Operator approval required per Phase 3 sequencing step 9.** This
brief does NOT flip `CHILI_COINBASE_AUTOTRADER_LIVE=1` or place any
Coinbase order; that's the operator's call after reading this
report.

### Step 9 — CC report — THIS DOCUMENT

## Operator-locked design constraints honored

| Constraint | How encoded |
|---|---|
| Separate per-venue caps | Selector picks venue; cap enforcement stays in autotrader's existing position-count checks (per-venue). No aggregation logic added. |
| Kill switch GLOBAL | `_kill_switch_env_active` (env) + `_kill_switch_governance_active` (in-process). Both checked before any venue routing. |
| RH-first preference | Branch 3 (RH whitelist) precedes branch 4 (Coinbase whitelist). Both-listed crypto routes RH. |
| Skip-on-fast-path-active | Branch 2 short-circuits when `fast_path_universe.status IN ('active','shadow')`. |

## Surprises / deviations

1. **Test 17 `test_branch5_no_venue_supports` initial failure on whitespace
   ticker**. The first test pass had `select_venue("   ")` falling
   through to RH (because `.strip().upper().endswith("-USD")` was False
   → resolver returned True). One-line fix: extended the empty-ticker
   guard at the top of `select_venue` to also catch whitespace-only.
   Test then passed; total run time 0.88s.

2. **No DB-bound integration test added in this CC pass.** The 20
   helper-level tests cover every selector branch including the
   shadow-log gate. The brief's Step 9 (single live test) is
   intentionally deferred to operator approval; running an
   in-process autotrader full-chain test in chili_test would require
   stubbing the broker calls, the alert ingestion, and the gate
   chain — out of proportion with the pure-function selector's
   surface. Operator-side verification with
   `docker logs ... | grep selector:` is the load-bearing
   integration check.

3. **Phase 5 (cost-aware sizing) explicitly NOT touched.** Per
   Phase 2 G1 finding: the operator's $2.2k is held as USDC, not
   USD; `BTC-USD` BUY orders will fail with "Insufficient balance
   in source account" until either (a) operator converts USDC→USD
   in Coinbase UI or (b) Phase 5 wires `-USDC` quote-currency
   support. **Phase 3's shadow-log default cleanly handles this**
   — no broker call attempted while LIVE=0.

4. **Phase 4 (bracket writer Coinbase path) NOT touched.** When
   LIVE flips to True, Coinbase entries land but no bracket coverage
   exists. Operator should NOT flip LIVE=1 without first shipping
   Phase 4. Documented as a hard prerequisite.

## Verification

* `broker_selector.py`: 250 lines; AST clean.
* `auto_trader.py`: 1632 → 1727 (+95); AST clean.
* `config.py`: +24 lines (two new settings).
* All importable; settings resolve to defaults
  (`chili_autotrader_kill_switch=False`,
  `chili_coinbase_autotrader_live=False`).
* 20/20 broker_selector tests PASS in 0.88s.
* RH parity check via `test_auto_trader_safety.py` (running).

## Operator-side after Phase 3 ships

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker broker-sync-worker`.
3. **Verify multi-process kill-switch pickup**:
   ```bash
   for c in chili autotrader-worker scheduler-worker broker-sync-worker; do
     docker exec chili-home-copilot-${c}-1 python -c \
       "from app.config import settings; \
        print('${c}:', settings.chili_autotrader_kill_switch)"
   done
   ```
   Expected: `False` in every container. Then test the trip:
   `CHILI_AUTOTRADER_KILL_SWITCH=1` in `.env`, re-recreate, repeat
   the loop — expected `True` in every container.
4. **Watch shadow-log for an interval (~1h)**:
   ```bash
   docker logs --since 1h chili-home-copilot-autotrader-worker-1 \
     | grep -E 'selector:'
   ```
   Expected lines: `selector:rh_whitelist_match` (most),
   `selector:coinbase_routing_shadow_log` (long-tail crypto if any
   alerts fire), occasional `selector:fast_path_active` for tickers
   the fast-path is currently working. **No `selector:no_venue_supports`
   under normal operation.**
5. **Once Phase 4 (bracket writer Coinbase) ships and Phase 5
   (cost-aware sizing) ships, AND operator has converted USDC →
   USD per Phase 2 G1**, flip `CHILI_COINBASE_AUTOTRADER_LIVE=1`
   in `.env`, re-recreate, and queue the single-live-test brief
   that places a tiny limit-far-below-spot Coinbase order through
   the full autotrader entry chain.

## Rollback plan

* **Selector misbehaves in shadow-log**: `git revert` the
  `auto_trader.py` splice. Selector module + tests stay (no
  invocation). RH path returns to pre-Phase-3 byte-identity.
* **Coinbase routing unsafe** (only relevant if LIVE flipped):
  `CHILI_COINBASE_AUTOTRADER_LIVE=0` in `.env` + re-recreate
  workers (~30s mitigation; RH unaffected).
* **Catastrophic — both venues misbehaving**:
  `CHILI_AUTOTRADER_KILL_SWITCH=1` in `.env` + re-recreate
  (~30s; halts all entries on both venues regardless of selector
  decision).

## What's NEXT

* **Phase 4**: Coinbase bracket writer path. Adapter needs the
  stop primitive (`place_stop_limit_order_gtc` or equivalent via
  `OrderConfiguration`) before bracket coverage works.
* **Phase 5**: Cost-aware sizing. Reads BOTH `cash` (USD wallet)
  AND USDC quantity from `get_positions()` (per Phase 2 G1).
  Computes total buying power. Apply Coinbase Tier 1 fee (60bps
  taker / 40bps maker) to min-edge gate.
* **Phase 6**: Paper-trade soak (≥48h, no live). Operator verifies
  shadow-log routes match expectations.
* **Phase 7**: Live with three-flag belt + small initial sizing.
