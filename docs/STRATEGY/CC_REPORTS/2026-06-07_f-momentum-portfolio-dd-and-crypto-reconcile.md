# CC_REPORT: f-momentum-portfolio-dd-and-crypto-reconcile

Operator-directed (in-conversation, not the queued NEXT_TASK which is Phase 5I).
Source: 2026-06-07 momentum-lane audit (adversarially verified). All three
findings are momentum_neural / broker-sync surfaces — not the keystone
entry-gate work.

## What shipped

Squash-merged as **`5acdf25`** — "fix(momentum-lane): arm portfolio DD breaker
(Hard Rule 2) + close crypto reconciliation gaps (#508)". 3 focused commits on
branch `chili/momentum-portfolio-dd-and-crypto-reconcile`, 4 files, **0
migrations**.

1. **Portfolio drawdown breaker armed (Hard Rule 2).** `config.py`:
   `chili_portfolio_dd_breaker_enabled` + `_live` defaults `False → True`.
   `momentum_neural/risk_evaluator.evaluate_proposed_momentum_automation`: added
   a `check_portfolio_drawdown_breaker` check alongside the daily-loss cap so the
   authoritative momentum arm path enforces it as a hard block (live) / warn
   (paper) — not only at the fail-open venue gate + auto_arm Guard 3.
2. **Ticker-keyed Coinbase mirror reconcile.** New
   `_reconcile_orphan_coinbase_mirrors()` in `coinbase_service.sync_positions_to_db`:
   closes any `coinbase` `trading_positions` mirror that is `state='open'` but
   absent from the live balance, independent of an open Trade, once it has no
   open Coinbase Trade, no working sell, and is stale past the absent-no-fill
   age. `last_observed_at` staleness is the "absent for N syncs" signal; reuses
   existing settings-derived constants; R32 empty-snapshot guard; watchdog WARN
   for not-yet-closeable rows; `position_event` on close.
3. **Bounded momentum exit-submit retries.** `live_runner._submit_live_market_exit`:
   exponential backoff between broker submits + max-attempts cap that escalates
   to the broker-zero/dust reconcile (→ EXITED) or `LIVE_ERROR`. Knobs:
   `chili_momentum_exit_submit_{max_attempts,backoff_base_seconds,backoff_max_seconds}`.

## Verification

- **Pre-change live verification (chili DB + Coinbase):** breaker flags
  confirmed `False/False` (defaults; no env override; zero `portfolio_breaker*`
  rows ever). History ready: 50 all-closed close-days, 2σ threshold ≈ −$5,115.
  Phantom row 179 FIDA-USD `open` qty 172.64 / Trade 2299 closed `stop_loss_hit`
  / **Coinbase `available=0, hold=0`** (direct read-only fetch). live_runner
  already had the #502/#504 broker-zero reconcile.
- **Tests:** 158 passed (111 breaker/momentum-arm/live-runner +
  47 coinbase-reconcile/broker-zero/exit), 0 failures. `py_compile` clean.
- **Live deploy:** built `chili-app:main-clean-5acdf25`; recreated
  `broker-sync` + `autotrader` (env preserved via full-env-file, only
  CHILI_GIT_COMMIT overridden; old containers renamed `-pre*` for rollback; no
  duplicate workers running).
  - Breaker armed in the running autotrader: `enabled=True live=True sigmas=2.0`.
  - **Issue 2 confirmed live:** first post-deploy CB sync emitted
    `[coinbase_mirror_orphan_reconcile] closed phantom mirror#179 FIDA-USD
    (absent 28411s, no open Trade, no working sell) qty 172.64 -> 0`; row 179 now
    `closed/qty=0`; `position_event(coinbase_mirror_orphan_reconcile, envelope
    2299)` written.
  - autotrader AutoTrader-v1 tick/monitor jobs running clean, no tracebacks.

## Surprises / deviations

- **Audit premise corrected (operator decision taken).** The portfolio tier
  samples ALL closed trades; trailing-30d all-closed PnL is **+$282** — so
  arming will NOT show `would_have_tripped` today. The 0W/15L / −$147.76 is the
  *momentum lane only*, diluted portfolio-wide. The fix still closes a real
  Hard-Rule-2 gap (nothing enforced portfolio DD on any entry path). Operator
  chose **arm shadow + live now** (not shadow-only, not a separate lane-scoped
  guard).
- **Docker engine incident mid-session.** The host's `docker-desktop` WSL2
  distro wedged on the documented orphaned-socket crash
  (`docker-secrets-engine\engine.sock ... cannot be accessed by the system`).
  The auto-recovery watchdog's crash-signature check missed on timing (gentle
  relaunch only). Recovered by: rename orphaned socket dirs (`Docker\run`,
  `docker-secrets-engine`) → `.broken<ts>` + relaunch, then a direct
  `wsl -d docker-desktop -e echo` nudge to boot the still-Stopped distro. Stack
  restored via the watchdog (good-set only). See `[[reference_docker_recovery]]`.

## Deferred

- **No dedicated unit test for `_reconcile_orphan_coinbase_mirrors`** — verified
  by the live integration pass (closed row 179) + the existing coinbase sync
  suite confirming no regression. A focused unit test (mock
  `_coinbase_has_working_sell_orders` + DB fixture) is a worthwhile follow-up.
- **Issue 3 live exercise** — the backoff/cap is deployed + unit-covered, but a
  live wedged-exit was not forced to observe the cap escalation end-to-end.

## Open questions for Cowork

- The momentum lane is 0W/15L / −$147.76 all-time. The portfolio breaker is a
  catastrophe backstop (won't fire on a lane-scoped bleed this small). Do we
  want a **lane-scoped** drawdown/quality kill for momentum_neural (it already
  has a daily-loss cap), or is the strategy-side work (why 0 wins) the right
  lever? Flagged, not actioned — out of this audit's scope.
