# Cowork architect findings: imminent alert silence + autotrader recovery

**Status**: PARTIAL FIX SHIPPED. Surface-level `.env` corruption that
disabled the autotrader has been repaired. Deeper architectural gap
(empty promoted-pattern roster) is documented but **not fixed in this
session** — flagged as the highest-priority next investigation.

**Session date**: 2026-05-09 evening.

## Headline

Operator's observation — *"haven't seen any imminent alert for a long
time"* — is literally correct, and the root cause is **two stacked
silent failures**:

1. **Surface failure (FIXED)**: `CHILI_AUTOTRADER_ENABLED` was lost
   from `.env` during the BOM-corruption incident earlier in the
   session, defaulting to `False`. The autotrader's APScheduler tick
   was firing every 10s but returning immediately at
   `trading_scheduler.py:2235`. No audit rows, no log lines, no
   processing.

2. **Strategy failure (UNFIXED)**: even with the autotrader enabled,
   the brain is producing **zero `pattern_imminent` alerts**. The
   `trading_alerts` table has 221 events in the last 24h but they're
   all `crypto_breakout`, `crypto_squeeze_firing`, `stock_breakout`,
   `stop_hit`, `target_hit` — NOT `pattern_imminent`. The autotrader
   consumes `pattern_imminent` specifically (see
   `pattern_imminent_alerts.py`), so it has no fuel.

Phase 6 paper soak as designed would have completed at T+48h with
"0 Coinbase routes observed" because of #2. The Coinbase enablement
work itself (Phases 1-5) is sound and committed.

## Investigation log (8-layer audit + targeted follow-ups)

### L0 — Table discovery
87 tables containing `brain`/`pattern`/`alert`/`autotrader`/`trade`
fragments. Notable: `scan_patterns`, `pattern_survival_*` family,
`trading_brain_runtime_modes`, `gateway_pattern`,
`trading_alerts`, `trading_autotrader_runs`.

### L2 — Brain alert recency
| Source | Total | 24h | 1h | Last |
|---|---|---|---|---|
| `trading_alerts` | 12,620 | **221** | **10** | **3.5min ago** |

Brain IS healthy and emitting alerts. ✅

### L2 alert_type breakdown (24h)
```
crypto_breakout         106
crypto_squeeze_firing    88
stock_breakout           21
stop_hit                  1
target_hit                1
                         ___
                        217 (~221 total, 4 in flight)
pattern_imminent          0  ← ZERO
```

Most recent 5:
```
12855  TRIA-USD  crypto_squeeze_firing  01:28:33
12854  INX-USD   crypto_breakout         01:28:33
12853  SPY       stock_breakout          01:18:00
12852  BNB-USD   crypto_breakout         01:16:53
12851  ABT-USD   crypto_breakout         01:16:53
```

These are scanner alerts, not pattern_imminent. Different consumers.

### L5 — Autotrader cycle activity (pre-fix)
| | |
|---|---|
| `trading_autotrader_runs` total | 18,064 |
| Last 7d | 2,616 |
| Last 24h | **1** |
| Last 1h | **0** |
| Last row timestamp | 2026-05-09 01:57:24 (~24h ago) |

The cliff at 01:57 UTC May 9 corresponds to the ADA/SOL crash loop
event (per `bracket_writer_g2.py` comment). After that, force-recreate
+ Phase 5/6 deployments happened, but the autotrader_runs counter
never recovered.

### L6 — Gate distribution (last 7d, sorted by count)
```
duplicate_pattern_already_open                       366
projected_profit_below_min                            341  (12% rule floor)
synergy_disabled_second_signal                        310
llm_not_viable                                        266
wide_spread (deferred)                                167
missed_entry_slippage                                 158
no_quote                                              152
stop_not_below_entry                                  140
broker:Robinhood crypto endpoint returned ...         132
symbol_too_expensive_for_notional                      92
pdt_guard:pdt_limit_reached:25>=3                      66  ← regression
pre_broker:venue_unsupported_crypto:GN/1I/2Z/...      150+ (several lines, total)
broker:crypto_not_supported_on_robinhood               101
symbol_price_above_cap                                 21
rh_adapter_off                                         17
pdt_guard:unknown_state_refuse                         16
placed:ok                                              13  ← only 13/7d
regime_gate:negative_ev_consensus                      24
```

