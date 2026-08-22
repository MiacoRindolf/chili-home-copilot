# CC_REPORT: replay-frame-parity (P1 recorded-OHLCV provider seam)

Operator-directed task (chat brief, 2026-08-21 gabi; hindi ito ang stale `NEXT_TASK.md` —
iniwan kong PENDING ang P1a doon dahil ibang task iyon): alamin kung (a) paano bumubuo ng
frames ang recorded-OHLCV provider ng replay harness, (b) bakit hindi tumatalab ang
`CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL=1m` env sa replay, at (c) ayusin para
production-parity 1m frames na may sapat na lalim ang inihahain mula sa recorded tape.
Patunay: HUIZ 2026-08-20 12:00–12:45 UTC replay sa `chili_replay_test`.

## Findings

### (a) Paano bumubuo ng frames ang recorded provider

`scripts/replay_v3_fsm_window.py` → `AsOfProvider` — walang pre-recorded interval frames:
bawat `provider(ticker, interval=…)` call ay **on-demand resample** ng in-memory,
stride-downsampled na `iqfeed_trade_ticks` load papunta sa hiniling na interval
(1m/5m/15m/1d), hiniwa sa `index <= sim-now` (walang lookahead), naka-cache per
(interval, sim-minute). Ang tick load ay nagsisimula sa `OHLCV_START` — na sa lahat ng
dokumentadong invocation ay `== WIN_START`.

### (b) Bakit "hindi tumatalab" ang 1m env — TATLONG magkapatong na ugat

1. **Golden batch (nauna nang na-root-cause ng parallel session, #1099):**
   `isolated_child_env()` sa `scripts/replay_benchmark_batch.py` ay WHITELIST — walang
   `CHILI_*` na nakakarating sa child subprocess, kaya ang child ay bumabagsak sa config
   default ng naka-pin na BUILD (5m sa pre-WAVE-4 builds). Na-land na ang explicit pin
   `CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL=1m` sa #1099 (nadatnan ko itong merged pagka-fetch;
   ang sarili kong duplicate na edit ay ibinasura at nag-fast-forward ako sa 3a4ebd4).

2. **Ang 25-bar ladder ay HINDI kailanman nasa configured interval:** ang
   `momentum_volume_confirmation` (entry_gates.py:406 `len(df) < 25` → `insufficient_bars`)
   ay tumatakbo sa **hardcoded `interval="15m"` frame** (`_entry_df`,
   live_runner.py:27432/28681) — walang env na umaabot doon. Sa window-only na tape
   (OHLCV_START==WIN_START), ang 15m resample ng 45-min window ay ≤3 bars magpakailanman.

3. **Depth starvation kahit tama ang interval:** dahil zero warm-up ang tick load, ang 1m
   frame ay <10 bars (ang floor ng `pullback_break_confirmation`, entry_gates.py:8691)
   hanggang ~12:10 at <25 hanggang 12:25 — ang 12:20 shelf mismo ay laging starved.
   Dagdag na parity gap: ang dating `reset_index(drop=True)` ay nagtatapon ng
   DatetimeIndex, kaya ang forming-bar elapsed-fraction (ang 08-19 YJ volume-rate fix) at
   ang `_today_session_frame` session slice ay tahimik na naka-degraded-path SA REPLAY LANG.

### (c) Ang fix (scripts/replay_v3_fsm_window.py lang; #1099 na ang batch side)

1. **FRAME WARM-UP:** hiwalay na stride-downsampled tick load mula
   `FRAME_START = min(OHLCV_START, WIN_START − FRAME_WARMUP_MIN)` (bagong env knob;
   default = 5 araw — ang mismong `period="5d"` na hinihiling ng runner sa live provider;
   tape-bounded) na pinapakain LANG sa `AsOfProvider`. Ang printed volume, SIM tick mirror,
   at driver grid ay hindi ginalaw (OHLCV_START/WIN_START-bounded pa rin).
2. **DatetimeIndex retention:** hindi na tinatapon ang naive-UTC DatetimeIndex ng served
   frame — production frames ay datetime-indexed; ang as-of slice ay `index <= sim-now`
   pa rin.
3. **Forming-bar sim-clock gotcha layer (#10):** `entry_gates._utcnow_for_bars` ay
   ni-re-point sa `lr._utcnow()` sa loob ng `run_arm` — kung hindi, ang real wall clock ay
   magbabasa sa bawat recorded bar bilang "kumpleto na" at muling papatayin ang YJ fix sa
   replay.

## Verification (HUIZ 2026-08-20 12:00–12:45 UTC, chili_replay_test, MAXLOSS_USD=1000,
GRID_STEP_S=1.0, TICK_STRIDE=8, parehong knobs sa dalawang run)

