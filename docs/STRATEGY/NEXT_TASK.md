# NEXT_TASK: cleanup-2-healthcheck-and-protocol-infra

STATUS: PENDING

## Goal

Three small, scope-disjoint cleanup commits land before F6 starts. After this task:

1. **Strategy infrastructure is in version control.** `CLAUDE.md` (the protocol pointer block Cowork added) plus the entire `docs/STRATEGY/` tree (PROTOCOL.md, CURRENT_PLAN.md, NEXT_TASK.md, the CC_REPORT and review from F5 cleanup) are committed and pushed.
2. **Container `(healthy)` is durable.** The 90s `last_bar_at` healthcheck threshold no longer flaps when Coinbase's candles channel goes quiet on low-volatility pairs. Probe is split into two orthogonal checks (ws connectivity vs. candle freshness).
3. **The bracket-age classifier is documented.** A code comment in `exit_manager.py` explains why `brain_json.computed_at` vs. `entered_at` produces the bimodal distribution that `fast_exits_native` view relies on, so a future refactor doesn't accidentally normalize them and silently break the native/inherited filter.

## Why now

The F5 cleanup CC_REPORT confirmed:
- The unhealthy state is recurring and structurally driven by a too-tight healthcheck threshold (not transient, not a WS issue)
- F5's bracket-age classifier trick uses a load-bearing implicit invariant (`computed_at` is set once at bracket-decision time, not refreshed) that needs an inline comment so it's not lost
- Three working-tree changes (CLAUDE.md, docs/STRATEGY/, this very task file) are uncommitted — the protocol requires its own infrastructure to be versioned

We're not shipping F6 over a flapping unhealthy container — every future log review and incident triage gets harder. These are all small, surgical, low-risk changes.

## Scope — three subtasks, three commits, ordered

### 1. Commit strategy infrastructure FIRST

```
git add CLAUDE.md docs/STRATEGY/
git status   # confirm only those paths are staged
git commit -m "chore: strategy protocol infrastructure (Cowork ↔ Claude Code handoff)"
git push
```

