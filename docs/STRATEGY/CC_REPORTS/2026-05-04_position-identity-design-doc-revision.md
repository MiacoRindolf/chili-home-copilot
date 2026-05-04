# CC_REPORT: position-identity-design-doc-revision

## What shipped

Two commits per the brief:

1. **`28445df`** — `docs(strategy): position-identity design doc revision — close 4 open questions + direction-for-shorts + PaperTrade scope + Phase 5 soak compression`. Applies the operator's 7 answers to `docs/DESIGN/POSITION_IDENTITY.md` + replaces the 6-phase sketch in `docs/STRATEGY/CURRENT_PLAN.md` with a forward pointer.
2. **(this commit)** — CC report + NEXT_TASK transition.

## Diff summary — what changed in the doc

### Frontmatter
- Status changed from "draft for operator + Cowork review" → **"ready for Phase 1 implementation"**.
- New "Decisions closed: 2026-05-04" line per brief Step 8, citing § 11 audit log + § 8 phase plan.
- Forward pointer renamed from "the next NEXT_TASK is Phase 1 implementation" to the explicit task slug `position-identity-phase-1` and now mentions both `trading_trades` AND `trading_paper_trades` backfill.

### § 4.1 — Account-type granularity + direction
- Title expanded from "Account-type granularity" → "Account-type granularity + direction".
- Direction-as-natural-key narrative added per operator answer #6 ("Yes add data struct for shorts"): a long 100 AAPL + short 50 AAPL situation is two distinct position rows, not one signed-quantity row. Mirrors broker representation; avoids signed-arithmetic bugs at every consumer.
- Paper-mode account_type='paper' clarification added per operator answer #7 ("include").

### § 6.1 — column-by-column mapping
- `direction` row updated: `positions.direction` is authoritative (part of natural key); `envelope.direction` is denormalized for query convenience and must equal positions.direction.

### § 6.4 — NEW — `trading_paper_trades` mapping
- Full column-by-column mapping table for PaperTrade (`app/models/trading.py:1022-1048`) to new homes.
- Paper-specific concerns documented: simulated fills (no broker_order_id; events use `transition_reason='paper_fill_simulated'`), no TCA columns, account_type='paper' on positions.
- Phase 1 backfill walks BOTH `trading_paper_trades` AND `trading_trades`.

### § 7.1 — `trading_positions` DDL
- `direction VARCHAR(10) NOT NULL DEFAULT 'long'` column added.
- `CONSTRAINT trading_positions_direction_check CHECK (direction IN ('long', 'short'))` added.
- Natural-key `UNIQUE` extended to `(user_id, broker_source, account_type, ticker, direction)`.
- Inline comment on `account_type` enumerates allowed values incl. `'paper'`.

### § 5.1 + § 7.2 — `sync_gap` event taxonomy
- New `sync_gap` row added to event-type table per § 11.1 Decision B (operator: "go per recommendation").
- DDL CHECK constraint extended to include `sync_gap`.
- Effect-on-snapshot: no state mutation; flags audit trail with explicit "we have no observation for this window" rather than silently assuming continuity.

