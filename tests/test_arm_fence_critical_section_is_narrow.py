"""Ang process fence ay dapat hawakan lamang ang MUTATION, hindi ang gating (2026-08-25).

ANG PUWANG. Ang ``_generic_alpaca_arm_process_fence_acquired`` ay kumukuha ng
PostgreSQL advisory XACT lock, at ang gayong lock ay hawak hanggang sa mag-commit
ang transaction ng TUMAWAG. Sa ``begin_live_arm`` ay kinukuha ito sa linya 721
samantalang ang unang pagsulat ay nasa linya 1025 -- **304 na linya** ng purong
gating ang nasa loob ng isang PANDAIGDIGANG mutex nang wala itong pinoprotektahan.

Dahil ang lane ay umaarm ng maraming simbolo nang SABAY sa magkakaibang pooled
connection, ang bawat kapwa arm sa mahabang bintanang iyon ay tumatama sa mutex.

NASUKAT sa buhay na premarket (2026-08-25)::

    phase_seconds={'board_build': 7.56, 'gate_loop': 70.76, 'total': 78.33}
    isang may-hawak na may xact na 6s ang tanda
    fence hawak sa 14/25 pagkatapos 22/25 na sample (56-88%)

    [auto_arm] begin_live_arm blocked AIXI:  captured_paper_service_owns_alpaca_arm_path
    [auto_arm] begin_live_arm blocked RCON:  captured_paper_service_owns_alpaca_arm_path
    [auto_arm] begin_live_arm blocked SWVL:  captured_paper_service_owns_alpaca_arm_path
    [auto_arm] begin_live_arm blocked BDRX:  captured_paper_service_owns_alpaca_arm_path
    [auto_arm] begin_live_arm blocked WVVIP: captured_paper_service_owns_alpaca_arm_path
    [auto_arm] ignition->arm bridge: symbols=[...] armed=0

Lima sa lima -- at ang pangalan ng error ay nangangako ng isang bagay na hindi
totoo: walang captured-paper service na tumatakbo. Ang lane ang gumugutom sa sarili.

⚠️ ANG ARI-ARIANG PINANGANGALAGAAN. Ang sariling docstring ng fence ang nagsasabi
kung ano ang saklaw nito: "a captured service cannot start halfway through an
arm/promote MUTATION." Ang mga testong ito ay nagpapatibay ng eksaktong iyon --
hindi mas mahigpit (na nagdudulot ng gutom) at hindi mas maluwag (na magbubukas ng
puwang kung saan makakasingit ang service).

⚠️ AST, HINDI REGEX. Isang naunang bantay sa repong ito ang gumamit ng nakapirming
character window at TAHIMIK itong nabulok nang lumaki ang function.

Runnable: pytest tests/test_arm_fence_critical_section_is_narrow.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading" / "momentum_neural" / "operator_actions.py"
)

# Ang mga tawag na SUMUSULAT sa daan ng arm. Kung lilitaw ang alinman sa mga ito
# BAGO makuha ang fence, may mutation na nangyayari nang walang proteksyon.
_MUTATING = {
    "acquire_action_claim",
    "create_trading_automation_session",
    "build_session_risk_snapshot",
}

# Kilalang mabagal na gating na dapat NASA LABAS ng critical section.
_GATING = {
    "_live_symbol_arm_lock_acquired",
}


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"walang function na {name!r} sa {_SRC.name}")


def _call_lines(fn: ast.FunctionDef, wanted: set[str] | str) -> list[int]:
    names = {wanted} if isinstance(wanted, str) else wanted
    out: list[int] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        nm = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if nm in names:
            out.append(node.lineno)
    return sorted(out)


def _fence_line(fn: ast.FunctionDef) -> int:
    lines = _call_lines(fn, "_generic_alpaca_arm_process_fence_acquired")
    assert lines, f"{fn.name} ay dapat kumuha pa rin ng process fence"
    return lines[0]


def test_no_mutation_happens_before_the_fence_is_held():
    """⚠️ ANG ARI-ARIANG PANGKALIGTASAN. Bawat pagsulat sa daan ng arm ay dapat nasa
    LOOB ng fence. Kung may mutation na nauuna dito, may puwang kung saan
    makakasingit ang captured-paper service sa kalagitnaan ng pagbabago."""
    fn = _fn("begin_live_arm")
    fence = _fence_line(fn)
    early = [ln for ln in _call_lines(fn, _MUTATING) if ln < fence]
    assert not early, (
        "may pagsulat BAGO makuha ang fence, sa linya "
        f"{early} (fence sa {fence}) -- hindi protektado ang mutation"
    )


def test_the_fence_is_taken_LATE_not_at_the_top():
    """ANG PANGUNAHING KASO. Ang fence ay dapat nasa bingit ng unang pagsulat, hindi
    sa ibabaw ng daan-daang linya ng gating."""
    fn = _fn("begin_live_arm")
    fence = _fence_line(fn)
    writes = _call_lines(fn, _MUTATING)
    assert writes, "ang begin_live_arm ay dapat may pagsulat pa rin"
    gap = writes[0] - fence
    assert 0 < gap <= 40, (
        f"ang fence (linya {fence}) ay {gap} linya bago ang unang pagsulat "
        f"(linya {writes[0]}). Ang malaking puwang ang mismong bug: hawak ang "
        "pandaigdigang mutex sa buong gating at gumugutom sa mga kapwa arm."
    )


def test_the_slow_gating_runs_OUTSIDE_the_fence():
    """Ang naghihintay na per-symbol lock ay dapat nakuha BAGO ang fence -- ito ang
    pumipigil sa double-arm at gusto nating hawak ito habang tumatakbo ang gate,
    pero hindi ito dapat magsama ng pandaigdigang mutex."""
    fn = _fn("begin_live_arm")
    fence = _fence_line(fn)
    gating = _call_lines(fn, _GATING)
    assert gating, "ang per-symbol arm lock ay dapat nananatili"
    assert gating[0] < fence, (
        f"ang per-symbol lock (linya {gating[0]}) ay dapat mauna sa fence "
        f"(linya {fence}) -- kung hindi ay hawak muli ang pandaigdigang mutex sa gating"
    )


def test_the_fence_is_taken_exactly_once_in_the_arm_path():
    """Dalawang pagkuha ay nangangahulugang lumaganap muli ang critical section."""
    fn = _fn("begin_live_arm")
    assert len(_call_lines(fn, "_generic_alpaca_arm_process_fence_acquired")) == 1


@pytest.mark.parametrize("name", ["confirm_live_arm", "promote_paper_session_to_live_arm"])
def test_the_sibling_arm_paths_still_hold_the_fence(name: str):
    """⚠️ HINDI PWEDENG MAWALA ANG FENCE SA IBANG DAAN. Ang pagpapaliit sa
    begin_live_arm ay hindi dapat maging dahilan para tanggalin ito sa iba.
    (Ang mga daang ito ay malamig -- isang tawag kada arm -- kaya hindi sila
    pinapaliit dito; ang gutom ay nasukat sa begin_live_arm lamang.)"""
    assert _call_lines(_fn(name), "_generic_alpaca_arm_process_fence_acquired")


def test_a_rejected_gate_returns_without_ever_touching_the_fence():
    """ANG PAKINABANG, ISINULAT BILANG BANTAY. Ang isang arm na tinanggihan ng gate
    ay dapat bumalik BAGO ang fence, kaya hindi na ito humahawak ng pandaigdigang
    mutex kahit isang sandali -- iyon ang tunay na nagpapalaya sa kapwa nito."""
    fn = _fn("begin_live_arm")
    fence = _fence_line(fn)
    early_returns = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Return) and n.lineno < fence
    ]
    assert len(early_returns) >= 5, (
        "inaasahan ang maraming maagang pagtanggi ng gate bago ang fence; "
        f"nakita lang ang {len(early_returns)} -- lumipat ba pataas ang fence?"
    )
