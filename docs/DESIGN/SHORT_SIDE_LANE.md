# Short-side lane — design + phased plan

**Status:** PLAN (2026-06-29). Design-only — no code yet. The momentum lane is **long-only**;
this adds a SHORT side on the **Alpaca** rail (the RH agentic isolated-CASH account cannot
short; Alpaca can). Operator confirmed: *"di pwede magshort sa agentic ngayon, kaya sa Alpaca
na lang."*

## Why (the problem this solves)

CHILI's momentum lane only goes **long** — it rides the explosive low-float verticals up, or
sits them out. Ross makes a meaningful fraction of his green days on the **short** side, fading
the SAME names: parabolic-exhaustion tops, failed-breakout rollovers, and gap-up fades. Today
CHILI is structurally blind to that half of the move:

- The Ross-bridge **silently drops every down/short signal** — `_equity_movers_for_ross_bridge`
  keeps only `direction ∈ {up, long, bull, ""}` (`trading_scheduler.py:3500–3523`, the drop is
  line ~3519); the docstring says *"The momentum lane is LONG-only, so short ORB breakdowns are
  dropped."*
- The execution lifecycle is hard-wired long: every entry leg is `side="buy"`
  (`live_runner.py:9096`, plus the add/pyramid legs at `:11432/:11924/:12320/:12788/:7695`) and
  every exit/scale leg is `side="sell"` (`live_runner.py:1345/1363/2357/11303`).
- The stop/target call is hard-coded `side_long=True` (`live_runner.py:7213`), and the
  paper-execution geometry functions early-return for shorts.

**Why Alpaca, not RH-agentic.** The sanctioned RH **Agentic Trading MCP** rail is an isolated
**CASH** account (no margin, no short) — `project_robinhood_agentic_mcp`. Alpaca is API-first,
margin-enabled, has a **free paper sandbox** (`https://paper-api.alpaca.markets`), supports
**`SELL_TO_OPEN` / `BUY_TO_CLOSE` position-intent** (verified in the installed `alpaca-py`
0.43.4 — see below), and is **already wired as an execution family** (`alpaca_spot`). The short
lane reuses the venue-agnostic FSM that already drives Alpaca paper for the long soak.

**The latent asset (the big finding).** The paper-execution geometry layer is **already
short-aware** — it was written with dual long/short branches that simply never fire because every
caller passes `side_long=True`. `stop_target_prices` (`paper_execution.py:233–280`) already
computes a short stop ABOVE entry and target BELOW; `breakeven_stop_after_partial` (`:455–470`),
`runner_trail_stop` (`:1113–1146`), `volnorm_runner_trail_stop` (`:1348–1382`), and
`cushion_adaptive_trail_stop` (`:1626–1720`) all have a working `else`-branch for shorts. The
work is far smaller than greenfield: **wire a `side` through the lifecycle, flip the dropped
signals into a short queue, add the short-entry triggers, and harden the short-specific risk
rails.**

## What Alpaca gives us for shorts (verified 2026-06-29)

- **`alpaca-py` 0.43.4** (installed; `requirements.txt` pins `alpaca-py>=0.30`).
  - `OrderSide` = `BUY` / `SELL`.
  - `PositionIntent` = **`BUY_TO_OPEN`, `BUY_TO_CLOSE`, `SELL_TO_OPEN`, `SELL_TO_CLOSE`** — the
    exact disambiguation a short needs (a flat-account `SELL` + `SELL_TO_OPEN` opens a short; a
    `BUY` + `BUY_TO_CLOSE` covers it).
  - `LimitOrderRequest` accepts `position_intent`, `order_class`, `take_profit`, `stop_loss`
    (native bracket/OCO available later).
- **Margin / shorting** is an Alpaca account capability (the paper account shorts freely;
  live requires a margin account and per-name **borrow availability**).
- **SSR (Reg SHO short-sale restriction).** When a name is down ≥10% from the prior close,
  exchanges set SSR for the rest of that day + the next — short sells may then only execute on
  an **up-bid** (a non-marketable sell limit at/above the NBB). Alpaca enforces this venue-side;
  our marketable-down short entry can be **rejected/held** under SSR. The adapter must surface
  this (today it does not).
