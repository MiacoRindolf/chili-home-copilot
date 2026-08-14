# 2026-08-14 — Window 1: unang takbo ng legacy Alpaca paper lane (W20260814-01/01B)

## TL;DR

Ang unang operator-authorized time-share window ng legacy lane ay tumakbo nang buo
(08:06–20:00 UTC, dedicated na bagong $12.5k paper account c7d421e0…) na may **zero
trades** — pero natuklasan at naayos ang DALAWANG istrukturang harang na
magpapawalang-bisa rin sana sa bawat susunod na window:

1. **Maling equity rail (umaga, 08:06–15:53):** `CHILI_EQUITY_EXECUTION_RAIL=
   robinhood_agentic_mcp` sa `.env` → ang equity arms ay nakaturo sa RH LIVE rail
   (hindi konektado dito) sa halip na Alpaca paper. AYOS: `CHILI_MOMENTUM_EQUITY_
   EXECUTION_VIA_ALPACA_PAPER=true` + `AUTO_ARM_EQUITY_ONLY=true` sa process-scoped
   WINDOW_ENV (supervisor v3.3); restart bilang W20260814-01B.
2. **Loss-guard zombie deadlock (buong araw, natuklasan post-close):** 814 na
   `live_error` sessions mula 06-12..07-10 (ended_at NULL, walang account-generation
   stamp) ang binu-bump ng stale-session reaper araw-araw → pumapasok sa bawat
   kasalukuyang ET-day terminal inventory ng `load_current_live_loss_history` →
   `loss_guard_account_generation_unknown` → **146/146 auto-arm passes skipped**.
   AYOS: **mig359 (PR #1027 → main `2cfff46`)** — `ended_at = started_at + 1 day`
   backfill; 2,116 rows na-terminalize; verified na `CURRENT_LIVE_COMPLETE` na ang
   reader sa prod.

## Ano ang NAPATUNAYAN ngayong araw (unang beses lahat)

- Supervisor v3.2–v3.4: lease (ALPA/OWNR) + Job Object + fail-closed census + ACCEPT/
  PREPARED receipts — buong lifecycle nang dalawang beses (kasama ang graceful
  restart mid-window at eksaktong on-time shutdown sa 20:00 UTC)
- Broker census sa bagong dedicated account: malinis (ACTIVE, 0/0, cash 12500)
- **Order plumbing: tunay na submit → broker ACCEPTED → cancel** (1sh F limit@$1.00,
  `client_order_id=claude-window1b-plumbing-smoke-test`) — walang natirang epekto
- Kill switch CLEAR, drawdown breaker HINDI tripped, user binding tama
- Selection buhay buong araw: 609 symbols / 36,590 eligible rows (segment B lang),
  leaders ONFO/SXTC/WETO/AKAN/BANL; float gate, promotion-check backtests tumakbo
- #1026 dispatch escape + L13 + buong lever stack: naka-deploy sa window build

## Mga aral (idinagdag din sa memory)

- **Ika-apat na report-binding-not-defaults**: ang `.env` rail binding ang
  nag-nullify sa buong misyon ng umaga. Window 2 preflight: i-print ang EFFECTIVE
  routing (`_lane_execution_family()`) bago ang open.
- **Ang change-only skip logging ay TAHIMIK kapag pare-pareho ang dahilan** — ang
  `skip=loss_guard_history_unavailable` ay lumabas MINSAN (08:58) at hindi na
  naulit kahit iyon ang estado ng lahat ng 146 passes. Kailangan ng per-pass
  attribution (nakalista sa Window 2 prep).
- Supervisor PREPARE census race: ang 5s fixed sleep ay kulang sa 1.7GB uvicorn
  teardown → na-detect ang sariling app → clean=False nang walang tunay na dahilan.
  AYOS na sa v3.4 (hintayin ang app exit + app_pid exemption).
- git push over HTTPS ay HUMAHANG sa box na ito ngayon (kahit token URL; gh api
  gumagana) — ang branch ay nailathala via git data API (blob→tree→commit→ref).

## Estado pagkatapos ng araw

- main = `2cfff46` (mig359); wt-rossparity tree = IDENTIKAL sa main (verified
  tree sha `ad2d457f…`; ang lokal na branch label ay `lane/legacy-live-error-
  terminalize` dahil hang ang fetch — kosmetiko lang)
- PREPARED receipt ng Window 1B: `prepared_20260814T200018Z_a7edd453…` (clean=False
  dahil lang sa self-detection race na naayos na sa v3.4; substance malinis:
  broker 0/0, counters 0, lease verified)
- Account: buo ang $12,500; isang canceled smoke-test order lang ang history

## Window 2 (mungkahi, kailangan ng operator go)

1. Parehong Option C setup, buong araw 08:00–20:00 UTC (may premarket na ngayon —
   ang 13:30–15:00 UTC ang pinakamainit na oras ni Ross na hindi nasaklaw ngayon)
2. Bago ang open: preflight na nagpi-print ng effective rail + loss-guard coverage
3. Idagdag ang per-pass arm attribution logging (maliit na PR) para kung walang
   trade ulit, may numero tayo kung aling gate at gaano kalayo ang mga kandidato
