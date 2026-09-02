"""Stop-noise floor at the C4 viability tighten — SHADOW only (2026-09-02).

MEASURED (Alpaca paper, trading_automation_events):

    CANF 19471  entry 4.34 x355 11:10:20.456Z; stop 4.265389592760181 (1.72%)
                11:10:55.220 viability_degraded_tighten 4.2654 -> 4.3183
                            (= 4.34 * 0.995, 2.17c under entry; live spread
                             4.33/4.35 = 2c; 60s bid range ~4.25-4.37 = 12c)
                11:10:58.107 stop_breach_pending_confirm bid 4.28
                11:11:05.845 live_exit_filled 4.119915  (-78.13, -2.94R)
    UPC  19457  entry 5.395955 x314; stop 5.309003607 (1.61%); spread 4c;
                NO tighten; sweep 08:48:06 5.35->5.26; fill 5.24 (-48.97)
    AUUD 19337  entry 1.11 x551; stop 1.0981307 (1.07%, 0.30x 1-min TR);
                NO tighten; max_loss_circuit; fill 1.030127 (-44.01)

The 21-day forensics (docs/STRATEGY/CC_REPORTS/2026-09-02_stop-noise-floor-
forensics.md) found every tighten breached within 0.1-4 s but $0 difference
under any clamp (the flush crossed the ORIGINAL stop too), and 2/3 tightens
were right. So the clamp is NOT applied: the C4 writer still places
``max(_live_stop_c4, avg * 0.995)``; the event payload now carries where the
clamped stop WOULD have been (``nf_*`` fields) for the event study that gates
the apply step.

Contract pinned here:
  * helper: current_stop <= out <= candidate; fail-open on missing measures;
    identity when there is no tighten; never widens a placed/initial stop.
  * shadow: no DB call on the exit-critical path; wired ONLY into the
    ``viability_degraded_tighten`` emit; the applied stop line is unchanged.

Runnable: pytest tests/test_stop_noise_floor_shadow_0902.py -v
"""
from __future__ import annotations

import ast
import inspect
import math

import pytest

from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.paper_execution import stop_tighten_noise_clamp


# ── measured numbers ─────────────────────────────────────────────────────────

CANF_AVG = 4.34
CANF_STOP0 = 4.265389592760181
CANF_TIGHTEN = CANF_AVG * 0.995          # 4.3183 — the event's new_stop
CANF_BID, CANF_ASK = 4.33, 4.35          # live spread at the tighten (2c)
CANF_RING_LO, CANF_RING_HI = 4.25, 4.37  # per-second bid range in the minute before

UPC_AVG = 5.395955
UPC_STOP0 = 5.309003607274401
UPC_BID, UPC_ASK = 5.35, 5.39            # 4c spread before the sweep

AUUD_AVG = 1.11
AUUD_STOP0 = 1.0981307
AUUD_BID, AUUD_ASK = 1.10, 1.11          # 1c spread


def _clamp(**kw):
    return stop_tighten_noise_clamp(**kw)


# ── 1. CANF: the tighten lands inside the spread; the clamp would hold it out ─


def test_canf_spread_term_moves_the_tighten_out_of_the_spread():
    spread_frac = (CANF_ASK - CANF_BID) / ((CANF_ASK + CANF_BID) / 2.0)
    out, meta = _clamp(candidate=CANF_TIGHTEN, current_stop=CANF_STOP0,
                       ref_price=CANF_AVG, spread_frac=spread_frac)
    # 1.5 x 2c = 3c under entry -> 4.31 (vs the placed 4.3183, 1.08x spread)
    assert out == pytest.approx(4.31, abs=1e-6)
    assert meta["inside_noise"] is True and meta["moved"] is True
    assert meta["floor_term"] == "spread"
    assert CANF_STOP0 < out < CANF_TIGHTEN


def test_canf_own_range_term_turns_the_tighten_into_a_no_op():
    spread_frac = (CANF_ASK - CANF_BID) / ((CANF_ASK + CANF_BID) / 2.0)
    noise_frac = (CANF_RING_HI - CANF_RING_LO) / CANF_AVG     # 12c / 4.34
    out, meta = _clamp(candidate=CANF_TIGHTEN, current_stop=CANF_STOP0,
                       ref_price=CANF_AVG, spread_frac=spread_frac, noise_frac=noise_frac)
    # 1.0 x 12c = 4.22 < the placed 4.2654 -> the clamp returns the CURRENT stop:
    # the tighten becomes a no-op, the original structural stop stays.
    assert out == pytest.approx(CANF_STOP0, abs=1e-12)
    assert meta["floor_term"] == "noise"
    assert meta["floor_px"] == pytest.approx(4.22, abs=1e-6)
    assert meta["inside_noise"] is True and meta["moved"] is True


# ── 2. UPC: hypothetical tighten sits between the placed stop and the candidate


