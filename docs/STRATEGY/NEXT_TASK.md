# NEXT_TASK: f-fastpath-maker-only

STATUS: PENDING

**Re-promoted 2026-05-08** after `f-fastpath-rotator-coinbase-fixes-bundle` shipped (commit `727456e`). The rotator is now functional in HEAD; the deploy-side verification is operator-track. This brief lays the maker-only execution path so it's ready when the rotator's shadow rows accumulate.

## Why now

The rotator can finally scan Coinbase and emit `fast_path_universe` rows
(verified in HEAD: `_http_get_json` uses `requests`, `_fetch_book` populates
top-of-book sizes, `.env` workaround removed, all 12 helper tests pass).
Once the operator deploys + recreates the three services, shadow pairs
will start accumulating `fast_signal_decay` rows.

But — per the alpha-replay research — **none of those pairs clear taker
round-trip cost**. ICP-USD clears the maker round-trip
(+2.76 bps net at 5m). RENDER/ARB/INJ/TAO sit close. **Maker-only mode
is the only economic path for live activation on Coinbase at retail
fees.** Code can ship in parallel with the rotator soak.

References:
- Original brief: `docs/STRATEGY/QUEUED/f-fastpath-maker-only.md`
- Alpha replay: `docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md`
- Just-completed rotator review: `docs/STRATEGY/COWORK_REVIEWS/2026-05-08_f-fastpath-rotator-coinbase-fixes-bundle.md`

## Goal

Implement the maker-only execution mode per the QUEUED brief. Full scope
in `docs/STRATEGY/QUEUED/f-fastpath-maker-only.md`. Summary:

1. **Migration 232** — `fast_path_maker_attempts` and `fast_signal_decay_maker_filled` tables.
2. **Three execution-mode flags** — `taker` (default) / `maker_only` / `maker_first_then_taker`.
3. **Three new settings** — `cost_aware_maker_fee_bps: float = 40.0`, `maker_cancel_on_timeout_s: int = 10`, `maker_first_taker_fallback_s: int = 5`. All settings-tunable; doc comments per the no-magic-numbers rule.
4. **`place_maker_only` path in `executor.py`** — `post_only=true` limit orders, cancel-on-timeout, 1-outstanding-per-(ticker,side) cap.
5. **`fast_signal_decay_maker_filled` writer** — adverse-selection-aware decay table.
6. **`gate_cost_aware_admission` reads from `fast_signal_decay_maker_filled`** when `execution_mode == 'maker_only'`. Cold-start `no_data` allows-through.
7. **Status surface** — per-pair maker fill rate over last 24h.
8. **Tests**:
   - `tests/test_fastpath_maker_only.py` (helper-level; broker stub).
   - `tests/test_fastpath_maker_settings_validation.py` — explicit `test_cost_aware_maker_fee_bps_default_is_retail_tier_1` asserting `40.0`. Same defect-class as today's earlier fee-fix; CC's review must verify this.

## Acceptance criteria

Mirrors the QUEUED brief. Highlights:

- All 5 new tests pass; existing 7 cost-aware-gate + 7 rotator helper-level tests still green; existing `test_fastpath_settings_validation.py` (5 tests) still green.
- `cost_aware_maker_fee_bps` default = `40.0` exactly (Coinbase Advanced Trade retail tier 1 maker, per-side, in bps). Plausible-range test `[1.0, 100.0]`.
- Migration 232 applied idempotently; `fast_path_maker_attempts` and `fast_signal_decay_maker_filled` tables exist with documented columns; CHECK constraint on `fill_outcome`.
- `executor.py` paper-mode default unchanged; `execution_mode='taker'` is the default; bit-identical at switchover.
- CC report at `docs/STRATEGY/CC_REPORTS/2026-05-08_f-fastpath-maker-only.md`.

## Brain integration (reuse, don't rewrite)

Same as the QUEUED brief's section. Highlight: `coinbase_ohlcv.py` HTTP pattern is the safe path for any new Coinbase REST integration (default `requests` UA passes Cloudflare; custom UAs do not — verified today).

## Constraints / do not touch

(All from the QUEUED brief, plus three new ones learned today:)

- **Edit-tool truncation discipline (HARD).** Memory: `reference_2026_05_07_widespread_truncation.md`. Three rounds of silent file truncation today (settings.py, gates.py, then universe_rotator.py + test file post-commit). For any non-trivial edit:
  - Use the splice pattern from the start: `git show HEAD:<path> | python str.replace + ast.parse + write`.
  - Verify post-edit with **(a) `wc -l` against HEAD, AND (b) `ast.parse()`**.
  - Critical files for this brief that **must use splice pattern**: `executor.py`, `gates.py`, `calibration.py`, `decay_miner.py`. The Edit/Write tool may *appear* to succeed and silently drop trailing content.
