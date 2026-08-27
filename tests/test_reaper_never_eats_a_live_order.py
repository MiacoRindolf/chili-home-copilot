"""Nag-reap ang reaper ng session na may buhay na na-fill na broker order.

ANG INSIDENTE (2026-08-27, BRNX session 17370). Sa 16:38:24Z nag-submit ang
session ng BUY 20 BRNX (order 20c15aab, resting limit ~8.55). Ang order ay
NAG-FILL sa loob ng ilang segundo (tape bumagsak sa 8.45; broker: filled 20 @
8.58). Pero ang tik na magpoproseso sana ng fill ay bumagsak sa
``LockNotAvailable`` (hawak ng docker scheduler ang row lock), kaya walang
position record — at pagkatapos ay **ni-reap ng reaper ang session bilang
"never entered" kada 10 segundo** habang ang 20 shares ay nakaupo sa broker
nang WALANG STOP nang halos isang oras.

Ang docstring ng reaper ay nangangako: "Never touches a session that holds a
position" — pero walang tseke para sa buhay na SUBMITTED ORDER na hindi pa
na-attribute ang fill. Ang session na may aktibong order ay hindi "never
entered"; ito ay "entered at hindi pa alam".

Runnable: pytest tests/test_reaper_never_eats_a_live_order.py -v
"""
from __future__ import annotations

import ast
import pathlib

from app.services.trading.momentum_neural import auto_arm as AA

_SRC = pathlib.Path(AA.__file__)


def _reap_fn() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_reap_stale_watching_sessions"
    )


def test_the_guard_checks_all_three_order_signals():
    """ANG PANGUNAHING KASO. Bago ang cancel, ang loop ay dapat tumitingin sa
    (a) aktibong entry_order_id, (b) entry_submitted, (c) hindi-nalutas na
    order sa history — at lumalaktaw kapag alinman ang totoo."""
    src = ast.unparse(_reap_fn())
    assert "entry_order_id" in src, "kailangan ng tseke sa aktibong pointer"
    assert "entry_submitted" in src, "kailangan ng tseke sa submitted marker"
    assert "entry_orders_resolved" in src, (
        "kailangan ng tseke sa hindi-nalutas na history"
    )


def test_the_guard_runs_before_the_cancel():
    """⚠️ Ang pagkakasunod ANG lunas: ang tseke ay dapat NAUUNA sa
    cancel_automation_session sa loob ng loop."""
    src = _SRC.read_text(encoding="utf-8")
    fn_src = ast.unparse(_reap_fn())
    i_guard = fn_src.index("entry_order_id")
    i_cancel = fn_src.index("cancel_automation_session(")
    assert i_guard < i_cancel, "ang guard ay dapat bago ang cancel"


def test_an_unreadable_snapshot_is_skipped_not_reaped():
    """⚠️ FAIL-CLOSED: huwag mag-reap ng hindi mo naiintindihan. Ang bulag na
    cancel sa sirang snapshot ay ang parehong klase ng aksidente."""
    fn_src = ast.unparse(_reap_fn())
    # ang except sa paligid ng guard ay dapat nag-co-continue, hindi bumabagsak
    # sa cancel
    i_try = fn_src.index("entry_order_id")
    tail = fn_src[i_try:]
    assert "continue" in tail.split("cancel_automation_session(")[0], (
        "ang guard (at ang error path nito) ay dapat lumalaktaw bago ang cancel"
    )


def test_the_skip_is_observable():
    """⚠️ Ang tahimik na paglaktaw ay hindi masusuri — dapat may log na
    nagsasabi KUNG BAKIT hindi na-reap."""
    src = ast.unparse(_reap_fn())
    assert "reap SKIPPED" in src


def test_the_incident_is_recorded_at_the_guard():
    """Ang susunod na magbabasa ay dapat makita ang BRNX 17370 na kaso — hindi
    lang ang panuntunan kundi ang pinsalang pinipigilan nito."""
    src = _SRC.read_text(encoding="utf-8")
    assert "17370" in src and "20c15aab" in src
