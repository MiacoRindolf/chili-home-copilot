# CC_REPORT: cup-and-handle-chase-guard-hardening

Cross-ref: [`docs/STRATEGY/CC_REPORTS/2026-06-26_warrior-courses-reaudit.md`](2026-06-26_warrior-courses-reaudit.md)
(ENTRIES pillar — cup-and-handle was the "implemented but flag default-OFF" item the
gatekeeper explicitly blocked from flipping ON in Cluster 2/b349aed pending this hardening).

## What shipped

- **Commit `10c7018`** (branch `chili/momentum-cup-handle-harden`, off `b349aed` — the live
  momentum base): *"momentum: harden cup-and-handle ENTRY with the standard chase-guards
  (so it can ship ON)"*.
- **Files touched: 2** — `app/services/trading/momentum_neural/entry_gates.py`
  (`cup_and_handle_confirmation`), `app/config.py` (cup flag ANTI-CHASE description only).
- **Migrations added: 0.**
- **Image built: `chili-app:main-clean-10c7018`** (from the isolated worktree; pip layer
  cached from b349aed, requirements unchanged). Build log
  `D:/CHILI-Docker/_build_10c7018.log`, exit 0.
- **Deployed:** `chili-clean-recovery-scheduler` recreated on `chili-app:main-clean-10c7018`
  reusing `_sched_env_10c7018.env` (copied from `_sched_env_b349aed.env`).

### The change

`cup_and_handle_confirmation` already had the structural guards (ATR-filtered double-top
pivots, equal-highs band, shallow-handle cap, `_collapse_cap`, 9-EMA hold, L2 hidden-seller
veto, volume-surge on the break) and a structural stop (the handle low). It was missing the
three chase-guards every *other* live breakout trigger (`wedge_break_entry`,
`absorption_snap_entry`, `hod_break_confirmation`) carries. Added, mirroring `wedge` exactly:

1. **TAPE REQUIRED + FAIL-CLOSED** (`tape_confirms_hold`) — the LAST gate before BOTH fire
   paths (tick-break AND completed-bar). Buyers must be lifting the ask THIS tick; a
   disabled flag / no-tape / thin / stale / crypto / error ⇒ NO fire. Reasons:
   `cup_and_handle_tape_unconfirmed`.
2. **NOT PARABOLIC** (`_hod_extension_ok` vs the 9-EMA AND VWAP) — reject a vertical run INTO
   the rim as a blow-off. Reason: `cup_and_handle_extended`.
3. **NOT BACKSIDE / NOT BELOW-VWAP** (`_detect_back_side` + `front_side_state`, fail-CLOSED
   on a thin frame). Reasons: `cup_and_handle_back_side`, `cup_and_handle_backside_lifecycle`.

Also expanded the `compute_all_from_df(needed=...)` set to include `ema_20 / macd /
macd_signal / vwap`. **This was load-bearing, not cosmetic:** `compute_all_from_df` only
computes what is requested, so without it `_detect_back_side` would have received empty
ema_20/macd arrays and silently no-op'd (a chase hole). `wedge_break_entry` already requests
them; `hod_break_confirmation` does NOT (a latent partial-degrade in that older trigger —
flagged below, not fixed here).

Kill-switch `chili_momentum_cup_and_handle_entry_enabled` default OFF ⇒ the function returns
`cup_and_handle_disabled` before any compute ⇒ **byte-identical** when off.

## Verification

- **Adversarial chase-safety: 19/19 PASS** (`conda run -n chili-env python _adv_verify_cup.py`,
  scratch, not committed):
  - flag-OFF byte-identical (returns disabled before touching the df — passing `None` df
    raises nothing);
  - BOTH `return True` fire paths are textually dominated by the `tape_confirms_hold` gate;
  - a structurally-valid BREAKING cup (forced double-top pivots + controlled indicator
    arrays) FIRES only when front-side + not-extended + tape-confirmed, and rejects with the
    PRECISE reason on each adversarial flip: ema9<ema20 ⇒ `back_side`; MACD cross-below ⇒
    `back_side`; `front_side_state.is_backside`/below-VWAP ⇒ `backside_lifecycle`; rim far
    above 9-EMA&VWAP (front-side) ⇒ `extended`; tape unconfirmed ⇒ `tape_unconfirmed` on
    BOTH the tick-break and completed-bar paths;
  - guard-helper parity: cup uses the identical 4 helpers as the gatekeeper-approved
    `wedge_break_entry`.
