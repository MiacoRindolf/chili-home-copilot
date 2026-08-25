"""Ang mga test na nagbabasa ng source ay dapat magsabi ng ``encoding="utf-8"``.

ANG NAKAWALA (2026-08-24). Ang `Path(...).read_text()` na walang `encoding` ay
gumagamit ng `locale.getpreferredencoding()` -- **cp1252 sa makinang ito**. Ang
repo na ito ay UTF-8 at puno ng komentong Filipino na may `⚠️`, kaya ang bawat
ganoong tawag ay isang bomba na may orasan::

    UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f

**Sumabog ito sa akin nang mismong araw na iyon.** Ang PR #1149 ay nagdagdag ng
komentong may `⚠️` sa `broker_service.py`, at agad na nasira ang
`test_broker_stop_construction.py` -- na nabasa ang file na iyon nang walang
encoding. Naipasa ko iyon sa main bago ko namalayan.

⚠️ AT ANG DAHILAN KUNG BAKIT HINDI KO NAHULI: masyadong makitid ang regression
glob ko (`^test_(broker_service|autopilot_scope|auto_trader_monitor)`), kaya
hindi tumugma ang `test_broker_stop_construction`. **Kapag umeedit ka ng isang
module, patakbuhin ang bawat test na BUMABASA nito, hindi lang ang mga
kapangalan nito.**

66 na tawag sa 22 na file ang naayos. Ang bantay na ito ay nagpapanatili niyon.

Runnable: pytest tests/test_source_reads_are_utf8.py -v
"""
from __future__ import annotations

import io
import pathlib
import re

_TESTS = pathlib.Path(__file__).resolve().parent

# `.read_text()` na walang anumang argumento -- ang eksaktong depektong pattern.
_BARE_READ_TEXT = re.compile(r"\.read_text\(\s*\)")


_SELF = pathlib.Path(__file__).name


def _test_files() -> list[pathlib.Path]:
    """Bawat test file MALIBAN sa file na ito.

    Ang bantay na ito ay kinakailangang naglalaman ng pattern bilang DATOS
    -- sa docstring at sa regex -- kaya matatapakan nito ang sarili.
    """
    return [p for p in sorted(_TESTS.glob("*.py")) if p.name != _SELF]


def test_the_scan_sees_the_suite():
    """Bantay laban sa isang walang-laman na tseke na tahimik na pumapasa."""
    files = _test_files()
    assert len(files) > 100, f"inaasahan ang daan-daang test file, nakuha {len(files)}"


def test_no_test_reads_a_file_without_declaring_utf8():
    """⚠️ ANG PANGUNAHING BANTAY. Ang isang bare na `read_text()` ay pumapasa sa
    ASCII at bumabagsak sa unang `⚠️` na maidadagdag ng sinuman sa module na
    binabasa nito -- isang kabiguan na walang kinalaman sa test mismo."""
    offenders: list[str] = []
    for path in _test_files():
        # ⚠️ utf-8-SIG: siyam na file sa suite na ito ang may UTF-8 BOM.
        # Ang basahin sila bilang plain utf-8 ay nagbibigay ng maling
        # SyntaxError sa linya 1 -- nangyari iyon sa unang bersyon ng
        # tsekeng ito, at halos napagkamalan kong sira ang siyam na file.
        src = io.open(path, encoding="utf-8-sig").read()
        for n, line in enumerate(src.splitlines(), 1):
            if _BARE_READ_TEXT.search(line):
                offenders.append(f"{path.name}:{n}")
    assert not offenders, (
        "bare read_text() -- gumagamit ng cp1252 sa makinang ito at sasabog sa "
        "anumang non-ASCII:\n  " + "\n  ".join(offenders)
    )


def test_the_repo_really_does_contain_non_ascii_source():
    """Ang panganib ay tunay, hindi teoretikal: may non-ASCII na komento ang
    mga module na binabasa ng mga test na ito."""
    target = _TESTS.parents[0] / "app" / "services" / "broker_service.py"
    raw = target.read_bytes()
    assert any(b > 127 for b in raw), (
        "kung magiging puro-ASCII na ang broker_service.py, muling suriin kung "
        "kailangan pa ang bantay na ito -- pero huwag itong alisin nang basta"
    )
