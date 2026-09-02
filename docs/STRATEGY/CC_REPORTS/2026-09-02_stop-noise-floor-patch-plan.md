# Stop-noise floor — minimal patch plan + ablation (2026-09-02)

**Branch:** `seam/stop-noise-floor-0902` (off `origin/main` 9fad8adf9) · **PR:** #1290 (forensics + this plan + shadow) ·
**Inputs:** mechanism inventory (scratchpad `stopnoise/stop_noise_floor_mechanism_inventory_0902.md`),
`2026-09-02_stop-noise-floor-forensics.md` / `.csv` (8 stop-driven Alpaca live exits in 21 d, 3 viability tightens),
memories `project_losing_entries_forensics_0901` (#1278 A/B REJECTED), `project_bailout_shakeout_negative_results_0830`
(3/3 exit-timing fixes rejected), `project_60s_burst_exit_validated_0901`, `project_first_target_rr_2p5_shipped_0901`.

## 0. Decision

| term | what it changes | ablation (n, avoided, extra loss, net) | decision |
|---|---|---|---|
| **T-shadow** — measure the C4 tighten against live spread + own 60 s range, emit in the existing event | telemetry only; applied stop unchanged | n/a (no behaviour) | **SHIP, LIVE+ON** (this PR) |
| **T-spread** — clamp the C4 tighten candidate to `avg × (1 − 1.5 × spread_frac)` | the one stop writer set by a constant (`avg × 0.995`) | n=3 tightens: avoided 0/3, extra loss 0 to −7 $, net **0 to −7 $** (see §2) | **DO NOT SHIP (apply)** — no measured benefit; 2/3 tightens were right |
| **T-range** — same clamp with the own-tape 60 s bid range (1.0×) | same site; turns CANF's tighten into a no-op | n=3: avoided 0/3, net **0 to −7 $** | **DO NOT SHIP (apply)** — same |
| **F1** — spread floor on the INITIAL stop (k = 1.5/2/3) | sizing (risk-first qty) | n=8: avoided 1/2/2, extra 0, net +0.00 / +1.16 / +10.44 $ (+0.00 / +0.07 / +0.64 R) | **DO NOT SHIP** — the stops already clear the spread (median 2.9×); touches sizing (#1278 shape) for ≈ nothing |
| **F2** — 1-min true-range floor on the INITIAL stop (m = 1/1.5/2) | sizing, 1.5–7.5× smaller qty | n=8: avoided 3/5/5, extra 0, net +54.76 / +79.98 / +94.78 $ same-qty (+2.1 / +3.9 / +5.3 R) | **DO NOT SHIP** — it is the #1278 lever (size) that the interleaved replay A/B rejected 09-02 00:30 (LIDR entry lost below min size, SLE −1.07); the "avoided" trades end in a #1277 burst exit at −0.3 R, not in wins; sample has zero winners so the cost side is invisible |
| **H** — hold-through at breach confirm (spread-relative) | exit timing | — | **DO NOT SHIP** — exit-timing class, 3/3 A/B rejected; breaches persist ≥ 28/60 s in 7/8 |

Bottom line: **the evidence does not support applying a floor anywhere today.** The gap statement
("breached by a single wide-quote tick, then filled far below") is contradicted by the tape for both 09-02
trades — CANF was a −5 % / 4 s flush that crossed the *original* stop 0.7 s before the tighten; UPC was a
1,500-share sweep with no tighten. Today's losses are spent-leg entries + 4–7 s held-tick quote staleness
(premarket stand-in pinned at the 11:10:54.47 row for 5.2 s) + 7.7–14 s breach→fill — the #1280/#1281/#1282
plumbing family, not stop geometry. What ships is the measurement that makes the apply decision falsifiable.

## 1. The patch (shipped in this PR, shadow only)

1. `paper_execution.stop_tighten_noise_clamp(candidate, current_stop, ref_price, spread_frac, noise_frac,
   spread_mult=1.5, noise_mult=1.0) -> (out, meta)` — pure.
   `floor_frac = max(1.5 × spread_frac, 1.0 × noise_frac)` over the readable terms;
   `out = max(current_stop, min(candidate, ref_price × (1 − floor_frac)))`.
   * One documented ratio per term, both reused: 1.5 = `volnorm_trail_dist_pct.spread_floor_mult`;
     1.0 = `#1278 stop_noise_floor_decision` (stop ≥ 1× own 30 s range).
   * INVARIANT-A: `current_stop ≤ out ≤ candidate`. Never lowers a placed stop; never raises a candidate;
     never touches the initial stop or sizing (the #1278 failure mode is structurally excluded).
   * Fail-open: no readable term ⇒ candidate unchanged (`reason=no_measure`); unreadable prices ⇒ unchanged.
   * "Never widen past the structural stop": the clamp can only *lower* a tighten toward `current_stop`,
     and `current_stop` is the placed (structural or vol-floored) stop — so it can never go below it.
   * Breakeven lock after ≥ 1R (`max(stop, entry)` after a partial) and the deadman are different writers and
     are not touched; the trail's own `trail_noise_floor_clamped` (hwm-anchored) is unchanged.
2. `live_runner._c4_tighten_noise_shadow(le, avg, bid, ask, candidate, current_stop, now_epoch)` — query-free:
   `spread_frac = (ask − bid)/mid` of the same tick; `noise_frac = (max − min)` of `le["burst_track"]`
   (the #1275/#1277 ring of the runner's own bid samples, 60 s lookback) `/ avg`. **No `iqfeed_trade_ticks`
   read on the exit-critical path** (memory: the 89 GB table's probes time out; `_own_tape_noise_floor_pct`
   is not safe inside a tick). Fail-open to `{"nf_reason": ...}`.
3. C4 site (`live_runner.py:41035`): `tighter_stop = max(_live_stop_c4, avg * 0.995)` is **unchanged**; the
   `viability_degraded_tighten` payload gains `nf_spread_frac, nf_noise_frac, nf_noise_samples, nf_floor_frac,
   nf_floor_term, nf_floor_px, nf_clamped_stop, nf_candidate_inside_noise, nf_would_move, nf_spread_mult,
   nf_noise_mult[, nf_reason]`. One event per reason by construction (C4 writes once per position: after the
   write `pos.stop_price == avg × 0.995`, so `tighter_stop > _live_stop_c4` is false on later ticks).
4. Config keys: **none** (telemetry has nothing to toggle; no dark flag).
5. Tests `tests/test_stop_noise_floor_shadow_0902.py` (DB-free, house style): CANF spread term → 4.31 (vs placed
   4.3183, 1.08× spread); CANF range term (12 c ring) → clamp returns the original 4.2654 (tighten = no-op);
   UPC hypothetical tighten bounded both sides; AUUD initial stop (0.30× TR) is **not** widened; identity when
   no tighten; fail-open on None/NaN/crossed/zero; invariant-A over a 256-point grid; multipliers pinned to the
   existing ratios; shadow replays CANF from the burst ring (stale sample excluded), spread-only fallback,
   never raises; AST guards: shadow wired exactly once inside the `viability_degraded_tighten` emit, the pure
   clamp has no consumer other than the shadow, the applied-stop line is verbatim, the shadow takes no `db`
   and makes no DB call.

## 2. Ablation — the tighten-site terms (the only site where a floor is not a sizing change)

All three `viability_degraded_tighten` events in 21 days, replayed against the tape (forensics CSV columns
`F1_k1.5__tighten_stop … F2_m1__tighten_stop`, `*__outcome`, `*__pnl_same_qty`):

| event | placed tighten | T-spread (1.5×) | T-range (1.0×) | what happened next | Δ$ T-spread | Δ$ T-range | tighten was |
|---|---|---|---|---|---|---|---|
| CANF 19471 11:10:55.2 | 4.3183 (1.08× 2 c spread) | 4.31 | 4.2654 (no-op) | flush 4.33→4.12 in 4 s crossed **every** candidate; runner's stale bid 4.28 ≤ 4.31 → same breach tick; under 4.2654 the breach is seen at the next fresh quote (11:11:00.6, bid 4.14) → fill ≈ 4.10–4.12 vs actual 4.1199 | **0** | **0 to −7** | wrong (+7.8 % in 15 min) but non-causal |
| LIDR 19415 13:03:22.9 | 1.6616 (0.84× 1 c spread) | 1.655 | 1.63 | sim: burst exit at 1.65 under every stop (−2.86 in all arms); name then −9 % | **0** | **0** | right |
| CDTG 16534 11:43:47.8 | 1.3631 (breached 0.1 s later) | ≈1.35 (spread not on tape) | n/a | runner died 11:45; −15 % after; any lower stop fills lower | **≤ 0** | **≤ 0** | right |
| **total** | | | | | **0 $, 0 R, avoided 0/3** | **0 to −7 $, avoided 0/3** | 2/3 right |

Why this is still "not an unnecessary gate" if it is ever applied: it adds no decision — it modifies the candidate
of an existing writer and is the identity on every tick where the tighten already clears the measured noise;
it cannot block an entry, change size, or delay an exit. Why it does not ship now: on n=3 it never changed an
outcome, and its only directional effect (a lower fill when the viability call is right, 2/3) is negative.

## 3. Ablation — initial-stop floors (from the forensics, 8 stop-driven exits; per-second bid sim, latency fill, #1277 ON)

| rule | changed | avoided N | avoided $ (baseline loss) | then target / burst / wider-stop / open | extra loss $ | net $ same-qty | net $ risk-first qty | net R | N |
|---|---|---|---|---|---|---|---|---|---|
| F1 k=1.5 | 3/8 | 1 | −2.86 | 0/1/2/0 | 0.00 | +0.00 | +9.28 | +0.00 | 8 |
| F1 k=2 | 4/8 | 2 | +14.55 | 1/1/2/0 | 0.00 | +1.16 | +17.96 | +0.07 | 8 |
| F1 k=3 | 5/8 | 2 | +14.55 | 1/1/3/0 | 0.00 | +10.44 | +42.81 | +0.64 | 8 |
| F2 m=1 | 6/8 | 3 | −70.65 | 1/2/3/0 | 0.00 | +54.76 | +116.24 | +2.09 | 8 |
| F2 m=1.5 | 7/8 | 5 | −101.43 | 1/4/2/0 | 0.00 | +79.98 | +153.24 | +3.90 | 8 |
| F2 m=2 | 7/8 | 5 | −101.43 | 1/4/2/0 | 0.00 | +94.78 | +169.46 | +5.31 | 8 |
| F3 = max(F1,F2) | = F2 | | | | | | | | |

Reading: F2's dollars are two-thirds **size** (AUUD 551→163/108 sh, CANF 355→94/63 sh) — the mechanism the
09-02 replay A/B rejected — and the "avoided" stop-outs become #1277 burst exits at −1 to −3 % vs entry
(CANF 4.23 vs 4.34), i.e. −2.9 R → −0.3 R, never a win. AUUD and UPC are stopped under every rule (real
breakdowns). No winner in the sample ⇒ the cost side (min-size entry loss, SLE nick) is unmeasurable here.

## 4. Measurement that would license the apply step (the proposal)

1. **Event study on the shadow fields** — ≥ 20 `viability_degraded_tighten` events (or 10 trading days),
   query `payload->>'nf_candidate_inside_noise'`, `nf_clamped_stop`, then per event on the tape:
   breached within 60 s of the tighten? recovered ≥ +1 R (initial R) within 15 min? counterfactual $ from
   `scripts/research/stop_noise_forensics_0902.py::simulate_with_stop` with `stop=nf_clamped_stop`,
   `stop_start_epoch=tighten ts`, the trade's own breach→fill latency.
   **Apply criterion:** among `inside_noise` events, Σ(Δ$ clamped − placed) > 0 **and** the clamped stop is
   worse in < 1/3 of events where the tighten was right (name went on to −5 % or more).
2. **Interleaved replay A/B** (`reference_replay_ab_must_interleave_arms`) with the clamp applied — safety gate
   only (the sim enters at its own time; it cannot prove benefit, it can prove harm).
3. If both pass: apply PR = replace `avg * 0.995` by `stop_tighten_noise_clamp(candidate=avg*0.995, …)[0]` in the
   C4 branch, emit `stop_noise_floor_applied {old_stop, candidate, new_stop, spread_frac, noise_frac,
   floor_term}` once per position (in place of the `nf_would_move` field), config
   `chili_momentum_stop_tighten_noise_floor_enabled` default **True** (LIVE+ON, kill via env only, no ramp),
   no new multipliers.
4. **The better-evidenced levers for today's two losses** (not this PR): premarket held-tick stand-in staleness
   (`chili_momentum_held_stand_in_max_age_seconds` 15 s allowed a 5.2 s pinned quote across three events while
   `momentum_nbbo_spread_tape` had three newer rows) and the 7.7–14 s breach→fill — CANF's −3.4 % vs its
   original stop is entirely there.

## 5. Post-deploy kill criteria

Shadow (this PR):
* any `viability_degraded_tighten` with `nf_reason` starting `shadow_error:` → bug, fix forward (behaviour is
  unchanged either way);
* the count of `viability_degraded_tighten` events per session must be identical to the pre-deploy shape
  (one per position) — a change means the shadow altered firing, which it cannot by construction;
* no new DB statements from the tick (the shadow is query-free; verified by AST test).

Apply step (future, only after §4):
* INVARIANT-A: any event where `pos.stop_price` decreases → revert immediately;
* two consecutive clamped positions whose realised loss exceeds the unclamped counterfactual by > 0.5 R →
  set `CHILI_MOMENTUM_STOP_TIGHTEN_NOISE_FLOOR_ENABLED=0`;
* clamp rate > 80 % of tightens over 10 events → the floor dominates the writer (the tighten is dead);
  re-examine the tighten itself rather than the floor.

## 6. Files

* `app/services/trading/momentum_neural/paper_execution.py` — `stop_tighten_noise_clamp` (pure).
* `app/services/trading/momentum_neural/live_runner.py` — `_c4_tighten_noise_shadow` + payload spread at the C4 emit.
* `tests/test_stop_noise_floor_shadow_0902.py` — DB-free tests + AST guards.
* This document.
