# NEXT_TASK: f6.5-calibration-hygiene

STATUS: DONE

## Goal

Lock in the F6 findings via two small, surgical changes to the calibration consumers — without retuning any threshold. After this task:

1. **An execution-latency floor exists on calibrated `max_hold_s`** so the exit_manager can never schedule a holding period shorter than realistic round-trip latency (place + verify + exit). This isn't a strategy magic number — it's a hardware/network reality.
2. **Negatively-predictive signals are auto-excluded at the gate level** based on the data the miner is producing. If a `(ticker, alert_type, score_bucket)` combination has statistically significant negative forward edge (`mean_return + 2*stderr < 0` with `sample_count >= 30`), it's blocked regardless of score. `volume_breakout_long` would auto-block under current data; future signals get the same treatment as their data accrues.
3. **`CURRENT_PLAN.md` reflects the F6 findings.** The "edge-proof bar > 50 round trips" criterion gets superseded by the empirical answer F6 produced. F8 (signal redesign) becomes the load-bearing next move.

This is hygiene, not strategy work. It locks in the truth F6 surfaced and prevents bad decisions in the soak window between now and when F8 ships.

## Why now

F6's CC report produced three load-bearing findings:
- `volume_breakout_long` is **negatively** predictive (n=120, mean = −28.5 bps). Should not fill.
- Imbalance signals are too weak to beat trading cost at any threshold (best is +5 bps).
- Calibrated `max_hold_s` for high-score buckets is 1–5 seconds — below execution latency floor.

Without F6.5, two failure modes remain open:
- An `imbalance_long` `high` bucket gets a calibrated `max_hold_s = 1s` and the exit_manager schedules a 1-second hold. At Coinbase live latency (>100ms typical place + verify), the position can't actually fill before the time stop fires. Better to floor at 10s and let calibration pass through to the floor — if a signal "wants" to be held for 1s, that's the signal telling us it's not tradeable at our latency profile.
- Volume_breakout_long alerts continue to slip through gates if their score happens to land high. Even one fill on a −29 bps mean signal is expected-negative and waste.

## Architectural commitments

- **Don't lower the trading cost threshold.** The current behavior (calibration gate blocking almost everything) is correct given the data. Lowering thresholds without finding higher-edge signals papers over the F6 finding.
- **No new magic numbers.** The two new constants in this task are bounded by reality:
  - `CALIB_EXEC_FLOOR_S` is set by Coinbase placement + verification round-trip latency, not by strategy preference.
  - The negative-edge exclusion threshold (`mean_return + 2*stderr < 0` AND `n >= 30`) is a pure statistical rule, not a tunable.
- **Reads stay zero-cost.** Both changes execute inside the existing `calibration.py` helpers; no new DB calls, no new tables.
- **Pure, additive changes.** No constants modified, no existing behavior removed, no migrations.

## Scope — three subtasks, three commits

### 1. Execution-latency floor on calibrated `max_hold_s`

In `app/services/trading/fast_path/calibration.py`:

```python
# Hardware/network reality, not a strategy choice. Coinbase live placement
# round-trip (place + broker confirm + exit + broker confirm) is ~200-500ms
# typical. A calibrated max_hold_s shorter than this is empirically saying
# "this signal isn't tradeable at our latency profile" — but the cleaner
# expression is "we don't try to hold for less than the floor; if calibration
# argues for that, fall through to the floor and let the position prove or
# disprove its edge over a survivable horizon."
CALIB_EXEC_FLOOR_S = 10
```

Modify `get_calibrated_max_hold_s(ticker, alert_type, score)` so that when the function would have returned a value less than `CALIB_EXEC_FLOOR_S`, it returns `max(calibrated, CALIB_EXEC_FLOOR_S)` instead.

Log the substitution at INFO level the first time it happens per `(ticker, alert_type, score_bucket)` so we can see it operating without spamming. Use a small in-memory dedup set; cleared on process restart, fine.

The floor applies BEFORE the existing fallback to the legacy `MAX_HOLD_S_DEFAULT`. So the precedence is:
1. Calibrated value, if available AND >= CALIB_EXEC_FLOOR_S → use as-is.
2. Calibrated value, if available AND < CALIB_EXEC_FLOOR_S → use the floor.
3. No calibration available → fall back to the legacy default.

