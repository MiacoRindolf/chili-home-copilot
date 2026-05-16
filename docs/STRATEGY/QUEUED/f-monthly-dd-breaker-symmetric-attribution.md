# f-monthly-dd-breaker-symmetric-attribution

> ⚡ **TIME-SENSITIVE:** the monthly DD breaker is on track to arm organically around **2026-05-29** (per the daily arming-watch report — currently at n=21 attributed close-days, need 30, rate ~5.44/wk). On arm day, the operator will flip `CHILI_MONTHLY_DD_BREAKER_ENABLED=true`. If this brief hasn't shipped by then, the breaker will trip immediately on losses the threshold mathematically cannot see. Ship before 2026-05-28.

> **Parent:** ARCHITECT-FLAG surfaced in `docs/STRATEGY/CC_REPORTS/2026-05-16_phase3-monthly-dd-breaker-arming-watch.md`.
> **Type:** SQL filter symmetry + small helper extraction + 2 regression tests.
> **Scope:** ~50 LOC across `portfolio_risk.py` + `tests/test_phase3_stop_bleed.py`. Single deliverable, single session.
> **Status:** unblocked.

## Why this is needed

The Phase 3 monthly DD breaker has two arms — a **threshold** (the empirical Gaussian lower-bound on 30-day realized PnL, computed from rolling-180d daily-PnL variance) and a **numerator** (the actual 30-day realized PnL). They currently use different attribution scopes:

| Component | Scope | Filter |
|---|---|---|
| `_monthly_dd_threshold()` (line ~909) | CHILI-attributed | `scan_pattern_id IS NOT NULL AND scan_pattern_id != -1` |
| `check_drawdown_breaker()` numerator SELECT (line ~1088) | **all closed** | none |

Result: a no_pattern bleed (manual / legacy / reconciler-imported trades with NULL or -1 `scan_pattern_id`) pushes the numerator deep negative without widening the threshold's variance estimate. The threshold is calibrated against attributed-day std (~$58/day per the 2026-05-16 arming-watch); the numerator is currently −$1,216 over 30d, dominated by ~$1,560 of no_pattern bleed per the 2026-05-15 quant audit.

**On arm day, the breaker would trip immediately** — monthly_pnl=−$1,216 vs threshold≈−$34 — halting all trading on losses the breaker can't statistically see.

This is a definitional asymmetry one SQL clause wide. Fix is one filter.

## Goal

Make the numerator match the threshold's attribution scope. The breaker becomes "alert when *attributed* strategy is losing more than its historical attributed-day variance suggests." Clean, composable, action-able.

## Design

### Extract helper

Add `_monthly_attributed_pnl(db, user_id) -> float` in `app/services/trading/portfolio_risk.py` immediately after `_monthly_dd_threshold` (line ~970). Body:

```python
def _monthly_attributed_pnl(db: Session, user_id: int | None) -> float:
    """Sum of realized PnL on CHILI-attributed closed trades over the
    trailing 30 days.

    Matches _monthly_dd_threshold's attribution scope
    (scan_pattern_id IS NOT NULL AND scan_pattern_id != -1) so the
    monthly DD breaker's numerator and denominator are calibrated
    against the same population. Without this filter, no_pattern bleed
    inflates the numerator without widening the threshold's variance
    estimate -- tripping the breaker on losses the threshold cannot
    statistically see.

    See ARCHITECT-FLAG in 2026-05-16_phase3-monthly-dd-breaker-arming-watch.md.
    """
    from sqlalchemy import text
    result = db.execute(
        text(
            \"\"\"
            SELECT COALESCE(SUM(pnl), 0)::float
              FROM trading_trades
             WHERE user_id = :uid
               AND status = 'closed'
               AND pnl IS NOT NULL
               AND scan_pattern_id IS NOT NULL
               AND scan_pattern_id != -1
               AND COALESCE(exit_date, last_fill_at, filled_at)
                   >= now() - interval '30 days'
            \"\"\"
        ),
        {"uid": user_id},
    ).scalar()
    return float(result or 0.0)
```

### Swap the inline SQL

In `check_drawdown_breaker` (line ~1088), replace:

```python
from sqlalchemy import text as _text_dd
monthly_pnl = db.execute(
    _text_dd("""SELECT COALESCE(SUM(pnl), 0)::float
                FROM trading_trades
                WHERE user_id = :uid AND status = 'closed' AND pnl IS NOT NULL
                  AND COALESCE(exit_date, last_fill_at, filled_at)
                      >= now() - interval '30 days'"""),
    {"uid": user_id},
).scalar() or 0.0
```

with:

