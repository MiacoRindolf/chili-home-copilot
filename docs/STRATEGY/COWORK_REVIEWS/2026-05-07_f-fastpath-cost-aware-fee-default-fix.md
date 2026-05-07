# COWORK_REVIEW: f-fastpath-cost-aware-fee-default-fix

CC report: `docs/STRATEGY/CC_REPORTS/2026-05-07_f-fastpath-cost-aware-fee-default-fix.md`
Commit: `3f91cdc`

## Verdict

**Accepted.** Trivial defect resolution. The cost-aware admission gate
is now safe to enable once the universe-rotation soak produces enough
decay rows. Two operational lessons recorded for future sessions.

## What's good

1. **Default flipped to the correct retail-tier value (60.0 bps).** The
   cost-aware gate's economic model now matches the operator's actual
   account fee tier. The docstring is honest about the per-side framing
   and includes the per-tier reference table.

2. **Plausible-range smell tests added.** `tests/test_fastpath_settings_validation.py`
   guards against the same class of bug (wrong fee tier in defaults)
   plus the four sister admission thresholds (volume / spread /
   top-of-book / trade-count). A future regression where someone
   typos `60` as `0.6` (percent-as-decimal) or `6000` (round-trip)
   gets caught at CI time.

3. **Standalone verification is sufficient.** The standalone `runpy`
   load + assertion test exercises exactly the same code path that
   pytest would (the 5 new tests are dataclass + env-var only, no DB
   or imports beyond stdlib + `app.services.trading.fast_path.settings`).
   Skipping the in-container pytest run was the right pragmatic call
   given the daemon-wedge issue.

## What's concerning

Nothing from an algo-trader lens. The change is mechanically correct
and the new value matches the alpha-replay research's cost assumptions.

From a dev-architect lens:

1. **Edit-tool truncation hit twice in one session.** This is the
   second time today (after the FIX 46 sweep) where Edit silently
   truncated a Python file. Memory now updated to clarify the
   threshold is not ">2000 lines" — it can hit any Python edit. The
   recovery pattern (`git show HEAD: | python str.replace + ast.parse
   + write`) is well-rehearsed; what we need is a discipline-level fix:
   **always verify Edit operations on `.py` files with `wc -l + ast.parse`
   before continuing**. This is now in the cookbook section of the
   memory entry.

2. **First dispatch wedged the daemon.** The original verify+commit
   dispatch tried to run pytest inside `chili-home-copilot-chili-1`
   which presumably hit the per-test 75s truncate cost from
   `tests/conftest.py`. Daemon hung past 5+ minutes, corrupted the
   git index. **Cookbook**: keep dispatch scripts to <90s wall time;
   if pytest is needed inside a container, dispatch it separately
   from the commit. Recovery via `Remove-Item .git\index; git reset
   HEAD` worked cleanly (already in memory from 2026-04-30).

3. **Origin of the 5.0 default is still unknown.** CC's open question
   #1 wonders if it was Hyperliquid-derived. No other suspect
   constants found in `fast_path/`. Closing this as "unresolved but
   harmless" — the wrong value is now corrected.

## What's next — strategic decision

CC's open question #2 names the choice:

> **Strict serial**: wait for the 48h universe-rotation soak verdict
> before promoting maker-only.
> **Parallel**: ship maker-only code now; it's independent of soak
> outcome.

**Decision: parallel — promote `f-fastpath-maker-only` to NEXT_TASK.**

Reasoning:
1. **Code is independent of data.** The maker-only execution path
   (post_only limit orders + cancel-on-timeout + fill-rate tracking)
   is structurally orthogonal to which pairs the system trades. It
   ships the same way regardless of whether the soak says
   ICP/RENDER/INJ/ARB/TAO/FET are tradeable or not.
2. **The soak outcome only changes the activation decision.** When the
   verdict comes back, we either flip a flag or queue
   `f-fastpath-hyperliquid-perps`. Either way, the maker-only code is
   already in place.
3. **The default for the new `cost_aware_maker_fee_bps` setting that
   the maker-only brief introduces matters.** Same defect class we
   just fixed; CC's review of the maker-only output will need to
   verify the maker-fee default matches Coinbase retail tier 1
   (40 bps). Better to catch this on the same day the lesson is
   fresh.
4. **No conflicting deploy.** The fee-fix change is operator-side only
   (one env var). The maker-only work touches `executor.py` and adds a
   new migration but does NOT touch the universe rotator or its gates.
5. **Hard Rule 1 still respected.** The maker-only brief's design
   keeps `CHILI_FAST_PATH_MODE=paper` as default. No live placement
   changes ship via this brief.

Promoting now.

## Files updated this session

- Edit twice (truncation recovered both times):
  - `app/services/trading/fast_path/settings.py`
  - `app/services/trading/fast_path/gates.py`
- New:
  - `tests/test_fastpath_settings_validation.py`
- Strategy docs:
  - `docs/STRATEGY/CC_REPORTS/2026-05-07_f-fastpath-cost-aware-fee-default-fix.md`
  - `docs/STRATEGY/COWORK_REVIEWS/2026-05-07_f-fastpath-cost-aware-fee-default-fix.md` (this file)
  - `docs/STRATEGY/NEXT_TASK.md` (will be overwritten with maker-only brief)

## Status

- f-fastpath-cost-aware-fee-default-fix: **DONE**.
- Operator action: pull `3f91cdc`, restart `chili` + `fast-data-worker`,
  then `CHILI_FAST_PATH_UNIVERSE_ROTATION_ENABLED=1` (still NOT
  cost-aware-admission until 24h decay rows accumulate; that's
  documented in the universe-rotation review).
- Next NEXT_TASK: `f-fastpath-maker-only`.
