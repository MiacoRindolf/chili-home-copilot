"""Ang ordinary route lock ay dapat mag-retry, hindi sumuko o pumila (2026-08-25).

ANG PUWANG. Ang ``_load_ordinary_live_session_for_update`` ay isang
``FOR UPDATE NOWAIT``. Kapag may ibang transaction na hawak ang row ay **agad**
itong bumabagsak, ang tick ng runner ay sumasabog, at ang session ay naiiwang
hindi natitikan -- magpakailanman.

NASUKAT NANG BUHAY (2026-08-25, premarket at open)::

    psycopg2.errors.LockNotAvailable: could not obtain lock on row in
        relation "trading_automation_sessions"
      captured_paper_dispatcher.py:829  _load_ordinary_live_session_for_update
      [scheduler] live runner tick failed session=15676

    session 15676 (AIXI) sa live_pending_entry nang 650 SEGUNDO
    runner tick failed: 9 sa 35 na live_runner na linya  (26%)

⚠️ AT SADYANG NOWAIT ITO, KAYA NANATILI ITONG NOWAIT. Sa 60 mabilisang probe ng
``pg_locks`` ay may naghihintay na row lock sa **60 sa 60** (129 waiter, ~2
sabay-sabay). Ang pagpapalit nito sa isang humaharang na hintay ay magpapasali sa
runner sa isang pilang laging punô -- at ang runner ay ``max_instances=1``, kaya
ang isang mahabang harang doon ay nagpapatigil sa stop/trail management ng BAWAT
hawak na posisyon. Iyon ang eksaktong 648-segundong pagka-freeze ng session 14440.

Kaya ang hugis ay MULING PAGTATANGKA, hindi PAGPILA.

⚠️ TUNAY NA POSTGRES. Semantiko ng row lock at ng savepoint ang sinusuri.

Runnable: pytest tests/test_ordinary_route_lock_bounded_retry.py -v
"""
from __future__ import annotations

import os
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.captured_paper_dispatcher import (
    CapturedPaperRuntimeUnavailableError,
    _load_ordinary_live_session_for_update,
)

_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not _URL, reason="kailangan ng TEST_DATABASE_URL")

# ⚠️ Ang variant_id ay may FK papunta sa momentum_strategy_variants, kaya
# kailangan ng tunay na variant. Ang fixture ng conftest ay nagtu-truncate kada
# test, kaya gagawin ito kada test at hindi minsanan.
_INSERT_VARIANT = text(
    """
    INSERT INTO momentum_strategy_variants
        (family, variant_key, version, label, params_json, is_active,
         execution_family, created_at, updated_at)
    VALUES ('micro', :vkey, 1, 'route-lock test', '{}'::jsonb, false,
            'alpaca_spot', now(), now())
    RETURNING id
    """
)

_INSERT = text(
    """
    INSERT INTO trading_automation_sessions
        (venue, execution_family, mode, symbol, variant_id, state,
         risk_snapshot_json, started_at, created_at, updated_at)
    VALUES ('alpaca', 'alpaca_spot', 'live', :sym, :vid, 'live_pending_entry',
            '{}'::jsonb, now(), now(), now())
    RETURNING id
    """
)


