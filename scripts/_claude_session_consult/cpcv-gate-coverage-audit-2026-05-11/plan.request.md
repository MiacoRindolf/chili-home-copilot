# Plan: f-cpcv-gate-coverage-audit (Phase 0)

Session: `cpcv-gate-coverage-audit-2026-05-11`
Brief: `docs/STRATEGY/QUEUED/f-cpcv-gate-coverage-audit.md`
Parent: `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
Plan-gate consultation request — awaiting APPROVED / REVISE / ABORT.

---

## 1. Scope confirmation (read-only, no code under app/)

I have read CLAUDE.md, PROTOCOL.md, COWORK_ADVISOR_BRIEF.md, the parent
architecture brief, this task's brief, NEXT_TASK.md, the cpcv_gate
handler at `app/services/trading/brain_work/handlers/cpcv_gate.py`,
`app/services/trading/promotion_gate.py`, and confirmed the
`backtest_completed` emitter (`brain_work/emitters.py:209`) writes
payload `{"scan_pattern_id": int, ...}` keyed by the int pattern id,
which the handler reads via `payload.get("scan_pattern_id")`.

**Hard constraints I will respect end-to-end:**

- READ-ONLY. No DB writes. No edits under `app/`. No restarts. No new
  migrations / tables / columns. No env edits.
- `psql -c` SELECT only. `docker exec python -c` blocks MUST wrap the
  whole CPCV call in `try: ... finally: sess.rollback(); sess.close()`.
- No edits to `cpcv_gate.py` or any handler — read only.
- Memo D4 quotes exact percentages per classification, not hand-waves.
- Cap candidate set at 50 patterns to keep the audit cheap.
- All new files live under `scripts/` or `docs/`. Nothing under `app/`.

## 2. Files I will create

```
scripts/audit-cpcv-gate-coverage.ps1          (D1, ~180 lines)
scripts/audit-cpcv-gate-force-eval.ps1        (D2, ~120 lines)
scripts/audit-cpcv-gate-coverage-out.txt      (D3, generated run output)
docs/AUDITS/2026-05-11_cpcv_gate_coverage.md  (D4, one-page memo)
docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-coverage-audit.md  (D5)
```

No edits to anything that already exists, except `docs/STRATEGY/NEXT_TASK.md`
(flip STATUS PENDING → DONE per protocol).

## 3. D1 — `scripts/audit-cpcv-gate-coverage.ps1`

Style mirrors `scripts/dispatch-drought-probe-2.ps1` (Out-File ascii
temp .sql, `docker cp` to postgres container, `psql -f`, append result
to `-out.txt`; PowerShell parser-clean).

### 3a. Candidate selection query

```sql
WITH ptr_counts AS (
    SELECT scan_pattern_id, COUNT(*) AS ptr_rows
      FROM trading_pattern_trades
     WHERE outcome_return_pct IS NOT NULL
     GROUP BY scan_pattern_id
)
SELECT sp.id, sp.name, sp.lifecycle_stage, sp.promotion_status,
       sp.last_backtest_at, sp.oos_evaluated_at,
       pc.ptr_rows
  FROM scan_patterns sp
  JOIN ptr_counts pc ON pc.scan_pattern_id = sp.id
 WHERE sp.active = TRUE
   AND pc.ptr_rows >= 30
   AND sp.cpcv_n_paths IS NULL
   AND COALESCE(sp.lifecycle_stage, '') NOT IN ('promoted', 'retired')
 ORDER BY pc.ptr_rows DESC
 LIMIT 50;
```

Why these filters: they mirror the handler's own preconditions
(handler exits early for `lifecycle_stage IN ('promoted','retired')`
at line 76, and bails when `len(ptr_rows) < 30` at line 94). Cap 50
keeps the per-pattern docker-log grep below a 60s wall-clock budget.

### 3b. Per-pattern probe (loop body, one pattern at a time)

For each candidate id:

```sql
-- A. Most recent backtest_completed event referencing this pattern.
SELECT id AS event_id, status, attempts, last_error,
       created_at, processed_at, payload
  FROM brain_work_events
 WHERE event_type = 'backtest_completed'
   AND (payload->>'scan_pattern_id')::int = :pid
 ORDER BY created_at DESC
 LIMIT 1;

