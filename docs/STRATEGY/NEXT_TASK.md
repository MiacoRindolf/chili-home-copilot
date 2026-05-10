# NEXT_TASK: f-coinbase-bracket-coverage-fix

STATUS: DONE

CC_REPORT: `docs/STRATEGY/CC_REPORTS/2026-05-10_f-coinbase-bracket-coverage-fix.md`

## Goal

Three structural bugs leave Coinbase positions unprotected at the venue.
Right now 9 open Coinbase trades have stop_loss populated in the DB row
but no GTC stop sitting at Coinbase. Real-money exposure ≈ $2,700.

## Brief (full)

The full brief is at
`docs/STRATEGY/QUEUED/f-coinbase-bracket-coverage-fix.md`.

## Phases

Single-shot fix; no multi-phase decomposition needed.

## Deliverables

- Code fix for Bug A (intent emission only fires on alert events,
  never at entry)
- Code fix for Bug B (reconciler doesn't backfill intents for trades
  with stop_loss but no intent row)
- Code fix for Bug C (writer attempts on Coinbase trade 1842 ACS-USD
  produce zero log lines — investigate why and fix)
- Tests covering the three fixes
- CC_REPORT documenting what was found and shipped
- Hot-fix SQL note for ACS-USD #1842 (operator manually closed
  yesterday; row still status=open)

## Hard constraints

- Crypto-side only. **Do not** touch options/equity entry code.
- Edit-tool truncation discipline: use `Write` for any file >500 lines.
- Coinbase Phase 6 LIVE soak active: do not disable existing protections.
- Don't add magic-fallback values for missing measurements.
- Plan-gate protocol active: write `plan.request.md`, wait for approval.
