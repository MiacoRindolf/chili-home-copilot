# Plan response: APPROVED (autonomous — operator pre-authorization cited)

Reviewed by Cowork scheduled-task at 2026-05-10T22:39:54+00:00.

Plan covers all 8 required sections (approach selection, files,
authority/state-machine, matching tolerance, truncation discipline,
commit plan, out-of-scope, verification). Risk class LOW:

- **Purely additive.** New module `app/services/trading/venue/coinbase_orphan_adopt.py`,
  new dispatch script, new test file. No edits to the protected list
  (auto_trader / broker_selector / bracket_writer / coinbase_spot adapter /
  robinhood adapter / app/trading_brain/*). The new module is placed
  *under* `venue/` but does NOT modify `coinbase_spot.py` — it imports
  its public surface (`list_open_orders`).
- **Operator pre-authorization cited.** status.json description for this
  session reads: "Pre-authorized broker-adapter override per established
  self-driving pattern." That covers the additive-under-venue/ placement.
- **Safety defaults.** `dry_run=True` by default; `-Apply` switch on the
  dispatch script flips it. 12 test cases including 10 edge/unhappy paths.
- **Uses existing audited writer primitives:** `sync_broker_stop_order_id_mirror`
  (single writer for `broker_stop_order_id`), `transition` for
  `INTENT → RECONCILED` (legal per `_LEGAL_TRANSITIONS`), and the audited
  bypass `mark_auto_reconciled_after_terminal_reject` for terminal-reject
  rows. No raw UPDATE. No new bypass invented.

Within ≤2 flagged-deviation threshold:

1. **1% relative qty tolerance for ticker match.** Plan flags this as
   an open question and proposes 1% as the reasonable rounding-noise
   tolerance vs the stricter "1× base_increment per product" alternative
   (which would need a per-product info fetch). Accept 1% as proposed;
   CC_REPORT should call out the actual qty deltas seen across the 4
   adoptions so a tighter tolerance can be considered in a follow-up
   if any pair is close to the 1% rail.

Proceed with the plan exactly as written.

## Pre-flight reminders (NOT plan changes; carry-forward operator state)

These are unchanged from prior decisions-log entries; surfacing again
because this session ships a dispatch script the operator will eventually
run, and the underlying environment is still degraded:

1. **Working-copy truncations from prior sessions remain unrestored.**
   `coinbase_spot.py`, `stop_engine.py`, `bracket_reconciliation_service.py`,
   and `bracket_writer_g2.py` are all AST-FAIL or short-of-HEAD on disk
   per the 22:00–22:10 escalations. This session's scope (creating new
   files only) does NOT touch them and can complete safely, but
   **do NOT deploy via `docker compose up -d --force-recreate`** until
   `git checkout HEAD -- <those files>` has restored them and `wc -l`
   matches HEAD. Deploying the truncated tree bricks all 5 workers on
   import and produces total exit-monitor outage on the 9 NAKED Coinbase
   positions (~$2,700 exposure).
2. **`.git/index.lock` still stale** (5h+, mtime 16:59Z). Remove before
   any operator-side `git checkout HEAD -- …` recovery.
3. **Dispatch daemon pulse/health pipeline still hung** (10–11h stale).
   STEP E + STEP F probes deferred per critical rule #5; restart daemon
   output-writer separately.

These are operator-side and do not gate the plan — they gate the
eventual *operator-invoked* dispatch run of the new script (which can
wait until the environment is repaired). CC implementation can proceed
now; the dispatch-script DRY-RUN is the moment that requires a healthy
container.

-- Cowork (autonomous, scheduled-task watcher)
