# CC_REPORT: flush-dip-conversion-fixes (2026-08-21, operator-directed)

> Operator brief (hindi mula NEXT_TASK.md — iniwan kong PENDING ang stale P1a doon):
> ang flush_dip_buy audit (39 fires → 33 pending_place → 2 trades) ay nagpakita ng
> tatlong pumapatay post-fire: (a) one-shot wide_bbo_spread demote (kasama ang phantom
> 3131bps, WYHG session 14675 08-20 18:41Z), (b) live_entry_wait_late_window ×80,
> (c) FSM regression sa trigger_wait kung saan nabubulok ang setup.

## What shipped

Tatlong mekanismo, lahat default-ON (walang dark flags), lahat may sariling
kill-switch + documented bases sa `app/config.py`:

1. **Punch-window retry hold** — pagkatapos ng dip-family candidate fire, ang isang
   TRANSIENT book-quality veto (`wide_bbo_spread` / `stale_bbo` / `unstable_spread` /
   `invalid_bbo`) ay HINDI na one-shot na nagde-demote sa `WATCHING`: ang candidate ay
   naka-hold sa loob ng ATR-scaled window (60-120s; kalmado=120s, ≥3× ref ATR=60s —
   ang parehong 1%/1×-3× vocabulary ng `_dip_velocity_size_mult`) habang ang bawat
   tick ay muling nagpapatakbo ng buong quote gate KASAMA ang secondary NBBO
   refetch/rescue. Namamatay nang maaga kapag `mid < structural_stop` (patay na ang
   setup). Event: `live_entry_punch_window_hold`.
   - `entry_gates.py`: `dip_punch_window_seconds` + settings wrapper
   - `live_runner.py`: stamp sa fire; hold sa quote-block demote site

2. **Late-window dip fresh-HOD placement** — ang A2 ×0.0 sa late/AH ay binubuksan na
   ng L8 dati para lang sa MONSTER days (px/lo ≥ 1.5). Ngayon, ang dip-family fire na
   FRESH ang HOD — `recent_high/session_high ≥ 1 − min(0.25, max(0.10, 1×ATR%))`,
   recent = huling 4 bars ng entry interval — ay nagpa-place sa ×0.5 (parehong
   reduced-size convention ng L8 monster). Parehong OHLCV fetch + per-minute memo ng
   L8 (walang bagong I/O). Event: `live_entry_late_window_dip_fresh_hod_placement`.
   - `entry_gates.py`: `late_window_dip_fresh_hod_mult` + wrapper
   - `live_runner.py`: L8 block extension (memo now carries hi/rhi/atr)

3. **L2 bid-stack confirm tilt** — ang kabilang kalahati ng B2: ang decision-time
   `imbalance5 ≥ +0.4` (ang sinukat na threshold, baligtad ang sign) sa dip-family
   fire = bounded conviction boost `[1.0, 1.25]`, composed sa ilalim ng PAREHONG 3x
   clamp + max_notional. Hindi kailanman veto/shrink. Sa replay ito ay inert (ang
   harness ay nagne-neutralize ng RH pricebook), kaya structural + unit-test evidence
   lang. Event: `live_entry_dip_bid_stack_tilt`.
   - `entry_gates.py`: `_dip_bid_stack_tilt_mult`
   - `live_runner.py`: stash sa fire + factor sa sizing product

4. **Shared dip-family vocabulary** — `DIP_FAMILY_TRIGGER_REASONS` sa entry_gates,
   pinalitan ang inline tuple ng dip-velocity lever (dating 7 reasons). **KASAMA na
   ang `double_bottom_break`/`double_bottom_break_tick_ok`**: mula #1093 (flush
   dip-buy double-bottom acceptance) ang flush-low retest ay pumuputok bilang
   double-bottom — ang detector mismo ay dip-buy ang semantiko (second-low
   bottoming-tail "flush that got bought back up"; stop = flush low). Na-obserbahan
   sa replay: ang mismong flush na nag-fire ng `flush_dip_buy` live noong 08-20 ay
   `double_bottom_break_tick_ok` na ngayon. No-op sa dip-velocity lever (walang
   `dip_roc_per_bar` ang detector debug ⇒ 1.0).

## Verification

### Unit tests (bago): `tests/test_dip_punch_window_late_hod.py` — 21 pass
WYHG measured geometry bilang fixtures; + `test_late_ah_monster_placement.py` (10) at
`test_refire_cooldown.py` (5) pass — hindi nagbago ang L8 monster semantics.
Kalapit na flush-dip tests: 134 pass, 1 pre-existing red
(`test_momentum_adaptive_spread_cost_veto.py::test_flag_default_is_off` — bumabagsak
din nang naka-stash ang buong diff; hindi kaugnay).

