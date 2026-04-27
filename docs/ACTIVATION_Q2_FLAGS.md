# Q2 activation runbook — six flags, three groups

PRs #49–#63 shipped infrastructure for six Q2 capabilities, all wired and tested but **gated OFF by default**. This runbook covers how to flip each, what "healthy" looks like, what the failure modes are, and when to roll back.

The flags split into two groups: **data + learners** (independent of trading) and **K Phase 3 consumers** (the survival-classifier-driven gates that actually affect trading decisions). Phase 3 has a parent kill-switch and three sub-flags so the operator can ramp them one at a time.

## Group 1 — Data + learners (PRs #49–#56)

| # | Flag | What flipping ON enables | Risk |
| --- | --- | --- | --- |
| 1 | `chili_pattern_survival_classifier_enabled` | Daily 03:30 PT job writes features to `pattern_survival_features`. Weekly Sun 04:30 PT job backfills 30d labels + trains LightGBM-equivalent classifier + scores new features. | Read-only. Cannot affect trading. |
| 2 | `chili_perps_lane_enabled` | Hourly job ingests Hyperliquid perps data into `perp_quotes` / `perp_funding` / `perp_oi` / `perp_basis` | Read-only ingestion. No orders placed. (Live trading is gated separately by `chili_perps_lane_live`, which stays OFF.) |
| 3 | `chili_strategy_parameter_learning_enabled` | Background pass updates `strategy_parameter.current_value` from realized outcomes; live readers see adapted thresholds | Touches the entry/exit decision path. Read-side adaptive; learner can nudge thresholds within bounded ranges. |

## Group 2 — K Phase 3 consumers (PRs #59–#63)

The survival classifier produces a per-pattern `survival_probability` once Group 1 Flag 1 has accumulated ~30 days of features and the model has trained at least once. Phase 3 wires that score into three decisions. **All four Phase 3 flags require Group 1 Flag 1 to be ON first** — otherwise there are no predictions to consume.

| # | Flag | What flipping ON enables | Risk |
| --- | --- | --- | --- |
| 4 | `chili_pattern_survival_decisions_enabled` | Parent kill-switch for all three consumers below. Required to be ON for any sub-consumer to take effect. | Bookkeeping only — flipping this without a sub-flag does nothing. |
| 5 | `chili_pattern_survival_sizing_enabled` | Autotrader entry notional gets multiplied by `clamp(SIZING_FLOOR + (1-FLOOR)*p, FLOOR, 1.0)` after HRP allocation. | Lowest blast radius. Bounded by `chili_pattern_survival_sizing_floor` (default 0.25, configurable). |
| 6 | `chili_pattern_survival_demote_enabled` | Daily 04:00 PT pass demotes `live → challenged` (1 day at p < threshold) and `challenged → decayed` (3 consecutive days at p < threshold). | Medium. Lifecycle changes are real. Streak guard smooths single-day blips; operator can manually re-promote. |
| 7 | `chili_pattern_survival_promote_gate_enabled` | Candidates passing CPCV but with first-prediction p < 0.40 land in `pattern_survival_promote_review_queue` instead of auto-promoting. | Highest. Recommend deferring until #5 + #6 have run clean for 30+ days. |

All seven flags default OFF and ship in a no-op state — every read path returns the original hardcoded defaults until flipping the flag, and Phase 3 consumers all log a `no_op` row to `pattern_survival_decision_log` on every call so the operator gets shadow-mode visibility before flipping anything ON.

## Pre-flight (run once before any of the three)