Commit: `feat(fast-path): F6.5 execution-latency floor on calibrated max_hold_s`.

### 2. Negative-edge auto-exclusion gate

New gate function in `app/services/trading/fast_path/gates.py`:

```python
def gate_negative_edge_excluded(alert: dict, ctx: ExecContext) -> GateResult:
    """Block alerts whose calibrated forward return is statistically
    significantly negative.

    Looks up the alert's (ticker, alert_type, score_bucket) at the
    calibrated optimal horizon. If that bucket has mean_return + 2*stderr < 0
    AND sample_count >= MIN_NEGEDGE_SAMPLES, the alert is rejected with
    reason=negative_edge.

    Insufficient samples or non-negative edge → allow (other gates still apply).
    """
```

Helper in `calibration.py`:

```python
MIN_NEGEDGE_SAMPLES = 30  # statistical floor for invoking the exclusion

def is_negative_edge_excluded(engine, ticker, alert_type, score) -> tuple[bool, dict]:
    """Returns (is_excluded, evidence_dict). evidence_dict has keys:
      - score_bucket
      - sample_count
      - mean_return
      - stderr (= sqrt(m2_return / sample_count) / sqrt(sample_count))
      - upper_ci (= mean_return + 2 * stderr)
    """
```

Wire `gate_negative_edge_excluded` into `DEFAULT_GATES` in gates.py. Order matters: place it AFTER `gate_calibrated_tradeability` (the existing F6 gate that requires positive edge above cost) but BEFORE `gate_capacity` (the existing per-pair limit). That way:

- If the bucket has insufficient calibration data → both calibrated gates pass-through, normal flow.
- If the bucket has ≥30 samples and positive expected edge → both calibrated gates allow.
- If the bucket has ≥30 samples but mean is below cost → existing tradeability gate rejects.
- If the bucket has ≥30 samples and statistically negative edge → THIS gate rejects with `negative_edge` reason.

Verify that volume_breakout_long auto-rejects after deploy:

```sql
SELECT alert_type, COUNT(*)
FROM fast_executions
WHERE decision = 'rejected'
  AND reject_reason LIKE 'negative_edge%'
  AND decided_at > NOW() - INTERVAL '5 minutes'
GROUP BY alert_type;
```

Should show `volume_breakout_long` (and only that, at current data) with a non-zero count.

Commit: `feat(fast-path): F6.5 gate_negative_edge_excluded - auto-block stat-sig negative signals`.

### 3. Update CURRENT_PLAN.md to reflect F6 findings

Append a new "Findings — 2026-05-02" section to `docs/STRATEGY/CURRENT_PLAN.md` that:

- Records the three F6 findings (volume_breakout_long negative, imbalance signals trivially small, calibrated horizon sub-10s)
- Supersedes the "edge-proof bar > 50 round trips" criterion with: "F6 has answered this across hundreds of pre-trade alert trajectories — the existing scanner signals do not produce edge that beats trading cost at any reasonable threshold."
- States F8 (signal redesign) is the load-bearing next move
- Notes F7 (Kelly sizing) is deferred until F8 produces a signal with edge worth sizing
- Adds a "What we know vs. what we don't" subsection: we know our existing signals don't beat cost, we don't know yet whether scalp-credible signals exist on Coinbase 1m crypto data at all

Don't restructure the rest of the file. Don't delete the prior priority list — just supersede it inline with a "Superseded by 2026-05-02 F6 findings" note.