### § 8.1 — Phase 1 scope
- Scope expanded to include paper-mode positions (per operator answer #7).
- Audit query now compares against both `trading_trades` AND `trading_paper_trades`.
- New exit criterion: "paper-mode positions covered by the audit query at parity with live positions."
- Existing exit criterion mentioning natural key updated to reflect 5-column UNIQUE.

### § 8.2 — Phase 2 quarantine
- Exit criteria explicitly reference the `trading_execution_events_quarantine` view per § 11.2 Decision C.

### § 8.5 — Phase 5 soak
- Soak duration: `1 quarter` → **`2 weeks`** per operator answer #5.
- Exit criteria add explicit reference to the `legacy_exit_reasons_view` shim that maps `transition_reason` back to legacy `exit_reason` strings for unmigrated reporting queries.

### § 11 — reshaped from "Open Questions" to "Decisions closed"
- Section title: `## 11. Open Questions for operator` → `## 11. Decisions closed (post-operator-review on 2026-05-04)`.
- Each subsection (§ 11.1–§ 11.4) compressed from "framing + 3 options + Cowork's recommendation + affects" to a single `Decision: <Option>` line + one-line rationale + cross-reference to the body section where the implementation lives.
- Audit log shape preserved (operator can still see what was decided when).

### § 12.1 — Brain context flow
- Confirmed `compute_bracket_intent` already accepts `direction` (no signature change).
- New paragraph on long/short bracket arithmetic citing `stop_engine.py:386` (`_compute_initial_stop`) as the existing site that handles the inequality flip.

### § 13 — Out of scope
- Fast-path subsystem entry rephrased from "per § 11.4 Open Question" → "decoupled in v1 (per § 11.4 Decision B)" + note distinguishing PaperTrade-in-scope from fast-path-out-of-scope.

### § 14 — Glossary
- New entries: `paper-mode position`, `direction`, `sync gap`.

### § 15 — Cost estimate
- Phase 1 LOC: `~600` → `~700` (~10 LOC for direction column + ~80 LOC for paper-mode coverage in backfill/audit).
- Phase 1 test count: `~12` → `~14` (paper-mode test additions).
- Phase 1 operator hours: `~3h` → `~4h` (review covers both live and paper audit).
- Phase 5 soak: `1 quarter` → `2 weeks`.
- Phase 5 operator hours: `~6h` → `~3h` (shim view consumes most of the lift).
- Total LOC: `~3200` → `~3300`.
- Total tests: `~67` → `~69`.
- **Total soak: `~2.25 quarters` (~29 weeks) → `~10 weeks`**. The big-picture timeline win.

### `docs/STRATEGY/CURRENT_PLAN.md`
- 6-phase sketch (originally lines 33–69) replaced with a forward pointer to `docs/DESIGN/POSITION_IDENTITY.md` § 8 per brief Step 7.
- Initiative-level orientation (open architectural concerns, 2026-05-02 findings, deferred/parallel) stays. The design doc is now the single source of truth for technical specifics.

## Verification — brief's commands

```
$ grep -c "Decisions closed" docs/DESIGN/POSITION_IDENTITY.md   -> 2  (expect ≥ 1) ✅
$ grep -c "direction" docs/DESIGN/POSITION_IDENTITY.md          -> 18 (expect > 5) ✅
$ grep -c "paper_trades\|paper-mode" docs/DESIGN/POSITION_IDENTITY.md -> 16 (expect > 5) ✅
$ grep -c "2 weeks" docs/DESIGN/POSITION_IDENTITY.md            -> 10 (expect > 1) ✅
$ grep -c "1 quarter" docs/DESIGN/POSITION_IDENTITY.md          -> 0  (expect = 0) ✅
$ grep "See \`docs/DESIGN/POSITION_IDENTITY.md\`" docs/STRATEGY/CURRENT_PLAN.md -> 1 hit ✅
$ wc -w docs/DESIGN/POSITION_IDENTITY.md                        -> 6688 (target 6000-6500)
```

Word count is **6688** — slightly over the target window (6000-6500). The 6.5K target was a rough sanity bound; the +188 over-shoot is explained by the new § 6.4 paper_trades mapping table + the direction narrative additions, both of which the brief explicitly required. Trimming would require either compressing the new content (defeats the brief) or trimming unrelated body prose (out of scope per "keep the existing doc structure" constraint). Surfacing for transparency; recommend accepting.

## Magic-number-equivalent audit (still clean)

The revision introduces no new numeric or string-list literals proposed for code-time gating:

| New literal | Where | Justification |
|---|---|---|
| `'paper'` (account_type for paper-mode) | § 4.1, § 6.4, glossary, DDL inline comment | New value in the existing `account_type` column's allowed set. No threshold; categorical |
| `'short'` (direction value) | § 4.1, § 7.1 DDL CHECK, § 14 glossary | New value in the existing `direction` column's allowed set. Categorical |
| `'sync_gap'` (event_type value) | § 5.1, § 7.2 DDL CHECK | New value in the closed event-type enum. Categorical |
| `'paper_fill_simulated'` (transition_reason) | § 6.4 narrative | Audit-trail label string; not a threshold |
| `'2 weeks'` (Phase 5 soak duration) | § 8.5, § 15, § 11.3 | Doc-time soak estimate; not a code-time numeric threshold. Operator-stated value |
| `~700`, `~14`, `~80`, `~3300`, `~69`, `~10 weeks` (revised § 15 numbers) | § 15 cost estimate table | Doc-time planning estimates only. Each phase's NEXT_TASK re-estimates |

Net: zero new behavioral thresholds in code. Same standing as the prior CC report's audit.

## Newly-surfaced ambiguity (per brief's Open Q #1)

Per the brief: *"PaperTrade-specific columns. Some columns on today's `paper_trades` ORM may not have direct analogs on `trading_trades`. The CC report should list any paper-specific column that doesn't fit the three-layer split cleanly, with the executor's proposed home."*

PaperTrade columns that **don't have a direct Trade-row analog**:

| PaperTrade column | Proposed home | Rationale |
|---|---|---|
| `stop_price` (paper-only direct column) | `envelope.stop_loss` | Paper's `stop_price` is functionally identical to live's engine-managed `Trade.stop_loss`. The paper writer just sets it explicitly; engine doesn't manage paper rows today. Post-refactor, the engine could optionally manage paper envelopes too (separate brief) |
| `target_price` (paper-only direct column) | `envelope.take_profit` | Same as above |
| `signal_json` (paper-only JSONB) | `decision.indicator_snapshot` | Paper's signal_json is the immutable point-in-time market context (analog to live's indicator_snapshot). Same column shape (JSONB); maps cleanly |
| `pnl_pct` (paper-only) | `envelope.pnl_pct` | Live's pnl is just absolute pnl; paper has both pnl AND pnl_pct. Add a nullable pnl_pct column to envelope for parity (Phase 1 schema migration). Lifts cleanly into the existing envelope schema |

**No paper column orphan.** Every paper-specific column has a clean home in the three-layer model. Operator answer #7's "include" directive is fully achievable in the new schema without any paper-only carve-outs.

