"""Walang test ang dapat umasa sa TUNAY na orasan para sa isang entry decision.

ANG BITAG (natuklasan 2026-08-05). Ang `_premarket_tickbreak_confirmed`
(entry_gates.py) ay humihingi ng ATR thrust buffer sa PREMARKET — ang CUPR
false-pop guard — at hindi sa RTH o crypto. Pinipili nito ang session mula sa
parameter na `now`, at kapag `None` iyon ay ang TUNAY NA WALL CLOCK ang ginagamit.

Kaya ang isang test na nagpapasa ng `live_price` pero hindi ng `now` ay
sumusukat sa kung ANONG ORAS ito pinatakbo, hindi sa code:

    now=PREMARKET -> ok=False  premarket_tickbreak_unconfirmed
    now=RTH       -> ok=True   pullback_break_tick_ok

Limang test ang tahimik na apektado nito, at ang epekto ay lumabas bilang
"bagong" CI failure na walang kaakibat na code change — nagkataon lang na
tumakbo ang CI sa premarket. Ang guard na ito ay istruktural: hindi nito
sinusukat ang ugali kundi tinitiyak na walang bagong call site na makakalusot.

Ang CUPR guard mismo ay sakop ng test_premarket_tickbreak_confirm.py, kung saan
tahasang ipinapasa ang PAREHONG session.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_TARGETS = {"pullback_break_confirmation", "momentum_pullback_trigger"}
_TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _offenders() -> list[str]:
    bad: list[str] = []
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover — hindi dapat mangyari
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name not in _TARGETS:
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            has_double_star = any(k.arg is None for k in node.keywords)
            if "live_price" not in kwargs:
                continue  # walang live_price => hindi naaabot ang tick-break
            if "now" in kwargs:
                continue
            if has_double_star:
                # `**KW` ang nagdadala ng `now` sa ilang file; hindi ito
                # maaaninag ng AST, kaya hinahayaan — sakop naman ito ng
                # pagpasa/pagbagsak ng mismong test.
                continue
            bad.append(f"{path.name}:{node.lineno}")
    return bad


def test_walang_test_na_umaasa_sa_tunay_na_orasan():
    offenders = _offenders()
    assert not offenders, (
        "May test na nagpapasa ng `live_price` nang walang `now`, kaya ang "
        "resulta nito ay nakadepende sa oras ng pagtakbo (premarket vs RTH):\n  "
        + "\n  ".join(offenders)
        + "\n\nIpasa ang `now` na naka-angkla sa sariling bar timestamp ng fixture."
    )


def test_ang_detector_ay_tunay_na_humuhuli(tmp_path):
    """Ang guard ay walang silbi kung hindi ito nakakakita ng paglabag —
    kaya patunayan sa isang sadyang lumalabag na file."""
    offending = tmp_path / "test_kunwari_lumalabag.py"
    offending.write_text(
        "pullback_break_confirmation(df, live_price=1.23)\n", encoding="utf-8"
    )
    tree = ast.parse(offending.read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) in _TARGETS)
        and {k.arg for k in n.keywords} == {"live_price"}
    ]
    assert len(calls) == 1, "hindi nakita ng parser ang lumalabag na call"


@pytest.mark.parametrize("session_now,expect_ok", [("premarket", False), ("rth", True)])
def test_ang_session_ay_talagang_nagpapabago_ng_resulta(session_now, expect_ok):
    """Ang mismong dahilan kung bakit mahalaga ang guard: parehong code, parehong
    data, magkaibang session -> magkaibang desisyon."""
    from datetime import datetime, timezone

    import pandas as pd

    import app.services.trading.momentum_neural.entry_gates as eg
    from app.services.trading.momentum_neural.entry_gates import (
        pullback_break_confirmation,
    )

    eg.settings.chili_momentum_entry_verticality_atr_mult = 0.0
    rows = []
    px = 1.00
    for _ in range(18):
        rows.append((px, px + 0.07, px - 0.01, px + 0.06, 300_000))
        px += 0.055
    rows += [
        (1.95, 1.96, 1.88, 1.90, 420_000),
        (1.90, 1.94, 1.87, 1.92, 390_000),
        (1.92, 1.95, 1.86, 1.93, 410_000),
    ]
    df = pd.DataFrame(
        [{"Open": o, "High": h, "Low": lo, "Close": c, "Volume": v} for o, h, lo, c, v in rows],
        index=pd.date_range("2026-06-10 14:00:00", periods=len(rows), freq="1min", tz="UTC"),
    )
    kw = dict(entry_interval="1m", require_retest=False, require_sustained_volume=True,
              require_break_candle=True, require_vwap_hold=True, require_macd_bullish=False,
              allow_runaway_break=False)
    _, _, dbg0 = pullback_break_confirmation(df, **kw)
    poke = float(dbg0["pullback_high"]) + 0.01
    now = (
        datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)  # 08:00 ET premarket
        if session_now == "premarket"
        else datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc)  # 11:00 ET RTH
    )
    ok, _reason, _ = pullback_break_confirmation(
        df, live_price=poke, now=now, symbol="TEST", **kw
    )
    assert ok is expect_ok
