# COWORK_REVIEW: f-promotion-pipeline-rebalance — Phase 2

CC report: `docs/STRATEGY/CC_REPORTS/2026-05-09_f-promotion-pipeline-rebalance-phase2.md`
Commit: `e480d9f`
Session: `promotion-rebalance-phase2-retry4-2026-05-09` (after 3 launcher iterations to fix the daemon)
Duration: 32m 13s of CC; full clock 4h including 4 daemon-iteration retries
Verdict: **GREEN — exemplary execution**

## What was nailed

1. **The brief's hypothesis was vindicated on day one.** The whole motivation for the rebalance initiative was the suspicion that gate-laundered realized WR was misleading us about pattern quality. CC's live smoke run revealed pattern 585 — the pattern that nearly died on 25% realized WR (n=8 trades) — has a directional WR of **73.3% on 30 alerts**. Pattern 586 also at 73.3%. The clean signal is dramatically different from the gate-filtered noise. This single data point retroactively justifies the entire Phase 1+2+4 sequence.
2. **Live-container deploy was part of the session.** Migration applied, scheduler-worker logged the new job, settings propagated to all 4 worker containers, smoke run executed inside the running container. Operator did not have to re-run anything — exactly the autonomy goal.
3. **Test-injection seams designed in from the start.** `evaluate_directional_outcomes(db, *, now=None, fetch_ohlcv=None, settings_=None)` — three injection points covering the three sources of test brittleness (clock, network, config). The 19-test suite uses these without resorting to global patches. This is the kind of code we wish all our services started with.
4. **FIX 46 hygiene was applied without being told.** The new scheduler runner in `trading_scheduler.py` follows the rollback-before-close pattern even though the brief didn't call it out. CC has internalized the connection-leak discipline; the advisor brief just confirmed what it was already doing.
5. **Smart unprompted call: `alert_id` typed INTEGER not BIGINT.** Brief draft said BIGINT; CC noticed `trading_alerts.id` is INT4 in the SQLAlchemy model and matched the FK target exactly to avoid implicit-cast index inefficiency. Correct call, surfaced explicitly in deviations.

## Two minor things

- **Pytest-asyncio plugin error on the box** is pre-existing (CC ran with `-p no:asyncio` per existing convention). Not Phase 2's problem but worth queuing a follow-up to fix the plugin invocation, since it forces every CC run to remember the workaround.
- **WinError 10055 / 10053 during the test sweep** is environmental Windows ephemeral-socket exhaustion under load. CC worked around it by stopping fast-data-worker for the duration. Not a blocker but the workaround is fragile — long-term we want a docker-compose profile that pauses heavy workers during pytest, OR a smaller test pool size.

Neither blocks Phase 3. Both go into the queue for after the chain completes.

## Answers to CC's three open questions

### Q1 — Hold window: per-pattern vs. 24h default?

**Answer for Phase 4: read `pat.rules_json["hold_hours"]` if present, else default to the global setting (currently 24).**

Rationale: scalp patterns have 2-4h windows; swing patterns have multi-day windows. Forcing 24h on a 2h scalp inflates the noise floor (the price has 22h of unrelated drift to compete with the actual signal). Conversely, forcing 24h on a 5d swing under-measures the signal.

The brief already plans for this — Phase 4's composite scoring formula is operator-tunable. Adding a `hold_hours` lookup at evaluation time costs nothing and removes a known noise source.

Phase 2 should NOT be retroactively re-enriched (additive changes to a shipped phase invite drift). Phase 4 reads `rules_json["hold_hours"]` for new evaluations. Existing rows from Phase 2 stay at 24h — the 30-row rolling window will naturally turn over to per-pattern hold windows within ~2-7 days of organic flow.

### Q2 — Threshold: per-pattern vs. 1.5%?

**Answer for Phase 4: keep 1.5% global default for now; add a per-pattern override column in Phase 4's migration if needed.**

Rationale: 1.5% is a defensible default for the equity + crypto breakouts the bulk of the roster trades on. Per-pattern thresholds are only useful if we have evidence the default mis-ranks specific patterns — and we won't have that evidence until 30 outcomes have accumulated for each pattern (i.e., 1-2 weeks of data).

If Phase 4's composite scoring shows a known scalp pattern getting penalized because 1.5% is too coarse for it, we add the column then and backfill. Premature per-pattern threshold-tuning risks Goodhart's-law overfit.

### Q3 — Phase 6 backfill: 90-day historical vs. organic accumulation?

**Answer: organic accumulation. NO 90-day backfill.**

Rationale: a backfill applies the *current* directional-correctness math to *historical* alerts. Three problems:
- Pattern definitions evolve (their `rules_json` may have changed since the alert fired). Evaluating an old alert against a new rule mismatches.
- OHLC data quality degrades for older windows (the sandbox's market-data adapters are sometimes unreliable for historical fetches; recent egress incidents prove this).
- A 90-day backfill is a one-off bulk load. We pay the OHLC fan-out cost (200 calls / 168 hours = ~28k calls for 90 days × all promoted patterns) for a result that will be slightly stale on day 1.

Organic accumulation gets to 30 outcomes per active pattern in 1-2 weeks naturally. Phase 6's verification soak (7 days post-Phase-5) gives us that data. If after 30 days we want longer history for archeology, do a backfill then with the then-current rules and a frozen analysis.

These three answers will be baked into Phase 4's brief when I write it.

## Carry-forward notes for Phase 4 (when I write its brief)

- Composite formula `w1*cpcv_sharpe + w2*deflated_sharpe + w3*(1-pbo) + w4*directional_wr + w5*(1-decay)` should source `directional_wr` from `pattern_directional_quality_v.wr` directly. View already exists.
- `directional_wr` should be NULL-tolerant in the formula — patterns with `rolling_sample_n < 10` should fall back to a default (or be filtered out of cohort eligibility entirely, since insufficient evidence). Don't use the `or 0.5` anti-pattern; either filter or propagate None.
- Phase 4 cohort eligibility should ALSO require `pattern_directional_quality_v.rolling_sample_n >= 10` to ensure the directional signal isn't itself thin-evidence noise.

## Phase 3 ahead

Phase 3 (`shadow_promoted` lifecycle) just queued at `scripts/_claude_session_queue/200-promotion-rebalance-phase3.session` with the **plan-gate prompt** active. CC will:
1. Read everything (incl. Phase 2's CC report so it understands what's now available)
2. Write its implementation plan to `scripts/_claude_session_consult/<id>/plan.request.md`
3. Wait for my response

When the daemon picks up the session and CC posts its plan, I'll review for:
- Whether the autotrader splice point is correctly chosen (the byte-identical parity gate hinges on this)
- Whether the parity test design is sufficient (must test BOTH non-shadow paths AND shadow paths)
- Whether edge cases are covered (mid-flight transitions, missing shadow-log path)

If any of those are weak, REVISE feedback. Otherwise APPROVED and CC proceeds.

Phase 3 is the highest-stakes change in the initiative. The plan-gate exists for this exact moment.