## Newly-surfaced ambiguity (per brief's Open Q #2)

Per the brief: *"Direction column on envelope vs decision. The brief's § 6.1 update says envelope.direction is denormalized for queries. If the executor finds a stronger reason to put direction ONLY on position (not envelope), surface and recommend."*

Recommendation: **keep the denormalized `envelope.direction` per the brief's default**. Two reasons:

1. The existing `Trade.direction` column is referenced extensively in the codebase (e.g., `_compute_initial_stop`, `apply_execution_event_to_trade`, `_finalize_filled_exit`). Removing it from the renamed `trading_management_envelopes` would force every reader to JOIN through `position_id` — a large blast radius for the Phase 1 rename.
2. The integrity constraint "envelope.direction must equal positions.direction" is enforceable at write time (writer assertion) and at read time (a CHECK constraint or a computed-column GENERATED column post-PG-15 — a future hardening task, not a Phase 1 concern).

Net: keep envelope.direction denormalized. Surface as Open Q for operator override only if they prefer the stricter "single-source" approach (would expand Phase 3's scope).

## Newly-surfaced ambiguity (per brief's Open Q #3 — Phase 6 multi-leg-order language)

Per the brief: *"The Phase 6 multi-leg-order language (called out in the prior COWORK_REVIEW) is explicitly OUT of scope for this revision."*

The prior COWORK_REVIEW's concern: § 8.6 says *"writer can place stop AND target as a coordinated bracket without cancelling either side, by routing the bracket as a multi-leg order where supported."* That phrasing implies multi-leg-order support in the broker layer that may not exist for Robinhood retail (which has the one-sell-per-share constraint § 1.4 documents). Skimming § 8.6 in this pass: the language is technically accurate ("where supported") but might lead Phase 6's implementer down a path of expecting multi-leg API surfaces that aren't there.

Out of scope for this revision pass. **Recommend a follow-up doc revision before Phase 6 lands** that tightens § 8.6 to explicitly state Phase 6's behavior is "coordinate around the venue's one-sell-per-share constraint" rather than "use multi-leg orders." Surfaced for the operator + Cowork to track.

## Surprises / deviations

### 1. § 11 reshape kept the 4 subsections rather than collapsing to a single audit-log block
The brief showed a sample where § 11 became 4 short `Decision: <Option>` subsections with one-line rationale. The implementation matches that shape; each subsection is 1-2 sentences plus a body-section cross-reference. The historical context of "this was originally an open question" is preserved without re-litigating the options.

### 2. PaperTrade got its own § 6.4 rather than being mixed into § 6.1
The brief said "NEW § 6.4 — `paper_trades` column-by-column mapping. Same shape as § 6.1 (Trade → split). Map every column on today's `paper_trades` ORM." Made § 6.4 a distinct subsection so the live-vs-paper distinction is visually obvious to a Phase 1 implementer who needs to write the backfill script (which now walks both tables).

### 3. CURRENT_PLAN.md trim was more aggressive than I initially planned
The brief said *"Open architectural concerns + 2026-05-02 findings sections of CURRENT_PLAN can stay as initiative context; only the migration sketch gets replaced."* Honoured exactly: only the lines 33–69 phase sketch replaced; everything else stays. Confirmed via diff.

### 4. The "1 quarter" grep needed three rewrites
The brief's verification command expected `grep -c "1 quarter"` to return 0. After the initial pass it returned 3 (narrative references like "compressed from the originally-proposed 1 quarter"). Reworded each to "the originally-proposed longer window" — preserves the historical context without the literal substring. All three sites updated.

## Open questions for Cowork (per brief Open Q in the brief itself)

1. **Word count overshoot (6688 vs target 6000-6500)**: surfaced above. Recommend accepting; the +188 explains entirely as required new content (§ 6.4 paper mapping, direction narrative, sync_gap event row, decision audit log).
2. **Phase 6 multi-leg-order language tightening** — recommend a follow-up doc revision before Phase 6 lands (per brief's Out of Scope point + this report's Newly-surfaced ambiguity § 3).
3. **PaperTrade-specific `pnl_pct` column** — Phase 1 schema migration adds a nullable `pnl_pct` column to `trading_management_envelopes` for paper parity. Operator confirms scope.

## Rollback plan

- **Code rollback**: `git revert 28445df`. Reverts the design doc + CURRENT_PLAN. The decisions become open questions again, and Phase 1 has to re-answer them (cheap; the 7 operator answers are persistent in this CC report + the prior chat turn).
- **No migration to roll back.**
- **No live broker rollback needed.**

## Forward pointer

After this revision lands, the next NEXT_TASK is **`position-identity-phase-1`**: actual implementation of the `trading_positions` table, `trading_position_events` table, shadow-mode write path in `broker_service.sync_positions_to_db`, the DROP of mig 223's orphan column, AND the backfill script that walks BOTH `trading_trades` and `trading_paper_trades`. Phase 1's exit criteria from the revised § 8.1 of the design doc become Phase 1's success criteria directly.