The commit should include:
- `CLAUDE.md` (just the new "FIRST" section pointer block at the top)
- `docs/STRATEGY/PROTOCOL.md`
- `docs/STRATEGY/CURRENT_PLAN.md`
- `docs/STRATEGY/NEXT_TASK.md` (this file, which the previous task marked DONE — that gets committed in its DONE state since it's a historical record of what F5 cleanup was)
- `docs/STRATEGY/CC_REPORTS/.gitkeep`
- `docs/STRATEGY/CC_REPORTS/2026-05-01_f5-cleanup-and-baseline.md`
- `docs/STRATEGY/COWORK_REVIEWS/2026-05-01_F5_exit_manager.md`
- `docs/STRATEGY/COWORK_REVIEWS/2026-05-01_f5-cleanup-and-baseline.md`

Do NOT include `app/models/trading.py` or `.env.example` — both are unrelated working-tree changes the previous CC_REPORT correctly excluded.

### 2. Healthcheck split-probe

Modify `app/services/trading/fast_path/healthz.py`. Current single check on `last_bar_at` freshness with a 90s threshold flaps because Coinbase's candles channel goes silent on low-volatility pairs while WS, heartbeats, and L2 books continue normally.

**Refactor into two orthogonal probes that AND together for the 200/503 verdict:**

- **`ws_connected`** — passes if WS connection is healthy. Signals: `error_count_60s < cb_threshold` AND a recent heartbeat-or-book-or-bar event (i.e. SOME freshness on SOME channel) within a short window. Suggested threshold: **60s**. Reads `fast_path_status` per pair plus the most-recent `fast_orderbook.snapshot_at`.

- **`candle_freshness`** — passes if AT LEAST ONE pair has a `fast_snapshots` row within a long window. Suggested threshold: **300s (5 min)**. The "at least one" is deliberate — Coinbase legitimately emits no candles for individual quiet pairs for several minutes, but they don't all go quiet at once. If they DO all go silent for 5 min, that's a real outage.

`/healthz` returns 200 iff both probes pass. The response body should include both probe states so an operator can see WHICH check failed without log diving:

```json
{
  "ok": true,
  "ws_connected": true,
  "candle_freshness": true,
  "details": {
    "ws_window_s": 60,
    "candle_window_s": 300,
    "newest_book_age_s": 0.4,
    "newest_bar_age_s": 47.2,
    "freshest_pair_for_bars": "ETH-USD"
  }
}
```

Boot grace logic stays as-is (the existing 30s grace on first start should still apply to `candle_freshness` — give the snapshot replay time to populate).

Then commit:
```
git add app/services/trading/fast_path/healthz.py
git commit -m "fix(fast-path): split /healthz into ws_connected + candle_freshness probes"
git push
```

After commit, restart fast-data-worker and observe `(healthy)` consistently for **at least 10 minutes** before declaring success. If it still flaps, STOP and flag — don't iterate calibrations in this task.

### 3. Document the bracket-age classifier invariant

In `app/services/trading/fast_path/exit_manager.py`, find the spot where `brain_json` gets populated (you wrote this in F5; the report mentions `computed_at` is set at bootstrap time). Add a docstring/inline comment along the lines of:

```python
# IMPORTANT: brain_json["computed_at"] is set ONCE at the moment the
# bracket is decided — at entry time for native F5 trades, at bootstrap
# time for inherited F4-era positions. The gap between this and
# ``entered_at`` is what migration 219's ``fast_exits_native`` view
# uses to filter inherited rows out of P/L analysis. Do not refresh
# this timestamp on later updates; doing so silently breaks the
# native-vs-inherited classifier.
```

Adjust wording to match your preferred style; the load-bearing fact is "computed_at is set once at bracket-decision time and must never be refreshed."

Then commit:
```
git add app/services/trading/fast_path/exit_manager.py
git commit -m "docs(fast-path): document bracket-age classifier invariant in exit_manager"
git push
```

## Brain integration (reuse, don't rewrite)

- `app/services/trading/fast_path/healthz.py` — read it for context, refactor in place.
- `app/services/trading/fast_path/status_tracker.py` — its `error_count_60s` and `last_reconnect_at` fields are what the `ws_connected` probe should read.
- `app/services/trading/fast_path/db_writer.py` — its in-memory queue depth could optionally be a third signal for `ws_connected` (if queue is healthy, downstream is consuming, which is upstream evidence WS is alive). Not required.

## Constraints / do not touch

- **Live-placement safety belts.** All 8 layers in `_place_coinbase_order_live`, `is_live_authorized()`, the mode_interlock gate.
- **Strategy thresholds.** No tuning of `MIN_SIGNAL_SCORE`, `MAX_SPREAD_BPS`, `IMBALANCE_LONG_THRESHOLD`, `VOL_BREAKOUT_MULT`, `ALERT_RECENCY_MAX_AGE_S`, `MAX_OPEN_POSITIONS_PER_PAIR`, `DAILY_NOTIONAL_BUDGET_USD`. None of them. F6 will derive these from data.
- **Stop / target / time-stop bracket policy.** Same as last task — F6 needs the existing distribution as training signal.
- **The 11 inherited bootstrap positions.** Don't touch them. Exit manager handles them naturally.
- **`models/trading.py` and `.env.example` working-tree changes.** These are someone else's work. Leave uncommitted; they'll be picked up in their proper commit later.

## Out of scope

- F6 signal half-life mining (next task after this).
- Tuning any healthcheck threshold beyond the 60s + 300s defaults proposed above (if you have a strong reason to deviate, propose it in Open Questions, don't deviate silently).
- Changing the `/healthz` URL, port, or endpoint shape beyond adding the JSON response body fields described.
- LISTEN/NOTIFY conversion. Watchdog task. Correlation gate. Anything new.

## Success criteria

1. `git log --oneline -5` shows three new commits, all pushed to origin
2. `git status` is clean for the three sets of files this task touched (untouched files stay untouched)
3. After the healthz commit + container restart, `docker compose ps fast-data-worker` shows `(healthy)` for at least 10 consecutive minutes (verify by waiting and re-checking; healthcheck runs every 30s by compose default, so 20+ green checks)
4. `curl -k https://localhost:8000` is irrelevant — instead `docker compose exec fast-data-worker python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8090/healthz').read())"` should return JSON with `ws_connected: true` AND `candle_freshness: true`
5. `docs/STRATEGY/CC_REPORTS/2026-05-02_cleanup-2-healthcheck-and-protocol-infra.md` written following PROTOCOL.md format. (Date may roll to 2026-05-02 by the time you finish; use whatever today's UTC date actually is.)

## Open questions for Cowork (surface in your report only if relevant)

- **Are 60s (ws) and 300s (candles) the right thresholds?** I picked them from Coinbase candle-cadence anecdata. If you observe live data after deploy that suggests one or both should move (e.g., even 300s is too tight on extremely quiet pairs), document the observation and propose new defaults — don't tune them inside this task.
- **Should the `details` block in the /healthz JSON also include per-pair age breakdown?** The current proposal returns aggregate (newest book age, freshest pair for bars) which is enough for binary decisions. Per-pair detail would help debugging but bloats the response. Defer to your judgment; if you add per-pair, keep it under a `pairs:` key so the top level stays small.
- **Anything you want me to retroactively add to PROTOCOL.md?** First end-to-end run revealed F5-cleanup followed it cleanly. If anything was ambiguous, surface it.

## Rollback plan

- Strategy infra commit: simple revert; no behavioral change in app code.
- Healthcheck commit: revert restores 90s single-probe behavior. Container will go back to flapping but won't crash. Safe.
- exit_manager comment commit: pure docstring change, no behavior. Pure rollback.
