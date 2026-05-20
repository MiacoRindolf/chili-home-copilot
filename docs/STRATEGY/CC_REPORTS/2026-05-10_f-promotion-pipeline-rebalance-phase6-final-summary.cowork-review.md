# COWORK_REVIEW — promotion-rebalance-phase6-2026-05-10

Reviewed by Cowork scheduled-task at 2026-05-10T17:32Z.

## Verdict

**Phase 6 PASSED clean.** Doc-only closeout; no .py/migration changes. CC
report contains no WARN / FAIL / regression / STOP / ABORT / parity-break
markers. Hard-rules check passed (no live-placement-safety changes, no
prediction-mirror authority change, sequential idempotent migrations).

Initiative summary as written in CC report:
- Phases 1–4 SHIPPED in repo (mig 235/236/237).
- Phase 5 DEFERRED (daemon launch error; brief preserved).
- Pattern 585 vindication: 25% pre-rebalance WR (gate-laundered, n=8) →
  96.7% rolling-30 directional WR; composite 0.843. ~70 pp gap quantifies
  the rebalance hypothesis.
- Phase 4 ships dormant (`chili_cohort_promote_enabled=False`). Operator
  controls activation per the deploy steps in the CC report.
- Pattern 586 auto-demote between Phase 2 and Phase 6 = Phase 1's
  AND-logic firing correctly. Expected behavior.

## Pause flag NOT removed (STEP D deviation, with rationale)

STEP D would normally clear `_claude_session_pause.flag` and append
`REVIEWED-AND-UNPAUSED`. **Cowork is intentionally leaving the pause flag
in place** because:

1. The pause flag itself is suspicious — file is 190 bytes, truncated
   mid-sentence at `(no .py/mig` with no trailing newline. Pattern matches
   the PowerShell `Out-File` BOM/truncation bug already in user memory.
   The flag's claim of "SCOPE-DRIFT during Phase 6" cannot be verified
   because git is broken.
2. **Git index is corrupted** since 16:56Z (`fatal: unknown index entry
   format 0x4c460000`). `git status --porcelain` returns exit 128. STEP
   B2 scope-drift detection is disabled until operator rebuilds the
   index. The next queued session is `phase5-retry`, where Phase 5's
   allowed scope is brain-side only — without git, drift cannot be
   detected, so unpausing now is unsafe.
3. **Daemon is hung.** Probes re-pended this run did not refresh
   `dispatch-crypto-pulse-out.txt` (10:18:20Z, ~7h 11m stale) or
   `dispatch-autotrader-health-out.txt` (12:01:29Z, ~5h 28m stale).
   `_claude_pending.txt` disappears post-append without daemon pickup.
   Unpausing achieves nothing while the daemon is hung.

## Operator action required (carried forward from prior runs)

- Rebuild `.git/index` (`git read-tree HEAD` or `rm .git/index && git
  reset`).
- Restart dispatch daemon (`scripts/_claude_daemon.ps1`).
- Fix probe schema drift in
  `scripts/dispatch-autotrader-health-probe.ps1` and
  `scripts/dispatch-crypto-pulse.ps1` (`occurred_at` / `t.broker` /
  `t.qty` columns no longer exist).
- Rotate `GROQ_API_KEY` (auth_failed key in cascade).
- After daemon healthy: re-verify exit-monitor cycles + ADA-USD #1808
  and ACS-USD #1842 stop-protection state.

## Carried-forward UNPROTECTED_POSITION concerns (stale-data reference)

From the **last-known snapshot only** (cannot be re-verified while
daemon is hung):

- **ADA-USD trade #1808** — `bracket_reconciliation` writer fires
  `place_missing_stop ok=false reason=venue_unsupported_crypto_path` on
  every sweep. Robinhood crypto-stop primitive gap. Position has been in
  this loop for ≥ several hours per prior pulse outputs. Operator should
  treat as UNPROTECTED on resume.
- **ACS-USD trade #1842** — `crypto_exit FIX A-5b: cannot resolve broker
  qty (local_qty=1564945.0); deferring sell` recurring every ~30s.
  Plus `stop_engine FALLBACK_FIRED` ATR=None at very low price tier.
  Exit attempts are being indefinitely deferred.

These are not fresh-data ESCALATE-AUTOTRADER entries (probes are stale)
but they are the open positions the operator flagged as the highest
overnight concern. Surface immediately on operator wake.

-- Cowork (autonomous, scheduled task)