```python
# Attribution-symmetric numerator: same scope as the threshold
# (scan_pattern_id IS NOT NULL AND != -1). Without this, no_pattern
# bleed pushes the numerator down without widening the threshold's
# variance estimate -- see ARCHITECT-FLAG in
# 2026-05-16_phase3-monthly-dd-breaker-arming-watch.md.
monthly_pnl = _monthly_attributed_pnl(db, user_id)
```

Also update the log-message in the trip-reason (~line 1107) to make the scope explicit:

```python
_breaker_reason = (
    f"monthly_dd_breaker: 30-day CHILI-attributed realized PnL "
    f"${float(monthly_pnl):.2f} <= empirical Gaussian "
    f"lower-bound ${float(threshold):.2f} "
    f"(K={k_val}σ, computed from {n_obs}d CHILI history)"
)
```

(One word: `realized` → `CHILI-attributed realized`.)

### Regression tests

In `tests/test_phase3_stop_bleed.py::TestD1MonthlyDdBreaker`, add two tests:

**Test 1 — helper-level:**

```python
def test_numerator_filters_no_pattern_matching_threshold_scope(self, db):
    """ARCHITECT-FLAG fix 2026-05-16: numerator must filter scan_pattern_id
    just like the threshold. Seeds 30 attributed days at +$10/day plus a
    raw-SQL-inserted -$2,000 no_pattern row. Asserts:
      - _monthly_attributed_pnl returns +$300 (attributed only)
      - unfiltered SUM(pnl) returns -$1,700 (proves the asymmetry existed)
    """
    from sqlalchemy import text
    uid = _seed_user(db, user_id=998)
    for d in range(30):
        _seed_chili_attributed_trade(db, user_id=uid, pnl=10.0, days_ago=d + 1)
    # Bypass @validates("scan_pattern_id") via raw INSERT — no_pattern + NULL
    db.execute(text("""
        INSERT INTO trading_trades (
            user_id, ticker, direction, entry_price, exit_price, quantity,
            entry_date, exit_date, last_fill_at, filled_at, status, pnl,
            broker_source, scan_pattern_id, auto_trader_version
        ) VALUES (
            :uid, 'NPL', 'long', 10.0, 10.0, 1.0,
            NOW() - INTERVAL '2 days', NOW() - INTERVAL '1 day',
            NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day',
            'closed', -2000.0, 'manual', NULL, NULL
        )
    """), {"uid": uid})
    db.flush()
    attributed_pnl = _monthly_attributed_pnl(db, uid)
    assert attributed_pnl == pytest.approx(300.0, abs=1.0), (
        f"numerator should be attributed-only +$300, got ${attributed_pnl:.2f}"
    )
    # Sanity-check the asymmetry the fix closes
    unfiltered = db.execute(text("""
        SELECT COALESCE(SUM(pnl), 0)::float FROM trading_trades
        WHERE user_id = :uid AND status = 'closed' AND pnl IS NOT NULL
          AND COALESCE(exit_date, last_fill_at, filled_at)
              >= NOW() - INTERVAL '30 days'
    """), {"uid": uid}).scalar()
    assert float(unfiltered) == pytest.approx(-1700.0, abs=1.0)
```

**Test 2 — end-to-end:**

```python
def test_breaker_no_trip_on_no_pattern_bleed_when_flag_on(
    self, db, monkeypatch,
):
    """With flag ON, a -$1,000 no_pattern bleed alongside 35 +$10/day
    attributed days must NOT trip the monthly_dd path."""
    from sqlalchemy import text
    from app import config as app_config
    uid = _seed_user(db, user_id=997)
    for d in range(35):
        _seed_chili_attributed_trade(db, user_id=uid, pnl=10.0, days_ago=d + 1)
    db.execute(text("""
        INSERT INTO trading_trades (
            user_id, ticker, direction, entry_price, exit_price, quantity,
            entry_date, exit_date, last_fill_at, filled_at, status, pnl,
            broker_source, scan_pattern_id, auto_trader_version
        ) VALUES (
            :uid, 'NPL', 'long', 10.0, 10.0, 1.0,
            NOW() - INTERVAL '2 days', NOW() - INTERVAL '1 day',
            NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day',
            'closed', -1000.0, 'manual', NULL, NULL
        )
    """), {"uid": uid})
    db.flush()
    monkeypatch.setattr(
        app_config.settings, "chili_monthly_dd_breaker_enabled", True,
    )
    tripped, reason = check_drawdown_breaker(db, uid, capital=1_000_000.0)
    # The monthly_dd path must NOT trip. Other 5d/30d %-of-capital checks
    # use a different scope ("autotrader-placed") and might fire on the
    # -$1,000 row; the monthly_dd reason has a distinctive prefix.
    if tripped and reason:
        assert "monthly_dd_breaker" not in reason
```

