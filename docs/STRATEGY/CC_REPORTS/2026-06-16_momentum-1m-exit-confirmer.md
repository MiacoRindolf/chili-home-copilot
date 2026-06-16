# CC_REPORT: momentum-1m-exit-confirmer

Direct operator task (not from NEXT_TASK.md — that file is still the stale Phase 5I
position-identity soak and was left untouched). Add a **1m candle exhaustion
confirmer** as one more AND-gated input into the existing `ofi_exhaustion_lock`
confluence, observe-first, evidence-gated against the live LNAI sessions.

## What shipped

Branch `worktree-chili+momentum-1m-exit-confirmer`, **based on latest `origin/main`
(#762)** — the working branch `chili/momentum-concurrency-basis-independent` predates
#703/#704 (the lock didn't exist there), so I built in an isolated worktree off main.

Files touched (6 + this report):
- `app/services/trading/momentum_neural/candles.py` — new pure helper
  `macd_hist_rollover_from_df` (+ `_ema`/`_closes_from_df`); pandas-free, fail-safe.
- `app/services/trading/momentum_neural/paper_execution.py` — `ofi_exhaustion_lock`
  gains `candle_exhaustion`/`candle_gate_live` params + the AND-gate on the FLOW
  confluence + `candle_ok`/`candle_would_suppress` A/B fields.
- `app/services/trading/momentum_neural/live_runner.py` — STATE_LIVE_TRAILING block
  fetches a **cached (once/min/session) 1m df** (mirrors the 5m-EMA cache), computes
  `topping_tail OR macd_rollover`, passes it to the lock, and emits the candle A/B.
- `app/config.py` — 3 flags: `..._CANDLE_CONFIRM_ENABLED` (default True, kill-switch),
  `..._CANDLE_CONFIRM_LIVE` (default **False**, observe-first), `..._CANDLE_CONFIRM_USE_MACD`
  (default True).
- `docs/DESIGN/ADAPTIVE_OFI_EXIT.md` — new "1m-candle exhaustion confirmer" section.
- `tests/test_candles.py` + `tests/test_ofi_exhaustion_lock.py` — new coverage.

No migrations. No change to `_maybe_event_exit_hint` (the WS-receive-thread path — left
strictly alone per the brief; the fetch lives in the poll path that already does I/O).

## Design (why it's safe)

- **AND-gate only** ⇒ it can only ever SUPPRESS a flow fire whose 1m candle shows no
  exhaustion; it can NEVER manufacture a new fire (can't sell a winner the lock
  wouldn't already). The absorption OR-bypass (leading signal) is intentionally not
  candle-gated.
- **Fail-OPEN**: missing/thin 1m df ⇒ `candle_ok=True` ⇒ no restriction ⇒ existing
  captures untouched.
- **INVARIANT A** preserved: the gate only blocks a fire, never lowers a stop.
- **Observe-first**: with `_LIVE=False` (default) the live lock decision is
  **byte-identical**; only `candle_would_suppress` (the A/B) is recorded. Equity +
  crypto identical (same `fetch_ohlcv_df`).

## Verification

- `pytest tests/test_candles.py tests/test_ofi_exhaustion_lock.py` → **61 passed**
  (12 new: 5 MACD-helper + 7 candle-gate, incl. byte-identical default, live
  suppression, capture-preserve, fail-open, never-creates-a-fire, absorption-not-gated).
- `pytest tests/test_ofi_exhaustion_lock_live.py` → **3 passed** (incl.
  `test_equity_armed_flag_is_byte_identical_no_ofi_partial` — equity parity holds).
- All 4 edited modules AST-parse; Settings() constructs; helper functional checks pass.

### Live evidence — LNAI 5170 / 5192 / 5204 (live equity, 2026-06-16)

Pulled `live_ofi_exhaustion_lock` events + entry/exit fills from
`trading_automation_events`, then fetched the actual LNAI 1m bars and evaluated the
candidate confirmer at each lock-fire minute:

| Session | Lock | Fire bar (1m) | topping_tail | macd_roll | **confirmer** | Outcome |
|---|---|---|---|---|---|---|
| 5170 | FIRED | 20:53 `H4.33→C4.04` uw=0.61 | **True** | False | **AGREE** | exit 4.0518 vs band-cf 3.976 |
| 5204 | FIRED | 21:13 `H6.16/C6.12` uw=0.10 | False | **True** | **AGREE** | exit 5.8997 vs band-cf 5.70 |
| 5192 | never | — (micro stayed +39) | — | — | n/a | ran +2.3R, scaled out at target |

- The confirmer **AGREED with 2/2 live fires** ⇒ **zero capture regression** if flipped
  live on this sample.
- **MACD is load-bearing**: 5204's real top had no dominant wick — caught **only** by the
  MACD rollover. Wick-only would have wrongly suppressed a good capture ⇒ keep USE_MACD on.
- The confirmer is genuinely discriminating (~half the surrounding bars were False), not a
  rubber stamp.

## Surprises / deviations

- **Wrong-base branch.** The task's line numbers (paper_execution.py:524,
  live_runner.py:4014) and `live_runner_loop.py` only exist on `origin/main`, not the
  checked-out `chili/momentum-concurrency-basis-independent` (HEAD predates #703/#704).
  Built off `origin/main` in a worktree instead.
- **"Adds capture" is not yet provable from these 3 sessions** — no fire had a
  *non-confirming* candle, so the suppression benefit (filtering noisy-OFI early-sells)
  has no positive instance here. This is exactly why it ships **observe-first**: the
  `candle_would_suppress` A/B will accumulate those ticks; flip `_LIVE=true` only once it
  shows the would-suppress fires were recoveries, not real tops.

## Deferred

- Flipping `CHILI_MOMENTUM_EXIT_CANDLE_CONFIRM_LIVE=true` — gated on the observe-first A/B
  showing net-positive (the would-suppress fires being early-sells). Low-regret to flip
  later (AND-gate, INVARIANT A), but no evidence yet that it *helps*, only that it doesn't
  *hurt*.
- 5170's exit at 4.05 was itself followed by a recovery to 4.75+ — i.e. the *existing*
  lock early-sold there, and the 1m candle (a real topping-tail) AGREED, so this confirmer
  does not fix that case. A future lever (not this task): a longer/structural exhaustion
  read that distinguishes a 1-bar rejection from a true top.

## Open questions for Cowork

- Combination logic is `topping_tail OR macd_roll` (either 1m exhaustion sign). Prefer
  AND (stricter, fewer suppressions) once we have more armed-tick data? Currently OR keeps
  it conservative (rarely blocks) — the safe default for a first ship.
- Should the candle gate eventually also gate the absorption OR-bypass? Left ungated now
  (absorption is the one leading signal + off by default).