- **Borrow / locate.** Easy-to-borrow names short instantly; hard-to-borrow names can be
  rejected or fee'd. CHILI already fetches the borrow signal — `short_mechanics.py`
  (`get_short_mechanics`) pulls **cost-to-borrow** + short-interest from Ortex (today only as a
  long squeeze-fuel *selection tilt*); the short lane reuses CTB as a **locate-feasibility
  gate** (very high CTB ⇒ skip; the squeeze risk that fuels a long is the danger that kills a
  short).

> Current gap: `alpaca_spot.py:_submit` (`:346–400`) maps side via
> `OrderSide.BUY if side=="buy" else OrderSide.SELL` and passes **no `position_intent`** and
> **no SSR/borrow surfacing**. A `sell` is therefore ambiguous (open-short vs close-long) — the
> #1 adapter change in P0.

## The Alpaca short rail

### Adapter changes (`venue/alpaca_spot.py`)

1. **Position-intent plumb-through.** Add an optional `position_intent` to
   `place_market_order` / `place_limit_order_gtc` (and `_submit`). Map:
   - open short → `OrderSide.SELL` + `PositionIntent.SELL_TO_OPEN`
   - cover → `OrderSide.BUY` + `PositionIntent.BUY_TO_CLOSE`
   - (long open/close keep BUY/SELL with `BUY_TO_OPEN` / `SELL_TO_CLOSE`, byte-identical to
     today when intent is omitted — the Protocol stays backward-compatible).
2. **SSR / borrow surfacing.** `get_product` already reads the Alpaca asset
   (`alpaca_spot.py:232–254`); extend the normalized `raw` with `shortable`,
   `easy_to_borrow`, `shorting_enabled` (Alpaca `Asset` fields) so the short-entry gate can
   fail-closed when a name is not shortable. Surface order rejections that carry an SSR/borrow
   reason distinctly (so the runner can DEFER, not retry into a wall).
3. **Account shorting capability.** `get_account_snapshot` (`:419–429`) should also report
   `shorting_enabled` / `multiplier` so the lane never arms a short on a cash/no-margin account.

The `VenueAdapter` Protocol (`venue/protocol.py:134–184`) uses a generic `side: str`; adding an
optional `position_intent` keyword is additive (other venues ignore it). Coinbase/RH adapters
do not implement shorts and are never routed a short session (asset-class + capability gate).

### Routing — an isolated short bucket

Routing is keyed off **`execution_family`** (`execution_family_registry.py`). The short lane is
its **own family**, kept separate from the long agentic lane so risk, daily-loss, and
concurrency caps isolate it:

- Add `EXECUTION_FAMILY_ALPACA_SHORT = "alpaca_short"` to the registry
  (`execution_family_registry.py:27` neighborhood), asset-class `equity`
  (`_EQUITY_EXECUTION_FAMILIES`, `_EXECUTION_FAMILY_ASSET_CLASSES`), and to
  `IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES` (`:46–51`).
- `resolve_live_spot_adapter_factory` (`:260–283`) returns `AlpacaSpotAdapter` for it (same
  adapter, short intent set per-order by the runner).
- `resolve_execution_family_for_symbol` (`:212–247`) is **direction-blind** today (a symbol → a
  venue). The short selection path explicitly stamps `alpaca_short` on a *short-candidate*
  session; the existing long resolver is untouched.
- `venue_for_execution_family` (`:250–257`) → `"alpaca"` for the short family (so broker-sync /
  governance attribute it to the Alpaca account).

**Paper-first.** Reuse `CHILI_ALPACA_PAPER` (default **True**). The short lane proves the full
short FSM against the paper endpoint at zero risk before any live size, exactly as the long
Alpaca soak did.

## Short entry triggers (3 Ross setups)

All three reuse signals CHILI **already computes** — no new feature engineering, mostly
inversion. (Citations from the live signal map.)

### Anti-chase guard (applies to all three — the inverse of the long front-side guard)