def test_upc_hypothetical_tighten_is_bounded_both_sides():
    cand = UPC_AVG * 0.995
    spread_frac = (UPC_ASK - UPC_BID) / ((UPC_ASK + UPC_BID) / 2.0)
    out, meta = _clamp(candidate=cand, current_stop=UPC_STOP0,
                       ref_price=UPC_AVG, spread_frac=spread_frac)
    assert UPC_STOP0 < out < cand
    assert out == pytest.approx(UPC_AVG * (1.0 - 1.5 * spread_frac), abs=1e-9)


def test_upc_no_tighten_is_identity():
    # UPC never tightened: candidate == placed stop -> identity, no floor applied.
    out, meta = _clamp(candidate=UPC_STOP0, current_stop=UPC_STOP0,
                       ref_price=UPC_AVG, spread_frac=0.0074)
    assert out == UPC_STOP0
    assert meta["reason"] == "no_tighten" and meta["moved"] is False


# ── 3. AUUD: the helper never WIDENS a placed stop (the #1278 failure mode) ───


def test_auud_initial_stop_inside_noise_is_not_widened():
    # AUUD's initial stop (0.30x 1-min TR) sits inside the noise, but the helper
    # is a clamp on a TIGHTEN candidate — it must never lower the placed stop.
    spread_frac = (AUUD_ASK - AUUD_BID) / ((AUUD_ASK + AUUD_BID) / 2.0)
    out, _ = _clamp(candidate=AUUD_STOP0, current_stop=AUUD_STOP0,
                    ref_price=AUUD_AVG, spread_frac=spread_frac, noise_frac=0.036)
    assert out == AUUD_STOP0
    cand = AUUD_AVG * 0.995
    out2, meta2 = _clamp(candidate=cand, current_stop=AUUD_STOP0,
                         ref_price=AUUD_AVG, spread_frac=spread_frac)
    # floor 1.11*(1-1.5*0.905%) = 1.0949 < placed 1.0981 -> clamp = placed stop
    assert out2 == pytest.approx(AUUD_STOP0, abs=1e-12)
    assert meta2["moved"] is True and out2 >= AUUD_STOP0


def test_candidate_below_current_stop_is_returned_unchanged():
    out, meta = _clamp(candidate=4.20, current_stop=4.2654, ref_price=4.34, spread_frac=0.0046)
    assert out == 4.20 and meta["reason"] == "no_tighten"


# ── 4. fail-open ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("spread_frac,noise_frac", [
    (None, None), (float("nan"), None), (0.0, 0.0), (-0.01, None), ("x", None),
])
def test_no_readable_measure_leaves_the_candidate_untouched(spread_frac, noise_frac):
    out, meta = _clamp(candidate=CANF_TIGHTEN, current_stop=CANF_STOP0,
                       ref_price=CANF_AVG, spread_frac=spread_frac, noise_frac=noise_frac)
    assert out == CANF_TIGHTEN
    assert meta["reason"] == "no_measure" and meta["moved"] is False


@pytest.mark.parametrize("bad", [None, "x", float("nan"), float("inf")])
def test_unreadable_prices_leave_the_candidate_untouched(bad):
    out, meta = _clamp(candidate=bad, current_stop=CANF_STOP0, ref_price=CANF_AVG, spread_frac=0.005)
    assert out is bad or (isinstance(bad, float) and isinstance(out, float) and (math.isnan(out) or math.isinf(out)))
    assert meta["reason"] == "unreadable_inputs"
    out2, meta2 = _clamp(candidate=CANF_TIGHTEN, current_stop=CANF_STOP0, ref_price=0.0, spread_frac=0.005)
    assert out2 == CANF_TIGHTEN and meta2["reason"] == "unreadable_inputs"


def test_invariant_a_over_a_grid():
    """current_stop <= out <= candidate for every readable combination."""
    for cur in (4.20, 4.2654, 4.30, 4.3183):
        for cand in (4.2654, 4.30, 4.3183, 4.33):
            for sf in (None, 0.001, 0.0046, 0.02):
                for nf in (None, 0.005, 0.0276, 0.10):
                    out, _ = _clamp(candidate=cand, current_stop=cur, ref_price=4.34,
                                    spread_frac=sf, noise_frac=nf)
                    if cand <= cur:
                        assert out == cand
                    else:
                        assert cur - 1e-12 <= out <= cand + 1e-12, (cur, cand, sf, nf, out)


def test_multipliers_are_the_existing_ratios_not_new_knobs():
    sig = inspect.signature(stop_tighten_noise_clamp)
    assert sig.parameters["spread_mult"].default == 1.5   # volnorm_trail_dist_pct.spread_floor_mult
    assert sig.parameters["noise_mult"].default == 1.0    # #1278 stop_noise_floor_decision ratio


# ── 5. the shadow at the C4 site (query-free, fail-open) ─────────────────────


def _canf_ring(t: float) -> list:
    # [epoch, bid] samples as _burst_track_push stores them; the first is stale (>60s)
    return [[t - 70.0, 4.20], [t - 50.0, 4.37], [t - 40.0, 4.33],
            [t - 30.0, 4.25], [t - 10.0, 4.34], [t - 2.0, 4.28]]


