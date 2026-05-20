# f-phase3-stop-bleed — Phase 3 stop-the-bleeding from 2026-05-15 quant audit

## Context

The 2026-05-15 quant audit (`docs/AUDITS/audit-discovery-stats-output.txt`)
+ legacy-cleanup verification
(`docs/AUDITS/audit-no-pattern-timing-output.txt`) established:

- 67-day live track: 444 closed trades, cum realized **−$1,172.35**.
- The −$1,172 is dominated by legacy pre-CHILI position cleanup. After
  excluding `scan_pattern_id IS NULL` trades the system is **+EV**:
  n=234, win_rate 34.2%, expectancy +$1.66/trade, profit_factor **1.54**,
  cum +$387.65.
- The legacy cleanup is essentially complete (12 closes in May vs 108 in
  March; only 2 open no_pattern positions remain).
- April lost $1,748 in a single month — the drawdown breaker as
  currently configured did not halt trading. This must not recur.
- Rejection histogram (last 7d) surfaced 4 actively-firing code defects
  and 1 broker-side error producer.

This brief ships the **stop-the-bleeding** tier of Phase 3.

## Goal

Ship 7 deliverables in a single CC session:

1. Monthly-realized-drawdown gate inside `check_drawdown_breaker`
2. Better diagnostic on `coinbase_cap_unavailable:NameError`
3. Pre-send product_id normalizer in Coinbase venue adapter
4. Pre-flight cash-check at the Coinbase venue layer
5. Upstream fix for the producer of stop-at-or-above-entry alerts
6. `@validates("scan_pattern_id")` model-layer guard
7. BNB-USD zombie row cleanup (mig or one-off UPDATE)

Plus tests + a verification re-run of the discovery probe.

## Deliverables

### D1. Monthly-realized-drawdown extension to existing breaker (data-driven threshold, no magic fallback)

**Where:** `app/services/trading/portfolio_risk.py:909` —
`check_drawdown_breaker(db, user_id, capital)`. Extend, do not duplicate.

**What:** Add a new check that trips when realized 30-day PnL falls below
an **empirically-derived** lower bound on the system's 30-day PnL
distribution. The threshold is recomputed at every call from CHILI's
own realized history — no hardcoded dollar amount, no "5% of equity"
magic constant.

**Methodology (per COWORK_ADVISOR_BRIEF §2.6 no magic fallbacks):**

The dollar threshold is the lower bound on 30-day realized PnL under
Gaussian assumption fit to CHILI-attributed daily PnL history:

```
threshold_$ = mean_30d_pnl − K × std_30d_pnl

where
  daily_pnls = [SUM(pnl) per close-date over last 180d of CHILI-attributed
                closed trades — i.e. scan_pattern_id NOT NULL AND != -1]
  mean_30d = 30 × mean(daily_pnls)
  std_30d  = √30 × stdev(daily_pnls)   # iid scaling
  K        = chili_monthly_dd_breaker_lower_bound_sigmas (default 2.0,
             standard 95%-Gaussian lower-bound multiplier)
```

When `len(daily_pnls) < 30` (insufficient history), the threshold helper
returns `None` and the breaker skips the check WITH a logged warning at
the call site — **no fallback dollar value**, per the no-magic principle.
As history accumulates the threshold tightens organically.

**Implementation sketch:**

