# NEXT_TASK: (none — fast-path/maker-only initiative complete; awaiting AWS recovery + operator direction)

STATUS: DONE

The fast-path rotator + maker-only initiative is complete in HEAD as of 2026-05-08.

## What's shipped today

| Brief | Status | Commits |
|---|---|---|
| `f-fastpath-universe-rotation` | DONE | foundation in `22cb7bd`/`d83ff03`/`a096651`/`107c349` |
| `f-fastpath-cost-aware-fee-default-fix` | DONE | `3f91cdc` |
| `f-fastpath-rotator-coinbase-fixes-bundle` | DONE | `727456e` |
| `f-fastpath-rotator-auction-mode-fix` | DONE | `bb6a4e4` |
| `f-fastpath-rotator-http-retry` | DONE | `db34f5d` |
| `f-fastpath-maker-only` (foundation) | DONE | foundation in CC's earlier session |
| `f-fastpath-maker-only-executor` | DONE | `b994373`/`381e151`/`e12142e`/`347ad4f`/`4ed74f2`/`e9a6a45` |

End state: maker-only execution fully wired, behind `execution_mode=taker` default. Bit-identical at switchover. Tables (mig 232) populated when rotator + executor run; status endpoint at `GET /api/trading/fast-path/maker-stats`.

## Why no new NEXT_TASK

**Track A (operator-side rotator soak) is environmentally blocked.** Coinbase REST is currently 100% unreachable from the operator's Docker container — AWS Northern Virginia data center outage (overheating) per Google AI overview 2026-05-08. Customer funds confirmed safe; markets in Cancel-Only mode. Recovery expected on AWS's timeline, not ours.

**Track B (CC code work) is at a natural pause.** The full rotator + maker-only chain is in HEAD. Anything more before observed soak data is premature optimization.

## What the operator can do

1. **Monitor https://status.coinbase.com/** for restoration.
2. **Periodically check** (no rush, no manual action needed during outage):
   ```powershell
   docker exec chili-home-copilot-scheduler-worker-1 python -c "import requests; print(requests.get('https://api.exchange.coinbase.com/products', timeout=8).status_code)"
   ```
   When this returns `200`, the rotator's hourly cron will start populating `fast_path_universe` automatically — the retry layer handles the partial-recovery phase.
3. **After ~24h of shadow rows accumulating**, evaluate the alpha-replay top picks (RENDER, ICP, ARB, INJ, TAO, FET) actually appear and have decay data forming.
4. **After ~48h of shadow soak**, consider flipping `CHILI_FAST_PATH_EXECUTION_MODE=maker_only` in `.env` and re-up.
5. **At that point**, ping me and we'll do a structured review of the soak data.

## Three pivot directions for tomorrow's session

When you're ready for the next initiative:

1. **Soak verification brief** — short, becomes actionable when egress recovers. Pure analysis (Cowork + operator), no CC code. Verifies rotator rows + maker-stats endpoint + per-pair fill rate distributions.

2. **`f-fastpath-microstructure-features-v2`** — toxic flow, depth-decay, OFI. Out-of-scope of today's work but on the original alpha research roadmap. Real signal-quality work.

3. **`f-fastpath-hyperliquid-perps`** — alternative venue with much cheaper fees (~3.5 bps taker vs Coinbase 60 bps). Could survive the operator's actual volume tier without maker-only constraint. Bigger scope but addresses the economic root cause.

These aren't promoted yet — operator picks tomorrow.

## Operator action items right now

None. Sleep. The system is in a consistent state; nothing degrades while waiting.

## What CC should do if `claude` is launched right now

Read this NEXT_TASK, see STATUS: DONE, say so, and ask the operator for direction. Do not auto-pick one of the three pivot options above without explicit operator buy-in.
