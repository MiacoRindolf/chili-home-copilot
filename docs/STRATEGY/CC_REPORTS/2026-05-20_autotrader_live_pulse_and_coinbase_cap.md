# Autotrader Live Pulse + Coinbase Cap Alignment - 2026-05-20

## Summary

After the bracket and sell-event repairs, the autotrader was no longer blocked by shadow-pattern noise, PDT over-counting, or stale missing-stop state. The remaining live Coinbase blocks were caused by an internal cap mismatch: Coinbase's default venue cap was `$50`, while the autotrader's crypto order floor and payoff sizing produce roughly `$300-$375` candidate orders.

## Operator Config Change

`.env` was updated with:

```text
CHILI_COINBASE_MAX_NOTIONAL_USD=400
CHILI_COINBASE_MAX_CONCURRENT_POSITIONS=1
```

This keeps Coinbase paper-soak conservative: one live Coinbase position at a time, capped to about one pattern-sized entry. Existing flags remain live:

```text
CHILI_COINBASE_MAKER_ONLY_ENABLED=true
CHILI_AUTOTRADER_PAYOFF_SIZING_ENABLED=true
```

`autotrader-worker` was force-recreated and confirmed to see all four env vars.

## Smoke Test

`per_venue_cap_check("coinbase")` in the live container now returns:

- `$300` proposed notional -> allowed
- `$375` proposed notional -> allowed
- `$401` proposed notional -> blocked

## Live Pulse

Post-repair autotrader ticks show `candidate_pool=0`, not a blocker. There are simply no fresh eligible alerts in the current after-hours / low-signal window.

Last six-hour blockers were historical:

- `coinbase_cap:venue_notional_cap_exceeded` before the cap flip
- `selector:shadow_promoted_pattern_eval` before retiring/promoting the noisy patterns
- `monitor_exit_rejected: Sell may cause PDT designation` from ABNB before the PDT cutoff correction; ABNB later closed

Current open positions are eight Robinhood positions, all with stops. Coinbase open positions are zero, so the next eligible Coinbase alert should exercise maker-only routing under the new cap.

