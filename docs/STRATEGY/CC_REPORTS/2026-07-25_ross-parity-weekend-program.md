# CC Report — Ross-parity weekend profitability program (2026-07-24 → 07-25)

**Status: COMPLETE — 9 PRs merged (#928–#936), one day ahead of the approved Sat/Sun plan.**
Operator directives: "check Ross's course playbook, make sure lahat ng styles covered talaga" → "gawa ka ng ross parity profitability plan" → approved full 8-PR scope with auto-merge-on-green.

## 1. The coverage audit (07-24, the driver for everything)

Method: all 14 Warrior Trading courses were already mirrored locally (`D:\CHILI-Docker\chili-data\ross_courses\`, 357 transcripts + frames) — no site access needed. 3 extraction agents built (a) the 16-setup entry taxonomy with mechanical triggers/stops/targets, (b) the selection/tape/risk framework with Ross's actual numbers, (c) the CHILI code inventory. A 41-agent verification workflow (`wf_986c0eac`) then read every claimed detector against the Ross spec — mechanism-level, not name-matching.

**Verdict: breadth REAL, depth PARTIAL, selection half-blind.** 4 FULL / 24 PARTIAL / 9 NAME-ONLY / 3 ABSENT. Every Ross long setup had an implementation with correct trigger geometry + structural stop, but the PARTIAL divergences were exactly the confirmation/quality filters that carry Ross's win rate. Dangerous false-positives called out: `bounce_curl` ≠ the Curl (single-bar filter, not the trendline setup); the borrow/squeeze subsystem is dead code (`get_short_mechanics` → `{}`, `squeeze_fuel_rank_pct` never written); catalyst typing was sign-inverted historically; the AS101 "thin book = tradeable" edge is polarity-inverted vs our book-pressure gates (flagged for verification, not yet acted on).

Binding-verified flag reality (not defaults): `tape_hold` was already ON (refuting the audit's scariest sub-claim — challenge-the-diagnosis paid off); `sub_vwap_trap`/`inverse_head_shoulders`/`bottom_reversal` + several quality gates were genuinely dark.

## 2. Friday night: dark-flag promotion (#928) + the replay instrument

- Built `scripts/replay_ab_dark_flags.py` from the proven `replay_window.py` harness (real `tick_live_session` over recorded NBBO grid + full-density tape, `chili_replay2_test` sink). Three sink landmines found and fixed en route: fresh sink needs exactly ONE brain node (the evolution-trace FK) — seeding all 120 prod nodes arms a fail-closed hub freshness gate; `seed_replay_session` leaks an active `replay_v3_*` variant per run (accumulation → `viability_missing` on every run after the first) → per-run variant-board reset; stale G4 settings attrs crash on current Settings.
- 9-run matrix on CLRO 07-02 / QTTB 07-13 / PLSM 07-13: **sub_vwap_trap byte-identical to base in all 3 windows** (never mis-fires when the geometry is absent); **bail_on_no_confirmation net +$18.74** (QTTB +26.92 cutting fading losers; CLRO −8.18 grind churn; PLSM 0) with zero interaction (both == bail). Deterministic: base re-run reproduced to the cent.
- **#928 merged**: both flags default-ON. With WAVE-4's #926, all four previously-dark Ross setups are now live by default.

## 3. Saturday: the 8-PR program (all merged)

| PR | Lever | Proof highlight |
|---|---|---|
| #930 PR-0 | `FLAGS_JSON` driver hook + baseline re-bank on post-#928 main | dual-proof: forcing the #928 flags off reproduced the pre-#928 baseline to the cent |
| #929 PR-1 | Catalyst re-sign: collision-safe SEC dilution forms (`form s-1`/`424b`/…) → WEAK; confirmed buyout-target phrasings → new ARB-FLAT class (negative tilt + live-ineligible, precedence over strong; acquirer-side stays strong) | 51 catalyst tests green; plumbing was already live so the change is narrow |
| #931 PR-2 | `_tick_break_tape_ok` on the naked ORB/ABCD tick paths (tape confirm + ABCD thrust buffers; tape-fail falls through to the bar path; `pattern_tape_gate` rollback fail-OPENs — independent rollback domains) | parity 3/3 to the cent; QTTB −2.90 diagnosed as size-quantization noise (fills identical) |
| #933 PR-3 | flush_dip relative-volume gate (reuses `pullback_volume_spike_multiple`, fail-OPEN on thin data) | parity ✓; **QTTB +4.56** — the gate catching a fade-day flush-dip fire |
| #934 PR-4 | Ross stop alignment ×3: inv-H&S → right-shoulder low; wick_reclaim → reclaim-bar low (max() degeneracy guard); vwap_reclaim → loss-of-VWAP. One flag, `stop_model` provenance | parity 3/3; treatments byte-neutral (no such fires on the tapes) → 92 pytest carry the proof; sizing hazard bounded by `structural_or_vol_floored_atr_pct` (takes the FURTHER of structural vs vol floor — verified) |
| #935 PR-5 | Latent-bug fix: `orb_break*`/`inverse_head_shoulders_break*` added to the structural-reason set via a flag-gated accessor read by ALL THREE consumers (their emitted stops were silently dropped to ATR before) | parity ✓; source-level assert that no raw membership check remains |
| #936 PR-6 | **GAP-B fresh-ignition re-entry exemption** at the stopout-cap terminalization edge (bounded, default 1/session; pure helper beside `reentry_after_stop_allowed`; grant only recycles — re-entry still passes G4 escalated confirmation) | see §4 |
| #932 PR-7 | Float gate on the final ranked universe subset (Ross scans float FIRST): bounded lookups (2× max_universe, never the uncapped ceiling), fail-OPEN, reference = the ONE viability 20M ceiling | 21 tests; Ross profile keeps the field None so nbbo_tape's dormant consumer stays inert |

**Universal proof recipe held throughout**: every behavior PR ran a FLAGS_JSON parity arm (lever kill-switch OFF) that had to reproduce the then-current main baseline **to the cent** before merging — 10+ parity checks, all exact. Kill-switches are therefore proven restores, not hopes.

## 4. GAP-B: reject → tighten → re-validate (evolve-not-devolve in full)

v1 (OR-predicate: instant tape-accel OR running-up burst) proved the mechanism exactly — iso-treat arms granted precisely +1 entry (the bound) deterministically on both windows — but the granted cycles measured **−$3.90 aggregate**: the instant-accel leg fired on momentary lifts during fade/chop windows (noise). Per the plan's pre-declared reject-response, the predicate tightened to **AND** (buyers lifting now AND a sustained ≥3%/5min burst — the JEM-class re-ignition signature). v2 arms: **byte-parity with controls across all three runs** (zero noise grants; no interference under defaults). The upside case cannot exist on recorded loser-class tapes; Monday soak explicitly owns first-activation observation.

## 5. Monday live-soak checklist (what offline cannot prove)

1. `live_reentry_cap_ignition_exempt` first activation + per-grant churn watch
2. First live fires of the three tightened stops + sizing observation
3. `flush_dip_low_volume` / `tape_reason` counters on live premarket density
4. ORB/inv-H&S structural-stop provenance events
5. Float-gate latency + exclusion list in the live selection loop
6. Arb-flat headline sampling; the deferred merger/acquisition ambiguity study
7. L2 defensive vetoes (`entry_l2_veto` + exit OFI hidden-seller) — the remaining audit lever; needs a depth-liveness gate, live-soak only
8. Admission-seam cooldown watch (the deferred GAP-B second door)

## 6. Method learnings (worth keeping)

- **Chain baselines**: each PR's parity arm compares against the previous PR's treatment (main at its merge point). Every link matched to the cent — the harness is deterministic enough to use cents as the parity unit.
- **Config-anchor merge conflicts**: flags inserted at the same anchor conflict under merge-as-you-go; segment scripts now carry a `MERGE CONFLICT ABORT` guard (the first PR-3 chain ran with conflict markers → SyntaxError everywhere; caught in seconds).
- **Def-time settings binding**: helpers with `settings: Any = settings` defaults dodge test patches — call sites must pass `settings=settings` explicitly.
- **The "seeded symbol is always leader" replay assumption is false at the cap edge** — which is exactly why the PR-6 defaults arm surfaced real behavior instead of hiding it.
- Replay sink hygiene: 1-node brain seed + per-run variant-board reset are now baked into the driver.

## 7. State for Monday

- The captured-paper activation lane remains HEAD-pinned and untouched by this program; the new levers ride the next build of the trading images.
- Memory updated: `project_ross_parity_program_0726`, `project_ross_coverage_audit_0724`, MEMORY.md pointers.
- Remaining ranked-audit backlog (not this weekend's scope): L2 defensive vetoes (live-soak), catalyst reliability learner (stubbed multiplier), borrow/Ortex ingestion, ADF print tagging, stock-type risk buckets, short-side family (deferred until long conversion is proven and a short venue exists).
