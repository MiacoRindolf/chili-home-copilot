# NEXT_TASK: position-identity-design-doc-revision

STATUS: DONE

## Goal

Apply the operator's seven answers to `docs/DESIGN/POSITION_IDENTITY.md` so the doc moves from "draft for review" to "ready for Phase 1 implementation." This is a doc-revision pass, not a redesign — the body and the structure stand; the open questions get closed and a few small additions land.

After this task, the next NEXT_TASK is `position-identity-phase-1` — actual code.

## Why now

Operator answered all 4 doc-internal open questions plus 3 review-pass items in a single chat turn. Closing the loop at the doc level (where iteration is cheap) is the agreed path. Phase 1's implementer should read the revised doc as the single source of truth — no chat-only knowledge to translate.

## Operator's 7 answers — verbatim, with what each one closes

| # | Operator answer | Closes |
|---|---|---|
| 1 | "go per recommendation" | § 11.1 stale event tolerance → **Option B** (explicit `sync_gap` event) |
| 2 | "go per recommendation" | § 11.2 backfill atomicity → **Option C** (quarantine ambiguous + bulk for clean cases) |
| 3 | "go per recommendation" | § 11.3 reporting deprecation timeline → **A + B** (dual-write through soak + shim view) |
| 4 | "go per recommendation" | § 11.4 fast-path dependency → **Option B** (decoupled in v1, integrate after Phase 4) |
| 5 | *"shorter, it's tooo long. SHOOORTER"* | Phase 5 soak compresses; total initiative time drops |
| 6 | *"Yes add data struct for shorts"* | Direction handling promoted to a first-class schema concern |
| 7 | *"include"* | PaperTrade migrates in this initiative, not deferred |

## Path

### Step 1 — Close § 11.1 — § 11.4 with operator answers

For each of § 11.1, § 11.2, § 11.3, § 11.4:

- Replace the multi-option framing with a single subsection titled `Decision: <Option>`.
- Move the resulting design choice into the body of the doc where it applies (e.g., § 5.1 event taxonomy adds `sync_gap` semantics; § 8.2 Phase 2 exit criteria reference the quarantine view; § 8.5 Phase 5 specifies dual-write + shim view; § 13 captures fast-path as decoupled-v1).
- The § 11 section itself shrinks to a single subsection per answered question with the heading `Decision (closed YYYY-MM-DD): <Option>` and a one-line rationale. Keeps the audit trail of what was decided when, without leaving open-question prose hanging in a "ready for implementation" doc.

### Step 2 — Compress Phase 5 soak duration

Operator's signal: *"shorter, it's tooo long. SHOOORTER"* — 1 quarter is overkill for a solo-dev reporting surface. New target: **2 weeks**.

Apply across the doc:

- § 8.5 Phase 5 — soak duration `1 quarter` → `2 weeks`. Update exit criteria's "1 quarter of dual-write" language to "2 weeks of dual-write."
- § 15 cost estimate table — Phase 5 soak `1 quarter` → `2 weeks`. Recompute the totals row.
- Anywhere else the doc references "quarter" or "Q3" as a Phase 5 marker — replace with the 2-week target.

The total initiative soak drops from ~2.25 quarters to roughly 9 weeks across all phases. Operator review: confirm this matches the "shorter" intent or signal-back if even-shorter is wanted.

### Step 3 — Add direction handling for shorts

Operator's signal: *"Yes add data struct for shorts"* — direction becomes a first-class schema concern, not an open question.

Apply:

- **Natural key change**: `trading_positions` natural key adds `direction VARCHAR(10) NOT NULL DEFAULT 'long'`. New unique constraint: `UNIQUE(user_id, broker_source, account_type, ticker, direction)`.
- **DDL § 7.1**: add the `direction` column with the existing `Trade.direction` semantic (`'long'` / `'short'`); CHECK constraint `CHECK (direction IN ('long', 'short'))`.
- **§ 4.1 narrative**: explain the choice — separate position rows per direction means a long 100 AAPL + short 50 AAPL situation is two positions with two `id`s, not one signed-quantity row. This matches the broker's own representation (Robinhood and Coinbase both report long and short as distinct positions where supported) and avoids signed-arithmetic bugs at every consumer.
- **§ 6.1 mapping**: row for today's `Trade.direction` resolves to `position.direction` (the natural-key column) + `envelope.direction` (denormalized for queries, matches today's Trade.direction).
- **§ 11 (formerly Open Questions)**: the in-line note at § 6.1 row 3 about short positions resolves; remove the "open question" mention.
- **Brain context flow § 12**: confirm `compute_bracket_intent` accepts direction; the brain function signature already takes `direction`, so no contract change. Add a one-line note that long/short bracket logic differs (stop above entry for short; below for long) and the engine reads direction from the position row.

