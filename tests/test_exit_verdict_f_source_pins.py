"""EXIT VERDICT G -- source pins (2026-09-10, [21]/[44]/[47] + Amendments 1-3).

The elif ORDER, the fallback's placement, the ONE whole-exit submit, the two guards, the
absence of every partial / runner / sibling mechanism, the reasons, the recycle keys, the
parity alphabet, the untouched nine bailout callers, PATH B still unwired, NO config knob for
the fraction, the receipts -- asserted on the source and the AST so a later edit cannot
silently move any of them. The doctrine pin: no exit site arms `momentum_break_stop` any
more; it survives only as the -USD / unreadable-anchor fallback.

Runnable: pytest tests/test_exit_verdict_f_source_pins.py -v   (DB-free)
"""
from __future__ import annotations

import ast
import inspect

from app.config import Settings
from app.services.trading.momentum_neural import exit_verdict as EV
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.replay_parity import LOAD_BEARING_TRANSITIONS

TICK = inspect.getsource(lr.tick_live_session)
MODULE = inspect.getsource(lr)
VERDICT = inspect.getsource(lr._exit_verdict_tick)


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


def _string_constants(src: str) -> set[str]:
    """Every string LITERAL in the code (docstrings and comments excluded) -- a pin on what the
    code can NAME, not on what its prose mentions."""
    tree = ast.parse(src)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    # drop docstrings (the first statement of every function / module body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.discard(first.value.value)
    return out


def _code_names(src: str) -> set[str]:
    return {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}


# ── the held tick's exit priority ──────────────────────────────────────────────

def test_the_independent_verdict_precedes_optional_trail_arm_and_smart_hold():
    i_mlc = TICK.find('"reason": "max_loss_circuit"')
    # The earlier quote-unavailable path also evaluates the verdict. This pin
    # concerns the held tick with a usable quote, after hard-loss protection.
    i_ev = TICK.find("_exit_verdict_active(sess, le)", i_mlc)
    i_early = TICK.find("# EARLY TRAIL-ARM", i_ev)
    i_smart = TICK.find("_smart_hold_on =", i_early)
    i_mb = TICK.find('reason="momentum_break_bars"')
    i_bw = TICK.find('"live_burst_window_exit"')
    i_bb = TICK.find("breakout_failed_to_hold(")
    assert 0 < i_mlc < i_ev < i_early < i_smart < i_mb < i_bw < i_bb
    block = TICK[i_ev - 500: i_ev + 500]
    assert "st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)" in block
    assert "as_of=tick_as_of" in block
    # the break elif's condition text is byte-identical to #1377's
    i_flag = TICK.find('"chili_momentum_failed_pop_break_exit_enabled"')
    assert "_failed_pop_break_fires(db, sess, le, bid=bid, avg=avg)" in TICK[i_flag: i_flag + 300]


def test_no_exit_site_arms_momentum_break_stop_it_is_only_the_named_fallback():
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
    assert ast.unparse(enclosing.test) == "_exit_verdict_supported(sess, le)"
    # its receipt names why the verdict could not exist; the marker is written once, there
    i = TICK.find('"verdict_unavailable": _exit_verdict_unsupported_binding(sess, le)')
    assert i > 0 and TICK.find('reason="momentum_break_stop"') > i
    assert TICK.count('le["pending_exit_reason"] = "momentum_break_stop"') == 1
    # no opinion site ARMS it, and the verdict machine never names it
    for c in _calls_named(ast.parse(MODULE), "_arm_opinion_exit"):
        assert _const(_kw(c, "reason")) != "momentum_break_stop"
    assert "momentum_break_stop" not in _string_constants(VERDICT)      # the code cannot name it
    assert "momentum_break_stop" not in repr(lr._EXIT_VERDICT_ACTIONS)


def test_the_break_elif_records_the_opinion_on_equity_with_the_bar_inputs_and_the_bbo_envelope():
    i = TICK.find('reason="momentum_break_bars"')
    block = TICK[i - 400: i + 1000]
    assert 'prior_event="live_momentum_break_exit"' in block
    assert '**(le.get("failed_pop_break_dbg") or {})' in block
    assert "**_held_bbo_receipt_fields(le)" in block
    assert '"opinion_exit_armed": "momentum_break_bars"' in block
    assert "_exit_verdict_bbo(" not in MODULE                  # the PR's own bbo helper is gone


