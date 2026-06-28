# L2 / Time-and-Sales as a PRIMARY signal — design

**Status:** design (read-only pass, 2026-06-26). No code written yet. Operator scoping pending.
**Source:** SS101 #029 (Ross: *"I would not be able to trade my strategy if it were not for using level two"*) + the live CHILI L2 stack.

## Goal
Graduate L2/T&S from its current **tilt + veto** role to a **confirmer**, then (conditionally) a guarded **trigger** — the way Ross enters off the tape, not just off the chart.

## What's ALREADY live (do NOT rebuild)
- **OFI** (Cont/Stoikov, `pipeline._compute_ofi_micro`) + **micro-price edge** + top-of-book **book_imbalance** → a small agreement-guarded **SELECTION tilt** (`CHILI_MOMENTUM_OFI_TILT_WEIGHT`).
- **`_l2_entry_veto`** (`entry_gates.py:1047`) — hidden-seller / big-resting-ask **VETO** (self-relative `depth_imbal_pctile<=0.15` + absorption shape; fail-open on `n_snaps<=0`).
- **`_entry_flow_veto`** at the live entry seam (`live_runner.py:5928-5961`) off FRESH `_live_ofi_microprice` / `_live_trade_flow`.
- `read_ladder_distribution` is **class-aware** (equity=`iqfeed_depth_snapshots`, crypto=`fast_orderbook`) + `as_of`-plumbed for replay parity.
- **Ross-style signals ALREADY COMPUTED but LOG-ONLY** (`microstructure_log.py`): `_ask_eaten`, `_hidden_seller`, `_spoof`, `absorption_ratio` → `trading_microstructure_log` — **but crypto-only and never wired into decisions.**

## Three corrections from the adversarial review (accepted)
1. **There is NO equity signal-drain.** `chili_micro_log_equity_enabled` is **dead** (definition + docstring only, zero call-sites); `run_microstructure_log_drain_job` hardcodes `asset_class="crypto"`. So tape-additivity is **UNPROVEN** for equity until an equity drain is built.
2. **The spoof SUPPRESSOR is unbuildable for equity.** `_spoof/_ask_eaten/_hidden_seller` need per-price-level arrays (`asks[1:5]`); the IQFeed depth bridge persists only **aggregate 5-level SUMS + top-of-book @ ~2s** (per-venue ladder lives only in the bridge process, never written). Equity's only spoof defense is the self-relative percentile (can't catch a fast pull-before-fill inside the 2s cadence).
3. **Equity `ask_lift_rate` is NOT "the primary."** Derived from top-of-book stepping across 2s snaps → near-collinear with positive OFI + only a 2s proxy. **The genuinely additive, tick-resolution signal is the TAPE** (`tick_rate`, `signed_tape_accel`, `buy_lift_vol` from `iqfeed_trade_ticks` — a true tick-by-tick bridge, Lee-Ready via `_aggressor_imbalance`).

## Phased roadmap
- **Phase 0 — BUILD + CALIBRATE** (zero decision impact): build `_compute_signals_equity` over `iqfeed_depth_snapshots` + `iqfeed_trade_ticks`, wire into the drain with `asset_class='equity'`, accumulate labeled signals + forward returns, run the additivity regression (forward-return ~ tape_accel + ask_lift_rate, **controlling for OFI**). GO/NO-GO per signal (must survive fees — the −1.58pp falsified-accelerant lesson). *This is the "log-only-first" the operator has previously rejected for OFI.*
- **Phase 1 — CONFIRMER** (the safe graduation, most value/least risk): a **DEFER-only** `_l2_entry_confirm` at the existing seam (`live_runner ~5928`), AFTER the chart trigger + AFTER both existing vetoes pass. **Tape-primary** (`signed_tape_accel`+`tick_rate`; OFI + rising depth-pctile as secondary). On no-confirm → DEFER (stay `WATCHING_LIVE`, re-enter when the tape confirms) + emit `live_l2_confirm_defer` counterfactual. Can ONLY reduce entries, never add a fire; entry-only (never blocks exits); fail-open (None→confirm); kill-switch `chili_momentum_l2_confirm_enabled`; OFF = byte-identical; permissive-start (floor=0 ⇒ never defers, tune up only if the counterfactual shows it catches losers). **Watch the E1-backside lesson** (over-deferring valid reclaims).
- **Phase 2 — HARDEN:** staleness / thin-book floors (the `venues` count, `STALE_VENUE_ROW_S`), tune adaptive floors from Phase 0/1 counterfactuals.
- **Phase 3 — PRIMARY TRIGGER** (conditional on Phase 1 A/B net-positive): let an L2 tape-burst ADVANCE the fire ~1 tick when ALREADY armed at a level + structural stop + liquid-only. Separate flag `chili_momentum_l2_trigger_enabled`. For equity MUST require **TAPE** confirmation (not a bare 2s depth-burst — spoof-suppressor unbuildable). Explicit OFF=byte-identical parity test (NEW fire path).
- **Phase 4 — CRYPTO:** once the Coinbase WS level2 ring is populated (the known crypto-L2 gap).

## Honest scope
- **Mechanizable (this design):** depth imbalance + self-relative percentile trend; OFI + micro-price (live); **TAPE** tick_rate/accel/buy_lift_vol (the equity primary tick-fire proxy); equity ask-lift rate (confirmer input only).
- **Discretionary — NOT building:** spoof gut-read, hidden-buyer/seller real-time ID, momentum-vs-algo-churn, price-velocity intuition, key-level psychology, tape-conviction "feel." These are Ross's years-of-tape-reading and do not cleanly mechanize.

## Operator decisions
1. **Phasing:** Phase-1 confirmer wired LIVE first (operator's usual no-log-only-first mode; DEFER-only = safe) **vs** Phase-0 calibrate-offline first (the design's rigor, proves additivity, but log-only-first).
2. The TAPE (not depth) is the equity edge — confirms the iqfeed_trade_ticks bridge must stay healthy during RTH.
