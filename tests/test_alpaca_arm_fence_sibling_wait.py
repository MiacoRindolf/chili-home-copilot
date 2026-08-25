"""Ang kapwa arm ay dapat PUMILA sa fence, hindi mamatay (2026-08-25).

ANG PUWANG. Ang captured-paper fence ay isang pandaigdigang mutex sa Alpaca arm
path, pero ang lane ay umaarm ng maraming simbolo nang SABAY at ang
``pg_try_advisory_xact_lock`` ay HINDI naghihintay. Kaya ang bawat kasabay na arm
maliban sa isa ay agad na bumabagsak nang fail-closed.

NASUKAT SA BUHAY NA PREMARKET (2026-08-25, sariling pag-sample ng ``pg_locks``)::

    hawak ang fence sa 14/25 pagkatapos 22/25 na sample (56-88%)
    lahat ng may hawak mula 172.18.0.1 -- ang host lane mismo
    ang may hawak ay may xact na 6s ang tanda, nagpapatakbo ng arm-path query

    [auto_arm] resolved arm capacity family=alpaca_spot budget=5 candidate_count=5
    [auto_arm] begin_live_arm blocked AIXI:  captured_paper_service_owns_alpaca_arm_path
    [auto_arm] begin_live_arm blocked RCON:  captured_paper_service_owns_alpaca_arm_path
    [auto_arm] begin_live_arm blocked SWVL:  captured_paper_service_owns_alpaca_arm_path
    [auto_arm] begin_live_arm blocked BDRX:  captured_paper_service_owns_alpaca_arm_path
    [auto_arm] begin_live_arm blocked WVVIP: captured_paper_service_owns_alpaca_arm_path
    [auto_arm] ignition->arm bridge: symbols=[...] armed=0

Lima sa lima. Kasama ang RCON -- ang pinakamataas sa viability (``move_pct=114.93
scored_ok=True``), ang mismong pangalan na pinasok ni Ross nang umagang iyon.

⚠️ TUNAY NA POSTGRES ang ginagamit ng mga testong ito. Ang buong bagay na sinusuri
ay semantiko ng PG advisory lock (session laban sa xact, savepoint na paglalaman ng
abort, saklaw ng SET LOCAL) -- ang isang doble ay susuriin lamang ang sarili nitong
haka-haka.

Runnable: pytest tests/test_alpaca_arm_fence_sibling_wait.py -v
"""
from __future__ import annotations

import os
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.captured_paper_service_fence import (
    CAPTURED_PAPER_SERVICE_FENCE_CLASS_ID as CID,
    CAPTURED_PAPER_SERVICE_FENCE_OBJECT_ID as OID,
    try_acquire_generic_alpaca_arm_fence,
)

_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not _URL, reason="kailangan ng TEST_DATABASE_URL")

SCOPE = "alpaca:paper"


