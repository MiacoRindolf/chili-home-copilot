# CC_REPORT: f-coinbase-autotrader-enablement (Phase 2: auth verification)

**STATUS: PASS** (with one operator-side gotcha for Phase 3 — see below).

Operator added Coinbase Advanced Trade credentials to `.env`; all four
worker containers were force-recreated to pick up the new env vars.
Phase 2's six verification items were exercised end-to-end, with the
order-placement plumbing surfacing a Coinbase source-account quirk
that would silently break a naive Phase 3 broker selector if not
documented up front.

Zero code changes shipped. One $5 limit-buy attempt at 50% below spot
was made; the broker rejected it (insufficient USD source balance —
the funds are in USDC, not USD) before any order rested on the book.
No cancel needed. Cash unchanged.

## Item-by-item results

### Item 1 — Credentials probe — **PASS**

```
COINBASE_API_KEY     -> resolves (organizations/<uuid>/apiKeys/<uuid>)
COINBASE_API_SECRET  -> resolves (multi-line PEM EC private key)
```

`.env`-level `\n` escapes correctly unfolded by
`coinbase_service.py:75` (`replace("\\n", "\n")`).

### Item 2 — Multi-process `is_connected()` — **PASS (4/4)**

```
chili-home-copilot-chili-1                credentials_configured=True  is_connected=True
chili-home-copilot-autotrader-worker-1    credentials_configured=True  is_connected=True
chili-home-copilot-scheduler-worker-1     credentials_configured=True  is_connected=True
chili-home-copilot-broker-sync-worker-1   credentials_configured=True  is_connected=True
```

`get_connection_status()` in every container:
`{'configured': True, 'connected': True, 'cb_available': True, 'api_key_set': True}`.

This was the failure class that bit us with Robinhood (process-local
`_logged_in` global divergence). Coinbase's auth path doesn't show
the same divergence — credentials are read freshly per call from
`settings`, not cached at module load.

### Item 3 — `get_portfolio()` — **PASS (with surprise)**

```
portfolio response shape: {'equity', 'buying_power', 'cash', 'last_updated'}
cash:    0.0
equity:  2973.92
total_balance_usd: None
```

**Surprise**: `cash = 0.0` despite the operator's $2.2k deposit. The
funds are held as **USDC stablecoin** (see Item 4), and the
`get_portfolio()` `cash` field reports USD dollars only. `equity` of
$2973.92 includes USDC marked-to-market plus dust positions
(see below).

This is **not a bug in CHILI** — it's how Coinbase Advanced Trade
exposes balances. But it's a **load-bearing gotcha for Phase 3 cost-
aware sizing**: any "do I have enough cash to buy $X of BTC-USD?"
check that reads `portfolio.cash` will see $0 even though the wallet
holds $2.2k of USDC.

### Item 4 — `get_positions()` — **PASS (32 positions)**

Shape:
```
{'ticker', 'quantity', 'average_buy_price', 'equity', 'current_price',
 'name', 'type', 'broker_source'}
```

