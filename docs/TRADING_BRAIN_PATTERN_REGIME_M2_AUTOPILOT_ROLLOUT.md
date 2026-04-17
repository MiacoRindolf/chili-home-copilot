# Phase M.2-autopilot — Rollout & Rollback

The **auto-advance engine** for the three Phase M.2 slices
(**tilt / killswitch / promotion**). It replaces the manual
`.env` flip from `shadow` → `compare` → `authoritative` with a
**policy-driven daily tick** that writes a runtime-mode override
into the database.

This doc is the single operational contract: what it does, what it
will never do, the gates that decide progression, the order lock,
the auto-revert triggers, and the release blocker.

---

## Scope (single source of truth)

The autopilot only controls these three slices:

| Slice | Runtime name | Env fallback |
|---|---|---|
| Tilt | `pattern_regime_tilt` | `BRAIN_PATTERN_REGIME_TILT_MODE` |
| Kill-switch | `pattern_regime_killswitch` | `BRAIN_PATTERN_REGIME_KILLSWITCH_MODE` |
| Promotion | `pattern_regime_promotion` | `BRAIN_PATTERN_REGIME_PROMOTION_MODE` |

Each slice's `_raw_mode()` consults `trading_brain_runtime_modes`
first; if absent, falls back to env. A DB hiccup never makes a
slice worse — it drops back to env.

## Non-negotiables

1. **Never skip stages.** The engine advances `shadow → compare →
   authoritative` only, one step at a time, at most once per slice
   per UTC day.
2. **Order lock.** The cutover order is **tilt → kill-switch →
   promotion**. A slice cannot advance **past shadow** until prior
   slices are `authoritative`. Tilt is never locked.
3. **Approval for authoritative.** Advance to `authoritative`
   **requires** a live row in `trading_governance_approvals`
   (`status='approved'`, `decision='allow'`, `expires_at > NOW()`).
   The engine inserts one automatically when every gate passes.
4. **One-step auto-revert** when any of:
   - `authoritative` but no live approval row
   - `anomaly_refused_authoritative` seen in slice log (last 24h)
   - release blocker unclean (for the slice)
   - diagnostics unhealthy / stale
5. **Never resurrect `off`.** The autopilot does not wake
   `off`-mode slices. A human must move them to `shadow` first.
6. **Rate-limited.** At most **one** advance **or** revert per
   slice per UTC day; subsequent ticks hold.
7. **Additive only.** No existing columns modified, no frozen
   contract changed. `scan_status` frozen contract stays intact.

---

## Control flags (`.env`)