def test_shadow_replays_canf_from_the_burst_ring():
    t = 1_756_811_455.220  # 2026-09-02 11:10:55.220Z
    le = {"burst_track": _canf_ring(t)}
    out = LR._c4_tighten_noise_shadow(
        le, avg=CANF_AVG, bid=CANF_BID, ask=CANF_ASK,
        candidate=CANF_TIGHTEN, current_stop=CANF_STOP0, now_epoch=t,
    )
    assert out["nf_noise_samples"] == 5                       # the 70s-old sample is excluded
    assert out["nf_noise_frac"] == pytest.approx(0.12 / CANF_AVG, abs=1e-9)
    assert out["nf_spread_frac"] == pytest.approx(0.02 / 4.34, abs=1e-9)
    assert out["nf_floor_term"] == "noise"
    assert out["nf_clamped_stop"] == pytest.approx(CANF_STOP0, abs=1e-12)
    assert out["nf_candidate_inside_noise"] is True and out["nf_would_move"] is True
    assert "nf_reason" not in out
    assert le == {"burst_track": _canf_ring(t)}               # read-only on the ledger


def test_shadow_without_ring_falls_back_to_spread_only():
    out = LR._c4_tighten_noise_shadow(
        {}, avg=CANF_AVG, bid=CANF_BID, ask=CANF_ASK,
        candidate=CANF_TIGHTEN, current_stop=CANF_STOP0, now_epoch=1.0,
    )
    assert out["nf_noise_samples"] == 0 and out["nf_noise_frac"] is None
    assert out["nf_floor_term"] == "spread"
    assert out["nf_clamped_stop"] == pytest.approx(4.31, abs=1e-6)


@pytest.mark.parametrize("le", [{"burst_track": "garbage"}, {"burst_track": [[None, None], "x", []]}, None])
def test_shadow_never_raises_and_is_fail_open(le):
    out = LR._c4_tighten_noise_shadow(
        le, avg=CANF_AVG, bid=None, ask=None,
        candidate=CANF_TIGHTEN, current_stop=CANF_STOP0, now_epoch=1.0,
    )
    assert out["nf_clamped_stop"] == CANF_TIGHTEN
    assert out["nf_reason"] == "no_measure"


# ── 6. AST guards: shadow only, wired once, no DB call, applied stop unchanged ─


def _fn_of(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _call_sites(tree: ast.AST, callee: str) -> list[str]:
    """Enclosing function name of every Call to ``callee`` (module-level = '<module>')."""
    sites: list[str] = []

    def _walk(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _walk(child, child.name)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == callee:
                sites.append(fn)
            _walk(child, fn)

    _walk(tree, "<module>")
    return sites


def test_shadow_is_wired_exactly_once_inside_the_tighten_emit():
    tree = ast.parse(inspect.getsource(LR))
    assert len(_call_sites(tree, "_c4_tighten_noise_shadow")) == 1
    hits = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_emit"
            and len(node.args) >= 3 and isinstance(node.args[2], ast.Constant)
            and node.args[2].value == "viability_degraded_tighten"
        ):
            inner = [
                c for c in ast.walk(node.args[3])
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == "_c4_tighten_noise_shadow"
            ]
            hits += len(inner)
    assert hits == 1, "the shadow must be spread into the viability_degraded_tighten payload"


def test_clamp_is_not_applied_by_any_stop_writer():
    tree = ast.parse(inspect.getsource(LR))
    # the only consumer of the pure clamp is the shadow function itself
    assert set(_call_sites(tree, "stop_tighten_noise_clamp")) == {"_c4_tighten_noise_shadow"}
    # and the C4 writer still places max(_live_stop_c4, avg * 0.995) verbatim
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "tighter_stop"
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "max" and len(node.value.args) == 2
            and isinstance(node.value.args[0], ast.Name) and node.value.args[0].id == "_live_stop_c4"
        ):
            found = True
    assert found, "C4 applied-stop line changed — the shadow patch must not alter the placed stop"


def test_shadow_makes_no_db_call():
    fn = _fn_of(ast.parse(inspect.getsource(LR)), "_c4_tighten_noise_shadow")
    banned_attr = {"execute", "query", "fetchall", "scalar", "scalars", "get"}
    banned_name = {"optional_fetchall", "text", "_own_tape_noise_floor_pct", "_live_realized_vol"}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                assert f.id not in banned_name, f"DB read {f.id} on the exit-critical path"
            elif isinstance(f, ast.Attribute):
                # le.get(...) / meta.get(...) are dict reads; a db.execute/query is not allowed
                if f.attr in banned_attr and isinstance(f.value, ast.Name) and f.value.id == "db":
                    raise AssertionError(f"db.{f.attr} on the exit-critical path")
    assert "db" not in {a.arg for a in fn.args.args + fn.args.kwonlyargs}
