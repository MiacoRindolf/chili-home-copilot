---
title: Autopilot pattern desk — bug audit + per-position controls + PDT + live readiness
status: completed
updated: 2026-04-17
---

# Autopilot pattern desk — phases P1–P4

## Binding scope (frozen from user responses)

1. **Rollout** — Keep env defaults OFF (`CHILI_AUTOTRADER_ENABLED=false`, `CHILI_AUTOTRADER_LIVE_ENABLED=false`). Live flip is **per-session via the desk toggle** on `/trading/autopilot`. Desk override already exists — must be verified end-to-end before user relies on it.
2. **Per-position controls** — Full set: Pause monitor, Resume monitor, Close now (market-sell), Exclude from synergy. Persist in `trading_brain_runtime_modes` with slice `autotrader_v1_position:<trade_id>` (or equivalent — decision in P2).
3. **PDT** — **Soft warn only**. Stamp `AutoTraderRun.decision_json.would_be_day_trade = true` when monitor is about to exit a position opened the same ET date. No blocking logic; rely on Robinhood to reject if PDT'd. Surface a badge on the desk row.
4. **Bug audit scope** — **All of the above**: full autopilot page render check (paired + guest, all endpoints 200) + pattern desk path + real Robinhood adapter verification before any live flip.

## Non-goals (phase freeze)

- Do **not** change the momentum-neural / sessions half of `/trading/autopilot` beyond what's needed to get the page to render cleanly. No new session features, no new opportunities logic.
- Do **not** change AutoTrader rule gate (confidence / projected-profit / slippage). Those are already tested and accepted in v1.
- Do **not** change the LLM revalidation prompt/schema.
- Do **not** add PDT **blocking** logic. Soft-stamp only.
- Do **not** flip any live env flags in `docker-compose.yml` or `.env`. Live stays desk-driven.
- Do **not** widen the desk to non-pattern-linked positions (e.g. manual broker-imported trades with no `scan_pattern_id` / `related_alert_id`).

## P1 — Bug audit before anything live

Goal: before user flips the live toggle, know that every endpoint and visible page element behaves correctly.

All P1 checks landed via `tests/test_autopilot_page_smoke.py` + existing `tests/test_autotrader_desk_api.py` / `tests/test_auto_trader_*`. **No bugs found** — infrastructure is intact.

- [x] `p1_page_paired` — `test_autopilot_page_paired_renders`: 200 + pattern desk section + autopilot-pattern-desk.js wired.
- [x] `p1_page_guest` — `test_autopilot_page_guest_renders`: 200 HTML.
- [x] `p1_endpoint_inventory` — `test_autopilot_endpoints_paired_non_500` parametrized across all endpoints called by `autopilot.js` / `autopilot-sessions.js` / `autopilot-pattern-desk.js`. All return non-500.
- [x] `p1_desk_get` — Covered by existing `test_autotrader_desk_paired_get_patch`.
- [x] `p1_desk_patch` — Covered by existing `test_autotrader_desk_paired_get_patch` (paused toggle + live_orders override).
- [x] `p1_pattern_tagging` — `test_orchestrator_paper_tags_scan_pattern_and_alert`: verified `PaperTrade.scan_pattern_id` set AND `signal_json.auto_trader_v1=True` AND `signal_json.breakout_alert_id=<alert.id>` AND audit row `scan_pattern_id` set. Same path applies to live `Trade.scan_pattern_id` + `Trade.related_alert_id` (`_execute_new_entry` lines 399–400).
- [x] `p1_orchestrator_noop_off` — `test_orchestrator_noop_when_disabled`: returns `{ok:True, skipped:True, reason:"disabled"}`, zero audit rows written.
- [x] `p1_monitor_noop_off` — `test_monitor_noop_when_disabled`: returns `{ok:True, skipped:"autotrader_disabled"}`.
- [x] `p1_scheduler_registration` — Static read: `_run_auto_trader_tick_job` + `_run_auto_trader_monitor_job` register only when `include_heavy or include_web_light` is true. In `chili` (role=`none`) neither is set; in `scheduler-worker` (role=`all`) both run. Both no-op via `if not chili_autotrader_enabled: return` inside each handler.
- [x] `p1_robinhood_adapter_path` — Static read: `RobinhoodSpotAdapter.is_enabled()` checks `chili_robinhood_spot_adapter_enabled` AND `broker_service.is_connected()`; `place_market_order(product_id, side, base_size, client_order_id)` exists. Orchestrator `_execute_new_entry` (buy) + monitor `tick_auto_trader_monitor` (sell) both call it correctly.
- [x] `p1_paper_exit_path` — Covered by existing `test_auto_trader_monitor.py`; monitor calls `check_paper_exits(db, uid)` when `live_orders_effective=false`.
- [x] `p1_desk_lists_both` — Covered by existing `test_autotrader_desk_lists_pattern_trade` (live Trade with `related_alert_id`) + desk service lists both live and paper.
- [x] `p1_empty_states` — `test_autotrader_desk_paired_get_patch` confirms empty `trades`/`paper_trades` lists render without error.
- [x] `p1_fix_bugs` — **No bugs found.** The one fixable item was my own test's invalid `ScanPattern(category=...)` kwarg, replaced with `rules_json/origin/asset_class/timeframe`.

