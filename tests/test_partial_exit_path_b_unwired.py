"""TRIPWIRE: ang PATH B ay HINDI PA nakakabit, at nananatiling totoo ang mga
premise na nagdulot ng pagpapaliban.

BAKIT MAY GANITONG TEST. Ang `venue/alpaca_spot.py::replace_order_qty` (#1276)
ay naipasok noong 2026-09-01 nang WALANG production caller. Napatunayan ng
live probe na gumagana ang mekanismo, kaya ang tukso ay ikabit ito sa unang
exit site na makikita. Limang konkretong butas ang nahanap ng dalawang
adversarial review kung gagawin iyon nang walang durable claim-phase marker
(tingnan ang `docs/DESIGN/PARTIAL_EXIT_PATH_B.md`), at dalawa sa mga iyon ay
NAGPAPATAY sa buong posisyon o sa LAHAT ng exit path:

  R1  ang `pending_replace` ay hindi `certifiably_active`, kaya ang unang
      PATCH ay humahantong sa `_queue_full_close(deadman_active_certification_failed)`
      — nagfa-flatten ng buong runner sa ordinaryong transient.
  R2  ang whole exit na dumarating habang nakaturo pa sa predecessor ang
      ledger ay nagfi-freeze ng close handoff laban sa isang `replaced` na
      order — successor hindi kailanman ma-certify, cancel hindi kailanman
      maging terminal, bawat deadman lease naharang: WALANG exit path.

ANG BANTAY. Kung may magdagdag ng production caller ng `replace_order_qty`,
babagsak ang test na ito na may pahiwatig sa disenyo. HINDI ito panghabang-buhay:
BURAHIN ang file na ito sa PR na talagang ikakabit ang PATH B, at palitan ito
ng mga call-site guard (isang caller lamang; tinatawag mula sa SCALING_OUT site
lamang; hindi kailanman sa loob ng burst branch).

IKALAWANG PASADA (2026-09-02). Ang guard na ito ay HINDI GUMAGANA noong
naipadala ito: `parents[3]` ang scan root at `_APP / "app"` ang glob, kaya
`<repo>/app/app` — wala iyon, ZERO na file ang na-scan, at ang assert ay
pumapasa kahit ano. Naayos na, at may self-check at positibong kontrol na
ngayon sa ibaba para hindi na ito muling maging bulag.

Runnable: pytest tests/test_partial_exit_path_b_unwired.py -v
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue import alpaca_spot as als

# `live_runner.py` ay nasa app/services/trading/momentum_neural/, kaya:
#   parents[0]=momentum_neural  [1]=trading  [2]=services  [3]=app  [4]=repo root
#
# BUG NA NAAYOS (2026-09-02): ang unang bersyon ay gumamit ng `parents[3]` at
# saka nag-glob ng `_APP / "app"` — ibig sabihin `<repo>/app/app`, na WALA.
# Zero na file ang na-scan, kaya ang guard ay pumapasa NANG WALANG KONDISYON:
# papasa ito kahit nakabit na ang `replace_order_qty` sa bawat exit site.
# Isang guard na hindi kayang bumagsak ay mas masahol pa sa walang guard, dahil
# ito ay binabanggit sa PR bilang ebidensya. Kaya may self-check na ngayon sa
# ibaba: ang scan ay dapat may laman AT may napatunayang positibong kontrol.
#
# IKAAPAT NA PASADA (2026-09-02). Tinanggal ang exclusion ng venue adapter. Ang
# `replace_order_qty` ay lumilitaw sa `alpaca_spot.py` bilang `def` LAMANG
# (linya 3713), at ang `_calls_named` ay tumutugma sa `ast.Call` at hindi
# kailanman sa isang `FunctionDef` — kaya walang PINOPROTEKTAHAN ang exclusion.
# Ang binibili lamang nito ay isang bulag na sulok: ang natural na hakbang ng
# isang wiring PR ay isang convenience method sa LOOB mismo ng adapter
# (`partial_exit_under_deadman()` na tumatawag sa `self.replace_order_qty(...)`)
# — production wiring na hindi makikita ng nag-iisang depensa ng PATH B, habang
# ang test ay binabanggit pa rin bilang ebidensyang hindi pa nakakabit. Iyon ang
# kaparehong argumento ng file na ito laban sa bulag na `<repo>/app/app`.
_REPO_ROOT = Path(lr.__file__).resolve().parents[4]
_APP_DIR = _REPO_ROOT / "app"
_VENUE_ADAPTER = Path(als.__file__).resolve()

_MIN_EXPECTED_PRODUCTION_FILES = 100


def _production_py_files() -> list[Path]:
    return list(_APP_DIR.rglob("*.py"))


def test_the_tripwire_actually_scans_the_production_tree():
    """SELF-CHECK ng guard mismo. Kung mali ang scan root ay walang sinasabi
    ang natitirang test dito."""
    assert _APP_DIR.is_dir(), f"maling scan root: {_APP_DIR}"
    files = _production_py_files()
    assert len(files) > _MIN_EXPECTED_PRODUCTION_FILES, (
        f"ang tripwire ay nag-scan ng {len(files)} na file — mali ang scan root"
    )
    resolved = {p.resolve() for p in files}
    assert Path(lr.__file__).resolve() in resolved
    # ...at ang venue adapter ay HINDI na ibinubukod (ikaapat na pasada): ang
    # exclusion ay hindi nagpoprotekta ng kahit ano at nagbubukas ng sulok kung
    # saan puwedeng mabuo ang wiring nang hindi nakikita.
    assert _VENUE_ADAPTER in resolved


def test_the_definition_of_replace_order_qty_is_not_mistaken_for_a_caller():
    """Ang dahilan kung bakit ligtas ang pag-scan sa adapter: ang `def` ay
    hindi isang `ast.Call`. Kung sakaling maging tugma iyon ng isang refactor ng
    `_calls_named`, ang tripwire ay magiging palaging pula at ide-delete — kaya
    ito ay tahasang naka-pin."""
    tree = ast.parse(_VENUE_ADAPTER.read_text(encoding="utf-8"))
    assert not _calls_named(tree, "replace_order_qty"), (
        "may tumatawag na sa `replace_order_qty` sa loob ng venue adapter"
    )
    defs = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "replace_order_qty"
    ]
    assert defs, "nawala ang `replace_order_qty` sa adapter"


def test_the_tripwire_can_actually_find_a_call():
    """POSITIBONG KONTROL. Isang simbolo na TIYAK na tinatawag sa production
    ay dapat mahanap ng parehong makinarya na naghahanap ng
    `replace_order_qty`. Kung hindi ito mahanap ay sirang-sira ang AST walk at
    ang pangunahing assert ay walang kabuluhan."""
    hits = 0
    for path in _production_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        hits += len(_calls_named(tree, "_ensure_alpaca_deadman_stop"))
    assert hits > 0, "hindi mahanap ng AST walk ang isang kilalang caller"


def _calls_named(tree: ast.AST, name: str) -> list[int]:
    """Lineno ng bawat tawag na ang huling attribute/name ay `name`."""
    out: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        got = (
            func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name)
            else None
        )
        if got == name:
            out.append(node.lineno)
    return out


def test_replace_order_qty_still_has_zero_production_callers():
    """Ang PATH B ay disenyo pa lamang. Walang production code ang nag-PATCH
    ng nakaupong deadman stop."""
    offenders: list[str] = []
    for path in _production_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for lineno in _calls_named(tree, "replace_order_qty"):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "Ang PATH B ay nakabit na nang hindi dumadaan sa disenyo. Basahin ang "
        "docs/DESIGN/PARTIAL_EXIT_PATH_B.md (lalo na ang R1 at R2) bago "
        "ipagpatuloy, saka palitan ang test na ito ng mga call-site guard sa "
        f"§9. Mga caller: {offenders}"
    )


def test_the_pure_core_is_not_imported_by_the_live_runner_yet():
    """Ang purong module ay naka-ship pero HINDI nakakabit — kapag na-import
    na ito ng live_runner ay may wiring na, at may ibang guard na dapat."""
    src = inspect.getsource(lr)
    assert "path_b_partial" not in src


def test_pending_replace_is_still_not_a_certifiably_active_lifecycle():
    """Ang PREMISE ng R1. Kapag naidagdag ang `pending_replace` sa set na ito
    nang walang marker gate ay tahimik na magiging 'protektado' ang isang
    order na wala pang kapalit — mas malala kaysa sa flatten."""
    assert "pending_replace" not in lr._ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES
    assert "new" in lr._ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES


def test_nonactive_lifecycle_handler_still_has_no_pending_replace_branch():
    """Ang D8 branch ng disenyo ay wala pa. Nasa `_ensure_alpaca_deadman_stop`
    ang handler bilang closure, kaya sa buong source ito hinahanap."""
    src = inspect.getsource(lr)
    assert "path_b_replace_pending" not in src


def test_the_scaling_out_site_still_excludes_alpaca_from_the_split():
    """Ito ang mismong suppression na nag-emit ng
    `alpaca_scale_out_suppressed_for_deadman` sa CANF: habang buo ang stop ay
    all-or-nothing ang Alpaca exit. Hindi ito puwedeng alisin nang mag-isa —
    kailangan muna ng PATH B."""
    src = inspect.getsource(lr)
    assert "ALPACA_EXECUTION_FAMILIES" in src
    assert "alpaca_scale_out_suppressed_for_deadman" in src
