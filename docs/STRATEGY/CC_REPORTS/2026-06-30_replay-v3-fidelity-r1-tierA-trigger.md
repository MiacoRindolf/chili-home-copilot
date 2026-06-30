# Replay v3 fidelity — R1 history table + Tier-A feasibility + the 13:08 trigger root cause

**Date:** 2026-06-30
**Author:** CHILI Code
**Scope:** Complete the Replay v3 fidelity work in three parts — (1) the R1 future-perfect
eligibility table, (2) try Tier-A for the 06-29 UPC past replay, (3) investigate the
trigger-at-13:08 (artifact vs real gate). Each part verified; the green parts committed.
**Related:** `docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md` (R1, §2.3, P3); the prior A/B report
`docs/STRATEGY/CC_REPORTS/2026-06-30_replay-v3-upc-0629-grace-ab.md`;
`scripts/replay_v3_upc_0629.py`.

---

## PART 1 — R1 FUTURE perfect fidelity: `momentum_viability_history` append-table ✅ SHIPPED

**The gap (design R1):** `MomentumSymbolViability.live_eligible` is a single mutable snapshot
column with a UNIQUE(symbol, variant_id) constraint — it has NO history. So the eligibility
TIME-SERIES that produced the UPC TOCTOU flicker is not directly recorded; every replay has to
RECONSTRUCT it (Tier B/C). Going forward we now record it.

**What shipped:**
- **Table** `momentum_viability_history` (migration **`311_momentum_viability_history`**, after
  the prior last id 310). Append-only, naive-UTC. Columns:
  `(id, symbol, variant_id, scope, observed_at, live_eligible, paper_eligible, freshness_ts,
  viability_score, rvol, change_pct, spread_bps, blocked_reason, correlation_id, source_node_id,
  created_at)` — the boolean PLUS the few scorer inputs needed to recompute / audit the verdict.
  Indexed on `(symbol, observed_at DESC)`, `(symbol, variant_id, observed_at DESC)`, and
  `(observed_at, id)`. Idempotent (CREATE … IF NOT EXISTS throughout). Registered in `MIGRATIONS`;
  the id-uniqueness assert passes (301 migrations, last = `311_momentum_viability_history`).
- **Write-path:** `persistence.persist_neural_momentum_tick` — the single viability write
  chokepoint — buffers one history row per persisted name and bulk-inserts them after the upsert
  loop. **Flag-gated** `chili_momentum_viability_history_enabled` (**default ON** — cheap, append-
  only observability; no dark flags). **Fail-open:** the append runs inside a `begin_nested()`
  SAVEPOINT; any error is swallowed and rolled back so it can NEVER block the live viability
  upsert. Flag OFF ⇒ zero history rows, byte-identical to pre-R1.
- **Retention guard:** TTL-pruned in `data_retention.run_retention_policy` via the same batched
  `_prune_operational_time_log` drain the other operational logs use
  (`brain_retention_viability_history_days`, default **30d**). The `(observed_at, id)` leading-time
  index is what enables that drain — so the table cannot balloon.
- **Test** `tests/test_viability_history_append.py` (3 tests, all green): migration idempotent
  (runs twice, no error); a viability update appends a history row with the scorer inputs
  captured + append-only on the next tick; flag-off ⇒ no append. The existing
  `test_momentum_neural_persistence.py` (8 tests) still passes — no regression.

**VERDICT — PART 1: DONE.** Future incidents replay at perfect fidelity (read the exact recorded
`live_eligible` series, no reconstruction). Commit `9ad4ecf`.

---

## PART 2 — Tier-A for the 06-29 UPC PAST replay: **INFEASIBLE (no fakery), Tier-B is most faithful**

**Tier A** = recompute `live_eligible` AS-OF-t by re-running the viability scorer on UPC's
recorded inputs at the flicker instant (13:08:31). I added `--tier={auto,A,B}` to
`scripts/replay_v3_upc_0629.py` plus a read-only **Tier-A feasibility probe** against `chili`.

**The probe found Tier A is INFEASIBLE for UPC 06-29** — the as-of-t scorer inputs are simply
not recorded:

| Check (all must pass for Tier A) | Result |
|---|---|
| a `momentum_symbol_viability` snapshot with `freshness_ts` in the 06-29 13:00–14:00 entry window | **0** (the single mutable row was overwritten by later ticks — the exact R1 gap) |
| the recorded session `viability_brief` carries the scorer INPUTS | **False** — it holds only the OUTPUT (`live_eligible=true, viability_score=0.55, freshness_ts=13:08:08`) |
| `execution_readiness_subset` carries as-of-t features | **empty `{}`** |
| a `trading_microstructure_log` row in the entry window to rebuild features | **0 rows** |

The scorer needs the as-of-13:08:31 `ross_signals` batch + exec-readiness features + regime
context; none of that is persisted. The only surviving UPC viability snapshots carry today's
freshness (e.g. variant 123 freshness `2026-06-30 07:33`), reflecting later scans — not the
entry instant. Recomputing the scorer would require **fabricating** the missing inputs, which
the harness refuses by design. `--tier=A` therefore **honestly logs WHY and falls back to
Tier B** (the EFFECTIVE TIER printed is `B (Tier A requested but infeasible -> B; no fakery)`).

