# Ang arm path ay polling; dapat itong instantaneous

Bawat numero rito ay nasukat sa buhay na lane noong **2026-08-26**.

## Ang sintomas na binabayaran natin

| sinukat | halaga |
|---|---|
| ignition na **na-queue** (naghintay sa tumatakbong pass) | **2,368** |
| ignition na **tumakbo** | **341** |
| **nalunod** | **86%** |
| paghihintay ng ignition | p50 **4s** · p90 **14s** · max **55s** |
| auto-arm pass | naka-schedule kada **10s**, p50 **25.8s** |
| nalaktawang tick (`max_instances=1`) | **81** |
| pagitan ng tick sa isang sesyon | ~**7 minuto** |

Ang bunga, dalawang beses na naitala at dalawang beses na nangyari:

- **Zero submit.** `live_runner.py` (2026-08-19): *"a median of 408s — SEVEN
  MINUTES — between 'this is a good entry' and the pre-submit quote check, so the
  planned limit was minutes stale... 14 `execution_bbo_above_planned_limit`
  defers and ZERO `live_entry_submitted` on a day with 53 pending_place."*
  **2026-08-26: 21 pending_place, 5 defer, 0 submitted.**
- **Hindi nakikita ang sariling fill.** Session 16759: `live_entry_submitted`
  17:35:36, na-fill sa broker @ 14.94, at ang sesyon ay patuloy na nag-emit ng
  **PRE-ENTRY** na event nang 6m52s. Walang stop, walang exit management.

## Tatlong depekto sa istruktura

### 1. Ang ignition ay nagtatayo ng board na hindi nito kailangan

Ang ignition ay nagngangalan ng **ISANG** simbolo. Ang scoped na landas
(`only_symbols`) ay tama nang nilalaktawan ang mabigat na reaper — ngunit
nagtatayo pa rin ito ng **40-simbolong board**: p50 **5.6s**, max **49.3s**.

Matapos ang migration 372 ang isang hilera ay **~1 ms** na basahin. Ang scoped na
landas ay dapat magbasa ng isang hilera, hindi apatnapu.

### 2. Ang hot at cold ay naghahati ng isang slot na `max_instances=1`

Sa iisang 10-segundong job:

| HOT (dapat instant) | COLD (kahit anong cadence) |
|---|---|
| ignition → arm | `_reap_stale_watching_sessions` |
| entry evaluation | `_finalize_stale_exited_sessions` |
| | `reap_stale_live_sessions` |
| | `load_current_live_loss_history` |
| | broker OAuth refresh (**20s** hanggang #1191) |
| | anim na prestart counter |

Sa `max_instances=1`, ang mabagal na COLD ay **naglalaglag** ng HOT tick — hindi
nagpipila. Iyon ang 86%.

### 3. Polling ito

Ang cadence ay 10s dahil iyon ang interval ng scheduler. Ang momentum ay hindi
dumarating sa mga hangganan ng 10 segundo. May `iqfeed_wake_listener` na sa
codebase; ang daang ito ay hindi ito ginagamit. (Naitala rin: ang 2026-08-17 na
loop→batch cutover ay **nag-alis** ng tick-speed dispatch.)

## Ang mungkahi, ayon sa halaga kada panganib

**A. Ang scoped na ignition ay huwag nang magtayo ng board** *(pinakamalaki, pinakamaliit ang panganib)*
Kapag `only_symbols` ay may laman, basahin ang mga hilerang iyon nang tuwiran sa
halip na tumawag ng `_fresh_live_eligible_candidates(limit=40)`. Inaasahan:
5.6s → milliseconds. Walang binabagong pasya; ang parehong hilera ang binabasa.

**B. Hatiin ang job sa dalawa** *(katamtaman)*
`momentum_arm_hot` (1–2s, ignition lamang) at `momentum_arm_cold` (60s, lahat ng
sweep/reaper/history). Bawat isa ay may sariling `max_instances`. Ang cold ay
hindi na kailanman makakapaglaglag ng hot.

**C. Event-driven arming** *(pinakamalaki ang saklaw)*
Ang tape ay dumarating na sa isang listener. Ang ignition ay dapat **gumising**
ng arm, hindi maghintay ng pulse. Ito ang tunay na "instantaneous"; ang A at B
ang naghahanda ng daan.

## Ang panuntunan na nawawala

> Ang isang gawaing may hangganan sa oras at isang gawaing panlinis ay hindi
> kailanman dapat maghati ng scheduler slot. Kapag ganoon, ang panlinis ang
> nagtatakda ng bilis ng kalakalan.
