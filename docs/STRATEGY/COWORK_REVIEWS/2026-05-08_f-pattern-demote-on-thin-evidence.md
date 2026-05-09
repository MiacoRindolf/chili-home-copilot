# COWORK_REVIEW: f-pattern-demote-on-thin-evidence

**Verdict:** Mixed. Sweep code is correct; predicate and 15 tests
are tight; pattern 585 demoted cleanly when called directly. **But
the wiring is dead** — CC hooked into an event-driven dispatcher
event (`execution_feedback_digest`) that the brain-worker logs show
is firing at `processed=0 claimed=0` per cycle. Pattern 585 sat
`promoted` for 75+ minutes after restart. Hand-kicked the sweep
to recover; queued `f-pattern-demote-sweep-wiring-fix` to fix the
hook.

## Algo-trader lens

The code that ships does the right thing in the right shape:
ignores `avg_return_pct` (correctly identifies it as outlier-fragile
on tiny N), gates on the four criteria the brief specified, demotes
to `challenged` (not `decayed`, which is a different lifecycle
state owned by `run_live_pattern_depromotion`). Hand-kick output:

```
result: {'ok': True, 'demoted': 1, 'demoted_ids': [585]}
```

Pattern 585 → `challenged`, `promotion_demote_reason='thin_evidence_low_realized_wr'`,
`demoted_at=2026-05-09 00:04:25 UTC`. 1011 and 1016 stay
`promoted` (correct — they have N=409/565 and WR=63/70%). 1047
unchanged (correct — already `challenged` from the EV gate on
2026-04-28).

Alert volume in the 5 minutes after the hand-kick: **0**. The
`pattern_imminent_alerts` filter on `lifecycle_stage='promoted'`
worked as designed. Pre-fix volume was 158/24h (100% from 585).

## Dev-architect lens — what went well + what went wrong

**Went well:**
1. **Three demote concerns identified, properly delineated.** CC
   audited the existing `handle_trade_closed` (EV-gate, event-
   driven) and `run_live_pattern_depromotion` (live-vs-OOS gap,
   target lifecycle `decayed`) and correctly identified that
   neither could carry the new criteria-driven sweep. Implemented
   `run_thin_evidence_demote` as a third sibling. Right call.
2. **Raw SQL for the UPDATE.** `demoted_at` and
   `promotion_demote_reason` exist in the schema but not on the
   ORM `ScanPattern` class. CC chose raw SQL over expanding the
   ORM — surgical, avoids cascading into other handlers.
3. **15 tests, all passing**, including boundary tests
   (`trade_count=10` boundary at the threshold), JSONB-as-string
   surface compatibility, and audit-fingerprint replay tests for
   patterns 585, 1011, and 1047.
4. **`promotion_status` left untouched** — held the line on the
   brief's exact column list rather than over-writing.

**Went wrong:**
1. **The wiring choice was untested in vivo.** CC's open-question
   #1 said "Verified: the sweep rides on the
   `execution_feedback_digest` work event, which fires whenever
   the existing `run_live_pattern_depromotion` runs. Per brief,
   that's per-cycle today; pattern 585 should demote within
   1-2 brain cycles after deploy." The verification was based on
   reading code, not observing live brain-worker behaviour.
   The actual log evidence:

   ```
   [brain] work ledger dispatch round
   processed=0 claimed=0
   per_type={'execution_feedback_digest': 0, ..., 'live_trade_closed': 0,
             'paper_trade_closed': 0, 'broker_fill_closed': 0, ...}
   ```

   ALL 9 event types at 0/cycle. The dispatcher is purely
   event-driven; with no upstream producers active (no live
   trades closing, autotrader's PDT count is 0 so no entries
   have placed), nothing enqueues `execution_feedback_digest`.
   The "per-cycle" claim was based on `run_live_pattern_depromotion`
   running per-cycle, but that function ALSO requires an
   `execution_feedback_digest` event to fire — same dependency,
   same gap.

2. **Pattern was recoverable, but only via direct invocation.**
   `docker exec ... python -c "run_thin_evidence_demote(SessionLocal())"`
   demoted 585 in <1s. Operator-facing this means: the fix is
   real, but fragile. Future thin-evidence patterns won't be
   caught autonomously until the wiring is fixed.

## Lessons

1. **Verification must be live, not just code-read.** Today's two
   instances of the "tests-not-AST" lesson (sentinel-shadowing in
   the PDT brief, mock-collision in the rotator brief) were both
   caught at the test-suite level. This one is the next layer:
   *integration verification*. CC's tests pass, the predicate is
   correct, the SQL UPDATE works. But the dispatcher hook never
   fires in the live system. Going forward: when a brief's
   acceptance criterion is "X happens within N brain cycles
   after deploy," verify it actually happens — not just that the
   code path exists.

2. **Empty work ledger is a separate signal.** The fact that ALL
   9 event types are at 0/cycle today raises a structural
   question: is the brain learning loop actually receiving
   inputs? Surfaced in the wiring-fix brief's open question #1
   and worth a separate follow-up audit. If the work ledger is
   structurally empty for reasons unrelated to PDT (e.g.,
   no breakout alerts being resolved, no backtests completing),
   that has implications well beyond the demote sweep.

## Recovery + What's left

### Immediate (already done)

- Hand-kicked `run_thin_evidence_demote` via daemon. Pattern 585
  demoted. Alert pipeline went silent.
- Patterns 1011/1016 remain `promoted` (correct — they don't
  match the criteria). They'll fire when their setup conditions
  are met.

### Queued

`f-pattern-demote-sweep-wiring-fix` — wires `run_thin_evidence_demote`
into `run_brain_work_dispatch_round` (the per-cycle dispatcher
loop) instead of the event-driven `_handle_execution_feedback_digest`.
The per-cycle loop fires every ~75-90s today regardless of
work-ledger state. Sweep is cheap (one SELECT + tiny UPDATE),
idempotent, conceptually correct in that location.

Acceptance criteria include: live-verifiable hook fires every round,
sweep failure doesn't poison the round, and a chili_test
seed-and-sweep test that exercises the round-level hook directly.

### Three queued briefs remain parked

- **`f-pdt-crypto-bypass-cleanup`** — hygiene; ship anytime.
- **`f-autotrader-pdt-aware-exit-deferral`** — premise was flawed
  (no real autotrader same-day round-trips); needs rewriting.
- **`f-equity-broker-reconcile-wipeout-protection`** Phase D — if
  the 7-day post-R32 phantom soak shows new phantoms post-Phase-C,
  reopen with that data.

### Suggested next direction

`f-pattern-demote-sweep-wiring-fix` — small scope, completes the
intent of today's brief. Without it, the pattern lifecycle has a
slow leak: today's 585 was hand-fixed, but the next
`provisional_small_paths` promotion will sit at `promoted` until
the next manual intervention.

## Final note on the audit-first protocol

Today's session used audit-first cleanly twice (Phase B's
post-R32 phantom audit determined Case C; the pre-deploy pattern
audit confirmed only 585 would be demoted). Both worked. This
brief's failure mode wasn't audit-first — it was
*integration verification*. The brief's "Open questions" section
asked CC to verify cadence; CC reported "verified" based on
code-reading. The lesson for future briefs: include an explicit
"prove the hook fires" step in the acceptance criteria, with a
log-grep or counter check that runs against the live system, not
the code.
