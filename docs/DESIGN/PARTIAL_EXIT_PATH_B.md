# PATH B — partial exit under a resting full-qty deadman stop (Alpaca)

**Status: DESIGN ONLY. The wiring is deferred. This PR ships the design, the pure
helpers, and a tripwire test. `venue/alpaca_spot.py::replace_order_qty` still has
ZERO production callers and that is asserted by
`tests/test_partial_exit_path_b_unwired.py`.**

Base: `origin/main` 7e6b873. LR = `app/services/trading/momentum_neural/live_runner.py`,
AOC = `.../alpaca_orphan_claims.py`, AS = `app/services/trading/venue/alpaca_spot.py`.

Line numbers below were taken at 9fad8ad, the commit the two adversarial reviews
were run against. Everything above LR:~40000 is unshifted at 7e6b873
(`_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES` is still LR:9200,
`_handle_nonactive_alpaca_lifecycle` still LR:11649); the tick sites below that
moved **+84** (the maintenance call quoted as LR:40165 is LR:40249 at 7e6b873).
Anchor on the identifiers, not the numbers.

---

## 1. The problem

On Alpaca the deadman stop rests for the **whole** position quantity, so it
consumes the entire `qty_available`. A partial sell therefore cannot be
submitted. Today's CANF trade is the canonical instance:

```
alpaca_scale_out_suppressed_for_deadman   (target 4.63, qty 355)
tranche_oco_skipped_extended_hours        (40310000 oco_rth_only)
```

Every Alpaca exit is all-or-nothing. `live_partial_exit_filled` has been zero
since 2026-08-01. The SCALING_OUT handler explicitly disables the split for
Alpaca (LR:44920-44925, `normalize_execution_family(...) not in ALPACA_EXECUTION_FAMILIES`).

**PATH B** is the only route that does not surrender the floor: shrink the
resting stop from `Q` to `R = Q - f` with a PATCH, then sell `f`.

### Broker facts (probe 2026-09-01, paper; recorded in AS:3713-3743)

| fact | consequence |
|---|---|
| PATCH round-trip ~200-260 ms, works in the extended session | the mechanism is viable premarket, where OCO is not |
| PATCH is **422 while the order is `accepted`**; only a **`new`** order is replaceable | the decision site must strict-read lifecycle `new` |
| the replace is **not atomic**; documented stuck-`pending_replace` | there is a window with no terminal truth |
| the predecessor **keeps resting** until the successor is accepted | there is **no unprotected window during the replace itself** |
| the reservation while transitioning is **the larger of the two** | the freed qty is **NOT usable until the replace is terminal** — the partial sell MUST wait for certification |

That last row is load-bearing and contradicts the loose summary "PATCH
immediately frees qty_available". It does not. It frees it once the replace
is terminal. Every design below sequences the POST strictly after
certification.

---

## 2. Why the first design was refuted

Two adversarial reviews found five holes, each verified against the code in
this pass:

* **R1 — certifying in the same pulse flattens the position.** After a PATCH
  200 the predecessor reads `pending_replace`. `pending_replace` is **not** in
  `_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES` (LR:9200-9207) and
  `_handle_nonactive_alpaca_lifecycle` (LR:11649) returns `None` for it, so
  control falls to LR:12136 → `_queue_full_close("deadman_active_certification_failed")`
  → sticky `operator_flatten_requested_utc`. The first PATH B fire would queue
  a **whole-position flatten**. Worse: whenever the ensure returns
  pending/unprotected, LR:40183-40192 returns from the tick **before every
  software-stop and trail site** (40620/40645/40875/40997/44751/44929) — so the
  software stop is disabled for the whole pending window, which premarket is
  zero protection.
* **R2 — lineage deadlock.** A whole exit reaching
  `_release_deadman_at_literal_submit` (LR:14273-14332) while `le.deadman_stop`
  still names the predecessor freezes a close handoff keyed to a *replaced*
  order. Then D2 fails `replacement_successor_owner_generation_mismatch` every
  pulse (AOC:2401-2427), handoff pass 2 cancels a `replaced` order forever
  (`deadman_cancel_not_terminal`), and AOC:3573-3579 blocks every deadman
  lease. **No exit path at all.**
