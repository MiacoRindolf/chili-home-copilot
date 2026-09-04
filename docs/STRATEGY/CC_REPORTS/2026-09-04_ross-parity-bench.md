# 2026-09-04 — Ross Parity Bench: instrumento bago lever

STATUS: Phase 0 sinimulan. 4 PR bukas, 0 naka-merge. Walang FSM code change.
PLANO: `~/.claude/plans/magical-whistling-candle.md` (aprubado 09-04 02:35Z).
SAKLAW: gabi ng 09-03 post-close hanggang 09-04 06:10Z.

## Ang tanong ng operator

> "Kapag ni-rehydrate ko ang mga araw na panalo ni Ross, makukuha ba ni CHILI lahat ng panalong
> trade niya at maiiwasan ang mga talo?"

Iyon ay **benchmark**, hindi fix. Kaya ang ginawa ngayong gabi ay instrumento at sukat, hindi
pagbabago ng threshold. Walang gate ang ginalaw.

## Ang sukat na sumasagot sa tanong ngayon

79 video → 187 Ross trade → 68 symbol-day na may DB truth ng CHILI:

| | |
|---|---|
| Ross, 68 symbol-day | +$428,050 |
| CHILI, parehong 68 | $0.00 |
| Bahaging UPTIME (48/68) | $321,000 = 71% |
| Ross sa 8 kasong may tunay na gate na desisyon ng CHILI | **−$13,190** |

Ang unang pagbasa ay hindi "mas magaling siyang mangalakal". Sa mga araw na buhay si CHILI at
nagpasya talaga, **nalugi si Ross**. Ang agwat ay nasa pagiging buhay at nasa pagkakita, hindi
sa paghatol. Iyon ang nagtakda ng pagkakasunod ng plano.

Artifact ng scorecard: https://claude.ai/code/artifact/3ba51e57-ec51-4084-b0e7-819086e7334a

## Apat na PR

Lahat ay seam branch mula `origin/main` sa `E:\dev\wt-seams` at `E:\dev\wt-bench`. Wala pang merge.

- **#1305** `seam/tick-retention-job-0903` — 14-araw na pk-range prune para sa `iqfeed_trade_ticks`
  (94 GB, walang retention) + migration 375 na nagtataas ng autovacuum aggressiveness sa 4
  append-heavy na table. 32 test. Dalawang adversarial round; ang safety lens ng round 1 ang
  nakadiskubre ng late-arriving-print na katangian ng bridge, kaya 3 guard ang nadagdag.
- **#1306** `seam/nightly-replay-child-log-0904` — pine-persist na ng nightly replay ang buong
  stdout/stderr ng anak, ang exit code, at ang elapsed time, at iniuulat ang unang traceback.
  Ito ang R3 ng plano. Ang pangangailangan ay napatunayan ng insidente sa ibaba.
- **#1307** `seam/rossbench-instrument-0904` — sini-mirror na ang NBBO tape papasok sa replay sink,
  sina-salaan ang provenance ng tape, iginagalang ang `EXEC_FAMILY`, at naglalabas ng run receipt.
- **#1308** `seam/admission-ignition-signal-0904` — pinahihintulutan ang ignition payload na
  patunayan ang sariling Ross universe.

## Ang natuklasan na nagbago ng pagkakasunod: sarado ang discovery loop

Bawat pinagmumulan ng "event" — ang WS ignition loop, ang `IgnitionDetector` ng bridge, ang
`pg_notify` consumer, ang tape-delta feeder, ang hot-mover hint job — ay makakaputok LAMANG sa
simbolong nasa `build_equity_universe` na. At iyon ay isang poll ng snapshot na naka-gate sa
5% na pagbabago. **Walang daan para sa simbolong hindi pa na-screen na makagawa ng event.**

Ang kongkretong bunga: **zero `ross_event_admitted` sa 3,379 live session sa 14 araw.** Hindi
kailanman naka-admit ang ignition path. Ang sanhi ay isang linya — ang caller ay nagpapasa ng
`signal=None` at itinatapon ang presyo, ang 60-segundong pagbabago, at ang dollar volume na
dala mismo ng nomination payload, kaya ang universe proof ay palaging bumabagsak nang sarado.
Iyon ang #1308.