def test_the_one_whole_exit_submit_carries_the_four_keywords_and_a_tagged_cid():
    tree = ast.parse(TICK)
    seen = []
    for c in _calls_named(tree, "_submit_live_market_exit"):
        reason = _kw(c, "reason")
        r = _const(reason) if isinstance(reason, ast.Constant) else ast.unparse(reason)
        if r == "_ev_reason":
            seen.append(c)
            names = {k.arg for k in c.keywords}
            assert {"client_order_id", "bid", "ask", "mid", "le", "product_id", "quantity", "reason", "extra"} <= names
            cid = ast.unparse(_kw(c, "client_order_id"))
            assert "_ev_cid_tag" in cid and "chili_ml_" in cid
            assert "quantity=float(pos.get('quantity') or 0.0)" in ast.unparse(c)   # the WHOLE position
            extra = ast.unparse(_kw(c, "extra"))
            assert "_exit_verdict_receipt(le)" in extra and "_EV_EXIT_FRACTION" in extra
    assert len(seen) == 1
    # no other verdict reason is submitted as a literal anywhere in the tick
    for c in _calls_named(tree, "_submit_live_market_exit"):
        assert _const(_kw(c, "reason")) not in ("tape_sellers_took_it", "tape_accel_rollover",
                                                 "tick_deadman_stop", "tape_sellers_took_it_d2")
    # the reason / tag come from the marker's decision, through ONE table
    i = TICK.find('_ev_reason = str(_ev_exit.get("reason") or "tape_sellers_took_it")')
    assert i > 0 and '_ev_cid_tag = str(_ev_exit.get("cid_tag") or "tv")' in TICK[i: i + 200]
    assert 'le["pending_exit_reason"] = _ev_reason' in TICK[i: i + 400]
    assert lr._EXIT_VERDICT_ACTIONS == {
        "tick_deadman": ("tick_deadman_stop", "td"),
        "accel_rollover": ("tape_accel_rollover", "ta"),
        "since_high_verdict": ("tape_sellers_took_it", "tv"),
    }


def test_the_decision_is_written_ahead_and_submitted_on_the_same_tick():
    """The stale gate can never withhold a DECIDED exit: the decision is durable (phase
    exit_pending, `_commit_le`) inside `_exit_verdict_tick`, and the elif submits right
    after it returns an action -- no stale check, no second read, in between."""
    i = VERDICT.find("def _decide(action: str, extra: dict[str, Any]) -> None:")
    assert i > 0
    body = VERDICT[i: i + 900]
    assert 'ev["phase"] = "exit_pending"' in body and "_commit_le(sess, le)" in body
    assert body.find('ev["phase"] = "exit_pending"') < body.find("_commit_le(sess, le)")
    j = TICK.find("_ev_action = str(_ev.get(\"action\") or \"\")")
    k = TICK.find("sr = _submit_live_market_exit(", j)
    assert 0 < j < k and "stale" not in TICK[j: k]


# ── no partial, no runner, no sibling ──────────────────────────────────────────

def test_no_partial_runner_or_sibling_mechanism_survives_in_the_module():
    for gone in ("_verdict_partial_to_runner", "_service_exit_verdict_partial", "_certify_exit_verdict_cover",
                 "_exit_verdict_place_partial_sell", "_exit_verdict_service_partial_sell",
                 "_exit_verdict_release_sibling_for_whole_exit", "_exit_verdict_partial_failed",
                 "_exit_verdict_adopt_sibling_fill", "_exit_verdict_pending_partial_qty", "_exit_verdict_sibling",
                 "_exit_verdict_sell_rung", "_exit_verdict_split", "partial_shrink_pending",
                 "partial_sell_pending", "runner_exit_pending", "tape_sellers_took_it_d2",
                 "verdict_partial_split_arithmetic_invalid", "chili_momentum_exit_verdict_sell_fraction",
                 "_SELL_FRACTION_DERIVATION", "walk_runner_prints", "partial_split"):
        assert gone not in MODULE, gone
    head = inspect.getsource(lr._ensure_alpaca_deadman_stop)
    assert "exit_verdict" not in head                        # no head guard: the stop covers Q
    impl = inspect.getsource(lr._submit_live_market_exit_impl)
    assert "exit_verdict" not in impl                        # no chokepoint-head abandonment
    assert "pending_exit_is_scale_out" not in VERDICT and "STATE_LIVE_SCALING_OUT" not in VERDICT
    assert "_apply_confirmed_live_partial_exit" not in VERDICT
    assert EV.PHASES == ("armed", "exit_pending", "exited")