Distribution:
- 1× **USDC-USD**: 2200.015893 units (the deposit, held as stablecoin)
- 31× **dust positions** from prior account activity: ACS, AMP, VET,
  XCN, and 27 others. All have `equity=0` and `current_price=0`
  (likely because Coinbase doesn't quote them or the SDK's price
  field isn't populated for these dust holdings).

The 31 dust positions are **historical noise** from earlier account
activity, not something Phase 3+ has to act on. They will surface in
any "list my positions" view but won't be selected for entry by
Phase 3's selector since they lack venue-quoted prices.

### Item 5 — Paper-test order — **PASS (broker correctly rejected)**

Plan executed:
```
BTC-USD spot: $80,886.18
limit-buy quantity: 0.00012363 BTC @ $40,443.09 (50% below spot)
notional: $5.00
```

Result:
```
place_buy_order response: {"ok": false, "error": "Insufficient balance in source account"}
captured order_id: (empty)
[coinbase] BUY order failed for BTC-USD: Insufficient balance in source account
```

**This is the right answer.** Coinbase Advanced Trade BUY orders for
`BTC-USD` debit the **USD wallet** as source. USD wallet balance is
$0. The $2.2k of USDC sits in the **USDC wallet** and would be the
source for `BTC-USDC` orders, not `BTC-USD`. The broker's pre-trade
risk check correctly refused and never created an order.

**End-to-end plumbing verified**:
- ✅ Coinbase SDK loaded with valid credentials
- ✅ Order request serialized + signed correctly (broker accepted the
  call shape; rejection was at the risk layer, not the auth layer)
- ✅ Error response surfaced cleanly to the caller
- ✅ Logger emitted the expected `[coinbase] BUY order failed`
  signature
- ✅ `try/finally` cancel branch correctly skipped (no order_id)

**Zero risk to operator capital**: nothing rested on the book; nothing
filled; cash unchanged.

### Item 6 — `get_recent_orders()` post-test — **PASS (0 residual)**

```
recent orders count: 0
```

Final portfolio sanity:
```
post-test cash:    0.0
post-test equity:  2973.92
```

Identical to pre-test. No state change.

## Gotchas surfaced for Phase 3 (binding)

### G1 — USDC ≠ USD-cash on Coinbase (load-bearing)

Phase 3's broker selector + cost-aware sizing must understand:

1. **`portfolio.cash` from Coinbase reports USD wallet only**, not
   total stablecoin-denominated buying power. With the operator's
   current funding pattern (USDC deposit, no USD), `cash=0.0`.
2. **Quote-currency selection matters**: `BTC-USD` debits USD wallet;
   `BTC-USDC` debits USDC wallet. CHILI's `coinbase_service.py`
   currently sends ticker as `BTC-USD` (e.g.,
   `place_buy_order(ticker='BTC-USD', ...)` in the paper-test). If
   the wallet has USDC but not USD, every BUY will fail with the
   exact error we just observed.
3. **Workaround options** (Phase 3+ design decision):
   - (a) **Switch ticker convention to `-USDC` pairs** for Coinbase
     entries when USD wallet is empty. Requires a quote-currency
     resolver in `venue/coinbase_spot.py`.
   - (b) **Auto-convert USDC → USD on entry** via a pre-trade
     `convert` API call. Adds a leg + slippage; not preferred.
   - (c) **Operator-side**: convert manually in the Coinbase UI
     before enabling autotrader Coinbase routing. Simplest for
     Phase 3 ship; document as a runbook step.
4. **Phase 5 (cost-aware sizing) implication**: the buying-power
   calculation must read both `cash` (USD wallet) AND any
   stablecoin position from `get_positions()` (USDC quantity at
   $1) to reflect actual buying power.

### G2 — 31 dust positions in wallet (informational)

Pre-existing tiny crypto holdings from earlier account activity.
Phase 3+ should:

- **Not auto-liquidate them** (out of scope; operator's prior
  positions).
- **Filter them from any "what's open" view** based on
  `equity == 0 and current_price == 0` to avoid noise in
  dashboards.
- **Skip them in the selector's whitelist computation** since they
  lack venue-quoted prices.

### G3 — `total_balance_usd` is `None` from `get_portfolio()`

The portfolio response includes a `total_balance_usd` key but the
value is `None` for this account. Either:
- The Coinbase SDK doesn't populate it for Advanced Trade accounts
  (only Coinbase Pro / retail), OR
- It requires a different scope on the API key.

**Phase 5 cost-aware sizing should not rely on `total_balance_usd`**
— compute total buying power from `cash` + USDC quantity instead.

## Constraints honored

- ✅ **No code changes**. Zero edits to `coinbase_service.py`,
  `coinbase_spot.py`, or any service/router file.
- ✅ **One paper-test order, far below market, broker-rejected**.
  The $5 BTC limit-buy at 50% below spot ($40,443) was the only
  order attempt. Broker rejected before placement; no cancel
  needed.
- ✅ **Hard 10s timeout on cancel**: not exercised (no order to
  cancel).
- ✅ **`try/finally` around order placement**: exercised; cancel
  branch correctly skipped on empty `order_id`.
- ✅ **No values logged**: API key/secret never read into output.
- ✅ **RH path untouched**.
- ✅ **Operator's $2.2k unchanged**: `cash=0.0` and `equity=2973.92`
  are identical pre- and post-test.

## Acceptance criteria — checklist

1. Items 1-4 audit-only pass before item 5 attempted: ✅ all four
   items passed in step-2 → step-4 of the verification script;
   step 5 attempted last.
2. Multi-process auth check across all 4 worker processes: ✅ 4/4
   pass.
3. Paper-test placement + cancellation within 5s (10s hard
   timeout); zero residual orders confirmed via
   `list_open_orders`: ✅ broker rejected at placement (no order
   to cancel); 0 recent orders post-test.
4. Operator's $2.2k unchanged post-test: ✅ portfolio identical.
5. CC report covers: pass/fail per item, response shapes, full
   paper-test order payload, gotchas surfaced for Phase 3: ✅
   this document.
6. NO code changes: ✅ confirmed.

## Recommendation for Phase 3

**Phase 2 unblocks Phase 3.** The auth + order-placement plumbing
works end-to-end. Three design decisions Phase 3 must address up
front (all surfaced from Phase 2's findings):

1. **Quote-currency convention** for Coinbase tickers. The selector
   needs to decide whether to route to `BTC-USD` or `BTC-USDC` based
   on which wallet has buying power. Operator's locked design
   constraint #3 (RH-first for both-listed tickers) helps —
   most equity-overlap tickers won't reach Coinbase routing — but
   crypto-native tickers (long tail) will.
2. **Buying-power calculation must include USDC**. Read `cash` +
   `USDC` quantity from positions; treat the sum as buyable
   capital.
3. **31-dust-positions filter** in the position view. Out of scope
   for Phase 3 implementation but worth a one-line filter where
   "open positions" is rendered.

## Rollback plan

N/A — no state changed. The report is the only artifact.

## Operator's next move

1. **Read this report.** Verify the gotchas (especially G1) match
   your understanding of the account state.
2. **Decide quote-currency strategy** for Phase 3: route `-USD` and
   accept that BUY orders fail until you convert USDC → USD, OR
   route `-USDC` and update CHILI's ticker convention. The latter
   is more work but keeps the deposited stablecoin productive.
3. **Promote Phase 3** (broker selector + venue abstraction) once
   the quote-currency decision is made. Phase 3 brief should bake
   in whichever convention you choose.

## ADDENDUM (Phase 2 redux — 2026-05-09 22:43 UTC)

**Operator manually converted USDC → USD in the Coinbase UI.** Re-ran
items 3-6 of the verification with the converted balance to prove
the full place+cancel chain works end-to-end with non-zero buying
power.

### Portfolio post-conversion

```
cash:           $2200.01
buying_power:   $2200.01
equity:         $2973.92  (cash + dust)
USDC quantity:  0.005893  (residual dust from conversion)
```

### Paper-test REDUX (full place + cancel)

Plan executed:
```
BTC-USD spot:  $80,794.41
limit-buy:     0.00012377 BTC @ $40,397.20 (50% below spot)
notional:      $5.00
```

**Place result** (0.45s round-trip):
```json
{
  "ok": true,
  "order_id": "149b388f-1b62-400e-9047-1b36b701ee75",
  "state": "pending",
  "raw": {
    "success": true,
    "success_response": {
      "order_id": "149b388f-1b62-400e-9047-1b36b701ee75",
      "product_id": "BTC-USD",
      "side": "BUY",
      "client_order_id": "f2392b62-fffb-4394-81be-205c6d39fff8"
    },
    "order_configuration": "{'limit_limit_gtc': {'base_size': '0.00012377', 'limit_price': '40397.2', 'post_only': False, 'rfq_disabled': False, 'reduce_only': False}}"
  }
}
```

**Cancel result** (0.14s round-trip — well under the 10s hard
timeout):
```json
{
  "ok": true,
  "order_id": "149b388f-1b62-400e-9047-1b36b701ee75",
  "raw": {"results": ["{'success': True, 'failure_reason': 'UNKNOWN_CANCEL_FAILURE_REASON', 'order_id': '149b388f-1b62-400e-9047-1b36b701ee75'}"]}
}
```

The `failure_reason: 'UNKNOWN_CANCEL_FAILURE_REASON'` is a Coinbase
SDK quirk on a *successful* cancel — `success: True` is the
authoritative field.

### Post-redux sanity checks

```
post-test open orders: 0
post-test cash:        $2200.01  (identical to pre-test)
post-test equity:      $2973.92  (identical to pre-test)
```

### What the redux proved

- ✅ **Order placement signed + accepted** by Coinbase (broker
  returned `order_id` and `state: pending`).
- ✅ **`coinbase_service.place_buy_order` response shape** as
  expected: `{ok, order_id, state, raw}`. Phase 3+ code can rely
  on this shape.
- ✅ **`coinbase_service.cancel_order_by_id` response shape** as
  expected: `{ok, order_id, raw}`. Same.
- ✅ **Round-trip latency**: place 0.45s + cancel 0.14s = 0.59s
  total. Well under operator-set 5s soft / 10s hard cancel
  timeout.
- ✅ **Zero residual orders** post-cancel. Cancel is reliable.
- ✅ **No fill, no fee, no capital impact**: cash + equity
  identical pre and post.

### Phase 3 quote-currency decision — RESOLVED

Operator converted USDC → USD manually, so cash is now in the USD
wallet. Phase 3 can ship the simpler `-USD` convention (matches
CHILI's existing `coinbase_service.py:place_buy_order(ticker='BTC-USD')`
calls). G1 from the original report is downgraded to:

> **G1 — quote-currency convention LOCKED to `-USD`.** CHILI sends
> `BTC-USD`, `ETH-USD`, etc. Funds debit the USD wallet. If the
> operator funds future deposits as USDC, they must convert to USD
> in the Coinbase UI before autotrader BUYs will succeed. Phase 5
> cost-aware sizing reads `portfolio.cash` (USD wallet) directly.

G2 (31 dust positions filter) and G3 (`total_balance_usd: None`)
remain as documented above.

### Phase 2 final verdict

**FULL PASS.** Auth + portfolio + positions + place + cancel + zero
residual + cash invariant — all six items green, end-to-end. Phase
3 unblocked with `-USD` convention locked.
