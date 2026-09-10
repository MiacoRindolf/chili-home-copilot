"""EXIT VERDICT F -- source pins (2026-09-10, [21]/[44]/[47]).

The elif ORDER, the fallback's placement, the two guards, the head guard's position, the
chokepoint head hook, the reasons, the recycle keys, the parity alphabet, the untouched
nine bailout callers, PATH B still unwired, the config default -- asserted on the source
and the AST so a later edit cannot silently move any of them.

Runnable: pytest tests/test_exit_verdict_f_source_pins.py -v   (DB-free)
"""
from __future__ import annotations

import ast
import inspect

from app.config import Settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.replay_parity import LOAD_BEARING_TRANSITIONS

TICK = inspect.getsource(lr.tick_live_session)
MODULE = inspect.getsource(lr)


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


# ── the held tick's elif chain ─────────────────────────────────────────────────

def test_the_verdict_elif_sits_between_the_usd_cap_and_the_break_elif():
    i_mlc = TICK.find('"reason": "max_loss_circuit"')
    i_ev = TICK.find("_exit_verdict_active(sess, le)")
    i_mb = TICK.find('reason="momentum_break_bars"')
    i_bw = TICK.find('"live_burst_window_exit"')
    i_bb = TICK.find("breakout_failed_to_hold(")
    assert 0 < i_mlc < i_ev < i_mb < i_bw < i_bb
    block = TICK[i_ev - 400: i_ev + 500]
    assert "st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)" in block
    assert "as_of=tick_as_of" in block
    # the break elif's condition text is byte-identical to #1377's
    i_flag = TICK.find('"chili_momentum_failed_pop_break_exit_enabled"')
    assert "_failed_pop_break_fires(db, sess, le, bid=bid, avg=avg)" in TICK[i_flag: i_flag + 300]


def test_the_only_momentum_break_stop_submit_is_the_named_fallback():
    tree = ast.parse(TICK)
    submits = [c for c in _calls_named(tree, "_submit_live_market_exit")
               if _const(_kw(c, "reason")) == "momentum_break_stop"]
    assert len(submits) == 1
    call = submits[0]
    # the call sits in the `else` of `if _exit_verdict_supported(sess, le):`
    enclosing = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and any(
            isinstance(n, ast.Call) and n is call for n in ast.walk(ast.Module(body=node.orelse, type_ignores=[]))
        ):
            enclosing = node
    assert enclosing is not None
    test_src = ast.unparse(enclosing.test)
    assert test_src == "_exit_verdict_supported(sess, le)", test_src
    # its receipt names why the verdict could not exist
    i = TICK.find('"verdict_unavailable": _exit_verdict_unsupported_binding(sess, le)')
    assert i > 0
    assert TICK.find('reason="momentum_break_stop"') > i


def test_the_break_elif_arms_the_verdict_on_equity_with_the_bar_inputs():
    i = TICK.find('reason="momentum_break_bars"')
    block = TICK[i - 400: i + 1000]
    assert 'prior_event="live_momentum_break_exit"' in block
    assert '**(le.get("failed_pop_break_dbg") or {})' in block
    assert "**_exit_verdict_bbo(le)" in block
    assert '"opinion_exit_armed": "momentum_break_bars"' in block


def test_every_verdict_exit_submit_carries_the_four_keywords_and_a_cid():
    tree = ast.parse(TICK)
    seen = set()
    for c in _calls_named(tree, "_submit_live_market_exit"):
        reason = _kw(c, "reason")
        r = _const(reason) if isinstance(reason, ast.Constant) else ast.unparse(reason)
        if r in ("tape_sellers_took_it", "_ev_reason"):
            seen.add(r)
            names = {k.arg for k in c.keywords}
            assert {"client_order_id", "bid", "ask", "mid", "le", "product_id", "quantity", "reason"} <= names, r
            cid = ast.unparse(_kw(c, "client_order_id"))
            assert "chili_ml_tv_" in cid or "_ev_cid_tag" in cid, cid
    assert seen == {"tape_sellers_took_it", "_ev_reason"}
    i = TICK.find('_ev_reason = "tick_deadman_stop" if _ev_action == "runner_deadman" else "tape_sellers_took_it_d2"')
    assert i > 0
    assert '_ev_cid_tag = "td" if _ev_action == "runner_deadman" else "tv2"' in TICK[i: i + 300]