# ── the machine judges every equity leg from the fill; the two guards ─────────

def test_the_machine_needs_no_opinion_to_run():
    src = inspect.getsource(lr._exit_verdict_active)
    assert "opinion_exit_armed" not in _string_constants(src)      # no marker read in the code
    assert "_exit_verdict_supported" in _code_names(src)
    src = inspect.getsource(lr._arm_opinion_exit)
    assert '"armed_exit"' in src and '"superseded"' in src
    assert '"exit_verdict_g_all" if _exit_verdict_supported(sess, le) else "momentum_break_stop"' in src


def test_the_tick_order_is_walk_then_ratchet_then_g_then_d_and_the_frontier_is_the_walk():
    i_walk = VERDICT.find("walk = _ev_walk_held_prints(")
    i_front = VERDICT.find('ev["frontier_at"] = _exit_verdict_iso(walk["frontier"][0])')
    i_dead = VERDICT.find('_decide("tick_deadman", {')
    i_rat = VERDICT.find("new_level, moved = _ev_tick_deadman_ratchet(")
    i_since = VERDICT.find("rows_read = _leg_since_high(")
    i_g = VERDICT.find("g = _ev_accel_rollover(")
    i_trig = VERDICT.find('trigger = "accel_rollover" if g.get("fired") else ("since_high_verdict" if v.get("fired") else None)')
    assert 0 < i_walk < i_front < i_dead < i_rat < i_g < i_since < i_trig
    # the frontier is NEVER the tick's as_of (review of #1385, major)
    assert 'ev["frontier_at"] = _exit_verdict_iso(as_of)' not in VERDICT
    # on a stale tick the decisions are withheld and `accel_prev` is not advanced
    i_stale = VERDICT.find("if stale:")
    seg = VERDICT[i_stale: i_stale + 900]
    assert 'result["withheld"] = "stale_tape"' in seg and "return result" in seg
    assert 'ev["accel_prev"]' not in seg


def test_the_chandelier_block_uses_current_tick_verdict_authority():
    i = TICK.find("_trail_authority = _exit_verdict_trail_authority(le, as_of=tick_as_of)")
    assert i > 0
    j = TICK.find("if not _ev_trail_bypass:")
    k = TICK.find('"live_trail_ratchet"')
    assert i < j < k
    assert '_ev_trail_bypass = _trail_authority["bypass"]' in TICK[i: j]
    assert '_be_floor = avg if pos.get("partial_taken") else stop_px' in TICK[i: j]
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
    assert "_held_bbo_receipt_fields(le)" in body_src           # main's [48] fields kept in the emit
    # the topping-tail receipt site is NOT inside the guard (it still runs while armed)
    assert TICK.find('reason="topping_tail_runner_exit"') < i
    assert EV.TRAIL_BYPASS_PHASES == {"armed", "exit_pending"}


def test_the_first_target_block_is_guarded_and_no_new_scaling_out_transition_exists():
    i = TICK.find("_safe_transition(db, sess, STATE_LIVE_SCALING_OUT)")
    assert i > 0
    assert "_exit_verdict_phase(le) not in _EV_FIRST_TARGET_BYPASS_PHASES" in TICK[i - 900: i]
    assert TICK.count("_safe_transition(db, sess, STATE_LIVE_SCALING_OUT)") == 1   # unchanged vs main
    assert EV.FIRST_TARGET_BYPASS_PHASES == {"exit_pending"}


# ── reasons, keys, alphabet, config, receipts ──────────────────────────────────

def test_the_three_reasons_fail_open_on_the_freshness_seam_and_the_d2_reason_is_gone():
    for r in ("tape_sellers_took_it", "tape_accel_rollover", "tick_deadman_stop", "momentum_break_stop"):
        assert r in lr._FRESHNESS_FAIL_OPEN_EXIT_REASONS, r
        assert lr._exit_reason_fails_open(r) is True, r
    assert "tape_sellers_took_it_d2" not in lr._FRESHNESS_FAIL_OPEN_EXIT_REASONS


