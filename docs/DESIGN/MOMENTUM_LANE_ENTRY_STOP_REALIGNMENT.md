# Momentum Lane — Entry/Stop Realignment + Capital-Protection (one coherent initiative)

Status: **DESIGN** · Owner: trading-brain · Created 2026-06-07
Supersedes the entry/stop slices of `docs/DESIGN/MOMENTUM_LANE.md` §3.2-3.3 (M4) and §7.2-7.4.
Direct operator brief (not a Cowork `NEXT_TASK`); position-identity Phase 5I soak is unaffected.

## 0. One-paragraph summary

The momentum lane is **0 wins / 8 losses all-time** and runs **live with real money**
(`CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=1` in `chili-clean-recovery-scheduler`). A
multi-lens, adversarially-verified audit found four coupled real-money defects. The
post-exit shakeout learner labels **every** loss `stop_too_tight=true` and **zero**
`thesis_invalidated` — setups were directionally right; price ran 3-13% to target
**after** the stop. So the dominant fix is **entries + stops**, not selection quality.
This doc designs the fix as **one coherent initiative shipped as a tight ordered series**,
with the live runner staying **on** behind the capital-protection fixes (operator
decisions 2026-06-07). The keystone goes to the **deep cause** (selection→entry
misalignment), not just the retrace formula, and go-live of the entry change is **gated
on a dry-run** that proves the new gate fires at a sane rate on real candidate bars.

## 1. The four findings — verified in code (2026-06-07)