```python
def _monthly_dd_threshold(db: Session, user_id: int | None,
                         settings=None) -> Optional[float]:
    """Empirical Gaussian-lower-bound on 30-day realized PnL, computed
    from CHILI-attributed live history (scan_pattern_id NOT NULL and
    != -1). Returns None when len(daily_pnls) < 30. See
    f-phase3-stop-bleed D1 + COWORK_ADVISOR_BRIEF §2.6 — no fallback
    dollar value; caller must handle None."""
    rows = db.execute(text("""
        SELECT DATE_TRUNC('day', COALESCE(exit_date, last_fill_at, filled_at))
                 AS d,
               COALESCE(SUM(pnl), 0)::float AS daily_pnl
          FROM trading_trades
         WHERE user_id = :uid
           AND status = 'closed' AND pnl IS NOT NULL
           AND scan_pattern_id IS NOT NULL AND scan_pattern_id != -1
           AND COALESCE(exit_date, last_fill_at, filled_at)
               >= now() - interval '180 days'
         GROUP BY 1
    """), {"uid": user_id}).fetchall()
    if len(rows) < 30:
        return None
    daily = [float(r.daily_pnl or 0.0) for r in rows]
    n = len(daily)
    mean_d = sum(daily) / n
    var_d = sum((p - mean_d) ** 2 for p in daily) / max(n - 1, 1)
    std_d = var_d ** 0.5
    s = settings if settings is not None else _settings_module()
    K = float(getattr(s, "chili_monthly_dd_breaker_lower_bound_sigmas", 2.0))
    return (30.0 * mean_d) - K * ((30.0 ** 0.5) * std_d)


# Inside check_drawdown_breaker, after the existing peak-trough check:
flag_enabled = bool(getattr(settings,
                            "chili_monthly_dd_breaker_enabled", False))
if flag_enabled:
    threshold = _monthly_dd_threshold(db, user_id, settings=settings)
    if threshold is None:
        logger.warning(
            "[risk] monthly_dd_breaker enabled but <30d "
            "CHILI-attributed history; skipping check (the breaker "
            "activates organically once history accumulates)"
        )
    else:
        monthly_pnl = db.execute(text("""
            SELECT COALESCE(SUM(pnl), 0)::float AS pnl
              FROM trading_trades
             WHERE user_id = :uid AND status = 'closed' AND pnl IS NOT NULL
               AND COALESCE(exit_date, last_fill_at, filled_at)
                   >= now() - interval '30 days'
        """), {"uid": user_id}).scalar() or 0.0
        if float(monthly_pnl) <= float(threshold):
            return True, (
                f"monthly_dd_breaker: 30-day realized PnL "
                f"${float(monthly_pnl):.2f} <= empirical Gaussian "
                f"lower-bound ${float(threshold):.2f} "
                f"(K={K}σ, computed from {n}d CHILI history)"
            )
```

**Settings additions** in `app/config.py`:
```python
chili_monthly_dd_breaker_enabled: bool = False  # default off until soak
chili_monthly_dd_breaker_lower_bound_sigmas: float = 2.0  # 95%-Gaussian; tighten to 3.0 for ~99.7%
```

**Hard constraints:**
- **No fallback dollar number anywhere.** If history is insufficient the
  helper returns None and the breaker skips — caller logs and continues.
- All queries scoped to `user_id` (single-user but be defensive).
- Filter scan_pattern_id NOT NULL AND != -1 so legacy/reconcile rows
  don't pollute the empirical distribution.

**Walk-forward verification** (part of D9 below): replay 2026-03-10 →
2026-05-16 daily, computing the threshold each day from the rolling
180d history (which during the early period is just whatever exists),
and recording the date the breaker *would have* tripped. The reasonable
target is "tripped on or around 2026-04-22" — the cumulative-PnL trough
date. If walk-forward shows the breaker tripping much earlier or never,
the methodology needs revisiting before the operator flips the flag ON.

### D2. NameError diagnostic improvement (C5 from audit)

**Where:** `app/services/trading/auto_trader.py:1597-1612` — the catch
that wraps the call to `cost_aware_gate.per_venue_cap_check`.

**What:** The current reason string is `coinbase_cap_unavailable:NameError`
which tells us a NameError fires 54x/week but not *which name* is unbound.
Improve to capture the actual identifier so we can pin it down.

```python
# Replace line 1602:
#   reason=f"coinbase_cap_unavailable:{type(exc).__name__}",
# With:
_exc_detail = type(exc).__name__
if isinstance(exc, NameError) and getattr(exc, "name", None):
    _exc_detail = f"NameError:{exc.name}"
reason=f"coinbase_cap_unavailable:{_exc_detail}",
```