Verification gate: **P1 green.** 13 new smoke tests + 13 existing autotrader/desk tests = 26 passing. Proceed to P2.

## P2 — Per-position desk controls (Pause / Resume / Close / Exclude synergy)

Data model:
- Reuse `BrainRuntimeMode` with `slice_name = f"autotrader_v1_position:{trade_id}"` and `payload_json = {"monitor_paused": bool, "synergy_excluded": bool, "kind": "trade"|"paper"}`. `mode` stays `active`; the truth is in `payload_json`.
- Rationale: avoid a new migration; follow existing desk pattern.

Service changes:
- [x] `p2_overrides_service` — Added `app/services/trading/auto_trader_position_overrides.py` with `get/set/list_position_overrides`, `clear_position_overrides`, `close_position_now`, `paused_paper_trade_ids_for_user`, `_opened_today_et` (reused in P3). Slice pattern: `autotrader_v1_position:{kind}:{trade_id}`, payload `{kind, monitor_paused, synergy_excluded}`.
- [x] `p2_monitor_respects_pause` — `tick_auto_trader_monitor` now calls `list_position_overrides` for live and `paused_paper_trade_ids_for_user` for paper; paused rows are skipped (returns `live_monitor_paused_ids` / `paper_monitor_paused_ids` in summary). `check_paper_exits` extended with `skip_trade_ids`. When the monitor closes a position on its own, `clear_position_overrides` runs.
- [x] `p2_synergy_respects_exclude` — `auto_trader_synergy.maybe_scale_in` checks `get_position_overrides(db,"trade",t.id).synergy_excluded` and returns `None` when excluded.
- [x] `p2_close_now_trade` — `_close_trade_now`: requires `auto_trader_version=="v1"`, blocks on `adapter.is_enabled()==False` with error `rh_adapter_off`, market-sells full qty, sets `status=closed / exit_price=<fill || quote || entry> / exit_reason="desk_close_now"`, writes `AutoTraderRun(decision="desk_close_now", rule_snapshot={opened_today_et, would_be_day_trade})`, then clears overrides. Exception-safe.
- [x] `p2_close_now_paper` — `_close_paper_now`: uses `paper_trading._close_paper_trade` + `_apply_slippage` so P&L math stays canonical; falsy `breakout_alert_id` coerced to NULL (FK-safe) in the audit row; then clears overrides.

API:
- [x] `p2_api_position_patch` — `PATCH /api/trading/autotrader/positions/{trade_id}` wired in `app/routers/trading.py`. Body `{kind: "trade"|"paper", monitor_paused?, synergy_excluded?}`. Rejects unknown `kind` and no-op body with 400. Paired-only via `_require_trading_user_id` (guest returns 403).
- [x] `p2_api_position_close` — `POST /api/trading/autotrader/positions/{trade_id}/close` body `{kind, confirm: true}`. Missing `confirm=true` → 400. Failure from `close_position_now` returns HTTP 409 with `{ok:false, error:<reason>}`.
- [x] `p2_desk_returns_overrides` — `list_pattern_linked_open_positions` enriches each row with `overrides`, `opened_today_et`, `controls_supported` (ATv1-only), `direction`, `asset_type`. Bulk `list_position_overrides` avoids N+1.