-- B. Counts: total events for this pattern, by status (sanity).
SELECT status, COUNT(*) FROM brain_work_events
 WHERE event_type = 'backtest_completed'
   AND (payload->>'scan_pattern_id')::int = :pid
 GROUP BY status;
```

Then a `docker logs --since 24h chili-home-copilot-brain-worker-1`
grep — scoped to a `Select-String` for `[brain_work:cpcv_gate]` AND
either `ev_id=<event_id>` OR `pattern_id=<pid>`. Output limited to
last 5 matching lines per pattern.

### 3c. Classification (the funnel-break verdict)

Per pattern, assign exactly one bucket:

| Classification                       | Detection rule                                                                                      |
|--------------------------------------|------------------------------------------------------------------------------------------------------|
| `event_missing`                      | No `backtest_completed` row referencing this pattern_id, ever.                                       |
| `event_pending_or_retry`             | Event row exists with `status IN ('pending','processing','retry_wait')`. Dispatcher hasn't run it.   |
| `event_dead`                         | Event row exists with `status = 'dead'` (max retries exhausted). `last_error` captured.              |
| `event_done_but_no_handler_log`      | Event row `status = 'done'` but no `[brain_work:cpcv_gate]` log line for `ev_id`/`pattern_id` in 24h.|
| `handler_logged_but_no_persist`      | Handler log line exists but `cpcv_n_paths` still NULL on `scan_patterns` (the symptom we audit for). |
| `unknown`                            | Anything else (e.g. event status not in any of the above).                                           |

Notes:
- `event_done_but_no_handler_log` is plausible because the 24h grep
  window can miss older done events; we'll surface that caveat in the
  memo rather than over-claiming a handler bug.
- `handler_logged_but_no_persist` is the most damaging — it means the
  handler ran but the persist path silently lost the verdict.

### 3d. Aggregation + output

At the bottom of `audit-cpcv-gate-coverage-out.txt`:

```
## SUMMARY (50 of 275 patterns audited)

| classification                  | count | %     |
|---------------------------------|-------|-------|
| event_missing                   |   N1  |  P1%  |
| event_pending_or_retry          |   N2  |  P2%  |
| event_dead                      |   N3  |  P3%  |
| event_done_but_no_handler_log   |   N4  |  P4%  |
| handler_logged_but_no_persist   |   N5  |  P5%  |
| unknown                         |   N6  |  P6%  |
| TOTAL                           |   50  | 100%  |

## TOP 10 EXAMPLES per non-zero bucket
... (id, name, ptr_rows, last_backtest_at, event_id, event_status)

## ALL 50 RAW ROWS
... (one row per pattern with all probe fields)
```

The audit script does not commit the bucket logic to DB anywhere —
classification is computed in PowerShell from the per-pattern psql
results and emitted only to the text file.

### 3e. Approach for running the SQL block 50× cheaply

To avoid 50 distinct `docker exec psql` invocations (slow), I will
generate ONE SQL file that does the whole sweep:

```sql
\copy (
  WITH candidates AS ( <selection from 3a> )
  SELECT c.id AS pid, c.name, c.ptr_rows, c.last_backtest_at,
         bwe.id AS event_id, bwe.status AS event_status,
         bwe.created_at AS event_created_at,
         bwe.processed_at AS event_processed_at,
         bwe.last_error AS event_last_error,
         bwe.attempts AS event_attempts
    FROM candidates c
    LEFT JOIN LATERAL (
        SELECT id, status, created_at, processed_at, last_error, attempts
          FROM brain_work_events
         WHERE event_type = 'backtest_completed'
           AND (payload->>'scan_pattern_id')::int = c.id
         ORDER BY created_at DESC
         LIMIT 1
    ) bwe ON TRUE
    ORDER BY c.ptr_rows DESC
) TO '/tmp/audit_cpcv.csv' WITH (FORMAT csv, HEADER true);
```

Then `docker cp` the CSV back to the host. PowerShell reads it,
loops once per row to check the handler log (single
`docker logs --since 24h` call grepped 50× in memory), and writes the
classified report.

Net: one `psql -f` call + one `docker logs --since 24h` call =
sub-minute total wall clock for the whole audit. Well under the brief's
"<60s" guidance.

## 4. D2 — `scripts/audit-cpcv-gate-force-eval.ps1`

Read-only dry-run of `check_promotion_ready` against ONE pattern.

### 4a. Inputs / defaults

- `-PatternId <int>` — defaults to **731** (largest trade count among
  the candidates from probe E7 of the parent brief).
- `-MinTrades <int>` — defaults to 30 (matches handler line 40).
- `-NHypotheses <int>` — defaults to 1 (matches handler call site line 120).

### 4b. Body — runs in `conda run -n chili-env docker exec` block

```python
# Invoked as: docker exec chili-home-copilot-brain-worker-1 \
#   python -c "<heredoc>"
import json
from app.db import SessionLocal
from app.models.trading import PatternTradeRow as PTR, ScanPattern
from app.services.trading.mining_validation import check_promotion_ready
from app.services.trading.promotion_gate import (
    normalize_ptr_row_features, cpcv_eval_to_scan_pattern_fields,
)

