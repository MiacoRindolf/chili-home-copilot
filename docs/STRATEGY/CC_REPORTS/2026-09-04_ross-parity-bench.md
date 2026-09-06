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


---

## Part 4 — 2026-09-04/05 night: the canon family reaches its first real gate; the winners sweep starts (PRs #1327–#1331)

### The operator's new TASK (23:00Z, side chat) and how it maps

*Capture ALL of Ross's winners except a few knife-risky ones.* Burden of proof flipped: a gate that blocked a Ross winner is **wrong until the case timeline proves knife risk**; fix mechanisms by conditioning on tape state, never by loosening thresholds; every fix must take more winners **without** taking more of his losers (losers = negative control). Order: (1) bench every pinnable winner, each with timeline + report; (2) list every miss → first blocker, second, code ref, grouped by mechanism with count + Ross $; (3) fix largest-$ group first, A/B on winners and losers, ship, rollback ready; (4) re-bench after each fix; (5) misses replay cannot prove → paper from Tuesday 09-08.

This is the plan's Phase 1.3–1.4 → 2.3 with the burden inverted. The plan's five *designed* refusals are the "few knife-risky" exceptions and must be proven per case, not assumed.

Tooling landed for it tonight: a manifest-driven case list (winners = `expected_action==trade` ∧ `ross_net_usd>0`, one case per Ross **wave** as `SYMBOL:DATE:<manifest_id>` — 62 symbol-days carry more than one wave), a divergence-ledger aggregator over `rpi.json` + `timeline.meta.json` (mechanism → count, Ross $, CHILI $, first-divergence second, code ref), the receipt-scoring helper, and the no-op comparer.

### Alpaca canon, gates 6–8 — the last harness gaps before the strategy

| # | attempt blocker (26/26 attempts each run) | mechanism | fix |
|---|---|---|---|
| 6 | `live_entry_blocked_by_breaker` — `daily_loss_cap_broker`, "$0 breach" | for paper families the per-broker daily-loss observation reads the REAL Alpaca account (`AlpacaSpotAdapter().get_account_snapshot()`, module-level) → in replay a transient fail-closed read every tick; the RH family reads the DB ledger, which is why the control arm filled | #1329 governance `alpaca_account_snapshot_provider` seam (cache bypassed while installed); mock answers from its own book (`equity = start + cash flow + open MTM at the recorded quote`, `last_equity = start`, sim-clock stamp); driver installs it around `driver.run()`. Cap = 5% × EQUITY via `risk_policy._REPLAY_EQUITY` = 5,000, matching the run |
| 7 | `live_entry_deferred_final_bbo` — `alpaca_broker_clock_unavailable` ×19 | `_strict_alpaca_clock_truth` requires `get_market_clock_snapshot()` (the `/v2/clock` twin), timestamp within [−1, 30] s of `_utcnow()` | #1330 mock clock from the sim instant, **truthful**: `is_open` = 09:30–16:00 ET Mon–Fri (holidays not modelled, said so); a premarket replay reads `is_open=False` and the runner's own premarket carve-out decides |
| 8 | `live_entry_deferred_final_bbo` — `alpaca_account_posture_unreadable` ×19 | `_strict_alpaca_empty_entry_posture` calls `list_positions()` + `list_open_orders(strict=True)`, both fresh ≤ 2 s; the mock lacked the first and refused `strict` | #1331 mock `list_positions()` from its fills in the real row shape; `strict` accepted |

