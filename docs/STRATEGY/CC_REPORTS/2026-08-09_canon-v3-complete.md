# 2026-08-09 — Canon v3 kumpleto (21/22) + #996 merged: ang buong linggo ng sukatan, sarado

**Merged ngayong araw:** [#996](https://github.com/MiacoRindolf/chili-home-copilot/pull/996) → main `f716b82` (admin, explicit operator order; bingi pa rin ang Actions sa PR events ng repo — tatlong trigger ang nabigo: sariwang push, close/reopen, bagong PR #997). Naka-integrate sa typed-prefix helper ng #998 ni Codex: ang sampler ay tumatakbo lang sa purong-fallback na landas. 26/26 pasado ang pinagsamang census suite sa local.

## 1. Ang canon v3 @ `fbfa7f2` — kumpleto na ang sukatan

21 sa 22 windows, arm `intended`, net **−1,416.20**. Nasa canonical root (`replay_batch/scorecard.{json,md}`) ang buong talaan; ang v2 ay naka-archive.

**Ang istruktura ng talo ang kuwento:** tatlong sakuna-window ang 84% ng gross na pula —

| | PnL | E/X | Hugis |
|---|---:|---|---|
| VTAK 07-08 | −748.39 | 24/25 | buong-araw na re-entry sa pabagsak |
| CWD 07-02 | −309.20 | 18/19 | gayundin |
| LHSW 07-06 | −252.29 | 20/20 | gayundin |

Ang natitirang 14 na pulang window ay churn na −106 lang KABUUAN, laban sa +149.94 mula sa apat na green (TC +6.72, JLHL +19.20, HYFM +24.46, **CELZ +99.56** — 6 entries/11 exits, patunay na kaya ng makina ang malaking green kapag tumakbo ang scaling).

**ROI map (ranked, ebidensya-basado):**
1. **Sakuna-class**: ang tatlong itaas ay iisang mekanismo — walang tumitigil sa symbol-day pagkatapos ng sunud-sunod na talo. Kandidatong lever: symbol-day loss lockout (ang "walk away" ni Ross) o backside-regime detect. Ito ang pinakamalaking pera.
2. **Churn-class**: TVRD (29e, −68), LHAI (17e, −12), CLRO 07-07 (15e, −18) — ang L2a program; ang bail retirement ay merged na (#963), kaya ang natitirang suspek ay `breakout_failed_to_hold` na HINDI arm-isolatable — kailangan ng per-exit-path attribution bago ang lever.
3. **Winner-press**: CELZ ang patunay; ang partials-into-strength/scaling ang susunod na pag-aaralan pagbukas ng paper conversion.

**Scale-grid verdict (pinal):** ang VTAK `intended-minus-scale-grid` ay **−684.11 nang eksakto, 3× deterministic, kasama ang fill signature** (bumalik ang hindi-pantay na 216.69/1227.88 sells). Net na epekto ng grid sa buong canon = **−64.28, puro VTAK, zero positibong ambag saanman**. Malakas na kandidato ang pag-OFF; desisyon ng operator + isang confirm-A/B kung nais.

**Exclusion:** UPC|06-29 — 3× timeout hanggang 18000s (5h); patolohiyang lampas sa density (kapwa-1:1-density na JEM 2.38M events ay natapos sa 3h). Nakapila ang py-spy diagnosis.

## 2. Ang linggo ng infra forensics — apat na magkakapatong na sanhi

Ang "coverage_unavailable na gabi" ay APAT na magkaibang depekto na nagkataong sabay-sabay:

1. **Walang `.env` ang sariwang worktree** → ang post-run mine step (bumubuo ng app `Settings`) ay ValidationError → bawat row minarkahang untrusted. Lunas: `export DATABASE_URL` sa chain scripts. (Ang phase 1 ay "gumana" dahil ang wt-rossparity ay may `.env` — tsamba, hindi disenyo.)
2. **Na-reset ng WAL crash recovery ang pg_stat** (docker restart 08-07) → walang planner stats → seq scans → statement timeouts. Lunas: ANALYZE — pero hindi ito sapat mag-isa:
3. **Ang NBBO receipt query ay pinapatay ng planner** na pumipili ng `observed_at`-only index (binabasa ang 2.53M row ng LAHAT ng symbol sa oras-range, 30× maling estimate) sa halip na ang symbol index. Lunas: **DROP ang `replay_golden_nbbo_observed_at_idx`** + bagong `(symbol, observed_at, id)` index sa parehong golden table → receipt 5.9s mula sa >60s timeout. Walang sealed code na ginalaw — data-side lang, reversible.
4. **1:1-NBBO-density windows** (ang 06-29..07-02 rescue pins) = 1–2.4M events bawat window sa ~160 events/s ng engine → kailangan ng 10800–18000s timeout, hindi ang default na 3900s.

Ang bawat isa ay may kanya-kanyang bantay na ngayon sa working memory; ang huling batch ay tumakbo nang **zero coverage_unavailable sa 13 sunud-sunod na window**.

## 3. Golden archive

Anim na bagong pin (DSY/NAMI/MB/CLRO 08-07 + CLRO/WYHG 08-06 — ang +$37k day ni Ross) na may hatinggabing AH top-up; resibo sa `project_ws/_tools/golden_library/ross_0806_0807_inventory.json`. Kasama rin dito ang 4/4 Ross-name eligibility cross-check ng 08-07 (CLRO/NAMI/DSY/MB lahat paper+live eligible, timestamps tugma sa salaysay ni Ross) — ikatlong sunod na araw ng selection hit; **conversion pa rin ang nag-iisang bottleneck**.

## 4. Estado ng conversion (ang tunay na pera)

Zero fills pa rin kailanman. Ang landas: ① reseal ni Codex mula `f716b82` (hiniling sa `CODEX_RESEAL_REQUEST_2026-08-09.md`) → ② unang RTH census na may `raw_samples` = ang tunay na rejection reason → ③ ayusin ang dahilang iyon (strategy side man o sealed admitter) → unang paper trade. Kasabay: ang selection→admission na bagong hinto sa 9294202-era builds ay kay Codex.