**Tier-B vs Tier-A faithfulness for UPC 06-29:** Tier B is the MORE faithful available
reconstruction. The recorded `live_blocked_by_risk` event pins the flicker to the EXACT recorded
block instant (13:08:31.364) with the eligible-at-confirm initial state — a real recorded anchor.
Tier A cannot improve on that here because its inputs don't exist; if forced it would have to
invent them, which would be LESS faithful, not more. (This is precisely the gap PART 1's history
table closes for the future — once `momentum_viability_history` accumulates, a future incident's
Tier A becomes feasible from real recorded rows.)

**VERDICT — PART 2: Tier-A INFEASIBLE for UPC 06-29 (documented, no fakery); Tier-B is the most
faithful tier and remains the harness default.** The `--tier` switch + probe are committed so the
finding is reproducible and a FUTURE incident can take Tier A when the inputs exist.

---

## PART 3 — the trigger-at-13:08: **NOT a harness artifact; the premise was wrong**

**The flagged finding:** "the harness's reconstructed bars at 13:08 don't fire the entry trigger,
but session 9505 reached `live_pending_entry` (the REAL trigger fired) — is the harness a parity
artifact?"

**Root-cause investigation (read-only against `chili`):** I pulled session 9505's recorded events
and state, and swept ALL UPC sessions on 06-29:

- **Session 9505 has exactly THREE events:** `live_arm_requested` (13:08:18) →
  `live_arm_confirmed` (13:08:28, `initial_runner_state=queued_live`) → `live_blocked_by_risk`
  (13:08:31, `errors=["Not live-eligible per neural viability."] severity=block`). Its final
  state is **`live_error`** — NOT `live_pending_entry`.
- To reach `live_pending_entry` the FSM must walk `queued_live → watching_live →
  live_entry_candidate → live_pending_entry` (`live_fsm.py:114-123`). Session 9505 **blocked at
  the `queued_live` boundary-risk gate (on `live_eligible`) at the very first runner tick** — it
  never reached `watching_live`, let alone the trigger or `live_pending_entry`.
- **Across ALL 10 UPC sessions on 06-29 there is NOT a single `live_pending_entry`,
  `live_entry_submitted`, or `live_entry_filled` event.** UPC never reached pending-entry the
  entire day. The closest entry-stage events are all REJECTIONS: `live_entry_trigger_wait`
  (`reason=waiting_for_vwap_reclaim` — the trigger was waiting, i.e. NOT firing),
  `live_entry_backside_benched` (`benched_backside_below_vwap`), `live_entry_midday_deweighted`.

**Conclusion — it is NOT a harness artifact, and the premise is incorrect.** The REAL entry
trigger did **not** fire for UPC at 13:08 (or any time on 06-29); the REAL session 9505 blocked
earlier, on `live_eligible`, exactly as the grace A/B reproduces. The harness's MODE-1 honest
caveat ("the trigger does not fire on the real 13:08 bars") is therefore **FAITHFUL to reality**,
not an over-coarse reconstruction failing a trigger the live path passed. There is no
fill-masking reconstruction bug to fix.

> The `live_pending_entry` that appears in the harness's **MODE 2 (grace-isolation)** arms is the
> harness's OWN substituted trigger-passing frame — it is explicitly the isolation path, not the
> real 9505 trace. The real 9505 never reached that state.

**The one genuine (smaller) parity refinement — a documented P3 item, not a bug:** the harness's
`RecordedOhlcvProvider` serves the WHOLE recorded frame on every in-tick fetch (its docstring
already flags "as-of slicing is P2/P3"). The live runner would have seen bars sliced as-of-t.
For UPC 06-29 this does not change the conclusion (the real trigger didn't fire either way), so
it's a fidelity refinement to fold into the full P3 parity harness, not a cheap reconstruction
bug. I did NOT "fix" the harness bar reconstruction because the harness is already faithful here;
fabricating a trigger-firing reconstruction would be the actual infidelity.

**VERDICT — PART 3: the 13:08 trigger non-fire is REAL, not a harness artifact. The premise
(9505 reached `live_pending_entry`) is refuted by the recorded events. The remaining P3 work
(as-of-t OHLCV slicing in the full parity harness) is scoped below.**

---

## What's left (P3, the larger remaining piece)

The focused-P4 UPC harness proves the grace A/B and now reports Tier-A feasibility honestly. The
larger **P3 full parity harness** (`docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md` §4 P3 /
`tests/test_replay_v3_live_fsm_parity.py`) is still the big remaining item:
- as-of-t OHLCV slicing in `RecordedOhlcvProvider` (serve bars ≤ t, not the whole frame);
- FSM-transition-level parity against a real recorded session that DID reach `live_entered`
  (UPC 06-29 is not such a session — pick one that filled);
- determinism + grace-flag-invariance assertions.

None of that changes today's three verdicts; it hardens the general instrument.

## Verification + commits

- `py_compile` clean on all touched files; `--tier=A` and `--tier=B` both run read-only
  (`chili` → `chili_test`) and preserve the `UPC-FILLS-IN-REPLAY` verdict.
- PART 1 test green (3/3) + no regression in `test_momentum_neural_persistence.py` (8/8).
- Commits: PART 1 `9ad4ecf`; PART 2/3 (harness `--tier` + Tier-A probe + this report) — see the
  follow-up commit. `ross_momentum.py` / `test_squeeze_quality_floor.py` untouched (parallel
  session).
