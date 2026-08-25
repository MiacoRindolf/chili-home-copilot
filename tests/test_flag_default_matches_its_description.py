"""Ang deskripsyon ng isang flag ay hindi dapat sumalungat sa sarili nitong default.

ANG NAKAWALA (2026-08-24). Limang boolean na setting ang may ``default=True``
habang ang sariling ``description`` nila ay nagsasabing FALSE/OFF::

    chili_momentum_overnight_tape_enabled            "DEFAULT FALSE"
    chili_momentum_bull_flag_entry_enabled           "Ship DARK (default FALSE
                                                      -- NEW + never-run)"
    chili_momentum_adaptive_spread_cost_veto_enabled "DEFAULT FALSE = byte-identical"
    chili_momentum_pullback_raw_break_when_explosive "Default OFF"
    chili_momentum_explosive_recalibration_enabled   "(all default off)"

Lahat sila ay dumating nang ``default=True`` sa ``ac850b1b7`` (#1024) -- isang
**13,817-linyang** muling pagsulat ng ``config.py`` -- habang ang mga tekstong
isinulat para sa isang dark ship ay dinala nang buo.

BAKIT ITO MAHALAGA, at hindi cosmetic:

- Ang ``bull_flag_entry_enabled`` ay isang BUHAY na entry trigger
  (``entry_gates.py:3355``). Ang teksto nito ay nagsasabing "never-run; operator
  ramps" -- babasahin iyon ng sinuman bilang PATAY.
- Ang ``adaptive_spread_cost_veto_enabled`` ay humahawak ng ENTRY SIZING
  (``live_runner.py:33784``), at ang docstring ng consumer nito ay nagsasabi rin
  ng "(default False)".
- Ang ``overnight_tape_enabled`` ay TAHIMIK na nagpapula ng isang test nang
  ma-flip ito, at nanatiling nagsisinungaling ang doc nang mga dalawang linggo
  (naayos sa #1143).

⚠️ ANG DEFAULT ANG TAMA, HINDI ANG TEKSTO. Ang patakaran dito ay *no dark flags --
implement LIVE + ON*. Huwag "ayusin" ang paglihis na ito sa pamamagitan ng
pagbabaligtad ng default; iyon ay magpapatay ng buhay na gawi. Itama ang teksto.

Runnable: pytest tests/test_flag_default_matches_its_description.py -v
"""
from __future__ import annotations

import ast
import io
import pathlib
import re

_CONFIG = pathlib.Path(__file__).resolve().parents[1] / "app" / "config.py"

# Mga pariralang nag-aangking ang default ay OFF.
_CLAIMS_OFF = re.compile(
    r"default\s*(is\s*)?(false|off|disabled)"
    r"|disabled by default|off by default"
    r"|DEFAULT\s+FALSE|DEFAULT\s+OFF",
    re.IGNORECASE,
)
# Mga pariralang nag-aangking ang default ay ON -- kasama ang tahasang pagwawasto,
# para ang isang naitamang teksto ay hindi na muling maituring na salungat.
_CLAIMS_ON = re.compile(
    r"PAGWAWASTO 2026-08-24"
    r"|default\s*(is\s*)?(true|on|enabled)"
    r"|enabled by default|on by default"
    r"|DEFAULT\s+TRUE|DEFAULT\s+ON",
    re.IGNORECASE,
)


def _bool_fields() -> list[tuple[str, bool, int, str]]:
    """(pangalan, default, linya, deskripsyon) para sa bawat bool na Field."""
    src = io.open(_CONFIG, encoding="utf-8").read()
    out: list[tuple[str, bool, int, str]] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "Field"):
            continue
        default = None
        desc = ""
        for kw in call.keywords:
            if kw.arg == "default":
                try:
                    default = ast.literal_eval(kw.value)
                except Exception:
                    default = None
            elif kw.arg == "description":
                try:
                    desc = ast.literal_eval(kw.value)
                except Exception:
                    desc = ""
        if isinstance(default, bool) and desc:
            out.append((node.target.id, default, node.lineno, desc))
    return out


def test_the_scan_actually_sees_the_settings():
    """Bantay laban sa isang tahimik na walang-laman na tseke: kung mabigo ang
    parsing, ang tunay na tseke sa ibaba ay magiging walang kabuluhang pass."""
    fields = _bool_fields()
    assert len(fields) > 200, f"inaasahan ang daan-daang bool na setting, nakuha {len(fields)}"


def test_no_default_true_flag_claims_it_defaults_off():
    """⚠️ ANG PANGUNAHING BANTAY. Isang buhay na flag na ang teksto ay nagsasabing
    patay ito ay mas masahol pa sa walang dokumentasyon."""
    bad = [
        f"config.py:{ln}  {name}  (default=True)"
        for name, default, ln, desc in _bool_fields()
        if default is True and _CLAIMS_OFF.search(desc) and not _CLAIMS_ON.search(desc)
    ]
    assert not bad, (
        "ang deskripsyon ng flag ay nagsasabing OFF pero ang default ay TRUE:\n  "
        + "\n  ".join(bad)
        + "\n\nItama ang TEKSTO, hindi ang default -- ang patakaran ay 'no dark flags: LIVE + ON'."
    )


def test_no_default_false_flag_claims_it_defaults_on():
    """Ang kabaligtaran na paglihis ay pantay ding nakakalito."""
    bad = [
        f"config.py:{ln}  {name}  (default=False)"
        for name, default, ln, desc in _bool_fields()
        if default is False and _CLAIMS_ON.search(desc) and not _CLAIMS_OFF.search(desc)
    ]
    assert not bad, (
        "ang deskripsyon ng flag ay nagsasabing ON pero ang default ay FALSE:\n  "
        + "\n  ".join(bad)
    )