@pytest.fixture()
def engine():
    eng = create_engine(_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_id(engine):
    """Isang tunay na row. Natatangi kada test para walang pagsasapawan."""
    stamp = int(time.time() * 1000) % 1_000_000
    s = Session(bind=engine)
    try:
        vid = s.execute(_INSERT_VARIANT, {"vkey": f"zroutelock-{stamp}"}).scalar()
        sid = s.execute(_INSERT, {"sym": f"ZR{stamp % 100000}", "vid": int(vid)}).scalar()
        s.commit()
    finally:
        s.close()
    yield int(sid)
    s2 = Session(bind=engine)
    try:
        s2.execute(
            text("DELETE FROM trading_automation_sessions WHERE id = :i"), {"i": int(sid)}
        )
        s2.execute(
            text("DELETE FROM momentum_strategy_variants WHERE id = :i"), {"i": int(vid)}
        )
        s2.commit()
    finally:
        s2.close()


def _hold_row(engine, sid: int) -> Session:
    """Isang hiwalay na transaction na humahawak sa row lock."""
    h = Session(bind=engine)
    h.execute(
        text("SELECT id FROM trading_automation_sessions WHERE id = :i FOR UPDATE"),
        {"i": int(sid)},
    )
    return h


def test_an_uncontended_load_returns_the_row(engine, session_id):
    """Ang mabilisang daan ay hindi nagbabago."""
    s = Session(bind=engine)
    try:
        # ⚠️ PAINITIN ANG QUERY, HINDI LANG ANG CONNECTION. Nasukat: unang tawag
        # 0.641s, bawat sumunod na tawag 0.000-0.016s. Ang gastos ay ang
        # UNA-SA-LAHAT na ORM statement compilation ng SQLAlchemy para sa hugis na
        # ito, hindi ang connection handshake at hindi ang muling pagtatangka.
        # Ang pagsukat ng unang tawag ay sinusukat ang compiler.
        s.execute(text("SELECT 1"))
        _load_ordinary_live_session_for_update(s, session_id)
        s.rollback()
        t0 = time.monotonic()
        row = _load_ordinary_live_session_for_update(s, session_id)
        assert row is not None
        assert int(row.id) == session_id
        assert time.monotonic() - t0 < 0.5, "walang kalaban => walang antala"
    finally:
        s.rollback()
        s.close()


def test_a_BRIEF_collision_is_recovered_by_retry(engine, session_id):
    """ANG PANGUNAHING KASO. Ang panandaliang banggaan -- ang uri na sumisira sa
    26% ng runner tick -- ay dapat na mabawi sa loob ng badyet."""
    holder = _hold_row(engine, session_id)

    def _release_soon():
        time.sleep(0.2)
        holder.rollback()
        holder.close()

    t = threading.Thread(target=_release_soon, daemon=True)
    t.start()

    s = Session(bind=engine)
    try:
        row = _load_ordinary_live_session_for_update(s, session_id)
        assert row is not None, "dapat nabawi ito ng muling pagtatangka"
        assert int(row.id) == session_id
    finally:
        s.rollback()
        s.close()
    t.join(timeout=5)


def test_a_PERMANENT_holder_fails_closed_and_FAST(engine, session_id):
    """⚠️ HINDI ITO PUMIPILA KAILANMAN. Laban sa isang may-hawak na hindi bumibitaw
    ay dapat itong sumuko sa loob ng badyet at hindi maghintay ng minuto -- iyon
    ang pumapatay sa max_instances=1 na runner."""
    holder = _hold_row(engine, session_id)
    s = Session(bind=engine)
    try:
        t0 = time.monotonic()
        with pytest.raises(CapturedPaperRuntimeUnavailableError):
            _load_ordinary_live_session_for_update(s, session_id)
        dt = time.monotonic() - t0
        assert dt < 3.0, f"dapat may HANGGANAN ang badyet, umabot ng {dt:.2f}s"
    finally:
        s.rollback()
        s.close()
        holder.rollback()
        holder.close()


def test_a_failed_load_does_NOT_poison_the_callers_transaction(engine, session_id):
    """⚠️ ANG SAVEPOINT ANG BUONG PUNTO. Ang isang nabigong FOR UPDATE NOWAIT ay
    NAGTATAAS at umaabort sa transaction na tumatakbo nito. Kung walang savepoint
    ay walang saysay ang muling pagtatangka -- patay na ang transaction."""
    holder = _hold_row(engine, session_id)
    s = Session(bind=engine)
    try:
        with pytest.raises(CapturedPaperRuntimeUnavailableError):
            _load_ordinary_live_session_for_update(s, session_id)
        # ANG ASERSIYON: buhay pa rin ang transaction ng tumawag.
        assert s.execute(text("SELECT 42")).scalar() == 42
    finally:
        s.rollback()
        s.close()
        holder.rollback()
        holder.close()


def test_a_won_lock_survives_to_the_MAIN_transaction(engine, session_id):
    """⚠️ HINDI NAGBABAGO ANG KONTRATA. Inaasahan ng tumawag na naka-lock ang row
    pagbalik. Ang lock na nakuha sa isang subtransaction na NAG-COMMIT ay hawak
    hanggang sa katapusan ng PANGUNAHING transaction."""
    s = Session(bind=engine)
    rival = None
    try:
        assert _load_ordinary_live_session_for_update(s, session_id) is not None
        rival = Session(bind=engine)
        with pytest.raises(Exception):
            rival.execute(
                text(
                    "SELECT id FROM trading_automation_sessions "
                    "WHERE id = :i FOR UPDATE NOWAIT"
                ),
                {"i": session_id},
            )
    finally:
        if rival is not None:
            rival.rollback()
            rival.close()
        s.rollback()
        s.close()


def test_zero_budget_restores_try_once(engine, session_id, monkeypatch):
    """Ang knob ay may tunay na off switch."""
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_ordinary_route_lock_retry_budget_ms", 0, raising=False
    )
    holder = _hold_row(engine, session_id)
    s = Session(bind=engine)
    try:
        t0 = time.monotonic()
        with pytest.raises(CapturedPaperRuntimeUnavailableError):
            _load_ordinary_live_session_for_update(s, session_id)
        assert time.monotonic() - t0 < 0.4, "ang 0 ay hindi dapat mag-retry kailanman"
    finally:
        s.rollback()
        s.close()
        holder.rollback()
        holder.close()


def test_a_missing_session_is_still_None_not_an_error(engine):
    """Ang isang session na wala ay None pa rin -- walang bagong exception path."""
    s = Session(bind=engine)
    try:
        assert _load_ordinary_live_session_for_update(s, 999_999_999) is None
    finally:
        s.rollback()
        s.close()