### Replay A/B (chili_replay_test; scripts/replay_v3_fsm_window.py; WYHG 08-20 tape)

Named window **18:30-19:00Z** (ang audit window; 14:30-15:00 ET = late band):

| Arm | Fires | Placements opened | Trades | PnL |
|---|---|---|---|---|
| BASELINE (3 flags OFF) | 10 | 0 (10× `live_entry_wait_late_window`, 2× wide_bbo) | 0 | $0.00 |
| FIX (defaults ON) | 8 | 8× `live_entry_late_window_dip_fresh_hod_placement` | 1 | −$2.08 |

Ang FIX trade: BUY 12 @ 5.58 (18:41Z-class fire, ×0.5 late-dip size sa $50 frozen
diagnostic cap) → breakout-or-bailout SELL @ 5.41 sa pre-vertical wobble. LEGIT na
trade — ang parehong flush na binili ni Ross; ang bailout ay existing exit logic.

Extended window **18:30-19:15Z** (kasama ang 19:00-19:05 vertical → 6.92):

| Arm | Trades | PnL | Binding blocker sa vertical |
|---|---|---|---|
| BASELINE | 0 | $0.00 | `live_entry_wait_late_window` ×10 (buong leg) |
| FIX | 1 | −$2.08 | `momentum_reentry_chase_blocked` ×66 pagkatapos ng bailout |

**Ang conversion defect (fires→trades) ay FIXED.** Ang natitirang pera sa vertical ay
nasa KILALANG hiwalay na lever: post-bailout re-entry lockout (dokumentado na sa
project_ross_replay_scorecard_0709 "winner-killer = tight-trail + re-entry lockout"
at sa HUIZ 2nd-leg gap ng project_replay_ross_size_scaling_0821). Ngayon lang ito
NAGBI-BIND dahil ngayon lang nakaka-entry ang lane sa mga window na ito.

Ang punch-window hold ay hindi na-exercise ng WYHG replay (ang recorded NBBO ay ang
TOTOONG book; ang phantom 3131bps ay Alpaca-IEX cache artifact na hindi namo-model ng
replay) — unit tests + code-path review ang evidence; ang live phantom case ang
target nito.

HUIZ hot-window regression A/B (12:00-12:45Z; walang late band, walang binding
book-quality veto): **BYTE-IDENTICAL** ang dalawang arms — +$87.66 PnL, magkaparehong
2 entries / 6 exits (82@1.70, 200@1.77 → ladder hanggang 2.85) at magkaparehong event
histogram. Sa labas ng saklaw nila, ZERO ang epekto ng tatlong feature
(evolve-not-devolve).

## Surprises / deviations

- **#1093 nag-shift ng trigger identity**: ang flush structure ay pumuputok na ngayon
  bilang `double_bottom_break_tick_ok`, hindi `flush_dip_buy` — kaya idinagdag ang
  double-bottom pair sa dip family (nakadokumento sa constant kung bakit).
- Ang unang bersyon ng fresh-HOD test (`1 − k×ATR` na may 0.90 floor bilang
  pinaka-maluwag) ay HINDI bumukas sa WYHG mismo (session high 6.04 ay ~2h nang luma
  sa fire) — binaligtad sa `slack = min(0.25, max(0.10 base, k×ATR))`: ang 10%
  "near the highs" ang base, ATR ang nagpapalawak, 25% ang cap.
- NEXT_TASK.md ay stale (P1a mula 07-06) — HINDI minarkahang DONE; operator-directed
  ang task na ito.

## Deferred

- **Post-bailout re-entry lockout sa dip verticals** — ang bagong binding blocker
  (66× `momentum_reentry_chase_blocked` sa WYHG vertical). Hiwalay na lever, kailangan
  ng sarili niyang study (ito na ang susunod na pera).
- `live_entry_flow_veto` (7×) sa recovery leg — existing OFI protection; hindi ginalaw.
- Ang phantom-book ROOT (Alpaca-IEX cache na nagseserve ng 3131bps) — ang punch-hold
  ay belt; ang cache mismo ay hindi inayos dito.

## Open questions for Cowork

- Ang late-dip ×0.5 ay nagbubukas ng late-band placements sa lahat ng dip-family
  fires na malapit sa HOD — kung gusto nating mas maagresibo/konserbatibo, ang mga
  knob: `chili_momentum_late_dip_hod_slack_base_frac` (0.10),
  `_atr_k` (1.0), `_max_frac` (0.25), `_mult` (0.5), `_recent_bars` (4).
- Dapat bang palawigin ang punch-hold para saklawin din ang `live_entry_flow_veto`
  (flow ay tick-scale ding nagbabago)? Hindi ko ginawa — book-quality lang ang saklaw
  ngayon, konserbatibo.
