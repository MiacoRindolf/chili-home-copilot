# Golden library + benchmark scorecard — 2026-07-26 (Linggo)

STATUS: BASELINE BANKED (15 windows; ang VTAK/JLHL/VEEE ay tumatakbo pa sa gabi at dadagdag
sa isang regeneration — ang utos sa ibaba; ang 1:1-density giants ay nakapila sa heavy-night):

```bash
python scripts/replay_scorecard.py \
  --results D:/CHILI-Docker/chili-data/replay_batch/results.jsonl \
  --meta D:/CHILI-Docker/chili-data/replay_batch/meta.json \
  --manifest project_ws/AgentOps/ross_video_evidence/manifest.json \
  --library-manifest D:/CHILI-Docker/chili-data/replay_batch/window_manifest.json \
  --db postgresql://chili:chili@localhost:5433/chili \
  --out-json D:/CHILI-Docker/chili-data/replay_batch/scorecard.json \
  --out-md D:/CHILI-Docker/chili-data/replay_batch/scorecard.md
```

## 1. Ano ito

Ang 30-araw na pruner ay kumakain ng isang araw ng recorded tape kada araw — bawat window na
hindi na-pin ay hindi na maibabalik (nawala na ang canonical JEM 07-09 nang ganito; ngayong
araw mismo, nasaksihan naming kainin ng pruner ang ZDAI 06-26 habang tumatakbo ang rescue —
29 na tick na lang sa 60,306 ng census). Ang programa ngayong Linggo:

1. **RESCUE** — pin ang LAHAT ng 219 buhay na high-value windows (78 GOLD ≥100k ticks/≥10k
   nbbo + 141 SILVER ≥20k/≥2k, 07-25 census) sa pruner-immune na `replay_golden_ticks` /
   `replay_golden_nbbo` (~8.9GB kasama ang indexes), may census verify + disaster-recovery dump.
2. **BENCHMARK RUNNER** — permanenteng N-window regression instrument: bawat window ay
   dumadaan sa TUNAY na FSM (`scripts/replay_ab_dark_flags.py`, GOLDEN=1, ARM=base,
   driver-default equity/exec para sa tie-back sa banked canonical numbers), sequential na may
   sink mining kada run + resumable JSONL + STOP_AT deadline guard.
3. **ROSS GROUND TRUTH** — pinagsamang manifest ng frame-verified evidence (19 trades, AUDIT
   batches 1–4) + transcript extractions (lumang 6 na araw + 9 SARIWANG recaps na
   dinownload+tinranscribe ngayong araw) na may per-field confidence.
4. **BASELINE SCORECARD** — unang systematic na sukat ng Ross-capture sa buong buwan ng
   windows: per-window PnL, per-setup attribution, Stage-0-style replay expectancy, at ang
   canonical ①②③ table (ROSS_CAPTURE_PARITY format).

## 2. Mga file / instrumento (lahat merged sa main)

| File | Papel |
|---|---|
| `scripts/data/golden_harvest_inventory.json` | 07-25 census ng 219 windows bilang data |
| `scripts/harvest_golden_windows.py` | urgency-ordered batch pin + verify + census md + dump |
| `scripts/derive_replay_windows.py` | deterministic burst-window derivation + baseline-tier manifest |
| `scripts/replay_benchmark_batch.py` | sequential GOLDEN=1 runner, sink mining, resumable JSONL, STOP_AT guard |
| `scripts/replay_scorecard.py` | Label A/B capture + expectancy + ①②③ crossref generator |
| `scripts/fetch_ross_recap.py` | recap audio → timestamped Whisper transcript sa evidence tree |
| `scripts/build_ross_manifest.py` | curated + transcript merge → ground-truth manifest |
| `tests/test_replay_scorecard.py` | 8 pure tests (pairing/capture/Stage-0/verdict/render) |

## 3. Harvest resulta — ✅ KUMPLETO

