# NEXT_TASK: f-thread-cleanup-2026-05-07

STATUS: DONE

## Goal

Close out the remaining housekeeping items on the long-running strategy thread so the working tree is clean and reviews stop having to carry-forward stale items. Five small subtasks bundled in one brief; each is operator-decidable and locally-scoped:

1. **Decide and ship the `_trade_phantom_close_guard` event listener** in `app/models/trading.py:198-244`. It's been pre-existing-untouched across 11+ CC reports and 5 reviews. Substantive code (catches the option-vs-underlying bug class that produced trade#392's $712 fake P&L); ready to commit; just needs the `NNN` placeholder replaced.

2. **Commit the `.env.example` flag additions.** Lines 263-270 add `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE_DRY_RUN` and `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE` documentation. This is the promotion-evidence audit codex stabilization plan #6 — env-doc, no behavior change.

3. **Backfill EKSO/ELTX P/L scar.** Trade 1815 (EKSO) and 1816 (ELTX) reported $0 P/L due to `emergency_close_all` artifact; actuals are −$38.80 and −$33.00. Two SQL UPDATEs.

4. **Delete 8 orphaned `.commit_msg_*.txt` files.** Pre-2026-05-01 orphaned commit-message preludes (backfill_patch, codex_plan, exit_reason_fix, fix_192, lifecycle_gate, opt_meta_fix, options_fix, variant_gate). The unsuffixed `.commit_msg.txt` itself is also stale (last touched 2026-04-27); delete that too.

5. **Don't touch `docs/AUDITS/*` or `data/ticker_cache/crypto_top.json`.** Operator-tracked audit working-set; out of scope for this cleanup. Leave exactly as found.

After this lands, the working tree carries only CRLF line-ending mount artifacts (which are workspace-mount churn, not real diffs) — no meaningful uncommitted work.

## Why now

The user wants to close this strategy thread. Each item above has been carry-forward across 5+ reviews; none of them can stay in carry-forward indefinitely without inviting accidental commits when an unrelated CC touches the same file. Bundling them in one cleanup pass beats five separate small briefs.

## Scope boundary