| # | Finding | Verified at | Severity |
|---|---|---|---|
| 1 | **KEYSTONE** — pullback-break entry gate never fires; #485 structural stop is dead code | `entry_gates.py:200` retrace formula; `live_runner.py:1326-1396` trigger + `:1382` structural set / `:1385` pop; same gate in `auto_arm.py:219-243` gates arming too | Critical |
| 2 | Market-order stop exits slip past the stop in wide crypto spreads (no exit-side quote guard) | `live_runner.py:360` pure `place_market_order`; `:800-804` quote gate is entry-states-only | High (~25% of realized loss) |
| 3 | Per-trade caps spike 4-6x transiently from a spiked equity read; oversized trades = ~60% of the halting daily loss | `risk_policy.py:101-143` `_equity_relative_cap`; both loss(0.01) + notional(0.15) caps share `_account_equity_usd`; frozen in `momentum_policy_caps` at admission `risk_policy.py:338-353` | High (turned bleed → halt) |
| 4 | Structural stop encoded as ATR-pct vs `guarded_ask` but re-applied vs realized `avg`; lands at `pullback_low` only when `avg==guarded_ask` | `paper_execution.py:90` `(ep-sp)/ep/mult` with `ep=guarded_ask` (`live_runner.py:1733`); re-applied `live_runner.py:1447` `stop_target_prices(avg, atr_pct=...)` | Medium (latent until #1 fires) |

## 2. Interaction analysis — why piecemeal fails

```
#1 selection→entry misalignment  (KEYSTONE / root)
   lane SELECTS 24h movers (ross_momentum daily-change/RVOL) → faded by entry time
   retrace = (win_high - pb_low)/impulse_range uses the STALE 20-bar window high
   ⇒ gate reads pullback_too_deep ~99.4% of the time (pullback_break_ok = 0.56%)
        ├──► all 74 live entries fell to the momentum_volume fallback
        │      = CHASE on extension (price>EMA9+vol on 15m) — the worst entry
        ├──► structural_stop_price never set ⇒ #4 (#485 structural stop) is DEAD CODE
        └──► fallback uses the noise-tight ATR stop
               ⇒ ALL 8 losses flagged stop_too_tight; price ran 3-13% to target after

#3 cap spike  (independent amplifier — what turned the bleed into a HALT)
   spiked Coinbase equity read 4-6x's BOTH caps (same _account_equity_usd source)
   risk-first qty = max_loss / stop_distance; tight stop (#1) ⇒ sizing demands max qty
   normally clamped by the notional ceiling — but the ceiling spiked too ⇒ clamp released
        ⇒ 2 oversized trades (FIDA sess14 cap 107, KAIO sess25 cap 156) = ~60% of the
          daily loss that tripped Guard 4 and halted the lane

#2 market-exit slippage  (exit-side analog of #1)
   tight stops (#1) get hit often; each exit is a pure market sell with no spread guard
        ⇒ ~$13 (~25% of realized) bled purely from fills below the stop in wide spreads
```

**Coupling that dictates sequencing:**
- **#1 is the keystone** — it unblocks the structural stop (makes #4 *live*) and fixes
  entry quality. It is also the riskiest (it changes *when/where* real money enters).
- **#4 must ship with #1's structural path** — otherwise the now-firing structural stops
  land at the wrong level under slippage.
- **#3 is independent and the highest safety-value** — a spiked equity read inflates the
  whole risk envelope regardless of entry logic; it must be neutralized **first** so the
  runner can stay live while we rework entries (and so the halt mode can't recur).
- **#2 is independent and protects every genuine stop-out** — ship early; it also reduces
  the damage of #1's tight stops while #1 is still being reworked.

## 3. The coherent initiative (ordered series, runner stays live)

Operator decisions 2026-06-07: **(a)** design the whole thing first, ship as a tight
ordered series; **(b)** keystone = retrace-formula fix **AND** selection→entry alignment,
gated on a dry-run; **(c)** keep the live runner **on** behind the safety fixes.

Execution order is **safety → measurement → entry/stop redesign**, because the runner
stays live: protect capital first, then instrument, then change entry behavior under
measurement. Each step is one logical commit/PR (git workflow: sync → branch from latest
`origin/main` → change → test → commit → push → PR).

### ME-1 — Capital protection #3: bound the per-trade caps (ship first)
- **Root**: clamp the *equity input* in `_account_equity_usd` to a bounded multiple of its
  **rolling median** before it derives either cap — fixing both loss (0.01) and notional
  (0.15) caps coherently at the single shared source, rather than clamping two correlated
  caps independently. (Flagged choice — the audit's literal wording was "clamp the
  per-trade cap"; clamping the shared source is strictly stronger and avoids
  double-clamping correlated caps. Equivalent guard also applied to the resolved cap as a
  belt-and-suspenders.)
- **Rolling median source**: recent `TradingAutomationSession.risk_snapshot_json`
  equity/cap reads (DB-backed, survives restart); fall back to the documented fixed cap
  when history is too thin (never size against an unverifiable spike).
- **No-magic**: ONE documented knob = the allowed multiple of the rolling median
  (`chili_momentum_risk_cap_max_median_multiple`, default e.g. 2.0). Everything else is
  derived from observed history. Reference point is a FLOOR/ceiling, not a magic cap.
- **Logging**: `[momentum_neural]` line logging the derivation inputs on every admission:
  raw equity, rolling median, fraction, resulting cap, clamped? (so a future spike is
  visible, not silent).
- **Acceptance**: a synthetic 5x equity spike produces a cap ≤ `multiple × median`, not 5x;
  derivation line present in logs; existing sizing tests still pass.

### ME-2 — Capital protection #2: protected-limit stop exits + slippage audit
- Replace the pure `place_market_order` in `_submit_live_market_exit` with a **marketable
  limit** (a few bps **through** the bid) via the existing `place_limit_order_gtc`, with a
  bounded reprice/fallback so a fast-moving exit still completes (protect against, not
  prevent, the exit). Reuse the existing adaptive spread tolerance
  (`_adaptive_live_max_spread_bps`) to derive "a few bps" — **no magic**.
- **Audit**: record realized exit fill vs intended stop (`exit_vs_stop` bps) on every
  stop-out, mirroring the entry-slip TCA, so exit slippage is measured not assumed.
- **Acceptance**: stop exits route as protected limits; an exit-slippage row is recorded
  per stop-out; a wide-spread test asserts the exit does not submit a naked market order.

### ME-3 — Telemetry + dry-run harness (the measurement gate for ME-4)
- Add counters in `pullback_break_confirmation` (wrapper, not the pure fn) tallying
  `pullback_break_ok` vs **each** rejection reason (`pullback_too_deep`,
  `pullback_below_ema9`, `waiting_for_break`, `break_low_volume`, `no_range`,
  `insufficient_bars`), surfaced via `telemetry.py` / `[momentum_neural]` logs.
- A read-only dry-run script that sweeps the gate (old vs new formula) over recent bars for
  the **current live candidates** and prints the fire-rate histogram. This is the gate that
  must show a sane fire rate **before** ME-4 trades.
- **Acceptance**: histogram reproduces the audit's ~0.56% old-formula rate on real bars,
  giving a measured baseline to beat.

### ME-4 — Keystone entry redesign #1 + stop fidelity #4 (shipped together, gated on ME-3)
- **Retrace formula** (`entry_gates.pullback_break_confirmation`): measure pullback depth
  against the **most-recent up-impulse leg** — find the local swing low that starts the
  current impulse and normalize `(impulse_high - pb_low)` by **that** leg's range, not the
  stale 20-bar window high. Shallow = retraced < threshold of the *current* leg.
- **Selection→entry alignment**: the lane must enter on an intraday pullback of a move
  happening **NOW** — require the candidate's impulse to be **recent** (impulse high within
  N bars) so faded 24h gainers don't reach the entry gate. Tie arming
  (`auto_arm._entry_trigger_fires`) and the live trigger to the same intraday-freshness
  check (both call the same gate today, so one fix covers both).
- **Stop fidelity #4**: when `entry_stop_model == 'structural_pullback'`, recompute the
  placed stop **directly** from `avg` and the persisted `le['structural_stop_price']`
  (place at `pullback_low` − buffer, absolute level, not an ATR-pct re-applied to `avg`);
  re-derive the target off the actual `avg→stop` distance × R:R. Keep the vol-floor as the
  shake-out guard (never tighter than the floor).
- **No-magic**: thresholds stay percentile/derived; reference points are FLOORS the learner
  can raise; the only new documented knobs are the impulse-leg lookback and recency window,
  both single documented settings.
- **Acceptance**:
  - ME-3 histogram shows the new gate firing at a **sane, materially higher** rate on real
    candidate bars (target validated before merge — not a guess).
  - A live entry fires via `pullback_break_ok` with a non-NULL `structural_stop`.
  - **#4 unit test**: with `guarded_ask != avg`, the placed stop is within tolerance of
    `pullback_low` (asserts the drift bug is fixed).
  - Parity: paper vs live gate produce identical decisions on identical bars
    (`tests/test_entry_feature_parity.py` pattern).

## 4. Safety posture (runner stays live)

- Live runner stays `ON`. ME-1 + ME-2 land **before** ME-4 so capital is bounded while the
  entry path is still the old chase. Guard 4 (daily-loss breaker) + kill switch unchanged.
- ME-4 changes entry behavior only after ME-3 proves the new gate fires sanely — no blind
  flip of where real money enters.
- No change to: prediction-mirror authority (Hard Rule 5), broker reconciliation, the
  kill-switch/drawdown/lane-cap safety stack, or the position-identity refactor surfaces.

## 5. Rollback

Each step is an independent commit/PR; `git revert` of any one is safe.
- ME-1: revert restores the unclamped equity read (mig-free; settings-only knob).
- ME-2: revert restores market-order exits.
- ME-3: pure-additive telemetry/script; revert is harmless.
- ME-4: revert restores the old retrace formula + ATR-re-applied stop; the structural path
  simply goes dormant again (its pre-fix state). No schema change.

## 6. Open questions / new documented knobs (flag if any feels like a magic number)

- `chili_momentum_risk_cap_max_median_multiple` (ME-1) — allowed multiple of the rolling
  median cap. Default proposed 2.0; operator to confirm the value/derivation source.
- Impulse-leg lookback + impulse-recency window (ME-4) — proposed as bar counts derived
  from the entry interval; operator to confirm they shouldn't be percentile-derived instead.
- ME-1 root-vs-belt choice: clamp the shared equity source (recommended) vs each resolved
  cap. Flagged in §3 ME-1.
```