- **Image smoke (inside the new container):** 3 hardened guard reasons present in
  `entry_gates.py`; `import app...entry_gates` OK; 2 fire paths, both after the tape gate.
- **Deploy health:** scheduler recreated cleanly on `10c7018`, no Traceback/ImportError/
  NameError at startup (the scheduler→live_runner→entry_gates import chain loaded — proves
  the changed file is valid in the container). No held live momentum positions at deploy
  time (equity lane closed for the weekend), so the restart interrupted nothing.

## Surprises / deviations

1. **The flag was ALREADY ON in the live env, with the UNSAFE code** —
   `CHILI_MOMENTUM_CUP_AND_HANDLE_ENTRY_ENABLED=1` (and its hard dependency
   `CHILI_MOMENTUM_TAPE_HOLD_ENTRY_ENABLED=1`) were already set in `_sched_env_b349aed.env`,
   so the running b349aed scheduler was firing the chase-UNSAFE cup. This made the deploy a
   live-exposure fix, not a dormant feature flip. **No env edit was needed to "enable" it —
   only the hardened code had to land.** (Avoided touching the shared sched env file =
   avoided the duplicate-key hazard.)
2. **Confirmed live exposure (the killer evidence):** in the last ~24h the old unsafe cup
   fired **3 real live entries, 0 winners** — VIA `-$3.05` (−55bps, bailout), PYXS `-$2.90`
   (−108bps, trail_stop), TPCS `-$6.12` (−197bps, stop). The worst (TPCS, sess 9431, 23:48)
   fired on `cup_and_handle_break_tick_ok` into an L2 `imbalance5 = -0.95` (ask5=12500 vs
   bid5=325 — heavily seller-stacked) with `structural_stop=null` and no tape confirmation —
   exactly the chase the new tape/extension guards block. (Most cup candidates, 59/62, were
   already caught by the call-site sticky-backside-bench; these 3 slipped that and entered.)
3. **Parallel agent in the shared etfrank worktree.** A concurrent session was implementing
   the EXITS cluster (GAP1 affirmative bail-on-no-confirmation, GAP2 instant bid-below-fill
   cut, GAP3 regime-conditioned hold-time) — uncommitted edits to `live_runner.py`,
   `paper_execution.py`, `config.py`. To avoid entangling their in-flight work into my
   commit/image, I branched a **fresh isolated worktree off `b349aed`**, applied only my
   2-file change there, and built from it. The deployed image is therefore `b349aed` + cup
   hardening ONLY — it does NOT include (and does not regress) the parallel EXITS work, which
   was never in the live b349aed image either.

## Deferred

- **Monday premarket live soak.** The equity lane is closed (weekend) + crypto live-arm is
  OFF, so the hardened cup cannot be exercised live until ~04:00 ET Monday. Watch the first
  live cup candidate: expect `cup_and_handle_tape_unconfirmed` / `_back_side` / `_extended`
  rejections on chase-y setups and `cup_and_handle_break(_tick_ok)` only on tape-confirmed,
  front-side, non-extended rims. Re-pull the VIA/PYXS/TPCS-class books and confirm 0 chase
  fires.
- **`hod_break_confirmation` backside-check degrade.** It reads `ema_20/macd/macd_signal`
  for `_detect_back_side` but does NOT request them from `compute_all_from_df`, so its
  backside MACD/ema20 leg silently no-ops (only the `front_side_state` leg is live). Same
  one-line fix as here. Flagged for a separate small commit — out of scope for this task.
- **Committed pytest.** Kept the adversarial verification as a report artifact (matching how
  the Cluster-2 wedge/absorption triggers were verified ad-hoc, "4/4", without committed
  tests). A committed regression suite for the whole Batch-C/Cluster-2 entry-gate family
  would be good hygiene — Cowork's call.

## Open questions for Cowork

1. **Branch/merge path.** This is on `chili/momentum-cup-handle-harden` off `b349aed`, NOT on
   the etfrank branch (`chili/momentum-defensive-veto-bundle`, which the parallel agent has
   checked out). Do you want me to (a) push it + open a PR to main, or (b) hand the commit to
   the etfrank lineage so all four re-audit clusters land together? The live image is already
   deployed regardless of the git decision.
2. The 3 unsafe cup entries (`-$12.07`) and the call-site backside-bench catching 59/62
   suggests the call-site bench is doing most of the work; the new in-trigger guards are the
   belt to its suspenders. Worth confirming over the Monday soak whether cup adds net edge at
   all once properly guarded, or whether it should stay ON only as a completeness item.