Observability fixed on the way (#1328): the receipt filter dropped everything a breaker writes (`{}` ×26 in the receipt while the sink row named `daily_loss_cap_broker`), and the runner's breaker block coerced the observation's `realized=None` to `0.0` and dropped `reason/transient/source` — a transient fail-closed read and a measured $0 breach were the same line. Both now carry the truth.

**What the seven-gate run showed (run #8, `alpaca_canon_235106Z`, tree `715846851`, VERIFIED refs):** all 26 attempts reached `live_entry_final_bbo` with `execution_bbo_ok` — the mock's execution BBO passes the real submit-boundary validator — and **7 of 26 were vetoed by `spread_exceeds_expected_move_budget`**. That is the first *strategy* gate the bench has reached on the Alpaca canon, and it is the same gate that vetoed every RTH entry on the live lane on 09-04 (215 candidates → 0 submits). It enters the divergence ledger as a mechanism to condition, not a harness gap to fix.

### No-op A/B — verification step 3 passed

`base` vs `base_copy` on the same commit (`e71229451`), interleaved, SDOT 06-26, stride 1: **0 scalar diffs, 7 fills identical, 4,190 events identical including payloads, Δ +0.00**, both timelines VERIFIED, same first divergence 09:17:48 ET. The harness is hermetic on the Robinhood family.

### Post-close deploy — done, with two of my own faults on the way

The task ff'd `wt-window2` to `715846851` and built `chili-app:main-715846851` (10 s, cached layers), then **aborted its own content check (rc=3)**. Recomputed in Python on raw bytes: image == git on `live_runner.py`, `persistence.py`, `governance.py`, `replay_mock_broker.py`. The checker's git-side hash went through a PowerShell 5.1 pipeline (`git show | python`) which re-encodes a UTF-8 file with non-ASCII comments — that hash can never match. Fixed in the script (python hashes both sides). Then `recreate_containers.py` failed on a name conflict: the morning's backup still held `<name>.old`, `docker rename` failed silently, and `docker run --name` collided; the stale backups were renamed to `.old-078487738` (kept for rollback, not deleted) and the recreate succeeded: **3 containers Up on `main-715846851` at 00:11Z**. Hydration of 60 IQFeed + 9 Polygon symbol-days is running from the deploy tree (`postclose_hydrate_20260904_manual.log`).

### The winners sweep — batch 1 running

Robinhood control family first (sink 1, wt-bench @ `c624d59df`, stride 2): the top-12 hydrated winners by |Ross $| — PPCB 08-27 t2, EHGO 07-23, EDBL 07-27, NCRA 07-29, BIYA 07-20, LABT 07-22, VIVS 07-15, VRAX 07-09 ×2, NXTC 07-14 ×2, VEEE 07-13 — $309,990 of Ross P&L; ~25 min per case on the denser Aug tapes, ETA ~03:30Z. The Alpaca canon sweep follows once run #9 (eight gates) fills or names the next real gate. Losers hydrated + pinned tonight: only 2 (EZRA 08-03 t3, PPCB 08-27 t3) — the negative control is thin until tonight's hydration lands.

### Sequencing lessons added tonight

- Merging a PR in the same breath as launching a bench from a not-yet-ff'd tree gets the launch refused by `verify_tree` (correct) — three times tonight. Merge, ff, **then** launch, or launch from a tree checked out at `origin/main` after the merge.
- A worktree with an untracked file (someone else's `tests/test_backside_retrace_window.py` in wt-seams) is DIRTY to the receipt → UNVERIFIED code refs. Runs now come from `wt-alpaca` (clean, detached at `origin/main`) and `wt-bench`.
- The `-k "daily_loss or governance"` selection has **11 pre-existing failures** identical on `main` and on the gate-6 branch — CI is systemically red; a regression check must diff failing sets, not counts.

### Gate 8 (#1331) and the first sweep case with a tape-backed timeline (#1332)

Gate 8: `_strict_alpaca_empty_entry_posture` calls `list_positions()` + `list_open_orders(strict=True)` before every attempt; the mock lacked the first and refused `strict` → `alpaca_account_posture_unreadable` ×19. Fixed (#1331), canon run #9 launched from `wt-alpaca @ 21387ef70`.

A multi-case sweep resets the sink before each case, so a finished case's tape is gone by scoring time; the timeline can now read the hydrated source DB with the driver's provenance predicate (`--tape-source-dsn`, #1332). First use — **PPCB 2026-08-27 t2, Ross +$50,000 (entry pinned 08:30:49 ET, `level_cross`, ambiguous), CHILI −$36.28**, VERIFIED refs at `c624d59df`:

| ET | tape | CHILI | ref |
|---|---|---|---|
| 08:31:03 | | candidate (`momentum_ok_tick_stream`), blocked once `wide_bbo_spread` 2.74/2.88 | |
| 08:31:06 | 3.18/3.22 | `pending_place` | |
| 08:31:07 | prints **3.47, 3.49, 3.37** in the first 8 ms, last of the second 3.22 | **`live_entry_submitted`** | `live_runner.py:40824` |
| 08:31:08 | last 2.89, 2.82/2.88 | **filled $3.43 ×48** (the ask at the submit instant) | `:19586` |
| 08:31:09 | last 2.68 | **`live_bailout max_loss_circuit`**, unrealized −$36.28 | `:42660` |
| 08:31:10 → :33 | | 22 × `live_exit_pending_confirmation`, exit filled 3.21 → −$10.64 | |
| 08:31:38 → :44 | | maker re-entry 3.04 → scale-out 3.11 (+7.37) → `trail_stop` 3.00 (−4.21) | |
| 08:33:35 → :58 | | third entry 3.21 → `breakout_failed_fast_bail` → 3.01 (−28.80) | |

The $3.43 is real (the recorded NBBO's max ask in that window is 3.49): CHILI's submit landed on the top print of a two-second spike — a 4-second candidate→submit path (plan L-E, place-time staleness) into an extension — then the exit stack cut every subsequent attempt within seconds on a name that went on to make Ross $50k. Two ledger mechanisms from one case: **entry timing (chase into a spike top)** and **bailout within seconds of fill**. Neither is a gate-threshold question.

Instrument defect found by the same timeline: the PPCB pin carries `exit_ts_utc_pinned == entry_ts_utc_pinned` (no exit clock stated → the pinner defaulted the exit onto the entry), so Ross's stage reads `exited` at 08:30:49 and the first-divergence label is `exited/blocked`. The pinner should leave an unstated exit unpinned (Ross stays `filled`); noted for `rossbench_pin_ross_events.py`.

Sweep batch 1 pace: ~25 min per case on the denser August tapes (PPCB: 282k prints, 69k Polygon quotes, 78 t/s at stride 2); EDBL running at 00:30Z.


---

## Part 5 — 2026-09-05 early hours: the canon fills; the ninth gate was a production hazard; the sweep universe

### Alpaca canon — gate 9 and the first fill

Gate 9 (#1335): `risk_ledger_unreadable` ×19 folded `psycopg2.errors.UndefinedTable: relation "broker_symbol_action_claims" does not exist` — raised in `read_action_claim` against the **hydrated tape DB**. The Alpaca claim helpers open `SessionLocal()` (app/db.py's process-wide engine, bound at import to `DATABASE_URL`), and the bench contract had set `DATABASE_URL` to the tape source because the driver's `PROD` was `DATABASE_URL`. **In the nightly the same driver runs with `DATABASE_URL` = the live `chili`: a replay that reached this seam would have read — and on the claim paths written — production.** Fix: `TAPE_SOURCE_URL` names the tape; `DATABASE_URL` is the sink in the bench contract and the nightly; `broker_symbol_action_claims` joins the per-run sink reset. The Robinhood family never calls these helpers, which is why the control arm filled for three runs while the canon did not.

**Run #10 (nine gates, tree `0b0c1fd2e`, VERIFIED refs): the first Alpaca fill.** SDOT 2026-06-26, 09:18:12 ET, 15 shares — the same second the Robinhood arm entered. Then:

| ET | CHILI (Alpaca canon) | Robinhood control, same second |
|---|---|---|
| 09:18:13 | filled; `tranche_oco_skipped_extended_hours`; `alpaca_scale_out_suppressed_for_deadman`; `live_blocked_by_risk wide_bbo_spread` (`max_spread_bps=12.00`) | filled; scale-out limit placed |
| 09:18:13 → 09:29:59 | **546 × `wide_bbo_spread`** — spread 17–460 bps against a 12-bps cap, later 300; no exit logic ran | bailed out 09:18:32 (−$10.6), re-entered 09:23:23, scaled out, trailed out 09:24:34; net +$3.50 |
| 10:00 | still held, 15 sh, MTM **+$25.20** at the grid's last bid (SDOT ran to $12) | flat |

Stage: `filled_exited_worse(no_exit_event)`, CHILI +$25.20 vs Ross +$5,885. The canon's mechanism is not a gate: **a premarket Alpaca position has no exit path** — the deadman stop is inert in extended hours (measured 2026-08-26), OCO tranches are skipped, and the FSM's own exits are blocked every tick by the held-tick spread cap (12 bps on a $10 name, the same 12 bps the live ANPA position sat behind on 09-04). On SDOT that accident was worth +$25 versus the Robinhood arm's +$3.50; the sweep will say what it is worth across the winners and the losers. Neither the 12-bps cap nor the caps in the lane's `.env` are strategy levers to loosen by hand — they enter the ledger.

Also from this run: the lane's `.env` carries 153 `CHILI_*` keys and the bench passed two (account id, dispatch mode). The 12-bps figure is a default, so this case is faithful, but any of the other 151 could change a decision. `--lane-env` (#1337) now layers the lane's `CHILI_*` keys under the contract (names + sha256 in the run record, never values). Note the deployed env is `.env` **plus** the supervisor's launch overlay (`timeshare_supervisor.py:88` sets the dispatch mode there, not in `.env`), so the two required keys stay explicit exports alongside `--lane-env`.

### Hydration: the post-close IQFeed pass aborted at row 32

`StringDataRightTruncation` — in the hydrator's own failure path, recording the failure of a corpus row whose symbol field carried narrative (`NUWE (09:30 pivot 5.34)`) into `hydration_jobs.symbol` (varchar 32). The remaining 28 rows (WETO 08-17, UPC 08-03 among them) were never attempted; three more narrative "symbols" sat in the same CSV. Fixed (#1336): a non-ticker is rejected before any DB work and reported; recording a failure can never fail the corpus; the corpus builder flags such rows `symbol_malformed` and keeps them out of `corpus.csv`. The 23 clean remaining rows were relaunched at 01:19Z.

Corpus after the first pass: **152 replayable symbol-days; 65 Ross winners hydrated + pinned ($792k of his P&L); 9 losers ($51.8k)** — the sweep universe and its negative control.

### Sweep batch 1 — restarted with an hour per case

The first launch used a 25-minute per-case timeout; the denser July/August tapes take 25–40 minutes at stride 2 (PPCB: 282k prints + 69k Polygon quotes). EHGO timed out and two more produced no receipt; batch 1 was stopped (PPCB's receipt kept) and relaunched for the remaining 11 cases with `--timeout-s 3600` at 01:09Z. The tape-provenance invariant that flagged PPCB (`iqfeed` trades + `polygon` quotes) was refined to a per-table rule (#1334) — that pairing is how every August/September symbol-day is hydrated by design.

---

## Part 6 — 2026-09-05 02:00–02:50Z: the tenth gate was the mock, not the market; the full Alpaca sweep starts (PRs #1342–#1346)

### Correction to Part 5

Part 5 read the Alpaca canon as "a premarket Alpaca position has no exit path — deadman inert, OCO skipped, 546 × `wide_bbo_spread` blocking every held tick". The held-tick reading was wrong, and it was wrong in the way the plan's invariant list warns about: the loudest event was taken for the blocker. Measured on batch 1 of the alpaca sweep (8/12 receipts by 02:29Z, every one a single fill held to the window's end, MTM +$200…+$1,087):

- `live_blocked_by_risk reason=wide_bbo_spread` is emitted on a held tick (`live_runner.py:32700`) but the function **does not return** there for a held session — `_live_entry_quote_gate_applies` (`:22431`) is False once the position is held, so `:32721` never fires. Those 1,775–2,992 events per case are telemetry.
- The branch that RETURNS is `:42598`: `if deadman_state.get("pending") or deadman_state.get("unprotected"): return {...}` — before any trailing/scale-out/bailout logic. The deadman was pending on every tick because **the replay mock has no `place_deadman_stop`**: `:12933` raised `AttributeError`, the result was folded to `submit_indeterminate`, the strict CID read (`:27604`, needs `get_order_by_client_order_id_truth`, also absent on the mock) resolved `unknown`, and `:13092` recorded `deadman_submit_indeterminate`. Once. Forever.

So the tenth Alpaca canon gate is a harness gap, exactly like gates 1–9 — not a strategy fact. **#1345** gives the mock the real adapter's contract (`venue/alpaca_spot.py:4077` and `:2508`, same refusal strings, the same `raw` certification fields the runner's own `_owner_transport_order_matches` reads; a stop triggers at `min(stop, bid)` in the regular session only, because Alpaca queues stops through extended hours — the runner documents itself as the sole premarket protection at `:3525`). Non-stop orders keep their raw shape byte-identical; the parity suites are unchanged. Not mirrored: `replace_order_qty` (#1276 partial-exit PATCH lineage) — partials stay on the legacy suppression path in the bench and are reported as a limitation.

What stays true from Part 5: the 12-bps held-tick floor (`:32598 → :25068`, no expected move on a held tick) is below one tick for every sub-$8 name — LABT at $2.10 is 48 bps per tick, and the held-period spread p10 was ≥ 14 bps on all six cases. In the live DB the same event fired **7,680 times in 310 sessions over 14 days** (ANPA 2026-09-04: 4,002 in one session at $4.29, median 75 bps). On a held tick it is noise; at ENTRY in premarket, where `_skip_spread_gate` is forced off (`:32553`), the same floor is a real refusal on the price band Ross trades. That is a conditioning candidate for the ledger, not a threshold change.

### Invariant 7 (cold start) — applied, then re-denominated

- **#1342**: `cold_start_tags` had existed since the harness PR but nothing called it. The bench now tags every decisive event inside the window lead and marks a case unscoreable when its FIRST entry-decisive event is cold. Measured immediately: alpaca batch 1 first fills at runner uptime 11 s (PPCB), 13 (NCRA), 17 (EHGO), 31 (EDBL), 36 (LABT), 70 (BIYA); RH EHGO 17 s. Under the 60-second wall-clock reading 5/6 alpaca cases and RH EHGO were unscoreable — nearly the whole sweep.
- **#1343**: the verdict is tick-denominated. The FSM's own cold-start guard is `insufficient_bars` (NCRA: 7 × `live_entry_trigger_wait reason=insufficient_bars`, candidate on grid tick 8, fill at tick 13); the plan's two knobs are one quantity in two units (6 ticks = 60 s at the live 10-s cadence) and the bench grid is 1 s, so wall uptime overstates coldness ten-fold in replay. Now: `ticks_seen = uptime / GRID_STEP_S`; unscoreable only when the first entry-decisive event has `ticks_seen < 6`; uptime-only coldness is recorded (`cold_start.uptime_only_entries`) and logged. RH PPCB t2 (runner started at Ross's ignition print 12:31:02Z, filled 5 s later at the spike top) stays cold under both readings — the seeded arm was placed at an instant the live scheduler could not have reached, which is Tier 1's stated limit.

### Receipt and timeline fidelity

- **#1344**: the receipt keeps `spread_bps / max_spread_bps / expected_move_bps / median_spread_bps / bid / ask / mid / client_order_id / broker_error / …` — the cap a block was measured against and the deadman's pending detail had been dropped by the payload whitelist and had to be reconstructed from source.
- **#1346**: timeline code refs are reason-aware. `live_blocked_by_risk reason=wide_bbo_spread` had resolved to `live_runner.py:30269` — the `kill_switch` emit, the lowest of seven sites — and the ledger printed the kill switch as the mechanism for every alpaca case. `resolve_code_refs` now takes the reasons seen in the run and resolves `<event_type>|<reason>`: the site whose payload literal spells the reason wins; otherwise only dynamic-payload sites remain and a site spelling a different reason is excluded.
- Divergence ledger (scratchpad tool): `--pins` value no longer consumed as a run dir; the ordering check uses `entry_ts_utc_pinned` or, for stated-only rows, `grading_anchor_utc` (91 pinned / 55 anchor-only / 11 neither in `pins.json`); a `first_fill_before_ross_window(<source>)` tag; an `UNSCORABLE cold_start` bucket; open positions never counted as CAPTURED.

### The sweep as it stands (02:50Z)

- Robinhood batch 1b (sink 1, `wt-bench` @ `c4bb57b15`, 11 cases, an hour each): 2 receipts. RH EHGO 07-23 (Ross +$41k): 20 fills, +$27.67, active round trips (scale-out ×7, trail ×4, burst-exit ×2); first divergence at Ross's stated 09:15 ET entry: CHILI `exited`, Ross `filled`.
- Alpaca batch 1 (sink 2, no deadman): kept as the harness negative control — entries are unaffected by #1345, only what happens after.
- **Alpaca full sweep with the mock deadman: 82 cases** (83 winners + 11 losers, minus 12 the bench refuses because neither the manifest row nor the pins row carries an anchor — GMM 07-10, PN 07-30, CANF/VCIG 03-04, ARTL/JTAI/WLDS 04-20, SPRC 08-12 have no pin at all; NUWE 07-30, ENVB 04-20, PPCB t3 and EHGO Ts7C resolve through the bench's pin-anchor branch and run as part D). Four sinks in parallel (`chili_rossbench{2,3,4,5}_test`, sinks 3–5 created from the `chili_test` template), two clean worktrees at `b31566939`, losers distributed round-robin. Scoring: timelines from `wt-seams` @ main against `wt-ref` at the receipt sha, then the ledger over all dirs.

### Next

1. Ledger the 82 + 11 as receipts land (task step 2): mechanism × count × Ross $, code ref per miss.
2. Largest-$ mechanism first, by conditioning; A/B on winners AND losers; the RH family is the control for exit machinery.
3. Bench: read the pins anchor when the manifest row has none (already branch 4; the case filter was mine); `replace_order_qty` in the mock so partials are measurable.
4. Paper from Tuesday 09-08 for what replay cannot prove (admission latency, dead lane).

---

## Part 7 — 2026-09-05 03:00–15:30Z: gates 11–14, the commit wall, the second ledger pass, and the first exit-side A/B (PRs #1348–#1352)

### Gates 11–13 — the Alpaca exit path, verb by verb

- **#1348** — the mock adapter gained the release verbs the runner requires before a software exit may replace the deadman (`cancel_order_by_id`, `get_position_quantity`, `get_order_by_client_order_id`), and the claim helpers gained a `replay_short_session_provider(db)` seam: `_with_short_session` had opened its OWN connection while the replay driver holds ONE transaction, so every "committed" read of `trading_automation_sessions` saw the seed row and `retire_deadman_handoff_reprotected` was False forever. The driver installs the seam (a SAVEPOINT on its own session) around `driver.run()`; production default is None (byte-identical).
- **#1349** — every order carries the adapter's raw shape under Alpaca families (`set_execution_family`), because `_poll_live_exit_fill` certifies exit fills through `_owner_transport_order_matches` reading `alpaca_status / filled_size / time_in_force / position_intent / extended_hours`; the mock had carried them on stop orders only, so a filled software exit stayed `pending_exit=True` and every Alpaca case was one leg. Probe FCUV 12:15–12:32: **four round trips** — the first exit-capable Alpaca replay.
- **#1350** — Postgres container CPU cap 3 → 8 in compose (12 GB memory limit stays); the bench's 1-s grid is ~10× the live query rate and the backends were CPU-bound at 17–19 drivers.
- **#1351** — a case that produces no receipt now says why on disk (`driver_stdout_tail.txt` next to the missing `run.json`) and in the log (`LOST <case>: status/rc/duration/tail`); a window with zero source ticks is recorded as `empty_window` without spending a driver (SCKT 08-10 t5 and FRTT 08-11 carried manifest anchors 1–1.5 h before the day's first print).

### Gate 14 — `tape_confirms_hold` was fail-closed in every replay since #1024 (2026-08-11)

The lockout-watch A/B on JWEL 08-10 held every trigger during the 11:31–11:34Z reclaim as `no_buyers_on_tape`, while `tape_confirms_hold("JWEL", as_of=11:31:47Z)` offline against the SAME sink returned confirmed (accel +6,899, tick_rate 137 ≥ floor 53, n=1,423). The discriminator was a five-second experiment: build the driver's wrapper shape offline → `tape_hold_error`; a forwarding wrapper → confirmed.

Root cause: the driver's sim-clock wrapper around `entry_gates.signed_tape_accel_features` re-declared the signature as `(symbol, *, db, window_s, as_of)`; #1024 added `settings_obj` to the wrapped function and `tape_confirms_hold` passes it on every call; the TypeError is swallowed by the confirmer's fail-closed `except`. Blast radius: the 12 pattern triggers that require the confirmer, the ORB/ABCD tick confirm (degraded to the bar path), `buyers_confirmed`, the ignition-exemption tape leg — dark in every bench receipt, and invisible in the receipt because the payload whitelist strips the `tape` dbg (97 receipts, 0 `tape_hold_*` strings).

**#1352**: `replay_harness_invariants.simclock_default_wrapper(fn, clock, key=)` forwards every argument and fills only `key` when None; an AST guard refuses any `_o=`-style wrapper in the driver without `**kw`. Consequence: every receipt before 11:30Z ran with the tape gate dark; tape-conditioned fixes are A/B'd with BOTH arms on the fixed driver; the baseline is being re-benched on it (`rh5*`, `p7*` @ `370be829c`). First evidence of the gate's weight: JWEL ml2 alpaca, base tree, −$109.43 on the old driver → **+$47.73** on the fixed driver, no other change.

### The commit wall

At 12:00Z the burst-2 A/B lost three cases in 0.02 s each (`driver_failed rc=3221225794` = `0xC0000142`): committed 141.8 GB of a 146 GB limit (64 GB RAM + 84 GB pagefile). Free physical RAM read 2.7 GB at the time — the RAM guard's metric was the wrong one. Who holds it (`Get-Process` grouped, 12:10Z): the Claude desktop app ×102 processes 47.3 GB, Docker/WSL2 31 GB, python ×28 15.5 GB (**a replay driver commits ~1 GB**), the rest ~15 GB of user apps. Sheds: the old-driver baseline sweeps (superseded by gate 14 anyway); a commit guard (3-min period, floor 8 GB free virtual, sheds one non-A/B driver at a time) replaced the RAM guard; the operator closed the non-trading apps at 15:10Z (free virtual 26 GB → 16 drivers).

### Ledger pass 2 (09:22Z, 26 receipts, old driver) — exit side named

Alpaca, 10 winners: CAPTURED 5 (Ross $132k; CHILI +$2,265 — XHG +$1,774 on 161 sh 1.18 → 5.93, STI, CETX, NXTC, FCUV), lockout ×2, deadman_stop, trail ×2; family +$1,895 vs MFE $3,427. Robinhood, 17 winners: CAPTURED 5 (+$332), **burst_window_exit ×4 (Ross $59.7k, CHILI −$181)**, lockout ×2 ($33.5k, −$173), bailout ×3, stop, trail; family −$173 vs MFE $5,609. Losers: DSY −$84 (locked), PPCB t3 −$0.25, PPBT +$50.

Four exit-side mechanisms, each with its line (memory `project_exit_side_mechanisms_first_ledger_0905`):

1. **Burst stamp survives recycle** — `_RECYCLE_ENTRY_STATE_KEYS` (live_runner.py:25483) lacks `burst_started_epoch/burst_track/burst_window_dbg`; every re-entry is killed 1–2 s after its fill (JWEL ml3: 79 of 85 legs). Fix branch `fix/burst-stamp-cleared-on-recycle` (3c7ebc29a). A/B on the old driver, 11 pairs: JWEL 176 → 8 fills, hold p50 2 → 67 s, −71.36 → −75.22; losers Σ 0.00 (8 pairs, none worsened).
2. **Symbol-day loss lockout −1.5R terminalizes the winner** (`risk_policy.py:4212`, `live_runner.py:47853`): JWEL locked at −$109 while Ross made +$42k on the name. Fix branch `fix/symbol-day-lockout-front-side` v2: the lock becomes a WATCH with a budget; a fired trigger may proceed only when `tape_confirms_hold` confirms buyers (fail-closed) and the budget allows; spending the budget makes the next lock terminal.
3. **Alpaca exit transport** — decision → fill 7–34 s (two-phase deadman handoff; `frozen_exit_limit_not_marketable_at_literal_post` re-derives on the next pulse) vs Robinhood 1–2 s. Not yet conditioned.
4. **Stop inside the tape's own noise → risk-first sizing buys 10× size.** Measured 15:20Z on the fixed driver (stack arm, JWEL ml3): 11:39:00 spike 6.25 → 5.08 in 11 s; 11:39:09 `double_bottom_break_tick_ok` bought **1,153 sh @ 5.57** (other legs 96–155) with a ~4-cent stop while the tape printed 10–20 cents per second; `max_loss_per_trade` bailout at 5.13 three seconds later = **−$507**. `compute_risk_first_quantity` (qty = risk / stop_distance) did what it says; the remedy exists — #1278 `stop_noise_floor_decision`, median 30-s high-low range of the name's own tape as the stop floor (`live_runner.py:37674`) — but `chili_momentum_stop_noise_floor_enabled` defaults False and is absent from the lane `.env`: a dark flag, off in production too. Counterfactual on the hydrated tape (10 buckets, median range 0.245 = 4.4%): 188 sh, −$83 on the same flush (−$46 at the stop). Doctrine says no dark flags; it is now an A/B arm (`nf`, env override) on winners and losers.

### First A/B on the fixed driver (15:05Z, both arms @ 370be829c / stack 22bd290d5)

| case | Ross | base | stack (burst + lockout watch) | Δ | reading |
|---|---|---:|---:|---:|---|
| EHGO 07-23 RH | +$41k | +73.68 | +144.99 | +71.30 | 21 → 15 legs, no burst kills |
| EDBL 07-27 RH | +$33k | −79.58 | −114.06 | −34.48 | watch granted one `sub_vwap_trap_tick` re-entry, stopped in 19 s |
| JWEL 08-10 ml3 RH | +$42k | −77.66 | −523.78 | −446.12 | ONE leg −507 (mechanism 4); without it +$61 vs base; the burst fix kept the session alive through 11:30 (base locked out at 11:30:53) and legs 4–8 captured the reclaim +$58; the watch's own re-entry −18.57 |
| JWEL 08-10 ml2 alpaca | +$42k | +47.73 | +47.73 (lockout only) | 0 | never reached the lock |
| DSY t4, INLF t1 (losers) | | | | 0.00 | negative control unchanged |

Both watch exemptions so far entered on non-front-side triggers (`sub_vwap_trap_tick`, `abcd_break_tick_ok` into a failed breakout) and lost; the conditioning candidate for v3 is `front_side_state` (new HOD / above VWAP) in addition to the tape, per Ross's "hands off until it proves itself".

### Running at 15:30Z (16–17 drivers, fixed driver unless noted)

Code arms: `base14_rh` / `stack14_rh` (14 RH cases), `base14_alpaca` / `lockout14_alpaca` (12). Env arms (`--arms nf=`): `nf14_rh`, `nf14_alpaca` (main + noise floor), `stacknf14_rh`, `stacknf14_alpaca` (stack + noise floor). Baseline re-bench: `rh5a–d`, `p7a–b`; the remaining parts queue as drivers free. Old driver, consistent pairs: `burst_rh`, `burst_alpaca`, `burst2b_rh` (the three cases lost to the commit wall).

### Next

1. Per-mechanism verdicts as pairs land: burst (bug fix; ship on losers Σ ≥ 0), noise floor (dark flag → ON if winners Σ > 0 and losers Σ ≥ 0), lockout watch (needs winners $↑ — v3 conditioning if the exemptions keep losing).
2. Pass-3 ledger on the fixed-driver baseline (`FIXED=1 score_all.sh`).
3. Mechanism 3 (Alpaca exit transport) conditioning after the above.
4. Ship passing fixes; deploy after close on Monday 09-07 (no market); paper soak Tuesday 09-08.

---

## Part 8 — 2026-09-05 15:30Z → 2026-09-06 00:00Z: the exit-side A/Bs on the fixed driver, two ships, one flip, one inert (PRs #1354–#1357)

### What shipped, what did not, and why

| mechanism | fix | A/B (fixed driver, both arms) | verdict |
|---|---|---|---|
| 1 burst stamp survives recycle | `_RECYCLE_ENTRY_STATE_KEYS` += the three burst keys | 12 pairs: ENVB −37.67 → +57.91 (87 → 10 legs), JWEL ml3 −3.87, 2 alpaca winners 0; **8 losers 0.00** | **shipped #1354** |
| FSM: a scaling-out position could not bail | `(live_scaling_out, live_bailout)` edge | found by the loser DFNS on the stack tree (`Invalid live FSM transition` after a scale-out max-loss circuit; driver died with the position open); refreshed pair base16 = stack2 = −75.72 | **shipped #1356** |
| 4 stop inside the tape's own noise → 10× size | noise floor as a hard bound on the structural/cap resolution; flag ON | 6 pairs at 20:00Z: winners +464.87, losers +2.96 → **merged #1355**. Full 15 pairs at 22:15Z: winners +462.14 (JWEL ml3 RH −505 → −85, JWEL ml2 alpaca +48 → +93) but **losers −81.84, 3 worsened** (EZRA t3 alpaca −75 → −143, INLF t1 RH −20 → −40, PPCB −1.59) | **flag OFF again #1357** by the program rule; bound + observability stay in the code |
| 2 symbol-day lockout terminalises the winner | v2: lock → WATCH; re-entry on `tape_confirms_hold` + budget | 19 pairs: all four exemptions fired above VWAP but 14–28% below the day high and all four lost; losers −22.83 | **FAIL** |
| 2, v3: re-entry only when back within one 30-s noise band of the session high | `symbol_day_lockout_watch_reentry(last, session_high, noise_abs)`, fail-closed | 14 pairs: every case Δ 0.00 — the watch activates (JWEL 88 holds, 82 `not_reclaimed`) and never grants inside the 60-min window | **inert; not shipped** (branch kept as evidence) |

Main at 00:00Z = `d63cfc379`: burst fix + FSM edge + the noise-floor bound present but OFF. That is the deploy candidate.

### The noise-floor flip is the lesson of the day

Per-leg $risk is constant by construction (risk-first sizing), so the floor cannot make a single leg lose more. What moved the losers is second-order state:

- **The −1.5R symbol-day lockout is a cliff.** EZRA t3 alpaca: the floored leg 3 lost −3.30 instead of −9.00; the session stayed $5.70 above the lockout threshold that had ended the base run, and four more legs followed (+14.0, −1.46, −4.90, then −81.32 on an 11.5%-in-38 s flush where `max_loss_per_trade` bailed late). Any change to per-leg loss flips whether a loser session survives.
- **Bail vs ride.** INLF t1 RH: the wider stop rode leg 2 down to a trail at −9.96 where the base's tight stop bailed at +5.13.

So mechanism 2 (the lockout) is entangled with every exit-side change, and it must be conditioned before any other exit-side fix can pass a clean negative control. The v3 reclaim test is the right shape ("hands off until it proves itself again" = back at the high), but nothing in the 60-min windows ever reclaimed; the next experiment is the same pairs with a 2-hour lag to learn whether Ross's reclaims sit beyond the window.

### Harness and machine

- Gate 14 (#1352) — `tape_confirms_hold` had been fail-closed in every replay since #1024 (a wrapper that owned the wrapped signature). Every earlier receipt is superseded; the baseline was re-benched.
- The commit wall (141.8/146 GB) and the Claude app's 47 GB commit; a commit guard (floor 6 GB free virtual) sheds non-A/B drivers one at a time; 20–25 drivers is the machine's envelope.
- Editing a running bash launcher truncates its log at exit (bash reads the script by byte offset) — launchers are now copied before edits.
- Every A/B arm runs head + reverse-order tail (+ a middle driver when a slot frees); duplicates are killed at the meeting point.

### Running at 00:00Z

Ledger baseline at the deploy candidate (`rh8a–e`, `p10a–f`, three tails, 17 drivers) for the pass-3 ledger; the last five lockout-v3 cases (completeness only).

### Next

1. Pass-3 ledger on the deploy candidate (`FIXED=1 score_all.sh`): mechanism × count × Ross $ × CHILI $, winners captured, losers avoided — the honest re-statement of the task after gate 14.
2. Lockout v3 with a 2-hour lag on JWEL 08-10 and EDBL 07-27 (both arms) — does the reclaim exist?
3. Mechanism 3 (Alpaca exit transport): on the fixed driver decision → fill is 3 s on every alpaca case measured; the earlier 7–34 s was the harness (gates 11–13). Closed unless the ledger shows otherwise.
4. Deploy `d63cfc379` after close Monday 09-07; paper soak Tuesday 09-08.
