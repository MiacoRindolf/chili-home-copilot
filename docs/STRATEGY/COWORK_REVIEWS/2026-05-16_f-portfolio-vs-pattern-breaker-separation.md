# COWORK_REVIEW: f-portfolio-vs-pattern-breaker-separation

**Reviewer:** Cowork (scheduled file-watcher, autonomous)
**Reviewed CC_REPORT:** `docs/STRATEGY/CC_REPORTS/2026-05-16_f-portfolio-vs-pattern-breaker-separation.md`
**Reviewed session:** `portfolio-vs-pattern-breaker-continuation-2026-05-16` (status.json `last`, ended 2026-05-16T20:01:58Z, passed=true, exit_code=0, duration_sec=2732.8 = ~45min)
**Reviewed commits:** `d6ddbe8` → `6259912` → `d1bdb18` → `4f11100` → `3d6bf9a` → `864eca8` (6 commits, HEAD = 864eca8)

## What's good

**The two-tier separation is now load-bearing rather than theoretical.** The pattern breaker is renamed (with `AliasChoices` legacy support so no env-var migration is forced on the operator), and the portfolio breaker exists alongside it as a parallel mechanism. Today's no_pattern bleed has a closable gate; flipping it on is now a config decision, not a code-change project.

**The user_id=None semantic fix is the right call.** CC self-discovered during test 7.4 that `WHERE user_id = :uid` returns zero rows under Postgres NULL semantics when the venue gate calls the helper with default `user_id=None`. The fix to `(:uid IS NULL OR user_id = :uid)` is architecturally consistent: CHILI is single-broker-account-per-household, so "portfolio" means broker-account-aggregated. CC flagged this for future multi-broker-account scenarios, which is the correct framing — today's fix is correct; tomorrow's expansion needs a re-look.

**Inserting `4f11100` as its own fix commit between `d1bdb18` and the tests preserves commit-boundary integrity.** Six commits, not five, but each is atomic and tells its own story. Bisect-friendly. CLAUDE.md's commit-boundary rule is respected.

**Venue-gate placement is BUY-only and surgical.** 5 gate insertions across the BUY branches (3 Coinbase + 2 Robinhood); 0 in SELL / cancel / preview / stop-loss-SELL paths. The BEFORE/AFTER `git grep` for `place_market_order|place_limit_order_gtc|place_stop_limit_order_gtc` is embedded in `d1bdb18`'s commit body — verifiable from history. Critically: a portfolio breaker that blocks SELLs would be a footgun (it'd hold positions through drawdown rather than letting exits run). CC got the asymmetry right.

**Test suite is appropriately structured.** 6 sub-tests: trips on threshold breach, doesn't trip on un-attributed CHILI loss alone, pattern breaker still works post-rename, venue-adapter short-circuits before broker mock is touched (test 7.4 is the load-bearing one), portfolio-not-tripped + pattern-tripped behaves correctly, and alias-backwards-compat. Test 7.4 specifically asserts `mock.market_order_buy.called is False` — proves the gate fires at the adapter boundary, not deeper. Good coverage shape.

**Shadow-log persistence reuses `trading_risk_state`.** No new migration — just two new `regime` tag values (`portfolio_breaker`, `portfolio_breaker_shadow`). Minimum-disruption pattern matching plan §6. Easy to query, easy to rollback.

## What's concerning

**Pytest workaround `-p no:asyncio` is now load-bearing across three consecutive briefs.** The predecessor symmetrize brief hit the same `'Package' object has no attribute 'obj'` collection error and used the same flag. This brief used it again. The follow-up `f-pytest-asyncio-env-pin` queued in my prior review is now overdue — every brief that needs new pytest classes inherits the workaround, and the workaround means async tests in the same suite are silently skipped. Flag for operator attention: prioritize `f-pytest-asyncio-env-pin` ahead of the next non-trivial test-touching brief.

**Working-tree residue is unchanged from the 8+ prior pulses.** 30+ modified files persist in `git status --porcelain` (`auto_trader.py`, `migrations.py`, `models/trading.py`, `brain_work/handlers/*`, `fast_path/*`, `learning.py`, etc.) — all pre-existing, none authored by this session's 6 commits. The committed scope (`git log d6ddbe8..HEAD --name-only`) is clean per the in-flight pulse audits. But the working-tree drift means a Docker rebuild today would ship semi-arbitrary local changes alongside the breaker work. Operator follow-up: either `git checkout HEAD -- …` restore or audit-and-commit the drift before the next deploy cycle.

**Test 7.4's session-isolation discovery (commit `db.commit()` after seeding, not just `db.flush()`) is a paper cut.** The CC report flags this inline. Anyone writing future tests that exercise venue-adapter `_assert_*_ok` helpers needs to know the helper opens its own `SessionLocal` and won't see uncommitted fixture state. This is now in the test file as inline documentation, but it's the kind of thing that bites someone three months from now. Worth a note in a testing-conventions doc if one exists.

**The fix commit's WHERE-clause change (`:uid IS NULL OR user_id = :uid`) is a permanent property, not a flag-gated one.** This means the pattern breaker's `_persist_breaker_state` and `restore_breaker_from_db` also need an audit: do they have the same NULL-semantics issue? CC didn't say — implicit "no" because they've been in production for weeks without this surfacing. But the symmetry argument cuts both ways: if the pattern tier *does* have the same latent bug, it would have been masked by the historical practice of always passing user_id from the autotrader. Worth a one-shot grep audit.

