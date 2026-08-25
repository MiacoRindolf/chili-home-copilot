"""Ang per-symbol arm lock ay hindi dapat humarang nang tuluyan o lumason (2026-08-25).

ANG PUWANG. Ang ``_lock_live_symbol_arm`` ay bare ``pg_advisory_xact_lock``: walang
``lock_timeout``, walang savepoint, nakabalot sa ``except Exception: return False``.
Dalawang bagay ang dinadala niyon, at parehong napatunayan.

**1. WALANG HANGGANANG PAGHARANG.** Dokumentado sa ``app/config.py`` ang bunga:
naiwan ang session 14440 (AZI) sa ``live_pending_entry`` nang **648 SEGUNDO** sa
loob ng iisang tick, na nagpa-freeze sa ``max_instances=1`` na runner at kasama
niyon ang stop/trail management ng BAWAT hawak na posisyon.

**2. TAHIMIK NA PAGLASON NG TRANSACTION.** Mula nang lumipat pababa ang process
fence (PR #1164) ay ganito ang pagkakasunod-sunod, na kinumpirma sa pamamagitan ng
AST laban sa HEAD::

    begin_live_arm                     SYMBOL(741)  -> FENCE(1051)
    confirm_live_arm                   FENCE(1252)  -> SYMBOL(1261)
    promote_paper_session_to_live_arm  FENCE(1679)  -> SYMBOL(1685)

Isang tunay na ABBA. Pumuputok ang deadlock detector ng PostgreSQL sa ~1s at
nagtataas ng ``40P01``; nilulunok iyon ng ``except`` at nagbabalik ng maayos na
``live_arm_generation_lock_unavailable``. Pero ABORTED na ang transaction:
nagpapatuloy ang auto-arm pass, bawat kasunod na pahayag ay nagtataas ng
``InFailedSqlTransaction``, at ang ``db.commit()`` sa dulo ng pass ay **NAWAWALA
ANG BUONG PASS -- kasama ang mga arm na nagawa na.**

⚠️ TUNAY NA POSTGRES. Semantiko ng PG lock ang sinusuri (subtransaction na
paglalaman ng abort, saklaw ng ``SET LOCAL``, kaligtasan ng lock sa isang
subtransaction na nag-commit). Isang doble ang susuriin lamang ang sarili nitong
haka-haka.

Runnable: pytest tests/test_live_symbol_arm_lock_is_bounded.py -v
"""
from __future__ import annotations

import ast
import os
import pathlib
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.operator_actions import (
    _lock_live_symbol_arm,
)

_URL = os.environ.get("TEST_DATABASE_URL", "")
_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading" / "momentum_neural" / "operator_actions.py"
)

pytestmark = pytest.mark.skipif(not _URL, reason="kailangan ng TEST_DATABASE_URL")

SYM = "ZLOCK"

# ⚠️ NATATANGING user_id KADA TEST. Ang lahat ng testong ito ay kumukuha ng
# tunay na advisory lock, at ang isang session na binitawan lang ng isang
# background thread ay maaaring magsapaw sa susunod na test. Ang natatanging
# susi kada test ay nag-aalis ng buong klaseng iyon ng pekeng pagbagsak.
_UID_SEQ = iter(range(424242, 424242 + 1000))


@pytest.fixture()
def uid() -> int:
    return next(_UID_SEQ)


@pytest.fixture()
def key(uid: int) -> str:
    return f"momentum_live_arm:{uid}:{SYM}"


