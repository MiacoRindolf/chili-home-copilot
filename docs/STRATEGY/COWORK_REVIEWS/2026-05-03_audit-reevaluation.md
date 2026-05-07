# Cowork Reevaluation: 2026-05-03 Comprehensive Audit

**Reviewing:** `docs/AUDITS/2026-05-03.md` (Codex direct audit) and `docs/STRATEGY/COWORK_REVIEWS/2026-05-03_audit.md` (auditor's own priority recommendation).
**Reviewer:** Cowork (second-opinion read).
**Date:** 2026-05-03.

This is a peer note to the auditor's own review, not a replacement. The auditor's verdict and priority change stand. This document spot-checks the load-bearing findings against the actual code, reprioritizes two HIGH items, and adds shape to the recommended emergency-repair fix.

## Findings spot-checked in code

| Finding | Audit severity | Confirmed | Evidence |
|---|---|---|---|
| Open Robinhood equities + missing stops + `terminal_reject` | CRITICAL | Yes | `app/services/trading/bracket_reconciliation_service.py:668-682` returns `state_gated_skip` with no escalation path when `intent_state ∈ {terminal_reject, closed}`. |
| Venue-truth shadow log dormant | HIGH | Yes | `record_fill_observation` exists only in `app/services/trading/venue_truth.py` (writer) and `scripts/phase_f_soak.py`. No production caller. |
| Pullback exits on 4h fallback hold | HIGH | Yes | `app/services/trading/fast_path/exit_manager.py:67` — `MAX_HOLD_S_DEFAULT = 4 * 3600`. Fallback returned at lines 134, 138. |
| Unsupported crypto reaches Robinhood | HIGH | Yes | `crypto_not_supported_on_robinhood` is generated *inside* `app/services/broker_service.py:2433/2515` and surfaced *after* the broker call at `app/services/trading/auto_trader.py:1064/1071`. No pre-broker prefilter. |
| `fetch_ohlcv_batch` leaks crypto to yfinance | MEDIUM | Yes | `app/services/trading/market_data.py:670-678` — falls through to `batch_download` + `_yf_history` for all remaining tickers, no crypto skip. The single-ticker path (322-347) and DataFrame path (542-572) do skip. Inconsistent. |

All spot-checks agree with the audit. No findings overstated. No findings missed in my read.

## Reprioritization

The audit's CRITICAL stays CRITICAL. The other shifts:

**Promote — unsupported-crypto pre-filter (audit HIGH #4) to top of HIGH queue.**
- Volume: ~127 unsupported-crypto blocks + 44 + 3 broker-error rows in 24h, plus reject notifications surfacing to the operator.
- Effort: small (cached Robinhood crypto-pairs lookup before broker call, OR static capability table).
- Side effect: cleans funnel signal so true risk blocks become legible — improves diagnosis quality on unrelated incidents.
- This is the highest signal-to-effort item in the HIGH bucket.

**Demote — pullback 4h fallback hold (audit HIGH #3) by one notch.**
- Real measurement-hygiene issue, contaminates F8a.
- Paper mode only — no live risk.
- Strategically subordinate to the missing-stop CRITICAL and the unsupported-crypto pre-filter.
- Fix per signal-specific cold-start hold remains the right shape; just not next.

**Audit HIGH #2 (venue-truth wiring) keeps HIGH severity** but moves below #4 in queue. It is an audit-trail gap, not active operational noise. Important to wire because `CLAUDE.md` describes it as load-bearing.

**MEDIUM and LOW items are unchanged from the audit.**

## Shape for the emergency-repair fix

The CRITICAL fix needs more shape than "add an escalation path." Specifically the new path must distinguish three scenarios using data the `BrokerView` already exposes:

1. **`broker_qty == 0` → phantom open trade.** Close the trade row, do not re-arm. FIX 51's `skipped_broker_qty_zero` already has the logic for the no-stop case; the new path reuses it but additionally marks the trade `closed` with an audit reason like `phantom_after_terminal_reject`.

2. **`broker_qty > 0` AND no stop → real exposure, real risk.** This is the case the audit is asking us to handle. Allowed action: a controlled, single-shot bypass of the `terminal_reject` gate that calls the existing FIX-51 cap-to-broker-qty re-arm logic, emits a CRITICAL log, and re-locks the gate immediately if the placement rejects again. Optionally throttled to one attempt per intent per N hours so a second-rejection doesn't reopen the storm.

3. **Broker unavailable / `broker.available == False`.** Skip silently, retry next sweep — same as FIX 51's `broker_qty_unknown` branch.

Do not blanket-remove the `terminal_reject` gate at lines 668-682. It still earns its keep against the rejection-storm pattern that FIX 51-53 was added to fix. The new path is *additive*: a one-shot escape valve, not a removal.

Operator triage of the seven affected positions (VFS, TLS, IMTX, ELTX, CRDL, CCCC, AIDX) is a **prerequisite** to deploying the new code path. Even with the fix in place, the existing positions need a human decision (close vs hold-with-restored-stop) before the automated path can be safely flipped on for them. The new code path should ship with a feature flag (`CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED`) defaulting OFF, so the deploy can land first and the flip happens after operator triage.

## Watch items added by this reevaluation

- **Brain-worker idle-in-tx session at ~10min age.** Same pattern that drove FIX 5/13/14 (`db_watchdog tighter + TCP keepalives`). Audit flagged at LOW; I'd add a confirm-watchdog-still-firing check to the next routine sweep. If the keepalive fix has regressed, that's its own incident.
- **Phantom-detection prerequisite for the new path.** If any of the seven affected trades have `broker_qty == 0`, the emergency-repair path will mark them `closed` rather than re-arming. This is correct, but should be flagged in the operator triage step so the human knows what state to expect.

## Direction confirmation

The auditor's `2026-05-03_audit.md` recommendation stands. This reevaluation refines but does not contradict it:

1. Preempt the queued `f8b-verification-soak-3` task (already preserved at `docs/STRATEGY/QUEUED/f8b-verification-soak-3.md` for re-promotion on/after 2026-05-04 16:30 UTC). The soak task is a pure-analysis task gated on data accumulation, so deferring it costs zero.
2. Replace `NEXT_TASK.md` with the missing-stop emergency-repair brief, scoped per the shape above.
3. After CC ships and the seven positions are protected, re-promote `f8b-verification-soak-3`.
4. Add unsupported-crypto pre-filter and venue-truth wiring to the queue behind soak-3.

`CURRENT_PLAN.md` is intentionally left unchanged. The plan's broader shape (prove edge before live activation) is undisturbed; only the immediate next step changed.
