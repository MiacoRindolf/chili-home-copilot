# Golden library + benchmark scorecard — 2026-07-26 (Linggo)

STATUS: IN PROGRESS — ang mga bilang sa dokumentong ito ay pinal na kapag naalis ang linyang ito;
ang scorecard mismo ay regenerable anumang oras:

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

## 5. Baseline run

(PUNAN: bilang ng windows attempted/ok, kabuuang oras, top-line PnL, entry rate)

## 6. Scorecard highlights

(PUNAN: per-setup table, expectancy gates, Label A/B, ①②③ crossref, top-5 gap ranking)

## 7. Ross recap harvest (sariwang ground truth)

(PUNAN: 9 videos transcribed, trades extracted, manifest row counts, JZXN 07-21 / EQPT 07-23
crossref status)

## 8. Mga natutunan / gotchas

- Ang pruner DELETE ay live habang nagre-rescue — ang urgency-first na pin order ang nagligtas
  sa UPC 06-26 (nauna sa DELETE) habang ang ZDAI 06-26 ay naabutan.
- Re-verify ng napin nang malalaking window ay ~5.5k rows/s (anti-join heap fetches) — ang
  idempotent re-cover ay hindi libre sa malalaking window; one-time cost lang.
- Ang derived burst windows ng mga gapper ay lumilitaw sa PREMARKET (hal. QTTB derived
  10:44–13:29Z vs hand-picked 13:00–16:00Z) — ang canonicals ay nananatiling hand-picked para
  sa tie-back; ang premarket-vs-session window choice ay isang deliberate na axis para sa
  susunod na library expansion.

## 9. Susunod

(PUNAN: library-tier runs sa mga susunod na gabi, per-PR before/after delta workflow sa
scorecard, Lunes activation watch)