## Tatlong beses ngayong gabi: berdeng ilaw sa ibabaw ng patay na proseso

Ito ang pinakamahalagang pattern ng gabi, at ito ang dahilan kung bakit R3 ang unang wave.

1. **Ang araw-araw na Ross lane ay patay nang tahimik** — ang monitor mula 08-24, ang prestream
   report mula 07-02. Parehong `Ready`, parehong rc=0. Ang PowerShell ay naglulunsad ng python na
   agad namamatay: walang working directory kaya hindi mahanap ng pydantic ang `.env`, at ang
   redirect ay pipe na walang pump kaya 0 byte ang log. Naayos; ang patunay ay isang sariwang
   13 KB na prestream report pagkatapos ng 63 araw, na naglilista ng 10 napangalanang blocker.
2. **Ang nightly replay noong 09-03** ay nag-ulat ng 4 mover, 0 fill, sampung segundo bawat isa.
   Mukhang resulta ng estratehiya. Hindi. Ang replay ay namatay sa 176 byte dahil kulang ng isang
   talahanayan ang sink DB, at pina-parse lang ng ulat ang stdout para sa fill at PnL habang
   itinatapon ang natitira. Naayos ang sink; tumakbo nang buo ang parehong window.
3. **Dalawang compaction script ko** ang lumabas nang tahimik na walang epekto at walang log.
   Ang sanhi ay UTF-8 na walang BOM: binabasa ng PowerShell 5.1 ang ganoong `.ps1` gamit ang ANSI
   code page, at isang em-dash ang nagiging tatlong character na ang huli ay curly quote, kaya
   nasira ang parse bago pa tumakbo ang unang linya.

## Mga pagkakamali ko, at ang naitama

Itinatala ko ang mga ito dahil bawat isa ay nagbago ng pamantayan ng pagsusuri para sa natitirang gabi.

- **Nag-ulat ako ng maling presyo sa 9.8% ng bench event.** Inihambing ko ang session HIGH sa
  kasalukuyang saklaw. Ang high ay *nararapat* na mas mataas. Ang tunay na rate ay 0.6%, at
  pareho itong after-hours at manipis. Walang bug.
- **Dalawang premise ko tungkol sa `live_arm_expired` ay pinabulaanan ng ipinatupad na sukat.**
  Hindi nito hinihinto ang arming sa buong lane: 0 sa 16 row ang nagbabago ng klasipikasyon.
  At ang "8 zero-P&L row" ay may submission evidence, at apat ay may broker P&L na −$442.46.
  Ang tamang saklaw ay label correctness para sa learner, hindi loss-guard fix.
- **Dalawang beses akong nagkamali sa "binary ≠ tree".** Ang nasukat na katotohanan: tugma ang
  image sha sa `git show`, walang bind mount, at ang wrapper string ay nasa D: tree lamang.
  Hindi kailangan ang rebuild ng container. Ang label ay build argument; ang nilalaman ang patunay.
- **Mali ang premise ko sa VHDX.** Ang compaction ay 382.4 → 121.0 GB pero hindi tumaas ang libreng
  espasyo sa C:. Sparse ang file; lohikal lang ang 382 GB.
- **Ako ang nakasira ng Docker.** Ang compaction script ko ay naglunsad ng Docker Desktop mula sa
  elevated context, kaya may mga socket na pag-aari ng admin at hindi mabura ng normal na Docker.
  Isa-isa silang humarang sa bawat restart. Labing-isang beses nang nangyari ito sa makinang ito
  mula pa Hunyo at wala ito sa recovery ladder. Nailagay na.

## Maintenance

DELETE ng 161M row, tapos VACUUM FULL sa dalawang tape. **DB 231 → 150 GB**, ticks 96 → 35 GB.
Bumalik ang Docker engine sa 29.5.2, malusog ang Postgres, tatlong container at dalawang bridge.