* **R3/H1 — sibling accounting.** When the `R` stop fills while the partial
  still rests, `_apply_terminal_deadman_outcome` (LR:11381-11594) books the
  deadman leg alone and pops `deadman_stop` (LR:11593). Both-filled →
  `_complete_confirmed_live_exit(quantity=R)` with the `f` leg **never booked**
  (realized P&L short by `(P_partial - entry) * f`). Partial-unfilled → the
  sibling adopter (LR:44810-44814) is gated on `not pos.get("partial_taken")`,
  which the deadman partial just set, so the sibling is **never adopted** →
  zero-qty strand, `alpaca_broker_zero_identity_block`, session never EXITED.
* **R4/H3 — naked remainder by design.** A second POST rejection or a stale
  cancel with `k < f` leaves `broker_qty - stop_qty = f - k` shares with no
  resting stop for the rest of the trade. Plus a TRAILING/SCALING_OUT state
  move can strand the sequence entirely.
* **R5 — no escape from a stuck replace.** `_service_deadman_replacement_containment`
  is only reachable on a terminal successor (LR:10223-10255) and its close is
  `order_type=market` / `extended_hours=False` (LR:9751-9763) — **inert
  premarket**, which is exactly when this lane trades.
* **H4 — geometry.** Wiring the burst site (LR:40595) would convert the
  validated whole-position burst exit (968 cases, +3.01%, 88% win, measured at
  100% out) into 30% out + a BE runner. That is a change to the burst exit
  itself, not "making the partial possible", and it would make the burst study
  unmeasurable.

---

## 3. The revised design

Scope: **the SCALING_OUT / first-target site only** (LR:44884-44929). The burst
site stays byte-identical; a burst partial requires its own interleaved-arm
replay A/B first.

### 3.1 Durable claim-phase marker

A sibling key `deadman_qty_replacement` on the action claim — deliberately
**NOT** under `deadman_close_handoff`, whose presence blocks every deadman
lease at AOC:3573.

```
{ identity_contract: 'alpaca_deadman_qty_replacement_v1',
  phase, reason, created_at_utc, updated_at_utc,
  edges: [ { edge_no, predecessor_client_order_id, predecessor_broker_order_id,
             predecessor_order_request, successor_client_order_id,
             successor_order_request (base_size R, same stop_price),
             successor_qty: R, partial_quantity: f,
             submitted_at_utc, submit_outcome,
             successor_broker_order_id, certified_at_utc } ],
  partial_client_order_id, partial_quantity: f, partial_broker_order_id,
  partial_cum_filled, partial_state, retry_count }
```

Every phase advance is a **CAS on the claim, committed BEFORE any dependent
HTTP**. `le.path_b_partial_attempt` is only a mirror and is rebuilt from the
claim whenever it lags — the claim is the truth, `le` is cache. This is what
makes the sequence idempotent across a crash between the HTTP call and the
outer transaction commit.

### 3.2 Phases

```
intent_frozen
  -> replace_submitted | replace_indeterminate | replace_rejected(T)
  -> successor_certified
  -> partial_posting -> partial_posted
  -> partial_filled(T) | partial_stale_adopted(T)
   | partial_rejected -> partial_rejected_final
  -> (restore) restore_intent_frozen -> restore_replace_submitted
             | restore_indeterminate -> restore_certified(T) | restore_rejected(T)
replace_stuck -> containment_queued -> containment_resolved(T) | replace_reverted(T)
abandoned(T)   consumed_by_exit(T)
```

`IN_FLIGHT = {intent_frozen, replace_submitted, replace_indeterminate,
replace_stuck, containment_queued, restore_intent_frozen,
restore_replace_submitted, restore_indeterminate}`.