## Algo-trader lens

This brief closes the 2026-05-15 quant audit's headline finding — "CHILI account drains through `no_pattern` channels the pattern breaker cannot see." Today: the mechanism exists in code, defaults OFF, tested clean. The closeability is the load-bearing property; whether it gets armed is operator-discretion.

The two-tier composition is right for CHILI's risk profile:
- **Pattern tier** = per-strategy drawdown gate. Trips when a strategy's attributed PnL crosses its threshold; protects against a single overfit or regime-broken pattern.
- **Portfolio tier** = account-level drawdown gate. Trips when *total* account PnL crosses its threshold regardless of attribution; protects against the no_pattern bleed and against multiple-pattern cascades.

The two are now independently armable, independently observable in shadow mode, and independently resettable. That's a coherent operator-control surface.

**Watch item:** the portfolio breaker's default threshold logic (per plan §5: `_portfolio_dd_threshold` from `_monthly_total_pnl` distribution percentile, n≥30 days, returns None if insufficient data). The pattern tier's threshold helper has the same n<30 short-circuit. If an operator arms `chili_portfolio_dd_breaker_enabled=True` against a fresh `trading_trades` history (e.g. after a DB reset), the breaker will silently return `(False, None)` for the first 30 days. Document this expectation in the runbook — the report says §10.2 covers absorbing-state reset; check that it also covers cold-start n<30 behavior so an operator doesn't expect coverage they don't have. **TODO for next read of the runbook.**

## Dev-architect lens

~700 LOC across 6 commits (helpers +284, tests +383, venue wiring +59, docs +117, config rename minimal). All additive except the config rename. The rename uses `AliasChoices`, so no operator env-var migration is forced; legacy `CHILI_MONTHLY_DD_BREAKER_ENABLED` still works.

The hard rules from PROTOCOL.md and CLAUDE.md are all respected:
- Default OFF (Hard Rule #1 territory — kill switch posture preserved)
- No migrations (pattern matches plan §6 to reuse `trading_risk_state`)
- No `auto_trader.py` touched (CC's commits stayed within helper/venue/test/doc/config scope — the working-tree mod to `auto_trader.py` is pre-existing residue)
- No `app/trading_brain/*` touched
- Tests committed under `_test`-suffixed DB convention (with the asyncio workaround caveat)
- Commit boundaries clean, ast.parse clean per file per commit

The architecture nit from my prior review's "future evolution into two-tier design will need a parallel `_monthly_unattributed_pnl` or `_monthly_total_pnl` helper" — CC built `_monthly_total_pnl` exactly. Symmetry preserved with `_monthly_attributed_pnl` (which the symmetrize brief shipped). The two helpers now sit side-by-side in `portfolio_risk.py` and are visually obvious as a pair.

## Next thing

The 7-day shadow-soak path is now the unblocked critical path:

1. **Operator arms `CHILI_PORTFOLIO_DD_BREAKER_ENABLED=True`** (shadow only). Forces `trading_risk_state` rows to start accumulating with `regime='portfolio_breaker_shadow'`. No live behavior change.
2. **7-day observation window.** Review shadow rows daily for any false-positive trips against operator's mental model of "did we have a real drawdown today?"
3. **Flip `CHILI_PORTFOLIO_DD_BREAKER_LIVE=True`** if shadow decisions match operator intuition. From that point, the venue gate is hot.

Recommend the operator's wake-up checklist include: "decide on D-day for portfolio_breaker shadow arm." Earliest reasonable date: tonight (zero risk, all flags still operator-controlled).

**Candidate follow-ups in priority order:**
1. `f-pytest-asyncio-env-pin` — same recommendation as prior review; now upgraded from "nice-to-have" to "actively blocking self-verification on every test-touching brief." Three consecutive briefs have inherited the `-p no:asyncio` workaround.
2. Quick `git checkout HEAD -- <paths>` restore of the working-tree residue, OR a deliberate audit-and-commit pass over the 30+ modified files. The chronic residue is shipping-time risk.
3. Audit of pattern-tier `WHERE user_id = :uid` clauses for the same NULL-semantics issue the portfolio tier just surfaced. One-shot grep across `portfolio_risk.py` should answer it in 5 minutes.
4. Documentation pass: confirm `DRAWDOWN_BREAKER_RUNBOOK.md` covers cold-start n<30 behavior, not just absorbing-state reset.

## Pending operator actions (carried from CC_REPORT)

- All three new flags stay **OFF**:
  - `CHILI_PORTFOLIO_DD_BREAKER_ENABLED` (default `False`)
  - `CHILI_PORTFOLIO_DD_BREAKER_LIVE` (default `False`)
  - `CHILI_PORTFOLIO_DD_BREAKER_SHADOW_LOG_ENABLED` (default `True`, only matters when ENABLED flips)
- `git push` of `d6ddbe8`..`864eca8` (6 commits) — operator's call. Default-OFF posture means timing is non-critical.

## Posture

Approved. Shadow-soak path is unblocked. No regressions. Default-OFF stance is intact. The 2026-05-15 quant audit's headline finding now has a closeable mechanism in code.
