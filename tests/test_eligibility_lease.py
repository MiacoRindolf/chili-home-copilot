"""L11b — eligibility retention lease (pure predicate; walang I/O).

Sukat sa prod 08-04 na nagbunsod nito: 645 distinct sa equity eligible band,
**482 ang lampas 600s ang edad**, 163 lang ang sariwa. Ang band ang cause #3 ng
IQFeed subscription resolver, at nang humati ang capacity sa 312 ay 100% ng ross
band ang na-evict.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.eligibility_lease import (
    lease_seconds,
    refresh_interval_seconds,
    should_demote,
)


class _S:
    def __init__(self, base=600.0):
        self.chili_momentum_risk_viability_max_age_seconds = base


def _d(**kw):
    base = dict(
        row_age_seconds=1200.0,
        producer_silence_seconds=30.0,
        lease=600.0,
        protected=False,
    )
    base.update(kw)
    return should_demote(**base)


def test_derivations_mula_sa_iisang_base():
    s = _S(600.0)
    assert refresh_interval_seconds(s) == 300.0
    assert lease_seconds(s) == 600.0  # max(600, 2*300)


def test_lease_ay_floor_hindi_constant():
    # Itinaas ang base -> sabay tumataas ang refresh AT ang lease.
    s = _S(1800.0)
    assert refresh_interval_seconds(s) == 900.0
    assert lease_seconds(s) == 1800.0
    # Napakaliit na base -> ang 60s refresh floor ang bumibigat, kaya lease = 2*60.
    tiny = _S(10.0)
    assert refresh_interval_seconds(tiny) == 60.0
    assert lease_seconds(tiny) == 120.0


def test_sirang_base_ay_bumabalik_sa_dokumentadong_default():
    class Broken:
        chili_momentum_risk_viability_max_age_seconds = "hindi-numero"

    assert lease_seconds(Broken()) == 600.0
    assert lease_seconds(_S(0.0)) == 600.0
    assert lease_seconds(_S(-5.0)) == 600.0


def test_expired_row_demoted():
    ok, reason = _d(row_age_seconds=1200.0)
    assert ok and reason == "lease_expired"


def test_within_lease_hindi_ginagalaw():
    ok, reason = _d(row_age_seconds=500.0)
    assert not ok and reason == "within_lease"


def test_isang_nalaktawang_cycle_ay_hindi_nagde_demote():
    # lease = 2 cycles, kaya ang 1 na-miss na refresh (300s) ay ligtas.
    ok, reason = _d(row_age_seconds=301.0)
    assert not ok and reason == "within_lease"


def test_patay_na_producer_ay_FAIL_OPEN():
    # ANG PINAKAMAHALAGANG SAFETY: kapag tumigil ang producer (outage/crash/weekend),
    # ang katahimikan ay HINDI patunay na hindi na tradeable ang pangalan. Kung wala
    # ito, buburahin ng sweep ang BUONG band tuwing may outage.
    ok, reason = _d(row_age_seconds=99999.0, producer_silence_seconds=99999.0)
    assert not ok and reason == "producer_silent_fail_open"


def test_aktibong_session_ay_protektado():
    ok, reason = _d(row_age_seconds=99999.0, protected=True)
    assert not ok and reason == "protected_active_session"


def test_hindi_alam_na_edad_ay_fail_toward_keep():
    for kw in ({"row_age_seconds": None}, {"producer_silence_seconds": None}):
        ok, reason = _d(**kw)
        assert not ok and reason == "unknown_age", kw


def test_lease_na_zero_ay_disabled():
    ok, reason = _d(lease=0.0)
    assert not ok and reason == "lease_disabled"


@pytest.mark.parametrize("age,expected", [(599.0, False), (600.0, False), (601.0, True)])
def test_hangganan_ay_mahigpit_na_lampas(age, expected):
    ok, _ = _d(row_age_seconds=age)
    assert ok is expected