**Why this and not "find and fix the NameError":** Without running the
code under production conditions we cannot reproduce the unbound name.
The diagnostic improvement turns 54 anonymous reports/week into 54
named reports/week. A follow-up CC task pins it down once we have the
name.

**Also:** make sure the `exc_info=True` in `logger.warning` at line 1610
actually emits — check the prod log configuration during CC run.

### D3. Product ID normalizer (C6 — INVALID_ARGUMENT)

**Where:** `app/services/trading/venue/coinbase_spot.py` — add a private
helper, call it at the top of each of:
- `place_market_order` (line ~642)
- `place_limit_order_gtc` (line ~746)
- `place_stop_limit_order_gtc` (line ~853)

**What:**
```python
import re
_VALID_PRODUCT_ID = re.compile(r"^[A-Z0-9]+-(USD|USDC)$")

def _normalize_product_id(product_id: str) -> str:
    """Coinbase Advanced Trade rejects malformed product_ids with HTTP
    400 INVALID_ARGUMENT. See f-phase3-stop-bleed D3. Reject locally
    instead of paying the round-trip + counting against rate limit."""
    pid = (product_id or "").strip().upper()
    # Common drift: ticker with no suffix (e.g. "BTC" instead of "BTC-USD"),
    # USD-stripped (e.g. "BTCUSD"), forward-slash separator from CCXT
    # conventions ("BTC/USD"). We do not auto-correct -- the producer is
    # the bug. Just refuse so the broker doesn't see it.
    if not _VALID_PRODUCT_ID.match(pid):
        raise ValueError(
            f"coinbase_spot: invalid product_id {product_id!r}; "
            f"expected '<BASE>-USD' or '<BASE>-USDC'"
        )
    return pid
```

At each placement method:
```python
product_id = _normalize_product_id(product_id)
```

**Verify upstream:** also grep for all callers of these three methods and
log the file:line for each. The producer of the bad product_ids must be
found and fixed in a follow-up — but the venue-layer guard alone reduces
broker rejections to zero immediately.

### D4. Pre-flight cash check (C7 — Insufficient balance)

**Where:** same three methods in `coinbase_spot.py`. Add immediately
after the product_id normalization, before the actual Coinbase SDK call.

**What:**
```python
from ..cost_aware_gate import resolve_coinbase_buying_power
# Conservative pre-flight: never send if local view of buying power is
# below the order's required notional. The exchange will check too, but
# 830 rejections/week on this exact error indicates we're losing
# race-conditions between our resolver and the placement call. A local
# refuse is cheaper than a broker round-trip + rate-limit charge.
if side.upper() == "BUY":
    required = float(quote_size or (base_size * limit_price)) * 1.005  # 0.5% slack for fees
    bp = resolve_coinbase_buying_power(db=db_session, user_id=user_id)
    if bp.total_usd is not None and bp.total_usd < required:
        raise InsufficientFundsError(
            f"coinbase_spot: local buying_power ${bp.total_usd:.2f} < "
            f"required ${required:.2f} for product={product_id}"
        )
```

**Hard constraint:** the resolver call must NOT block on a network round-trip
(`resolve_coinbase_buying_power` should serve from a recent cache; if it
doesn't, that's a separate fix). If the cache is stale by more than 5
seconds at the moment of the call, log a warning and allow the call
through (the broker is the final check).

**Caveat:** if `resolve_coinbase_buying_power` signature doesn't match
what's in `__all__`, adapt accordingly. CC should `grep -nE "def resolve_coinbase_buying_power"` and use the actual signature.

### D5. Upstream fix for `stop_not_below_entry` producer (M3)

**Where:**
- Rule that rejects: `app/services/trading/auto_trader_rules.py:915`
  (`stop_not_below_entry`) — keep this as the safety net, do not weaken
- Find the alert producer that's emitting bad stops:
  `grep -rnE "stop_loss\s*=" app/services/trading/` then filter to the
  small set of files that *write* into BreakoutAlert.stop_loss