- **Truncation-scan as Step 0** of CC's run. Memory has the one-liner. Run it before any code work; if any file is shorter than HEAD, restore via `git checkout HEAD -- <file>` first. Don't start writing code into a truncated working copy.
- **Operator's volume tier may not be tier 1.** The `cost_aware_taker_fee_bps=60.0` default we shipped today is tier 1 (<$10k 30d volume). The new `cost_aware_maker_fee_bps=40.0` is tier 1 maker. **Surface in the CC report whether the operator should override either via env.** Don't assume; ask.
- **Hard Rule 1**: live-placement safety belts unchanged.
- **Default `CHILI_FAST_PATH_EXECUTION_MODE=taker`** (preserves current behavior).
- **Migration 232 must check `_migration_NNN_` registry.** 230 and 231 are taken. 232 is next free.
- **Tests use `_test`-suffixed DB.**
- **No new magic numbers.**
- **No removal of taker-mode behavior.** It stays as benchmark.

## Out of scope

(Same as QUEUED brief.) Hyperliquid perps, microstructure features (OFI, depth-decay, toxic flow), queue-position estimation, smart routing, backfill of decay tables — all separate briefs.

## Sequencing within this task

1. **Truncation scan** (mandatory).
2. **Migration 232 + tables.** Verify with `.\scripts\verify-migration-ids.ps1`.
3. **`settings.py` additions** — three new fields + env loaders. **Splice pattern, then `wc -l + ast.parse`.**
4. **`gates.py` adaptation** — table-name dispatch on `execution_mode`. **Splice pattern.**
5. **`calibration.py` parameterization** — make `_fetch_bucket_rows` accept a table name.
6. **`decay_miner.py` writer** — write to `fast_signal_decay_maker_filled` on observed maker outcomes.
7. **`executor.py` maker-only path** — HIGH-RISK file. Splice pattern. Verify with grep for known landmarks (`def execute_paper_fill`, `class ExecContext`) post-edit.
8. **Status endpoint extension.**
9. **Tests** — both new test files. Run helper-level only; defer DB-bound per established pattern.
10. **One commit per logical step.** Don't bundle.
11. **CC report.**

## Operator-side after CC ships (combined with rotator deploy)

1. `git pull` on the operator's box.
2. **Truncation scan** (verify CC's commits are intact in working copy):
   ```powershell
   python -c "import subprocess,ast,os; mod=subprocess.check_output(['git','diff','--name-only','HEAD','--','*.py']).decode().strip().split('\n'); [print(f'TRUNCATED {f}') for f in mod if f and os.path.exists(f) and (lambda h,d: d.count(chr(10))<h.count(chr(10))*0.95)(subprocess.check_output(['git','show',f'HEAD:{f}']).decode('utf-8','replace'),open(f,encoding='utf-8',errors='replace').read())]"
   ```
3. If anything prints, restore via `git checkout HEAD -- <file>` before deploying.
4. `docker compose up -d --force-recreate chili scheduler-worker fast-data-worker`.
5. **Trigger rotator manually** (this is the rotator-fix verification step from the prior brief — do this AFTER pulling the maker-only commit too):
   ```powershell
   docker exec chili-home-copilot-scheduler-worker-1 python -c "from app.services.trading_scheduler import _run_fast_path_universe_rotator_job; _run_fast_path_universe_rotator_job(); print('done')"
   ```
   Should take ~140s. Verify rows: should see `[('shadow', 25)]`.
6. After 24h+ of decay rows accumulate on the new shadow pairs, **then** consider flipping `CHILI_FAST_PATH_EXECUTION_MODE=maker_only`. Not before.
7. After 48h+, evaluate `fast_signal_decay_maker_filled.fill_rate` per pair; pairs below 25% get dropped.

## Rollback plan

`git revert` the commit. Migration 232 is purely additive. Setting
`CHILI_FAST_PATH_EXECUTION_MODE=taker` (the default) restores prior behavior.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Operator's actual Coinbase volume tier.** Both today's `cost_aware_taker_fee_bps=60` and this brief's `cost_aware_maker_fee_bps=40` assume tier 1. Surface in CC report so Cowork can ask the operator for their actual tier before live activation.
2. **`fetch_book_fn` injection seam in `run_rotation_pass`** is documented as cosmetic in the rotator-fix CC report. If any maker-only test needs to mock book behavior at the rotator level, the seam is there. Otherwise no action.
3. **Three-call rate budget.** Rotator already does 3 calls/pair × 394 pairs at 0.12s = 141s. Maker-only doesn't add Coinbase REST calls (it adds broker calls), so the rate budget is unaffected. Surface only if observed Coinbase rate-limit behavior tightens.

## Push & deploy

One commit per logical step. After push, the operator runs the deploy
sequence above. The rotator's shadow window starts ticking from the moment
shadow rows appear; the first decay rows on new pairs land within minutes
of the WS subscription kicking in.