Don't forget to update the imports at the top of the test file:

```python
from app.services.trading.portfolio_risk import (
    _monthly_attributed_pnl,   # NEW
    _monthly_dd_threshold,
    check_drawdown_breaker,
)
```

## Deliverables

D1. **`app/services/trading/portfolio_risk.py`** — extract `_monthly_attributed_pnl` helper, swap inline SQL in `check_drawdown_breaker`, update log message. ~40 LOC additive.

D2. **`tests/test_phase3_stop_bleed.py`** — add the two regression tests + import update. ~100 LOC additive.

D3. **Anti-truncation discipline:** after every Edit/Write on `portfolio_risk.py`, run `wc -l` + `git diff --stat -- app/services/trading/portfolio_risk.py` + `python -c "import ast; ast.parse(open('app/services/trading/portfolio_risk.py').read())"`. The file is ~1408 lines at HEAD; if post-edit count is outside [1440, 1490], STOP. Previous Cowork attempt on this exact file truncated mid-function.

D4. **Run only the new tests + the full TestD1MonthlyDdBreaker class:**

```
TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
pytest tests/test_phase3_stop_bleed.py::TestD1MonthlyDdBreaker -v
```

All existing D1 tests plus the two new ones must pass. The arming-watch behavior must not regress.

D5. **Two commits** (one per deliverable) and push:
- `fix(portfolio_risk): monthly_dd_breaker numerator-attribution symmetry`
- `test(phase3_stop_bleed): D1 attribution-symmetry regression tests`

## Hard constraints

- **No magic-default fallbacks** (advisor §2.6). The new helper returns `0.0` only when SUM(pnl) is genuinely zero/NULL — same as the threshold helper's behavior.
- **Default flag state unchanged.** `chili_monthly_dd_breaker_enabled` stays `False`. This fix makes arm day safe; it does not flip the flag.
- **No autotrader / venue / broker code touched.**
- **TEST_DATABASE_URL ends in `_test`.**
- **Anti-truncation discipline strict.** Two prior attempts to edit `portfolio_risk.py` mid-conversation truncated the file silently while ast.parse passed transiently. If post-edit line count is unexpected, restore from HEAD and try with smaller anchor.
- **The `auto_trader_version` field on the seeded test trades stays untouched** — the test file already sets it via `_seed_chili_attributed_trade`. The no_pattern test rows insert it as `NULL` deliberately (no_pattern ≠ autotrader-placed in this synthetic scenario).

## Acceptance

- Two new commits in HEAD; pushed to `origin/main`.
- All existing `TestD1MonthlyDdBreaker` tests still pass.
- Two new tests pass.
- HEAD on `app/services/trading/portfolio_risk.py` shows `_monthly_attributed_pnl` defined and called from `check_drawdown_breaker`.
- Manual probe against prod DB: `_monthly_attributed_pnl(db, user_id_1)` returns a value that excludes the ~$1,200 no_pattern bleed. (Expect roughly +$300 to +$500 based on the 2026-05-15 attributed cohort.)

## Operator activation after this ships

The arming watch already runs Mondays at 9am (or per ad-hoc dispatch). Once this fix is in HEAD AND the arming watch reports n≥30 attributed close-days (~2026-05-29 projected):

1. Read the arming-watch report — confirm threshold value is sane (expect roughly −$30 to −$100, K=2σ).
2. Confirm `_monthly_attributed_pnl(db, user_id)` returns a value above the threshold (no immediate-trip condition).
3. Flip `CHILI_MONTHLY_DD_BREAKER_ENABLED=true` in `.env`.
4. `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker`.
5. Monitor for 1 week — the breaker should not trip under normal operating conditions.

## Out of scope (future briefs)

- **Symmetric scope across all DD checks.** `check_drawdown_breaker` has three different population scopes today (5d/30d %-of-capital uses autotrader-placed via `_breaker_trade_filter`; monthly DD threshold uses pattern-attributed; monthly DD numerator pre-fix was all-closed). After this fix, monthly DD becomes self-consistent at pattern-attributed scope, but the 5d/30d still differ. Unification is a separate brief — call it `f-breaker-scope-unification`.
- **Statistically-derived 5d/30d limits.** The 5d/30d %-of-capital limits are operator-tuned constants, not data-driven. A future brief could apply the same Gaussian-lower-bound methodology to those horizons.
- **No_pattern drainage at the source.** The composite-reweight work (commits c4cf1ba → 7799545) throttled no_pattern alert generation. The historical 30-day bleed window will roll off organically. No additional action required.