def test_the_f_sell_is_a_sibling_not_a_chokepoint_close():
    """DEVIATION FROM THE SPEC'S §6.4 (proven on the claim tables): the outbox is single-slot,
    so the f sell POSTs straight to the adapter (the OCO-tranche precedent) and the dead
    bypass is NOT in `_release_deadman_at_literal_submit`."""
    impl = inspect.getsource(lr._submit_live_market_exit_impl)
    assert "_vp > 0.0" not in impl.split("def _release_deadman_at_literal_submit")[1].split("def _")[0]
    assert "_exit_verdict_release_sibling_for_whole_exit(" in impl
    assert impl.find("_exit_verdict_release_sibling_for_whole_exit(") < impl.find("_cancel_scale_limit_and_clamp(")
    assert impl.find("_exit_verdict_release_sibling_for_whole_exit(") < impl.find("exit_retry_backoff")
    place = inspect.getsource(lr._exit_verdict_place_partial_sell)
    assert "adapter.place_limit_order_gtc(" in place and "adapter.place_market_order(" in place
    assert "_submit_live_market_exit" not in place and "_lease_owner_transport_for_runtime" not in place
    assert 'position_intent="sell_to_close"' in place and 'time_in_force="day"' in place
    # the cid is durable BEFORE the POST
    assert place.find('"phase": "submitting"') < place.find("adapter.place_limit_order_gtc(")
    # the runner completer is reached ONLY through the sibling fill adoption
    tree = ast.parse(MODULE)
    callers = [c for c in _calls_named(tree, "_verdict_partial_to_runner")]
    assert len(callers) == 1
    assert "pending_exit_is_scale_out" not in inspect.getsource(lr._exit_verdict_tick)


# ── the two guards and the head guard ──────────────────────────────────────────

def test_the_chandelier_block_is_guarded_by_the_verdict_phase():
    i = TICK.find("_ev_trail_bypass = _exit_verdict_phase(le) in _EV_TRAIL_BYPASS_PHASES")
    assert i > 0
    j = TICK.find("if not _ev_trail_bypass:")
    k = TICK.find('"live_trail_ratchet"')
    assert i < j < k
    assert '_be_floor = avg if (pos.get("partial_taken") and not _ev_trail_bypass) else stop_px' in TICK[i: j]
    # AST: the ratchet emit is INSIDE the `if not _ev_trail_bypass:` body
    tree = ast.parse(TICK)
    guarded = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "not _ev_trail_bypass":
            body_src = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            if "live_trail_ratchet" in body_src:
                guarded = node
    assert guarded is not None
    body_src = ast.unparse(ast.Module(body=guarded.body, type_ignores=[]))
    assert "_replay_aware_fetch_ohlcv_df" in body_src           # the frame fetches are inside too
    assert "cushion_adaptive_trail_stop(" in body_src
    # the topping-tail arming site is NOT inside the guard (it still runs while armed)
    assert TICK.find('reason="topping_tail_runner_exit"') < i


def test_the_first_target_block_is_guarded_and_no_new_scaling_out_transition_exists():
    i = TICK.find("_safe_transition(db, sess, STATE_LIVE_SCALING_OUT)")
    assert i > 0
    assert "_exit_verdict_phase(le) not in _EV_FIRST_TARGET_BYPASS_PHASES" in TICK[i - 900: i]
    assert TICK.count("_safe_transition(db, sess, STATE_LIVE_SCALING_OUT)") == 1   # unchanged vs main
    assert "STATE_LIVE_SCALING_OUT" not in inspect.getsource(lr._exit_verdict_tick)
    assert "STATE_LIVE_SCALING_OUT" not in inspect.getsource(lr._verdict_partial_to_runner)
    assert "_safe_transition" not in inspect.getsource(lr._verdict_partial_to_runner)


def test_the_head_guard_sits_after_the_tranche_split_and_the_marker_helper_is_defined_above():
    src = inspect.getsource(lr._ensure_alpaca_deadman_stop)
    a = src.find("tranche_oco_split_arithmetic_invalid")
    b = src.find("verdict_partial_split_arithmetic_invalid")
    c = src.find("context = _alpaca_owner_transport_context(sess)")
    assert 0 < a < b < c
    assert "quantity = float(quantity) - _pending" in src[a: c]
    assert MODULE.find("def _exit_verdict_pending_partial_qty(") < MODULE.find("def _ensure_alpaca_deadman_stop(")


