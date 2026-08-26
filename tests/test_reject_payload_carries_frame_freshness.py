"""Ang reject ay hindi nagsasabi kung gaano kaluma ang frame (2026-08-26).

ANG PUWANG. Ang bawat detector sa `detector_rejects` ay nagbabasa ng BARS, pero
ang payload ay walang paraan para sabihin kung ang larawang tinitingnan nito ay
ilang segundo o ILANG MINUTO ang tanda. Kaya ang `waiting_for_vwap_reclaim` ay
maaaring mangahulugang hindi pa nangyayari ang reclaim -- O nangyari na ito at
hindi pa lang nakikita ng frame.

NASUKAT SA BUHAY (2026-08-26 premarket, 121 sample)::

    bar_age_max   min=353s   p50=634s   p90=1152s   max=119,180s

Ang PINAKASARIWANG frame na nakita ng entry path ay **5.9 minuto** ang tanda; ang
median ay **10.6 minuto** -- sa mga pangalang gumagalaw ng 20%+ kada minuto.

Ang nangingibabaw na reject ngayong araw::

    vwap_reclaim       waiting_for_vwap_reclaim      114
    flush_dip_buy      flush_dip_not_front_side       92
    momentum_pullback  reclaim_forming                67

Lahat ng tatlo ay tanong tungkol sa KUNG SAAN NA ANG PRESYO NGAYON.

⚠️ TELEMETRY LAMANG. Walang binabagong pasya. Ang `read_tick_bar_age_meter`
(#1116) ay sumusukat ng buong pass; ITO ang nag-uugnay ng edad sa MISMONG
rejection, para ang susunod na pagbabago sa OHLCV path ay maipaglaban mula sa
isang nasukat na kaugnayan sa halip na mula sa hinuha.

PANGALAWANG BAGAY. Ang `live_held_execution_bbo_blocked` ay hindi na "held"
lamang: bago ang #1177 ay ang HELD na landas lamang ang nagbabalik ng snapshot,
kaya tumpak ang pangalan. Ngayon ay nagbabalik na rin ang PRE-ENTRY. Nasukat
2026-08-26 12:59Z: 18 event sa 2 minuto, LAHAT mula sa `watching_live`.

Runnable: pytest tests/test_reject_payload_carries_frame_freshness.py -v
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)


class _Idx(list):
    pass


class _Frame:
    def __init__(self, last_dt):
        self.index = _Idx([last_dt]) if last_dt is not None else _Idx()


def test_the_measured_median_frame_age_is_readable():
    """ANG PANGUNAHING SUKAT. Ang 634s na median ay dapat mabasa ng helper na
    dinadala natin ngayon sa payload."""
    now = datetime.now(timezone.utc)
    age = LR._frame_bar_age_seconds(_Frame(now - timedelta(seconds=634)))
    assert age is not None
    assert 630 <= age <= 640


@pytest.mark.parametrize("seconds", [353, 634, 1152, 119180])
def test_every_point_of_the_measured_distribution_reads(seconds):
    """Ang buong nasukat na distribusyon, hilera kada hilera."""
    now = datetime.now(timezone.utc)
    age = LR._frame_bar_age_seconds(_Frame(now - timedelta(seconds=seconds)))
    assert age is not None
    assert abs(age - seconds) < 10


def test_an_unreadable_frame_yields_None_not_an_exception():
    """⚠️ Ito ay tumatakbo sa MAINIT na daan at hinding-hindi dapat makapagpabagsak
    ng tick."""
    assert LR._frame_bar_age_seconds(_Frame(None)) is None
    assert LR._frame_bar_age_seconds(None) is None
    assert LR._frame_bar_age_seconds(object()) is None


def test_a_future_stamped_frame_is_not_negative():
    """Ang frame na naka-stamp sa hinaharap ay artifact ng provider clock, hindi
    isang senyas ng freshness."""
    now = datetime.now(timezone.utc)
    age = LR._frame_bar_age_seconds(_Frame(now + timedelta(seconds=300)))
    assert age == 0.0


def test_the_wait_payload_carries_the_age():
    """BANTAY SA WIRING. Ang sukat ay walang silbi kung hindi ito naiuulat."""
    src = _SRC.read_text(encoding="utf-8")
    idx = src.find('_emit(db, sess, "live_entry_trigger_wait", _wait_payload)')
    assert idx > 0, "dapat umiiral ang emit"
    window = src[max(0, idx - 2200): idx]
    assert '_wait_payload["bar_age_seconds"]' in window
    assert '_wait_payload["bar_interval"]' in window
    assert "_frame_bar_age_seconds(_df_trig)" in window


def test_a_missing_key_is_preferred_over_a_guessed_number():
    """⚠️ Ang frame ay maaaring unbound sa ilang sangay. Ang nawawalang susi ay
    tapat; ang default na 0 ay magmumukhang SARIWA at magsisinungaling sa
    eksaktong tanong na sinasagot nito."""
    src = _SRC.read_text(encoding="utf-8")
    idx = src.find('_emit(db, sess, "live_entry_trigger_wait", _wait_payload)')
    window = src[max(0, idx - 2200): idx]
    assert "except Exception:" in window
    assert 'bar_age_seconds", 0' not in window
    assert "_wp_age is not None" in window


def test_the_bbo_block_event_declares_its_phase():
    """Ang `live_held_execution_bbo_blocked` ay pumuputok na ngayon para sa
    PRE-ENTRY din. Ang pangalan ay nananatili para sa pagkakatugma; ang
    katotohanan ay nasa payload."""
    src = _SRC.read_text(encoding="utf-8")
    idx = src.find('_emit(db, sess, "live_held_execution_bbo_blocked"')
    assert idx > 0
    window = src[idx: idx + 700]
    assert '"phase"' in window
    assert '"pre_entry"' in window and '"held"' in window
    assert "_HELD_LIVE_STATES" in window
    assert '"session_state"' in window


def test_the_phase_is_derived_from_the_same_set_the_router_uses():
    """⚠️ Kung ang phase ay hinuhulaan mula sa ibang listahan kaysa sa
    ginagamit ng ruter, ang dalawa ay maaaring maghiwalay nang tahimik."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "_HELD_LIVE_STATES" in names
    src = _SRC.read_text(encoding="utf-8")
    # ang ruter at ang label ay parehong sumasangguni sa iisang frozenset
    assert src.count("_HELD_LIVE_STATES") >= 3


def test_no_decision_reads_the_new_keys():
    """⚠️⚠️ ANG BANTAY NA PINAKAMAHALAGA. Ito ay TELEMETRY. Kung may gate na
    nagsimulang magpasya batay sa `bar_age_seconds` ay ibang bagay na iyon, na
    may sariling ebidensya at sariling A/B."""
    src = _SRC.read_text(encoding="utf-8")
    # ang susi ay dapat lumitaw LAMANG sa loob ng payload construction
    assert src.count('"bar_age_seconds"') == 1, (
        "ang bar_age_seconds ay dapat isinusulat isang beses at hindi binabasa")
    for forbidden in (
        'if _wait_payload["bar_age_seconds"]',
        'payload_json->>\'bar_age_seconds\'',
        "bar_age_seconds >",
        "bar_age_seconds <",
    ):
        assert forbidden not in src, "may nagpapasya na batay dito: %s" % forbidden