@pytest.fixture()
def engine():
    eng = create_engine(_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


def _session(engine) -> Session:
    return Session(bind=engine)


def test_an_uncontended_arm_takes_the_fence_immediately(engine):
    """Ang mabilisang daan ay hindi nagbabago: walang kalaban => agad na True."""
    s = _session(engine)
    try:
        t0 = time.monotonic()
        assert try_acquire_generic_alpaca_arm_fence(s, account_scope=SCOPE) is True
        assert time.monotonic() - t0 < 0.5, "ang walang kalaban ay hindi dapat maghintay"
    finally:
        s.rollback()
        s.close()


def test_a_sibling_arm_QUEUES_instead_of_dying(engine):
    """ANG PANGUNAHING KASO. Isang kapwa xact ang humahawak; dapat pumila ang pangalawa
    at magtagumpay kapag binitawan ng una -- hindi bumagsak agad."""
    holder = _session(engine)
    assert try_acquire_generic_alpaca_arm_fence(holder, account_scope=SCOPE) is True

    result: dict[str, object] = {}

    def _release_soon():
        time.sleep(0.6)
        holder.rollback()
        holder.close()

    rel = threading.Thread(target=_release_soon, daemon=True)
    rel.start()

    waiter = _session(engine)
    try:
        t0 = time.monotonic()
        result["ok"] = try_acquire_generic_alpaca_arm_fence(waiter, account_scope=SCOPE)
        result["dt"] = time.monotonic() - t0
    finally:
        waiter.rollback()
        waiter.close()
    rel.join(timeout=5)

    assert result["ok"] is True, "ang kapwa ay dapat pumila at magtagumpay"
    assert float(result["dt"]) >= 0.4, "dapat talagang naghintay ito, hindi nakuha agad"


def test_the_dedicated_SERVICE_still_fails_closed(engine):
    """⚠️ HINDI NAGBABAGO ANG PAG-AARI. Ang session lock ng service ay dapat pa ring
    magtulak sa generic na daan tungo sa fail-closed kapag natapos ang hangganan."""
    svc = engine.connect()
    got = svc.execute(
        text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": CID, "o": OID}
    ).scalar()
    assert got is True, "ang test mismo ay dapat makuha ang panig ng service"

    s = _session(engine)
    try:
        t0 = time.monotonic()
        assert try_acquire_generic_alpaca_arm_fence(s, account_scope=SCOPE) is False
        dt = time.monotonic() - t0
        assert dt >= 0.3, "dapat nagkaroon ito ng nakatakdang hintay bago sumuko"
        assert dt < 12.0, "at dapat may hangganan iyon"
    finally:
        s.rollback()
        s.close()
        svc.execute(text("SELECT pg_advisory_unlock(:c, :o)"), {"c": CID, "o": OID})
        svc.close()


def test_an_expired_wait_does_NOT_poison_the_callers_transaction(engine):
    """⚠️ ANG SAVEPOINT ANG BUONG PUNTO. Ang isang error na hindi nakakulong ay
    umaabort sa BUONG transaction ng tumawag; pagkatapos ay bumabagsak ang lahat ng
    sumusunod dito. Matapos ang isang nabigong fence ay dapat GUMAGANA pa rin ang
    session."""
    svc = engine.connect()
    svc.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": CID, "o": OID})

    s = _session(engine)
    try:
        assert try_acquire_generic_alpaca_arm_fence(s, account_scope=SCOPE) is False
        # ANG ASERSIYON: buhay pa rin ang transaction.
        assert s.execute(text("SELECT 42")).scalar() == 42
    finally:
        s.rollback()
        s.close()
        svc.execute(text("SELECT pg_advisory_unlock(:c, :o)"), {"c": CID, "o": OID})
        svc.close()


def test_the_lock_timeout_does_not_leak_into_the_caller(engine):
    """⚠️ Ang SET LOCAL ay umuurong lamang sa ABORT; sa isang release ay tumatagos ito
    hanggang katapusan ng nakapaloob na transaction. Ang isang tumagas na 2.5s ay
    magpapabagsak sa mga susunod na pahayag ng arm sa unang tunay na paghihintay."""
    s = _session(engine)
    try:
        before = s.execute(text("SELECT current_setting('lock_timeout')")).scalar()
        assert try_acquire_generic_alpaca_arm_fence(s, account_scope=SCOPE) is True
        after = s.execute(text("SELECT current_setting('lock_timeout')")).scalar()
        assert after == before, f"tumagas ang lock_timeout: {before!r} -> {after!r}"
    finally:
        s.rollback()
        s.close()


def test_zero_restores_the_old_try_once_behaviour(engine, monkeypatch):
    """Ang knob ay may tunay na off switch: 0 => walang hintay, agad na fail-closed."""
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_alpaca_arm_fence_wait_ms", 0, raising=False)

    holder = _session(engine)
    assert try_acquire_generic_alpaca_arm_fence(holder, account_scope=SCOPE) is True
    s = _session(engine)
    try:
        t0 = time.monotonic()
        assert try_acquire_generic_alpaca_arm_fence(s, account_scope=SCOPE) is False
        assert time.monotonic() - t0 < 0.4, "ang 0 ay dapat hindi maghintay kahit kailan"
    finally:
        s.rollback()
        s.close()
        holder.rollback()
        holder.close()


def test_a_foreign_account_scope_is_still_rejected(engine):
    """Ang bantay sa saklaw ay nauuna pa rin sa lahat -- walang bagong daan papasok."""
    s = _session(engine)
    try:
        assert try_acquire_generic_alpaca_arm_fence(s, account_scope="alpaca:live") is False
        assert try_acquire_generic_alpaca_arm_fence(s, account_scope="") is False
    finally:
        s.rollback()
        s.close()