Isang bagay ang lumitaw sa dulo at may kabuluhan sa launch: **hindi nag-a-ANALYZE ang VACUUM FULL.**
Lahat ng istatistika ng tape table ay naka-reset — zero na tinatayang row sa isang talahanayang may
89.6M. Ang isang simpleng bilang sa dalawang araw ay nag-timeout sa 60 segundo. Pinatakbo ang
ANALYZE; 15 segundo, at tama na ang tantiya.

Hindi ko pinatakbo ang kinanselang `pg_dump`. Tumatagal ito ng halos tatlong oras sa mga nakaraang
gabi, at tatakbo iyon nang tuwid sa Codex preflight at sa launch. Iyon mismo ang ipinagbabawal ng
sariling market-window guard ng script. Ang huling matagumpay na dump ay 09-02 at 14 ang nasa disk.
Ang susunod na naka-iskedyul ay 17:30 PT.

## Susunod

Phase 0.1 ang libro at ang label ng learner. Tapos ang natitirang R1–R5 ayon sa nasukat na dolyar
sa divergence ledger, hindi ayon sa hinala.


---

# Ikalawang bahagi — ang araw na naipadala ang lahat (hanggang 20:00Z)

STATUS: 10 PR naka-merge sa `main`. Bench TAPOS. Lane buhay sa bagong build.
Susunod na session: **Martes 2026-09-08** — hindi trading day ang Lunes 09-07 (Labor Day,
kumpirmado sa kalendaryo ng broker mismo).

## Ang naipasok sa main

| PR | Ano |
|---|---|
| #1305 | 14-araw na tick retention + migration 375 (autovacuum scale sa 4 append-heavy table) |
| #1306 | Pine-persist na ng nightly replay ang stdout/stderr at exit code — ang R3 |
| #1307 + #1313 | **Ang buong Ross Parity Bench** |
| #1308 | Admission ignition signal — ang `signal=None` na dahilan ng 0 admit sa 3,379 session |
| #1309 | Tamang outcome class ng `live_arm_expired` |
| #1310 | Ang pending exit na walang order id ay nag-e-escalate na |
| #1311 | Migration-id guard na hindi mag-e-expire |
| #1312 | **Ang Alpaca leg ng broker-zero** — 3 buwang bulag sa lane natin |
| #1314 | Broker-confirmed no-fill → `cancelled_pre_entry` |

## Ang bench: tapos, at ang aral na tatlong beses umulit

Sampung component, dalawang adversarial round. Ang unang round ay nagpakita ng **0 sa 418 na
scorable** — sampung indibidwal na maayos na piraso na hindi magkasya. Ngayon: **157/157 pin na
naka-join sa manifest, 11 SCORABLE case sa tunay na hydrated tape, populated ang Ross column ng
timeline (314 leg, 270 may UTC instant), 7 sa 8 lane-alive case ang naka-resolve.** 815 test,
kasama ang `test_replay_v3_parity` at `test_replay_v3_fill_model`.

Tatlong beses lumitaw ang parehong depekto: **ang runner ay nagbibigay ng pangalan at itinatapon
ito ng consumer.** Ang huling dalawa — ang reporter na nagge-grade ng ILLR `::ml3` laban sa `::ml1`
(ibang trade, ibang P&L) at ang exporter na hindi kinikilala ang `@selector` na directory kaya
limang known answer ang walang na-export — ay inayos nang kamay.

Ang bench ay may tahasang hangganan na nakatatak sa **bawat** ulat: ang Tier 1 ay force-seed ng
admission, kaya **hindi** nito masusukat ang selection, universe, admission, own-order impact,
broker ack timing, BBO-source mix, o liveness. Ang Liveness ay `null` na may dahilang
`tier2_required`, hindi isang numerong mababasa bilang admission-latency claim.

## Ang redeploy cascade — ang pinakamahal na aral

Nag-redeploy ako sa GITNA ng RTH. Anim na magkakasunod na hadlang ang bumukas at **bumulag ang
lane ng halos tatlong oras** habang nakikita nito ang +23% na mover na `scored_ok=True`.

