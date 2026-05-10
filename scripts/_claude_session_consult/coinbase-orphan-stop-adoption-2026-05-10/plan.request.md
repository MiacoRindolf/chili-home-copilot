# Plan: f-coinbase-orphan-stop-adoption

Session: `coinbase-orphan-stop-adoption-2026-05-10`
Brief: `docs/STRATEGY/QUEUED/f-coinbase-orphan-stop-adoption.md`
Plan-gate consultation request — awaiting APPROVED / REVISE / ABORT.

---

## 1. Approach selection: **Option A — one-shot dispatch script**

Reasoning:

- **Cause is sealed.** Commit `c8a3ff3` (verify-routing fix) prevents
  new orphans from forming. The 4 known orphans (AERGO, 1INCH, ACX,
  RARE) are historical residue from the Robinhood-404 era. There is
  no recurring need for adoption logic.
- **Smaller blast radius.** A reconciler-integrated adoption pass would
  run every 60s on every Coinbase intent in perpetuity; if matching
  logic is too lax it silently corrupts intent rows project-wide.
  A one-shot fires once, is reviewable in the operator transcript,
  and the worst-case blast is the 4 known rows.
- **Constraint-aligned.** Hard constraints say "Do NOT modify
  reconciler / writer / stop_engine / autotrader". Option A keeps
  those untouched. Option B would also keep them untouched in source
  (the new module would be imported by the reconciler) but the
  *behavioral* footprint of B inside an actively-soaking Phase 6 LIVE
  loop is non-trivial.
- **Cowork's lean is also A.** The brief states "My lean: A".

I will pick **A**.

---

## 2. Files

### 2a. New module — adoption pass logic

`app/services/trading/venue/coinbase_orphan_adopt.py` (~150 lines)

Public surface:

```python
def adopt_coinbase_orphan_stops(
    db: Session,
    *,
    adapter: CoinbaseSpotAdapter | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Pull open Coinbase SELL stop-limit orders, match each to a single
    naked bracket_intent row by (ticker, quantity), and persist
    broker_stop_order_id + transition intent_state to reconciled.

    Returns a structured report: {
        "ok": bool,
        "dry_run": bool,
        "open_stop_orders_examined": int,
        "naked_intents_examined": int,
        "adoptions": [ {intent_id, trade_id, ticker, broker_stop_order_id,
                        prev_state, new_state, qty_local, qty_broker} ],
        "skipped": [ {ticker, reason, detail} ],
        "errors": [ {context, message} ],
    }

    On dry_run=True (default), no DB writes happen — the report shows
    what WOULD be adopted.

    Coinbase API unreachable raises VenueAdapterError (does NOT swallow).
    """
```

Helpers (all module-private, single-purpose):

- `_load_naked_coinbase_intents(db) -> list[_NakedIntentRow]` — SELECT
  bracket_intents JOIN trades WHERE
  `t.status='open'` AND
  `bi.broker_source='coinbase'` AND
  `bi.broker_stop_order_id IS NULL` AND
  `LOWER(bi.intent_state) IN ('intent', 'confirmed_at_broker', 'amending', 'terminal_reject')`.
- `_list_open_coinbase_stops(adapter) -> list[NormalizedOrder]` — calls
  `adapter.list_open_orders(product_id=None, limit=250)` and filters to
  `side=='sell'` AND `order_type` substring-matches `'stop_limit'`
  (case-insensitive — Coinbase returns `STOP_LIMIT` / `stop_limit_stop_limit_gtc`).
- `_extract_broker_qty(order) -> float | None` — reads
  `order.raw.get("base_size") or order.raw.get("size") or order.raw.get("original_size")`
  via `_sf`-style coercion. Returns None if not parseable (caller logs and skips).
- `_match_intent_to_order(intents, orders) -> tuple[matches, skips]` — ticker-keyed
  bipartite match; ambiguous = skip (multiple intents OR multiple orders OR qty mismatch).
- `_persist_adoption(db, intent, order_id, prev_state) -> bool` —
  calls `sync_broker_stop_order_id_mirror(db, intent_id, broker_value=order_id)`,
  then transitions intent_state to RECONCILED:
  - If `prev_state == TERMINAL_REJECT`, use
    `mark_auto_reconciled_after_terminal_reject(db, intent_id)` (the
    documented audited bypass — see `bracket_intent_writer.py:769`).
  - Otherwise call `transition(db, intent_id, to_state=RECONCILED, reason='orphan_adopt')`.
  - Both writers do NOT commit; the adoption pass commits once at end
    of the batch (matches sweep-loop convention).