- **222/222 windows pinned: 221 OK, 1 short_ticks, 0 lost** (2.9h run, sequential,
  isang transaction bawat window)
- Archive: `replay_golden_ticks` **5.71GB** + `replay_golden_nbbo` **3.71GB** = 9.42GB
  (E: 361.8GB pa ang libre); per-window census sa
  `D:\CHILI-Docker\chili-data\replay_batch\golden_census_2026-07-26.md`
- Disaster-recovery dump: `E:\chili-backups\golden_windows_202607.dump` **628.6MB**
  (pg_dump -Fc sa loob ng container, verified ng `pg_restore --list`, atomic swap)
- **ZDAI 06-26 loss (ang short_ticks)**: nasaksihan naming tumakbo ang pruner DELETE habang
  nagre-rescue — 29 ticks na lang sa 60,306 ng census nang maabot ang ZDAI (buo pa ang 60,502
  nbbo rows). Ito rin ang window ng frame-verified na **Ross −$25,431.11** loss — nawalan tayo
  ng replay tape para sa pinakamalaking dokumentadong Ross loss ng buwan, sa mismong araw na
  itinayo ang archive. Ang UPC 06-26 (una sa urgency order) ay nailigtas nang buo minuto-oras
  bago ang DELETE. Wala nang ganitong mawawala muli — pruner-immune na ang golden tables at
  ang `pin_replay_window.py` same-day habit ang gate sa mga bagong canonical windows.

## 4. Canonical smoke (chain-integrity proof) — ✅ PASADO

| Window | Banked (07-25) | Ngayon (GOLDEN=1, sa runner) | Verdict |
|---|---|---|---|
| PLSM 07-13 | 0.00 / 0 entries / 0 exits | 0.00 / 0 / 0 | **EXACT** |
| QTTB 07-13 | −24.18 / 3 / 3 | −22.52 / 3 / 3 | istruktura EXACT; PnL delta IPINALIWANAG (sa ibaba) |
| QTTB re-run (determinism) | — | **−22.52 / 3 / 3, byte-identical fills** (20@19.36, 35@18.97, 54@19.05) | **DETERMINISTIC** |

**Ang QTTB $1.66 delta**: ang banked log (`QTTB_base_main.log`) ay ginawa 07-25 **08:34 AM**
— 14 oras BAGO ang tie-stable `ORDER BY (observed_at, id)` fix (9902fde, 07-25 22:41). Ang
delta ay isang equal-timestamp NBBO tie sa UNANG entry lang (fill 29@19.28 → 20@19.36; parehong
trigger, parehong exit 18.90; trades 2–3 byte-identical sa dalawang run). Walang behavioral
drift mula sa #944/#946. **Ang −22.52 ang canonical QTTB base baseline pasulong** (deterministic,
napatunayan sa dalawang magkaibang commit: 79a94de at bf4cdd7).

**CLRO 07-02 in-batch tie-back — ✅ EXACT**: ang batch run ay nagbalik ng **−11.37 / 4 / 4**,
eksaktong tugma sa dokumentadong golden==live parity reference ng 07-25 determinism proof.
Ang mas lumang `GP_golden_ordered.log` (−3.72/1) ay napatunayang CONTAMINATED: identical grid
(3359 steps) at mirror (158,587 ticks), pero may `SUSPECTED HALT (print-recency)` events —
nag-lag ang mirror write sa ilalim ng DB contention noong gabing iyon kaya "tumahimik" ang tape
sa mata ng FSM → halt lifecycle → ibang trade path. **Operational gotcha na naka-record**: huwag
patakbuhin ang replays kasabay ng mabibigat na DB jobs; ang print-recency halt detector ay
sensitibo sa mirror-lag, hindi lang sa tunay na halts.

## 5. Baseline run — 15 OK banked (+3 in flight, 4 giants deferred)

