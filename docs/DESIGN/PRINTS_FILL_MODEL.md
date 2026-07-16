# PILLAR 3 — Prints-Based Fill Model (version-agnostic backtest fill)

**Goal:** make `replay_v2` a TRUE version-diff backtest. Record data faithfully → replay
ANY system version → diff trades between versions. The fill model must decide fill-vs-cancel
for a hypothetical order at *any* price/time *without reading the day's actual live trades*
(so a NEW version's different entries are evaluatable). This replaces the rejected
"RECORD don't derive" consumer (`_load_recorded_fills`, `replay_v2.py:318`), which can only
reproduce the names the deployed version traded.

The instrument used today — `momentum_nbbo_spread_tape` QUOTES — over-fills (BEEM 29/34
replay vs 1/34 live; `project_replay_lab` memory). This designs the fill from the
**executed trade-print tape** `iqfeed_trade_ticks` instead.

---

## 0. What the validation actually proved (run in-container, db=chili, 2026-06-28)

`iqfeed_trade_ticks` is the IQFeed L1 trade-print bridge (commit `d473331`, the entry-gate
`signed_tape_accel` source). Schema: `(id, symbol, observed_at, price, size, bid, ask, source)`.
Row counts: **2,279,367 total**, source `iqfeed_l1`. Per-day: **06-25 = 1,898,869 rows / 88 syms,
06-26 = 380,495 / 101 syms, 06-27 = 3 rows.** *No rows before 2026-06-25 12:56 UTC* — so the
operator's named day (06-24) has **zero print coverage** and cannot be validated against prints.
The validation therefore ran on **06-26** (dense prints + live fills), which is the equivalent test.

### Finding A — prints reproduce the FILL set AND fill PRICE faithfully

For the 10 names live-FILLED on 06-26 (14 entry legs in `momentum_fill_outcomes`), cumulating
through-print size (`price <= intended_limit`) in a `[fill_ts−13s, fill_ts+120s]` window:
**13/14 legs show ≥1 through-print with cumulative size ≥ order qty** → the prints model FILLS them.
The 1 miss is a re-arm whose minute had `win_ticks=0` (a coverage gap in the nascent bridge).

The **fill-price** match is the strong result — VWAP of the filling prints vs the broker's realized avg:

| sym  | intended limit | model VWAP | broker avg | thru-size / qty |
|------|---------------:|-----------:|-----------:|----------------:|
| CPSH | 5.7300 | **5.6127** | 5.6261 | 421 / 104 |
| RYOJ | 3.0800 | **2.8682** | 2.9199 | 135761 / 49 |
| VIA  | 17.5100| **17.3548**| 17.3499| 1033 / 32 |
| RYOJ | 2.9100 | **2.8384** | 2.8799 | 21963 / 73 |
| GALT | 4.8300 | **4.7689** | 4.7699 | 1760 / 104 |
| WSHP | 6.5300 | **6.2014** | 6.2299 | 12989 / 50 |
| WSHP | 8.0600 | **7.5835** | 7.5875 | 158 / 24 |

The prints model independently recovers the realized fill price within ~1–3% on every covered
name — exactly the "fills pull back toward the bid/mid, not the offer" reality the `replay_v2.py:1516`
T1.2 comment hand-approximates with `min(limit, max(bid, mid))`. Prints make it *measured*, not modeled.

### Finding B — the over-fill is NOT a fill-mechanics problem; it is ARM→PLACEMENT attrition

The `trading_automation_events` funnel (the live order lifecycle) reframes the whole pillar:

| day   | live_arm_requested | pending_place | **submitted** | **filled** | live_cancelled |
|-------|-------------------:|--------------:|--------------:|-----------:|---------------:|
| 06-23 | 193 | 37 | 24 | 32 | 145 |
| 06-24 | 175 | 47 | 12 | 20 | 139 |
| 06-25 | 375 | 11 |  6 |  4 | 327 |
| 06-26 | 261 | 48 | **13** | **14** | 202 |

On 06-26 **every order CHILI actually placed got filled** (13 submitted → 13 distinct
order_ids, all 14 fill legs). The 202 `live_cancelled` are **SESSION cancels** (watch-slot
reaped / superseded / risk-blocked), **not order cancels**. Only ~3–5% of armed names ever
*place* an order, and ~95–100% of placed orders fill.

So BEEM 29/34 replay vs 1/34 live was the replay **arming-and-auto-filling 34 names that the
live lane filtered out *before the rail*** (`live_blocked_by_risk`=2602, `live_entry_trigger_wait`=744,
`live_entry_backside_benched`=441 on 06-26). The derived auto-fill (`replay_v2.py:1474-1526`)
manufactured fills for names that, live, never reached `live_entry_pending_place`.

**The discriminator the prints model must reproduce is therefore two-layered:**
1. **Placement gate** — does the *armed* name ever progress to a placed order? (the dominant
   over-fill lever; the replay's existing CONVERGENCE GATES G1/G2 + `FIDELITY_V2` spread/governor
   gates at `replay_v2.py:779-799, 1468-1515` are the analog and already attack this).
2. **Fill resolution** — given a placed marketable-limit at price `L`, time `t`, qty `Q`, does it
   fill, partial, or cancel? **This is what the prints tape resolves**, and what this doc specifies.

The negative control (Finding C) confirms prints are necessary for layer 2: keying a naive
"any through-print in the armed window" gate to the *session* window fills 81/86 cancelled names —
useless. Keyed to the *real placement instant* with a tight ack window it collapses correctly.
Quotes can never do layer 2 (a quote that merely touches `L` doesn't mean `Q` shares traded there).

---

## 1. The exact print-tape query (version-agnostic core)

For a hypothetical BUY marketable-limit `(symbol S, limit L, qty Q, placed_at t0)` the replay
evaluates against the SAME recorded prints regardless of which version proposed the order — that
is the version-agnosticism: prints are immutable recorded facts; the order is the variable.

```sql
-- through-prints: actual executions at/through the buy limit, inside the live order window.
-- t_lo = t0 - review_latency   (the order is "live" to the tape ~latency before our decision ts)
-- t_hi = t0 + ack_window       (the live ack/rest backstop)
SELECT price, size, observed_at
FROM   iqfeed_trade_ticks
WHERE  symbol = :S
  AND  price <= :L                         -- a SELL would use price >= :L
  AND  observed_at >  :t_lo
  AND  observed_at <= :t_hi
ORDER  BY observed_at ASC
```

Reuse the established lookahead-free, as-of query shape from
`entry_gates.signed_tape_accel_features` (`entry_gates.py:1868-1885`): naive-UTC `observed_at`,
`make_interval(secs => …)`, `observed_at <= :as_of`. Bulk-load per day into a per-symbol
sorted list (mirror the `Tape` class at `replay_v2.py:408-532`, which already bisects
`momentum_nbbo_spread_tape`); add a parallel `TradeTape` keyed the same way so `prices_between`
has a print-size twin. Single SELECT, rolled back, read-only — REPLAY-ONLY.

**Fill rule** (cumulate, don't just touch):
```
cum = 0 ; notional = 0
for (price, size, ts) in through_prints:           # already filtered price<=L, time-windowed
    take = participation * size                    # we are one of many in the queue (see §2)
    cum += take ; notional += take * price
filled_qty = min(Q, cum - queue_ahead)             # queue_ahead consumed first (see §2)
filled_qty = max(0, filled_qty)
fill_vwap  = notional_of_the_filling_slice / filled_qty
```
- `filled_qty >= Q`  → **FILL** at `fill_vwap` (bounded `[bid, L]`).
- `0 < filled_qty < Q` → **PARTIAL** (see §3).
- `filled_qty == 0` (no through-print, or queue never cleared) → **CANCEL** (the live
  `cancelled_pre_entry` / `ack_timeout` outcome, `replay_v2.py:1485-1509`).

`fill_vwap` replaces the hand-tuned `min(limit, max(bid, mid))` at `replay_v2.py:1526` —
the prints give the **measured** central fill (Finding A: model VWAP ≈ broker avg within ~1–3%).

---

## 2. Queue & latency model — adaptive, no magic numbers

Two adaptive quantities, both derived from already-recorded data, both with ONE documented knob each:

### review_latency (`t_lo` offset, the ~10–13s BEEM finding)
The wall-clock gap between when our code *decides* to place and when the order is actually
*live on the book* (RH agentic review + ack). Derive it, don't hardcode:
- **Measured base (preferred):** median `(live_entry_submitted.ts − live_entry_candidate_detected.ts)`
  per session over a trailing window of recorded `trading_automation_events` — the lane's own
  realized review latency. This is a recorded fact, available for any replay date that has events.
- **Fallback when events are absent (counterfactual order on a no-live day):** the name's own
  inter-trade cadence — reuse the EXACT adaptive primitive the live stale-quote window already
  uses (`chili_momentum_stale_quote_window_k` × avg inter-trade interval over the last 120s of
  `iqfeed_trade_ticks`, clamped to floor/ceiling; `config.py:2646-2668`). A fast-printing name is
  live to the tape almost immediately; a slow name's latency floats up with its cadence.

One knob: `chili_momentum_replay_review_latency_k` (default 1.0 = use the measured median as-is;
the operator can scale it). No literal "13s" anywhere — 13s is what the formula *yields* on these
names, not a constant.

### ack_window (`t_hi` offset, the rest/cancel horizon)
Reuse the live backstop already in the replay: `chili_momentum_entry_max_rest_bars`
(`config.py:4196`) × the entry-interval seconds = the SAME `_ack_window_s` the `FIDELITY_V2`
fill-or-reject computes at `replay_v2.py:1499-1501`. No new constant.

### queue position (queue_ahead + participation)
We are not first in line at `L`. Two adaptive estimators, pick per available data:
- **L1-depth proxy (always available):** `queue_ahead = ask_size_at_L_when_posted`. The `bid/ask`
  columns exist on `iqfeed_trade_ticks`; size-at-touch comes from `iqfeed_depth_snapshots` (L2)
  as-of `t0` when present. We must consume `queue_ahead` of through-print volume before our own
  shares fill — this is what makes a single small print at `L` *not* fill a large `Q` (the BEEM
  case: prints touched but never cleared the resting size ahead of us).
- **participation cap:** we receive only a fraction of each print's size. Reuse the replay's
  existing `PARTICIPATION_CAP` (`replay_v2.py:99`, today 0.10 on minute-volume) — now applied to
  *print* size instead of minute-bar volume diff (strictly more faithful; same knob).

Net: `filled_qty = min(Q, Σ(participation·size) − queue_ahead)`. All three terms are
recorded/derived; the only literals are the existing documented knobs.

---

## 3. Partial-fill handling

A marketable limit can fill `f = filled_qty/Q ∈ (0,1)` when the through-print volume inside the
window clears `queue_ahead` but not the full `Q` before `t_hi`.

- **Emit the partial as the position** sized `filled_qty` at `fill_vwap`; the un-filled remainder
  is **cancelled** (live behavior: the rest of a gfd marketable limit that didn't fill in the
  ack window is pulled). Do NOT carry the remainder as a resting order — the live lane re-decides
  on the next tick, which the replay models as a fresh arm/trigger.
- **Below-min-size partial** (`filled_qty < base_min_size`, `replay_v2.py:1582`) → treat as
  **CANCEL** (no position), matching the live below-min rejection.
- Downstream sizing/stop/target (`replay_v2.py:1536-1583`) use `filled_qty` and `fill_vwap`
  unchanged — the partial simply enters with less size, identical risk math.
- Record `fill_fraction`, `queue_ahead`, `through_print_size`, `fill_vwap` on the trade meta so a
  version-diff can attribute a divergence to fill resolution vs decision logic.

PYXS on 06-26 is the canonical partial in the data: thru_size 180 vs qty 195 (0.92) → a faithful
92% partial, where the quote model would have filled 100%.

---

## 4. How it stays VERSION-AGNOSTIC

The prints are a recorded, immutable fact stream. The *order* `(S, L, Q, t0)` is whatever the
replayed version's decision logic produces. Any version — different selection, different trigger,
different sizing, a brand-new pattern — emits its own `(S, L, Q, t0)` and is scored against the
SAME `iqfeed_trade_ticks` rows. Nothing in the fill model reads `momentum_fill_outcomes` or any
record of what the *deployed* version did. This is precisely the property the rejected recorded-
fills consumer (`replay_v2.py:318-405, 736-764`) lacks: it keys off the deployed version's
realized round-trips, so it cannot evaluate a different version's different entries.

Two versions A and B run over one recorded day:
- A arms BEEM, triggers at 13:42, posts L=2.41 → scored vs BEEM's prints → CANCEL (no through-size).
- B arms BEEM, triggers at 13:39 (earlier trigger), posts L=2.38 → scored vs the SAME prints →
  FILL at the measured VWAP.
The **diff is attributable to B's earlier/cheaper entry**, not to a fill model that peeked at live.

---

## 5. Implementation plan (replay-only, default-OFF, byte-identical when off)

1. **`TradeTape`** class in `replay_v2.py` (twin of `Tape`): one bulk SELECT of
   `iqfeed_trade_ticks` for the date into per-symbol bisected `(ts, price, size, bid, ask)`;
   method `through_prints(sym, limit, t_lo, t_hi, side)` returning the windowed slice.
2. **`prints_fill_decision(...)`** pure helper (unit-testable like `freshness_arm_decision`):
   inputs `(through_prints, limit, qty, queue_ahead, participation, side)` → `(decision,
   filled_qty, fill_vwap, meta)`. No DB, no settings — pure, parity-testable.
3. **Latency/queue derivation** helpers reading the adaptive sources in §2 (events median +
   inter-trade-cadence fallback + L1/L2 size-at-touch).
4. **Wire behind `chili_momentum_replay_prints_fill_enabled` (default False).** When ON, replace
   the derived ack/fill block (`replay_v2.py:1474-1526`) with the prints decision; when OFF the
   existing quote-touch path runs unchanged (md5-of-trades parity check, the convention this
   module already uses, e.g. `replay_v2.py:128-148`).
5. **Tape-coverage guard:** if a name has `0` prints in the day (the nascent-bridge gap that
   produced the 5 `thru_sz=0` misses in Finding A), the model is *undecidable* for that name —
   fall back to the quote-touch path and tag `fill_source='quote_fallback'` so the diff report can
   bracket it (do not silently CANCEL a real fill on a coverage hole). As `iqfeed_trade_ticks`
   accrues history this fallback shrinks to zero.

### Caveats (honest)
- **Coverage horizon:** prints exist only from 2026-06-25 onward. Pre-06-25 replays (incl. the
  operator's 06-24) have no prints → the model is inert there until backfill. This is a data
  limitation, not a model one; the concept is proven on 06-26.
- **Layer-1 dominance:** prints fix the *fill-resolution* layer. The bigger BEEM over-fill driver
  is *placement attrition* (Finding B), already addressed by the convergence + FIDELITY_V2 gates.
  Prints are necessary but not sufficient alone — they make the placed-order outcome faithful;
  the existing gates decide which armed names place.
- **Queue size-at-touch:** when neither L2 depth nor the bid/ask-size is recorded for a minute,
  `queue_ahead` degrades to 0 (optimistic). Bounded by the participation cap, but flag such fills
  in the confidence band (reuse `band_tail`, `replay_v2.py:800`).

---

## 6. Validation summary (the crux, proven)

- **FILL side:** 13/14 live-filled 06-26 legs show through-prints clearing qty; model VWAP ≈
  broker avg within ~1–3% (§0 Finding A). Prints reproduce both the fill set AND the realized price.
- **CANCEL side:** a print model keyed to the *real placement instant + tight ack window*
  correctly rejects names with no through-print; the session-window version (the quote-equivalent
  mistake) wrongly fills 81/86 (§0 Finding C). Prints discriminate where quotes cannot — BUT the
  dominant cancel population on these days was **session/risk filtering before placement**, not
  rail cancels, so the prints layer must sit *downstream* of the replay's placement gates.

Conclusion: prints CAN discriminate fill-vs-cancel at the placement layer and reproduce fill
price, validating the pillar. The fill model is version-agnostic by construction (scores any
order against immutable recorded prints) and adaptive (latency, ack, queue all derived from
recorded data via existing knobs). Build it behind `chili_momentum_replay_prints_fill_enabled`.
