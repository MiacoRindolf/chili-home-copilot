"""Ang micro-bar ay 10 segundo, at IISANG knob ang naghahawak nito (2026-08-25).

BAKIT 10. Si Ross mismo, sa buhay na stream ngayong umaga, habang ipinapaliwanag ang
isang micro-pullback na kinuha niya sa DAIC matapos ang break ng 3.75::

    "the 10 SECOND does kind of show that pattern, but not sure that many people
     would notice that"

Iyon ang frame na tinitingnan niya para sa geometry na hinahabol din natin. Ang 15s
ay pinili noong 2026-06-15 nang ang reklamo ay "1m too slow"; ito ang susunod na
hakbang ng parehong lohika.

⚠️ FAIL-SAFE ANG PAGPAPALIIT. Ang ``_build_micro_bar_df`` ay nagbabalik ng ``None``
kapag ang tick density ay nagbubunga ng <2 micro-bar, at ang tumatawag ay bumabalik
sa 1m nang byte-identical. Ang isang MANIPIS na pangalan ay hindi makakagawa ng 10s
na bar na wala talaga -- mas mabilis lang itong bumabalik sa 1m kaysa dati. Ang mga
siksik na mover -- ang tanging pangalang tinatrade natin -- ay nagbibigay ng ~168
tick/segundo.

⚠️ HINDI LUMILIIT ANG MGA COOLDOWN. Kinukwenta sila bilang
``max(30.0, 2 * micropull_bar_seconds)``, kaya sa 15s sila ay ``max(30,30)=30`` at sa
10s sila ay ``max(30,20)=30``. Ang 30-segundong sahig ang naghahawak.

⚠️ ANG NAKA-HARDCODE NA KOPYA ANG TUNAY NA BITAG. Dalawang call site ang nakapirmi sa
``bar_seconds=15`` habang ang knob ay umiiral na -- tahimik silang maghihiwalay sa
sandaling baguhin ang knob. May bantay dito ang testong ito.

Runnable: pytest tests/test_micropull_10s_bars.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.config import settings
from app.services.trading.momentum_neural.live_runner import _micropull_bar_seconds

_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"
)


def test_the_default_micro_bar_is_ten_seconds():
    """ANG PANGUNAHING KASO."""
    assert settings.chili_momentum_micropull_bar_seconds == 10


def test_the_micro_path_is_actually_enabled():
    """Ang 10s na bar ay walang saysay kung hindi tumatakbo ang micro path."""
    assert settings.chili_momentum_micropull_enabled is True


def test_no_call_site_hardcodes_a_bar_width():
    """⚠️ ANG BANTAY. Ang isang naka-hardcode na `bar_seconds=15` ay tahimik na
    naghihiwalay sa knob. Dalawa ang ganito bago ang pagbabagong ito."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if name != "_build_micro_bar_df":
            continue
        for kw in node.keywords:
            if kw.arg == "bar_seconds" and isinstance(kw.value, ast.Constant):
                offenders.append((node.lineno, kw.value.value))
    assert not offenders, (
        f"naka-hardcode ang bar_seconds sa linya {offenders} -- dapat ito ay "
        "_micropull_bar_seconds() para hindi maghiwalay sa knob"
    )


def test_every_build_call_passes_a_bar_width():
    """Walang call site na umaasa sa isang default -- ang knob ang laging nagsasalita."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name == "_build_micro_bar_df":
                assert any(kw.arg == "bar_seconds" for kw in node.keywords), (
                    f"linya {node.lineno}: dapat tahasang ipasa ang bar_seconds"
                )


@pytest.mark.parametrize("value,expected", [
    (10, 10), (15, 15), (5, 5), (30, 30),
    (1, 5), (999, 30), (None, 10),
])
def test_the_reader_clamps_to_the_declared_range(monkeypatch, value, expected):
    """Ang deklaradong saklaw (ge=5, le=30) ay ipinapatupad din sa pagbasa, hindi
    lamang ng pydantic -- ang isang env override ay dumadaan sa parehong daan."""
    monkeypatch.setattr(
        settings, "chili_momentum_micropull_bar_seconds", value, raising=False
    )
    assert _micropull_bar_seconds() == expected


def test_the_reader_survives_a_junk_value(monkeypatch):
    """Ang basura ay bumabalik sa base, hindi sumasabog sa gitna ng entry path."""
    monkeypatch.setattr(
        settings, "chili_momentum_micropull_bar_seconds", "kalokohan", raising=False
    )
    assert _micropull_bar_seconds() == 10


def test_the_add_cooldowns_do_NOT_shrink_at_ten_seconds():
    """⚠️ ANG ARI-ARIAN NA MADALING MASIRA. Tatlong cooldown ang kumukwenta ng
    `max(30.0, 2 * bar_seconds)`. Sa 15s: max(30,30)=30. Sa 10s: max(30,20)=30.
    Ang 30-segundong sahig ang naghahawak -- kung mawala iyon, isang wiggle ay
    makakapagpaputok ng dalawang add bago muling mabuo ang istruktura."""
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    hits = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")) != "max":
            continue
        seg = "\n".join(lines[node.lineno - 1: node.end_lineno])
        if "micropull_bar_seconds" not in seg:
            continue
        # ⚠️ HINDI LAHAT NG max() DITO AY COOLDOWN. May isang site na nagko-convert
        # papuntang minuto (`trigger_bar_minutes=max(0.01, bar_seconds/60.0)`) at
        # TAMA na sumusukat iyon kasabay ng bar -- iyon nga ang buong punto ng 10s.
        # Ang COOLDOWN lamang ang dapat may 30-segundong sahig.
        if "cooldown" not in seg.lower():
            continue
        hits += 1
        assert "30" in seg, (
            f"linya {node.lineno}: nawala ang 30s na sahig sa cooldown"
        )
    assert hits >= 3, f"inaasahan ang >=3 na naka-pin na cooldown, nakita ang {hits}"


def test_the_trigger_bar_width_DOES_scale_with_the_knob():
    """Ang kabaligtaran ng nasa itaas: ang conversion papuntang minuto ay dapat
    sumunod sa knob, kung hindi ay magtuturo ang 10s na bar sa 15s na bintana."""
    src = _SRC.read_text(encoding="utf-8")
    assert "trigger_bar_minutes=max(" in src
    idx = src.index("trigger_bar_minutes=max(")
    window = src[idx: idx + 400]
    assert "micropull_bar_seconds" in window, (
        "ang trigger bar width ay dapat kunin ang knob, hindi isang nakapirming numero"
    )


def test_the_failsafe_docstring_still_promises_the_1m_fallback():
    """⚠️ Ang buong kaligtasan ng pagpapaliit ay nakasalalay sa fallback na ito.
    Kung mawala ito, ang isang manipis na pangalan ay makakagawa ng basurang bar."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_micro_bar_df"
    )
    doc = ast.get_docstring(fn) or ""
    assert "None" in doc
    assert "fall" in doc.lower() or "fallback" in doc.lower()