The long lane sizes UP into strength via `front_side_strength_score` (Kaufman ER spine +
VWAP-dist + OFI + tape; `ross_momentum.py:1535–1579`) → `front_side_size_tilt`
(`:1590–1647`, floor 0.25). **Invert it for shorts:** do **not** short a strong, clean up-push
(high strength, ER≈1, positive OFI). A short is only admitted when strength is **rolling over**
(`front_side_state.rolled_over` / `chasing_top`, `ross_momentum.py:1409–1422`: HOD is not the
last bar AND a confirmed lower-high formed > `rollover_min_range_frac`). This is the safety that
keeps CHILI from shorting a name that's still going vertical (the squeeze that kills shorts).

### Trigger A — parabolic-exhaustion top

The textbook "short the blow-off." Confluence:
- **Overextension:** the long-side extension veto already measures it —
  `_entry_extension_veto` (`entry_gates.py:2238–2310`): `entry ≥ level·(1 + max(floor, K·atr))`
  with floor 0.08, K 8.0. For shorts this is the **precondition**, not a veto: the name has run
  8–12%+ past its breakout level.
- **Topping tail / climax:** `is_topping_tail` (`candles.py:50–65`, upper wick > 50% of range
  AND > body) + `macd_hist_rollover_from_df` (`candles.py:156–182`, momentum decel peak/zero-
  cross). The long lane already has a **red-volume-exhaustion** detector
  (`red_vol_exhaustion_veto`, `entry_gates.py:7223–7265`) — the session high is a climactic
  high-volume red bar. That veto's TRUE condition is exactly the short-entry condition.
- **First lower-high:** `rolled_over` (`ross_momentum.py:1409–1422`).
- **OFI flip:** `ofi_level_and_slope` (`paper_execution.py:1447–1466`) negative level + negative
  slope = selling accelerating. The long-side exhaustion EXIT (`ofi_exhaustion_lock`,
  `paper_execution.py:1723–1822`: profit-arm + micro-rollover + OFI flip + giveback) is the
  same confluence — when a long would lock/exit on exhaustion, a short ENTERS.

**Entry:** sell-to-open on the first lower-high after the climax, marketable-down limit.

### Trigger B — failed-breakout rollover (the trap)

Price breaks a level, fails to hold, and **reclaims back below** it (trapped longs flush):
- **Reclaim-below:** `front_side_state.below_vwap` / the level-reclaim logic
  (`ross_momentum.py:1403–1422`) — the long lane's VWAP/level *reclaim-UP* detector, run in
  reverse (lose the level after breaking it).
- **Structure:** confirmed lower-high (`rolled_over`) + a break back below the broken level /
  the confirmed swing structure (`entry_gates.py:24–50`, swing-low/break helpers — for a short,
  a break back below the breakout pivot).
- **OFI:** negative + accelerating (`ofi_level_and_slope`).

**Entry:** sell-to-open on the reclaim-below / loss of the failed level; stop just above the
failed level (where the trap would un-trap).

### Trigger C — gap-up fade

A name gaps up premarket, then **loses VWAP / the premarket high** intraday:
- **VWAP loss:** `front_side_state.below_vwap` (`ross_momentum.py:1403–1407`) + signed
  `vwap_dist_sigma` going negative (`:1560–1561`).
- **Premarket-high reference:** the lane already tracks premarket levels and HOD/realized-high
  (used for long targets); the gap-fade trigger fires when price closes back below the
  premarket high / opening range and loses VWAP.
- **No-reclaim filter:** require the anti-chase guard (strength rolling over) so a quick
  VWAP-reclaim doesn't get shorted.

**Entry:** sell-to-open on the confirmed VWAP loss after the gap; stop above the premarket high /
the morning HOD.

## Inverted lifecycle

The position carries `side_long=False`; every geometry/decision function below is fed the real
side instead of the hard-coded `True`.