```bash
# 1. Confirm main is at or past commit 6000f02 (PR #51 merge)
git log --oneline -1
# Expected: hash 6000f02 or later

# 2. Migrations 183 + 184 applied
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
rows = db.execute(text(
    \"SELECT version_id FROM schema_version \"
    \"WHERE version_id IN ('183_pattern_survival_meta_classifier', \"
    \"'184_seed_hyperliquid_perp_contracts') ORDER BY version_id\"
)).fetchall()
for r in rows: print(r[0])
"
# Expected: both migration ids present.

# 3. Kill switch + drawdown breaker NOT tripped
docker compose exec -T chili python -c "
from app.services.trading.governance import get_kill_switch_status
print(get_kill_switch_status())
"
# Expected: {'active': False, 'reason': None}

# 4. KPI strip endpoint healthy (smoke test for the broader brain)
curl -k -s https://localhost:8000/api/brain/health/kpi | python -c "import sys, json; d = json.load(sys.stdin); print(f\"ok={d['ok']} as_of={d['as_of']}\")"
# Expected: ok=True with a recent timestamp.
```

If any of the four fail, **stop and resolve before flipping flags.** A pre-flight failure usually means the deploy is incomplete or a hard-rule guard is tripped; flipping flags on top of that obscures what's wrong.

## Flag 1 — `chili_pattern_survival_classifier_enabled`

### What it enables

The scheduled job `pattern_survival_snapshot` runs daily at 03:30 America/Los_Angeles. When the flag is ON, it iterates over patterns in `lifecycle_stage in ('live', 'challenged')` and inserts/updates one row per (pattern, day) into `pattern_survival_features`. Each row captures lifecycle, age, realized 30d performance, CPCV evidence, regime tag, and family-diversity context.

This is **feature collection only**. Phase 2 (training) and Phase 3 (decision wiring) are separate flags (`chili_pattern_survival_decisions_enabled`) and separate work.

### How to flip ON

Add to `.env` and restart the chili service:

```bash
CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED=true
```

```bash
docker compose restart chili
```

### Verification

```bash
# Within ~24h of activation, verify the daily job ran:
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
n = db.execute(text(
    \"SELECT COUNT(*), MAX(snapshot_date) FROM pattern_survival_features\"
)).fetchone()
print(f'rows={n[0]} latest={n[1]}')
"
# Expected after first run: rows = number of (live + challenged) patterns,
# latest = today's date in PT.
```

### Expected log lines

Success (info-level):
```
[pattern-survival] daily snapshot: {'snapshot_date': '2026-04-27', 'patterns_snapshotted': N, 'patterns_failed': 0}
```

Flag still OFF (job runs but skips):
```
[pattern-survival] daily snapshot: {'skipped': 'flag_off'}
```

Per-pattern persistence failure (warning-level — does not abort the pass):
```
[pattern_survival] persist features for pattern <id> failed: <error>
```

### Rollback signal

Flip OFF if you see:

- `patterns_failed > 0` for two consecutive days with the same error (likely schema or DB issue, not transient)
- The job consistently runs >5 minutes (currently <2s for ~20 patterns; if it explodes, something joined wrong)
- The `pattern_survival_features` table is growing faster than 1 row per (pattern, day) — suggests the unique-key upsert is broken

### Time horizon

Phase 2 (LightGBM training) needs ~30 days of features before label backfill is meaningful. Set a calendar reminder for May 27 to check K Phase 2 readiness.

## Flag 2 — `chili_perps_lane_enabled`

### What it enables

The scheduled job `perps_ingestion` runs hourly. When the flag is ON, it iterates over `perp_contracts` grouped by venue, and:

- For Binance contracts (9 seeded): fetch attempted but currently fails with HTTP 451 (geo block — see `project_binance_geoblock`). Returns 0 inserts. Code is correct; data won't land until a non-US deployment or VPN exists.
- For Hyperliquid contracts (15 seeded — BTC/ETH/SOL/BNB/XRP/AVAX/LINK/DOGE/MATIC/ARB/OP/LTC/ATOM/APT/INJ): one bulk POST to `api.hyperliquid.xyz/info` returns mark/oracle/funding/OI for all coins; per-symbol funding-history calls populate `perp_funding`.

Live order placement is **separately** gated by `chili_perps_lane_live` (default OFF). This flag only controls **data ingestion + paper-only strategy proposals**. Trading is a deliberate second flip.

