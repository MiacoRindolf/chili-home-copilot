# Cowork Review: f-trump-usd-poisoned-quote-source-audit

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-07_f-trump-usd-poisoned-quote-source-audit.md`
**Reviewer:** Cowork.
**Date:** 2026-05-07.

## Verdict

**4/4 phases SHIPPED. APPROVE.** Two commits (`67eff1c` feat, `0c90344` docs). 11/11 helper tests pass. Zero net new magic numbers. The boundary-guard pattern is the right architectural call — single point of trust at `fetch_quote`, source-agnostic, naturally extends to future consumers. The thread that started with "TRUMP-USD won't exit" 36 hours ago now ends with the upstream cache poisoning closed at the data layer.

## What Claude Code did right

1. **Phase 1.2 found ARB-USD too.** I'd briefed this as a TRUMP-USD investigation; CC's history query surfaced ARB-USD as a parallel storm (98 rows over 2.5 days) that was already happening before TRUMP. Same fingerprint — identical bad price across all rows of each ticker — confirming the singleton-cache hypothesis from two independent samples. **The brief author (me) was anchored on a single ticker; CC widened the lens and made the diagnosis stronger.**

2. **Phase 2 deferred the price_bus write-time guard in favor of a single boundary guard at `fetch_quote`.** Brief Phase 2 listed multiple branches (write-time guard at price_bus IF that's the source, etc.). CC chose the architect-level path: validate at the consumer boundary, source-agnostic, no redundant invariant maintenance burden. That's the right call. A write-time guard at price_bus would have created a parallel invariant requiring its own tests + documentation; the boundary guard at `fetch_quote` covers ALL upstreams (price_bus, Massive WS, Massive REST, Polygon, yfinance) with one rule.

3. **Used the shared `is_implausible_quote` helper from `_exit_monitor_common`.** Net new magic numbers from this brief: ZERO. The 0.1x/10x bounds are sourced from the shared module; the new `_REJECTION_THRESHOLD = 5` and `_REJECTION_WINDOW_S = 600` constants are alert-rate tuning (not data-layer thresholds) and are documented as such. CC's "magic-number audit" section calling this out explicitly is exactly the kind of self-critical reporting that makes review easy.

4. **Phase 3 alert lives on `runtime_surface_state.market_data` — the existing surface.** No new alert pipeline. Same `state='degraded'` shape the existing market-data heartbeat uses. Future data-quality detectors can follow this pattern without inventing parallel surfaces. **Cookbook entry "runtime_surface_state.market_data is the alerting surface for data-quality issues" promotes this from one-off to design pattern.**

5. **Honest limitation report on Phase 1.1.** Cold-process diagnostic couldn't reproduce the in-process WS subscriber state because the script runs out-of-band. CC reported "diagnostic was less informative than hoped, Phase 1.2 was the load-bearing diagnostic" rather than overclaiming. Open Q #3 ("run via `docker exec autotrader-worker python /app/scripts/...` next time") is the right operational lesson.

6. **`Up 2 hours` clue on the autotrader-worker.** CC noticed the storm had already cleared 12h before investigation, AND that the autotrader-worker was at uptime 2h. That's evidence that a recent restart cleared the per-process cache — **which is the bug shape itself confirming the per-process-cache hypothesis**. Without a restart the storm would have continued indefinitely. The fix makes the cache durable against poison rather than relying on restarts.

7. **Postmortem actually filed.** Operator now has `docs/AUDITS/2026-05-07_trump-usd-poisoned-quote-postmortem.md` with the full Round-13 → 2026-05-04 ARB recurrence → 2026-05-06 TRUMP storm timeline. The "this has happened before" pattern is now documented for the next person who hits a third occurrence.

## What I'd push back on (none, this run)

Zero pushback. Each deviation was either explicit pre-authorization in the brief (Phase 2 branching choice was authorized — "Branch on what Phase 1.1 finds") or a defensible architectural call (single boundary guard vs scattered write-time guards) with clear reasoning.

## Answers to CC's open questions / escalations

1. **Per-ticker volatility-derived bounds (CC Open Q #1).** Defer. The 0.1x/10x bounds catch 100% of the storms we've seen (ratios 0.000124 and 0.00684, both 100x+ below the lower bound). A 0.12x case slipping through is theoretical until we actually observe one. Surface as `f-implausible-quote-per-ticker-vol` ONLY when a real low-ratio-but-implausible quote slips through. Not actionable today.

2. **Cross-process cache (CC Open Q #2).** Defer. CC's analysis is correct — the warm-up risk is primarily a stale-state risk (poisoned anchor → all subsequent quotes pass), not an immediate data-quality risk. The 5-in-10min alert mechanism would surface the storm even in this scenario (a poisoned cache anchor accepting subsequent legitimate quotes wouldn't trip the rejection counter; the bug shape would be different — chronic skew rather than acute rejection storm). Not in scope unless we observe the warm-up scenario in practice.

3. **Diagnostic reproduction from inside the container (CC Open Q #3).** Right operational lesson. Confirm `scripts/` is mounted in `autotrader-worker` (per `docker-compose.yml` it should be — same bind-mount as `app/`). Add `docker exec autotrader-worker python /app/scripts/dispatch-trump-quote-trace.py` as the canonical incident-response command in any future poisoned-cache runbook.

## Code-level spot checks

- `_exit_monitor_common.is_implausible_quote` is the single source of truth for the 0.1x/10x bounds. Confirmed via grep — `market_data.py:832-834` imports and uses it; the three exit-monitor lanes use it; nothing else has a parallel threshold. ✓
- `_KNOWN_GOOD_CACHE` is a module-level dict. Per-process scope. CC explicitly documented this in Surprise #3 as intentional ("the cache is fast-path memo, not authoritative state; cross-process consistency would require Redis or DB; that's premature").
- `_resolve_implausibility_anchor` priority: cache → open-Trade `entry_price` → None. The "None means accept" semantics for first-quote-ever is right per the brief Open Q #4 — the alternative (refuse if no anchor) would refuse all warm-up quotes and starve the system.
- `_record_implausible_rejection` uses a `deque` keyed by `(ticker, source)`. Per-source isolation tested explicitly; can't have rejections from the broken WS source push the healthy REST source over threshold.
- Boundary guard returns `None` on rejection. **Not a substitute price.** Forces every consumer to handle absence rather than acting on poison. The no-hardcoded-fallback rule applied correctly.

## Architectural state after this run

The implausible-quote story is now structurally complete:

| Layer | Mechanism | Where |
|---|---|---|
| **Shared bound** | `IMPLAUSIBLE_QUOTE_RATIO_LOW = 0.1`, `IMPLAUSIBLE_QUOTE_RATIO_HIGH = 10.0` | `_exit_monitor_common.py` |
| **Predicate** | `is_implausible_quote(px, anchor)` | `_exit_monitor_common.py` |
| **Boundary guard** | `_apply_boundary_guard` at `fetch_quote` | `market_data.py` |
| **Per-ticker anchor** | `_KNOWN_GOOD_CACHE` + open-Trade entry fallback | `market_data.py` |
| **Alert** | 5-in-10min per `(ticker, source)` → `runtime_surface_state.market_data` `state='degraded'` | `market_data.py` |
| **Consumer-side gate (defense-in-depth)** | `should_consult_monitor_after_refusal(reason, abstained_implausible=...)` | `_exit_monitor_common.py`, used by all 3 exit lanes |

The next data-quality issue (e.g., a non-implausible-but-still-suspect pattern like quote staleness) plugs into the same surface: extend `_apply_boundary_guard` with the new check, write `state='degraded'` with structured `details`, follow the same shape.

## Cookbook updates from this run

1. **Boundary validation > consumer validation when the cost difference is small.** Validating at every consumer leaks complexity and risks new consumers forgetting to validate. Validating at the data boundary is single-point, source-agnostic, and naturally extends. CC's `fetch_quote` boundary guard is the canonical example.

2. **Stale singleton fingerprint = identical bad value over multi-hour windows.** When you see this in production data, suspect an in-process cache without TTL or write-time sanity. The boundary-guard pattern from this brief is the durable answer.

3. **`runtime_surface_state.market_data` is the alerting surface for data-quality issues.** Not just heartbeats — implausibility rejections route here too with `state='degraded'` and structured `details`. Future detectors should follow the same shape.

4. **History queries triangulate diagnostic hypotheses.** Phase 1.2's SQL widened the investigation from one ticker to two, which strengthened the singleton-cache hypothesis from two independent samples instead of one. Always run the history check even when the surfaced incident has a single ticker — the priors update if the pattern is broader than expected.

5. **`Up Nh` for the affected container is a forensic clue.** A storm that ended exactly when the container restarted is evidence the bug is in the container's per-process state. The fix needs to make the state durable against the failure mode, not rely on restart hygiene.

## Watch items (operator-side, post-deploy)

The fix is already shipped + pushed (CC committed `67eff1c` and `0c90344`). After your next `autotrader-worker` restart:

- **Zero new `DATA_IMPLAUSIBLE` rows in `trading_stop_decisions` for the next 1h.** If a poisoned upstream resumes, `fetch_quote` returns None at boundary, the exit-monitor never sees the bad value, and no `DATA_IMPLAUSIBLE` row gets written. If rows DO appear, surface to me — that means a different code path is bypassing the boundary guard.

- **First `runtime_surface_state` row with `surface='market_data', state='degraded'`** during the next storm. That's the alert-pipeline proof of life. If you see it, the 5-in-10min mechanism is firing as designed.

- **`_KNOWN_GOOD_CACHE` warm-up behavior on container restart.** Each container starts with empty cache; first quote per ticker seeds the anchor. If the WS subscriber connects to a poisoned frame on first connect, that bad value becomes the seed. CC's Open Q #2 covers this — defer until observed in practice.

## Close-out for the thread

This is the natural end of the implausible-quote chain. The thread covered:

1. **2026-05-06 14:05 UTC** — User reports "TRUMP-USD won't exit." Direct patch to `crypto/exit_monitor.py` (`f-crypto-exit-monitor-pattern-exit-now`).
2. **2026-05-06 16:45 UTC** — `f-options-exit-monitor-pattern-exit-now-audit` shipped. Options got the LLM-advisory branch + helpers factored into `_exit_monitor_common.py`.
3. **2026-05-06 17:10 UTC** — `f-crypto-exit-monitor-pattern-exit-now-test` shipped. Case 5 surfaced the implausible-quote-vs-exit_now ordering bug as `xfail(strict=True)`.
4. **2026-05-06 17:55 UTC** — `f-fix-implausible-quote-vs-exit_now-ordering` shipped. Crypto + options got the refusal-aware gate.
5. **2026-05-06 (later)** — `f-exit-monitor-quote-guard-unification` shipped. Equity got the implausible-quote guard added; thresholds + post-refusal logic centralized.
6. **2026-05-07 11:15 UTC** — `f-trump-usd-poisoned-quote-source-audit` (this brief) shipped. Boundary guard at `fetch_quote` + 5-in-10min alert.

Six ship cycles, all green, all on the same architectural axis. The reasoning that started "the LLM is right but the engine refuses to act on its own data" ends with "the engine never sees bad data anymore." Clean closure.

## Next promotion candidates

With this brief shipped, the highest-value remaining QUEUED briefs are:

1. **`f-exit-parity-metric-v2`** — algo-trader-architect-grade structural work; data is now ≥48h accumulated. Highest structural value remaining.
2. **`f8b-verification-soak-3`** — verification soak; medium urgency.
3. **`bracket-writer-cover-policy-clarify`** — comment cleanup; low priority.

Operator's call which to promote next.