| Concern | Long (today) | Short (inverted) | Where |
|---|---|---|---|
| **Sizing** | `qty = max_loss_usd / stop_distance` (direction-agnostic) | **unchanged** — same formula | `risk_policy.py:compute_risk_first_quantity` (~`:1372–1419`) |
| **Stop** | `entry·(1 − atr·mult)`, BELOW | `entry·(1 + atr·mult)`, **ABOVE** — already coded | `paper_execution.py:233–280` (else-branch `:277–279`) |
| **Target** | `entry + rr·risk`, ABOVE | `entry − rr·risk`, **BELOW** — already coded | `paper_execution.py:279` |
| **Breakeven ratchet** | `max(stop, entry)` | `min(stop, entry)` — already coded | `paper_execution.py:455–470` |
| **Runner trail** | chandelier below HWM, `max()` | above LWM, `min()` — already coded | `paper_execution.py:1113–1146 / 1348–1382 / 1626–1720` |
| **Entry order** | `side="buy"`, near/above ask | `side="sell"` + `SELL_TO_OPEN`, near/below bid | `live_runner.py:9096` (+ add legs) |
| **Scale-out** | `side="sell"`, below bid | `side="buy"` + `BUY_TO_CLOSE`, above ask | `live_runner.py:2357` |
| **Exit / flatten** | marketable sell DOWN | marketable buy UP | `live_runner.py:1254–1369` |
| **Max-loss circuit** | `pnl=(bid−avg)·q`, floor `avg − k·sd` | `pnl=(avg−ask)·q`, floor `avg + k·sd` | `risk_policy.py:1464–1543` |

**Scale-out / target ladder.** `scale_grid_levels` (`paper_execution.py:546–604`) currently
early-returns `[]` for shorts and builds R-levels ABOVE entry. Invert: cover tranches at R-
levels (and round numbers) **below** entry; a runner stays short for the flush. The adaptive-
target lift and round-number pull-in (`adaptive_first_target_reward_risk` `:334–393`;
`round_number_first_scale_target`) are long-only enhancements — for v1 the short uses the base
R:R geometry (correct and safe), with short-side adaptive lift as a later lever.