### How to flip ON

```bash
CHILI_PERPS_LANE_ENABLED=true
```

```bash
docker compose restart chili
```

### Verification

```bash
# Within ~70 minutes of activation:
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
for tbl in ['perp_quotes', 'perp_oi', 'perp_funding', 'perp_basis']:
    rows = db.execute(text(
        f'SELECT venue, COUNT(*) FROM {tbl} GROUP BY venue ORDER BY venue'
    )).fetchall()
    print(f'{tbl}: ' + ', '.join(f'{r[0]}={r[1]}' for r in rows))
"
# Expected after first run:
#   perp_quotes:   hyperliquid=15
#   perp_oi:       hyperliquid=15
#   perp_funding:  hyperliquid=45  (15 contracts x 3 most-recent periods)
#   perp_basis:    hyperliquid=14  (one symbol's spot lookup may fail)
# Binance counts stay at 0 until the geo block is resolved.
```

### Expected log lines

Success (info-level, hourly):
```
[perps.ingest] pass complete: {'contracts': 24, 'venues': {'binance': {...zeros...}, 'hyperliquid': {'contracts': 15, 'quotes_inserted': 15, ...}}}
[perps] ingestion: {same dict}
```

Flag still OFF (wrapper short-circuits, no log):
```
(silence — the wrapper returns before any log line)
```

Network failure (debug-level, expected for Binance):
```
[binance] premiumIndex fetch failed: 451 Client Error: ...
```

### Rollback signal

Flip OFF if you see:

- `perps.ingest pass complete` shows `quotes_inserted: 0` for Hyperliquid for two consecutive hours (Hyperliquid is rate-limited, but not by enough to zero out a hourly cadence — this would mean the API changed shape or the auth/CORS situation changed)
- A 429 or sustained 5xx from Hyperliquid (would show in container logs at debug level — bump `app.services.trading.perps.venue_hyperliquid` to INFO temporarily to confirm)
- `perp_funding` rows growing past ~360/day across all symbols (15 contracts × 24 hourly periods = 360/day expected; double that signals a broken ON CONFLICT)

### Time horizon

Funding-carry / oi_divergence strategies need at least the 30d trailing window in `perp_basis` to compute basis_z_score. Set a calendar reminder for May 27 to check whether basis features are populated and seed strategies can be evaluated. Until then, the lane is "data accumulating, no decisions wired."

## Flag 3 — `chili_strategy_parameter_learning_enabled`

### What it enables

The scheduled job `strategy_parameter_learning` runs every 6 hours. It computes Bayesian posterior updates over recent outcomes recorded against parameter values and either auto-applies low-stakes updates or writes proposals for operator review.

When the flag is OFF (default), the **read path still works** — every `get_parameter()` call returns the registered default. Code that consumes adaptive thresholds (`auto_trader.confidence_floor`, `setup_vitals.rsi_overbought`, `momentum_continuation.rvol_min`, `exit_engine.trailing_atr_mult`) sees a coherent value either way.

When the flag is ON, the learner can adjust `current_value` for any registered parameter. Each parameter has hard-coded `min_value` / `max_value` bounds in its `register_parameter()` call, so the learner cannot push a threshold into nonsense territory even if outcomes mislead it.

### How to flip ON

```bash
CHILI_STRATEGY_PARAMETER_LEARNING_ENABLED=true
```

```bash
docker compose restart chili
```

### Verification

```bash
# Confirm the four registered parameters are still at initial values
# immediately after flipping (the learner needs outcome data first):
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
rows = db.execute(text(
    'SELECT strategy_family, parameter_key, current_value, initial_value '
    'FROM strategy_parameter ORDER BY strategy_family, parameter_key'
)).fetchall()
for r in rows:
    delta = float(r[2]) - float(r[3])
    print(f'{r[0]:25s} {r[1]:22s} cur={r[2]} init={r[3]} delta={delta:+.4f}')
"
# Expected immediately after flipping: delta=0.0000 for all rows.
```

