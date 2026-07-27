# 2026-07-26 — NaN volume fails CLOSED on the conviction fires (+ the stamp sweep 6c35c23 missed)

Branch `ross-parity/vol-nan-fail-closed` (isolated worktree off `ross-parity/golden-library`
@895e008 — wt-rossparity was mid-batch and its baseline runner spawns a subprocess per window
from that tree, so editing it live would have contaminated the golden scorecard).

## The decision

6c35c23 made the vol_ratio stamps JSONB-safe but deliberately preserved the legacy gate
semantics: a NaN vol_ratio **passes** the volume gates because `NaN < mult` is False. Decision
implemented here:

- **bull_flag + vwap_reclaim volume gates FAIL-CLOSED on NaN** (new reasons
  `bull_flag_volume_unknown` / `vwap_reclaim_volume_unknown`). Their design intent is
  "volume spike = conviction" — a ratio that cannot be measured cannot prove conviction, and
  the family's own `_avg<=0` fallback already blocks (yields 0.0). NaN passing was an IEEE
  comparison accident, witnessed live-shaped on CLRO 2026-07-07 where a NaN rode through the
  bull_flag volume gate into the tape gate.
- **flush_dip stays FAIL-OPEN** — its volume gate's fail-open on uncomputable ratios is a
  documented contract (Ross-parity L1b, PR-3), not an accident. Untouched.
- Default-ON, kill-switch `CHILI_MOMENTUM_VOL_NAN_FAIL_CLOSED_ENABLED=0` restores the legacy
  NaN pass-through exactly (proven below).

## What the task surfaced: 6c35c23 fixed 3 of 14 stamp sites

The in-flight golden baseline batch **re-crashed CLRO|2026-07-07 at the fixed sha** — same
`InvalidTextRepresentation: Token "NaN"`, same sim instant (16:39:22), and the crash JSON's
key order (`"tape_reason": "tape_hold_error", "vol_ratio": NaN` — tape stamped *before*
vol_ratio) matches none of the three fixed sites. Sweep found **11 more un-guarded stamps**:
abcd, tight_false_break_reclaim, double_bottom, inverse_head_shoulders, cup_and_handle, the
pullback-break trigger-bar stamp, hod_break, blue_sky_break, orb, red_to_green,
ma_vwap_pullback. (`ignition_loop`'s rvol stamp is already NaN-proof — `_rv > 0` is False for
NaN.)

## Commits

- **4965a59** `fix: complete the NaN-safe vol_ratio stamp sweep — 11 more sites` — STAMP-ONLY,
  every comparison keeps legacy NaN semantics; NaN windows now complete instead of crashing
  the session UPDATE.
- **8cfc0f8** `feat: NaN volume fails CLOSED on the conviction fires` — the lever + config
  flag + 4 mock-fire tests (ON blocks with a JSONB-safe None stamp; OFF preserves the legacy
  fire) at both sites.

## Proof (FSM replay A/B, `replay_ab_dark_flags.py`, GOLDEN=1, isolated sinks)

Sinks: `chili_replay3_test` / `chili_replay4_test` (clones of `chili_replay2_test` minus tape
data — replay2 was advisory-locked by the in-flight batch). One run per sink at a time.

### W1 — CLRO 2026-07-02 14:00–16:00 (NaN-free window): parity + no-collateral

| arm | code | flags | PnL | fills |
|---|---|---|---|---|
| baseline | pristine 895e008 | defaults | **−11.37** | 4 buys / 4 sells |
| parity | 4965a59+8cfc0f8 | lever **OFF** (FLAGS_JSON) | **−11.37** | identical 8 fills, same prices/sizes |
| lever ON | 4965a59+8cfc0f8 | defaults (ON) | **−11.37** | identical 8 fills, same prices/sizes |

All three arms byte-identical to the cent (BUY 70@3.70 / 51@3.79 / 74@3.75 / 10@3.75, SELL
70@3.72 / 51@3.67 / 74@3.67 / 10@3.68). The baseline also ties the batch's own
CLRO|2026-07-02 record (−11.37) — cross-sink, cross-sha determinism.

### W2 — CLRO 2026-07-07 16:38–19:23 (the NaN witness window): causal measure

Pre-change baseline for this window **does not exist: pre-change code crashes** — reproduced
twice by the golden batch (sha 152ae1b and ≥6c35c23), identical traceback both times.

| arm | code | flags | PnL | fills |
|---|---|---|---|---|
| legacy shape (lever OFF) | 4965a59+8cfc0f8 | lever OFF | **−25.84** | 15 buys / 16 sells, full window |
| lever ON | 4965a59+8cfc0f8 | defaults (ON) | **−25.84** | identical 31 fills, same prices/sizes |

Reading the identical result honestly: ENTRY_DIAG shows **0 `*_volume_unknown` blocks in
either arm** — on this tape no bull_flag/vwap_reclaim fire ever hinged on a NaN ratio. That
matches the crash forensics: the NaN that poisoned the crash-era stamp came from the
abcd/ihs/cup/orb family (the `tape_reason`-before-`vol_ratio` key order), which this lever
deliberately does not govern, and that evaluation was tape-blocked (`tape_hold_error`)
regardless. So the lever's bite is **prospective** — it closes the "conviction fire passes
with unmeasurable volume" hole on the two governed patterns (unit-proven), with **zero
observed collateral** in replay. The window win belongs to the stamp sweep: pre-change =
deterministic crash; post-sweep = completes end-to-end, and the two arms double as a
same-window determinism replication (identical to the cent).

## Tests

`test_momentum_mock_fire_{breakout,pullback,reversal}.py`: **60 passed, 1 failed** — the
failure (`TestMicroPullbackPrimary::test_ideal_micro_pullback_fires`,
`micro_primary_buyers_unconfirmed`) reproduces identically on the **unmodified** tree at
895e008: pre-existing (a newer buyers-confirm gate is unmocked in that test), flagged as a
separate task chip.

## Scope notes / follow-ups

- The other volume-gated families (abcd, ihs, cup, orb, hod, blue_sky, red_to_green,
  double_bottom, ma_vwap, tight_false_break_reclaim) keep the legacy NaN pass-through — this
  lever deliberately covers only the two fires named by the task. Extending fail-closed to
  the rest is a follow-up lever needing its own A/B.
- A writer-boundary hardening (NaN-rejecting risk_snapshot_json serializer) would make the
  whole class impossible; per-stamp sanitization is the established, replay-proven pattern,
  so that stays a follow-up.
- This branch carries the 5 unpushed golden-library base commits (152ae1b..895e008) — noted
  in the PR body.
