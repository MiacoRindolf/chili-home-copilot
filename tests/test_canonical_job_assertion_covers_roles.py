"""Ang canonical-job assertion ay dapat sumaklaw sa bawat NAKA-DEPLOY na role.

ANG DEPEKTO. Ang FIX C5 na tseke sa dulo ng ``start_scheduler`` ay nagbibilang
ng nawawalang canonical job::

    _missing = [j for j in _expected_per_role.get(role, []) if j not in _registered_ids]

Hanggang 2026-08-25 ay TATLONG key lamang ang nasa mapa -- ``worker``,
``cron_only``, ``all`` -- at **wala ni isa sa mga iyon ang aktuwal na naka-deploy**:

    HOST lane            -> momentum_exec_only
    scheduler container  -> rnd_only
    web container        -> none
    brain container      -> (walang scheduler)

Para sa bawat isa, ang ``.get(role, [])`` ay nagbabalik ng ``[]``, kaya walang
laman ang ``_missing``, at ang assertion ay **nag-uulat ng OK habang wala palang
sinusuri**. Isang tripwire na nakaharang sa daan na walang dumaraan.

⚠️ HINDI ITO TEORETIKAL. Habang tahimik na "pasado" ang tsekeng ito, ang
``chili-clean-recovery-broker-sync`` at ``-autotrader`` ay ``Exited (137)`` nang
**pitong linggo** -- walang broker-DB sync, walang stuck-order canceller, walang
disconnect alarm, at walang nag-e-evaluate ng crypto stop habang may buhay na
posisyon. Ito mismo ang tsekeng isinulat para sumigaw doon.

⚠️ ANG TUNAY NA AYOS ay hindi ang listahan kundi ang **maingay na hindi kilalang
role**. Ang listahan ay mabubulok; ang isang bagong role na naidagdag nang walang
entry ay dating tahimik na puwang at ngayon ay ERROR na.

Runnable: pytest tests/test_canonical_job_assertion_covers_roles.py -v
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from tests.source_region import function_body, read_source

REPO = pathlib.Path(__file__).resolve().parents[1]
SCHED = REPO / "app" / "services" / "trading_scheduler.py"

# Ang mga role na aktuwal na tumatakbo o maaaring i-deploy mula sa compose.
DEPLOYED_ROLES = (
    "momentum_exec_only",   # HOST lane
    "rnd_only",             # scheduler container
    "none",                 # web
    "broker_sync_only",     # broker-sync-worker
    "autotrader_only",      # autotrader-worker
    "market_snapshot_only", # market-snapshot-worker
    "web",
    "worker",
    "cron_only",
    "all",
)


def _expected_map() -> dict[str, list[str]]:
    """Kunin ang literal na mapa mula sa source gamit ang AST.

    ⚠️ Hindi sa pag-import ng module: ang ``start_scheduler`` ay 117k character
    at may side effect sa import-time ng buong app. Ang literal ang tinatanong.
    """
    src = read_source(SCHED)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        tgt = node.target
        if isinstance(tgt, ast.Name) and tgt.id == "_expected_per_role":
            return ast.literal_eval(node.value)
    raise AssertionError("hindi nakita ang _expected_per_role literal")


def test_every_deployed_role_is_a_key():
    """⚠️ ANG PANGUNAHING BANTAY."""
    m = _expected_map()
    missing = [r for r in DEPLOYED_ROLES if r not in m]
    assert not missing, (
        "may naka-deploy na role na WALA sa canonical-job map -- tahimik na "
        f"lalampasan ng assertion ang startup nito: {missing}"
    )


def test_none_is_present_and_explicitly_empty():
    """Ang pagkakaiba ng 'sinuri, walang inaasahan' at 'hindi sinuri' ang
    mismong bagay na nawala dati. Ang ``none`` ay dapat NANDOON at WALANG LAMAN."""
    m = _expected_map()
    assert "none" in m, "ang role na 'none' ay dapat tahasang nasa mapa"
    assert m["none"] == [], "ang 'none' ay sadyang walang inaasahang job"


def test_every_listed_job_id_actually_exists_in_the_scheduler():
    """⚠️ Ang isang canonical id na maling baybay ay isa na namang tahimik na
    puwang: hindi ito kailanman malalaman na naka-register, kaya PALAGING
    magrereklamo -- o mas malala, magiging ingay na natututunang balewalain."""
    src = read_source(SCHED)
    declared = set(re.findall(r'id="([^"]+)"', src))
    for role, ids in _expected_map().items():
        for jid in ids:
            assert jid in declared, (
                f"role={role} ay umaasa sa job id {jid!r} na hindi umiiral sa "
                "trading_scheduler.py"
            )


def test_an_unknown_role_is_reported_loudly():
    """ANG TUNAY NA AYOS. Ang listahan ay mabubulok; ito ang hindi."""
    body = function_body(SCHED, "start_scheduler")
    assert "if role not in _expected_per_role:" in body, (
        "ang hindi kilalang role ay dapat sumigaw, hindi dumulas bilang OK"
    )
    idx = body.index("if role not in _expected_per_role:")
    window = body[idx : idx + 700]
    assert "logger.error" in window, "ang hindi kilalang role ay dapat ERROR, hindi info"


def test_the_assertion_still_warns_rather_than_raises():
    """⚠️ Sinasadya: ang maling role ay hindi dapat pumatay ng startup. Kung
    maging raise ito ay mawawala ang buong scheduler sa halip na isang job."""
    body = function_body(SCHED, "start_scheduler")
    idx = body.index("FIX C5")
    window = body[idx : idx + 3000]
    assert "raise" not in window, "ang FIX C5 ay WARN-not-raise ayon sa disenyo"


@pytest.mark.parametrize("role", ["rnd_only", "momentum_exec_only", "broker_sync_only"])
def test_the_three_roles_that_were_blind_now_expect_something(role):
    """Ang tatlong ito ang aktuwal na tumatakbo. Ang walang lamang listahan para
    sa kanila ay ibabalik ang mismong bulag na puwang na inaayos nito."""
    m = _expected_map()
    assert m.get(role), f"{role} ay dapat may hindi-bababa-sa-isang canonical job"