PID = <PatternId>
sess = SessionLocal()
try:
    pat = sess.get(ScanPattern, PID)
    rows = (
        sess.query(PTR)
        .filter(PTR.scan_pattern_id == PID,
                PTR.outcome_return_pct.isnot(None))
        .order_by(PTR.as_of_ts.asc())
        .all()
    )
    ensemble = []
    for r in rows:
        fj = r.features_json if isinstance(r.features_json, dict) else {}
        d = normalize_ptr_row_features(
            outcome_return_pct=r.outcome_return_pct,
            as_of_ts=r.as_of_ts,
            ticker=r.ticker,
            timeframe=r.timeframe,
            features_json=fj,
        )
        d["ret_5d"] = float(r.outcome_return_pct or 0.0)
        ensemble.append(d)
    ok, detail = check_promotion_ready(
        ensemble,
        min_trades=<MinTrades>,
        n_hypotheses_tested=<NHypotheses>,
        scan_pattern=pat,
    )
    cpcv_payload = detail.get("cpcv_promotion_gate") or {}
    cpcv_patch = cpcv_eval_to_scan_pattern_fields(cpcv_payload)
    print(json.dumps({
        "pattern_id": PID,
        "ptr_rows": len(rows),
        "ready": bool(ok),
        "blocked": detail.get("blocked"),
        "cpcv_promotion_gate": {
            k: cpcv_payload.get(k) for k in (
                "skipped","reason","cpcv_n_paths","cpcv_median_sharpe",
                "deflated_sharpe","pbo","promotion_gate_passed",
                "promotion_gate_reasons","evaluator",
            )
        },
        "scan_pattern_patch_keys": list(cpcv_patch.keys()),
    }, indent=2, default=str))
finally:
    try:
        sess.rollback()
    except Exception:
        pass
    try:
        sess.close()
    except Exception:
        pass
