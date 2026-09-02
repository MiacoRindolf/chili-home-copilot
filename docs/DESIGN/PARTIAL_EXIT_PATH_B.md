# PATH B — partial exit under a resting full-qty deadman stop (Alpaca)

**Status: DESIGN ONLY, REVISION 3. The wiring is deferred. This PR ships the
design, the pure helpers, and three guard test files.
`venue/alpaca_spot.py::replace_order_qty` still has ZERO production callers and
that is asserted by `tests/test_partial_exit_path_b_unwired.py` — which, in
revision 1, scanned zero files and therefore asserted nothing (see §6).**

Revision 2 answered the 18 amendments of the second adversarial round (§7).
Revision 3 is a self-audit of revision 2 that found three more defects of the
same class — a fix applied to one artifact but not its twin — the largest being
that the S4 correction never reached `NAKED_RISK_PHASES`, so the phase set and
the protection checker disagreed about the entire normal PATH B window. See
§3.2 and §7.1.

Base: `origin/main` 7e6b873. LR = `app/services/trading/momentum_neural/live_runner.py`,
AOC = `.../alpaca_orphan_claims.py`, AS = `app/services/trading/venue/alpaca_spot.py`.

Line numbers are taken at **7e6b873** unless marked `@9fad8ad` (the commit the
first adversarial round ran against; the tick sites below LR:~40000 moved +84
between the two). Anchor on identifiers, not numbers.

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
Alpaca (`normalize_execution_family(...) not in ALPACA_EXECUTION_FAMILIES`).

**PATH B**: shrink the resting stop from `Q` to `R = Q - f` with a PATCH, then
sell `f`.

### Broker facts (probe 2026-09-01, paper; recorded in AS:3713-3743)

| fact | consequence |
|---|---|
| PATCH round-trip ~200-260 ms, works in the extended session | the mechanism is viable premarket, where OCO is not |
| PATCH is **422 while the order is `accepted`**; only a **`new`** order is replaceable | the decision site must strict-read lifecycle `new` |
| the replace is **not atomic**; documented stuck-`pending_replace` | there is a window with no terminal truth |
| the predecessor **keeps resting** until the successor is accepted | there is **no unprotected window during the replace itself** |
| the reservation while transitioning is **the larger of the two** | the freed qty is **NOT usable until the replace is terminal** — the partial sell MUST wait for certification |

That last row contradicts the loose summary "PATCH immediately frees
`qty_available`". It does not. Every sequence below posts the partial strictly
**after** certification.

---

## 2. What two rounds of adversarial review found

### Round 1 (against the first plan) — R1-R5, H1-H4

* **R1** — certifying in the same pulse drives the ordinary transient
  `pending_replace` into `_queue_full_close("deadman_active_certification_failed")`:
  a whole-position flatten, plus the software stop disabled for the pending
  window (LR:40183-40192 @9fad8ad returns before every trail/stop site).
* **R2** — a whole exit reaching `_release_deadman_at_literal_submit` while the
  ledger still names the predecessor freezes a close handoff against a
  `replaced` order: successor never certified, cancel never terminal
  (`deadman_cancel_not_terminal`), every deadman lease blocked (AOC:3573-3579).
  **No exit path at all.**
* **R3 / H1** — the `R` stop filling while the partial rests books one leg and
  drops the other, or strands a zero-qty position
  (`alpaca_broker_zero_identity_block`).
* **R4 / H3** — two failure branches leave the remainder with no resting stop
  for the rest of the trade; a TRAILING/SCALING_OUT move can strand the sequence.
* **R5** — a stuck `pending_replace` has no escape, and the containment close is
  `market` / `extended_hours=False` (LR:9751-9763): **inert premarket**.
* **H4** — wiring the burst site converts the validated whole-position burst
  exit (968 cases, +3.01%, 88% win, measured at 100% out) into 30/70 + BE and
  makes the burst study unmeasurable.

### Round 2 (against revision 1 of this document and its shipped code) — S1-S7, L1-L5

Round 2 is the reason this document is revision 2. Two of its findings are
defects in code revision 1 actually **shipped**, and five are holes in §3 that
made the amendments unimplementable as written. All are verified in this pass
against 7e6b873:

* **L1 (shipped defect, fixed here)** — the tripwire's scan root was
  `parents[3]` and it globbed `_APP / "app"`, i.e. `<repo>/app/app`, which does
  not exist. `test_replace_order_qty_still_has_zero_production_callers` scanned
  **zero files** and passed unconditionally. A guard that cannot fail is worse
  than no guard, because the PR cites it as evidence.
* **S4 (shipped defect, fixed here)** — `assess_protection()` counted the
  resting partial **limit** as protection. The partial is a sell limit *above*
  the market (CANF: 4.63) — upside liquidity, not downside cover. The canonical
  PATH B window (355 / 249 / 106) certified as `covered, ok=True`, and a shipped
  test asserted that as correct. The invariant checker the document told every
  future wiring to call would have given a green light to the one state PATH B
  is actually paying for.
* **S1** — the successful PATCH can **never be certified**.
  `_dispatch_alpaca_replaced_deadman_successor` builds the expected successor
  envelope by copying the predecessor and swapping only the cid
  (LR:10101-10104), so `base_size` stays `Q`, while
  `_owner_transport_order_matches` requires `abs(broker_qty - base_size) <= tol`
  (LR:9108-9130). A successor resting for `R` fails by exactly `f`, forever
  (`replacement_deadman_successor_lineage_unproven`). Revision 1's D3 amendment
  targeted LR:10202-10208, which is *downstream* of this gate, and never
  mentioned the second gate at LR:10174-10190 either. **The happy path was
  unreachable, and revision 1's escape from it (`replace_stuck` → containment →
  "otherwise close the whole position") flattened the runner 30 s after a
  successful partial-exit PATCH.** This is now proven by
  `tests/test_partial_exit_path_b_lineage_seam.py` against the real predicate.
