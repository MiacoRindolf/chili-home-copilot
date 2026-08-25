"""Ang pagtanggi ng risk-first sizing ay dapat UMABSTAIN sa BAWAT venue.

ANG TRAPDOOR (natuklasan 2026-08-24 ng audit ng PR #732). Sa
``live_runner._place_live_entry``, kapag ang ``compute_risk_first_quantity`` ay
nagbalik ng 0 -- ibig sabihin *"hindi ko ito kayang sukatin nang ligtas"* -- ang
Alpaca ay tama ang pag-abstain, ngunit ang lahat ng IBANG execution family ay
bumabagsak sa BUONG notional ceiling::

    else:
        qty = _round_base_size(max_notional / guarded_ask, inc, mn)
        le["entry_sizing"] = {"model": "notional_first_fallback", ...}

⇒ ``robinhood_spot``, ``robinhood_agentic_mcp``, ``coinbase_spot`` -- mga rail na
**LIVE** -- ay maglalagay ng MAXIMUM-SIZE na order (~15% ng equity) nang eksakto
sa sandaling sinabi ng risk engine na hindi ito ligtas.

⚠️ ANG DIREKSYON NG KABIGUAN ANG PINAKAMASAMA. Dahil
``qty = max_loss / stop_distance``, ang mas malayong stop o na-zero na budget ay
nagbibigay ng MAS MALIIT na risk-first qty -- hanggang umabot ito sa 0, at saka
**TUMATALON ang qty sa maximum.**

NAAABOT ITO. Hindi lamang sa mga sukdulang presyo:
``tests/test_momentum_edge_size_composition.py`` mismo ang nagdodokumento na ang
isang negatibong helper ay lumilikha ng negatibong budget ->
``max_loss_nonpositive`` -> qty 0. Tinatawag nitong "the downstream SAFETY NET" --
at ito NGA sa sizing function, pero hindi sa live runner hanggang sa pag-aayos na ito.

PANGALAWANG PINSALA. Ang ``entry_sizing`` ay hindi nagdadala ng ``stop_distance``
sa fallback na landas, kaya ang #769 max-loss circuit ay bumabagsak sa
``avg - pos['stop_price']`` at ang threshold nito ay nagiging mga **7x** ng
dinisenyong per-trade budget bago pumutok.

PARITY. Ang ``replay_v2.py:2120`` ay UMAABSTAIN nang walang kundisyon sa parehong
kalagayan (``if not want_qty or want_qty < 1.0: ... continue``) -- kaya **hindi
kailanman kayang gayahin ng replay ang over-size**. Ang butas sa parity ay nasa
direksyong NAGTATAGO ng bug, na siyang dahilan kung bakit ito nabuhay nang matagal.

Runnable: pytest tests/test_risk_sizing_refusal_abstains.py -v
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity


def _place_src() -> str:
    """Ang entry-placement na bloke ay nasa loob ng `tick_live_session` (walang
    hiwalay na `_place_live_entry`; label lang iyon sa audit)."""
    return inspect.getsource(live_runner.tick_live_session)


def test_the_notional_first_fallback_is_gone():
    """⚠️ ANG PANGUNAHING BANTAY. Ang pagbalik nito ay muling magbubukas ng
    trapdoor: pagtanggi sa sizing -> maximum-size na order sa isang LIVE na rail."""
    assert "notional_first_fallback" not in _place_src()


def test_no_venue_sizes_off_the_notional_ceiling_when_sizing_refuses():
    """Walang sangay na kumakalkula ng qty mula sa notional pagkatapos tumanggi."""
    # Ang buong bloke ng placement: walang qty na dinederive mula sa notional
    # ceiling kahit saan sa landas ng pagtanggi.
    assert "max_notional / guarded_ask" not in _place_src()


def test_the_refusal_path_abstains_and_returns():
    """Ang pag-abstain ay dapat i-transition PABALIK sa watching at bumalik --
    hindi dumaloy pababa sa placement.

    Dalawa ang dapat na abstain site ngayon: ang orihinal na Alpaca at ang
    bagong all-venue na sangay. Bago ang pag-aayos ay iisa lang.
    """
    src = _place_src()
    assert src.count("live_entry_risk_sizing_unavailable") == 2, (
        "inaasahan ang DALAWANG abstain site (Alpaca + lahat ng ibang venue)"
    )
    assert src.count("risk_first_required") == 2
    assert "\"skipped\": \"risk_sizing_unavailable\"" in src
    assert "\"skipped\": \"alpaca_risk_sizing_unavailable\"" in src


def test_the_sizing_function_still_refuses_the_reachable_triggers():
    """Ang upstream na pagtanggi ay tunay -- ito ang nagpapasimula ng landas."""
    qty, meta = compute_risk_first_quantity(
        entry_price=5.0, atr_pct=0.02, max_loss_usd=-100.0,
        max_notional_ceiling_usd=10_000.0,
    )
    assert qty == 0.0
    assert meta["reason"] == "max_loss_nonpositive"

    qty2, meta2 = compute_risk_first_quantity(
        entry_price=0.0, atr_pct=0.02, max_loss_usd=50.0,
        max_notional_ceiling_usd=10_000.0,
    )
    assert qty2 == 0.0
    assert meta2["reason"] == "invalid_entry"


def test_no_sizing_path_ever_rounds_qty_UP():
    """⚠️ ANG KATABING INVARIANT. Ang buong argumento na 'sinasalo ng risk-first
    sizing ang malayong stop' ay nakasalalay dito: ang bawat quantizer ay dapat
    mag-round PABABA o TUMANGGI, hindi kailanman mag-floor pataas. Ang isang
    `max(1, qty)` saanman ay magpapawalang-bisa niyon nang tahimik."""
    src = inspect.getsource(compute_risk_first_quantity)
    assert "math.floor" in src
    assert "math.ceil" not in src
    assert "below_min_size" in src
    # ang below-min ay TINATANGGIHAN, hindi ino-floor pataas
    assert "return 0.0, {\"reason\": \"below_min_size\"" in src


def test_replay_still_abstains_so_parity_holds():
    """Kung mag-iiba ang replay, mababalik ang butas na nagtago ng bug na ito."""
    from app.services.trading.momentum_neural import replay_v2

    src = inspect.getsource(replay_v2)
    i = src.index("compute_risk_first_quantity(")
    tail = src[i:i + 900]
    assert "if not want_qty" in tail
    assert "continue" in tail
