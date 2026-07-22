# 2026-07-21 — Ross-parity remaining-gap study (decision-grade)

6-agent evidence study (docs + live DB + Ross video evidence + code) of where
CHILI still fails to make Ross Cameron's profitable trades, after the 07-09 exit
repair, <1s event arming, and with the captured-paper lane (first-dip candidate
mode) about to activate.

## Two reframes that change how to read everything

1. **The only live-outcome data (`chili` DB) ends 2026-06-24 — ~2 weeks BEFORE
   the 07-09 exit repair and the whole 07-12→07-18 batch.** So the alarming live
   numbers (30% WR, inverted median R, 59% reconciler-driven exits, 2,151
   stop-placement rejections) describe PRE-FIX CHILI. Everything 07-09→07-18 is
   REPLAY evidence on current code. **We have NO live confirmation the shipped
   fixes moved the live distribution — closing that replay→live loop is itself a
   first-order task, and the paper lane is that instrument.**
2. **The paper-lane activation already addresses the two biggest STRATEGY gaps** —
   entry shape (dip-buying = first-dip candidate mode) and exit discipline
   (9-EMA/partials = the 07-09 chain). Those are "validate in paper," NOT open
   gaps. What remains is an execution/conversion/re-entry/sizing cluster the
   activation does NOT touch.

## The remaining money is in CONVERSION, not signal

Selection is solved (7/7 correct names), loss-shape avoidance is solved, and
entry-shape + exit are being validated by the activation. Ranked remaining gaps:

| Rank | Gap | Category | Dollar evidence | Fix posture |
|------|-----|----------|-----------------|-------------|
| **1** | **Arm→fill conversion + exec-worker liveness** — CHILI's tape SEES the movers but FILLS nothing | execution/infra | VRAX 07-09 tape rode $6→13.19, all 5 sessions rotted `live_arm_pending`→`cancelled` (worker "unhealthy" 42h); CLRO 07-07 →16.50; CETX $8,917 — all tape-present, fills-absent | **SHIP-LIVE (verification, not risky code).** Gates everything. |
| **2** | **Re-entry lockout has no ignition/leader bypass** — the winner-killer | re-entry | VRAX 07-09 ~212 blocks while 8→13.19; VEEE 601; VIVS 07-18 8× `g4_reentry_escalation_blocked` | **VALIDATE-FIRST.** Blanket unlocks regressed twice (#914, chase-cap v2 JEM −$2,676). Needs surgical ignition-conditioned fix. |
| **3** | **Stop-placement / exit control** — CHILI cedes closing to the reconciler, not decisions | exit/infra | 2,151 `g2_place_missing_stop_rejected`; 59% reconciler-driven exits (PRE-fix; may be closed by #900) | **VERIFY-FIRST (cheap, diagnostic).** |
| **4** | **Sizing inversion + intraday death-spiral** — full size on weak probes, tiny on the #1 name; early bailouts pin `exp_mult=0.5` and lock the afternoon | sizing | CLRO 07-07 #1 name 26sh vs TTRX 1,026sh; afternoon budget-blocked while CLRO→16.50 | **VALIDATE-FIRST** (cheap bug-shaped exclusion at `risk_policy.py:1601-1614`; meta-label half data-gated). |
| **5** | **News-catalyst selection pillar + real sub-$1 gate** | selection | SILO enrollment-grade vs VRAX supply-agreement news; catalyst grade discarded at `trading_scheduler.py:3088` | **SHIP-LIVE** (`news_catalyst_weight_enabled`+`news_pr_cadence_enabled`, TURN_ON_SAFE). |

## Corrections to prior belief (important)

- **The price floor is `$1.00`, not $2** (`universe.py:111`). VIVS 07-15 at $1.91
  was **NOT** price-blocked — it was the co-located `min_dollar_volume=1_000_000`
  / `min_change_pct=5.0` / viability `≥0.60` gate. Chase THAT, not a price floor.
- The re-entry lockout is a **contested** gap: 07-12 hermetic re-measure found
  terminalize→re-arm is the correct channel and every blanket gate-unlock was
  net-negative. The residual is the escalation path's post-stop structural-trigger
  demand refusing an igniting leader — surgical, not a flag flip.
- `ask_thins_dip_entry` + `tight_false_break_reclaim` hard-require the L2/OFI
  depth bridge (unavailable) — flipping their flags only adds dark flags. Do NOT.

## Top-3 next actions (regression-safe)

1. **GAP A — prove the live conversion path first (SHIP-LIVE, verification).**
   Run `verify_ross_event_admission_runtime.py` in-container; confirm a live
   pg_notify → `admit_ross_event` round-trip; deploy the ignition detector
   `68b8357` (host-bridge restart + `chili_iqfeed_l1_authoritative_bridge_build`
   re-stamp; the LISTEN consumer is "UNDEPLOYED"). On the first paper session,
   assert every armed name reaches an ENTRY ATTEMPT, not `live_arm_pending`. This
   is the GATE on trusting any other metric from the run.
2. **GAP B — ignition-conditioned re-entry bypass (VALIDATE-FIRST).** Scope
   `entry_flow_veto_explosive_exempt` (`entry_gates.py:2287`) to fire ONLY for a
   high-RVOL leader with fresh upward OFI/tape, via terminalize→re-arm (not by
   lowering the 300s/120min cooldowns). FSM-replay VRAX 07-09 / VEEE 07-13 /
   VIVS 07-18 (leaders) AND JEM + a CLRO-chop day (the cases that killed prior
   attempts); acceptance = leaders' tail captured AND chop days no worse.
3. **GAP D — confirm stop-placement reliability (VERIFY-FIRST, data-first).**
   Query current `g2_place_missing_stop_rejected` rate + reconciler-vs-decision
   exit split on POST-07-09 fills. If reconciler exits dropped from 59%→~0, close
   it. If not, isolate the reject cause before touching code (Hard Rule 3).

## Ship-live vs validate-first (the operator's regression fear)

- **SHIP-LIVE (safe default-on):** GAP A verification; GAP D diagnostic;
  halt-resume directional refinement (`false_halt_avoid_enabled` +
  `halt_resumption_direction_enabled`, TURN_ON_SAFE); news-catalyst pillar; the
  06-27 ENV-DRIFT restore bundle (excl. measured_move_exit + order_burst guard).
- **VALIDATE-FIRST (replay proof required):** GAP B re-entry bypass; first-dip
  candidate → `promoted` (needs OOS receipt — earn it in paper, don't force);
  the dip-family A/B triggers (mid-A/B, do not flip); GAP C death-spiral
  exclusion; meta-label sizing (data-gated).

## One-line strategic read
The remaining money is conversion, not signal: (1) make the live worker actually
FILL what the tape arms, (2) let the system re-enter an igniting leader it just
stopped out of — proven surgically, not flipped — and (3) confirm CHILI, not the
reconciler, closes its positions. Rank-1 (liveness) is prerequisite: until it
passes, no other metric from the paper run is trustworthy.

_Full 6-agent evidence in workflow wf_7ad80095; source docs: 2026-07-16
ross-5day-replay-scorecard, 2026-07-18 weekend-entry-gate-levers, 2026-07-16
dark-flag-audit, DESIGN/ROSS_CAPTURE_PARITY.md._