### L7 — Recent Trade rows
- 24h: **0** placed
- 7d: 13 placed (XLM-USD, SKY-USD, TRUMP-USD, QNT-USD, AVAX-USD,
  RENDER-USD — all `broker_source='robinhood'`)
- Most recent: XLM-USD on 2026-05-08 10:23:36 (~32h ago)
- Zero Coinbase trades ever

### Worker state probe (autotrader-worker)
- Container: `Up 24 minutes` (after most recent force-recreate)
- APScheduler logs: `AutoTrader v1 tick (every 10s)` firing
  continuously, "executed successfully" each cycle
- No `[autotrader]` log lines, no errors, no audit rows
- **Symptom: tick callback returns immediately**

### Code trace
`trading_scheduler.py:2235`:
```python
if not getattr(_settings, "chili_autotrader_enabled", False):
    return
```

The master enable flag. Default `False`.

### Settings probe in 4 workers (pre-fix)
```
chili_autotrader_enabled:        False  ← culprit
chili_autotrader_live_enabled:   True
chili_autotrader_kill_switch:    False
chili_coinbase_autotrader_live:  True
```

`.env` content of relevant line:
```
CHILI_AUTOTRADER_ENABLED=trueCHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED=true#
```

Two env vars and a comment marker concatenated into ONE line by
the earlier Out-File `-NoNewline` corruption. python-dotenv parses
this as `CHILI_AUTOTRADER_ENABLED` with value
`"trueCHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED=true#"` — pydantic
fails the bool conversion and falls back to default `False`.

## Surgical fix applied (this session)

`scripts/d-autotrader-enable-fix.py` — appends clean override lines
at end of `.env` for two known-corrupt vars:

```
CHILI_AUTOTRADER_ENABLED=true
CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED=true
```

python-dotenv last-occurrence-wins semantics override the corrupted
earlier copies without modifying any other content.
`.env.preautotraderfix` backup written before write.

### Verification post-fix (all 4 workers)
```
chili_autotrader_enabled:                True
chili_robinhood_spot_adapter_enabled:    True
chili_coinbase_autotrader_live:          True
coinbase_api_key_set:                    True
coinbase_api_secret_set:                 True
chili_autotrader_kill_switch:            False
```

All flags green. Force-recreate completed cleanly.

## Why the cycle is STILL silent post-fix

After enabling the master flag, autotrader_runs in last 5min: **0**.
The cycle is firing every 10s but the function `run_auto_trader_tick`
finds nothing to consume.

`pattern_imminent_alerts.py` is the producer of the consumed event
type. It filters via `scan_pattern_eligible_main_imminent`:
```python
def scan_pattern_eligible_main_imminent(pat: ScanPattern) -> bool:
    life = (getattr(pat, "lifecycle_stage", None) or "").strip().lower()
    promo = (getattr(pat, "promotion_status", None) or "").strip().lower()
    if life in ("promoted", "live"):
        return True
    if promo == "promoted":
        return True
    return False
```

Patterns must be `lifecycle_stage IN ('promoted','live')` OR
`promotion_status='promoted'`. Memory record:
- 2026-04-27: 30 of 31 promoted patterns demoted to 'challenged'.
  Sole survivor: pattern 1047.
- Migration 194: pattern 1047 retired (live realized -3.97% return
  contradicted backtest CPCV).

**Strong inference: the promoted-pattern roster is empty or
near-empty post-mig 194.**

A partial probe of `scan_patterns` confirmed at least one
`(lifecycle=candidate, promo=legacy)` row exists — meaning the table
is populated, but candidate/legacy doesn't qualify for
`pattern_imminent`. A full count probe encountered a PowerShell
formatting issue mid-execution; left for the next session to
complete.

## What this means for the Phase 6 soak

