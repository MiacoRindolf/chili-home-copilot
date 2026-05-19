# NEXT_TASK: f-coinbase-maker-only-paper-soak

STATUS: DONE

## Outcome (2026-05-19)

Paper-soak started via daemon end-to-end:

**Step 1 — baseline captured (30d, Coinbase):**
- n=158 trades
- avg entry bps = **+153.93** (target post-flip: <30 bps)
- sd 306.01 / min −276.58 / max +1426.29

**Step 2 — flag flipped:**
- `.env`: `CHILI_COINBASE_MAKER_ONLY_ENABLED=true` (ASCII WriteAllBytes per memory)
- Container env verified: flag live

**Step 3 — worker restart:** `docker compose up -d --force-recreate autotrader-worker` clean.

**Step 4 — initial observations:** No maker-only attempts at T+3min. Autotrader is gated by an unrelated kill switch (`price_monitor_freeze:disconnected`); needs operator reset before any entries (maker-only or otherwise) can fire.

**Step 5 — automation added:** Weekly Windows scheduled task `CHILI-maker-only-tca-probe` fires Sundays 18:00 PT, runs the TCA-delta probe via daemon, emits machine-readable VERDICT. T+0 probe ran cleanly → `VERDICT=IN_FLIGHT`.

CC report: `docs/STRATEGY/CC_REPORTS/2026-05-19_f-coinbase-maker-only-paper-soak.md`

## Pending operator actions

1. **Reset the `price_monitor_freeze:disconnected` kill switch** when ready to let the autotrader trade. Without this, the maker-only flag has nothing to act on.
2. **Read the weekly probe output** at `scripts/dispatch-maker-only-tca-probe-out.txt` Sunday evenings.
3. **Promote** (leave flag on) if `VERDICT=IMPROVED` after sufficient trades. **Rollback** (`CHILI_COINBASE_MAKER_ONLY_ENABLED=false`) if `VERDICT=REGRESSED` or `NO_CHANGE`.

## What's queued next (operator picks)

After the maker-only paper-soak resolves, the remaining queued briefs:

1. **`f-stop-engine-payoff-ratio-gate`** — apply Tier A payoff_ratio gate (default ≥1.5, n≥5) to autotrader entry sizing. Smaller alpha-protection scope; ~30 LOC.
2. **`f-position-identity-phase-5-envelope-rename`** — the big position-identity refactor. Gated on at least one `[phase4_*]` log line in production (requires RH session restoration + a broker-alive / locally-closed discrepancy to fire).
3. **`f-pid-537-watcher-elevation-decision`** — gated on n=15 from the daily pid537 watcher.
