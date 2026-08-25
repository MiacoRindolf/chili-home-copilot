"""Ang import sa isang arm ng if/else ay hindi maaaring gamitin sa kabilang arm.

ANG MEKANISMO. Sa Python, ang anumang binding sa loob ng isang function -- kasama
ang ``import`` -- ay gumagawa ng LOKAL na pangalan para sa **buong** function, hindi
lamang para sa block na kinaroroonan nito. Kaya ito ay sumasabog::

    def f(cond):
        if cond:
            from json import loads
            return loads("[1]")
        else:
            return loads("[2]")   # UnboundLocalError

Napatunayan sa mismong interpreter na ito:
``UnboundLocalError: cannot access local variable 'loads' where it is not
associated with a value``.

ANG PANGYAYARI (2026-08-25). Ang ``_recent_mfe_samples`` sa ``live_runner.py`` ay
may ganitong hugis: ang ``from .optional_db_read import optional_fetchall`` ay
nasa loob ng ``if _epoch is not None:`` (replay path) habang ang ``else`` (LIVE
path) ay tumatawag din nito.

⚠️ TAHIMIK ITO. Ang buong function ay nakabalot sa ``try/except`` na fail-OPEN
(``any error => []``), kaya ang live path ay nagbabalik ng WALANG LAMAN NA
LISTAHAN magpakailanman -- walang traceback, walang log, walang bakas. Ang
uri ng pagkabigo na hindi mo mahahanap sa pagbabasa ng log dahil walang
isinusulat.

⚠️ NAKABAOD ANG PINSALA, at tapat itong sabihin: **isang**
``momentum_mfe_realized`` na event lamang ang naitala sa 30 araw, kaya wala
namang datos na nawala ngayon. Ang halaga ng pag-aayos ay hindi ang nakaraan --
kapag naayos ang emitter, tahimik sanang bubuwagin ito ng bug na ito.

SINAGAP ANG BUONG ``app/``: **isa** lamang ang ganitong hugis. Nakahiwalay na
depekto, hindi klase -- pero ang klase ang pinipigilan nitong bumalik.

Runnable: pytest tests/test_no_branch_local_import_used_in_sibling_branch.py -v
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
APP = REPO / "app"


def _imported(nodes) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    out[a.asname or a.name.split(".")[0]] = sub.lineno
    return out


def _loaded(nodes) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                out.setdefault(sub.id, sub.lineno)
    return out


def _offenders(tree: ast.AST, path: str) -> list[str]:
    """Import sa isang arm, ginagamit sa kabila, at HINDI rin naka-import doon."""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not node.orelse:
            continue
        a_imp, b_imp = _imported(node.body), _imported(node.orelse)
        a_use, b_use = _loaded(node.body), _loaded(node.orelse)
        for nm, ln in a_imp.items():
            if nm in b_use and nm not in b_imp:
                out.append(f"{path}:{ln} import {nm!r} ginagamit sa linya {b_use[nm]} (else-arm)")
        for nm, ln in b_imp.items():
            if nm in a_use and nm not in a_imp:
                out.append(f"{path}:{ln} import {nm!r} ginagamit sa linya {a_use[nm]} (if-arm)")
    return out


def _scan_app() -> list[str]:
    hits: list[str] = []
    for p in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(io.open(p, encoding="utf-8-sig").read())
        except (SyntaxError, OSError):
            continue
        hits.extend(_offenders(tree, str(p.relative_to(REPO)).replace("\\", "/")))
    return hits


def test_the_scan_sees_the_app():
    """Bantay laban sa walang-lamang tseke na tahimik na pumapasa."""
    n = sum(1 for _ in APP.rglob("*.py"))
    assert n > 100, f"inaasahan ang daan-daang module, nakuha {n}"


def test_python_really_does_bind_the_whole_function():
    """⚠️ ANG PREMISE, hindi alaala. Kung magbago ito sa isang bagong Python ay
    dapat malaman natin dito, hindi sa isang tahimik na walang lamang resulta."""
    def demo(cond):
        if cond:
            from json import loads
            return loads("[1]")
        else:
            return loads("[2]")

    assert demo(True) == [1]
    with pytest.raises(UnboundLocalError):
        demo(False)


def test_no_branch_local_import_is_used_in_the_sibling_branch():
    """⚠️ ANG PANGUNAHING BANTAY."""
    bad = _scan_app()
    assert not bad, (
        "may import sa isang arm ng if/else na ginagamit sa kabilang arm -- "
        "UnboundLocalError na lalamunin ng anumang try/except sa paligid:\n    "
        + "\n    ".join(bad)
    )


def test_the_scan_finds_a_planted_one(tmp_path):
    """Kontrol: patunayan na hindi ito walang-lamang pumapasa."""
    f = tmp_path / "probe.py"
    f.write_text(
        "def f(c):\n"
        "    if c:\n"
        "        from json import loads\n"
        "        return loads('[1]')\n"
        "    else:\n"
        "        return loads('[2]')\n",
        encoding="utf-8",
    )
    tree = ast.parse(f.read_text(encoding="utf-8"))
    hits = _offenders(tree, "probe.py")
    assert len(hits) == 1 and "loads" in hits[0]


def test_an_import_in_both_arms_is_not_flagged(tmp_path):
    """Hindi depekto kung parehong arm ang nag-i-import -- huwag mag-alarma roon."""
    f = tmp_path / "probe.py"
    f.write_text(
        "def f(c):\n"
        "    if c:\n"
        "        from json import loads\n"
        "        return loads('[1]')\n"
        "    else:\n"
        "        from json import loads\n"
        "        return loads('[2]')\n",
        encoding="utf-8",
    )
    assert _offenders(ast.parse(f.read_text(encoding="utf-8")), "probe.py") == []