# ── reasons, keys, alphabet, config ────────────────────────────────────────────

def test_the_three_reasons_fail_open_on_the_freshness_seam():
    for r in ("tape_sellers_took_it", "tape_sellers_took_it_d2", "tick_deadman_stop", "momentum_break_stop"):
        assert r in lr._FRESHNESS_FAIL_OPEN_EXIT_REASONS, r
        assert lr._exit_reason_fails_open(r) is True, r


def test_recycle_clears_the_marker_and_the_two_fixed_in_passing_families():
    for key in ("exit_verdict", "bailout_breach_pending_utc", "bailout_breach_trigger",
                "fpb_bucket", "fpb_fire", "failed_pop_break_dbg", "opinion_exit_armed"):
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS, key


def test_the_parity_alphabet_has_the_five_in_and_the_mechanics_out():
    for ev in ("live_opinion_exit_armed", "live_exit_verdict_armed", "live_exit_verdict_partial",
               "live_tick_deadman_exit", "live_exit_verdict_exit"):
        assert ev in LOAD_BEARING_TRANSITIONS, ev
    for ev in ("live_tick_deadman_ratchet", "live_exit_verdict_partial_shrunk",
               "live_exit_verdict_partial_failed", "live_exit_verdict_runner_started",
               "live_exit_verdict_unreadable", "live_exit_verdict_partial_submitted",
               "live_momentum_break_exit"):
        assert ev not in LOAD_BEARING_TRANSITIONS, ev


def test_the_arming_receipt_names_which_exit_it_armed():
    src = inspect.getsource(lr._arm_opinion_exit)
    assert '"armed_exit"' in src and '"superseded"' in src
    assert '"exit_verdict_f" if _exit_verdict_supported(sess, le) else "momentum_break_stop"' in src


def test_the_nine_bailout_callers_and_the_five_arming_sites_are_untouched():
    tree = ast.parse(MODULE)
    assert len(_calls_named(tree, "_transition_to_bailout")) == 9
    reasons = sorted(str(_const(_kw(c, "reason"))) for c in _calls_named(tree, "_arm_opinion_exit"))
    assert reasons == ["breakout_failed_fast_bail", "close_below_structure", "lost_vwap_confirmed",
                       "momentum_break_bars", "topping_tail_runner_exit"]


def test_path_b_is_still_unwired():
    assert "path_b_partial" not in MODULE
    tree = ast.parse(MODULE)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "replace_order_qty"]


def test_the_sell_fraction_default_and_its_derivation():
    """Spec §10: the acceptance table re-reports the share tick-by-tick and, if it differs,
    the default moves in the same PR. It did: 8/32 (bid-priced, every 3.19-s tick) vs the
    in-memory 20/31 => 24/32 = 0.75 shipped; both numbers stay in the description."""
    field = Settings.model_fields["chili_momentum_exit_verdict_sell_fraction"]
    assert field.default == 24 / 32
    for tok in ("8/32", "24/32", "0.75", "20/31", "11/31", "0.5", "2026-09-10", "[0.13, 0.42]"):
        assert tok in field.description, tok


def test_the_existing_exit_receipts_carry_the_verdict_snapshot():
    src = inspect.getsource(lr._complete_confirmed_live_exit)
    assert 'payload["exit_verdict"] = _exit_verdict_receipt(le)' in src
    assert '_ev_done["phase"] = "exited"' in src
    i = TICK.find('"unrealized_pnl_usd": (bid - avg) * qty,')
    assert i > 0
    assert '"exit_verdict": _exit_verdict_receipt(le)' in TICK[i: i + 500]


def test_every_verdict_receipt_carries_the_common_fields():
    src = inspect.getsource(lr._exit_verdict_receipt_base)
    for key in ('"derivation"', '"as_of"', '"phase"', '"state"', '"bid"', '"tape_frontier_age_s"',
                '"stale_tape_bound_s"', '"opinion_exit_armed"'):
        assert key in src, key
    bbo = inspect.getsource(lr._exit_verdict_bbo)
    for key in ('"bbo_source"', '"bbo_age_s"', '"bbo_reason"'):
        assert key in bbo, key
    assert 'le.get("last_held_execution_bbo")' in bbo
