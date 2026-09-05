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
