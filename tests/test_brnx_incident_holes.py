"""Ang tatlong natitirang butas ng BRNX −$82 na insidente (2026-08-27).

ANG INSIDENTE: nag-fill ang BUY 20 BRNX @ 8.58 sa loob ng ilang segundo ng
16:38:24Z na submit, pero (1) ang tick na magpoproseso ng fill ay pinatay ng
row lock (NOWAIT), (2) ang owner-claim recovery ay umikot nang GANAP na tahimik
kada tick, at (3) ang late-fill sweep ay hindi maabot dahil nakaupo ito sa
likod ng eligibility gates at ang BRNX ay hindi na live-eligible matapos
bumagsak. Ang 20 shares ay naiwang walang stop nang 56 minuto sa −47% na
collapse: −$82 sa halip na ~−$5.

Ang reaper hole (#1209) at ang ids_all eraser (ang paulit-ulit na cancel ng
reaper na may entry_state_reset_keys) ay sarado na roon; ito ang tatlong iba.

Runnable: pytest tests/test_brnx_incident_holes.py -v
"""
from __future__ import annotations

import ast
import pathlib

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


def _tick_fn() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "tick_live_session"
    )


# ── Butas 1: bounded lock wait ───────────────────────────────────────────────


def test_the_tick_waits_for_the_row_lock_by_default():
    """ANG PANGUNAHING KASO. Sa 10s na cadence, ang paghihintay ng 800ms sa
    panandaliang research-job lock ay libre; ang nakain na tick ay maaaring ang
    fill-processing tick."""
    assert int(settings.chili_momentum_tick_row_lock_wait_ms) == 800


def test_the_wait_uses_lock_timeout_not_unbounded_blocking():
    """⚠️ Ang walang-hangganang FOR UPDATE ay magpapasabit sa buong batch sa
    likod ng isang na-wedge na row. Ang bound ay dapat SET LOCAL lock_timeout."""
    src = ast.unparse(_tick_fn())
    assert "lock_timeout" in src
    assert "chili_momentum_tick_row_lock_wait_ms" in src


def test_zero_restores_legacy_nowait():
    """0 ⇒ byte-identical legacy — nananatili ang NOWAIT branch."""
    src = ast.unparse(_tick_fn())
    assert "nowait=True" in src, "dapat buhay pa ang legacy NOWAIT branch"


# ── Butas 2: ang tahimik na must_return_early ────────────────────────────────


def test_the_must_return_early_path_emits_once_per_reason():
    """ANG BCCQ TREATMENT. Ang 17370 ay umikot dito kada tick nang walang
    event, walang log — ang stall class na ito ay dapat kitang-kita sa events
    nang walang event-spam."""
    src = ast.unparse(_tick_fn())
    i = src.index("must_return_early")
    region = src[i:i + 2500]
    assert "live_entry_owner_claim_reconcile_block" in region, (
        "ang must_return_early ay dapat nag-e-emit (minsan kada dahilan)"
    )
    assert "owner_recovery_block_sig" in region, "sig-deduped dapat"


# ── Butas 3: ang sweep ay dapat NAUUNA sa eligibility ────────────────────────


def test_the_early_sweep_runs_before_the_entry_gates():
    """ANG PRINSIPYO NG #1194: ang fill ng broker ay outranks ang eligibility.
    Ang pag-a-adopt ay hindi bagong panganib — pagkilala ito sa panganib na
    HAWAK NA natin. Ang sweep ay dapat tumakbo BAGO ang quote/boundary gates."""
    src = ast.unparse(_tick_fn())
    i_early = src.index("late_fill_repointed_early")
    i_boundary = src.index("runner_boundary_risk_ok")
    assert i_early < i_boundary, (
        "ang early sweep ay dapat nauuna sa boundary/eligibility gates"
    )


def test_the_original_sweep_remains_as_backstop():
    src = ast.unparse(_tick_fn())
    assert src.count("_sweep_unresolved_entry_orders(") >= 2, (
        "ang orihinal na sweep ay dapat nananatili bilang backstop"
    )


def test_the_early_sweep_is_gated_and_cheap():
    """Ang early sweep ay dapat naka-gate sa (walang aktibong pointer) AT (may
    hindi-nalutas na id) — kung hindi ay dict lookup lang ito kada tick."""
    src = ast.unparse(_tick_fn())
    i = src.index("late_fill_repointed_early")
    region = src[max(0, i - 1500):i]
    assert "_unresolved_entry_order_ids" in region
    assert "entry_order_id" in region
    assert "chili_momentum_early_late_fill_sweep_enabled" in region


def test_an_early_sweep_error_never_kills_the_tick():
    """⚠️ FAIL-OPEN sa tick: ang pagsabog sa sweep ay hindi dapat pumatay sa
    natitirang bahagi ng tick (ang exit management ay nasa ibaba nito)."""
    src = ast.unparse(_tick_fn())
    i = src.index("late_fill_repointed_early")
    region = src[max(0, i - 2000):i + 500]
    assert "except Exception" in region


# ── Ang mga flag ─────────────────────────────────────────────────────────────


def test_both_fix_flags_ship_ON_with_the_incident_recorded():
    for name in (
        "chili_momentum_early_late_fill_sweep_enabled",
    ):
        assert getattr(settings, name) is True
        desc = str(type(settings).model_fields[name].description or "")
        assert "BRNX" in desc and "2026-08-27" in desc
    desc = str(type(settings).model_fields[
        "chili_momentum_tick_row_lock_wait_ms"].description or "")
    assert "BRNX" in desc
