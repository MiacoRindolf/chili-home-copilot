"""Ang mga test na nagbabasa ng source ay dapat magsabi ng ``encoding="utf-8"``.

ANG NAKAWALA (2026-08-24). Ang ``Path(...).read_text()`` na walang ``encoding``
ay gumagamit ng ``locale.getpreferredencoding()`` -- **cp1252 sa makinang ito**.
Ang repo na ito ay UTF-8 at puno ng komentong Filipino na may ``⚠️``, kaya ang
bawat ganoong tawag ay bomba na may orasan::

    UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f

**Sumabog ito sa akin nang mismong araw na iyon.** Ang PR #1149 ay nagdagdag ng
komentong may ``⚠️`` sa ``broker_service.py``, at agad na nasira ang
``test_broker_stop_construction.py`` -- na nabasa ang file na iyon nang walang
encoding. Naipasa ko iyon sa main bago ko namalayan.

⚠️ KUNG BAKIT HINDI KO NAHULI: masyadong makitid ang regression glob ko
(``^test_(broker_service|autopilot_scope|auto_trader_monitor)``), kaya hindi
tumugma ang ``test_broker_stop_construction``. **Kapag umeedit ka ng isang
module, patakbuhin ang bawat test na BUMABASA nito, hindi lang ang mga kapangalan
nito.**

ANG PANGALAWANG ARAL (2026-08-25). Ang unang bersyon ng bantay na ito ay
naghanap ng ``.read_text()`` gamit ang **line regex**. Nagkamali iyon sa
``test_captured_paper_isolated_stage0.py:74``::

    "value = resources.files('bounddep').joinpath('data.txt').read_text()\n"

Iyon ay **nasa loob ng string literal** -- Python source na isinusulat sa temp
file at pinapatakbo sa subprocess. Hindi iyon test na bumabasa ng source, at ang
pagpilit doon ay walang aayusin.

Iyon din mismo ang pattern na nakasira sa file na iyon noong una: ang blanket
regex ko ay nag-edit sa loob ng string literal at binasag ang syntax nito, kaya
ibinalik ko ang buong file at naiwan ang tsekeng ito na **PULA SA MAIN**
magdamag.

Ngayon ang AST na ang nagsasabi. Ang isang ``Call`` node ay isang tawag; ang
teksto sa loob ng ``Constant`` ay hindi. Ang bantay na may maling alarma ay
sinasanay ang lahat na huwag ito pansinin.

Runnable: pytest tests/test_source_reads_are_utf8.py -v
"""
from __future__ import annotations

import ast
import io
import pathlib

_TESTS = pathlib.Path(__file__).resolve().parent


def _test_files() -> list[pathlib.Path]:
    """Bawat Python file sa suite, KASAMA ang file na ito.

    Ang naunang bersyon ay kailangang laktawan ang sarili dahil naglalaman ito ng
    pattern bilang datos. Ang AST ay walang ganoong problema -- ang isang tawag
    sa loob ng docstring ay hindi ``Call`` node -- kaya ang bantay ay saklaw na
    rin ng sarili nitong panuntunan.
    """
    return sorted(_TESTS.glob("*.py"))


def _bare_read_text_calls(path: pathlib.Path) -> list[int]:
    """Linya ng bawat TUNAY na ``.read_text()`` na walang ``encoding``.

    ⚠️ utf-8-sig: siyam na file sa suite na ito ang may UTF-8 BOM. Ang basahin
    sila bilang plain utf-8 ay nagbibigay ng maling ``SyntaxError`` sa linya 1 --
    nangyari iyon sa unang bersyon ng tsekeng ito at halos napagkamalan kong sira
    ang siyam na file.
    """
    try:
        tree = ast.parse(io.open(path, encoding="utf-8-sig").read())
    except SyntaxError:
        return []
    out: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read_text"
            and not node.args
            and not any(kw.arg == "encoding" for kw in node.keywords)
        ):
            out.append(node.lineno)
    return out


def test_the_scan_sees_the_suite():
    """Bantay laban sa isang walang-laman na tseke na tahimik na pumapasa."""
    files = _test_files()
    assert len(files) > 100, f"inaasahan ang daan-daang test file, nakuha {len(files)}"


def test_the_scan_can_parse_essentially_every_file():
    """⚠️ Ang ``_bare_read_text_calls`` ay nagbabalik ng ``[]`` sa ``SyntaxError``.
    Iyon ay tahimik na pagpasa, kaya bilangin kung ilan ang tinatanggihan --
    hindi dapat ito maging paraan para makatakas ang isang file sa bantay."""
    unparsed = [
        p.name for p in _test_files()
        if not _can_parse(p)
    ]
    assert not unparsed, f"hindi ma-parse (kaya hindi nasusuri): {unparsed}"


def _can_parse(path: pathlib.Path) -> bool:
    try:
        ast.parse(io.open(path, encoding="utf-8-sig").read())
        return True
    except SyntaxError:
        return False


def test_no_test_reads_a_file_without_declaring_utf8():
    """⚠️ ANG PANGUNAHING BANTAY. Ang bare na ``read_text()`` ay pumapasa sa ASCII
    at bumabagsak sa unang ``⚠️`` na maidadagdag ng sinuman sa module na
    binabasa nito -- kabiguang walang kinalaman sa test mismo."""
    offenders = [
        f"{p.name}:{n}" for p in _test_files() for n in _bare_read_text_calls(p)
    ]
    assert not offenders, (
        "bare read_text() -- gumagamit ng cp1252 sa makinang ito at sasabog sa "
        f"anumang non-ASCII:\n    " + "\n    ".join(offenders)
    )


