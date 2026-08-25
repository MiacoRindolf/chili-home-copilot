"""Ang isang source guard ay hindi dapat maghula kung gaano kalaki ang binabantayan.

ANG PANGYAYARI (2026-08-25). Ang
``test_scheduler_startup_restores_durable_circuit_breaker`` ay bumagsak sa
``main``. Ang mukha nito ay isang paglabag sa Hard Rule 1 -- na hindi na
naibabalik ang kill switch sa startup. Ang totoo ay nandoon pa rin ang dalawang
tawag at tama pa rin ang pagkakasunod. Ang bumagsak ay ang test::

    body = src[idx : idx + 6200]        # <- hula

Ang ``start_scheduler`` ay **117,271** character na. Ang window ay **6,200**:
**5.3%**. Ang test ay tumigil sa pagbabantay ng bagay na dahilan ng pag-iral
nito, pagkatapos ay bumagsak sa isang dahilang walang kinalaman doon -- ang
pinakamasamang klase ng pagkabigo, dahil ang mukha nito ay isang insidente sa
kaligtasan.

Sinuri ang buong suite noong araw na iyon: **7** na ganitong slice ang mas maikli
kaysa sa binabantayan nila. Ang lahat ay may POSITIBONG assertion, kaya lahat ay
babagsak nang maingay -- pero ang karaniwang tugon sa isang maingay na bumabagsak
na test ay ang pagtaas ng numero, at babalik lang ito sa susunod na paglaki.

Ang tsekeng ito ang pumipigil doon. Hindi nito hinahatulan ang laki ng window;
inihahambing nito ang window sa TUNAY na sukat ng function na kinukuha ng
anchor. Ang AST ang nakakaalam ng dulo.

Runnable: pytest tests/test_source_region_windows_are_not_guesses.py -v
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from tests.source_region import function_body, read_source

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent

_SLICE = re.compile(
    r"=\s*(?:src|SRC|html|body|block|seg)\s*\[\s*([A-Za-z_]\w*)\s*:\s*\1\s*\+\s*(\d{3,})\s*\]"
)
_ANCHOR = re.compile(r'\.find\(\s*[\'"](?:def\s+|async\s+def\s+)?([A-Za-z_]\w*)')
_PATH = re.compile(r'["\']((?:app|scripts)/[\w/]+\.py)["\']')


def _spans(path: pathlib.Path) -> dict[str, int]:
    src = read_source(path)
    lines = src.split("\n")
    return {
        n.name: len("\n".join(lines[n.lineno - 1 : n.end_lineno]))
        for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _undersized() -> list[tuple[str, str, int, int]]:
    cache: dict[str, dict[str, int]] = {}
    out: list[tuple[str, str, int, int]] = []
    for tf in sorted(TESTS.glob("test_*.py")):
        txt = read_source(tf)
        try:
            tree = ast.parse(txt)
        except SyntaxError:  # pragma: no cover - hinuhuli ng ibang test
            continue
        lines = txt.split("\n")
        module_paths = _PATH.findall(txt)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = "\n".join(lines[fn.lineno - 1 : fn.end_lineno])
            m = _SLICE.search(seg)
            anchor = _ANCHOR.search(seg)
            if not m or not anchor:
                continue
            win, name = int(m.group(2)), anchor.group(1)
            # ⚠️ Itali ang source path sa TEST MISMO muna. Ang isang test file ay
            # maaaring bumasa ng maraming source file, at ang pagpili ng una sa
            # buong file ay nagtatago ng mga hit -- halos nalampasan ko ang mismong
            # test na bumagsak dahil doon.
            for rel in (_PATH.findall(seg) or module_paths):
                cache.setdefault(rel, _spans(REPO / rel))
                span = cache[rel].get(name)
                if span is None:
                    continue
                if span > win:
                    out.append((tf.name, name, win, span))
                break
    return out


def test_no_fixed_window_is_smaller_than_what_it_claims_to_guard():
    bad = _undersized()
    if bad:
        detail = "\n".join(
            f"    {f}: window {w} laban sa {name}() na {s} char ({100*w/s:.1f}%)"
            for f, name, w, s in bad
        )
        pytest.fail(
            "May source guard na sumusuri ng mas maliit kaysa sa binabantayan nito.\n"
            "Ang isang window na mas maikli sa function ay hindi bumabantay --\n"
            "gamitin ang tests.source_region.function_body() sa halip na maghula:\n"
            + detail
        )


def test_function_body_reaches_the_real_end_not_a_guess():
    """Ang mismong tawag na ikinabagsak ay nasa offset 10806 -- lampas sa 6200."""
    body = function_body(REPO / "app/services/trading_scheduler.py", "start_scheduler")
    assert len(body) > 6200, "ang function ay mas malaki kaysa sa lumang window"
    assert "restore_kill_switch_from_db()" in body
    assert "restore_breaker_from_db()" in body


def test_the_kill_switch_still_precedes_the_breaker():
    """⚠️ HARD RULE 1. Ito ang tunay na tinatanong ng bumagsak na test, at ito ang
    hindi nito nasagot sa loob ng hindi malamang gaano katagal."""
    body = function_body(REPO / "app/services/trading_scheduler.py", "start_scheduler")
    assert 0 < body.find("restore_kill_switch_from_db()") < body.find("restore_breaker_from_db()")


def test_a_missing_function_raises_instead_of_returning_empty():
    """⚠️ Ang tahimik na walang laman ay gagawing walang-laman na pumapasa ang
    bawat negatibong assertion na susunod. Mas mabuti ang malakas na pagsabog."""
    with pytest.raises(AssertionError, match="walang function"):
        function_body(REPO / "app/main.py", "_walang_ganitong_function_kailanman")


def test_the_helper_survives_a_byte_order_mark():
    """Ang ilang file dito ay may BOM; ang plain utf-8 ay nagbibigay ng
    'invalid non-printable character U+FEFF'. Nadapa rito ang sarili kong
    checker bago naisulat ang helper."""
    body = function_body(REPO / "app/main.py", "lifespan")
    assert body.lstrip().startswith(("async def lifespan", "def lifespan"))
    assert "\ufeff" not in body