After 6 hours, the learning pass runs once. Re-run the verification command — `delta` may be non-zero for parameters that have collected enough outcome samples.

### Expected log lines

Success (info-level, every 6h):
```
[strategy-param] learning pass: {'parameters_evaluated': N, 'proposals_written': N, 'auto_applied': N, ...}
```

Per-parameter sample count (debug-level — bump module to INFO if needed):
```
[strategy_param] <family>/<key>: N samples, posterior_mean=X.XX, proposing Y.YY
```

Flag still OFF (job not registered):
```
(silence — `add_job` was never called)
```

### Rollback signal

Flip OFF if you see:

- `current_value` for any parameter pinned at its `min_value` or `max_value` for two consecutive learning passes (the learner is being yanked to a bound by extreme outcomes; investigate before letting it stay there)
- `auto_applied > 0` AND a same-day uptick in autotrader rejected entries with `confidence_below_floor` (the floor moved against the live regime — manual revert via `record_outcome` rollback or direct `current_value` reset is the fix)
- Any parameter `current_value` drifting more than 20% from `initial_value` within a week (legitimate adaptation should be slower; this signals overfitting to a small sample)

### Hard rollback

If the learner has moved values you don't trust, reset everything to initial:

```bash
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
db.execute(text(
    'UPDATE strategy_parameter SET current_value = initial_value'
))
db.commit()
print('reset all strategy_parameter rows to initial_value')
"
```

This is safe — it doesn't touch the outcome history, just the active value.

### Time horizon

The learner won't propose updates for any parameter until it has at least 30 outcome samples (`_MIN_SAMPLES_FOR_PROPOSAL`). For low-traffic parameters (`exit_engine.trailing_atr_mult` only fires on stop-out events) this could take weeks. For autotrader confidence_floor it will accumulate faster (every entry decision logs an outcome). Expect first proposals in 1–2 weeks of normal trading.

## Cross-flag interactions