Today's data is essentially all `direction='long'`; the column with default `'long'` migrates cleanly. The schema is ready for the operator's eventual short trades (perps via Hyperliquid/dYdX/Kraken Futures) without a future ALTER.

### Step 4 — Include PaperTrade in Phase 1 scope

Operator's signal: *"include"* — PaperTrade is part of this initiative, not a future-phase deferral.

Apply:

- **§ 4.1 account_type narrative**: paper-mode positions get `account_type='paper'` in the same `trading_positions` table. Paper and live separate by account_type; no separate "trading_paper_positions" table.
- **NEW § 6.4 — `paper_trades` column-by-column mapping**: same shape as § 6.1 (Trade → split). Map every column on today's `paper_trades` ORM (`app/models/trading.py:1022+`) to its post-refactor home. Most columns mirror Trade's mapping; flag any paper-specific columns that need a paper-only home.
- **§ 8.1 Phase 1 scope**: backfill includes paper trades. Backfill script walks `paper_trades` AND `trading_trades` to seed `trading_positions` rows.
- **§ 8.1 Phase 1 exit criteria**: add row "paper-mode positions covered by the audit query at parity with live positions."
- **§ 11.4 fast-path decision (now closed)**: reaffirm fast-path stays decoupled despite paper-mode inclusion. Paper-mode in the LIVE trading stack (PaperTrade rows from `auto_trader.py` paper paths) IS in scope; fast-path's `fast_executions` etc. are separate and stay decoupled.
- **Glossary § 14**: add `paper-mode position` as an entry — a position where `account_type='paper'` and broker truth is simulated rather than real.

### Step 5 — Update § 11 to reflect closed status

The § 11 section's purpose was "open questions for operator." All four are now closed. New § 11 shape:

```markdown
## 11. Decisions closed (post-operator-review on YYYY-MM-DD)

### 11.1 Stale event tolerance — Decision: B (explicit sync_gap event)
[one-line rationale]

### 11.2 Backfill atomicity — Decision: C (quarantine ambiguous + bulk for clean cases)
[one-line rationale]

### 11.3 Reporting deprecation timeline — Decision: A + B (dual-write through soak + shim view)
[one-line rationale + new soak duration]

### 11.4 Fast-path dependency — Decision: B (decoupled in v1, integrate after Phase 4)
[one-line rationale]
```

The decisions go into the implementation-relevant body sections (per Step 1); § 11 is the audit log of what was decided when.

### Step 6 — Update § 15 cost estimate

Per Step 2 (Phase 5 soak) AND Step 4 (PaperTrade in Phase 1 scope) AND Step 3 (direction column in natural key):

- Phase 1 LOC estimate increases slightly (~50-100 LOC for paper-mode coverage in backfill + audit queries; ~10 LOC for direction column).
- Phase 5 soak drops from `1 quarter` to `2 weeks`.
- Total soak drops accordingly.
- Recompute the totals row.

### Step 7 — Update CURRENT_PLAN.md

Replace the 6-phase sketch in `docs/STRATEGY/CURRENT_PLAN.md` with a forward pointer:

```markdown
## Migration plan

See `docs/DESIGN/POSITION_IDENTITY.md` § 8 for the authoritative phase plan + per-phase exit criteria. CURRENT_PLAN keeps initiative-level orientation; the design doc is the single source of truth for technical specifics.
```

The Open architectural concerns + 2026-05-02 findings sections of CURRENT_PLAN can stay as initiative context; only the migration sketch gets replaced.

### Step 8 — Add `Decisions closed` to the doc's frontmatter

Top of the doc, after the Status line, add:

```markdown
**Decisions closed:** 2026-05-04 — operator-reviewed § 11.1–11.4 + Phase 5 soak compression + direction-for-shorts schema + PaperTrade inclusion. See § 11 for the decision audit log and § 8 for the implementation-relevant phase plan.
```

Signals to anyone reading the doc cold that the open questions are closed; the doc is implementation-ready.

## Constraints / do not touch

- **No new design decisions.** This is an answer-application pass. If the executor finds a question that genuinely needs a new decision (not implied by the 7 answers), surface it in the CC report rather than answering it.
- **No code changes.** Doc-only.
- **No schema migrations.** Doc-only.
- **Keep the existing doc structure.** Section numbering (§ 1, § 2, etc.) stays. Body text stays where it is unless an answer requires moving it.
- **Cite, don't speculate.** Every "operator stated X" claim references the chat turn or this brief verbatim.
- **No magic numbers introduced** by the revision (the existing magic-number-equivalent audit in CC report stays clean).
- **PROTOCOL Hard Rules apply** (no `git push --force`, no force-push to main, etc.).

