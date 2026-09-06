"""CALL-SITE GUARDS: the PATH B wiring, and the shape it is allowed to have.

This file used to be a TRIPWIRE asserting the opposite -- that
`venue/alpaca_spot.py::replace_order_qty` (#1276) had ZERO production callers. It shipped
in that shape on 2026-09-01 because two adversarial reviews found five concrete holes in
wiring it without a durable claim-phase marker, and two of those holes kill the whole
position or every exit path:

  R1  `pending_replace` is not `certifiably_active`, so the first PATCH reaches
      `_queue_full_close(deadman_active_certification_failed)` -- flattening the entire
      runner on an ordinary transient.
  R2  a whole exit arriving while the ledger still points at the predecessor freezes a
      close handoff against a `replaced` order -- the successor never certifies, the
      cancel is never terminal, every deadman lease is blocked: NO exit path at all.

PATH B is wired now (2026-09-06), and that tripwire's own instructions said what to
replace it with: call-site guards. So the AST machinery below is kept verbatim -- it is
the only thing in the repo that can see a caller appear anywhere in `app/` -- and the
three assertions that named the unwired state are inverted to name the WIRED one: exactly
one production caller, inside the service step, with R1 and R2 handled rather than
bypassed.

The blindness self-checks stay too. The first version of this file globbed
`<repo>/app/app`, scanned ZERO files and passed unconditionally while being cited in a PR
as evidence -- a guard that cannot fail is worse than no guard.

Runnable: pytest tests/test_partial_exit_path_b_wired.py -v
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


def test_replace_order_qty_has_exactly_one_production_caller():
    """One PATCH site, and it is the service step.

    The shape matters more than the count: the qty PATCH is the moment the runner's
    protection shrinks, so it must happen where the durable marker already records
    what is being shrunk, to what, and which tranche it pays for. A second caller
    anywhere would be a stop shrunk without that record."""
    callers: list[str] = []
    for path in _production_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for lineno in _calls_named(tree, "replace_order_qty"):
            callers.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")
    assert len(callers) == 1, (
        "PATH B PATCHes the resting deadman stop from exactly ONE place, the "
        "service step. Read docs/DESIGN/PARTIAL_EXIT_PATH_B.md (R1 and R2) before "
        f"adding another. Callers: {callers}"
    )
    # and that one call is inside `_service_path_b_marker`, not at some exit site
    assert "replace_order_qty(" in inspect.getsource(lr._service_path_b_marker)


def test_the_phase_graph_is_the_only_thing_that_authorises_a_phase():
    """Every advance goes through the pure core, never a literal at a call site.

    `advance_phase` raises on an edge the design deleted, so a wiring that has not
    been updated cannot write one. That is why the graph is a `MappingProxyType` and
    why the claim writer consults it instead of trusting its caller."""
    src = inspect.getsource(lr)
    assert "from . import path_b_partial as pb" in src
    for helper in ("plan_replacement_edge", "marker_successor_envelope",
                   "marker_ceiling_forced_target", "blocks_whole_exit",
                   "requires_sibling_reconcile", "open_partial_qty_from_marker"):
        assert f"pb.{helper}(" in src, helper


def test_pending_replace_is_still_not_a_certifiably_active_lifecycle():
    """Ang PREMISE ng R1. Kapag naidagdag ang `pending_replace` sa set na ito
    nang walang marker gate ay tahimik na magiging 'protektado' ang isang
    order na wala pang kapalit — mas malala kaysa sa flatten."""
    assert "pending_replace" not in lr._ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES
    assert "new" in lr._ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES


def test_a_pending_replace_is_reported_as_protected_not_as_a_missing_stop():
    """The D8 branch. While the PATCH is in flight BOTH orders rest at the broker
    and the predecessor is still live, so the position is covered -- and saying so is
    what keeps maintenance from reading the transient as a missing stop and disabling
    the software stop underneath it. R1's premise is handled HERE rather than by
    widening the certifiably-active set, which is why the test above still holds."""
    src = inspect.getsource(lr._service_path_b_marker)
    i = src.find('if lifecycle == "pending_replace":')
    assert i > 0
    branch = src[i:i + 900]
    assert '"protected": True' in branch
    assert '"path_b_replace_pending": True' in branch
    # past one owner-transport lease it stops being a wait: `replace_stuck`
    assert "pending_replace_past_lease" in branch


def test_the_scaling_out_site_no_longer_decides_by_execution_family():
    """The suppression that emitted `alpaca_scale_out_suppressed_for_deadman` on CANF
    was all-or-nothing BECAUSE of the venue. The question asked there is now
    feasibility, and when the answer is no, the answer itself opens the edge that
    changes it instead of quietly taking a different trade."""
    src = inspect.getsource(lr.tick_live_session)
    i = src.find("scale_qty, runner_qty, can_split = scale_out_quantity(")
    j = src.find('exit_reason = "scale_out_target" if scaling else "target"', i)
    assert 0 < i < j
    block = src[i:j]
    assert "scaling = bool(_partial_wanted and _tranche_ok)" in block
    assert "alpaca_partial_tranche_sellable(" in block
    assert "_open_path_b_partial_marker(" in block
