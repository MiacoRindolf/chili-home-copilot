# CC_REPORT: squeeze-regime-index

> Operator-directed task (2026-08-22, hindi mula sa NEXT_TASK.md — ang NEXT_TASK ay
> lumang P1a brief na hindi ko ginalaw). Pinagmulan: Ross "Forcing a Crash"
> (U3TQJxaDZus) ANALYSIS.md candidate **#3** — Suppression/Squeeze Regime Index (S6).

## What shipped

- **PR #1103**, squash-merged sa main = `7f5ade9` (base `8874b48`). 7 files, +828/−2.
- **Bagong module** `app/services/trading/momentum_neural/squeeze_regime.py`:
  - Kada UTC na araw mula `momentum_viability_history.change_pct`: bilang ng
    **+50%** at **+100%** movers; **HELD** (huling obserbasyon ng araw ≥ ½ ng max
    gain) vs **FADED** (round trip); **UNRESOLVED** kapag binitawan ng scanner ang
    mover bago ang huling ¼ ng day span (hindi hinuhulaan ang hindi nakita).
  - **FTD aggregate** (opsyonal, mula `sec_fails_to_deliver` #1102): market-total
    fails + subset sa mga mover ng araw sa pinakabagong settlement date —
    **naka-record lang, HINDI bahagi ng label** (~30-araw lag ng SEC files).
  - **Label** (suppression | neutral | squeeze): 5-araw window vs 20-araw baseline,
    p20/p80 percentile symmetry (parehong documented base ng breadth regime).
    Reasons: `squeeze_hold`, `squeeze_emergent` (patay na baseline → biglang may
    nagho-hold na movers — ang post-May-11 na hugis), `suppression_fade`,
    `suppression_vanish`, `mixed`/`thin_baseline`/`no_daily_rows` → neutral.
- **Migration 369**: `momentum_squeeze_regime_daily` (1 row/araw, walang ORM —
  raw-SQL pattern gaya ng `sec_fails_to_deliver`) + partial covering index
  `ix_mvh_mover_scan` (mig363 pattern) — index-only ang per-day mover scan.
- **`BreadthRegime`**: bagong `squeeze_label/reason/hold_ratio/window_movers`
  fields (defaults sa dulo = byte-identical ang lahat ng positional constructions);
  attach sa cached front lang (`_attach_squeeze_axis`, LIMIT-25 PK read na may
  300s memo) — hindi ginalaw ang `_compute_breadth_regime_uncached` na
  ini-inspect ng single-flight test.
- **Persistence stamp**: `regime_snapshot_json["squeeze_regime"]` sa bawat
  viability row → dadaloy sa `entry_regime_snapshot_json` sa fill.
- **Scheduler**: daily refresh **3:10 AM LA** (pagkatapos ng FTD ingest 3:00),
  idempotent 28-araw backfill (30d history retention − 2d na margin laban sa
  partial-prune).
- **Kill-switch**: `CHILI_MOMENTUM_SQUEEZE_REGIME_ENABLED` (default **True** —
  LIVE+ON). False = walang job, walang stamp, neutral, byte-identical.
- **OBSERVABILITY-ONLY** ayon sa brief: naka-log (`[squeeze_regime]`) at
  naka-persist; **walang sizing/selection na nagbabasa** ng label sa phase na ito.

## Verification

- **Tests**: 11 bago (`tests/test_squeeze_regime.py`) + 28 regression
  (`test_momentum_breadth_regime` 5, `test_breadth_regime_single_flight` 4,
  `test_ftd_ingest` 6, `test_momentum_neural_persistence` 9,
  `test_viability_history_append` 3) — **39/39 PASADO**. Migration-ID check OK
  (356 migrations, walang collision).
- ⚠️ Tumakbo sa hiwalay na **`chili_squeeze_test`** DB (ginawa ko) — may
  tumatakbong replay batch (`replay_ab_dark_flags` + `replay_benchmark_batch`)
  sa `chili_test` nang mag-run ako; ang isolation rule ay tumutukoy sa mismong DB,
  hindi sa pangalan.
- **Live seed (Sabado, market closed)**: itinayo ang `ix_mvh_mover_scan` sa live
  `chili` nang **CREATE INDEX CONCURRENTLY** (mig363 precedent; magiging no-op
  ang mig369 sa susunod na startup), tapos pinatakbo ang 28-araw backfill:
  **lahat ng 28 araw na may datos ay na-compute** (hal. 08-19: 29 movers / 7 held
  / 15 faded; 07-30: 39 movers / 27 held). FTD columns populated (hal. 174.6M
  market-wide fails, 2.7–10.4M sa mga mover ng araw).
- **Kasalukuyang live label**: `neutral (mixed)`, window hold_ratio **0.404**,
  114 movers_50 / 49 movers_100 sa huling 5 araw, as_of 2026-08-21.

## Surprises / deviations

- **Malakas ang FADE sa pinakahuling mga araw** (08-19: 7/15, 08-20: 5/17
  held/faded) laban sa mas mahold na late-July (07-30: 27/4) — ang label ay
  neutral pero ang hold-ratio trend ay pababa. Ito mismo ang klase ng signal na
  gustong makita ng observability phase bago mag-tilt.
- **May weekend rows ang viability history** (08-15/16, 08-08/09: tig-4 na
  "mover" na lahat held) — malamang stale weekend boards. Maliit ang epekto sa
  percentile math, pero kandidatong i-exclude ang non-trading days sa susunod na
  phase kung lumabo ang baseline.
- Ang unresolved bucket ay malaki sa ilang araw (07-31: 21/35) — ang scanner ay
  madalas humihinto sa pag-track ng mover bago mag-EOD. Dahilan kung bakit
  TAPAT (hindi hinuhulaan) ang held/faded split; nire-report ang unresolved
  bilang sariling column.

## Deferred

- **Tilt wiring** (suppression → mas maagang profit-taking + mas mahigpit na
  entry bar; squeeze → mas mahabang holds + add ladders) — tahasang out-of-scope
  ng brief; kailangan muna ng ilang araw ng live label data.
- FTD sa label — nakalagay bilang observability columns lang; desisyon ni Cowork
  kung papasok ito sa label kapag may sapat nang kasaysayan.
- Weekend/non-trading-day exclusion sa daily rows (tingnan ang Surprises).

## Open questions for Cowork

1. Ilang araw ng live label observation bago ang tilt-wiring phase? (Mungkahi:
   isang linggo ng `[squeeze_regime]` lines + daily rows, tapos i-korelate sa
   sarili nating entry outcomes bago magdisenyo ng tilt.)
2. Dapat bang mag-emit din ng label ang morning brief / operator readouts, o
   sapat na ang log + `momentum_squeeze_regime_daily` sa ngayon?