```

The rollback is unconditional (no early returns before the `finally`),
satisfying the brief's "wraps the whole thing in `sess.rollback()`
finally" requirement.

### 4c. Output

Writes the JSON to `scripts/audit-cpcv-gate-force-eval-<pid>-out.txt`
and stdout. Also captures stderr so any LightGBM import warnings are
visible. **No commit. No SQL writes.**

## 5. D3 — `scripts/audit-cpcv-gate-coverage-out.txt`

Generated by running D1 once. I will run it from the host, capture the
full output verbatim, and commit it.

If running D1 reveals environmental issues (postgres container down,
brain-worker not running, etc.) that prevent the audit, I will stop,
write a CC_REPORT documenting the blocker, mark NEXT_TASK
`STATUS: BLOCKED`, commit, and not invent results.

I'll also run D2 once against pattern 731 (default) and commit
`scripts/audit-cpcv-gate-force-eval-731-out.txt` as supporting evidence
referenced from the memo.

## 6. D4 — `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`

One-page memo, ~150 lines. Sections:

1. **TL;DR** — one-sentence verdict on where the funnel breaks.
2. **Methodology** — what I measured, sample size (50 of 275), why.
3. **Classification breakdown** — the summary table from D1, quoting
   **exact percentages** (no hand-waves like "most" / "majority").
4. **Force-eval result for pattern 731** — would-pass verdict, metrics,
   reasons; if blocked, the exact reason string.
5. **Concrete recommendation for Phase 1** — based on classification:
   - If `event_missing` dominates → Phase 1 enqueues synthetic
     `backtest_completed` events with payload
     `{"scan_pattern_id": <id>, "source": "cpcv_backfill_2026_05_11"}`,
     N per minute rate-limited.
   - If `event_pending_or_retry` dominates → Phase 1 is a dispatcher
     unblock, NOT a backfill. Different remedy.
   - If `event_done_but_no_handler_log` dominates → handler is
     short-circuiting silently; needs handler-side investigation before
     any backfill is safe.
   - If `handler_logged_but_no_persist` appears at all → critical bug;
     persist path is dropping verdicts.
6. **Open caveats** — 24h log window may miss older done events; 50/275
   sample may underrepresent rare buckets; force-eval is one pattern.

## 7. D5 — `docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-coverage-audit.md`

Standard CC_REPORT format from PROTOCOL.md: What shipped, Verification,
Surprises/deviations, Deferred, Open questions for Cowork.

Key items the report will surface:
- The 5 deliverables and commit hash.
- The classification numbers (referencing the memo).
- Whether Phase 1 backfill is the correct next step, per the memo.
- Any open question Cowork should rule on before Phase 1 (e.g.
  rate-limit setting, event source tag format).

## 8. NEXT_TASK update

After all 5 deliverables land and audit run succeeds:

- Flip `STATUS: PENDING` → `STATUS: DONE` in `docs/STRATEGY/NEXT_TASK.md`.
- Do NOT delete the file (PROTOCOL.md §3).

## 9. Truncation discipline

Per advisor brief §2.1, after each Write/Edit:
- `wc -l` on the new file.
- `git diff --stat` (will be `0 → N` for new files; sanity for size).
- `[System.Management.Automation.Language.Parser]::ParseFile($PWD\<path>, [ref]$null, [ref]$null)`
  on every new .ps1.
- AST parse not needed (no .py written).

If any check fails, restore from HEAD (for any pre-existing file I
might touch) or rewrite via Write (for new files) before commit.

## 10. Commit plan

One commit containing all 5 deliverables + NEXT_TASK flip:

```
audit(brain): cpcv-gate coverage Phase 0 (f-cpcv-gate-coverage-audit)

Read-only diagnostic for the promotion drought. Classifies 50 of 275
candidate patterns (active, PTR>=30, cpcv_n_paths NULL, lifecycle
not promoted/retired) by where the CPCV gate funnel breaks: missing
event / pending / dead / done-no-handler-log / handler-logged-no-persist.
Force-evaluates pattern 731 to confirm gate would produce a verdict
when reached.

- scripts/audit-cpcv-gate-coverage.ps1
- scripts/audit-cpcv-gate-force-eval.ps1
- scripts/audit-cpcv-gate-coverage-out.txt
- scripts/audit-cpcv-gate-force-eval-731-out.txt
- docs/AUDITS/2026-05-11_cpcv_gate_coverage.md
- docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-coverage-audit.md
- docs/STRATEGY/NEXT_TASK.md (STATUS: DONE)

No app/ code changes. No DB writes (force-eval rolls back). No migrations.
```

Then `git push origin main`.

## 11. Open questions (none blocking; flagging upfront)

1. **Default force-eval pattern is 731.** Parent brief E7 lists 731 as
   the largest-trade-count NULL-OOS pattern (10,341 trades). If Cowork
   prefers a different default (e.g. a smaller-trade-count one nearer
   the 30-row floor), say so in the response and I'll switch.

2. **24h grep window for handler logs.** Brain-worker container log
   rotation is unknown to me; if it's shorter than 24h, the
   `event_done_but_no_handler_log` bucket will be over-inflated. I'll
   call out the actual log span in the memo. If Cowork has a preferred
   window or container log retention info, mention it.

3. **`unknown` bucket disposition.** If any pattern lands in `unknown`,
   I'll surface its event_status verbatim in the memo so the bucket is
   useful, not a wastebasket.

---

## Awaiting

- [ ] APPROVED → I proceed with implementation as written above.
- [ ] REVISE: <feedback> → I rewrite this file and resubmit.
- [ ] ABORT: <reason> → I write a CC_REPORT and exit non-zero.