* **S2** — oversell / short-flip window. `_cancel_scale_limit_and_clamp` is
  keyed entirely on the `le` mirror: `oid = le.get("scale_limit_order_id"); if
  not oid: return float(requested_qty)` (LR:18968-18970) — a pass-through noop.
  Its own docstring says the guard exists so "the resting limit and the market
  exit could both execute and flip the account short". In `partial_posting` /
  `partial_indeterminate` the sibling's cid is known only to the claim, and
  revision 1 deliberately left those phases unblocked, so a stop/bailout/EOD
  exit submits `Q` while `f` may be resting: `Q + f` of sell authority against
  `Q` shares.
* **S3** — the head guard subtracts `le["scale_limit_qty"]`, the **original**
  `f`, which is only ever assigned (LR:18827/18933/42475) and never decremented
  as the sibling fills. After `k` fills the position is `Q-k` but the guard
  still subtracts `f`, minting a stop for `(Q-k)-f` and leaving `f-k` shares
  naked with no error and no event. At CANF's numbers (355/106/40): position
  315, stop 209, **106 naked**. If the remainder ever falls to `<= f` the same
  guard takes the `else` → `_queue_full_close("tranche_oco_split_arithmetic_invalid")`.
* **S5** — post-certification transients (a legitimate pyramid add, two strict-read
  misses) escalated to `replace_stuck` → containment → flatten, even though the
  `R` stop is known to be resting.
* **S6 / L5** — `partial_stale_adopted` and `restore_rejected` were
  simultaneously TERMINAL, NAKED_RISK, and had empty transition sets. The
  remedy R4 mandates (append a restore edge, or queue a flatten) could not be
  recorded at all; `advance_phase` raises on any move out of them, and the
  marker carries a single `phase` field so the docstring's "start a new edge"
  escape hatch did not exist. Likewise `consumed_by_exit` was reachable only
  from `successor_certified`, so the most likely real race — a whole exit
  landing while the `f` limit rests, which §3.6 deliberately permits — was
  unrecordable.
* **S7 / L2** — `blocks_whole_exit` was `is_in_flight`, and IN_FLIGHT contained
  `replace_stuck`, `containment_queued` and every `restore_*` phase. But the
  containment close (LR:9959) and the R4 flatten both submit through
  `_release_deadman_at_literal_submit` (LR:15878 / 15975). The phase blocked the
  only action that could clear the phase, indefinitely, including operator
  flatten, EOD and the drawdown breaker. Worse, `NAKED_RISK ∩ IN_FLIGHT` was
  non-empty: in exactly the phases where the module reported a naked remainder
  and demanded a flatten, it also blocked the flatten. **R2 reincarnated by the
  R2 fix.**
* **L3** — `_service_path_b_marker` placed "immediately before
  `_ensure_alpaca_deadman_stop`" (LR:40249) is unreachable whenever a close
  handoff exists: the handoff-priority block returns from the tick at
  LR:39788 (`replacement_deadman_predecessor_not_inert`), LR:39988, LR:40010 and
  LR:40241 — all above it.
* **L4** — `_service_deadman_replacement_containment` refuses at LR:9733-9740
  unless a successor broker id **and** cid both exist (a stuck `pending_replace`
  that never produced a successor has neither), and its post-cancel two-sided
  gate (LR:9838-9859) certifies **only** predecessor-`replaced` + successor-terminal
  — which is exactly the opposite of the `replace_reverted` outcome §3.9 wanted.
  Adding a `close_shape` parameter changes neither gate.

Nothing in round 2 overturned: the replace-before-sell ordering, the
single-owner-transport reasoning, `conservation_holds`, the oversell half of
`assess_protection`, §3.7's sibling-first accounting, the marker-gated D8
branch, or the H4 scope decision.

---

## 3. The revised design (revision 2)

Scope: **the SCALING_OUT / first-target site only** (LR:44884-44929 @9fad8ad).
The burst site stays byte-identical; a burst partial requires its own
interleaved-arm replay A/B first (H4).

### 3.1 Durable claim-phase marker

A sibling key `deadman_qty_replacement` on the action claim — deliberately
**NOT** under `deadman_close_handoff`, whose presence blocks every deadman lease
at AOC:3573.

```
{ identity_contract: 'alpaca_deadman_qty_replacement_v1',
  phase, reason, created_at_utc, updated_at_utc, phase_entered_at_utc,
  edges: [ { edge_no, predecessor_client_order_id, predecessor_broker_order_id,
             predecessor_order_request, predecessor_filled_size,
             successor_client_order_id,
             successor_order_request,      # base_size R — see §3.4a
             successor_qty: R, partial_quantity: f,
             submitted_at_utc, submit_outcome,
             successor_broker_order_id, certified_at_utc } ],
  partial_client_order_id, partial_quantity: f, partial_broker_order_id,
  partial_cum_filled, partial_state, retry_count }
```

Every phase advance is a **CAS on the claim, committed BEFORE any dependent
HTTP**. `le.path_b_partial_attempt` is only a mirror and is rebuilt from the
claim whenever it lags — the claim is truth, `le` is cache. `phase_entered_at_utc`
and `created_at_utc` are load-bearing: they feed the two wall clocks in §3.6.