**Exit philosophy (Ross-style short).** Cover INTO the flush (don't be greedy at the lows),
scale-out on the way down, target the obvious support / VWAP reclaim, trail the runner above the
local lower-highs. The default short target = VWAP / prior support; the runner trails above the
descending swing-highs via the existing `runner_trail_stop` short branch.

**The halt-up KILL (short-specific, must be built).** Halts are the asymmetric danger for a
short — a low-float halts UP and re-opens 30–80% higher, gapping THROUGH any stop. There is **no
halt-up cover logic today**; halt handling is only a static entry GATE (`halt_passed` read in
`auto_trader_rules.py`; `kill_switch_halts_new_entries` is the manual switch). **Required:** a
short-only watcher that, on a detected **halt-up** (or a fast vertical re-open above the stop),
**buys-to-cover at market/aggressive-limit immediately** rather than resting the structural
stop. This is the single most important new control.

## Phased plan

Each phase is independently shippable + testable, paper-first, kill-switched, byte-identical
when flag-off.

- **P0 — Alpaca short adapter + feasibility (S, ~1 day).**
  Plumb `position_intent` through `alpaca_spot._submit` / the two place methods; surface
  `shortable` / `easy_to_borrow` / `shorting_enabled` from the asset + account. Add the
  `alpaca_short` execution family. **Test:** a paper `SELL_TO_OPEN` opens a short position and a
  `BUY_TO_CLOSE` covers it (assert the resulting Alpaca position qty goes negative then flat);
  adapter unit test on the intent mapping. No lane logic yet.

- **P1 — ONE trigger end-to-end in paper: parabolic-exhaustion (M, ~2–3 days).**
  Thread `side_long` through the live-execution dict and the `stop_target_prices` /
  order-side call sites (`live_runner.py:7213`, `:9096`, `:2357`). Stand up a single short-entry
  detector (Trigger A) feeding a short-candidate session routed to `alpaca_short`. Drive the FSM
  paper end-to-end: detect top → sell-to-open → stop above / target below → cover. **Test:** a
  parity-style replay that feeds a known parabolic-top tape and asserts a short fills, the stop
  sits above entry, and a cover exits at the target.

- **P2 — inverted sizing / stop / exit hardening (M, ~2 days).**
  Make the max-loss circuit side-aware (`risk_policy.py:1464–1543`: `pnl=(avg−ask)·q`,
  floor `avg + k·sd`). Invert `scale_grid_levels` for cover tranches below entry. Wire the
  short-side breakeven + runner-trail (already coded; just feed `side_long=False`). **Test:**
  circuit fires on a squeeze (price runs UP through `avg + k·sd`); scale-grid produces strictly-
  descending cover levels.

- **P3 — the other 2 triggers + SSR + halt-up KILL (M–L, ~3–4 days).**
  Add Trigger B (failed-breakout rollover) and Trigger C (gap-fade). Un-drop the short ORB
  signals in `_equity_movers_for_ross_bridge` (`trading_scheduler.py:3519`) into a **short
  viability queue** (the long queue is untouched). Add the borrow/SSR locate gate (skip
  not-shortable / very-high-CTB names; defer-not-retry on SSR rejection). Build the **halt-up
  cover watcher**. **Test:** SSR/not-shortable names are skipped; a simulated halt-up triggers an
  immediate buy-to-cover.

- **P4 — paper soak → live ramp (M, ongoing).**
  Soak the short lane in paper alongside the long lane on the same names; measure short fill
  quality, expectancy, and squeeze-stop behavior. Add a **live alpaca family** to
  `REAL_DAILY_LOSS_FAMILIES` (see Risk note) and the per-broker cap. Flip `CHILI_ALPACA_PAPER`
  off for a **small** short size only after the soak is clean, behind the kill-switch + drawdown
  breaker (Hard Rules 1/2).

**Total:** ~10–14 focused days, front-loaded by the geometry already being short-aware.

## Risk controls + kill-switches

Shorts are dangerous (asymmetric/unbounded upside on a squeeze), so the controls are strict and
reversible:

- **Hard stop ABOVE the high — always.** A short never rests without a structural stop above the
  swing-high / failed level. The max-loss circuit (#769, `risk_policy.py:1464–1543`) is the
  absolute backstop: side-aware floor `avg + k·stop_distance`, anchored to entry+structural-risk
  (not a chasing ask), so a deep gap-UP-through fill is bounded.
- **Tighter sizing.** Short per-trade risk fraction set BELOW the long fraction (a new
  `chili_momentum_short_risk_loss_fraction_of_equity`, ≤ the long
  `chili_momentum_risk_loss_fraction_of_equity`). Risk-first sizing off the (wider, above-entry)
  stop naturally sizes smaller.
- **NO averaging-down / NO pyramiding a loser.** The long lane pyramids INTO strength; the short
  lane must **never add to a position moving against it** (a squeeze). Pyramid/add legs
  (`live_runner.py:11432/11924/12320/12788`) are **disabled for shorts** in v1.
- **Halt-up KILL.** On a detected halt-up or fast vertical re-open above the stop, cover at
  market immediately (P3). This overrides the resting structural stop.
- **SSR / borrow gate.** Skip not-shortable / very-high-CTB names; defer (don't retry) on an
  SSR-rejected entry.
- **Per-broker daily-loss cap (live).** `per_broker_daily_loss_cap_usd` (`governance.py:1125–
  1154`) is `family`-keyed off the broker's cash value via `_account_equity_usd(family,
  prefer_cash_value=True)` — the short Alpaca bucket gets its own cap automatically **once the
  live alpaca family is added** to `REAL_DAILY_LOSS_FAMILIES` (today `alpaca_spot` is treated as
  paper/fake and **excluded** — `governance.py:1032`, `:873/:885/:1110`). Until then the short
  lane is paper-only by construction.
- **Concurrency / open-risk budget.** `adaptive_max_concurrent_live_sessions` /
  `equity_relative_daily_loss_cap` (`risk_policy.py:445–499`) are already `execution_family`-
  keyed; the short family carries its own slot/risk budget, separate from the long lane.
- **Kill-switches (no dark flags — live + paper-gated):**
  - `CHILI_MOMENTUM_SHORT_ENABLED` (bool, default **False** until P1 proves) — master gate;
    OFF ⇒ byte-identical long-only lane.
  - `CHILI_ALPACA_PAPER` (existing, default True) — paper short until proven.
  - `CHILI_MOMENTUM_SHORT_TRIGGER_*` per-trigger flags (parabolic / failed-break / gap-fade) so
    each trigger ramps independently.
  - `CHILI_MOMENTUM_SHORT_HALT_UP_KILL_ENABLED` (default True once built — a safety, on by
    default).
  - Reuses the global kill-switch (Hard Rule 1) + drawdown breaker (Hard Rule 2) unchanged.

## How it composes with the long lane

- **Separate bucket, separate family.** `alpaca_short` is its own execution family with its own
  adapter routing, daily-loss cap, concurrency budget, and kill-switch. The long lanes
  (`robinhood_spot` / `robinhood_agentic_mcp` live, `alpaca_spot` paper soak) are **untouched**
  when the short flag is off (byte-identical).
- **Selection flags a name as short OR long, never both at once.** The selection layer already
  computes the front-side strength + rollover state; a name is a **long candidate** when it's
  on the front-side (strength high, above VWAP, HOD recent) and a **short candidate** when it has
  rolled over (lower-high confirmed, lost VWAP, exhaustion confluence). The short queue is fed by
  the un-dropped down/short ORB signals (P3) plus the inverted triggers; a single symbol resolves
  to one direction per evaluation, and a single-writer guard (the existing
  `management_scope='momentum_neural'` baton, `project_crvo_orphan_root_cause`) prevents a long
  and short on the same name simultaneously.
- **Shared geometry, opposite sign.** Both directions call the same `paper_execution` geometry
  functions; the only difference is the `side_long` argument. This keeps the long and short
  lanes in lockstep for parity testing.

## Risks + mitigations

- **Squeeze (the existential short risk).** A low-float runs vertical / halts up and gaps through
  the stop. *Mitigations:* hard stop above the high + side-aware max-loss circuit; halt-up KILL
  (immediate cover); NO averaging-down/pyramiding; the anti-chase guard refuses to short a name
  still on the front-side; tighter short sizing; reuse the Ortex CTB signal to AVOID the most
  squeeze-prone (highest-short-interest, hardest-to-borrow) names — the very names the long lane
  *prefers* are the names the short lane *avoids*.
- **Borrow availability.** Hard-to-borrow names reject or fee the short. *Mitigation:* the
  `shortable`/`easy_to_borrow` asset gate + CTB threshold (skip, fail-closed).
- **SSR.** A name down ≥10% can only be shorted on an up-bid, so a marketable-down entry is
  rejected/held. *Mitigation:* surface the SSR-shaped rejection distinctly and DEFER (or post a
  non-marketable up-bid limit), never blind-retry.
- **Paper-vs-live short-fill divergence.** Alpaca paper fills shorts frictionlessly (no real
  borrow, no real SSR queue, instant locate). Live shorts face borrow scarcity, SSR up-bid
  constraints, and fee'd locates — so paper short P&L overstates the live edge MORE than long
  paper does. *Mitigation:* treat the paper short soak as **relative/qualitative** (does the
  trigger catch Ross's fades? does the lifecycle behave?), not as a $-truth; gate the live ramp
  (P4) on a small size + a live fill-quality measurement, mirroring the long Alpaca soak posture.
- **Halt detection latency.** If halt-up detection lags the re-open, the cover is late.
  *Mitigation:* the structural max-loss floor still bounds the worst case; tune the watcher off
  the fastest available tape (IQFeed L1, the same feed wired into the entry gate).

## Operator action items

- Confirm the Alpaca **paper** account shorts (it does by default) and that a future **live**
  account is/will be **margin-enabled with shorting** (cash account cannot short).
- (Live only, P4) decide the short per-trade risk fraction (default: ≤ the long fraction).

## Dependencies

- `alpaca-py>=0.30` (installed 0.43.4 — supports `position_intent`, native bracket/OCO).
- Ortex key (`CHILI_ORTEX_API_KEY`) — already present for the long squeeze-fuel tilt; reused as
  the short borrow/locate gate.
- The IQFeed L1 tape (already wired into the entry gate) for halt-up detection latency.

See `ALPACA_LANE.md`, `MOMENTUM_LANE.md`, `MOMENTUM_ENGINE.md`, `project_robinhood_agentic_mcp`,
`project_momentum_lane`, `project_profitability_levers` (#769 max-loss circuit),
`project_per_broker_daily_loss`.