Ang kadena: ANPA ghost → walang Alpaca leg ang broker-zero → **ang settlement ko mismo** ang
gumawa ng classification conflict → 53 terminal session na walang outcome row → read-budget
starvation → anim na `flat_unknown` na row. Bawat isa ay kinailangang sundan hanggang sa
mekanismo. Ang buong kadena at ang bawat lunas ay nasa
`memory/project_redeploy_cascade_arming_outage_0904.md`.

**Dalawang bagay ang dapat manatili:**

Ang `loss_guard_history_unavailable` ay **mapanlinlang na label** — ito rin ang itinatakda ng
`auto_arm.py:5401` kapag pumalya ang `db.flush()`. Huwag hulaan; kunin ang
`out["loss_guard_history"]["reason"]` at ang `coverage_gap_session_ids`. At ang `.env` lang ay
kulang — kailangan ang `window_env_values` mula sa ACCEPTED receipt, kundi `skipped: flag_off` ang
makukuha mo at ibang bug ang hahabulin mo.

At: **ang redeploy ay pagkatapos ng close**, maliban kung ang bug mismo ang nagpapadugo.

## Ang araw sa pera

ANPA −$37.25 (ang burst exit na nagdesisyon pero hindi naglagay ng order; ang posisyon ay umabot
sa +$27.44 bago bumagsak). BIAF dalawang round trip, **+$15.02** — at doon **gumana ang exit**,
38 segundo mula bailout hanggang fill. Araw: **−$22.23**.

Ang koreksyon ng operator na binago ang pagkaunawa ko: hindi $35.29 ang nawala sa ANPA. **Tumaas
ang posisyon** — peak 5.35 sa 08:52:46, +11.7%, at nanatiling labintatlong minuto sa ibabaw ng
entry. Ang inalok ay +$27.44; ang natapos ay −$37.25. **$64.69 na baliktad.** At ang naipit na
pending exit ay hindi lang bigong maglagay — hinarangan nito ang scale-out rung sa 4.948 at 5.00
na **nasa pera** noong panahong iyon. Ang burst exit mismo ay pumutok sa lugi sa ika-50 segundo,
sa isang kasong nasa labas ng populasyong nagpapatunay nito.

## Naka-armas para sa Martes

`CHILI-Premarket-Backup-Launch-0908` (01:35 PT, epoch W20260908-01B) — ang header nito ay
naglilista ng apat na load-bearing na PR at kung paano kunin ang tunay na dahilan kapag
`armed=0`. `CHILI-PostClose-Deploy-Hydrate-0904` (17:05 PT) — hinihintay ang pagtatapos ng
window ng lane (hindi ito pinapatay), ff sa main, build, **content-check ang image laban sa git**,
recreate ang tatlong container, tapos hydrate ang 30 kulang na symbol-day.

## Hindi pa tapos

Ang **baseline run** — ang unang tunay na sagot sa tanong ng operator — ay hindi pa napapatakbo;
naghihintay ito ng hydration. Ang corpus ay **125 scoreable trade (83 panalo, 42 talo) sa 68
symbol-day**, 38 na hydrated at 30 ang kulang, dalawa lang ang lampas sa 180-araw na cliff.

Natitira rin: ang Phase 0.1 na libro (**3/19** ang nagki-credit sa learner; ang humaharang ay
`ledger_backfill_evolution_suppressed` ×10, `missing_entry_decision_packet` ×9,
`economic_ledger_parity_mismatch` ×3 — 17/19 kung maaayos), ang **spread budget** na 15% ng
expected move na pumatay sa buong RTH open ngayong umaga (215 candidate → 0 submit), at ang
**burst-exit trigger** na purong orasan at walang sahig sa entry.


---

## Part 3 — 2026-09-04 evening: from "zero events" to the first scored case (PRs #1318–#1326)

**Question the plan asked:** on a hydrated tape, does the bench point at ONE line where CHILI diverged from Ross? **Answer tonight: yes, on Robinhood; on the Alpaca canon, five gates deep and the fifth is a deployment mode, now carried.**