- **Kill switch** — independent of all three. A tripped kill switch does NOT block any of these three jobs (they're read-side ingestion / learning, not trade placement). Conversely, none of these flags affect the kill switch.
- **Drawdown breaker** — same: independent.
- **Prediction mirror authority (Hard Rule 5)** — independent. The K classifier writes to `pattern_survival_predictions`, which is its own table and its own authority lineage.
- **`chili_perps_lane_live`** — gates live order placement on perps. **Stays OFF**. Flipping `chili_perps_lane_enabled=true` alone is paper-only ingestion + proposal generation; no orders go to a venue.
- **`chili_pattern_survival_decisions_enabled`** — parent for Phase 3. Required ON for any of Flags #5, #6, or #7 to take effect. Flipping it alone does nothing.
- **`chili_pattern_survival_sizing_floor`** — float (default 0.25). Tunes how aggressively low-survival patterns are sized down. Lower floor = more risk-off. Tune from realized-PnL distribution after Flag #5 has run for a week.
- **`chili_pattern_survival_demote_threshold`** / **`chili_pattern_survival_demote_streak_required`** / **`chili_pattern_survival_promote_gate_threshold`** — float thresholds for the demote / promote gates. Defaults 0.30 / 3 / 0.40.

## Group 1 sequencing (data + learners)

1. **Day 0** — Pre-flight checks pass.
2. **Day 0** — Flip `chili_pattern_survival_classifier_enabled=true`. Lowest risk; read-only feature collection on already-known patterns. Restart chili.
3. **Day 1** — Verify `pattern_survival_features` got rows after the 03:30 PT job. If healthy, proceed.
4. **Day 1** — Flip `chili_perps_lane_enabled=true`. Restart chili.
5. **Day 1 + 70min** — Verify all four `perp_*` tables have hyperliquid rows. If healthy, proceed.
6. **Day 2** — Flip `chili_strategy_parameter_learning_enabled=true`. Restart chili.
7. **Day 2 + 6h** — First learning pass runs. Check learning-pass log line and `strategy_parameter.current_value` deltas.
8. **Day 7** — Audit week. Pull each verification command. Roll back any flag whose health signal is bad.

If anything fails at a step, **flip it back OFF, restart, and don't proceed to the next step**.

## Group 2 sequencing (K Phase 3 consumers)

**Prerequisite:** Group 1 Flag #1 must have run for ≥30 days so `pattern_survival_features` has labels resolved AND the weekly training pass has produced at least one model artifact under `/app/models/pattern_survival/`. Until both are true, Phase 3 consumers always log `no_op` with `skip_reason='no_prediction'` even when their flags are ON.

The activation order is intentionally slow — model lift verification → sizing → demote → promote_gate, with multi-week gaps between each:

### Week 0–4 (shadow mode evaluation)

Don't flip anything in Group 2 yet. While the classifier produces predictions:

1. After ~30 days, query labeled features:
   ```bash
   docker compose exec -T chili python -c "
   from sqlalchemy import text
   from app.db import SessionLocal
   db = SessionLocal()
   r = db.execute(text(\"SELECT COUNT(*), AVG(actual_survived::int) FROM pattern_survival_predictions WHERE actual_survived IS NOT NULL\")).fetchone()
   print(f'labeled={r[0]} base_rate={r[1]}')
   "
   ```
2. Compare `predicted_label` against `actual_survived`. Lift over base rate > 1.5× is the green light. Below that, hold and let the model see more data.

### Week 5 — Flip Flag #5 (sizing)

```bash
CHILI_PATTERN_SURVIVAL_DECISIONS_ENABLED=true
CHILI_PATTERN_SURVIVAL_SIZING_ENABLED=true
```

```bash
docker compose restart chili
```

**Verify** (30 minutes after first autotrader entry):

```bash
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
r = db.execute(text(
    \"SELECT consumer, decision, COUNT(*), AVG(predicted_survival) \"
    \"FROM pattern_survival_decision_log \"
    \"WHERE consumer='sizing' AND decided_at >= NOW() - INTERVAL '1 hour' \"
    \"GROUP BY 1, 2\"
)).fetchall()
for x in r: print(x)
"
```

Expected: `('sizing', 'apply', N, 0.4..0.8)` rows with realistic predicted_survival distribution.

**Expected log lines** (info-level):

```
[ps_decisions] consumer=sizing pattern=X p=0.42 mult=0.625 in=$2000 out=$1250
```

**Rollback signal:** realized PnL drops > 10% over the week AND you can attribute it to the multiplier (vs. market regime, autotrader gating, etc) by joining `pattern_survival_decision_log.details.multiplier` against `trading_trades.pnl`.

### Week 7 — Flip Flag #6 (demote)

After 2 weeks of clean sizing data:

```bash
CHILI_PATTERN_SURVIVAL_DEMOTE_ENABLED=true
```

```bash
docker compose restart chili
```

**Verify** (24 hours after the next 04:00 PT demote pass):

```bash
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
r = db.execute(text(
    \"SELECT consumer, decision, COUNT(*) \"
    \"FROM pattern_survival_decision_log \"
    \"WHERE consumer='demote' AND decided_at >= NOW() - INTERVAL '24 hours' \"
    \"GROUP BY 1, 2\"
)).fetchall()
for x in r: print(x)
"
```

**Expected log lines** (warning-level when a demote actually fires):

```
[ps_demote_pass] demoted pattern X: live -> challenged (p=0.28, streak=1)
```

**Rollback signal:** any pattern demoted that the operator believes was healthy. Manual revert:

```bash
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
db.execute(text(\"UPDATE scan_patterns SET lifecycle_stage='live', survival_at_risk_streak_days=0 WHERE id=:p\"), {'p': PATTERN_ID})
db.commit()
"
```

### Week 11+ — Flip Flag #7 (promote_gate)

Defer until both #5 and #6 have run clean for at least 30 days. The promote_gate adds a new manual-review surface (the queue table) which becomes operator workflow.

```bash
CHILI_PATTERN_SURVIVAL_PROMOTE_GATE_ENABLED=true
```

```bash
docker compose restart chili
```

The next time a candidate passes CPCV, if predicted survival < 0.40 the candidate stays at `lifecycle='candidate'` and a row lands in `pattern_survival_promote_review_queue`. Operator clears via:

```sql
UPDATE pattern_survival_promote_review_queue
SET review_decision = 'approve',
    review_decided_at = NOW(),
    decided_by = 'operator'
WHERE id = QUEUE_ID;
```

A subsequent CPCV pass will then promote the pattern normally.

**Verify** (next time a candidate is CPCV-eligible):

```bash
docker compose exec -T chili python -c "
from sqlalchemy import text
from app.db import SessionLocal
db = SessionLocal()
r = db.execute(text(\"SELECT scan_pattern_id, predicted_p, queued_at FROM pattern_survival_promote_review_queue WHERE review_decision IS NULL ORDER BY queued_at DESC LIMIT 10\")).fetchall()
for x in r: print(x)
"
```

**Hard rollback** for all three Phase 3 consumers:

```bash
CHILI_PATTERN_SURVIVAL_DECISIONS_ENABLED=false
```

The parent flag is the kill-switch for the whole Phase 3 surface.

## Where the wiring lives

| What | File | Function |
| --- | --- | --- |
| K snapshot scheduler hook | `app/services/trading_scheduler.py` | `_run_pattern_survival_snapshot_job` (CronTrigger 03:30 PT) |
| K snapshot job | `app/services/trading/pattern_survival/features.py` | `run_pattern_survival_snapshot_job` |
| Perps scheduler hook | `app/services/trading_scheduler.py` | `_run_perps_ingestion_job` (IntervalTrigger 1h) |
| Perps ingestion | `app/services/trading/perps/ingestion.py` | `run_perps_ingestion_pass` |
| Hyperliquid adapter | `app/services/trading/perps/venue_hyperliquid.py` | `fetch_premium_index` etc. |
| StrategyParameter learning hook | `app/services/trading_scheduler.py` | `_run_strategy_param_learning_job` (IntervalTrigger 6h) |
| StrategyParameter learner | `app/services/trading/strategy_parameter.py` | `run_parameter_learning_pass` |
| K Phase 2 training pass | `app/services/trading/pattern_survival/training.py` | `run_pattern_survival_training_pass` (CronTrigger Sun 04:30 PT) |
| K Phase 3 decisions module | `app/services/trading/pattern_survival/decisions.py` | `compute_decision`, `demote_policy`, `run_pattern_survival_demote_pass` |
| K Phase 3 sizing wire-in | `app/services/trading/auto_trader.py` | inline call to `compute_decision(consumer='sizing')` after HRP |
| K Phase 3 demote scheduler | `app/services/trading_scheduler.py` | `_run_pattern_survival_demote_job` (CronTrigger 04:00 PT) |
| K Phase 3 promote gate | `app/services/trading/learning.py` | `_persist_promotion_outcome` calls `compute_decision(consumer='promote_gate')` before `update_pattern` |
| KPI strip (always-on observability) | `app/routers/brain.py` | `GET /api/brain/health/kpi` |

The KPI strip endpoint is the fastest morning check across all flags. The relevant fields:

- `learning.live_but_inactive` / `learning.lifecycle_active_drift_warning` — Q's drift detector
- `learning.survival_at_risk_count` — Phase 3 gate's would-act-on count (independent of any flag)
- `learning.in_promote_review_queue` — Phase 3 promote_gate's pending count
- `pnl_30d_usd` / `concentration_warning` — autotrader effects
- `regime` — current HMM tag (orthogonal but useful context for shadow eval)
- `safety.flags` — full dump of every Q1/Q2 flag state for quick inspection