**What:** at the producer site, enforce the same invariant the rule
checks: `stop_loss < entry_price - epsilon` for long, `stop_loss >
entry_price + epsilon` for short, where epsilon is a small absolute
amount tied to instrument tick size. If the producer can't compute a
valid stop, it should emit no alert at all instead of emitting one with
a broken stop.

**Verify:** after the fix, `stop_not_below_entry` rejections should drop
to 0/week. The rule at line 915 remains as belt-and-suspenders.

### D6. `@validates("scan_pattern_id")` model guard

**Where:** `app/models/trading.py:Trade` class.

**What:**
```python
# f-phase3-stop-bleed D6 — guard against silent reintroduction of
# no_pattern trade rows. The 2026-05-15 audit found 210 such rows
# accounting for $1,560 of realized losses; they were legacy cleanup.
# Prevent regression by refusing INSERTs that lack pattern attribution
# unless the source is a known reconcile-import path.
_RECONCILE_IMPORT_SOURCES = frozenset({"reconcile_import", "manual"})
_NO_PATTERN_SENTINEL = -1

@validates("scan_pattern_id")
def _validate_scan_pattern_id(self, key, value):
    # Allow NULL only for explicit reconcile-import or manual sources.
    # Otherwise require either a real pattern or the sentinel.
    if value is None:
        bs = (getattr(self, "broker_source", None) or "").lower()
        # During before_insert the broker_source may not be set yet -- be
        # defensive: only enforce when both fields are known.
        if bs and bs not in _RECONCILE_IMPORT_SOURCES:
            raise ValueError(
                f"trade_anomaly: scan_pattern_id IS NULL with "
                f"broker_source={bs!r}; expected pattern attribution "
                f"or sentinel ({_NO_PATTERN_SENTINEL}). "
                f"f-phase3-stop-bleed D6"
            )
    return value
```

**Caveat:** if `broker_source` is set AFTER `scan_pattern_id` during insert,
this guard fires falsely. CC should verify the insert order via a
SQLAlchemy `before_insert` event listener if `@validates` proves
unreliable.

### D7. BNB-USD zombie row cleanup (BNB-USD only — CRDL stays open)

**Where:** `app/migrations.py` — add new migration `_migration_243_*`.

**What:**
```sql
-- f-phase3-stop-bleed D7: clean up the BNB-USD zombie row id=1861
-- (qty=0, entry=$680.46) which the 2026-05-15 audit flagged as a
-- still-open no_pattern position with $0 notional. Sterile -- not
-- real exposure -- just needs to clear the still-open count.
UPDATE trading_trades
   SET status = 'cancelled',
       exit_reason = 'zombie_cleanup_2026_05_15_phase3',
       exit_date = now()
 WHERE id = 1861
   AND ticker = 'BNB-USD'
   AND status IN ('open', 'working')
   AND COALESCE(filled_quantity, quantity, 0) = 0;
```

**Hard constraints:**
1. All four WHERE clauses must hold; do not run this if
   `filled_quantity > 0` (= row is not actually a zombie). The id=1861
   may have shifted between brief-write and CC-run; CC must verify by
   reading the row before applying.
2. **CRDL (id=1814, $289 notional) is NOT touched.** Operator decision
   2026-05-15: CRDL stays open; CHILI continues to manage the exit via
   the existing bracket stop ($1.1965) and target ($1.7539). The
   missing pattern attribution is a bookkeeping limitation; the exit
   machinery operates on stop/target regardless. Do not add any code
   path that mass-closes no_pattern positions.
3. **D6's @validates fires on attribute SET, not on UPDATE-without-set.**
   Closing CRDL (or any other open no_pattern trade) does not assign
   to `scan_pattern_id`, so the D6 guard does not interfere with the
   existing legacy positions' exit paths. CC must verify this with a
   regression test (D8 includes one).

### D8. Tests

**Where:** `tests/test_phase3_stop_bleed.py` — new file.

**What:** at minimum one test per deliverable.
- D1: drawdown breaker fires when monthly PnL exceeds threshold; does
  not fire when below threshold; respects `chili_monthly_dd_breaker_enabled`
  flag; query scopes to user_id.
