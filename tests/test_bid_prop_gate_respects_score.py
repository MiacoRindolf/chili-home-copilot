"""Ang bid-prop confirmer ay hindi dapat tumakbo sa hindi-admissible na session.

ANG DEPEKTO. Ang ``_trigger_ok`` ay sinisimulan nang optimistiko sa
``tick_live_session``::

    _trigger_ok, _trigger_reason = True, "score_only"

Ang trigger ladder na dapat pumalit doon ay naaabot lamang sa loob ng
``if _score_ok:``. Kaya kapag ``False`` ang ``_score_ok`` -- viability sa ilalim
ng floor, o hindi eligible ang session -- ay **nananatiling True** ang
``_trigger_ok``, dala ang placeholder nitong reason.

Ang admission mismo ay ligtas: hinihingi ng 29925 at 29934 ang ``_score_ok and
_trigger_ok``. Pero ang mga veto block sa pagitan ay hindi pare-parehong
nakabantay, kaya tumatakbo ang bid-prop confirmer sa BAWAT tick ng BAWAT
watching session na hindi naman makakapasok -- isang pagbasa sa 26 GB na
``momentum_nbbo_spread_tape`` at isang event row bawat isa.

SINUKAT (2026-08-25, 30 araw)::

    live_entry_bid_prop_unconfirmed  blocked_trigger=score_only         274
    live_entry_bid_prop_unconfirmed  blocked_trigger=momentum_ok_*       15
    live_entry_bid_prop_unconfirmed  blocked_trigger=orb/flush/abcd       3

**274 sa 292 = 93.8%.** Ang initializer ang TANGING pinagmumulan ng string na
``"score_only"`` sa file, kaya patunay ito at hindi hinuha.

⚠️ ANG MAS MALAKING PINSALA AY SA PAG-UNAWA, HINDI SA CPU. Ang istatistikang
"``bid_prop_book_deteriorating`` ang dominanteng pumapatay ng candidate, 54% ng
lahat" ay artifact ng bug na ito. Itinuro nito ang atensyon sa maling gate sa
loob ng ilang linggo. ~18 lamang sa 30 araw ang tunay na humarang ng detector.

⚠️ HINDI hinahawakan ang katabing post-open quality bar (``if _trigger_ok:``),
kahit pareho ang hugis. Sinukat: 148 na event, **0** ang may
``blocked_trigger="score_only"`` -- hindi generic trigger name ang ``score_only``
kaya hindi kailanman nagde-defer ang predicate nito. Ang pagbabago roon ay
pagbabago na walang inaayos.

⚠️ HINDI rin hinahawakan ang initializer sa 28423. Ang muling pag-seed nito
bilang ``False`` ay dadaloy sa bawat downstream na bumabasa ng
``_trigger_reason``; ang gate ang inaayos, hindi ang halaga.

ALAM ITO NG MAY-AKDA. Sa linya 28461, sa isa pang landas na lumalaktaw sa
ladder::

    if _refire_cooldown:
        # HINDI hinahayaang manatili ang initial True/"score_only" -- ang
        # laktaw sa ladder ay kailangang mag-iwan ng malinaw na WAIT state.
        _trigger_ok, _trigger_reason = False, "late_window_refire_cooldown"
    elif _score_ok:
        ...

Tama ang alituntunin at nakasulat na. Wala lang itong ``else``: kapag PAREHONG
False ang ``_refire_cooldown`` at ``_score_ok`` ay walang tumatakbong sanga at
nananatili ang initializer -- ang mismong bagay na sinasabi ng komento na hindi
dapat mangyari.

⚠️ ANG BUONG AYOS ay ang ``else`` na sangang iyon. SINADYANG hindi ito ginawa
ngayong gabi: ang muling pag-seed ng ``_trigger_reason`` ay dumadaloy sa BAWAT
downstream na bumabasa nito -- ang mga event payload, ang ``_reject_map``, at
ang mga wait-state emit -- at hindi iyon pagbabagong dapat gawin limang oras
bago ang isang buhay na session. Ang inaayos dito ay ang GATE, at iyon ay
mahigpit na pag-aalis ng trabaho.

Runnable: pytest tests/test_bid_prop_gate_respects_score.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from tests.source_region import function_body, read_source

REPO = pathlib.Path(__file__).resolve().parents[1]
RUNNER = REPO / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"

_BID_PROP_FLAG = "chili_momentum_bid_prop_confirmer_enabled"


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _guard_for(flag: str) -> ast.If:
    """Ang ``if`` node na ang kondisyon ay bumabanggit sa ``flag``.

    Sa AST, hindi sa teksto: ang isang substring na hanap sa isang 16,591-linyang
    function ay tumatama rin sa mga komento at sa katabing block.
    """
    tree = ast.parse(read_source(RUNNER))
    hits = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and any(
            isinstance(c, ast.Constant) and c.value == flag
            for c in ast.walk(n.test)
        )
    ]
    assert len(hits) == 1, f"inaasahan ang isang guard para sa {flag}, nakuha {len(hits)}"
    return hits[0]


def test_the_bid_prop_guard_requires_score_ok():
    """⚠️ ANG PANGUNAHING BANTAY."""
    guard = _guard_for(_BID_PROP_FLAG)
    names = _names_in(guard.test)
    assert "_score_ok" in names, (
        "ang bid-prop confirmer ay tatakbo sa mga session na hindi kailanman "
        "makakapasok; 93.8% ng mga veto event nito ang naging artifact"
    )
    assert "_trigger_ok" in names, "ang orihinal na kondisyon ay dapat manatili"


def test_the_optimistic_initializer_still_exists_and_is_unique():
    """Ang buong argumento ay nakasalalay dito. Kung mawala ang initializer o
    lumitaw ang pangalawang pinagmumulan ng string ay hindi na patunay ang
    sinukat na 274, at kailangang muling suriin ang gate na ito."""
    # ⚠️ AST, hindi teksto. Ang unang bersyon ng tsekeng ito ay nagbilang ng
    # substring at nakakita ng APAT -- tatlo sa mga iyon ay komento, kasama ang
    # dalawang idinagdag ko mismo kasama ng ayos. Ang Constant node ang tanging
    # bumibilang ng tunay na paggamit.
    tree = ast.parse(read_source(RUNNER))
    literals = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and n.value == "score_only"
    ]
    assert len(literals) == 1, (
        "ang 'score_only' ay dapat may IISANG pinagmumulan -- ang optimistikong "
        f"initializer -- nakuha {len(literals)}; hindi na patunay ang sinukat na 274"
    )
    body = function_body(RUNNER, "tick_live_session")
    assert '_trigger_ok, _trigger_reason = True, "score_only"' in body


def test_admission_still_requires_both_so_this_cannot_open_a_gate():
    """⚠️ ANG ARGUMENTONG PANGKALIGTASAN, pinipin.

    Ang pagbabagong ito ay nag-aalis ng TRABAHO, hindi nagluluwag ng gate. Kung
    mawala ang alinman sa dalawang admission site na ito ay hindi na totoo ang
    pangangatwiran at kailangang muling suriin ang pagbabago."""
    body = function_body(RUNNER, "tick_live_session")
    assert body.count("_score_ok and _trigger_ok") >= 2, (
        "ang admission ay dapat hinihingi pa rin ang PAREHONG kondisyon"
    )


def test_the_post_open_quality_bar_is_deliberately_left_alone():
    """⚠️ Sinukat, hindi ipinalagay: 148 na event, 0 ang may score_only. Ang
    pagpapatibay nito ay pagbabagong walang inaayos. Pinipin ang pasyang iyon
    para hindi ito 'ayusin' ng susunod na magbabasa."""
    body = function_body(RUNNER, "tick_live_session")
    assert "if _trigger_ok:\n            from .risk_policy import (" in body, (
        "sinadyang hindi ginalaw ang post-open quality bar; kung magbago ito ay "
        "sukatin muna ang blocked_trigger nito bago pahigpitan"
    )


@pytest.mark.parametrize("flag", [_BID_PROP_FLAG])
def test_the_guard_is_still_a_single_block(flag):
    """Bantay laban sa isang hinati na kondisyon na dadaan sa tseke sa itaas
    habang nag-iiwan ng pangalawang daan papasok."""
    _guard_for(flag)