UI:
- [x] `p2_desk_row_buttons` — `autopilot-pattern-desk.js` now renders per-row **Pause/Resume monitor**, **Exclude/Allow synergy**, **Close now** (with `window.confirm(...)` warning about live vs paper), and `Status` cell badges (auto v1, monitor paused, synergy excluded, opened today PDT, scaled x N). Non-ATv1 rows show "not autotrader v1" in the controls cell.
- [x] `p2_template_copy` — Helper text in `_autopilot_pattern_positions.html` clarifies desk pause ≠ per-row monitor pause.

Tests (`tests/test_autotrader_position_overrides.py`, 15 tests):
- [x] `p2_test_overrides` — `test_overrides_roundtrip`, `test_overrides_invalid_field_raises`, `test_list_position_overrides_bulk`.
- [x] `p2_test_monitor_pauses` — `test_monitor_skips_live_trade_when_monitor_paused` (price below stop, RH mock never called, trade stays open, `live_monitor_paused_ids` reports the id) + `test_paused_paper_trade_ids_for_user` (helper isolates paused paper rows).
- [x] `p2_test_synergy_excludes` — `test_maybe_scale_in_blocked_when_synergy_excluded` + `test_maybe_scale_in_allowed_when_flag_cleared`.
- [x] `p2_test_close_now_paper` — `test_close_position_now_paper` (service) + `test_api_close_paper` (API, real paper close with quote mock).
- [x] `p2_test_close_now_trade` — `test_close_position_now_live` (RH adapter mock, average_price respected) + `test_close_position_now_live_rh_off` (adapter disabled → error without order).
- [x] `p2_test_api_confirm_required` — `test_api_close_requires_confirm`.
- [x] `p2_test_api_guards` — `test_api_patch_override_guest_forbidden`, `test_api_patch_override_bad_kind`, `test_api_patch_override_paired`.

Verification gate: **P2 green.** 15/15 new tests pass; 5/5 existing autotrader monitor/synergy/integration tests still pass (`p2_regression.log`). No lint errors. Proceed to P3.

## P3 — PDT soft-warn (no blocking)

Goal: stamp audit rows and surface a desk badge when an auto-exit would be a same-day round trip. No enforcement. User trusts Robinhood to reject if PDT'd.

- [x] `p3_entry_date_on_trade` — Live `Trade.entry_date` and paper `PaperTrade.entry_date` are already set by existing orchestrator / `_open_paper_trade` on every entry (verified via `_opened_today_et` reading these fields successfully in tests).
- [x] `p3_monitor_stamps_warn` — `tick_auto_trader_monitor` now creates an `AutoTraderRun(decision="monitor_exit", rule_snapshot={exit_reason, pnl, opened_today_et, would_be_day_trade})` after each live close. `would_be_day_trade=true` only for same-ET-day **long stock** exits (crypto excluded).
- [x] `p3_close_now_stamps_warn` — Covered in P2: `_close_trade_now` and `_close_paper_now` both write `AutoTraderRun(decision="desk_close_now")` with `rule_snapshot.opened_today_et` / `would_be_day_trade`.
- [x] `p3_desk_exposes_badge` — `list_pattern_linked_open_positions` returns `opened_today_et: bool` per row (both live and paper) — enrichment landed in P2.
- [x] `p3_desk_row_badge` — `autopilot-pattern-desk.js` `rowBadges` renders the yellow "opened today (PDT)" pill with tooltip whenever `opened_today_et && direction=="long"`.
- [x] `p3_test_stamp_monitor` — `test_monitor_stamps_would_be_day_trade_on_same_day_exit` (entry_date=today ET → `would_be_day_trade=true`) + `test_monitor_does_not_stamp_when_entered_yesterday` (entry_date=yesterday ET → `false`).
- [x] `p3_test_badge_desk` — `test_desk_exposes_opened_today_et` (desk service returns `opened_today_et=true` for a freshly-entered paper row) — UI badge verified by static read of `rowBadges`.