@pytest.fixture()
def engine():
    eng = create_engine(_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


def _session(engine) -> Session:
    return Session(bind=engine)


def test_an_uncontended_lock_is_taken_immediately(engine, uid):
    """Ang mabilisang daan ay hindi nagbabago."""
    s = _session(engine)
    try:
        t0 = time.monotonic()
        assert _lock_live_symbol_arm(s, user_id=uid, symbol=SYM) is True
        assert time.monotonic() - t0 < 0.5
    finally:
        s.rollback()
        s.close()


def test_a_sibling_queues_and_succeeds_when_the_holder_releases(engine, uid):
    """Ang normal na kalabanan ay dapat pa ring pumila -- hindi ito try-once."""
    holder = _session(engine)
    assert _lock_live_symbol_arm(holder, user_id=uid, symbol=SYM) is True

    def _release_soon():
        time.sleep(0.5)
        holder.rollback()
        holder.close()

    t = threading.Thread(target=_release_soon, daemon=True)
    t.start()

    waiter = _session(engine)
    try:
        t0 = time.monotonic()
        got = _lock_live_symbol_arm(waiter, user_id=uid, symbol=SYM)
        dt = time.monotonic() - t0
    finally:
        waiter.rollback()
        waiter.close()
    t.join(timeout=5)

    assert got is True, "ang kapwa ay dapat pumila at magtagumpay"
    assert dt >= 0.3, "dapat talagang naghintay ito"


def test_the_wait_is_BOUNDED_not_indefinite(engine, uid):
    """ANG PANGUNAHING KASO. Ang isang may-hawak na hindi bumibitaw ay dapat mag-
    expire, hindi humarang magpakailanman -- iyon ang 648-segundong pagka-freeze."""
    holder = _session(engine)
    assert _lock_live_symbol_arm(holder, user_id=uid, symbol=SYM) is True
    s = _session(engine)
    try:
        t0 = time.monotonic()
        got = _lock_live_symbol_arm(s, user_id=uid, symbol=SYM)
        dt = time.monotonic() - t0
        assert got is False, "dapat sumuko ito, hindi humarang nang walang hanggan"
        assert 1.0 < dt < 12.0, f"dapat may HANGGANAN ang hintay, umabot ng {dt:.1f}s"
    finally:
        s.rollback()
        s.close()
        holder.rollback()
        holder.close()


def test_an_expired_wait_does_NOT_poison_the_callers_transaction(engine, uid):
    """⚠️ ANG PINAKAMAHALAGA. Ang isang na-expire na hintay ay nag-aabort sa
    subtransaction. Kung hindi iyon nakakulong, ang auto-arm pass ay tuloy pa rin
    at NAWAWALA ANG BUONG PASS sa commit -- kasama ang mga arm na nagawa na."""
    holder = _session(engine)
    assert _lock_live_symbol_arm(holder, user_id=uid, symbol=SYM) is True
    s = _session(engine)
    try:
        assert _lock_live_symbol_arm(s, user_id=uid, symbol=SYM) is False
        # ANG ASERSIYON: gumagana pa rin ang transaction ng tumawag.
        assert s.execute(text("SELECT 42")).scalar() == 42
        # at nakakapag-COMMIT pa rin ito -- iyon ang nawawalang pass.
        s.commit()
        assert s.execute(text("SELECT 7")).scalar() == 7
    finally:
        s.rollback()
        s.close()
        holder.rollback()
        holder.close()


def test_the_lock_timeout_does_not_leak_into_the_caller(engine, uid):
    """Ang SET LOCAL ay umuurong lamang sa ABORT; sa isang release ay tumatagos ito
    hanggang katapusan ng nakapaloob na transaction."""
    holder = _session(engine)
    assert _lock_live_symbol_arm(holder, user_id=uid, symbol=SYM) is True
    s = _session(engine)
    try:
        before = s.execute(text("SELECT current_setting('lock_timeout')")).scalar()
        _lock_live_symbol_arm(s, user_id=uid, symbol=SYM)  # mag-e-expire
        after = s.execute(text("SELECT current_setting('lock_timeout')")).scalar()
        assert after == before, f"tumagas ang lock_timeout: {before!r} -> {after!r}"
    finally:
        s.rollback()
        s.close()
        holder.rollback()
        holder.close()


def test_a_lock_won_after_waiting_survives_to_the_MAIN_transaction(engine, uid, key):
    """⚠️ ANG DOBLE-ARM AY DAPAT IMPOSIBLE PA RIN. Ang lock na nakuha sa loob ng
    isang subtransaction na NAG-COMMIT ay hawak hanggang sa katapusan ng
    PANGUNAHING transaction -- kung hindi, mababawi ito agad at makakapasok ang
    kambal."""
    holder = _session(engine)
    assert _lock_live_symbol_arm(holder, user_id=uid, symbol=SYM) is True

    def _release_soon():
        time.sleep(0.5)
        holder.rollback()
        holder.close()

    threading.Thread(target=_release_soon, daemon=True).start()

    winner = _session(engine)
    rival = None
    try:
        assert _lock_live_symbol_arm(winner, user_id=uid, symbol=SYM) is True
        # Ang isang kalabang session ay hindi dapat makakuha habang bukas ang xact
        # ng nanalo, kahit matagal nang na-release ang savepoint.
        rival = _session(engine)
        got = rival.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": key}
        ).scalar()
        assert got is False, "hawak pa rin dapat ng nanalo ang lock hanggang commit"
    finally:
        if rival is not None:
            rival.rollback()
            rival.close()
        winner.rollback()
        winner.close()


def test_zero_restores_try_once(engine, uid, monkeypatch):
    """Ang knob ay may tunay na off switch."""
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_live_symbol_arm_lock_wait_ms", 0, raising=False
    )
    holder = _session(engine)
    assert _lock_live_symbol_arm(holder, user_id=uid, symbol=SYM) is True
    s = _session(engine)
    try:
        t0 = time.monotonic()
        assert _lock_live_symbol_arm(s, user_id=uid, symbol=SYM) is False
        assert time.monotonic() - t0 < 0.4
    finally:
        s.rollback()
        s.close()
        holder.rollback()
        holder.close()


def test_no_bare_unbounded_advisory_wait_remains_in_the_helper():
    """BANTAY (AST, hindi regex). Ang bare `pg_advisory_xact_lock` ay dapat lamang
    lumitaw sa loob ng nakakulong na waiter, kung saan may lock_timeout at
    savepoint na nakapalibot dito."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_lock_live_symbol_arm"
    )
    # ⚠️ ALISIN MUNA ANG DOCSTRING. Binabanggit mismo ng docstring ang pangalan ng
    # lock habang ipinapaliwanag ang semantiko nito, kaya ang pag-dump ng buong
    # node ay tutugma sa PALIWANAG at hindi sa CODE.
    stmts = [n for n in fn.body if not (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    )]
    body = "".join(ast.dump(n) for n in stmts)
    assert "pg_advisory_xact_lock" not in body, (
        "ang _lock_live_symbol_arm mismo ay hindi na dapat gumamit ng humaharang na "
        "advisory lock; dapat itong nasa nakakulong na waiter"
    )
    waiter = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_wait_for_live_symbol_arm_lock"
    )
    wdump = ast.dump(waiter)
    assert "begin_nested" in wdump, "ang hintay ay dapat nasa loob ng SAVEPOINT"
    assert "lock_timeout" in wdump, "ang hintay ay dapat may HANGGANAN"