- D2: NameError catch produces `coinbase_cap_unavailable:NameError:<name>`
  reason string with the actual unbound identifier.
- D3: malformed product_id raises ValueError before SDK call (mock the
  SDK and assert it was not called).
- D4: insufficient buying power raises before SDK call (mock SDK and
  resolver).
- D5: alert producer emits no alert when stop computation is invalid.
- D6: Trade row with NULL scan_pattern_id and broker_source='robinhood'
  raises; Trade row with NULL scan_pattern_id and broker_source='reconcile_import'
  succeeds.
- D7: migration 243 is idempotent; running twice is safe; doesn't touch
  non-zombie rows.

All tests must pass. `TEST_DATABASE_URL` must end in `_test`.

### D9. Verification — re-run the discovery probe

**Where:** end of the CC run, after all commits land + container restart.

**What:** dispatch `.\scripts\dispatch-audit-discovery.ps1` via daemon or
direct execution. Compare key counts to the 2026-05-15 baseline:

| metric | 2026-05-15 baseline | post-deploy target |
|---|---|---|
| `coinbase_cap_unavailable:NameError` (7d) | 54 | <5, AND each remaining one has `:<name>` suffix |
| `INVALID_ARGUMENT Invalid product_id` (7d) | 48 | 0 |
| `broker:Insufficient balance` (7d) | 830 | <50 |
| `stop_not_below_entry` (7d) | 41 | 0 |
| new no_pattern entries (last 7d at probe time) | 12 | 0 |

The histogram clearing takes 7 days to fully bake (because the window
is 7d). At CC-end immediately after deploy, just verify the system
restarted cleanly and the breaker flag is configurable. Schedule a
follow-up probe 7 days later.

## Hard constraints

- **No hardcoded fallback values** (COWORK_ADVISOR_BRIEF §2.6). D1
  specifically: the threshold is data-driven; if history is short,
  return None and skip — do not pick a magic dollar number.
- One commit per deliverable (D1, D2, ..., D9 each on its own commit).
- All tests pass before final deploy.
- Drawdown breaker flag DEFAULT OFF; operator flips to ON after the
  walk-forward simulation in `docs/STRATEGY/CC_REPORTS/2026-05-15_phase3-stop-bleed.md`
  shows it would have tripped on/around 2026-04-22.
- No alpha-generation code touched (no changes to autotrader entry logic
  beyond adding gates, no changes to pattern miner, no changes to LLM
  cascade).
- TEST_DATABASE_URL must end in `_test`.
- The deliverables are independent; if D5 (upstream producer fix)
  turns out to be high-risk or non-obvious, CC may defer D5 to a separate
  follow-up brief and ship D1-D4, D6-D9 anyway. The rule at
  `auto_trader_rules.py:915` is correctly rejecting the bad orders today.

## Anti-truncation discipline (COWORK_ADVISOR_BRIEF §2.1)

The following files are large and have a history of being silently
truncated by the Edit tool:

- `app/services/trading/portfolio_risk.py` (~1300 lines)
- `app/services/trading/auto_trader.py` (~2500 lines)
- `app/services/trading/venue/coinbase_spot.py` (~1000 lines)
- `app/services/trading/auto_trader_rules.py` (~1000 lines)
- `app/models/trading.py` (~1500 lines)
- `app/migrations.py` (large, grows by migration)

For each of these files: use **Write tool with full overwrite** for any
substantive change, NOT Edit. After every modification, run:
- `wc -l <file>` (verify line count did not unexpectedly drop)
- `git diff --stat -- <file>` (sanity-check delta)
- `python -c "import ast; ast.parse(open('<file>').read())"` (must exit 0)

If `wc -l` reports a drop larger than your edit's net subtraction, STOP
and `git checkout HEAD -- <file>` to restore before proceeding.

## Result

Single CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-15_phase3-stop-bleed.md`
covering all seven deliverables, the walk-forward simulation result for
the drawdown breaker, and the post-deploy histogram (verifying that
short-window counts already showed reduction even before the full 7d
window bakes out).