def test_a_read_text_inside_a_string_literal_is_not_an_offender(tmp_path):
    """⚠️ ANG MALING ALARMA NA NAGDULOT NG PANGALAWANG ARAL.

    Ang generated source na nakabalot sa string ay hindi tawag. Ang line regex ay
    hindi kayang makita ang pagkakaiba; ang AST ay kaya. Nasa suite ang tunay na
    kaso: ``test_captured_paper_isolated_stage0.py:74``.
    """
    f = tmp_path / "probe.py"
    f.write_text(
        "src = (\n"
        "    \"from importlib import resources\n\"\n"
        "    \"value = resources.files('d').joinpath('x.txt').read_text()\n\"\n"
        ")\n",
        encoding="utf-8",
    )
    assert _bare_read_text_calls(f) == []


def test_a_real_bare_call_is_still_an_offender(tmp_path):
    """Kontrol: huwag hayaang maging blind spot ang pag-aayos ng maling alarma."""
    f = tmp_path / "probe.py"
    f.write_text("from pathlib import Path\nx = Path('a').read_text()\n", encoding="utf-8")
    assert _bare_read_text_calls(f) == [2]


def test_a_positional_encoding_is_accepted(tmp_path):
    """``read_text("utf-8")`` ay pumapasa ng encoding sa posisyon -- hindi depekto."""
    f = tmp_path / "probe.py"
    f.write_text("from pathlib import Path\nx = Path('a').read_text('utf-8')\n", encoding="utf-8")
    assert _bare_read_text_calls(f) == []


# --- pangalawang uri ng source hygiene: hindi kilalang escape ----------------
#
# Ang isang Windows na landas sa loob ng di-raw na string (halimbawa ang letrang
# D, tutuldok, backslash, pagkatapos ay "dev") ay naglalaman ng isang backslash na
# sinusundan ng d. HINDI iyon kilalang escape. Pinapanatili ito ng Python nang
# literal ngayon, kaya gumagana ang assertion sa aksidente -- pero
# DeprecationWarning na ito at magiging SyntaxError. Apat ang ganito sa file na
# ako mismo ang sumulat (#1141).
#
# HINDI ito nagbibilang sa pamamagitan ng warnings. May per-location na registry
# ang warnings, at ang pag-parse ng buong suite sa IISANG proseso ay nagbibigay
# ng resultang nakadepende sa pagkakasunod -- iniulat nito sa akin ang isang file
# na wala palang depekto, at muntik ko nang inayos ang hindi sira. Ang source
# segment ang tinitingnan dito, kaya deterministiko ang sagot.
#
# Sinasadyang walang literal na backslash sa code sa ibaba. Ang chr(92) ang
# ginagamit, dahil ang mismong pagsulat ng escape sa isang bantay laban sa
# escape ay paulit-ulit na nakakasira ng tooling na dinadaanan nito.

_BACKSLASH = chr(92)
_VALID_ESCAPES = set("abfnrtv0123456789xNuU") | {chr(34), chr(39), chr(92), chr(10)}
_RAW_PREFIXES = ("r", "br", "fr", "rb", "rf")


def _unknown_escapes(path: pathlib.Path) -> list[tuple[int, str]]:
    """Linya at escape ng bawat hindi kilalang sequence sa di-raw na literal."""
    try:
        src = io.open(path, encoding="utf-8-sig").read()
    except OSError:
        return []
    # Mabilis na pagtanggi. Karamihan ng file ay walang backslash kahit saan, at
    # ang pag-parse ay ang mahal na bahagi.
    if _BACKSLASH not in src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    # HATIIN ANG SOURCE NANG MINSAN. Ang ast.get_source_segment ay muling
    # hinahati ang BUONG file sa bawat tawag; sa ~200 test file na may libo-libong
    # string ay naging O(n**2) iyon at umabot sa lampas 10 minuto ang suite.
    lines = src.splitlines(keepends=True)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.end_lineno is None or node.end_lineno - node.lineno > 400:
            continue
        if node.lineno == node.end_lineno:
            seg = lines[node.lineno - 1][node.col_offset:node.end_col_offset]
        else:
            first = lines[node.lineno - 1][node.col_offset:]
            mid = lines[node.lineno:node.end_lineno - 1]
            last = lines[node.end_lineno - 1][:node.end_col_offset]
            seg = first + "".join(mid) + last
        if seg[:2].lower().startswith(_RAW_PREFIXES):
            continue
        i = 0
        while i < len(seg) - 1:
            if seg[i] == _BACKSLASH:
                if seg[i + 1] not in _VALID_ESCAPES:
                    out.append((node.lineno, _BACKSLASH + seg[i + 1]))
                i += 2
                continue
            i += 1
    return out


def test_no_test_file_relies_on_an_unknown_escape_sequence():
    """Gumagana ito ngayon at magiging SyntaxError. Ang raw string ang tama."""
    bad = sorted({
        f"{p.name}:{n} {esc!r}"
        for p in _test_files()
        for n, esc in _unknown_escapes(p)
    })
    assert not bad, (
        "hindi kilalang escape sa di-raw na string (gumamit ng raw string): "
        + ", ".join(bad)
    )


def test_the_escape_scan_finds_a_planted_one(tmp_path):
    """Kontrol: patunayan na hindi ito walang-laman na pumapasa."""
    f = tmp_path / "probe.py"
    f.write_text("x = " + chr(34) + "D:" + chr(92) + "dev" + chr(92) + "wt"
                 + chr(34) + chr(10), encoding="utf-8")
    assert [e for _n, e in _unknown_escapes(f)] == [chr(92) + "d", chr(92) + "w"]


def test_a_raw_string_is_not_flagged(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text("x = r" + chr(34) + "D:" + chr(92) + "dev" + chr(34) + chr(10),
                 encoding="utf-8")
    assert _unknown_escapes(f) == []