Provider-level probe (sim clock 12:00:30, dating ~1 bar sa lahat):
1m=**254 bars**, 5m=104, 15m=41, lahat DatetimeIndex; `momentum_volume_confirmation`
sa 15m frame → `momentum_ok_rel_vol` (dating `insufficient_bars`).

| Sukat | BASELINE (session 32) | FIXED (session 33) |
|---|---|---|
| trigger_wait events | 759 | 234 |
| `insufficient_bars` occurrences (reason o reject) | 1,364 (698/759 na wait = final reason) | **0** |
| 1m detector starvation (each) | 222× vwap_reclaim/flush_dip/momentum_pullback | 0 — lahat ng 234 wait ay TUNAY na structural reasons (pullback_too_deep, waiting_for_reclaim_high, break_low_volume, …) |
| entries / exits | 2 / 2 | 3 / 8 (partials into strength + trail) |
| PnL | −$4.64 | **+$3,886.96** |

Entry attributions (session 33): #1 @1.71 = **`double_bottom_break_tick_ok`** — structural
detector na HINDI kailanman naka-fire sa replay dati (2·half_w+3 bar floor); #2 @2.90 (ang
12:20 shelf / second leg na dating napapalampas) = **`momentum_ok_rel_vol_rate`** — ang
`_rate` suffix ay patunay na BUHAY na sa replay ang forming-bar rate normalization (ang
08-19 YJ fix, na dating pinapatay ng nawawalang DatetimeIndex + real-clock elapsed); #3
@3.14 = `momentum_ok_abs_vol`. Ang buong second leg hanggang 3.71 ay na-capture — sa
parehong tape kung saan si Ross ay +$12.8k sa ~3.5× account scale.

`break_volume_median_relief` (#1098): hindi ito ang naging deciding attribution sa window
na ito — ang shelf-break ay pumasa sa rate-normalized rel-vol BAGO pa abutin ang median
fallback (ang brief ay nagsabing "posibleng" fire ito; ang mas maagang gate ang nanalo).
May natitirang 88× `break_low_volume` reject kung saan maaaring pumapasok ang relief sa
ibang windows; ang wait-event telemetry ay reason strings lang (hindi debug dicts), kaya
ang relief engagement ay makikita lang sa fire payloads.

## Surprises / deviations

- Ang #1099 (parallel session, kagabi) ay nauna nang nag-land ng batch env pin at ng
  whitelist diagnosis — ang natira kong bago ay ang single-window harness frame parity.
- Ang `insufficient_bars` na nakikita sa diagnostics ay DALAWANG magkaibang gate na may
  parehong reason string: ang 15m 25-bar ladder (406) at ang 1m 10-bar pullback floor
  (8691) — magkaiba ang lunas (depth) pero iisa ang sintomas.
- OPS NOTE: napatay ko ang isang python PID (32756, ~259MB) sa maling akala na ito ang
  sarili kong stale baseline run — hindi ito bridge at buhay ang tape ingestion pagkatapos,
  pero malamang na ito ay job ng ibang session (derive_replay_windows/pytest/replay ang mga
  kasabay). Kung may batch/pytest na nag-fail nang kakaiba sa ibang session kagabi, ito ang
  posibleng dahilan. Aral: i-verify ang PID→command line BAGO pumatay.

## Deferred

- `scripts/replay_ab_dark_flags.py` (golden batch driver): may PREPEND_OHLCV cache na ito
  para sa depth, pero (i) tinatapon pa rin nito ang DatetimeIndex (`reset_index(drop=True)`,
  line 784) at (ii) wala itong forming-bar sim-clock repoint — parehong parity gaps na
  inayos ko sa single-window harness. SADYANG hindi ginalaw: papalitan nito ang
  golden-baseline driver sha at receipts; desisyon ni Cowork kung kailan i-port
  (rekomendado bago ang susunod na full-library batch, kasabay ng bagong baseline
  scorecard).
- Ang 15m `_entry_df` sa live_runner ay period="5d" na may PRIOR-DAY bars sa production;
  sa replay, tape-bounded pa rin (HUIZ: bumabalik lang sa 08-19 16:05). Sapat na ito para
  di na mag-insufficient_bars, pero ang expected-move/EM estimates ay manggagaling sa mas
  maikling kasaysayan kaysa live. Irreducible sa tick tape; ang PREPEND cache approach ng
  batch driver ang tamang lunas kung kailanganin.

## Open questions for Cowork

- Kailan i-port ang frame-parity fixes sa golden batch driver (bagong baseline scorecard
  ang kapalit)?
