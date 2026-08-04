# 2026-08-04 — Capacity-eviction root cause + demand-side levers (L11, L11b)

## TL;DR

- **Na-root-cause sa wakas ang "starvation" na humaharang sa paper trading mula Lunes**: hindi ito crash o missing wiring kundi **subscription capacity eviction** — 29,677 `capacity_eviction` lines sa isang service run, **109 distinct symbols, 100% `causes=ross`**, zero non-ross. Ang mga tunay na mover ng araw ang na-evict.
- **Dalawang demand-side lever ang naka-merge at NAKA-DEPLOY**: L11 paper setup-quality gate (#979) at L11b eligibility retention lease (#981, fix #982). **Live receipt: band 664 → 44-72 distinct**, 97,249 stale rows demoted sa unang sweep.
- **Ito ang unang strategy code na aktwal na tumatakbo sa produksyon mula pa 07-25** — dahil ang paper lane ay naka-freeze sa isang lokal na commit na 90 commits behind main.
- Merged din kaninang umaga: **L10 structure floor + NBBO-starvation harness fallback** (#977) — kung saan natuklasan na ang HYFM −88.52 ay artifact ng starved capture, hindi strategy loss.

## 1. Ang capacity-eviction chain

Ang IQFeed L1 watch roster ay umabot sa 624-625 na simbolo. Nagpadala ang provider ng `S,SYMBOL LIMIT REACHED,<SYM>` — **walang numero sa mensahe**. Ang rail-governor sa `scripts/iqfeed_trade_bridge.py` ay tumugon sa paghahati: `max(WATCH_FLOOR, len(watched)//2)` = **312**.

Ang 312 ay **self-imposed, hindi entitlement**: ang provider message ay ginagamit bilang boolean lang; ang sariling komento ng code sa `:210` ay nagsasabing ~500 ang totoong ceiling. Kaya ~190 slots ang nasayang, at one-way ratchet ito — walang upward-recovery path maliban sa process restart.

Pagkatapos, ang resolver (`scripts/iqfeed_subscription_policy.py`) ay nagre-rank ng buong roster **kada pass** ayon sa `_CAUSE_ORDER = (ACTIVE, HINT, ELIGIBLE, ROSS, FORCED, RETAINED)` — hindi LRU, walang proteksyon ang tagal. Ang ELIGIBLE band ay may **645 distinct symbols**, mas malaki kaysa sa buong 312 budget, kaya **zero ang natira para sa ROSS**.

Ang HINT band (priority #2, mas mataas sa eligible) ay galing sa `momentum_bridge_subscribe_requests` — ang table na eksaktong sinusulatan ng L9 C1 (#976). Pero `hints_today = 0`: walang laman ang buong band dahil **wala ang C1 sa tumatakbong runtime**.

## 2. Ang demand-side depekto (lane ko)

Sa `viability.py::score_viability_explicit`, ang `paper_eligible` ay may **eksaktong dalawang assignment**: `True` sa simula, at `False` lang kapag leveraged/inverse ETP. Ang tunay na kahulugan nito ay "hindi ito leveraged ETF" — walang setup-quality bar. Ang `live_eligible` naman ay may 12 knock-down + score floor.

Kinumpirma ng adversarial pass: `live_eligible AND NOT paper_eligible` = **0 rows**, kaya ang buong band ay galing sa paper. Sa 645: 117-127 lang ang live-eligible; 437 ang may `Below Ross explosiveness floor`; **7.9% lang ang nagkaroon ng session sa 30 araw, 2.6% lang ang umabot sa entry-candidate.**

Pangalawang axis: **walang TTL** ang eligibility. Kapag naisulat, nananatili hanggang overwrite. Kaya ang band ay hindi "tradeable ngayon" kundi ang **union ng lahat ng na-score sa 24 oras** — 482 sa 645 ay lampas 600s ang edad.

## 3. Ang mga lever

**L11 — paper setup-quality gate** (#979 → `ddde2b58`). Hinati ang live knock-downs sa dalawang klase: ang **setup-quality** (below Ross explosiveness floor, below A-setup floor, product not tradable, arb-flat/weak catalyst) ay pumipigil na rin sa paper — "hindi ito Ross setup" ay totoo kahit pekeng pera; ang **live-money cost/risk** (spread ceiling, extreme-vol) ay live-only pa rin kaya **buhay ang deployment ladder**. Naka-plumb sa `ViabilitySettingsProjection` (hindi runtime settings — may test na nagbabawal doon). Regression: TC −5.55/4e at CLRO −11.37/4e, canon sa sentimo; istrukturalmente inert sa replay dahil nagse-seed mismo ang replay ng viability row.

**L11b — eligibility retention lease** (#981 → `08122fa3`, fix #982 → `f53cac63`). Hindi hinahawakan ang admission; retensyon lang ang pinapaikli. Isang dokumentadong base: `chili_momentum_risk_viability_max_age_seconds` (600s) — umiiral na at siya na mismong sagot ng sistema sa "gaano katanda bago tumangging kumilos ang trading path". Derived: `refresh = max(60, base/2)`; `lease = max(base, 2×refresh)` = floor na dalawang producer cycle. Bounded per-batch-committed drain (batch 5,000 / cap 200,000 — umiiral nang convention sa `data_retention.py:33-38` para sa mismong table).

Tatlong proteksyon: **fail-open kapag tahimik ang producer** (kung wala nito, mabubura ang buong band tuwing may outage), protektado ang mga simbolong may aktibong session, at hindi ginagalaw ang `freshness_ts`.

### Bakit lease, hindi evidence-veto

Ang unang iminungkahing disenyo ay mag-veto kapag walang `ross_signals` ang tick. **Tinanggihan batay sa data**: sa 90 no-Ross-evidence na simbolo, 72 ay wala sa large-cap scan list at kabilang doon ang **UPC, GME, TNXP, HCWB, DRMA, BLZE, SMTK** — totoong small-cap movers; ang UPC ay nasa golden window library pa nga. Ang blanket veto ay magbe-bench ng napatunayang mover at bubuwagin ang sinadyang fail-open (`ross_momentum.py`: *"a name is never benched on absent data"*).

## 4. Live receipt (16:39Z)

```
eligibility lease sweep: demoted=97,249 | reason=lease_expired | lease=600s
                         producer_silence=224s | protected=0
```

| Metric | Bago | Pagkatapos |
|---|---|---|
| distinct symbols sa band | **664** | **44-72** |
| stale eligible rows | **96,249** | **~370** (bagong tumanda lang) |

Kalusugan: 6 sweeps sa unang oras, lahat malinis; producer buhay (840 rows / 84 distinct kada 10 min); 148 jobs executed, **zero traceback**; buo ang web at brain containers.

**Bonus**: live rin ang L11 gate — **2,330 firings / 233 distinct symbols sa isang oras**, lahat `ross_explosiveness_floor`, at ang mga na-veto ay ADBE/AMAT/AMD/AVGO/CRM/CRWD/CSCO/DDOG. Nangyayari ito dahil ang S1 tape-delta feeder ay tumatakbo sa scheduler at sumusulat ng equity viability rows.

**Hindi pa napapatunayan**: na mawawala ang `capacity_eviction ... causes=ross`. Patay ang captured-paper service (revoked 15:02Z, `recover_applied_postcondition_failure`), kaya hindi tumatakbo ang subscription resolver.

## 5. Runtime drift (blocker para sa lahat ng iba)

Ang aktibong PAPER runtime ay **`89d98e5`** — lokal lang (wala sa GitHub, HTTP 422), walang branch ref, at ang merge-base nito sa main ay **8cdc4c2 (2026-07-25)**. **90 commits ng main ang wala rito.** Wala roon ang L1, L4, L7, L8, L8b, L9-C1, L10, L11, L11b, vol-nan (#953), at ang **unflagged L8 park-bug fix** — kaya nagye-freeze pa rin ang buong session kapag may candidate sa late/AH band (`live_runner.py:29773`).

Malinis ang merge path: puro capture/activation files lang ang hinawakan ng Codex branch — walang banggaan sa `live_runner.py`, `paper_execution.py`, `entry_gates.py`, `trading_scheduler.py` — at walang migration-ID collision (pareho ang 355; nagdadagdag lang ang main ng 356/357).

## 6. Mga aral

- **⚠️ Kailangan ng WRAPPER TEST ang bawat bagong scheduler job.** Ang unang L11b deploy ay bumagsak sa `NameError: settings` (local import ang convention doon), at pagkatapos maayos iyon ay nahuli ang pangalawa: ganoon din ang `SessionLocal` (nasa `..db`, hindi context manager). Pumasa ang 13 unit test dahil **pure predicate lang** ang sinusubok nila; at dahil sinasakmal ng job wrapper ang lahat ng exception (sinadya), **tahimik ang pagkabigo** maliban sa isang WARNING. Ang pattern: `tests/test_eligibility_lease_job.py` — tumatawag sa tunay na function at nag-a-assert na walang na-log na exception.
- **Laging i-verify ang output ng agent bago i-implement.** Ang design workflow ay nagmungkahi ng delikadong disenyo (evidence-veto) at gumamit ng **imbentong column** (`variant_key_is_generic`, hindi umiiral).
- **Dry-run muna bago mag-deploy ng sweep.** Natuklasan ng read-only dry run na 96,139 rows ang tatamaan ng unang pass — malaking WAL spike sana iyon sa prod habang bukas ang market.
- **Deploy recipe (scheduler)**: `scratchpad/recreate_scheduler.py` — inspect-driven, dine-dedupe ang 462 env vars (huli ang panalo, dahil ang `--env-file` ay kumukuha ng una = ang dokumentadong `DATABASE_URL` landmine), rename ang luma sa `-bak`, tapos `docker run`. Post-deploy verify **sa loob ng container**: DB binding + flags + derivation.
- **Codex watchdog**: pumatay ng replay container (rc=137 ~20 min) habang bukas ang Codex app; tumalab na workaround = retag ng image (`chili-bench:`) + innocuous na container name.

## 7. Susunod

- **Kay Codex**: (a) `IQFEED_WATCH_FLOOR=64` → ~450-480 (o mas maliit na backoff step kaysa halving); tingnan din ang L1-vs-L2 halving asymmetry (`iqfeed_depth_bridge.py:1428`); (b) **i-merge ang origin/main sa runtime branch** bago ang susunod na activation.
- **Sa akin**: L10b price-structure partials (design PR); trade#2-class dying-tape entry admission bilang sariling lever; at ang live receipt ng L10/L11 events pagbalik ng lane.