## Out of scope

- **Phase 1 implementation.** That's the next NEXT_TASK after this revision lands.
- **Operator pre-actions** (kill switch reset, EKSO/ELTX P/L cleanup) — operator-driven, decoupled.
- **Reviewing the doc's body claims for correctness.** The body was reviewed in the prior CC + COWORK_REVIEW pass. This task only applies the 7 answers; it doesn't re-litigate other content.
- **Phase 6 multi-leg-order language tightening** (called out in the prior COWORK_REVIEW). Worth doing but not part of this brief; queue as a follow-up doc revision if the operator wants it before Phase 6.

## Success criteria

1. **Two commits, both pushed:**
   - `docs(strategy): position-identity design doc revision — close 4 open questions + direction-for-shorts + PaperTrade scope + Phase 5 soak compression`
   - `docs(strategy): position-identity-design-doc-revision CC report + mark NEXT_TASK done`
2. **Doc revision applied.** The revised `docs/DESIGN/POSITION_IDENTITY.md` shows:
   - § 11 reshaped to "Decisions closed" with the 4 answers
   - § 4.1 + § 7.1 DDL include `direction` in natural key
   - § 6.4 NEW section maps `paper_trades` columns
   - § 8.1 Phase 1 scope includes PaperTrade backfill + audit
   - § 8.5 Phase 5 soak says `2 weeks` (not 1 quarter)
   - § 15 totals recomputed
   - Frontmatter notes `Decisions closed: 2026-05-04`
3. **CURRENT_PLAN.md updated** with the forward-pointer to the design doc § 8.
4. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-04_position-identity-design-doc-revision.md` per PROTOCOL format. Include: a diff-summary of what changed in the doc, any new ambiguity surfaced (none expected), the new total soak duration vs the prior estimate.
5. **Word-count sanity check**: revised doc is roughly 6000-6500 words (prior was 5834; revisions add ~200-500 words for direction handling + PaperTrade + Decision sections).

## Rollback plan

- **Code rollback**: `git revert <revision-commit>`. Reverts to the prior doc + CURRENT_PLAN. The decisions become open questions again, and Phase 1 has to re-answer them.
- **No migration to roll back.**
- **No live broker rollback needed.**

## Verification commands (for the executor)

```bash
# After commits land, doc revision applied
grep -c "Decisions closed" docs/DESIGN/POSITION_IDENTITY.md
# Expect: at least 1

grep -c "direction" docs/DESIGN/POSITION_IDENTITY.md
# Expect: > 5 mentions (was minimal before)

grep -c "paper_trades\|paper-mode" docs/DESIGN/POSITION_IDENTITY.md
# Expect: > 5 mentions

grep -c "2 weeks" docs/DESIGN/POSITION_IDENTITY.md
# Expect: > 1 (Phase 5 + the recomputed total)

grep -c "1 quarter" docs/DESIGN/POSITION_IDENTITY.md
# Expect: 0 (the prior reference is replaced)

grep "See \`docs/DESIGN/POSITION_IDENTITY.md\`" docs/STRATEGY/CURRENT_PLAN.md
# Expect: 1 hit (the forward pointer)

wc -w docs/DESIGN/POSITION_IDENTITY.md
# Expect: roughly 6000-6500
```

## Open questions for Cowork (surface in your CC_REPORT only if relevant)

1. **PaperTrade-specific columns.** Some columns on today's `paper_trades` ORM may not have direct analogs on `trading_trades` (e.g., paper-only simulation hooks, fake-fill semantics). The CC report should list any paper-specific column that doesn't fit the three-layer split cleanly, with the executor's proposed home.
2. **Direction column on envelope vs decision.** The brief's § 6.1 update says envelope.direction is denormalized for queries. If the executor finds a stronger reason to put direction ONLY on position (not envelope), surface and recommend.
3. **The Phase 6 multi-leg-order language** (called out in the prior review) is explicitly OUT of scope for this revision. If the executor finds the language is broken in a way that affects the Phase 5/6 boundary, surface it; otherwise leave for a follow-up.

## Forward pointer

After this revision lands, the next NEXT_TASK is **`position-identity-phase-1`** — actual implementation of the `trading_positions` table, the `trading_position_events` table, the shadow-mode write path in `broker_service.sync_positions_to_db`, the DROP of mig 223's orphan column, and the backfill script that covers BOTH `trading_trades` and `paper_trades`. Phase 1's exit criteria from the revised § 8.1 become Phase 1's success criteria directly.
