"""Kunin ang TUNAY na hangganan ng isang function mula sa source.

BAKIT ITO UMIIRAL. Maraming test dito ang nagbabantay ng startup ordering sa
pamamagitan ng pag-slice ng source sa isang NAKAPIRMING bilang ng character::

    idx = src.find("def start_scheduler(")
    body = src[idx : idx + 6200]          # <- hula
    assert body.find("restore_kill_switch_from_db()") > 0

Ang hulang iyon ay nabubulok. Noong 2026-08-25 ay bumagsak ang tsekeng iyon --
hindi dahil nawala ang tawag, kundi dahil ang ``start_scheduler`` ay lumaki na sa
**117,271** character habang ang window ay **6,200** pa rin: **5.3%** ng function.
Ang test ay tumigil sa pagbabantay ng bagay na dahilan ng pag-iral nito, tapos
bumagsak sa isang dahilang walang kinalaman doon.

Sinuri ang buong suite: **7** na ganitong slice ang mas maikli kaysa sa
binabantayan nila. **Wala** sa kanila ang may negatibong assertion, kaya ang
lahat ay bumabagsak nang MAINGAY sa halip na tahimik na pumasa -- pero ang
maingay na bumabagsak na test ay kadalasang tinataasan lang ang numero, at ang
susunod na paglaki ay babalik lang ito.

Ang AST ang nakakaalam ng tunay na dulo. Walang hulaan.

⚠️ Sinasadya nitong hindi hawakan ang mga nested def: ang hinihingi ay ang
LEXICAL na saklaw ng pinangalanang function, kasama ang lahat ng nasa loob nito
-- iyon nga ang tinatanong ng isang ordering guard.
"""
from __future__ import annotations

import ast
import io
import pathlib


def read_source(path: str | pathlib.Path) -> str:
    """⚠️ utf-8-sig, hindi utf-8. Ang ilang file dito ay may BOM, at ang plain
    utf-8 ay nagbibigay ng ``invalid non-printable character U+FEFF``. Ang sarili
    kong checker ay nadapa rito bago ito naisulat."""
    return io.open(path, encoding="utf-8-sig").read()


def function_body(path: str | pathlib.Path, name: str) -> str:
    """Ang buong source ng ``name``, mula sa ``def`` hanggang sa tunay nitong dulo.

    Nagre-raise kung wala -- ang isang nawawalang function ay isang tunay na
    regression, at ang tahimik na pagbabalik ng walang laman ay gagawing
    walang-laman na pumapasa ang bawat negatibong assertion na susunod.
    """
    src = read_source(path)
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"walang function na {name!r} sa {path}")


def call_order(body: str, *names: str) -> list[int]:
    """Ang offset ng bawat pangalan sa loob ng ``body``; -1 kapag wala.

    Ang tinatanong ng isang startup guard ay hindi "nandiyan ba" kundi
    "nauuna ba" -- ibinabalik nito ang hilaw na posisyon para masagot iyon.
    """
    return [body.find(n) for n in names]
