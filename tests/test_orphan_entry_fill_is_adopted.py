"""Isang na-fill na order na hindi kayang PANGALANAN ay hindi kayang AMPUNIN.

ANG DEPEKTO (nasukat 2026-08-26, inayos 2026-08-27). Ang boundary-risk block sa
``live_runner.tick_live_session`` ay may DALAWANG paraan ng pagtapon ng tunay na
share::

    1. RACED FILL. Kinakansela nito ang entry, nakikitang `filled_size > 0` --
       ATIN NA ANG SHARE -- at nagre-return ng `pending` na string na
       NAGLALAMAN NG SALITANG "adopt" habang WALANG inaampon. Nanatili ang
       session sa LIVE_PENDING_ENTRY na nakatakda ang pointer at walang
       posisyon, kaya inuulit ng bawat sumunod na tick ang parehong sanga:
       kanselahin ang na-fill nang order, makita ang fill, mag-return. Habang
       ang share ay walang stop, walang target, walang deadman.

    2. MALINIS NA KANSELA. Lumilipat ito sa WATCHING_LIVE nang NAKATAKDA PA RIN
       ang `entry_order_id`. Walang makakalutas doon:
         * ang late-fill sweep ay naka-gate sa `not le["entry_order_id"]`
         * ang `_unresolved_entry_order_ids` ay SADYANG hindi kasama ang aktibong
           pointer -- "the normal pending-entry handler owns that one"
         * pero ang handler na iyon ay tumatakbo lang HABANG pending-entry ang
           estado, at iniiwan natin iyon sa mismong susunod na linya.
       Kaya INVISIBLE ang order sa DALAWANG resolver: hindi kailanman naampon,
       hindi kailanman ni-void.

NASUKAT SA PRODUKSYON (2026-08-26)::

    215  live session na may dalang entry_order_id (90 araw)
     41  may pointer at WALANG posisyon
     18  may pointer, walang posisyon, at WALANG resolusyon   <- ang tumutulo

    RDIB session 16759: limit BUY 9sh @ $16.15 laban sa ask na $14.91
    (`planned_vs_execution_gap_bps: -767.8`). Hindi ito puwedeng hindi mag-fill.
    16m08s na walang stop habang nag-e-emit ng PRE-ENTRY na veto.

ANG LUNAS ay hindi nagdadagdag ng bagong landas ng pag-ampon. Para sa (1) ay
tinatawag nito ang UMIIRAL NANG ``_adopt_recovered_primary_fill_for_safety``
(ang fill ng broker ay mas mataas kaysa sa pagtanggi ng entry-risk -- parehong
prinsipyo ng ``_held_position_keeps_exit_on_boundary_fail`` ilang linya sa ibaba).
Para sa (2) ay nililinis lang nito ang pointer, na siyang NAGBUBUKAS ng umiiral
nang sweep.

Runnable: pytest tests/test_orphan_entry_fill_is_adopted.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


# ── Ang mekanismo mismo, sinusubok nang purong-datos ─────────────────────────


def test_a_dangling_pointer_hides_the_order_from_every_resolver():
    """ANG UGAT. Habang nakatakda ang aktibong pointer, ang order ay HINDI
    lumilitaw bilang unresolved -- kahit walang anumang resolusyon."""
    le = {
        "entry_order_ids_all": ["oid-A"],
        "entry_orders_resolved": {},
        "entry_order_id": "oid-A",
    }
    assert LR._unresolved_entry_order_ids(le) == [], (
        "sadyang hindi kasama ang aktibong pointer -- iyon ang buong punto"
    )


def test_releasing_the_pointer_makes_the_existing_sweep_see_it():
    """ANG LUNAS, sa isang linya. Ang paglilinis ng pointer ang NAGBUBUKAS ng
    umiiral nang sweep -- walang bagong makinarya."""
    le = {
        "entry_order_ids_all": ["oid-A"],
        "entry_orders_resolved": {},
        "entry_order_id": None,
    }
    assert LR._unresolved_entry_order_ids(le) == ["oid-A"]


def test_an_already_resolved_order_is_not_swept_twice():
    """⚠️ Ang paglilinis ng pointer ay hindi dapat magparami ng trabaho para sa
    order na nalutas na."""
    le = {
        "entry_order_ids_all": ["oid-A", "oid-B"],
        "entry_orders_resolved": {"oid-A": "void"},
        "entry_order_id": None,
    }
    assert LR._unresolved_entry_order_ids(le) == ["oid-B"]


# ── Ang aktuwal na sanga sa kodigo, sinusuri sa AST ──────────────────────────
#
# ⚠️ AST, HINDI regex. Ang isang nakapirming text window sa isang 42,000-linyang
# file ay nabubulok: ang negatibong assertion sa maling window ay TAHIMIK na
# pumapasa habang lumilipat ang kodigo palayo (naitala sa
# reference_source_guard_windows_rot).


def _boundary_risk_pending_entry_branch() -> ast.If:
    """Ang eksaktong `if` na naghahawak ng pending-entry sa ilalim ng risk block."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "tick_live_session"
    )
    hits = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if (
            "STATE_LIVE_PENDING_ENTRY" in test
            and "entry_order_id" in test
            and "position" in test
        ):
            hits.append(node)
    assert len(hits) == 1, (
        "inaasahang EKSAKTONG isang boundary-risk pending-entry na sanga, "
        "nakita: %d. Kung nahati ang sanga, i-update ang bantay na ito nang "
        "SADYA -- huwag itong paluwagin." % len(hits)
    )
    return hits[0]