### 2b. Dispatch script

`scripts/dispatch-coinbase-orphan-adopt.ps1` (~40 lines)

Style: mirrors `scripts/dispatch-stop-price-reject-probe.ps1`.
Behavior:

- Default mode: **DRY-RUN** (prints planned adoptions, makes no writes).
- Pass `-Apply` switch to flip dry_run=False.
- Output to `scripts/dispatch-coinbase-orphan-adopt-output.txt` AND stdout.
- Runs inside `conda run -n chili-env python -c "..."` (no new entry-point file
  needed — the script imports the module and calls the function directly).
- Pre-adoption SQL printout: the "expected SQL" from the brief
  (`SELECT t.id, t.ticker, bi.intent_state, bi.broker_stop_order_id ...`)
  so the operator sees before/after state in one log.

### 2c. Tests

`tests/test_coinbase_orphan_adopt.py` (~250 lines)

Cases (each constructs a stub `CoinbaseSpotAdapter` whose `list_open_orders`
returns scripted results — no live broker dependency):

1. `test_happy_match_single_intent_single_order` — one naked intent
   for AERGO, one open SELL stop-limit AERGO-USD, qty matches → one
   adoption, intent ends in RECONCILED with persisted order_id.
2. `test_qty_mismatch_skips_with_log` — qty differs by >tolerance →
   skip with reason `qty_mismatch`, no DB writes.
3. `test_multiple_intents_same_ticker_skips` — 2 naked intents for
   ACX → skip both with reason `multiple_intents`.
4. `test_multiple_orders_same_ticker_skips` — 2 open Coinbase stops
   for RARE → skip with reason `multiple_orders`.
5. `test_paper_trade_excluded` — intent where `broker_source IS NULL`
   never appears in the naked-intent set.
6. `test_intent_already_has_order_id_excluded` — intent with
   `broker_stop_order_id` already set never appears.
7. `test_terminal_reject_uses_auto_reconcile_bypass` — naked intent
   in `terminal_reject` state → uses
   `mark_auto_reconciled_after_terminal_reject`, ends in `reconciled`.
8. `test_intent_state_filter_excludes_closed_and_reconciled` —
   `closed` and `reconciled` rows are not candidates (already done).
9. `test_dry_run_reports_but_does_not_write` — dry_run=True → report
   shows planned adoption, but `broker_stop_order_id IS NULL` and
   `intent_state` unchanged in DB after call.
10. `test_adapter_unreachable_raises_not_swallowed` — adapter raises
    `VenueAdapterError` → adoption pass propagates (does not silently
    return ok=True).
11. `test_buy_orders_excluded` — open BUY orders for the same ticker
    are filtered out (only SELL stops are adoption candidates).
12. `test_non_stop_order_excluded` — open SELL LIMIT (target orders)
    are filtered out — only stop-limit type matches.

Test fixtures use the standard `db` fixture (truncated per-test) and
seed `Trade` + `BracketIntent` rows directly via the ORM. The adapter
stub is a small fake implementing `list_open_orders` + `is_enabled`.

---

## 3. Authority / state-machine choices