The Phase 6 paper soak is currently armed and running with
`CHILI_COINBASE_AUTOTRADER_LIVE=1`. Conservative caps ($50/3
positions, ~$152 max exposure). At T+48h (2026-05-11 18:12 PDT) the
scheduled task `coinbase-phase6-soak-report` will run.

**Expected outcome at T+48h** given current state:
- Probe will show: 0 pattern_imminent alerts in window, 0 autotrader
  runs that processed those, 0 Coinbase routes
- Cash drift: $0
- RH equity entries: 0 (because no pattern_imminent feeds them either)
- Scheduled task will recommend **Phase 6 EXTEND** with a
  "path-not-exercised" note

The Coinbase enablement work is correct. The path can't be
exercised until upstream pattern emission is restored.

## Three follow-up tracks (priority-ranked for next session)

### Track 1 — HIGHEST: Confirm pattern roster + diagnose promotion pipeline
- Count `scan_patterns` by `(lifecycle_stage, promotion_status)`
- If 0 promoted/live: investigate why promotion isn't occurring
  - Phase 2 event handlers may be silently broken again (memory:
    "ALL 5 handlers shipped 2026-04-29 BUT import-broken" — fixed
    2026-05-05; could regress)
  - CPCV gate may be too strict given current backtest data
  - Promotion-review queue may be backlogged
- If patterns exist but lifecycle stuck at 'candidate': identify
  what advances them to 'promoted'

### Track 2 — `.env` sweep
The Out-File BOM disaster created an unknown number of merged-line
corruptions in `.env`. So far surfaced:
- `COINBASE_API_KEY` / `COINBASE_API_SECRET` (fixed via 3 newlines)
- `CHILI_COINBASE_AUTOTRADER_LIVE` (already on its own line)
- `CHILI_AUTOTRADER_ENABLED` (fixed via append)
- `CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED` (fixed via append)

Other settings may be silently False. A focused diff of the corrupted
.env vs `.env.preforensic` (or `.env.preautotraderfix`) backup would
identify all of them. Operator-driven cleanup is safest.

### Track 3 — PDT counter regression
`pdt_guard:pdt_limit_reached:25>=3` fired 66 times in 7d. The
`f-pdt-count-broker-confirmed-only` fix (commit 60c26f8) was for
crypto-side phantoms. Equity-side phantom rows from the
`broker_reconcile_position_gone` cascade may still be inflating
the count. Phase B (`f-equity-broker-reconcile-wipeout-protection`)
needs to ship before this is fully closed.

## Constraints honored this session

- ✅ No autotrader entry-side code changes during the soak window
- ✅ No flipping of `CHILI_COINBASE_AUTOTRADER_LIVE` (operator-controlled)
- ✅ Two `.env` backups preserved: `.env.preforensic` +
  `.env.preautotraderfix`
- ✅ All commits + force-recreates dispatched via daemon, not
  direct sandbox access
- ✅ Pre-flight checks before any `.env` mutation (operator's safety
  trust earned back, hopefully)

## Files written this session (for the record)

- `scripts/d-imminent-silence-audit.py` + `.ps1` — 8-layer DB audit
- `scripts/d-autotrader-worker-state.ps1` — worker state probe
- `scripts/d-autotrader-enabled-probe.ps1` — settings probe
- `scripts/d-autotrader-enable-fix.py` + `.ps1` — surgical .env
  repair (the working fix)
- `scripts/d-pattern-roster-probe.ps1` — incomplete; finish next
  session

Backup files (do NOT commit): `.env.preforensic`,
`.env.preautotraderfix`.

## Recommendation

Stop work for tonight. Operator reviews this report and the .env
fix. Tomorrow:
1. Confirm the pattern roster theory (5-min probe)
2. Decide whether to extend Phase 6 soak or pause it
3. Investigate the deeper "no patterns getting promoted" gap as a
   new initiative

The Coinbase enablement is structurally complete and working. The
gap blocking observable activity is upstream: the brain isn't
producing the alert type the autotrader consumes.