def _calls_in(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def test_the_raced_fill_branch_actually_adopts():
    """ANG PANGUNAHING KASO (1). Ang salitang "adopt" sa isang return string ay
    hindi pag-ampon. Ang sanga ay dapat TUMAWAG sa adopt helper."""
    branch = _boundary_risk_pending_entry_branch()
    calls = _calls_in(branch)
    assert "_adopt_recovered_primary_fill_for_safety" in calls, (
        "ang sangang nakakakita ng `filled_size > 0` ay dapat umampon; "
        "natagpuang mga tawag: %r" % sorted(calls)
    )


def test_the_branch_releases_the_pointer_before_leaving_pending_entry():
    """ANG PANGUNAHING KASO (2). Dapat may pagtatakda ng
    `le["entry_order_id"] = None` sa loob ng sangang ito."""
    branch = _boundary_risk_pending_entry_branch()
    released = False
    for n in ast.walk(branch):
        if not isinstance(n, ast.Assign):
            continue
        if not (isinstance(n.value, ast.Constant) and n.value.value is None):
            continue
        for t in n.targets:
            if (
                isinstance(t, ast.Subscript)
                and isinstance(t.slice, ast.Constant)
                and t.slice.value == "entry_order_id"
            ):
                released = True
    assert released, (
        "ang sanga ay dapat maglinis ng `le[\"entry_order_id\"]` bago umalis sa "
        "PENDING_ENTRY -- kung hindi ay hindi kailanman makikita ng sweep ang order"
    )


def test_the_branch_still_transitions_to_watching():
    """⚠️ Ang lunas ay hindi dapat mag-iwan ng session na naipit sa
    PENDING_ENTRY sa malinis na landas ng kansela."""
    branch = _boundary_risk_pending_entry_branch()
    src = ast.unparse(branch)
    assert "STATE_WATCHING_LIVE" in src


def test_the_branch_emits_an_event_on_both_outcomes():
    """⚠️ Isang tahimik na paglipat ang dahilan kung bakit HINDI MABAWI ang
    mekanismong ito sa audit trail nang apat na buwan. Ang bawat labasan ngayon
    ay dapat nag-iiwan ng bakas."""
    branch = _boundary_risk_pending_entry_branch()
    src = ast.unparse(branch)
    assert "risk_block_cancel_raced_fill" in src
    assert "entry_order_released_to_sweep" in src


# ── Ang knob ─────────────────────────────────────────────────────────────────


def test_the_fix_ships_ON_with_a_revert_knob():
    """⚠️ Ito ay LUNAS, hindi tampok. Ipinapadala itong BUKAS; ang knob ay para
    lamang sa pagbawi nang walang deploy. Ang dark flag na naghihintay ng
    'A/B proof' ang eksaktong pattern na nagpahinto sa exit ladder nang ilang
    linggo -- huwag itong ulitin para sa isang pag-aayos ng kaligtasan."""
    name = "chili_momentum_orphan_entry_fill_adoption_enabled"
    assert getattr(settings, name) is True
    fields = type(settings).model_fields
    assert name in fields
    assert fields[name].validation_alias is not None, (
        "dapat maibabalik nang walang deploy"
    )
    desc = str(fields[name].description or "")
    assert "2026-08-27" in desc, "dapat may petsa ang pagbabago"
    assert "16m08s" in desc or "16759" in desc, (
        "dapat nakatala ang nasukat na ebidensya, hindi lang ang layunin"
    )


def test_the_knob_gates_both_halves():
    """Ang parehong depekto ay iisang klase, kaya iisang knob -- pero dapat
    talagang bantayan nito ang DALAWA."""
    branch = _boundary_risk_pending_entry_branch()
    src = ast.unparse(branch)
    assert src.count("chili_momentum_orphan_entry_fill_adoption_enabled") == 2, (
        "inaasahang binabantayan ng knob ang parehong sanga ng raced-fill at ng "
        "paglaya ng pointer"
    )


# ── Ang umiiral nang adopt helper, patunay na ito ang tamang kasangkapan ─────


class _Order:
    def __init__(self, oid, filled, avg, status="canceled"):
        self.order_id = oid
        self.client_order_id = "cid-" + oid
        self.filled_size = filled
        self.average_filled_price = avg
        self.status = status


def test_the_adopt_helper_refuses_an_order_that_is_still_open():
    """⚠️ ANG DIREKSYON NG KALIGTASAN. Ang pag-ampon ay nangangailangan ng
    TERMINAL na katotohanan. Habang bukas pa ang natitira, hindi natin alam ang
    huling laki -- kaya pinananatili ng tumatawag ang pointer at umuulit sa
    susunod na tick imbes na mag-imbento ng posisyon."""
    assert LR._order_open(_Order("o1", 9.0, 14.94, status="open")) is True


def test_a_terminal_partial_fill_is_adoptable():
    """Ang eksaktong hugis ng raced fill: kinansela, may share na natupad."""
    o = _Order("o2", 9.0, 14.94, status="canceled")
    assert LR._order_open(o) is False
    assert float(o.filled_size) > 0.0


@pytest.mark.parametrize("outcome", ["adopted", "void"])
def test_marking_an_order_resolved_removes_it_from_the_sweep(outcome):
    """Ang parehong resolusyon ay dapat nagpapatahimik sa sweep -- kung hindi ay
    walang hanggan ang pag-ikot nito sa parehong id."""
    le = {
        "entry_order_ids_all": ["oid-A"],
        "entry_orders_resolved": {},
        "entry_order_id": None,
    }
    assert LR._unresolved_entry_order_ids(le) == ["oid-A"]
    LR._mark_entry_order_resolved(le, "oid-A", outcome)
    assert LR._unresolved_entry_order_ids(le) == []