- **Adoption sets `intent_state = 'reconciled'`** rather than
  `'confirmed_at_broker'` because the broker order has been resting
  successfully (otherwise it wouldn't show up in `list_open_orders`).
  The semantics of `'reconciled'` per writer docstring (line 21) =
  "broker truth matches local intent (steady state)" — exactly the
  post-adoption condition.
- **Transition from `intent` → `reconciled`** is permitted by
  `_LEGAL_TRANSITIONS[IntentState.INTENT]` (writer line 142,
  comment: `# broker already has matching orders`).
- **Transition from `terminal_reject` → `reconciled`** is NOT permitted
  by the standard state machine (only `intent` or `closed` are legal
  exits). The writer already provides the audited bypass
  `mark_auto_reconciled_after_terminal_reject` (line 769) for exactly
  this scenario. I will use it — no new bypass invented.
- **Coinbase order_id persistence** uses
  `sync_broker_stop_order_id_mirror` (writer line 659), which is the
  existing single writer for the `broker_stop_order_id` column. I will
  NOT issue a raw UPDATE.

---

## 4. Matching tolerance

- **Ticker:** local intent `ticker` is bare (e.g. `AERGO`); broker
  order `product_id` is `AERGO-USD`. Match by uppercase
  `f"{intent.ticker.upper()}-USD" == order.product_id.upper()`.
- **Quantity:** broker `base_size` may have different decimal
  precision than local `quantity`. Use a relative tolerance:
  `abs(qty_local - qty_broker) / max(qty_local, 1e-9) <= 0.01`
  (1% relative tolerance), AND absolute tolerance
  `abs(qty_local - qty_broker) <= max(0.0001, qty_local * 0.01)`.
  Both must hold. If broker returns `base_size` as a string with
  scientific notation or padding, parse via the same `_sf` helper.
  - **Why 1%?** Coinbase rounds to the product's `base_increment`
    (e.g. AERGO is 0.01); local intent.quantity comes from the autotrader's
    pre-rounded compute. A 1% tolerance covers a reasonable
    rounding-driven divergence without accepting wildly different
    qtys. The brief says "If ambiguous (qty mismatch), log + skip,
    do NOT guess" — 1% within product-tick precision is not a guess,
    it's the expected rounding noise. Anything looser would be.
  - **Open question for Cowork:** is 1% acceptable or too lax? An
    alternative is "qty must match within 1× base_increment for the
    product" — more rigorous but requires per-product info fetch,
    which adds complexity. I will implement 1% and flag this in
    CC_REPORT.

---

## 5. Truncation discipline

After each Edit/Write on the new module:

```bash
wc -l app/services/trading/venue/coinbase_orphan_adopt.py
git diff --stat app/services/trading/venue/coinbase_orphan_adopt.py
conda run -n chili-env python -c "import ast; ast.parse(open('app/services/trading/venue/coinbase_orphan_adopt.py').read())"
```

Same for the test file. The `.ps1` script gets the PowerShell parser
check from advisor brief §2.1.

Commit only after all three checks pass on every modified file.

---

## 6. Commit plan

One logical commit per Cowork rule "one logical commit per task". If
mid-implementation I notice the brief needs a subtle deviation, I'll
stop, re-submit a revised plan.

Commit message draft:

```
feat(brain): coinbase orphan stop adoption pass (f-coinbase-orphan-stop-adoption)

One-shot adoption pass that pulls open Coinbase SELL stop-limit orders,
matches each to a single naked trading_bracket_intents row by (ticker,
qty), persists broker_stop_order_id, and transitions intent_state to
reconciled. Resolves the "Insufficient balance in source account" loop
caused by 4 historical orphan stops (AERGO/1INCH/ACX/RARE) reserving qty
at the venue while DB-naked.

- New: app/services/trading/venue/coinbase_orphan_adopt.py
- New: scripts/dispatch-coinbase-orphan-adopt.ps1 (DRY-RUN by default)
- New: tests/test_coinbase_orphan_adopt.py (12 cases)

Purely additive. Reconciler / writer / stop_engine / autotrader /
Robinhood adapter untouched. Coinbase Phase 6 LIVE soak unaffected.
```

---

## 7. Out of scope (per brief)

- ACS-USD #1842 qty divergence (DB=1.5M vs broker=0.27) — separate
  data-corruption issue, not orphan adoption.
- The 5 trades with no orphan stop (FIDA, COTI, ACH, ALEPH, ACS) —
  reconciler will retry now that verify-routing is fixed.
- Modifying reconciler / writer to AUTO-run adoption on every sweep.

---

## 8. Verification (post-implement)

1. Tests pass: `set TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test && conda run -n chili-env pytest tests/test_coinbase_orphan_adopt.py -v`
2. Truncation scan: `wc -l` on all 3 modified files matches
   `git show HEAD~1:<path>` + diff lines.
3. AST parse clean on .py + PowerShell parse clean on .ps1.
4. (Operator-side, post-merge) Run dispatch script in DRY-RUN mode,
   confirm 4 expected adoptions surface, then `-Apply` and verify
   the SQL from the brief shows 4 non-NULL `broker_stop_order_id`.

---

## Awaiting

- [ ] APPROVED → proceed with implementation
- [ ] REVISE: <feedback> → I rewrite this file and resubmit
- [ ] ABORT: <reason> → I write CC_REPORT and exit non-zero
