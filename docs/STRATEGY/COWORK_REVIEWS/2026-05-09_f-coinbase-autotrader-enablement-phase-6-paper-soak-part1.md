# COWORK_REVIEW: f-coinbase-autotrader-enablement (Phase 6 Part 1: paper-soak observability)

**Status**: PART 1 SHIPPED. Probe script + soak documentation
landed. Operator-side LIVE flip + 48h soak window are next. Part 2
(soak completion report) generates at T+48h after operator
re-promotes Phase 6.

**Commit**: `c6315a5` — feat(soak): Phase 6 paper-soak observability
probe + CURRENT_PLAN entry.

## What Part 1 delivered

| Deliverable | Result |
|---|---|
| `scripts/d-cb-phase6-soak-probe.py` (10579 bytes) | ✅ 7-section read-only probe (routing dist / cost-gate dist / cap-gate dist / Coinbase fills / bracket coverage / cash drift / anomaly summary) |
| CURRENT_PLAN.md soak section | ✅ Phases 1-5 status pointer, conservative caps, probe runbook + cadence (T+1h/T+12h/T+24h/T+48h), anomaly thresholds (RED/AMBER/INFO), kill-switch references, Phase 7 promotion criteria, Part 2 trigger |
| Sanity-test at T+0 | ✅ probe runs end-to-end against production DB + Coinbase API; no errors |

CC's split into Part 1 (now) + Part 2 (T+48h) is the right
shape. Part 2 isn't writable yet — it depends on data that
doesn't exist until the soak runs.

## Live verification (Cowork-direct)

Ran the probe against production at T+0:

```
# d-cb-phase6-soak-probe @ 2026-05-10T00:31:54Z
# window: trailing 1h (since 2026-05-09T23:31:54Z)

=== 1. ROUTING DISTRIBUTION (selector decisions) ===
(no autotrader runs in window)

=== 2. COST-GATE DISTRIBUTION ===
(no cost-gate decisions in window)

=== 3. CAP-GATE DISTRIBUTION (Coinbase per-venue cap) ===
(no cap-gate decisions in window)

=== 4. COINBASE FILLS (Trade rows broker_source='coinbase') ===
(no Coinbase Trades in window)

=== 5. BRACKET COVERAGE (Coinbase entries) ===
(no Coinbase entries to check coverage on)

=== 6. CASH DRIFT (current vs baseline) ===
  baseline cash: $2200.01
  current cash:  $2200.01
  drift:         $+0.00
  verdict:       GREEN — within tolerance

=== 7. ANOMALY SUMMARY (acceptance criteria) ===
  [INFO] no Coinbase entries this window — path not exercised yet
  [GREEN] cash drift $+0.00
```

Probe works in my hands. Output is well-structured: empty
sections get clean `(no X in window)` messages instead of
crashing or rendering empty tables. Cash baseline matches
operator's $2200.01.

## Architectural decisions CC made well

1. **Two-part split** (Part 1 tooling now, Part 2 report at
   T+48h). Aligns with the wall-clock nature of a soak — there's
   no "shipping the soak result" before the soak finishes.
2. **JSON output flag** (`--json`) for machine-readable parsing
   if operator wants to chart it.
3. **Configurable window** (`--window-hours`). Default 48h
   matches the brief; smaller windows for interim check-ins.
4. **Worst-case math documented**: $150 max exposure + $1.80
   round-trip fees + $2.25 stop slippage = ~$4 worst-case
   realized loss. Within the $5 amber threshold by design.
5. **CC report structure**: clear "Part 1 done / Part 2 pending"
   header + explicit Part 2 plan so the next CC session knows
   exactly what to produce.

## Operator-side actions to start the soak

Per CC's report (verbatim — this is what flips the system from
shadow-log to live):