Commit: `docs(strategy): F6 findings update CURRENT_PLAN`.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/fast_path/calibration.py` — extend in place. The two new helpers (`CALIB_EXEC_FLOOR_S` constant + `is_negative_edge_excluded()`) live alongside the existing F6 helpers.
- `app/services/trading/fast_path/gates.py` — add the new gate function alongside existing ones, register in `DEFAULT_GATES` tuple.
- `app/services/trading/fast_path/exit_manager.py` — uses `get_calibrated_max_hold_s()` already; the floor lands transparently with no exit_manager changes needed.
- `fast_signal_decay` table — read by the new gate via existing query patterns, no schema changes.

## Constraints / do not touch

- **Live-placement safety belts.** Same as always.
- **Trading cost threshold (`TRADING_COST_FRAC`, `TRADEABLE_COST_MULT`).** Don't lower these. F6 produced their honest verdict; we accept the verdict.
- **Score bucket boundaries.** Stay at the F6 defaults (low <0.40, med 0.40–0.65, high ≥0.65).
- **Strategy thresholds.** No tuning of any constant in `gates.py` other than adding the new gate.
- **`stop_engine.compute_initial_bracket()`.** Don't touch — the calibrated bracket path goes through `calibration.compute_calibrated_bracket()` which is the right level of abstraction.
- **The 11 inherited bootstrap positions.** Unchanged.
- **`models/trading.py` / `.env.example` working-tree changes.** Continue to leave them alone.
- **CURRENT_PLAN.md older priority list.** Supersede, don't delete; future readers should be able to see the project's evolution.

## Out of scope

- F8 (signal redesign) — separate next task. Will require operator design input.
- F7 (Kelly sizing) — deferred until F8 produces a tradeable signal.
- Lowering `TRADING_COST_FRAC`. Don't.
- Adding more horizons to the decay miner. Don't.
- Refactoring `ExecContext.engine` purity concern. Out of scope.
- TTL cache on calibration reads. Out of scope.
- Watchdog task on decay_miner. Out of scope.
- UI surface for `fast_signal_decay`. Out of scope.

## Success criteria

1. `git log --oneline -3` shows three new commits, all pushed.
2. `docker compose ps fast-data-worker` healthy after deploy. (No new behavior at the WS / book layer; risk is purely in calibration helpers.)
3. **Verify the floor is operating:** find an alert where calibration would have returned `max_hold_s < 10`, confirm via log that the floor substituted. Sample query:
   ```sql
   SELECT entry_execution_id,
          (brain_json->>'calibrated_max_hold_s')::float AS calib,
          (brain_json->>'effective_max_hold_s')::float AS effective
   FROM fast_exits
   WHERE decided_at > NOW() - INTERVAL '15 minutes';
   ```
   `effective` should always be ≥ 10 even when `calib` was lower.
4. **Verify negative-edge exclusion is firing:**
   ```sql
   SELECT alert_type, reject_reason, COUNT(*)
   FROM fast_executions
   WHERE decided_at > NOW() - INTERVAL '15 minutes'
     AND reject_reason LIKE 'negative_edge%'
   GROUP BY alert_type, reject_reason;
   ```
   `volume_breakout_long` should appear, others should not (under current data).
5. `docs/STRATEGY/CURRENT_PLAN.md` includes the new "Findings — 2026-05-02" section with all three findings.
6. `docs/STRATEGY/CC_REPORTS/<date>_f6.5-calibration-hygiene.md` written following the format in PROTOCOL.md.

## Open questions for Cowork (surface in your report only if relevant)

1. **Floor of 10s vs. 30s vs. something else.** I picked 10 based on Coinbase placement + broker round-trip + verify polling (200-500ms typical, conservative 2–10× headroom). If you observe live the floor needs to be tighter or looser, propose. Don't tune in this task.
2. **MIN_NEGEDGE_SAMPLES = 30.** Below 30, the t-stat for the exclusion is too noisy. Above 30, we have meaningful confidence intervals. If you have a stronger statistical reason for 50 or 20 propose it; 30 is the conventional threshold.
3. **Should `gate_negative_edge_excluded` log evidence on each rejection?** Currently the report's gate JSON includes the evidence dict. If we want a separate WARNING log line per rejection so operators see it without DB queries, add it.
4. **Do we expose the negative-edge exclusion in the autopilot UI?** The trades-history view shows reject reasons in the Recent Decisions feed; `negative_edge` will appear naturally there. A dedicated "blocked signals" card could be a future task.

## Rollback plan

- All three commits are additive. Reverting any of them restores prior behavior.
- The execution-latency floor only changes a maximum-min calculation. Reverting restores raw calibrated values flowing to exit_manager.
- The negative-edge gate is a pure function added to DEFAULT_GATES. Reverting drops it from the tuple; existing gates and behavior unchanged.
- The CURRENT_PLAN.md update is documentation-only.
- No migrations to roll back.
- No data migrations or backfills.
