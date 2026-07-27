# Captured-paper startup evidence handoff — 2026-07-27 (madaling-araw, 3 fires)

PARA KAY CODEX (subscription/provider layer owner). Tatlong ActivatePaper fires ngayong
madaling-araw (01:05, 02:04 sa 8cdc4c2; 03:01 sa 4a8f935). Bawat isa: BUONG validation
GREEN (61-test roster, ValidateOnly VALIDATED), Apply nagsimula, service nag-boot,
namatay sa startup, rollback KUMPLETO at malinis bawat pagkakataon. Ang mga natitirang
armadong fire (04:15 Backup / 05:00 Retry) ay DISABLED ng operator — deterministic ang
palya; huwag i-re-enable bago ang subscription-layer fix.

## Layer 1 — NAAYOS NA: pressure-sample staleness (commit `4a8f935`, landed sa
`codex/captured-paper-takeover`)

Ang 0105/0204 rejections (`QUEUE_INGRESS_REJECTED`) ay (kahit bahagyang) dahil sa
composition-time single pressure sample na `pressure_sample_max_age_seconds=5.0` lang
ang buhay habang ang selection initial publish ay minuto ang layo — ang EXACT staleness
na dokumentado at inayos ninyo sa capture-only smoke (`iqfeed_capture_only_smoke.py:436`,
caller-sampler sa max_age/2) pero hindi na-port sa service composition.
`_CapturedPaperPressureFeedWorker` (bagong UNANG pre-authority worker, bago ang
selection) ang nag-po-port ng pattern sa managed-worker lifecycle: isang synchronous
feed sa start + non-daemon refresh thread kada max_age/2; sampling failure = fatal +
natural fail-closed staleness. Focused tests 80/80 (service + supervisor + selection
integration; extended ang doubles sa `pressure_controller` surface; ang selection
worker sa integration test ay by-name lookup na). PUMASA ito sa buong sealed
validation ng 0301 run — legit ang fix, kulang lang.

## Layer 2 — NATITIRA (INYO): IQFeed v3 subscription client — ang kilalang
"provider socket WinError 10038" fix direction

Ebidensya mula sa 0301 run (`C:\chili-paper\premarket-20260727_0301-4a8f935`):

1. **03:32:16.781 `CRITICAL IQFeed selected-field acknowledgement mismatch`**
   (service stderr-dup, activation/*/handshake/): hiningi ang 18-field roster
   (may `Most Recent Trade Date`, `TickID`, `Bid Time`, `Ask Time`, `Delay`,
   `Decimal Precision`); ang in-ACK ay 16-field roster na may `Open/High/Low/Close`
   — mukhang LEGACY-bridge-style roster. Sinundan ng
   "ignored non-authoritative update-field roster". Ito ang omen: may
   IQConnect session/roster state inconsistency sa connect time.
2. **03:32:26.25/.33** — GMEX watch send bumagsak sa **`[WinError 10038]`**
   (operation on a non-socket = sarado na ang socket object sa ilalim ng sender)
   sa L1 AT L2 sabay (`watch_send_indeterminate`, `command_index=1/1` at `1/3`)
   → "connection invalidation required" → supervised reconnect nagsimula.
3. **03:32:26.40** — habang nasa loss window: ang selection initial publish ay
   `QUEUE_INGRESS_REJECTED` → poison → `ROLLBACK_NOT_QUIESCED` →
   `SERVICE_SHUTDOWN_INCOMPLETE` → `STARTUP_RECEIPT_UNAVAILABLE` → apply_failed →
   rollback_completed (malinis).

Mga NA-RULE-OUT para sa 0301 reject (para hindi na ninyo balikan):
- Tunay na resource pressure: sinukat ko sa mismong kondisyon — cpu 22–36%
  (enter=92), fsync sa C:\chili-paper 3–9ms (enter=100ms), memory/disk malalayo
  sa margins; at ang feeder ay nagpapakain kada 2.5s nang matagumpay
  (breadcrumb: pressure_feed start END nang malinis).
- Shared-budget bandwidth tokens: nagsisimulang PUNO (`_write_tokens = max_bytes`).
- Queue overflow / byte budget: unang event sa sariwang queue, 1.9GB bounds.
- Dalawang-controller theory: iisa lang ang instance (bootstrap :1448–:1482 —
  parehong object sa shared budget at composition; ang selection-queue ingress sa
  service :6372 ay gumagamit ng `shared_store.shared_admission_budget.pressure_controller`
  = parehong instance).

Ang natitirang hypothesis na tugma sa 150ms timing: ang lane-invalidation/coverage-gap
handling sa loss window ang nagpa-reject ng submit (hal. ingress/state closed o gap-path
interplay) — ang ugat ay ang subscription client na nagkakasira ng sariling socket
pagkatapos ng roster-ack mismatch. Posibleng kasangkot ang legacy-bridge IQConnect
session state (ang legacy bridges ay tumakbo hanggang sa cutover quiesce; ang roster na
in-ACK ay mukhang sa kanila).

## Iba pang artifacts
- py-spy dumps ng chain processes: scratchpad `pyspy_dumps/` (ang isolated -I service
  ay hindi ma-dump ng py-spy — os error 234; ang breadcrumbs ang pumalit at sapat)
- Ang 0204 run (pre-pressure-fix) at 0105 run ay buo pa sa C:\chili-paper para sa
  paghahambing (ang 0204 stderr ay hindi ko nasuri para sa 10038 — posibleng pareho)
- Fire script pins: `$EXPECTED_HEAD=4a8f935...`, artifact label `-4a8f935` na
  (C:\chili-paper\a86-premarket\activate-paper-premarket.ps1)

## Kalagayan ng host
Malinis: rollback_completed sa lahat ng 3 runs (candidate task absent, bridges/tasks
restored), `live_cash_authorized=false` sa bawat receipt, zero orders, walang natirang
mutation. Ang Early task ay one-shot na natupok; Backup/Retry DISABLED.