```bash
# 1. Edit .env: add or change
CHILI_COINBASE_AUTOTRADER_LIVE=1

# 2. Force-recreate the 4 workers
docker compose up -d --force-recreate \
  chili autotrader-worker scheduler-worker broker-sync-worker

# 3. Verify env pickup across all 4 workers
for c in chili autotrader-worker scheduler-worker broker-sync-worker; do
  docker exec chili-home-copilot-${c}-1 python -c \
    "from app.config import settings; \
     print('${c}:', settings.chili_coinbase_autotrader_live)"
done
# Expected: True in every container

# 4. Run probe at T+1h, T+12h, T+24h, T+48h
python scripts/d-cb-phase6-soak-probe.py --window-hours 12

# 5. At T+48h: re-promote Phase 6 NEXT_TASK to PENDING
#    Cowork dispatches CC; CC writes Part 2 soak completion report.
```

## What to watch during the 48h window

CC's anomaly thresholds, in order of severity:

**RED (halt soak immediately):**
- Coinbase entry without bracket coverage (60s gap or no
  `broker_stop_order_id`)
- Cost-gate decision contradicting the rule (e.g.,
  `coinbase_clears_fee_threshold` for a < 1.5% edge)
- Cash drift > $5 from $2200.01 baseline (unexplained)
- Any "Insufficient balance in source account" rejection
  (would suggest the buying-power-not-wired-in gap matters)

**AMBER (surface in next probe; consider abort):**
- A Coinbase position resting > 24h without fill / cancel
- `selector:no_venue_supports` for a known long-tail ticker
  (selector logic regression)
- Bracket placement timing > 5 min post-fill

**INFO (just log, no action):**
- 0 Coinbase entries during a window — path not exercised; not
  a failure mode; CC will recommend extending the window or
  using a synthetic alert at T+48h if total stays at 0

If RED hits: `CHILI_COINBASE_AUTOTRADER_LIVE=0` in `.env` +
force-recreate. Investigate. RH unaffected.

If catastrophic: `CHILI_AUTOTRADER_KILL_SWITCH=1` in `.env` +
force-recreate. Halts both venues in 30s.

## Real-money risk envelope

| Scenario | Loss |
|---|---|
| Worst case (every position hits stop, 0.5% slippage, full fees) | ~$4 |
| Black swan (every position 100% loss, never recovers) | ~$152 |
| Probable case for 48h soak | $0 to $20 (typical crypto noise + fees on 0-3 entries) |

All three are within the operator-acceptable envelope for
verification. The black-swan case (-$152) is improbable for
liquid crypto over 48h but documented as the absolute ceiling.

## Recommendation

**Operator decides when to flip LIVE=1**. The probe + docs
are armed. The defensible move is:

1. Read this review + CC interim report.
2. Pick a moment when you'll be online for the next 1-2h to
   watch the first cycle (e.g., not right before sleep).
3. Flip the flag, force-recreate, run probe at T+1h to confirm
   selector + cost-gate decisions are flowing.
4. Soak runs 48h. Run probe at T+12h / T+24h.
5. At T+48h: re-promote Phase 6 NEXT_TASK to PENDING. Cowork
   dispatches CC. CC writes Part 2 report and recommends Phase 7
   green-light OR Phase 5.5 / 6.5 fix.

If at any point the probe shows a RED anomaly, flip
`CHILI_COINBASE_AUTOTRADER_LIVE=0` and surface to Cowork for a
6.5 fix brief.

## Constraints honored (Part 1)

- ✅ No code changes during soak window. Probe is a script,
  not a runtime change.
- ✅ No autotrader edits. Read-only observability.
- ✅ Conservative caps stay conservative ($50 / 3 positions).
- ✅ Kill-switch references documented (both global +
  Coinbase-only).
- ✅ Edit-tool truncation discipline.
- ✅ Hard Rule 1 (live-placement safety belts unchanged).
- ✅ Hard Rule 5 (prediction-mirror authority untouched).

## What Part 2 will produce (T+48h)

Per CC's plan:
1. Probe with `--window-hours 48`.
2. Cross-check `trading_autotrader_runs` + `trading_trades` +
   `trading_bracket_intents` for full-window aggregates.
3. Pull final cash from Coinbase API.
4. Apply 6 acceptance-criteria checks.
5. Recommend Phase 7 GREEN-LIGHT, Phase 5.5, Phase 6.5, or
   Phase 6 EXTEND.