| Window | PnL | e/x | vs Ross |
|---|---|---|---|
| ZDAI 06-26 | 0.00 | 0/0 | tape pruned (Ross −$25,431 day) |
| SVRE 06-30 | −62.03 | 1/1 | Ross +$16,063 ③❌ |
| VWAV 06-30 | −7.09 | 2/3 | Ross +$10,799 ③❌ |
| CANF 07-01 | −68.22 | 4/5 | Ross +$7,371 (main) ③❌ |
| JEM 07-01 | −29.61 | 2/3 | ②❌ |
| TC 07-01 | −5.55 | 4/5 | ②❌ |
| CETX 07-02 | −160.35 | 2/2 | Ross +$8,917 ③❌ |
| CLRO 07-02 ⚓ | −11.37 | 4/4 | Ross +$2,943 (small) ③❌ |
| LHSW 07-06 | **−253.12** | **20/18** | churn signature |
| CLRO 07-07 | −25.84 | 15/16 | Ross +$3,284 ③❌ + churn |
| SILO 07-07 | −177.30 | 6/6 | Ross +$477 ③❌ |
| NVVE 07-08 | −2.57 | 3/3 | 3× instant bailout |
| VRAX 07-09 | −7.65 | 5/5 | Ross +$19k(main) ③❌ |
| PLSM 07-13 ⚓ | 0.00 | 0/0 | Ross window labas sa replay (∅) |
| QTTB 07-13 ⚓ | −22.52 | 3/3 | — |

⚓ = canonical tie-back (lahat pumasa — §4). Kabuuan: **net −833.02 sa 74 round trips**.

## 6. Scorecard highlights

- **①②③ verdict tally (18 graded)**: 0 ①✅ · 2 ①❌ · 0 ②✅¹ · 4 ②❌ · **12 ③❌** · 12 ∅
  (window_not_covered). ¹ang 2 ②✅ ng ZDAI ay tape-artifact (pruned), hindi binibilang.
  **ANG TEMA: pumapasok si CHILI sa tamang araw pero talo kung saan nanalo si Ross — ang gap
  ay OUTCOME/conversion, hindi admission.** Eksaktong kumpirmasyon (ngayon may sukat na) ng
  parity-program thesis.
- **Per-setup (may attribution ang huling windows)**: flag/abcd ang TANGING net-positive
  family (+18.24, 4W/6L); "unknown" −808 sa 52 trades (mga naunang window bago ang trace fix)
- **Replay expectancy (deltas only)**: n=74, PF 0.30, win rate 24%, 4 winners ≥1.5R (GREEN),
  avg loser 1.00R (red), empirical 1R = $21.70
- **Churn signature**: LHSW 20 entries / CLRO-0707 15 entries — paulit-ulit na re-entry sa
  chop; NVVE 3× sub-second bailouts — ang bailout/re-entry cadence ang pinakamalinaw na
  na-expose na behavioral lever
- **Top binding rejects (pinagsama)**: vwap_reclaim:waiting (19.5k), pullback_too_deep
  (14.3k), vwap_not_below_enough (10.4k), flush_dip past_morning_window (10.3k) /
  not_front_side (9.9k)
- Label A (within-trade MFE): hindi pa available — ang sink event clocks ay sim-run-anchored;
  kailangan ng driver clock-offset emit (nakalista sa §9)

## 7. Ross recap harvest (sariwang ground truth)

- **9/9 sariwang recaps** (07-20..07-24) na-download + Whisper-transcribed + na-extract
  (verbatim P&L kung saan stated: LABT day +$40k main / +$2,242.11 small; EHGO +$41k main /
  +$1,868.92 small; ZCMD −$12,548.45; atbp.)
- Manifest kabuuan: **148 windows (49 trade / 99 reject), 18 frame_verified** broker-panel
  dollar rows
- Fresh-tape crossref: ang JZXN 07-21 at EQPT 07-23 (ang tanging may-tape na sariwang araw) ay
  HINDI binanggit ni Ross kailanman; ang 07-21 ay no-trade day niya. Ang 07-23 ay may 3
  malalaking main-account Ross trades (JEM/ZCMD/EHGO) na WALANG CHILI tape — ang capture
  uptime ay measurement precondition (nasa Lunes checklist ang service revival)

