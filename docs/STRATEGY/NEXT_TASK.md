# NEXT_TASK: f-fastpath-universe-rotation

STATUS: DONE

**Promoted 2026-05-07 after empirical research confirmed the current 5-pair fast-path universe is the wrong end of the liquidity spectrum.** Companion brief `f-fastpath-maker-only` is queued as the immediate follow-up and is the prerequisite for live activation — universe rotation alone does not clear Coinbase taker round-trip cost, but it surfaces the pairs that will clear maker round-trip once that mode ships.

## Why now

24h replay across 20 Coinbase pairs (5 control + 15 mid-tier treatment) found:

| Group | 5m fwd-return | 15m fwd-return |
|---|---|---|
| **Current 5** (BTC/ETH/SOL/AVAX/DOGE) | **−0.80 bps** | −0.67 bps |
| **Mid-tier 15** (rank 5–30 by composite) | **+0.48 bps** | **+4.14 bps** |

Top pairs by realized 5m+15m Sharpe: **RENDER** (5m_Sharpe=2.08), **ICP** (5m=+6.13, 15m=+12.46), **ARB** (+4.17 / +7.70), **INJ** (+4.12 / +6.81), **TAO** (+2.55 / +2.48), **FET** (+3.24).

Current universe contains pairs that should be dropped:
- **AVAX-USD** rank 27 with 10.5 bps spread — already past the uneconomic line.
- **DOGE-USD** is not even in the live universe (zero recent 24h volume / bid==ask in this window).
- **SOL/ETH/SUI** are anti-predictive in the replay window.

Full writeup: `docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md`. Raw output: `scripts/research-fastpath-universe-2026-05-07-output.txt`. Brief: `docs/STRATEGY/QUEUED/f-fastpath-universe-rotation.md`.

## Goal

Replace the hardcoded 5-pair list (`settings.FAST_PATH_TICKERS`) with a **data-driven mid-tier rotation** read from a new `fast_path_universe` table, populated hourly by a new `universe_rotator` job. Add a **cost-aware admission gate** in `fast_path/gates.py`. Ship in **shadow mode** (paper-only on new pairs, no live placement changes). Soak 48h. End state: the operator can answer "is there ANY (ticker, alert_type, score_bucket) cell in `fast_signal_decay` that clears `2 × cost`?" with real data on the right universe.

## Acceptance criteria

1. `fast_path_universe` table exists (mig 230) and is populated by the rotator.
2. `universe_rotator` job runs hourly via the existing scheduler, scoring by `composite = volume_24h_usd / max(spread_bps, 0.5)`, applying the four gates (volume ≥ $10M, spread ≤ 10 bps, top-of-book size ≥ $5k, trades_24h ≥ 1k), writing the top-25 to `fast_path_universe(status='active')`.
3. `ws_client.py` subscribes from `fast_path_universe WHERE status='active'` instead of `settings.FAST_PATH_TICKERS`. The 5-pair fallback list remains as a hard floor in case the table is empty.
4. `gate_cost_aware_admission` rejects any signal whose `mean_return < 2 × (taker_fee_bps + median_spread_bps_for_ticker)` at the best-Sharpe horizon. Logged in `fast_executions.gates_json`.
5. Hysteresis on rotation: a pair must drop out of top-N by **≥ 3 ranks** to be demoted (avoids subscription churn).
6. Cold-start carve-out: new pair entries go to `status='shadow'` for the first 24h while `decay_miner` accumulates `fast_signal_decay` rows; no executor admission until promoted to `active`.
7. Status endpoint `GET /api/trading/fast-path/universe` returns the active set + last 24h of rotations + the metadata snapshot.
8. After a 48h shadow soak, at least one new pair has `sample_count ≥ 30` in `fast_signal_decay` for at least one (alert_type, score_bucket).
9. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-07_fastpath-universe-rotation.md` per PROTOCOL format.
10. Tests: `tests/test_fastpath_universe_rotator.py` + `tests/test_fastpath_cost_aware_gate.py`. Pass against `chili_test`.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/fast_path/calibration.py` → already reads `fast_signal_decay`. The cost-aware gate should call into it via the existing helpers, not duplicate decay-table reads.
- `app/services/trading/fast_path/gates.py` → add the new gate alongside `gate_calibrated_tradeability`; reuse the same `(ticker, alert_type, score_bucket, horizon_s)` cell-lookup pattern.
- `app/services/trading/fast_path/decay_miner.py` → already does cold-start backfill on subscribe. Reuse for new-pair shadow-warmup.
- `app/services/trading/fast_path/ws_client.py` → already has subscribe/unsubscribe primitives. Add a "diff against active set" caller, don't rewrite the WS lifecycle.
- `app/services/trading_scheduler.py` → register `universe_rotator` as a scheduled job (every 60 min), same pattern as the existing fast-path supervisor jobs.
- `app/migrations.py` → mig 230 follows the `_migration_NNN_*()` convention. Idempotent. Check the most recent `_migration_NNN_` number; do not reuse IDs.

## Constraints / do not touch

