# COWORK_REVIEW: position-identity-design-doc-revision

## Verdict

Clean answer-application pass. All 7 operator answers landed in the right body sections (not just § 11), the open-questions section reshaped to a decision audit log, the cost-estimate table recomputed, and `CURRENT_PLAN.md` updated with a single forward pointer. The grep-based verification commands from the brief all pass.

The big-picture result: **total initiative soak compressed from ~29 weeks to ~10 weeks** (per § 15 in the revised doc). That's the operator's "SHOOORTER" intent realized concretely. Phase 1's scope grew by ~100 LOC + 2 tests + 1 operator-hour for PaperTrade coverage; everything else compressed or stayed flat. Net: smaller, faster, more focused.

## Algo-trader lens

**What's good.** Direction-as-natural-key resolves the prior open note in § 6.1 row 3 cleanly — long 100 + short 50 = two distinct position rows, mirroring broker representation. Avoids signed-arithmetic bugs at every consumer (`current_quantity` stays positive everywhere; consumers don't need a `IF direction = 'short' THEN -qty ELSE +qty` walk). Schema is now ready for the operator's eventual perps-with-shorts trades (Hyperliquid/dYdX/Kraken Futures) without a future ALTER. Today's data is essentially all `direction='long'`; the column with default `'long'` migrates without drama.

PaperTrade inclusion + the new § 6.4 mapping is the right choice. Paper-mode positions live in the same `trading_positions` table with `account_type='paper'`. The CC's audit of paper-specific columns found NO orphans — every paper-only column has a clean home in the three-layer model. Phase 1's backfill walks both `trading_trades` and `trading_paper_trades` so the position layer covers paper-mode from day one.

The `sync_gap` event taxonomy entry (per § 11.1 Decision B) is the right shape — explicit "we have no observation for this window" rather than silent continuity. Aligns with the no-magic-numbers principle and makes broker-auth-flap windows auditable.

**What's narrow.** Phase 5's 2-week soak is aggressive for a quarter-long-spec'd phase. Acceptable for a solo-dev reporting surface, but if the operator has any external dashboards or downstream consumers (analytics warehouse, BI tool) those will need migration in 2 weeks. The CC report doesn't flag any external consumer; if the operator has them, this is the spot where Phase 5 will pinch.

The CC's recommendation on the `pnl_pct` column (§ Newly-surfaced Open Q #3 in the report) — adding a nullable `pnl_pct` to the renamed envelope table for PaperTrade parity — is a small Phase 1 schema scope expansion that wasn't called out in the brief explicitly. It's the right answer (paper currently tracks pct; live currently doesn't; null-for-live is harmless). Worth surfacing to operator for explicit acknowledgement before Phase 1 ships.

## Dev-architect lens

**What's good.** Verification commands all pass. Magic-number-equivalent audit stayed clean: all new literals are categorical enum values (`'paper'`, `'short'`, `'sync_gap'`, `'paper_fill_simulated'`) or doc-time soak/LOC estimates. Zero behavioral thresholds added.

Frontmatter status changed from "draft for review" to "ready for Phase 1 implementation" with a `Decisions closed: 2026-05-04` marker. The doc reads to a fresh implementer as implementation-ready, not as draft-with-open-questions.

The CC honestly surfaced three follow-up items in its Open Questions section: word-count overshoot (cosmetic), Phase 6 multi-leg-order language (real concern, deferred), pnl_pct column scope (real, needs operator acknowledgement). All three are noted-not-resolved — the right discipline for a tightly-scoped revision pass.

CURRENT_PLAN.md trim was minimal — only the 6-phase sketch lines (33-69 in original) got replaced with the forward pointer. Initiative-level orientation, open architectural concerns, and 2026-05-02 findings all stayed. Single source of truth pattern preserved without losing initiative-level context.

**What's concerning.**

1. **Word count discrepancy**: CC report says 6688 words; my own `wc -w` against the committed file says 5797. Possible explanations: encoding difference, line-ending count, or CC's count was on a different snapshot. Doesn't affect content quality but worth noting — when verifying future doc-revision tasks, settle on the canonical count tool to avoid this mismatch.

2. **The `pnl_pct` column expansion** (CC Open Q #3): adding a nullable column to the renamed envelope table is a clean migration but it's a scope item the brief didn't explicitly authorize. CC's recommendation (add it in Phase 1 for paper-parity) is correct. Operator should explicitly bless the column addition before Phase 1's migration script is written, or surface a different choice (drop pct from paper, compute on-the-fly, etc.).

3. **§ 11.1 sync_gap event detection logic** is now committed in the doc but not yet detailed at the implementation level. The Phase 1 brief needs to specify the gap-detection threshold (today's `sync_positions_to_db` runs every 2 minutes; what duration of observed gap warrants a `sync_gap` event?). Recommend Phase 1 emit `sync_gap` whenever the prior position-event for that position is older than the broker-sync cron interval × 2 (i.e., ~4 minutes). That derives from observable system state (the cron interval is configured in the scheduler) — no new magic number. Phase 1 brief should make this explicit.

4. **Phase 6 multi-leg-order language**: explicitly out of scope per the brief. CC's recommendation is a follow-up doc revision before Phase 6 lands. Tracked. Not blocking Phase 1.

## Decisions for the operator

1. **Confirm `pnl_pct` column addition to renamed envelope table.** Default recommendation: add the column nullable in Phase 1. Acknowledge or veto.
2. **Confirm sync_gap detection threshold derivation.** The Phase 1 brief proposes "2× the broker_sync cron interval" — derive from existing config, no magic number. Acknowledge or signal a different derivation.
3. **Confirm aggressive 2-week Phase 5 soak.** If you have external dashboards or downstream BI consumers, 2 weeks is tight; if this is a solo-dev surface, fine. Acknowledge.
4. **Operator pre-actions still outstanding** from earlier today:
   - Kill switch reset (you chose Path A — reset whenever).
   - EKSO/ELTX P/L cleanup (-$71.80; backfill or accept).

## Recommended next move

Phase 1 implementation brief is ready to stage. The doc § 7.1 + § 8.1 are concrete enough that Phase 1's `_migration_NNN_*` function can be written straight from the DDL.

Three small operator acknowledgements before Phase 1 starts (per Decisions section above) — none of them block; they can be handled inline in the Phase 1 brief as "operator-confirms-on-PR-review" rather than blocking chat-level decisions. Operator can answer in-thread as Phase 1 lands.

I'll stage Phase 1 next, with the three confirmation items flagged inline. Operator reviews on PR.

## Status of NEXT_TASK.md

CC marked DONE for `position-identity-design-doc-revision`. Ready for the next staging.