**In scope:**
- Edit `app/models/trading.py` — replace `NNN` placeholder with the next fix number (check `git log --oneline | grep -i "FIX [0-9]"` to find the latest; the brief expects something in the FIX 54+ range based on memory's last-known FIX 53). Keep all logic; only the human-readable identifier changes.
- Stage `.env.example` and `app/models/trading.py` as part of the cleanup commit.
- Two SQL UPDATEs for trades 1815 and 1816 (executed via `docker exec` against the live `chili` database).
- Delete the 9 stale `.commit_msg*.txt` files at repo root.

**Out of scope:**
- `docs/AUDITS/*` — operator's working set; do not commit, do not delete.
- `data/ticker_cache/crypto_top.json` — runtime cache; do not touch.
- `brain_worker.log` — runtime artifact; do not touch.
- The 1,720 CRLF-shifted "modified" files visible in `git status --short` — these are workspace-mount line-ending artifacts (CRLF on host, LF in git's view); a proper git config (`core.autocrlf=input` or a `.gitattributes` text=auto) is the durable fix but is its own brief; do not touch in this task.
- New tests for the phantom-close guard. Acceptable as-is; the docstring documents the bug it catches; if it ever fires, the CRITICAL log will give us the trade_id to investigate.

## Path

### Step 1 — Phantom-close guard

In `app/models/trading.py:198-244`, replace the two `NNN` placeholders with a real fix identifier. Approach:

```bash
git log --oneline --all | grep -iE "FIX [0-9]+" | head -10
```

Pick the next-available number. Use `R36` if FIX numbering has been retired (memory's most recent round is R35). Update the docstring's first line and the two log/raise message strings.

Optional improvement (if you want to ship it with this brief): add a one-line comment above the listener naming the structural-constant `_PHANTOM_CLOSE_RATIO_BOUND = 50.0` instead of the inline literal — but only if the operator standing principle of "no magic numbers, structural constants live in one home" feels appropriate here. Keep the threshold value (50.0) unchanged. If you decide to extract the constant, do it as a module-level `_PHANTOM_CLOSE_RATIO_BOUND: float = 50.0` with a brief comment explaining "structural data-feed-trust bound; option premium cannot be 50x underlying spot or vice versa." If you'd rather not, leave the literal.

Verify by running:

```bash
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_trading.py -v --tb=short -x
```

This is a SQLAlchemy `before_update` listener; existing trade-update tests will exercise it. The guard's `try/except ValueError: raise` clause surfaces rejections cleanly; `try/except Exception` swallows internal listener failures so the guard can never crash a commit. If a trade-update test that uses extreme entry/exit values starts failing, that's a real signal — surface to the operator rather than relaxing the threshold.

### Step 2 — `.env.example` cleanup

Already structurally fine — the addition at lines 263-270 documents the promotion-evidence audit flags. No edits needed; just stage and commit alongside Step 1.

### Step 3 — EKSO/ELTX P/L backfill

Run via Docker exec against the live database:

```powershell
# First verify the current state
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -c "SELECT id, ticker, entry_price, exit_price, pnl, exit_reason, status FROM trading_trades WHERE id IN (1815, 1816);"

# If still showing $0 pnl with status='closed' and exit_reason indicating emergency_close_all (or empty exit_price): apply the backfill
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -c "
UPDATE trading_trades
SET exit_price = 10.76,
    pnl = -38.80,
    exit_reason = 'broker_stop_filled_outside_chili'
WHERE id = 1815;

UPDATE trading_trades
SET exit_price = 10.70,
    pnl = -33.00,
    exit_reason = 'broker_stop_filled_outside_chili'
WHERE id = 1816;
"

# Verify after
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -c "SELECT id, ticker, exit_price, pnl, exit_reason FROM trading_trades WHERE id IN (1815, 1816);"
```

The container name may differ — try in order: `chili-home-copilot-postgres-1`, `chili-postgres-1`, `chili_postgres_1`. If the operator already cleaned these rows (pnl no longer 0), skip the UPDATEs and document in the CC report.

**Don't apply this from a feature flag or migration.** It's a one-time data backfill scoped to two specific trade_ids; a migration would imply this is a structural problem, but actually the structural fix already shipped (no auto-callers of `emergency_close_all`, R32 wholesale-empty-positions guard, etc.). The scar is from before those fixes.

### Step 4 — Delete orphaned `.commit_msg*.txt` files

```bash
cd /sessions/hopeful-peaceful-curie/mnt/chili-home-copilot
rm .commit_msg.txt
rm .commit_msg_backfill_patch.txt
rm .commit_msg_codex_plan.txt
rm .commit_msg_exit_reason_fix.txt
rm .commit_msg_fix_192.txt
rm .commit_msg_lifecycle_gate.txt
rm .commit_msg_opt_meta_fix.txt
rm .commit_msg_options_fix.txt
rm .commit_msg_variant_gate.txt
```

Or in PowerShell:

```powershell
Remove-Item .commit_msg.txt, .commit_msg_*.txt
```

These files don't go into git (the unsuffixed one is already in `.gitignore` per its untracked-but-modified status; verify with `cat .gitignore | grep commit_msg`). The deletion just cleans the working tree.

### Step 5 — Commit and push

```bash
git add app/models/trading.py .env.example
git commit -m "chore: ship phantom-close guard + env doc + thread cleanup

- app/models/trading.py: phantom-close guard event listener live (catches
  option-vs-underlying bug class; trade#392 produced $712 fake P&L by
  closing an option premium against the SPY underlying spot).
  Threshold 50x is a structural data-feed-trust bound, not strategy tuning.
- .env.example: document CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE flags.
- EKSO trade 1815, ELTX trade 1816: backfilled exit_price + pnl from
  broker truth (stop fired outside CHILI; emergency_close_all artifact
  was lying \$0).
- Removed 9 stale .commit_msg*.txt files (pre-2026-05-01 orphans).

(f-thread-cleanup-2026-05-07)"
```

Second commit for the CC report + NEXT_TASK STATUS=DONE.

## Constraints / do not touch

- **No new magic numbers.** The 50x ratio bound exists in the guard already; this brief either keeps it inline or relocates it to a single named module-level constant. Either way, no NEW numerical thresholds.
- **No behavior change to `emergency_close_all`.** That path is already not auto-callable; today's fix is data backfill only.
- **No touching `docs/AUDITS/*`.** Operator's working set.
- **No touching `data/ticker_cache/crypto_top.json` or `brain_worker.log`.** Runtime artifacts.
- **No `git add -A` or wildcard add.** Stage exactly the files this brief touches: `app/models/trading.py`, `.env.example`, and any new files in `docs/STRATEGY/CC_REPORTS/` + `docs/STRATEGY/NEXT_TASK.md`. The 1,720 CRLF-shifted "modified" files are workspace-mount churn; do not stage them.
- **PROTOCOL Hard Rules.** Tests use `_test`-suffixed DB. No `git push --force` to main.

## Success criteria

1. **Two commits, both pushed:**
   - `chore: ship phantom-close guard + env doc + thread cleanup (f-thread-cleanup-2026-05-07)`
   - `docs(strategy): f-thread-cleanup-2026-05-07 CC report + mark NEXT_TASK done`
2. **Working-tree state after the cleanup:**
   - `git status --short | grep -v "^.M\| M [a-z]"` shows ONLY the CRLF-mount-artifact `M` entries (no truly-uncommitted real work).
   - The 9 `.commit_msg*.txt` files are gone.
   - `docs/AUDITS/*` is unchanged from before this task.
3. **DB state after the backfill:**
   - `trading_trades.id=1815` has `pnl=-38.80`, `exit_price=10.76`, `exit_reason='broker_stop_filled_outside_chili'`.
   - `trading_trades.id=1816` has `pnl=-33.00`, `exit_price=10.70`, `exit_reason='broker_stop_filled_outside_chili'`.
   - If they were already correct (operator pre-cleaned), the CC report says so and explains the SQL was skipped.
4. **Phantom-close guard active.** Pytest run shows no regressions; if any test exercises a 50x+ trade-update transition (highly unlikely), surface to operator.
5. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-07_f-thread-cleanup-2026-05-07.md` per PROTOCOL format. Include:
   - Per-step status (5 steps).
   - Whether the EKSO/ELTX UPDATEs were applied or skipped (and why).
   - Confirmation that `docs/AUDITS/*` was untouched.
   - Magic-number audit (zero new literals; the 50x is relocated-or-kept-inline as you chose).
   - The git status delta (count of real changes before vs after; CRLF mount-artifacts excluded).

## Rollback plan

- **Code rollback:** `git revert <chore-commit>`. Phantom-close guard goes back to uncommitted-in-working-tree state (where it was for 11+ sessions; no observable regression).
- **DB rollback:** the EKSO/ELTX UPDATEs are atomic; rollback is two reverse UPDATEs setting back to `pnl=0, exit_price=NULL or original, exit_reason=<original>`. Capture the pre-UPDATE state in the CC report so rollback values are documented.
- **`.commit_msg*.txt` recovery:** if any of those orphaned files turn out to have been needed, `git reflog` shows nothing (they were never tracked); operator's local backup or the original task's chat history is the only source. Risk is low — they're orphaned commit-message preludes, not source.

## Open questions for Cowork (surface in CC report)

1. **Phantom-close guard threshold (50x) — should this become per-asset-class?** Today the same threshold protects equity (where 50x is a data feed error) and options (where 50x can mean an underlying-vs-premium mix-up). A future enhancement could derive per-asset-class bounds, but today's structural bound is conservative-enough.
2. **CRLF mount-artifact cleanup.** The 1,720 modified files are noise. A proper `.gitattributes` `* text=auto eol=lf` would normalize line endings; one CC pass to add the attribute file + `git add --renormalize .` cleans the working tree permanently. Out of scope for this brief; surface as `f-gitattributes-crlf-normalize` if the operator wants to reclaim a clean `git status`.
3. **`docs/AUDITS/*` long-term home.** Twelve untracked audit docs going back to 2026-04-28. If they're meant to be operator-only, `.gitignore` could absorb `docs/AUDITS/`. If they're meant to be shared, `git add docs/AUDITS/` ships them. Operator decision; out of scope for this brief.

## Forward pointer

After this lands, the strategy thread is closed: no carry-forward items, no live-money bug class known-broken, Phase 1 of the position-identity refactor still soaking through 2026-05-11 (passive). When the operator is ready to queue Phase 2, that's a fresh thread.

## Verification commands (for the executor + the operator)

```powershell
# After the cleanup commit
git log --oneline -2
# Expect: chore commit + docs commit at HEAD.

# Working tree real-uncommitted-work count
git status --short | grep -E "^A|^D|^\?\?|^M" | wc -l
# Expect: 0 or very small (only docs/AUDITS/* + data/ticker_cache + brain_worker.log).

# Phantom-close guard installed?
grep -n "phantom_close_guard\|PHANTOM_CLOSE_RATIO" app/models/trading.py
# Expect: at least 2 hits.

# DB scar resolved
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -c "SELECT id, ticker, pnl, exit_reason FROM trading_trades WHERE id IN (1815, 1816);"
# Expect: pnl=-38.80 and -33.00 respectively.

# Commit-message orphans gone
ls .commit_msg*.txt 2>/dev/null
# Expect: no files.
```