- **Hard Rule 1: live-placement safety belts.** Do not flip `CHILI_FAST_PATH_MODE` from `paper`. Do not change the eight belts in `executor.py`. New pairs are paper-only by default; live activation is a separate operator decision after `f-fastpath-maker-only` ships.
- **Frozen contracts:** `fast_signal_decay` schema, `fast_alerts` schema, `fast_executions.gates_json` shape — additive only. Do not break readers.
- **Cold-start data leak:** `decay_miner` cold-start backfill must be sandboxed to the new shadow pair only — must not retroactively touch existing rows for the current 5-pair universe.
- **No magic numbers.** The four admission gate thresholds (volume ≥ $10M, spread ≤ 10 bps, top-of-book ≥ $5k, trades_24h ≥ 1k) come from the brief and the research doc, but they should be **settings-tunable**, not hardcoded inline. Add them to `fast_path/settings.py` with explicit per-knob doc comments.
- **No new manual whitelists.** This is the whole point — no `if ticker == 'BTC-USD'` branches anywhere in the new code. The existing pullback-allowlist (BTC/SOL only, in `gates.py:280`) stays as legacy until a separate brief retires it.
- **Migration constraints (CLAUDE.md Hard Rule 6):** sequential, idempotent, check the last `_migration_NNN_` number, never reuse IDs. Run `.\scripts\verify-migration-ids.ps1` before merge.
- **Tests use `_test`-suffixed DB** (CLAUDE.md Hard Rule 4).

## Out of scope

- **Maker-only execution mode** (`f-fastpath-maker-only`). That's the next brief; it depends on this one. Do not preempt it. Leave `executor.py` alone.
- **Hyperliquid perps** (`f-fastpath-hyperliquid-perps`). Different venue, different rules.
- **Toxic-flow / depth-decay / OFI features** (`f-fastpath-microstructure-features-v2`). Only matter once the cost gate opens. Sequence after maker-only ships.
- **Backfill of `fast_signal_decay` for new pairs from historical bars.** Cold-start shadow window is the right way; backfill could leak data from windows where exits were observed retroactively. Memory entry `reference_2026_05_07_fastpath_universe_research.md` notes this.
- **Removing the legacy 5-pair pullback allowlist** in `gates.py:280`. Separate brief, lower priority.
- **Any change to the 5-pair `fast_path_universe` fallback list.** That's a hard floor for safety, not a bug.

## Sequencing within this task

1. **Migration 230 + table.** Idempotent.
2. **`universe_rotator.py`** with the four-gate composite scoring + hysteresis + cold-start shadow-mode.
3. **Scheduler registration** (60-min cron).
4. **`ws_client.py` subscription read path.**
5. **`gate_cost_aware_admission`** in `gates.py`.
6. **Status endpoint.**
7. **Tests.**
8. **Boot the new path in shadow mode.** New pairs go to `status='shadow'`; only `status='active'` rows get executor admission. Initial admission set is the existing 5 pairs (no behavior change at switchover).
9. **Trigger the rotator manually** to populate `fast_path_universe` with the top-25 mid-tier candidates from the live Coinbase API. Verify in DB.
10. **48h soak observation.** Watch `fast_signal_decay` accumulate rows on new pairs.
11. **CC report** with: rotator's top-25 selection, gate-rejection histogram, decay-row accumulation per new pair, anything surprising.

## Coinbase API specifics (confirmed working from research)

- Public REST base: `https://api.exchange.coinbase.com`
- Rate limit: ~10 req/s per IP. Batch the universe scan with 0.10s pacing.
- Endpoints used:
  - `/products` (list, no auth) — filter to `quote_currency='USD' AND status='online' AND trading_disabled=false`
  - `/products/{id}/stats` (24h volume in base currency)
  - `/products/{id}/ticker` (best bid/ask + last trade)
  - `/products/{id}/candles?granularity=60` (for cold-start backfill if needed; max 300 bars/call)
- WS subscription endpoint already in use; just diff the active set.

## Verdict-grade observations to surface in the CC report

The whole purpose of this work is to answer one question: **which (ticker, alert_type, score_bucket) cell in `fast_signal_decay` clears `2 × cost`?** Phase 1's CC report should:

1. List the rotator's selected top-25 active pairs (with the four gate values).
2. Show the cost-aware gate rejection histogram across the first 6h post-deploy (which (ticker, alert_type) combinations rejected most often).
3. Note any surprising rotation behaviors (pair churn, hysteresis triggers).
4. **Explicitly NOT** decide whether any pair is "live-eligible" — that's `f-fastpath-maker-only`'s call after the next phase ships. Just report the data.

## Rollback plan

- `git revert` the migration commit (mig 230 is purely additive — no destructive ALTERs).
- Set `settings.FAST_PATH_UNIVERSE_ROTATION_ENABLED=False` in compose to restore the 5-pair hardcoded list. The rotator job becomes a no-op; ws_client falls back to `settings.FAST_PATH_TICKERS`.
- Feature flag default: `OFF` for the new gate (`gate_cost_aware_admission` is in the gate list but no-ops if the flag is off). Keeps the existing executor behavior bit-identical at switchover.

## Push & deploy

- One commit per logical step (migration → rotator → ws integration → gate → tests → endpoint), tight series. Don't bundle.
- After commit + push, restart `chili` + `fast-data-worker` to pick up the new rotator + WS subscription path.
- Do NOT restart `autotrader-worker` for this — different lane.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Exact admission-gate thresholds.** Brief proposes ≥ $10M / ≤ 10 bps / ≥ $5k top-of-book / ≥ 1k trades. Surface the actual top-25 pairs that pass; if the cut is too aggressive (< 15 pairs admitted) recommend relaxing one threshold.
2. **Shadow window length.** Brief says 24h. If `fast_signal_decay` accumulates samples faster on a high-flow pair, propose shortening that window.
3. **Hysteresis size.** Brief says ≥ 3 ranks. If observed churn rate is high, suggest 5.
4. **Backfill from historical Coinbase candles?** I argued NO in the research doc (data-leak concern); but if cold-start shadow is genuinely too slow for the operator's iteration pace, raise the question with a specific leakage mitigation.