The machine, its legal transitions and the IN_FLIGHT / terminal / naked-risk
classification are implemented as a pure table in
`app/services/trading/momentum_neural/path_b_partial.py` and unit-tested. That
module is **not imported by live_runner** in this PR.

### 3.3 Ownership: a state-independent per-tick service step

The decision site executes **P0-P2 only**. Everything after the PATCH is owned
by `_service_path_b_marker(db, sess, adapter, *, le, product_id, bid, ask, mid, prod)`,
called **immediately before** `_ensure_alpaca_deadman_stop` at LR:40165 and
running for ENTERED / TRAILING / SCALING_OUT whenever the marker is unresolved
or `le.scale_limit_is_path_b` is set. This is what fixes R4's state-transition
strand and H2's restart deadlock: recovery happens *before* maintenance can see
an inconsistent owner.

### 3.4 What each tick does, per predecessor/successor lifecycle

| predecessor | successor | marker phase | action |
|---|---|---|---|
| `new` | absent | `intent_frozen` | PATCH once (P2). |
| `pending_replace` | absent/unknown | `replace_submitted` / `_indeterminate` | **D8 branch:** return `{ok: True, protected: True, path_b_replace_pending: True}` while `age < 30 s` (`_OWNER_TRANSPORT_LEASE_SECONDS`). Both orders rest at the broker; the predecessor is still live, so this is truthful. Critically it keeps LR:40183 from firing, so the software stop and every trail site stay reachable. Past 30 s → `replace_stuck`. **With no marker the branch returns `None` and the code is byte-identical** — `_queue_full_close` stays. |
| `replaced` | active, cid == `edge.successor_client_order_id`, qty `R` | `replace_submitted` | `_ensure_alpaca_deadman_stop` → LR:11691 → D3 → D2 → `successor_certified` in the same CAS; `le.deadman_stop` becomes the successor. |
| `replaced` | `replaced_by` names a foreign cid, or cid mismatch | any | `replace_stuck` (no "tolerate a broker-assigned cid" clause — `_owner_transport_order_matches` requires exact cid equality at LR:9116). |
| `replaced` (replayed) | certified | `successor_certified` | one maintenance pulse must record the predecessor watermark (filled 0, remaining Q) via LR:10527-10814 before the POST is allowed, else a later successor cancel/fill trips `deadman_replacement_lineage_terminal_truth_unavailable` (LR:10649-10689). |
| — | certified + watermark recorded | `successor_certified` | **P4:** re-run every precondition fresh, then POST the `f` limit. |

### 3.5 P4 — the POST, and its fresh re-checks

Immediately before the POST, all of: no close handoff (claim **and** `le`), no
`operator_flatten_requested_utc`, no emergency/flatten authority, no
`deadman_protection_reconcile_pending` / `_unavailable`; successor strict-read
certifiably active with requested qty `R` and the exact cid; predecessor strict-read
`replaced`; `adapter.get_position_quantity == Q`. Any of the last three failing
→ no POST this pulse, `partial_exit_post_deferred`; two consecutive → `replace_stuck`.
The handoff/flatten check failing → phase `consumed_by_exit` (the exit in flight
closes `Q` through the chokepoint and cancels the `R` stop; no restore needed).

The order shape follows the chokepoint's own extended-hours rule (LR:15668-15694):
`limit`, `time_in_force='day'`, `extended_hours = market_session_now(symbol) != 'regular'`,
`position_intent='sell_to_close'`.

### 3.6 Whole-exit interaction (R2)

* `_release_deadman_at_literal_submit`: **before** LR:14274 (the `handoff is None`
  branch), read the marker; phase in `IN_FLIGHT` → `_block('path_b_replace_in_flight')`,
  one pulse, no attempt increment. This prevents a close handoff being frozen
  against a `replaced` predecessor, which is the deadlock.
* The emergency/flatten authority at LR:29346 consults the same read and skips
  one pulse.
* `_read_exact_alpaca_deadman_handoff` (LR:8938) also returns `qty_replacement`
  so both sites read one source.