### 3.2 Phases

```
intent_frozen
  -> replace_submitted | replace_indeterminate | replace_rejected(T)
  -> successor_certified
       -> partial_posting -> partial_posted -> partial_filled(T)
       -> post_deferred -> {partial_posting | restore_intent_frozen}
partial_posted    -> partial_stale_adopted        (NAKED — not terminal)
partial_rejected  -> partial_rejected_final       (NAKED)
restore_intent_frozen -> restore_replace_submitted | restore_indeterminate
                      -> restore_certified(T) | restore_rejected (NAKED)
any NAKED phase   -> flatten_queued -> flattened(T)
replace_stuck (PRE-certification only) -> containment_queued
                      -> containment_resolved(T) | replace_reverted(T) | abandoned(T)
consumed_by_exit(T)  reachable from every phase where the whole exit is not
                     blocked, EXCEPT the two named in
                     EXIT_CONSUMPTION_UNRECORDABLE_PHASES (see below)
```

Four sets, and they are **not** the same set — conflating the first three was
S7/L2:

* `TERMINAL_PHASES` — no service step is owed. `is_in_flight(phase)` is exactly
  "not terminal".
* `NAKED_RISK_PHASES` — part of the position has no resting **downside stop**.
  From every one of them a `REMEDY_PHASE` (restore or flatten) is **reachable**
  — reachable, not adjacent (`reachable_phases`) — and
  `TERMINAL ∩ NAKED_RISK == ∅` (unit-tested).
* `WHOLE_EXIT_BLOCKING_PHASES` = `{intent_frozen, replace_submitted,
  replace_indeterminate}` — the only phases where the stop's **owner** is
  genuinely ambiguous. `WHOLE_EXIT_BLOCKING ∩ NAKED_RISK == ∅` (unit-tested):
  no phase may report a naked remainder and block the flatten that covers it.
* `EXIT_CONSUMPTION_UNRECORDABLE_PHASES` = `{replace_stuck,
  containment_queued}` — the named exception to the `consumed_by_exit` rule.
  There a broker order of **unknown ownership** rests; `consumed_by_exit` is
  terminal, so allowing it would stop servicing while that order is still
  live (the ghost/zombie-order shape). The containment lineage must keep
  running even after an operator has flattened the position, because the
  *order* is the remaining problem, not the position. The exception carries its
  own invariant: both phases stay serviced (non-empty transitions), both keep
  an explicit `-> abandoned` operator route, and neither is naked.