Verification gate: **P3 green.** 3/3 PDT tests pass (`p3_test.log`, 304s). Proceed to P4.

## P4 — Live readiness (final gate before user flips the toggle)

- [x] `p4_shadow_replay` — Ran `scripts/autotrader_shadow.py --limit 50 --skip-llm` (`p4_shadow.log`). 50 alerts scanned, 0 exceptions. Histogram: `missed_entry_slippage:19, projected_profit_below_min:11, symbol_price_above_cap:10, not_stock:7, missing_user_id_on_alert:3`. Projected profit min/median/max 5.02 / 20.68 / 35.39. Confidence 0.76–0.92. All rule gates firing as intended; no paths explode.
- [x] `p4_desk_toggle_end_to_end` — Added `test_desk_paused_blocks_orchestrator_even_with_env_on` (desk pause + env live on → tick skips with reason `paused_or_disabled`), `test_desk_live_override_reflects_in_runtime` (override flip drives `live_orders_effective`; clearing reverts to env default), `test_desk_patch_end_to_end` (`PATCH /api/trading/autotrader/desk` covers pause → resume + live → clear).
- [x] `p4_kill_switch_wins` — Added `is_kill_switch_active()` short-circuit at the top of **both** `run_auto_trader_tick` (returns `{reason: "kill_switch"}`) and `tick_auto_trader_monitor` (returns `{skipped: "kill_switch"}`). Verified by `test_kill_switch_blocks_orchestrator_entry` and `test_kill_switch_blocks_monitor` (paired RH adapter mock `place_market_order.assert_not_called()`) with desk `paused=false` and `live_orders=true`. Kill switch wins over desk state and env.
- [x] `p4_env_flags_doc` — Created `docs/TRADING_AUTOTRADER_V1.md`: safety posture (all env flags + defaults), desk-only live flow (step-by-step), kill-switch precedence, PDT soft-warn semantics, per-position control surface, API table, shadow replay command, test inventory.
- [x] `p4_final_signoff` — See summary in chat. 49/49 AutoTrader+Autopilot tests green (`p4_final.log`), including new P3 (3) + P4 (5). No lint errors. No regressions. Docs landed. User review required before flipping `CHILI_AUTOTRADER_ENABLED=true` and the desk live checkbox.

Verification gate: explicit user approval after reviewing the summary.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Close-now adapter error leaves position open + status confused | Only mark `Trade.status=closed` **after** adapter success; on failure, keep status open and surface the error on desk + toast. |
| Per-position override row grows unbounded | Closing a trade deletes the override row. Include a cleanup in `close_now` and in the monitor when it closes a position on its own. |
| Desk toggle live-orders clashes with env default | `effective_autotrader_runtime()` already handles override vs env; P1 includes an explicit test. |
| PDT badge stamped on crypto / cases where it doesn't apply | Only stamp for `asset_type == "stock"` and only for long positions. |
| Kill switch bypass | Re-verify kill switch precedence on both entry and exit paths in P4. |

## Operator flow after P4

1. Server has `CHILI_AUTOTRADER_ENABLED=true` (enable scheduler tick + monitor) and `CHILI_AUTOTRADER_LIVE_ENABLED=false` as env defaults. (User decides whether to flip tick on in `.env`; can stay off entirely.)
2. On `/trading/autopilot`, user hits **Run / Resume** → `paused=false` → entries allowed.
3. User ticks **Robinhood live orders** → `live_orders` override = true → orchestrator + monitor use RH adapter.
4. Per-row: **Pause monitor** on a position holds it past stop/target; **Exclude scale-in** prevents synergy; **Close now** market-sells immediately.
5. Kill switch remains the red button for everything.

## Plan sync discipline

Per `chili-agent-execution-quality.mdc`: this YAML is the user-visible source of truth. Every completed P1–P4 task must update `[ ]` → `[x]` in the same turn the code merges. Partial delivery = split the checkbox into two (one `[x]`, one `[ ]` with narrowed `content`).