### The first scored case — SDOT 2026-06-26 (robinhood_agentic_mcp, stride 1, 160,148 ticks, 159,863 NBBO rows, 44.5 t/s)

| | |
|---|---|
| Ross | +$5,885.15 (main), entry ~09:20 ET on the $10.00 break (pin `price_match` / `tape_ambiguous`) |
| CHILI (replay) | **+$3.50**, 18 states, 7 fills, 4,190 events; `filled_exited_worse(trail_stop)` |
| recorded stage | `unknown(armed_no_entry)` [xref_verdict] — session 9198 in the ledger |
| **first_divergence** | **09:17:48 ET — Ross `filled` vs CHILI `watching`: `live_entry_backside_bench_veto reason=benched_backside_sticky` — `live_runner.py:34043` (VERIFIED tree)** |
| first_money_divergence | 09:17:48 ET (same second) |
| then | CHILI entered 09:18:12 (24 s after the veto), bailed out 09:18:32; re-entered 09:23:23, scaled out 09:23:55 / 09:24:03, exited 09:24:34 |
| grade | UNSCORABLE for credit (`pin_ambiguous`: several tape clusters at $10.00 — the earliest was chosen, all are listed). Honest; expected per plan §0.3 step 3. |
| Capture ratio | 0.0006 (main bucket); `%-of-equity` null — no row states Ross's equity |

This is anchor 4 of the plan's verification (the sticky backside bench latched on the faded premarket `benched_at_hod=17.8626`, vetoing at Ross's exact level) — reproduced in REPLAY on the hydrated tape with a code reference, not inferred from the recorded lane.