def test_recycle_clears_the_marker_and_the_two_fixed_in_passing_families():
    for key in ("exit_verdict", "bailout_breach_pending_utc", "bailout_breach_trigger",
                "fpb_bucket", "fpb_fire", "failed_pop_break_dbg", "opinion_exit_armed"):
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS, key


def test_the_parity_alphabet_has_the_four_in_and_the_mechanics_out():
    for ev in ("live_opinion_exit_armed", "live_exit_verdict_armed", "live_exit_verdict_fired",
               "live_tick_deadman_exit"):
        assert ev in LOAD_BEARING_TRANSITIONS, ev
    for ev in ("live_tick_deadman_ratchet", "live_exit_verdict_unreadable", "live_exit_verdict_unavailable",
               "live_momentum_break_exit", "live_exit_verdict_partial", "live_exit_verdict_exit",
               "live_exit_verdict_partial_shrunk", "live_exit_verdict_runner_started"):
        assert ev not in LOAD_BEARING_TRANSITIONS, ev


def test_the_nine_bailout_callers_and_named_opinion_receipt_sites_remain():
    tree = ast.parse(MODULE)
    assert len(_calls_named(tree, "_transition_to_bailout")) == 9
    reasons = sorted(str(_const(_kw(c, "reason"))) for c in _calls_named(tree, "_arm_opinion_exit"))
    assert reasons == ["breakout_failed_fast_bail", "lost_vwap_confirmed",
                       "momentum_break_bars", "smart_hold_fast_bail", "topping_tail_runner_exit"]


def test_path_b_is_still_unwired():
    assert "path_b_partial" not in MODULE
    tree = ast.parse(MODULE)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "replace_order_qty"]


def test_the_exit_fraction_is_a_reported_constant_not_a_knob():
    assert "chili_momentum_exit_verdict_sell_fraction" not in Settings.model_fields
    assert lr._EV_EXIT_FRACTION == EV.EXIT_FRACTION == 1.0
    assert "sell_fraction" not in lr._exit_verdict_settings()
    base = inspect.getsource(lr._exit_verdict_receipt_base)
    assert '"exit_fraction": _EV_EXIT_FRACTION' in base and '"exit_fraction_derivation": _EXIT_FRACTION_DERIVATION' in base
    for tok in ("+157.52", "-59.25", "-1,216.28", "78 live Alpaca legs"):
        assert tok in lr._EXIT_FRACTION_DERIVATION, tok


def test_the_existing_exit_receipts_carry_the_verdict_snapshot():
    src = inspect.getsource(lr._complete_confirmed_live_exit)
    assert 'payload["exit_verdict"] = _exit_verdict_receipt(le)' in src
    assert '_ev_done["phase"] = "exited"' in src
    i = TICK.find('"unrealized_pnl_usd": (bid - avg) * qty,')
    assert i > 0
    assert '"exit_verdict": _exit_verdict_receipt(le)' in TICK[i: i + 500]


def test_every_verdict_receipt_carries_the_common_fields_and_the_48_bbo_envelope():
    src = inspect.getsource(lr._exit_verdict_receipt_base)
    for key in ('"derivation"', '"as_of"', '"phase"', '"state"', '"bid"', '"tape_frontier_age_s"',
                '"stale_tape_bound_s"', '"opinion_exit_armed"', '"exit_fraction"'):
        assert key in src, key
    assert "**_held_bbo_receipt_fields(le)" in src          # bbo_source / bbo_age_s / bbo_fallback_engaged
    for receipt in ('"live_exit_verdict_fired"', '"live_tick_deadman_exit"', '"live_tick_deadman_ratchet"',
                    '"live_exit_verdict_armed"', '"live_exit_verdict_unreadable"'):
        assert receipt in VERDICT, receipt
    # the fired receipt is built into `receipt` right BEFORE its emit
    i = VERDICT.find('_emit(db, sess, "live_exit_verdict_fired", receipt)')
    assert i > 0
    fired = VERDICT[i - 1600: i]
    assert "**base," in fired
    for key in ('"trigger"', '"accel_prev"', '"accel_now"', '"prints_since_entry"', '"prints_since_high"',
                '"bid"', '"exit_fraction"', '"binding"'):
        assert key in fired, key
    j = VERDICT.find('"live_tick_deadman_ratchet"')
    rat = VERDICT[j: j + 600]
    for key in ('"old"', '"new"', '"print"'):
        assert key in rat, key
