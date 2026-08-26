"""64 na feature sa mainit na landas ang ipinadala at hindi kailanman binuksan.

NASUKAT (2026-08-26, mula sa AST ng `app/config.py`)::

    kabuuang bool flag sa Settings                748
    naka-ON  (default=True)                       429
    DARK     (default=False)                      119
    dark sa MAINIT na landas                       69
      └ buhay lamang dahil sa env ng operator        5
      └ TALAGANG patay                              64

⚠️ Kasama sa limang "buhay lamang dahil sa env" ang
**`chili_momentum_live_runner_enabled`** -- ang live runner MISMO ay ipinapadala
nang `default=False`. Ang sinumang magbabasa ng repo ay maghihinuhang hindi
nagta-trade si CHILI.

TATLONG BESES ngayong araw ang isang dark flag ang naging harang:

1. `exit_ladder_live` + 2 kapatid -- naghihintay ng A/B na HINDI KAILANMAN
   darating (zero counterfactual sa 30 araw, dahil walang trade na
   maoobserbahan). Capture ratio 18.5%. Binuksan sa #1185.
2. `event_based_abandonment_enabled` -- ang DESCRIPTION ay nagsasabing
   "OFF (default)" gayong ang default ay `True`. Naling-lang ako mismo nito.
3. `broker_truth_reconciliation_enabled` -- patay, at ito sana ang naglinis ng
   saradong posisyon ng CDTG. Ang stale na hilerang iyon ang nag-defer ng
   **15 entry** ngayong hapon.

ANG ARAL: ang "ship dark, promote after A/B" ay tunog maingat pero HINDI ito
nagsasara -- ang A/B ay nangangailangan ng trade, at ang trade ay hinaharangan
ng flag. Ang flag na naka-OFF nang walang nakasulat na petsa ng pagsusuri ay
hindi maingat; ito ay nakalimutan.

Ang testong ito ay hindi nag-uutos ng pagbaba. Pumipigil lamang ito ng PAGLAKI:
ang bawat bagong dark flag sa mainit na landas ay dapat isang pasya, hindi isang
default.

Runnable: pytest tests/test_dark_flag_census_does_not_grow.py -v
"""
from __future__ import annotations

import ast
import pathlib
import re

_CONFIG = pathlib.Path(__file__).resolve().parents[1] / "app" / "config.py"

# Ang nasukat na baseline noong 2026-08-26. Bawasan ito kapag may naalis o
# nabuksan; ang PAGTAAS ay dapat maging sinasadyang pasya na may ebidensya.
DARK_HOT_PATH_BASELINE = 69

_HOT = re.compile(r"momentum|entry|exit|arm|ross|halt|scal|partial|ladder|runner")


def _bool_flags() -> tuple[list[str], list[str]]:
    """(dark, on) na pangalan ng `bool` na field sa Settings."""
    tree = ast.parse(_CONFIG.read_text(encoding="utf-8"))
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "Settings"
    )
    dark, on = [], []
    for st in cls.body:
        if not isinstance(st, ast.AnnAssign):
            continue
        if getattr(st.annotation, "id", None) != "bool":
            continue
        if not isinstance(st.target, ast.Name):
            continue
        value = None
        if isinstance(st.value, ast.Call):
            for kw in st.value.keywords:
                if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                    value = kw.value.value
        elif isinstance(st.value, ast.Constant):
            value = st.value.value
        if value is False:
            dark.append(st.target.id)
        elif value is True:
            on.append(st.target.id)
    return dark, on


def test_the_census_is_readable_at_all():
    """Kung hindi na mabasa ang hugis ng config, walang bantay na gagana."""
    dark, on = _bool_flags()
    assert len(dark) + len(on) > 500, "inaasahang daan-daang bool flag"


def test_the_hot_path_dark_count_does_not_grow():
    """ANG BANTAY. Ang bawat BAGONG dark flag sa mainit na landas ay dapat isang
    pasya na may ebidensya, hindi isang default na tahimik na naidagdag."""
    dark, _ = _bool_flags()
    hot = sorted(f for f in dark if _HOT.search(f))
    assert len(hot) <= DARK_HOT_PATH_BASELINE, (
        "ang dark na flag sa mainit na landas ay tumaas mula %d tungong %d.\n"
        "Kung sinasadya ito, ibaba ang layunin AT itala kung kailan ito susuriin.\n"
        "Ang mga bago: hanapin sa listahan sa docs/AUDIT/DARK_FLAG_CENSUS_2026-08-26.md\n"
        "kasalukuyan: %r" % (DARK_HOT_PATH_BASELINE, len(hot), hot))


def test_the_promoted_exit_levers_did_not_regress():
    """⚠️ Ang tatlong exit lever na binuksan sa #1185 ay hindi dapat tahimik na
    bumalik sa dark -- iyon mismo ang paraan kung paano nawala ang capture ratio
    nang ilang linggo."""
    dark, _ = _bool_flags()
    for flag in (
        "chili_momentum_exit_ladder_live",
        "chili_momentum_exit_ofi_hidden_seller_enabled",
        "chili_momentum_exit_ofi_lock_partial_enabled",
    ):
        assert flag not in dark, "%s ay bumalik sa default=False" % flag


def test_the_census_document_exists():
    """⚠️ Ang bilang na walang listahan ay hindi maaaksyunan. Ang dokumento ang
    nagsasabi KUNG ALIN, para hindi ito mabaon gaya ng dati."""
    doc = _CONFIG.parents[1] / "docs" / "AUDIT" / "DARK_FLAG_CENSUS_2026-08-26.md"
    assert doc.exists(), "nawawala ang census document"
    body = doc.read_text(encoding="utf-8")
    assert "chili_momentum_live_runner_enabled" in body, (
        "ang pinaka-nakakagulat na kaso ay dapat nakalista")
    assert "TALAGANG patay" in body