Run 1 (wall-clock events) vs run 2 (sim-clock events, #1319): same 7 fill instants, +$3.61 → +$3.50, sizes 24.71 → 24.24 sh on trades 2–3. The difference is real and production-faithful: the sizing now SEES the 09:18 bailout as recent (streak/fatigue derate); run 1 saw events 70 days in the future. **The no-op A/B (base vs base_copy, interleaved, same commit) is running on sink 1 to pin this — verification step 3.**

### The Alpaca canon — five gates, each a clean `rc=0` zero-event run until named

| # | tick skip / event | mechanism | fix |
|---|---|---|---|
| 1 | `alpaca_account_scope_unfrozen_or_mismatched` | quarantine gate reads ONLY `risk_snapshot_json` (#1304) | #1317 seeder freezes the admission proof field-for-field, all instants = arm anchor |
| 2 | `alpaca_account_generation_mismatch` | driver under `CHILI_PYTEST=1` does not read `.env` → expected id empty → seeder froze the mock identity | #1318 bench REFUSES an Alpaca run without `CHILI_ALPACA_EXPECTED_ACCOUNT_ID` (ambient, not contract) |
| 3 | `alpaca_adapter_account_generation_bind_failed`, `broker_calls: 0` | runner requires `adapter.bind_account_id(frozen) is True`; the mock had no such method | #1320 mock `bind_account_id` line-for-line vs `AlpacaSpotAdapter`; ONE identity source for seeder + mock; applied via `set_account_identity` (parity string pinned) |
| 4 | `live_held_execution_bbo_blocked` ×3 → `live_declined` → `live_cancelled` (71 s) | `_final_entry_bbo` requires `adapter.get_execution_bbo` with a provider-clocked age ≤ 2 s | #1322 mock `get_execution_bbo` mirroring the real adapter; provider clock = the sim instant; 7 tests drive the REAL validator |
| 5 | `live_entry_adaptive_risk_blocked reason=adaptive_risk_builder_source_invalid` ×26, 0 fills (31 min, 4,021 events) | the Alpaca entry builds adaptive-risk economics before legacy sizing; its capture provider is installed only by the sealed captured-paper service; the time-share lane runs on the LEGACY sizing escape (Option C, 2026-08-17) — `timeshare_supervisor.py:88` launches with `CHILI_MOMENTUM_LEGACY_ALPACA_DISPATCH_ENABLED=true`; the driver had the default (False) | #1326 the mode joins the account id as a required ambient env; the bench measures the lane AS DEPLOYED |

Observability fixed on the way: `live_entry_adaptive_risk_blocked` now carries `error_type` + `detail` (#1325) — the except arm folded `TypeError`/`ValueError` into the same label and dropped the builder's own detail (26 identical opaque blocks; L-G class).

### Instrument bugs the first real receipts exposed

- **Events on the wall clock (#1319).** `append_trading_automation_event` stamped `datetime.utcnow()` directly, bypassing the `_utcnow` chokepoint `replay_clock` freezes; fills were on the sim clock. 4,190 events graded `no_replay_events`. Writer takes `ts=None`; `_emit` passes `_utcnow()`; production byte-identical; other writers keep the wall clock (pinned).
- **Scorer graded a 7-fill run `no_arm_attempt` (#1323).** Tier-1 seeds admission and never emits arm events; the replay ladder now starts at `runner_never_started` when the receipt carries `seed_session_id` (`admission="harness_supplied"` in every detail).
- **`first_divergence 09:05:01 absent/watching` (#1323).** Row 0. Now anchored at Ross's first pin; with no pin, at CHILI's first fill (the Avoidance shape); with neither, NONE and the meta says why.
- **`candidate_no_submit` named the loudest veto (#1324).** 1,259 sticky-bench vetoes outvoted 26 attempt blockers. The qualifier is now the first refusal after each `pending_place`; both histograms stay in the detail.
- **Reporter hid the import error (#1323).** From a tree without `.env` the scorer import fails (`database_url Field required`) and every stage read `unavailable` as if the scorer had regressed. The problem line now carries the reason.
- **Evidence not in git (#1321).** `pins.json` / `corpus.json` / `corpus.csv` were untracked in wt-bench; any other tree died at `--pins: cannot read`.

### Sequencing lessons (mine)

- Merging moves `origin/main`; `verify_tree --ref origin/main` compares at run START — a bench launched from a not-yet-ff'd tree is refused (correct). Never ff, commit or edit tracked files in a tree while its driver runs: the receipt reads `tree.head` at the END (one RH receipt carries `4f801f63d ≠ 08de11057` from an evidence commit I made mid-run; content identical — 3 untracked files — proven by `git diff --stat`), and a DIRTY tree makes the timeline refuse verified code refs (one alpaca timeline is tagged UNVERIFIED for that reason). Launch from a SECOND worktree checked out at `origin/main`; keep a `wt-ref` worktree to check out the receipt's exact sha for timelines.
- Sink reads mid-run show only the last committed checkpoint (the driver holds one long transaction with savepoints): "657 events, stalled at 13:23:23" was a mirage; the receipt had 4,190.
- The per-tick skip reason lives only in the driver's `last_result` on stdout (the bench keeps 40 lines); a 3-minute direct driver run with the run's `contract_env` is the fastest read — but a window that starts mid-move has no frame warm-up and follows a different path (the 13:17–13:20 probe never reached a candidate).
- A scratchpad patch helper that opened the target for write BEFORE running the transform truncated `ross_bench_scoring.py` to 0 bytes when its assert fired; restored from git before any test ran. Transform first, then open.

### State at 22:15Z

- `main` @ #1325 (+#1326 pending): 8 PRs tonight on top of the 14 from the day.
- Sink 1: RH no-op A/B (base, base_copy) running, ETA ~22:42Z. Sink 2: free; alpaca SDOT relaunch with both lane keys next.
- Post-close task `CHILI-PostClose-Deploy-Hydrate-0904` armed 00:05Z (deploy main → containers; hydrate 60 IQFeed + 9 Polygon symbol-days). Premarket backup launch 2026-09-08 01:35 PT armed (09-07 is Labor Day).
- Not started: full 8-case baseline (needs tonight's hydration for UPC 06-29 / WETO 08-17), Phase 0.1 book, spread budget, burst-exit trigger.
