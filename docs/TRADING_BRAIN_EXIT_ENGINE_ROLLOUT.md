# Trading Brain — ExitEngine Unification Rollout (Phase B)

> **STATUS — 2026-05-15: cutover path RETIRED. Shadow logging stays
> active as a sanity check; the `shadow → authoritative` ladder is no
> longer the planned migration path.**
>
> **Why retired**
>
> 1. The cutover-gate verdict query
>    (`scripts/dispatch-exit-parity-cutover-gate.ps1`) is structurally
>    incapable of producing signal on backtest. The shadow harness
>    feeds both engines a synthetic bar with
>    `open=high=low=close=price`; both emit `exit_price=price` when
>    they close, so `exit_price_drift_bps = 0` for every `both_close`
>    row by construction. STDDEV=0 → t-stat=NULL → PASS regardless of
>    real engine behaviour.
> 2. The gate's `MIN_SAMPLE_N=1000` floor can't be reached on the live
>    cohort at current paper-soak rates (516 `both_hold` and 0
>    `both_close` over a 24h window on 2026-05-10).
> 3. The only structural disagreement the gate ever surfaced (39
>    `legacy_only_close` rows in the 2026-05-09 window, all
>    `priority_winner='exit_trail'`) was the ATR=0 trailing-stop bug
>    in legacy. It was fixed directly in
>    `app/services/backtest_service.py` on 2026-05-15
>    (`f-exit-parity-trail-atr-zero-divergence`). No canonical
>    migration was required.
> 4. The bigger evidence-quality questions are answered upstream by
>    the `f-evidence-fidelity-architecture` arc that landed
>    2026-05-14 through 2026-05-15 (Phases A–E + activations):
>    canonical-outcome-layer, execution-truth-wiring, triple-barrier
>    activation, NetEdge live wiring, multiple-testing discipline. The
>    proper exit-quality test is now "what P/L did this exit produce
>    against the triple-barrier label?", not "did canonical and
>    legacy emit the same string?". See the corresponding CC reports
>    under `docs/STRATEGY/CC_REPORTS/2026-05-14_*.md` and
>    `docs/STRATEGY/CC_REPORTS/2026-05-15_evidence-fidelity-followup-activations.md`.
>
> **What stays in place**
>
> - `BRAIN_EXIT_ENGINE_MODE=shadow` continues to log
>   `trading_exit_parity_log` rows on every backtest and live close.
>   Useful as a sanity check that engine refactors didn't change live
>   behaviour. Stripping the surface entirely is a separate cleanup.
> - The `compute_parity_v2_fields` helper and the live/backtest hooks
>   keep working. The migration columns (mig 230) stay.
> - Section 5's release blocker (no `mode=authoritative` ever) stays
>   in force — the cutover is retired, not delayed.
>
> **What is dropped from the plan of record**
>
> - The `shadow → compare → authoritative` ladder below.
> - The cutover-gate verdict as a gate. The scheduled task that
>   reports it is queued for retirement / repointing at a
>   triple-barrier and venue-truth digest instead.
> - The queued brief `f-exit-parity-trail-atr-zero-divergence`
>   (consumed by the 2026-05-15 fix; the brief's Option A shipped).
>
> The §2 onwards content is preserved verbatim below for archival
> reference. Read it as "what we *had* planned", not "what we plan
> to do next".

---

This document describes the rollout of the canonical `ExitEvaluator`
(`app/services/trading/exit_evaluator.py`) that unifies two divergent exit
paths — `DynamicPatternStrategy` inside `app/services/backtest_service.py`
and `compute_live_exit_levels` inside
`app/services/trading/live_exit_engine.py` — into a single source of truth.

**Phase B ships in shadow mode only.** No live or paper trade decision
changes in this phase. The evaluator's output is logged alongside the
legacy decision so we can measure drift before cutover.

Mirrors the contract laid down by:
- Prediction mirror rollout (`docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md`)
- NetEdgeRanker rollout (`docs/TRADING_BRAIN_NET_EDGE_RANKER_ROLLOUT.md`)

---

## 1. Why

`NetEdgeRanker` (Phase E) computes `expected_payoff` and `viability_score`
from historical backtest results. If backtests and live exits disagree
silently, `expected_payoff` is a lie in live and the whole net-edge
pipeline produces biased scores. Phase B removes the divergence so that
Phase D (triple-barrier labels) and Phase G (broker brackets) can depend
on a single, testable exit semantics.

Two observed divergences surfaced by the parity harness:

1. **Live legacy never closes on trailing stop** — it computes the trail
   value for reporting but its `action` only flips on `stop`, `target`,
   `time_decay`, or `bos`. Live flavor of `ExitConfig` therefore passes
   `trail_atr_mult=None`.
2. **Backtest legacy trail is non-monotonic** — it recomputes
   `highest - k*ATR` each bar without a monotonicity guard, so when ATR
   grows the trail loosens. Backtest flavor of `ExitConfig` passes
   `trail_monotonic=False` for bit-for-bit parity.
3. **Live legacy BOS overwrites an earlier stop/target** — the BOS check
   is the last writer and has no `action == "hold"` guard. Canonical
   priority is `stop > target > BOS > time_decay > trail > partial`.
   This is a recorded disagreement, not a parity bug. Cutover to
   canonical order is a separate future decision.

---

## 2. Rollout ladder

```
off -> shadow -> compare -> authoritative
```

| Mode | Meaning |
|---|---|
| `off` | Evaluator is not called. Legacy paths run alone. No parity rows written. |
| `shadow` | Evaluator is called and logged to `trading_exit_parity_log`; legacy path still decides the trade. |
| `compare` | Same as `shadow` for live; backtest may additionally enforce that disagreement rate stays below a documented threshold. Still no live behavior change. |
| `authoritative` | **Not in Phase B.** Canonical decision drives the close. Requires a separate, explicit cutover plan and its own verification gates. |

Any `[exit_engine_ops]` log line with `mode=authoritative` while the
deploy is meant to be anything else is a **release blocker** (see §5).

---

## 3. Forward / rollback

### Forward (turn shadow on)

```
BRAIN_EXIT_ENGINE_MODE=shadow
BRAIN_EXIT_ENGINE_OPS_LOG_ENABLED=true
BRAIN_EXIT_ENGINE_PARITY_SAMPLE_PCT=1.0
```

Apply to `.env`, then:

```powershell
docker compose up -d --force-recreate chili
# Confirm migration 128 applied
docker compose exec postgres psql -U chili -d chili_prod -c "SELECT version_id FROM schema_version ORDER BY id DESC LIMIT 5;"
# Sanity-check the parity table exists
docker compose exec postgres psql -U chili -d chili_prod -c "\\d trading_exit_parity_log"
```

### Rollback

Set mode back to `off`, recreate the service. The parity table stays
(shadow-safe, unused); no data loss.

```
BRAIN_EXIT_ENGINE_MODE=off
```

```powershell
docker compose up -d --force-recreate chili
```

---

## 4. Observability

### Ops log

Single bounded line per parity decision:

```
[exit_engine_ops] mode=shadow source=live position_id=1234 ticker=AAPL \
    legacy_action=exit_stop canonical_action=exit_stop agree=true \
    config_hash=ab12cd34ef567890 sample_pct=1.000
```

Fields (order and enums are frozen):

- `mode`: `off` | `shadow` | `compare` | `authoritative`
- `source`: `backtest` | `live`
- `position_id`: integer (PaperTrade/Trade id for live, `none` for backtest)
- `ticker`: trade ticker (truncated to 24 chars)
- `legacy_action` / `canonical_action`: one of `hold`, `exit_stop`,
  `exit_target`, `exit_trail`, `exit_bos`, `exit_time_decay`, `partial`
- `agree`: `true` | `false`
- `config_hash`: 16-char hash of the `ExitConfig` in use
- `sample_pct`: sampling rate (for future down-sampling; currently 1.0)

### Diagnostics endpoint

```
GET /api/trading/brain/exit-engine/diagnostics?lookback_hours=24
```

Returns:

```json
{
  "ok": true,
  "exit_engine": {
    "ok": true,
    "mode": "shadow",
    "lookback_hours": 24,
    "total": 123,
    "agree": 118,
    "disagree": 5,
    "disagreement_rate": 0.0406,
    "per_source": {
      "live": {"total": 40, "agree": 40, "disagree": 0, "disagreement_rate": 0.0},
      "backtest": {"total": 83, "agree": 78, "disagree": 5, "disagreement_rate": 0.0602}
    },
    "top_mismatches": [
      {"legacy_action": "exit_bos", "canonical_action": "exit_stop", "count": 3}
    ],
    "configs": [
      {"config_hash": "ab12cd34ef567890", "count": 120}
    ]
  }
}
```

Must remain read-only. Safe to hit repeatedly.

### Tables

- `trading_exit_parity_log` — one row per parity decision. See migration
  128 for schema and indexes.

---

## 5. Release blocker

**Do not ship** if any log line matches BOTH:

- `[exit_engine_ops]`
- `mode=authoritative`

while the environment is not supposed to be authoritative.

```powershell
docker compose logs chili --since 30m 2>&1 |
  .\scripts\check_exit_engine_release_blocker.ps1
```

Exit code 0 = pass. Exit code 1 = blocker lines found and printed to
stderr.

---

## 6. Frozen scope

Phase B **does not**:

- Introduce new exit rules.
- Change entry logic.
- Change `ScanPattern.exit_config` schema or persisted exit metadata.
- Delete the legacy exit blocks in `backtest_service.py` or
  `live_exit_engine.py`. Shadow coexists with legacy until cutover.
- Flip `authoritative` mode anywhere. Cutover is a separate phase.

See `.cursor/plans/phase_b_exit_engine_unification.plan.md` for the full
frozen contract, file-touch order, verification gates, and forbidden
changes.