| Flag | Default | Purpose |
|---|---:|---|
| `BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED` | `false` | Master on/off. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_KILL` | `false` | Emergency stop; overrides `_ENABLED`. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_OPS_LOG_ENABLED` | `true` | Emit one-line ops lines. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_CRON_HOUR` | `6` | Daily tick, local tz. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_CRON_MINUTE` | `15` | ^ |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_WEEKLY_CRON_HOUR` | `9` | Weekly summary. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_WEEKLY_CRON_DOW` | `mon` | ^ |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_SHADOW_DAYS` | `5` | BDs in shadow before eligible for compare. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_COMPARE_DAYS` | `10` | BDs in compare before eligible for authoritative. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_MIN_DECISIONS` | `100` | Decision-log rows required over window. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_TILT_MULT_MIN` | `0.85` | Tilt safety envelope lower bound. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_TILT_MULT_MAX` | `1.25` | Tilt safety envelope upper bound. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_PROMO_BLOCK_MAX_RATIO` | `0.10` | Max (block / baseline-allow) in compare. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_KS_MAX_FIRES_PER_DAY` | `1.0` | Max mean would-quarantines/day. |
| `BRAIN_PATTERN_REGIME_AUTOPILOT_APPROVAL_DAYS` | `30` | TTL of auto-inserted approval row. |

---

## Gates (compare → authoritative)

For each slice, **every** gate below must be `ok` before the
engine advances to `authoritative`:

1. `days_in_stage >= COMPARE_DAYS`
2. `total_decisions >= MIN_DECISIONS` (over the window)
3. `diagnostics_healthy == true`
4. `release_blocker_clean == true`
5. `scan_status_frozen_ok == true`
6. Slice-specific **safety envelope**:
   - **tilt**: mean would-apply multiplier in `[TILT_MULT_MIN, TILT_MULT_MAX]`
   - **promotion**: `block_ratio <= PROMO_BLOCK_MAX_RATIO`
   - **kill-switch**: `mean_fires_per_day <= KS_MAX_FIRES_PER_DAY`
7. Order lock: prior slice must be `authoritative`.

When all pass, the engine inserts an approval row **then** writes
the runtime-mode override to `authoritative`. If the approval
insert fails, the advance is downgraded to a hold — authoritative
is never written without a live approval.

Gates for `shadow → compare` are the same minus the safety
envelope.

---

## Artifacts

### DB tables (migration 146)

- **`trading_brain_runtime_modes`** — one row per slice with the
  active mode override. Reads pass through a 30s TTL cache. Absent
  row = "use env default".
- **`trading_pattern_regime_autopilot_log`** — append-only audit
  of every advance / hold / revert / weekly summary, with the full
  gate evaluation (`gates_json`) and evidence snapshot
  (`evidence_json`).

### Structured log prefix

```
[pattern_regime_autopilot_ops] event=<evt> mode=enabled slice=<name> from_mode=<m> to_mode=<m> reason_code=<code> ...
```

Events: `autopilot_advance`, `autopilot_hold`, `autopilot_revert`,
`autopilot_weekly_summary`, `autopilot_skipped`.

### Diagnostics endpoint

`GET /api/trading/brain/m2-autopilot/status`

Frozen shape:
```
{
  "ok": true,
  "m2_autopilot": {
    "enabled": bool,
    "kill": bool,
    "cron_hour": int,
    "cron_minute": int,
    "slices": {
      "tilt":       { "stage", "days_in_stage", "last_advance_date", "approval_live", "env_mode", "override_present" },
      "promotion":  { ... same shape ... },
      "killswitch": { ... same shape ... }
    }
  }
}
```

### Scheduler jobs

- `pattern_regime_autopilot_tick` — daily, gated by `ENABLED`.
- `pattern_regime_autopilot_weekly` — Monday cron, one ops line
  per slice.

---

## Release blocker

`scripts\check_pattern_regime_autopilot_release_blocker.ps1`

Exit 1 on any of:

1. A log line containing:
   ```
   [pattern_regime_autopilot_ops] event=autopilot_advance to_mode=authoritative ...
   ```
   **without** a real `approval_id=<int>` AND `approval_live=true`.
2. A log line containing:
   ```
   event=autopilot_revert reason_code=authoritative_approval_missing
   ```
3. A log line containing:
   ```
   event=autopilot_revert reason_code=anomaly_refused_authoritative
   ```
4. (If `-DiagnosticsJson` provided) any slice with
   `stage="authoritative"` and `approval_live=false`.

---

## Rollout

1. **Pre-flight (already done):**
   - Migration 146 applied in every environment (`schema_version` has `146_m2_autopilot`).
   - 32/32 pure tests pass (`tests/test_pattern_regime_autopilot_pure.py`).
   - 18/18 Docker soak checks pass (`scripts/phase_m2_autopilot_soak.py`).
   - Release-blocker smoke 5/5 pass; live logs clean.
   - `/api/trading/brain/m2-autopilot/status` returns frozen shape with `enabled=false`.

2. **Flip on:**
   ```
   BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED=true
   ```
   Recreate `chili` and `brain-worker`. The scheduler registers
   `pattern_regime_autopilot_tick` (06:15 by default) and
   `pattern_regime_autopilot_weekly` (Mon 09:00).

3. **Verify:**
   - `curl -sk https://localhost:8000/api/trading/brain/m2-autopilot/status` → `enabled=true`.
   - Scheduler log: `Added job Pattern x regime autopilot tick (06:15)`.
   - After the first tick:
     - `trading_pattern_regime_autopilot_log` has 3 rows for today.
     - No release-blocker matches.

4. **Observe.** The engine will:
   - Day 1-4 (shadow): holds each slice with
     `insufficient_days_in_stage` until SHADOW_DAYS (5) BDs pass.
   - Day 5+ (shadow→compare): tilt first; each slice advances
     when its gates pass. Order lock keeps killswitch and
     promotion in shadow until the prior slice is authoritative.
   - Day 15+ (compare→authoritative): safety envelope must hold
     across the compare window; approval row auto-inserted when it
     does.

---

## Rollback

### Emergency stop (instant)

```
BRAIN_PATTERN_REGIME_AUTOPILOT_KILL=true
```
Recreate `chili` + `brain-worker`. The daily tick becomes a no-op.
Existing runtime-mode overrides are **preserved** (slices stay at
whatever stage they reached).

### Revert a single slice manually

```sql
DELETE FROM trading_brain_runtime_modes WHERE slice_name = 'pattern_regime_tilt';
```
Slice falls back to its env mode. Emit a weekly summary on the next
Monday and the audit log records it.

### Full rollback

```
BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED=false
```
Scheduler deregisters the jobs on next recreate. Existing overrides
stay in place — they remain the source of truth for slice modes
until cleared.

### Nuclear

```sql
DELETE FROM trading_brain_runtime_modes;
```
All slices fall back to env values. Any autopilot audit rows stay
(append-only history).

---

## Operator reminders

- The autopilot **does not** touch `.env`. When an advance happens,
  `override_present=true` in diagnostics. That is the signal.
- The autopilot **does not** change any downstream behavior directly —
  it only changes which mode each slice uses. All tilt / promotion /
  killswitch behavior is still implemented in their own services.
- The autopilot **does not** ever enter `off`. If you want a slice
  paused, set its env mode to `off` *and* `DELETE` its override row.
- Weekly summary is a fact-pattern, not a decision. If you see
  `reason_code=weekly_summary` lines on Monday 09:00, that's normal.