**Revision 3 — `NAKED_RISK_PHASES` re-derived (this was revision 2's largest
surviving defect, and it had S4's exact shape).** Revision 2 fixed
`assess_protection` so it no longer counts the resting upside limit as
protection, but never propagated that to the phase set. The two shipped
artifacts then contradicted each other about the very window PATH B buys:

```
assess_protection(broker_qty=355, stop_qty=249, open_partial_qty=106)
    -> status="naked_downside", naked_downside_qty=106
"partial_posted" in NAKED_RISK_PHASES      -> False        (revision 2)
```

§4/D1 already stated the truth — "from certification until the partial is
terminal … `f` shares carry NO downside stop" — and that span is five phases,
none of which was flagged: `successor_certified` and `post_deferred` (full `Q`
held, `R` stopped, nothing posted: `f` naked with no resting sell at all), and
`partial_posting` / `partial_posted` / `partial_indeterminate` (`f` has a sell
limit, but *above* the market). The phase set is the only thing a wiring
consults to know whether a remedy is owed, so as written it would have skipped
the remedy across the entire normal window. Both are now flagged, and a test
ties the checker to the set so they cannot drift apart again.

The table, `advance_phase`, and the predicates live in
`app/services/trading/momentum_neural/path_b_partial.py`, **not imported by
`live_runner`** in this PR. `advance_phase` is the only sanctioned writer of
`phase`; there is no "new edge" bypass (revision 1's docstring offered one,
which would have let a wiring PR skip the only validator shipped here).

### 3.3 Ownership: a service step that runs ABOVE the handoff-priority block

The decision site executes **P0-P2 only**. Everything after the PATCH is owned
by `_service_path_b_marker(db, sess, adapter, *, le, product_id, ...)`, which
runs for ENTERED / TRAILING / SCALING_OUT whenever the marker is unresolved or
`le.scale_limit_is_path_b` is set, at **two** call sites:

1. **Above the handoff-priority block** — before the durable-handoff read that
   opens it, i.e. above LR:38856, not at LR:40249. Revision 1's placement
   ("immediately before `_ensure_alpaca_deadman_stop`") is unreachable whenever
   a close handoff exists, because LR:39788 / 39988 / 40010 / 40241 all return
   from the tick first (L3). The marker must be able to advance *while* a whole
   exit is frozen; otherwise the phase never reaches `replace_stuck`, the
   containment is never queued, and the deadlock is permanent.
2. **At the head of the exit chokepoint**, before `_cancel_scale_limit_and_clamp`
   (call site LR:12834, helper LR:18952) — see §3.6, S2.

Additionally the LR:39769-39794 `pending_replace` branch becomes **marker-aware**:
with a path-B marker present and `age < _OWNER_TRANSPORT_LEASE_SECONDS`, fall
through to the service step instead of returning
`replacement_deadman_predecessor_not_inert`. With no marker it is byte-identical.

The wiring PR must ship a **source-order AST guard** asserting that the
`_service_path_b_marker` call site strictly precedes every `return` inside the
handoff-priority block. A runtime test cannot reach that code without a DB;
this is the #1283 lesson applied to placement.

### 3.4 What each tick does, per predecessor/successor lifecycle

| predecessor | successor | marker phase | action |
|---|---|---|---|
| `new`, `filled_size == 0` | absent | `intent_frozen` | PATCH once (P2). A `partially_filled` predecessor is **refused by the planner** (§3.4b). |
| `pending_replace` | absent/unknown | `replace_submitted` / `_indeterminate` | **D8 branch:** return `{ok: True, protected: True, path_b_replace_pending: True}` while `age < 30 s`. Both orders rest at the broker; the predecessor is still live, so this is truthful, and it keeps LR:40183 from disabling the software stop. Past 30 s → `replace_stuck`. **No marker → return `None`, byte-identical** (`_queue_full_close` stays). |
| `replaced` | active, cid == `edge.successor_client_order_id`, qty `R` | `replace_submitted` | dispatch with the **marker envelope** (§3.4a) → D3 → D2 → `successor_certified` in the same CAS. |
| `replaced` | `replaced_by` names a foreign cid, or any cid mismatch | any | `replace_stuck` (no "tolerate a broker-assigned cid" clause; `_owner_transport_order_matches` requires exact cid equality at LR:9116). |
| `replaced` (replayed) | certified | `successor_certified` | one maintenance pulse must record the predecessor watermark (filled 0, remaining Q) via LR:10527-10814 before the POST, else a later successor cancel/fill trips `deadman_replacement_lineage_terminal_truth_unavailable` (LR:10649-10689). |
| — | certified + watermark recorded | `successor_certified` | **P4:** re-run every precondition fresh, then POST the `f` limit (§3.5). |
| — | certified | `successor_certified`, **broker qty grew** (pyramid add) | §3.4c. |

#### 3.4a — the qty-shrinking edge must be PROVABLE (S1; amendments R0-1, R0-2)

This is the item that made revision 1's happy path unreachable. **D3 amends TWO
gates, not one:**

* **Gate 1, LR:10101-10116.** `successor_request` must come from the
  **marker's** `successor_order_request` (base_size `R`) whenever a
  `deadman_qty_replacement` edge matches `(predecessor_cid, successor_cid)`, not
  from `{**predecessor_request, "client_order_id": cid}`.
  `_alpaca_replacement_successor_order_matches` / `_owner_transport_order_matches`
  keep the exact-cid and exact-stop-price checks and every other field identity
  (side, product, tif, `position_intent=sell_to_close`, `extended_hours=False`);
  only `base_size` is compared against the marker's `R`. The envelope must be
  built from the **marker**, never from a broker-echoed quantity — echoing the
  broker's own number back at it makes the check a tautology. With no marker
  edge, the code path is byte-identical.
  `path_b_partial.marker_successor_envelope()` is that construction, and
  `tests/test_partial_exit_path_b_lineage_seam.py` proves against the **real**
  production predicate that today's envelope rejects the `R` successor and the
  marker envelope accepts it, while every other field stays strict.
* **Gate 2, LR:10156 + LR:10174-10190** — the gate revision 1 missed entirely.
  `requested_qty = float(predecessor_request["base_size"])` (= `Q`) feeds
  `abs(local_qty - requested_qty) <= tol`. The instant `k` shares of the partial
  fill, `local_qty` is `Q-k` and this returns
  `replacement_deadman_successor_quantity_generation_mismatch` forever — H2's
  deadlock at a line the first amendment did not reach. It becomes
  `conservation_holds(broker_qty=local_qty, successor_qty=R, partial_qty=f,
  partial_cum_filled=k)`. The companion bound in the same predicate,
  `0.0 <= broker_qty <= requested_qty + tol`, must keep comparing against the
  **predecessor's** `Q` (the position before the edge), not `R`, or it fails
  before the partial has filled at all.
* **Gate 3, LR:10202-10208** — the `conserved` test, as in revision 1, becomes
  `conservation_holds(...)`.

#### 3.4b — a `partially_filled` predecessor is never split (amendment R0-10)

`partially_filled` **is** in `_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES`
(LR:9200-9207), so the lifecycle gate will not catch it; a triggered-and-partly-filled
stop would be PATCHed and would re-authorize shares already sold.
`plan_replacement_edge` therefore takes `predecessor_filled_size` and refuses
the split unless it is zero (`reason='predecessor_partially_filled'`).

#### 3.4c — a pyramid add while the marker is unresolved (amendment R0-4)

This lane pyramids (`pyramid_add_decision` / `pyramid_blend_on_fill`), so the
position can legitimately become `Q + a` between P2 and P4 and stay there. That
must not abort the partial and must never reach containment. Rule: **restore the
stop to the new broker quantity before the POST.** Append a restore edge
(`successor_qty = broker_qty - open_partial_qty`), certify it, then continue at
`partial_posting`. Until it certifies, the `a` added shares are naked and are
counted as such by `assess_protection`.

### 3.5 P4 — the POST, and its fresh re-checks

Immediately before the POST, all of: no close handoff (claim **and** `le`); no
`operator_flatten_requested_utc`; no emergency/flatten authority; no
`deadman_protection_reconcile_pending` / `_unavailable`; successor strict-read
certifiably active with requested qty `R` and the exact cid; predecessor
strict-read `replaced`; and

```
adapter.get_position_quantity(product_id) >= successor_qty + partial_qty   (± tol)
```

**not** `== Q` — a pyramid add is legal (§3.4c). Any precondition failing →
phase `post_deferred`, no POST this pulse. From `post_deferred` the only exits
are: retry the POST, take the restore edge, or surface
`path_b_post_deferred_operator_review`. **`successor_certified -> replace_stuck`
is removed from the graph** (S5): once the `R` stop is known-resting, a read
hiccup or a legal add must never escalate into anything that can flatten a
protected runner. Handoff/flatten present → phase `consumed_by_exit`.

The order shape follows the chokepoint's own extended-hours rule (LR:15668-15694):
`limit`, `time_in_force='day'`,
`extended_hours = market_session_now(symbol) != 'regular'`,
`position_intent='sell_to_close'`.

### 3.6 Whole-exit interaction (R2, S2, S7/L2)

**The block.** In `_release_deadman_at_literal_submit`, **inside the
`if handoff is None:` branch** (before the `_successor_request` derivation, LR:14274
@9fad8ad) — not merely "before" it, which was ambiguous between the safe and the
deadlocking reading. Placed there it prevents a *new* handoff being frozen
against a `replaced` predecessor (the R2 deadlock) and never blocks a submit that
already carries a durable handoff: the containment close (LR:9959) and the
priority close (LR:40017) both pass `alpaca_handoff_recovery` and must keep
working.

```python
blocks_whole_exit(phase, age_seconds=..., override_authority=...)
```

* blocking set is the three owner-ambiguous phases only;
* it takes the phase's age and a hard 30 s ceiling (one owner-transport lease)
  and returns `False` past it — the block is a request for one pulse, never a
  lock. An unknown, non-finite or negative age also returns `False`;
* `override_authority=True` for operator flatten, EOD, the drawdown breaker and
  the kill switch: those authorities are **never** deferrable behind a marker;
* a marker-level wall clock (`MARKER_UNRESOLVED_CEILING_SECONDS = 300 s`,
  `marker_ceiling_exceeded()`) forces `abandoned` plus a restore-or-flatten
  decision, so no marker can wedge exits indefinitely.

`replace_stuck`, `containment_queued` and every `restore_*` phase **do not
block**: there, the whole exit *is* the resolution (L2).

**The oversell fix (S2).** Amendment R0-5 offers two remedies and requires one.
We take the **alternative**, deliberately: `_service_path_b_marker` also runs at
the **head of the exit chokepoint**, before `_cancel_scale_limit_and_clamp`,
whenever `requires_sibling_reconcile(phase)` is true
(`partial_posting` / `partial_posted` / `partial_indeterminate`). It rebuilds
`le["scale_limit_order_id"]`, `le["scale_limit_qty"]` and
`le["scale_limit_open_qty"]` from the claim by strict-reading the partial cid, so
the clamp's OVERSELL INVARIANT actually runs instead of taking its
`if not oid: return requested_qty` pass-through.

Why the alternative and not the block: blocking those phases would defer an
operator flatten, an EOD close or a drawdown-breaker flatten while an `f` limit
rests — which amendments R0-6 and R1-2 both forbid, and which is the same
failure class as L2. Fixing the clamp's input is a root-cause fix; blocking the
exit is not. The two shipped predicates are tied together by a test asserting
that every `SIBLING_LIVE_PHASES` member is simultaneously *not blocking* and
*reconcile-required*, so removing one without the other fails loudly.

**Also.** The LR:29346 emergency/flatten authority consults the same read (with
`override_authority=True`, i.e. it is informed but never deferred).
`_read_exact_alpaca_deadman_handoff` (LR:8938) also returns `qty_replacement` so
both sites read one source. **D2 re-key:** a close handoff that already exists
with `deadman_client_order_id == predecessor_cid` and no `replacement_deadman_*`
fields is re-keyed to the successor in the same UPDATE, so an in-flight flatten
resumes against the `R` stop instead of mismatching forever.

### 3.7 Sibling-fill accounting (R3 / H1 / S3)

In `_apply_terminal_deadman_outcome`, when `le.scale_limit_is_path_b`:

1. **FIRST** `_cancel_scale_limit_and_clamp(requested_qty=0.0,
   reason='path_b_partial_deadman_terminal')` — cancel the sibling, strict-read
   it, book its fill `k`, pop `scale_limit_*`. Release-blocked → return pending
   **without** booking the deadman leg.
2. **THEN** book the deadman leg against the reduced position. If the remainder
   is 0, `_complete_confirmed_live_exit` runs only after the partial leg was
   booked, and `_resolve_retained_alpaca_entry_claim_after_broker_flat` only
   after `scale_limit_order_id` was popped. Both legs booked once, EXITED once,
   entry fee once, `momentum_mfe_realized` once.
3. A sibling filling **after** the deadman leg was booked routes through
   `_complete_confirmed_live_exit(reason='path_b_partial_final')` — **never**
   `_apply_confirmed_live_partial_exit` to zero, which is the zero-qty strand.
4. Every path-B adopter/stale rule is keyed on `scale_limit_is_path_b`, **not**
   on `not pos.get("partial_taken")` (LR:44810-44814/44837 @9fad8ad), which the
   deadman partial has already set. The existing OCO adopter gains
   `and not le.get("scale_limit_is_path_b")`.
5. **The head guard (LR:10385-10405) must subtract the OPEN partial, not the
   original one** (S3). `le["scale_limit_qty"]` is only ever assigned
   (LR:18827 / 18933 / 42475) and never decremented, so after `k` fills it
   over-subtracts by `k` and leaves `f-k` shares with no stop, silently. Either
   maintain `le["scale_limit_open_qty"]`, decremented in the **same commit** that
   books each sibling fill, or compute `open = scale_limit_qty -
   scale_limit_adopted_qty` (that counter already exists and is incremented at
   LR:19098/19133) from a strict read by cid. Additionally the `else` branch at
   LR:10404-10405 (`alpaca_legacy_scale_order_conflicts_with_deadman` → full
   close) **must be gated on `scale_limit_is_path_b` alongside
   `scale_limit_is_oco`**, or PATH B's own sibling flattens the position through
   the head guard on the very first pulse. When the remainder would be zero the
   guard **retains** a `le.deadman_stop` record with phase
   `path_b_partial_covers_remainder` so LR:12603 can still resolve a broker-zero
   read.
6. A `k < f` stale cancel books `k` through `_scale_out_to_runner` (BE ratchet +
   TRAILING), not a bare partial booking — otherwise it is a third exit regime
   contradicting the LR:44986-45000 doctrine.

### 3.8 Naked-remainder rules (R4, S6/L5)

The rule is **restore within one pulse, or flatten** — and the graph must be
able to *record* both, which revision 1's could not.

Every `NAKED_RISK` phase (`partial_rejected`, `partial_rejected_final`,
`partial_stale_adopted`, `restore_*`, `flatten_queued`) has a legal transition
into a restore or a flatten, and none of them is terminal. The restore edge is
edge 2 on the same marker (`predecessor = current successor`,
`successor_qty = broker_qty - open_partial_qty`, generation N+2 with the
watermark bump in the same UPDATE) → PATCH → certify. Rejected, or indeterminate
past the lease → `flatten_queued`, which is `_queue_full_close('path_b_stop_qty_unrestored')`,
then `flattened` when the position is confirmed flat.

That flatten is submitted on a *later* tick through the same chokepoint
(`_queue_full_close` only sets sticky `operator_flatten_requested_utc`,
LR:10368-10383). It works only because no `NAKED_RISK` phase blocks the whole
exit — the invariant `WHOLE_EXIT_BLOCKING ∩ NAKED_RISK == ∅`, unit-tested,
is what makes "flatten beats naked" true rather than aspirational.

### 3.9 Stuck-replace escape — split in two (R5, L4, S1, S5)

**Pre-certification only.** `replace_stuck` is reachable only from
`replace_submitted` / `replace_indeterminate`, i.e. while the owner is genuinely
ambiguous. A **new** `_service_path_b_stuck_replace(...)` owns it, because the
existing containment cannot: its identity gate (LR:9733-9740) requires a
non-empty successor broker id *and* cid, which a stuck `pending_replace` that
never produced a successor does not have; and its post-cancel two-sided gate
(LR:9838-9859) certifies **only** predecessor-`replaced` + successor-terminal,
which is precisely the opposite of the `replace_reverted` outcome we want. Its
own two-sided rule accepts **both** terminal shapes:

* **(a)** predecessor strict-reads back to `new` with no successor →
  `replace_reverted` (owner unchanged, position `Q` protected, fire-once
  consumed);
* **(b)** predecessor `replaced` with a terminal successor → borrow the existing
  `_service_deadman_replacement_containment`, which that shape does satisfy.

Only branch (b) needs the `close_shape: dict | None = None` extension (None →
byte-identical for the existing LR:10243 caller), so the close is a marketable
limit with `extended_hours = market_session_now(symbol) != 'regular'`
(LR:15668-15694 shape) instead of `market` / `extended_hours=False`
(LR:9751-9763), which is inert premarket — exactly when this lane trades.
`close_shape` alone is **not** the fix for R5 and must not be presented as one.

**Post-certification there is no containment and no whole-position close.** The
`R` stop is resting; the position is protected for `R`. The only legal responses
to a stuck or failing state are retry, the restore edge, or
`path_b_post_deferred_operator_review`. Revision 1's "otherwise close the whole
position" fallback is deleted from every post-certification path — with S1
unfixed it would have flattened the runner 30 s after every ordinary successful
PATCH.

### 3.10 Lineage bookkeeping

Conservation for a marker edge replaces LR:10202-10208's
`broker_qty == successor_requested` with
`broker_qty == successor_qty + (partial_qty - partial_cum_filled)`, and the same
form replaces the LR:10174-10190 comparison (§3.4a). `reused` detection is keyed
on `(predecessor_cid, successor_cid, successor_requested, edge_no)`, not on the
lineage hash — which includes `broker_remaining_quantity` and therefore misses
after a sibling fill.

---

## 4. Why the wiring is deferred

**The amendments from round 2 are addressed in §3 EXCEPT where noted below.
This document does not claim to be a ready specification.** Revision 1 ended
with the sentence "the design above satisfies every amendment"; that sentence
was false — four of its five amendment responses were unimplementable or
self-deadlocking, and it is deleted.

### D0 — Two of the blockers are code shape, not evidence

No amount of soak evidence resolves S1 or S2, and both must land before any
wiring:

* **S1** requires editing `_dispatch_alpaca_replaced_deadman_successor` and the
  two gates it delegates to — code on the deadman **protection** path that runs
  for every Alpaca position on every tick, including positions that never take a
  partial. The correct sequencing is a separate, marker-free PR that makes the
  envelope source a parameter and proves byte-identical behaviour with no marker
  anywhere.
* **S2** requires the chokepoint-head reconcile in §3.6. Until it exists, any
  wiring has an oversell/short-flip window in `partial_posting` /
  `partial_indeterminate`.

### D1 — One mandated invariant is unsatisfiable by PATH B, by construction

> *the remainder never lacks protection for more than one tick, and never lacks
> it at all in RTH*

Revision 1 understated this, because it reasoned with an `assess_protection`
that counted the upside limit as cover. The true exposure is larger and it is
not confined to failure branches:

> **From certification until the partial is terminal — the entire life of the
> partial, the normal case, the thing PATH B is for — `f` shares carry NO
> downside stop.** At CANF's numbers that is 106 of 355 shares, ~30% of the
> position, for however long the limit rests. Their only resting order is a sell
> limit at 4.63, *above* the market: on a gap-down the 249 stop fires and the 106
> ride down against a limit that can never be reached.
>
> Additionally, every post-certification failure branch (POST rejected twice,
> POST indeterminate, stale cancel with `k < f`, an un-certified pyramid add)
> leaves `f - k` shares naked until a restore edge certifies or the flatten
> lands — bounded by one lease window (30 s) plus the flatten's own round trip.

Revision 3 makes the code say this too: all five phases of that span are now in
`NAKED_RISK_PHASES` (§3.2, §7.1/N1). Revision 2 stated the exposure here in
prose while its own phase set denied it.

That is a real, irreducible weakening of the deadman guarantee, and it is the
guarantee the deadman exists to provide. **This is the number the operator must
accept in writing**, and it is much larger than revision 1's "naked ≤ 30 s".
Today's alternative — all-or-nothing exits — keeps the full-qty stop at all
times.

### D2 — The idempotency invariant cannot be *tested* under this run's constraints

Fire-once and restart-safety rest entirely on the phase CAS committing in an
**independent transaction before** each dependent HTTP. A DB-free fake claim
store proves call ordering inside the helper; it cannot prove the transactional
property, and that property is exactly what H2's rollback-between-POST-and-commit
deadlock turns on. Tests here are DB-free by mandate, so "idempotent across
restart" can be asserted but not demonstrated — the #1283 shape.

### D3 — The blast radius lands on the protection path itself

D2/D3/D4/D8/D9/D10 all modify code that runs for **every** Alpaca position on
**every** tick. The kill switch gates the decision site (P0) but not the
marker-gated branches — those are byte-identical only while no marker exists,
which is a property of the marker read, not of the flag. The failure mode of a
bug in the D8 `pending_replace` branch is **a genuinely dead stop reported as
`protected: True`** — a naked position that no site reports — reachable from a
single swallowed exception in the claim read.

### D4 — The chokepoint block is now bounded, but the placement is untested

Revision 2 removes the L2 deadlock by narrowing the blocking set, adding the
30 s ceiling, the 300 s marker ceiling and the unconditional operator/EOD/breaker
override. What remains untested here is the **placement**: the block must sit
inside the `if handoff is None:` branch, and the service step must sit above the
handoff-priority block (L3). Both are source-order properties that no DB-free
runtime test can reach; both need the AST guards described in §3.3 plus a live
pulse to confirm.

### What would unblock the wiring, in order

1. **S1 as its own PR**: marker-sourced successor envelope + both dispatch
   gates, proven byte-identical with no marker.
   `tests/test_partial_exit_path_b_lineage_seam.py` is the acceptance test — it
   currently asserts the defect exists and will flip when the fix lands.
2. **S2 as its own PR**: the chokepoint-head sibling reconcile, so
   `_cancel_scale_limit_and_clamp` can never be a silent noop while a sibling is
   live.
3. Operator acceptance, in writing, of the D1 exposure — **`f` shares with no
   downside stop for the whole life of the partial**, not merely "naked ≤ 30 s".
4. A DB-backed test run (`chili_repro2_test`) exercising the claim CAS across a
   simulated rollback between POST and commit (the H2 case).
5. Landing D8/D9/D10 as a **separate, marker-free refactor PR** (extract the
   branch points, prove byte-identical behaviour), so the marker PR's diff is
   additive only.
6. One PAPER soak of the §5 assumptions with the lane capped.

That is four PRs instead of one, and it removes the possibility that taking a
partial flattens a runner.

---

## 5. Assumptions still to verify on PAPER (no new probes were run)

* Alpaca echoes the requested `client_order_id` on the replacement order. The
  design makes this a **hard** requirement (a mismatch is contained by §3.9,
  never left naked), so a mismatch makes PATH B unusable rather than unsafe.
* The successor preserves `sell_to_close` / `gtc` / `extended_hours=False` / the
  same stop price — §3.4a's matcher requires every one of them.
* Hold-release timing: how many pulses after certification the freed qty is
  actually usable. One POST retry covers a single miss.
* Cancelling a stuck `pending_replace` reverts the predecessor to `new`
  (branch (a) of §3.9 depends on it; branch (b) is the fallback if it does not).
* A PATCH on the `R` stop (restore edge) behaves like the first edge.

---

## 6. What this PR ships

* This document (revision 3).
* `app/services/trading/momentum_neural/path_b_partial.py` — pure, no-I/O.
  Revision 2 changes: `blocks_whole_exit` is no longer `is_in_flight` and takes
  an age, a ceiling and an override; `assess_protection` no longer counts an
  upside limit as protection and reports `naked_downside_qty` separately from
  `unhedged_qty_with_resting_sell`; `partial_stale_adopted` and
  `restore_rejected` are no longer terminal and `flatten_queued` / `flattened` /
  `post_deferred` exist so the mandated remedies are recordable;
  `plan_replacement_edge` takes `predecessor_filled_size`;
  `marker_successor_envelope` is new. **Not imported by `live_runner`.**
  Revision 3 changes: `NAKED_RISK_PHASES` re-derived from the corrected checker
  (the five normal-window phases added — §3.2); `reachable_phases` and
  `REMEDY_PHASES` are new, so the R4 invariant is *reachability* rather than
  adjacency; `EXIT_CONSUMPTION_UNRECORDABLE_PHASES` names the one exception
  that revision 2 hid inside a test body.
* `tests/test_partial_exit_path_b_helpers.py` — unit tests, written as
  invariants (`TERMINAL ∩ NAKED_RISK == ∅`, `WHOLE_EXIT_BLOCKING ∩ NAKED_RISK == ∅`,
  every naked phase has a remedy, the CANF steady state is
  `naked_downside = 106`).
* `tests/test_partial_exit_path_b_lineage_seam.py` — **new**: calls the *real*
  `_alpaca_replacement_successor_order_matches` with a fake order and proves S1
  (today's envelope rejects the shrinking edge; the marker envelope accepts it,
  with every other field still strict). Also pins S2, S3, L4 and the
  `partially_filled` lifecycle fact as source guards.
* `tests/test_partial_exit_path_b_unwired.py` — the tripwire, **fixed**: the
  scan root was wrong so it scanned zero files and could not fail. It now
  self-checks that it scans the real production tree (>100 files, `live_runner`
  among them) and carries a positive control (`_ensure_alpaca_deadman_stop` is
  found by the same AST walk). Delete it in the PR that wires PATH B.

No production code path changes. The burst site is untouched.

---

## 7. Amendment conformance

| # | amendment (round 2) | where | status |
|---|---|---|---|
| R0-1 | marker-sourced successor envelope (LR:10101-10116) | §3.4a | specified + seam-tested; **code not landed (D0)** |
| R0-2 | the second gate, LR:10156/10174-10190, via `conservation_holds` | §3.4a gate 2 | specified + source-guarded |
| R0-3 | split containment; delete post-certification whole-close; remove `successor_certified -> replace_stuck` | §3.9, §3.2 | **done in code** (graph) + specified |
| R0-4 | `>= successor_qty + partial_qty`; pyramid add; failure → restore, never containment | §3.5, §3.4c | specified |
| R0-5 | block `partial_posting`/`partial_indeterminate` **or** reconcile at the chokepoint head | §3.6 | **alternative taken**, with the rationale and a test tying the two predicates together |
| R0-6 | age + deadline on the block; operator/EOD/breaker never deferred; marker wall clock | §3.6 | **done in code** |
| R0-7 | head guard uses the OPEN partial; gate the `else` on `scale_limit_is_path_b` | §3.7.5 | specified + source-guarded |
| R0-8 | `assess_protection` stops counting an upside limit as protection | §3.2, D1 | **done in code**; the steady-state test now asserts `naked_downside = 106` |
| R0-9 | TERMINAL ∩ NAKED_RISK == ∅; recordable remedies; delete the "new edge" hand-wave | §3.2, §3.8 | **done in code** |
| R0-10 | `plan_replacement_edge(predecessor_filled_size)` | §3.4b | **done in code** |
| R0-11 | delete "satisfies every amendment"; restate §4 with S1/S2 ahead of the soak | §4, D0 | **done** |
| R0-12 | the six named tests | §6 | **done** (i-vi) |
| R1-1 | fix the tripwire scan root + self-check + positive control | §6 | **done in code** |
| R1-2 | split `blocks_whole_exit` from `is_in_flight`; disjointness test; block inside `if handoff is None:` | §3.2, §3.6 | **done in code** + placement specified |
| R1-3 | service step above the handoff-priority block; marker-aware `pending_replace` branch; source-order AST guard | §3.3 | specified; the guard belongs to the wiring PR |
| R1-4 | separate `_service_path_b_stuck_replace`; `close_shape` is not the fix | §3.9 | specified + source-guarded |
| R1-5 | reachable restore from every naked phase; recordable `consumed_by_exit`; `replace_stuck -> abandoned` | §3.2, §3.8 | **done in code** |
| R1-6 | restate §4 | §4 (D0) | **done** |

### 7.1 Revision 3 — self-audit of revision 2

No third refuter ran; these came from auditing revision 2's shipped artifacts
against each other. All three have the shape the previous rounds kept finding:
a fix landed in one artifact and not in its twin, or an invariant was stated
universally and exempted quietly.

| # | finding | fix | status |
|---|---|---|---|
| N1 | the S4 fix reached `assess_protection` but not `NAKED_RISK_PHASES`; the checker calls the whole normal window `naked_downside=106` while the phase set called it safe — and the phase set is what a wiring consults | five phases added; a test asserts checker and set agree | **done in code** |
| N2 | `consumed_by_exit` was claimed reachable from every unblocked phase (§3.2, module docstring) while the test skipped `replace_stuck` / `containment_queued` with an inline `continue` | `EXIT_CONSUMPTION_UNRECORDABLE_PHASES`, with a stated rationale and its own invariant (still serviced, `-> abandoned`, not naked) | **done in code** |
| N3 | the R4 remedy invariant checked only *direct* targets, too weak to survive N1 (`successor_certified` restores via `post_deferred`) | `reachable_phases` + `REMEDY_PHASES`; the invariant is now reachability | **done in code** |

Both N1 and N3 were negative-controlled: the new tests fail against revision 2's
sets (N1 misses five phases; the adjacency check fails for four phases that
reachability passes). N1 does **not** change D1's conclusion — it makes the
code agree with what D1 already said in prose, which strengthens the case that
the wiring stays deferred until the operator accepts that exposure in writing.