* **D2 re-key:** a close handoff that already exists with
  `deadman_client_order_id == predecessor_cid` and no `replacement_deadman_*`
  fields is re-keyed to the successor in the same UPDATE, so an in-flight
  flatten resumes against the `R` stop instead of mismatching forever.

### 3.7 Sibling-fill accounting (R3 / H1)

In `_apply_terminal_deadman_outcome`, when `le.scale_limit_is_path_b`:

1. **FIRST** `_cancel_scale_limit_and_clamp(requested_qty=0.0,
   reason='path_b_partial_deadman_terminal')` — cancel the sibling, strict-read
   it, book its fill `k`, pop `scale_limit_*`. Release-blocked → return pending
   **without** booking the deadman leg (sibling truth is required first).
2. **THEN** book the deadman leg against the reduced position. If the remainder
   is 0, `_complete_confirmed_live_exit` runs only after the partial leg was
   booked, and `_resolve_retained_alpaca_entry_claim_after_broker_flat` only
   after `scale_limit_order_id` was popped. Both legs booked once, EXITED once,
   entry fee once, `momentum_mfe_realized` once.
3. A sibling filling **after** the deadman leg was booked is adopted by the
   service step, and an adopted delta that takes local qty to `<= eps` routes
   through `_complete_confirmed_live_exit(reason='path_b_partial_final')` —
   **never** `_apply_confirmed_live_partial_exit` to zero, which is what
   produces the zero-qty strand.
4. Every path-B adopter/stale rule is keyed on `scale_limit_is_path_b`, **not**
   on `not pos.get("partial_taken")` (which the deadman partial has already set).
   The existing OCO adopter at LR:44810 gains `and not le.get("scale_limit_is_path_b")`.
5. The head guard (LR:10385) subtracts the sibling qty and, when the remainder
   would be zero, **retains** a `le.deadman_stop` record with phase
   `path_b_partial_covers_remainder` so LR:12603 can still resolve a broker-zero
   read — `deadman_stop` is never absent while a sibling rests.
6. A `k < f` stale cancel books `k` through `_scale_out_to_runner` (BE ratchet +
   TRAILING), not through a bare partial booking — otherwise it creates a third
   exit regime that contradicts the LR:44986-45000 doctrine.

### 3.8 Naked-remainder rules (R4)

Failure branches after certification leave `f - k` shares with no resting stop.
The rule is: **restore within one pulse, or flatten.**

Append edge 2 to the same marker (`predecessor = current successor`,
`successor_qty = broker_qty - open_partial_qty`, generation N+2 with the
watermark bump in the same UPDATE) → PATCH → certify. Rejected, or
indeterminate past the lease → `_queue_full_close('path_b_stop_qty_unrestored')`.
Flatten beats naked.

### 3.9 Stuck-replace escape (R5)

`_service_deadman_replacement_containment` gains `close_shape: dict | None = None`
(None → byte-identical for the existing LR:10243 caller). Phase `replace_stuck`
**queues** the containment — it is never merely an operator event. Flow: cancel
the successor → strict-read both → predecessor certifiably `new` again →
`replace_reverted` (owner unchanged, position `Q` protected, fire-once consumed);
otherwise close the whole position with the **chokepoint's marketable-limit
extended-hours shape**, not `market`/`extended_hours=False`, so the escape is
not inert premarket.

---

## 4. Why the wiring is deferred

The design above satisfies every amendment. The wiring is still not shipped,
for four reasons that are properties of the situation rather than of the plan.

### D1 — One mandated invariant is unsatisfiable by PATH B, by construction

> *the remainder never lacks protection for more than one tick, and never lacks
> it at all in RTH*

PATH B shrinks the stop to `R = Q - f` **before** the partial is sold. From
certification until the sibling is terminal, the broker holds a stop for `R` and
a limit for `f` — sound. But every post-certification failure branch (POST
rejected twice, POST indeterminate, stale cancel with `k < f`) leaves `f - k`
shares with no resting stop, and the restore edge is itself a PATCH that must
pass through `pending_replace`. The honest guarantee is therefore:

> **naked for at most one lease window (30 s), then flattened**

not "never naked in RTH". That is a genuine, irreducible weakening of the
deadman guarantee, and it is the guarantee the deadman exists to provide. It
should be accepted deliberately by the operator, with the window quantified, not
smuggled in inside a wiring PR.

### D2 — The idempotency invariant cannot be *tested* under this run's constraints

Fire-once and restart-safety rest entirely on
`advance_deadman_qty_replacement_committed` committing in an **independent
transaction before** each dependent HTTP. A DB-free fake claim store proves the
*call ordering inside the helper*; it cannot prove the transactional property,
and the transactional property is exactly what H2's rollback-between-POST-and-commit
deadlock turns on. Tests here are DB-free by mandate, so the invariant "idempotent
across restart" can be asserted but not demonstrated. Shipping wiring whose
central safety property is untested is the failure mode this project already
paid for once (#1283: green helper tests, a seam that never worked).

### D3 — The blast radius lands on the protection path itself

D2/D3/D4/D8/D9/D10 all modify code that runs for **every** Alpaca position on
**every** tick, including positions that never take a partial. The kill switch
gates the decision site (P0) but not the marker-gated branches — those are
byte-identical only while no marker exists, which is a property of the marker
read, not of the flag. The failure mode of a bug in the D8 `pending_replace`
branch is: **a genuinely dead stop reported as `protected: True`** → a naked
position that no site reports. That is the worst available outcome in this
system and it is reachable from a single swallowed exception in the claim read.

### D4 — The chokepoint block introduces a new "flatten did not flatten" class

`IN_FLIGHT` includes `replace_stuck` and `containment_queued`, which are not
one-pulse phases. An operator, EOD or drawdown-breaker flatten arriving in those
phases is deferred into the containment close — a different close shape under a
different authority. Bounded, but new, and it deserves live soak evidence rather
than unit tests.

### What would unblock the wiring

1. Operator acceptance of the "naked ≤ 30 s, then flatten" guarantee in place of
   the absolute one, in writing.
2. A DB-backed test run (`chili_repro2_test`) exercising the claim CAS across a
   simulated rollback between POST and commit — the H2 case.
3. Landing D8/D9/D10 as a **separate, marker-free refactor PR** first (extract
   the branch points, prove byte-identical behaviour with no marker anywhere),
   so the marker PR's diff is additive only.
4. One PAPER soak of the assumptions in §5 with the lane capped.

That sequencing costs three PRs instead of one and removes the possibility that
taking a partial flattens a runner.

---

## 5. Assumptions still to verify on PAPER (no new probes were run)

* Alpaca echoes the requested `client_order_id` on the replacement order. The
  design makes this a **hard** requirement (a mismatch is contained by §3.9,
  never left naked), so a mismatch would make PATH B unusable rather than unsafe.
* The successor preserves `sell_to_close` / `gtc` / `extended_hours=False` /
  the same stop price.
* Hold-release timing: how many pulses after certification the freed qty is
  actually usable. One POST retry covers a single miss.
* Cancelling a stuck `pending_replace` reverts the predecessor to `new`.
* A PATCH on the `R` stop (restore edge) behaves like the first edge.

---

## 6. What this PR ships

* This document.
* `app/services/trading/momentum_neural/path_b_partial.py` — pure, no-I/O:
  the phase table with `advance_phase`, the classification predicates
  (`is_in_flight` / `is_terminal` / `blocks_whole_exit`), the split planner, and
  `assess_protection()`, the invariant checker every future wiring must call.
  **Not imported by `live_runner`.**
* `tests/test_partial_exit_path_b_helpers.py` — unit tests for the above.
* `tests/test_partial_exit_path_b_unwired.py` — the tripwire: asserts
  `replace_order_qty` has zero production callers, that the SCALING_OUT Alpaca
  exclusion is still in place, and that `pending_replace` is still absent from
  `_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES`. **Delete it in the PR that wires PATH B**,
  replacing it with the call-site guards described above.

No production code path changes. The burst site is untouched.
