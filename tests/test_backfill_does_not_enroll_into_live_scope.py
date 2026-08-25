"""Ang alert-link backfill ay dapat mag-ATTRIBUTE, hindi mag-ENROLL.

ANG NAKAWALA (2026-08-24). Ang `broker_service.sync_positions_to_db` ay may
backfill na nagtatakda ng `related_alert_id` (at `scan_pattern_id`) sa BAWAT
bukas na trade na walang alert link, tumutugma sa **TICKER LAMANG**::

    .filter(Trade.user_id == user_id,
            Trade.status == "open",
            Trade.related_alert_id.is_(None))
    ...
    .order_by(BreakoutAlert.score_at_alert.desc()).first()   # 14 araw

Sabi ng sariling komento nito na ang layunin ay attribution -- "so Monitor can
score health". Pero ang `related_alert_id` at `scan_pattern_id` ay **DALAWA sa
limang OR-branch** ng ``autopilot_scope.live_autopilot_trade_filter()``, at iyon
mismo ang scope na ginagamit ng ``auto_trader_monitor.py:603`` para pumili ng mga
row na **IBEBENTA nito sa merkado** (``:854 qty = float(t.quantity or 0)`` -- ang
BUONG posisyon).

⇒ Ang pagsulat na ito ay TAHIMIK na naglilipat ng posisyon mula sa LABAS ng live
execution monitor patungo sa LOOB nito.

ANG KONGKRETONG SITWASYON. Isang manu-manong binili na 200 share ng SOFI sa $6.00
(walang alert link, walang stop) ay nasa labas ng scope, tama. Pumutok ang isang
`pattern_imminent` na alert sa SOFI sa $14.00 na may stop 13.20. Sa loob ng
**2 minuto** (ang sync ay tumatakbo kada 2 min, 24/7, walang flag) ay naka-link na
ang row; itinatatak ng ``_seed_missing_levels`` ang stop mula sa alert; at
ibinebenta ng monitor ang **lahat ng 200 share** sa isang "stop" na **120% sa
ITAAS** ng cost basis ng operator.

Tumutugma ito sa **ticker lamang**, kinukuha ang **PINAKAMATAAS ANG SCORE** na
alert sa 14 araw (hindi ang pinakamalapit sa oras o presyo), at walang wrong-side
check.

⚠️ Sinuri 2026-08-24: ZERO ang kasalukuyang exposure -- ang tatlong bukas na row
ay pawang `-USD` na crypto sa Coinbase, at nilalaktawan ng Robinhood sell path ang
`-USD` at ang hindi-robinhood na source. Ang depekto ay nag-a-arm sa sarili nito
sa sandaling magkaroon ng manu-manong EQUITY na posisyon sa Robinhood.

KAPATID NG #1145, na nag-bound sa HALAGA na isinusulat ng maintenance sweep sa
`stop_loss` -- ang IBANG branch ng parehong filter, ibang writer, pareho ring
walang bantay.

Runnable: pytest tests/test_backfill_does_not_enroll_into_live_scope.py -v
"""
from __future__ import annotations

import inspect

import app.services.broker_service as broker_service
from app.services.trading.autopilot_scope import live_autopilot_trade_filter


def _backfill_block() -> str:
    """Ang bloke ng alert-link backfill sa loob ng sync."""
    src = inspect.getsource(broker_service)
    i = src.index("_link_cutoff")
    return src[max(0, i - 3000):i + 4000]


def test_the_backfill_query_is_scoped_to_already_managed_rows():
    """⚠️ ANG PANGUNAHING BANTAY. Kung wala ito, ang backfill ay
    nag-e-enroll ng hindi pinamamahalaang posisyon sa live sell scope."""
    blk = _backfill_block()
    assert "live_autopilot_trade_filter()" in blk, (
        "ang backfill ay dapat pumili LAMANG ng mga row na nasa live scope na"
    )


def test_the_scope_filter_is_imported_from_the_single_source_of_truth():
    """Huwag doblehin ang predicate -- isang depinisyon lamang ng 'managed'."""
    blk = _backfill_block()
    assert "from .trading.autopilot_scope import live_autopilot_trade_filter" in blk


def test_related_alert_id_really_is_an_execution_scope_key():
    """Ang buong panganib ay nakasalalay dito: ang field na isinusulat ng
    backfill ay isang branch ng scope. Kung mababago iyon, mababago rin ang
    pangangatwiran ng pag-aayos na ito."""
    sql = str(live_autopilot_trade_filter())
    assert "related_alert_id" in sql
    assert "scan_pattern_id" in sql


def test_the_backfill_still_writes_both_attribution_fields():
    """Ang attribution ay hindi dapat mawala para sa mga pinamamahalaang row --
    iyon ang tunay na layunin ng bloke."""
    blk = _backfill_block()
    assert "related_alert_id = _best.id" in blk
    assert "scan_pattern_id = _best.scan_pattern_id" in blk


def test_the_ticker_only_match_is_documented_as_a_hazard():
    """Ang pagtutugma sa ticker lamang + pinakamataas-ang-score sa 14 araw ay
    nananatili (attribution iyon), pero dapat nakatala kung bakit ito delikado
    kung sakaling luwagan muli ang scope."""
    blk = _backfill_block()
    assert "TICKER LAMANG" in blk or "ticker" in blk.lower()
    assert "score_at_alert.desc()" in blk


def test_the_monitor_still_uses_that_same_filter_to_pick_sell_candidates():
    """Kung titigil ang monitor sa paggamit ng filter, ang pag-aayos na ito ay
    magiging masyadong mahigpit at dapat muling suriin."""
    from app.services.trading import auto_trader_monitor

    src = inspect.getsource(auto_trader_monitor)
    assert "live_autopilot_trade_filter()" in src
    assert "qty = float(t.quantity or 0)" in src