## 8. Mga natutunan / gotchas (2 tunay na live-relevant bugs ang nahuli ng benchmark sa unang araw)

- **NaN-poisoning (live-relevant P1)**: unguarded `float(vr[cur])` sa 14 vol_ratio stamp
  sites → NaN sa `risk_snapshot_json` → tinatanggihan ng Postgres JSONB → namamatay ang
  session UPDATE. TATLONG patong ang kinailangan: 3-site sanitize (whack-a-mole, kulang) →
  engine-level `json_serializer` sa `app/db.py` (protektado na ang LIVE service) → ang
  SARILING sink engine ng driver (:378). Bonus discovery: ang `NaN < mult = False` ay tahimik
  na PUMAPASA sa conviction volume gates — hiwalay na lever na may sariling session (chip).
- **Docker /dev/shm 64MB DiskFull**: ang parallel VACUUM (DSM segments) + parallel gather sa
  lumaking golden tables ay humihingi ng 57–67MB — hindi kasya KAILANMAN. Fix: `VACUUM
  (ANALYZE, PARALLEL 0)` + `SET max_parallel_workers_per_gather=0` sa driver (session-scoped,
  walang container restart bago ang Lunes). Durable fix = `shm_size` sa docker-compose
  (post-Monday, kailangan ng recreate).
- **Runtime ay eventfulness-dependent, hindi lang density**: 1:1-density windows
  (06-29/06-30/07-01 bridge-sourced) >0.008s/tick; normal ~0.0025–0.0075. Ang apat na giants
  (UPC/TNMG 06-29, CELZ/JEM 06-30) ay dedicated heavy-night job.
- **Mirror-lag = pekeng halt**: ang print-recency halt detector ay nag-fire sa isang
  contended run ("tape went silent") → ibang trade path. Isang replay lang kada pagkakataon,
  walang kasabay na mabibigat na DB job.
- Ang pruner DELETE ay live habang nagre-rescue — ang urgency-first pin order ang nagligtas
  sa UPC 06-26; ang ZDAI 06-26 ay naabutan (29/60,306 ticks na lang).
- Derived burst windows ng gappers ay PREMARKET-leaning (QTTB derived 10:44Z vs hand-picked
  13:00Z) — canonicals nananatiling hand-picked; ang premarket-window axis ay deliberate na
  susunod na expansion.
- ENTRY_DIAG trace ay dating silent no-op (json.loads sa JSONB dict); fill-event mining ay
  substring-match (`entry_filled`/`exit_filled`) at ang trigger reason ay nasa
  `live_entry_candidate_detected`.

## 9. Susunod

1. **Heavy night**: patakbuhin ang 4 na 1:1-density giants (+ anumang deadline-cut) —
   resumable: `replay_benchmark_batch.py --tiers baseline --retry-errors`
2. **Library tier**: ~200 natitirang windows sa mga susunod na gabi (unattended, STOP_AT
   guard) — parehong utos, `--tiers baseline,library`
3. **Per-PR delta workflow**: bawat behavior lever mula ngayon = baseline vs treatment sa
   library subset, `ARM`/`FLAGS_JSON` na env ng runner + scorecard diff
4. **Label A enable**: driver emit ng sim-clock↔window-clock offset → scorecard maps fill
   timestamps → within-trade MFE capture per trade
5. **Bailout/re-entry cadence lever** (ang pinakamalinaw na behavioral finding: LHSW 20e /
   CLRO-0707 15e churn, NVVE sub-second bailouts) — kandidato sa susunod na parity PR
6. `shm_size` bump sa docker-compose post-Monday; scheduler redeploy sa post-#946 main
   (dati nang nakapila)
7. Lunes: activation watch (04:15 PT fire; walang ginalaw ang gabi na ito sa sealed chain)
