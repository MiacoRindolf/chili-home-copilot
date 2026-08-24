"""Ang feed-stall guard ay dapat sumukat ng PAGDATING, hindi ng oras ng pangyayari.

ANG BUG, SINUKAT LIVE (2026-08-24, DAIC). Ang guard ay gumagamit ng
``global_print_recency_age_s`` = ``now - max(observed_at)`` -- ang oras ng
PANGYAYARI ng pinakabagong NAKA-COMMIT na row. Pinagsasama niyon ang dalawang
magkaibang bagay:

  * tahimik ang market / patay ang feed   (ang gusto nitong makita)
  * nahuhuli ang WRITER                    (ang aktwal nitong nasusukat)

Noong 13:30-13:47 UTC ay umabot ang write lag sa **201 -> 333 segundo** habang
ang tape ay malusog na **12,890-22,514 row/min sa 41-46 simbolo**. Ang guard ay
nag-ulat ng ``global_age=215.9s``, nagpasyang *"the whole tape is silent"*, at
**sinupil ang halt inference sa DALAWANG TUNAY na LULD halt** sa isang +62.7% na
galaw ng DAIC. 382 na skip sa 16 na simbolo sa isang araw.

Ang ``available_at`` ay ang oras ng PAGDATING. Nananatili itong sariwa hangga't
may dumarating na row, gaano man kalaki ang backlog ng ``observed_at``.

⚠️ Ang mga test na ito ay nagtatanim ng SINTETIKONG row na may kinokontrol na
``observed_at``/``available_at``, kaya ang pagkakaiba ay masusukat, hindi
inaasahan.

Runnable: pytest tests/test_feed_stall_guard_ingest_clock.py -v
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.services.trading.momentum_neural.nbbo_tape import (
    global_print_recency_age_s,
    tape_ingest_recency_age_s,
)


def _plant(db, *, symbol: str, observed_ago_s: float, available_ago_s: float, n: int = 3):
    """Magtanim ng row na may TAHASANG hiwalay na event- at arrival-clock."""
    now = datetime.now(timezone.utc)
    for i in range(n):
        db.execute(
            text(
                "INSERT INTO iqfeed_trade_ticks "
                "(symbol, observed_at, price, size, source, available_at) "
                "VALUES (:s, :obs, :px, :sz, 'test', :avail)"
            ),
            {
                "s": symbol,
                "obs": (now - timedelta(seconds=observed_ago_s + i)).replace(tzinfo=None),
                "px": 1.0 + i,
                "sz": 100,
                "avail": now - timedelta(seconds=available_ago_s + i),
            },
        )
    db.flush()


def _sym() -> str:
    return f"Z{uuid.uuid4().hex[:5].upper()}"


# ── ANG PANGUNAHING KASO: uniform write lag ────────────────────────────────

def test_write_lag_blinds_the_legacy_measure_but_not_the_new_one(db):
    """ANG EKSAKTONG DAIC NA SITWASYON: dumarating ang mga row, luma ang backlog.

    observed_at 250s ang layo (backlog) pero available_at 1s (tuloy ang pagdating).
    """
    _plant(db, symbol=_sym(), observed_ago_s=250.0, available_ago_s=1.0)

    legacy = global_print_recency_age_s(db)
    ingest = tape_ingest_recency_age_s(db)

    assert legacy is not None and ingest is not None
    assert legacy > 200.0, (
        f"ang lumang sukat ay dapat magmukhang 'tahimik ang tape' ({legacy})"
    )
    assert ingest < 30.0, (
        f"ang bagong sukat ay dapat makitang DUMARATING pa ang mga row ({ingest})"
    )
    # Ang buong punto: sa 15s na threshold ay magkaibang desisyon ang dalawa.
    threshold = 15.0
    assert legacy >= threshold, "lumang sukat -> LALAKTAWAN (bulag)"
    assert ingest < threshold, "bagong sukat -> HINDI lalaktawan (tama)"


def test_a_real_ingest_stall_is_still_caught(db):
    """Ang orihinal na hangarin ng guard ay dapat manatili: kapag WALANG dumarating."""
    _plant(db, symbol=_sym(), observed_ago_s=250.0, available_ago_s=250.0)

    ingest = tape_ingest_recency_age_s(db)
    assert ingest is not None and ingest > 200.0, (
        f"walang dumating na row sa 250s -- dapat itong makita ({ingest})"
    )
    assert ingest >= 15.0, "dapat pa ring lumaktaw sa isang TUNAY na stall"


def test_a_healthy_tape_reads_fresh_on_both(db):
    """Kapag walang lag ay dapat magkasundo ang dalawang sukat."""
    _plant(db, symbol=_sym(), observed_ago_s=1.0, available_ago_s=1.0)
    legacy = global_print_recency_age_s(db)
    ingest = tape_ingest_recency_age_s(db)
    assert legacy is not None and legacy < 30.0
    assert ingest is not None and ingest < 30.0


@pytest.mark.parametrize("lag_s", [60.0, 200.0, 333.0])
def test_the_new_measure_is_invariant_to_the_size_of_the_lag(db, lag_s):
    """LAG-INVARIANCE: gaano man kalaki ang backlog, ang pagdating ang mahalaga.

    Ang 333s ay ang pinakamasamang lag na nasukat noong 2026-08-24 13:47.
    """
    _plant(db, symbol=_sym(), observed_ago_s=lag_s, available_ago_s=1.0)
    ingest = tape_ingest_recency_age_s(db)
    assert ingest is not None and ingest < 30.0, (
        f"lag={lag_s}s ay hindi dapat makaapekto sa arrival clock ({ingest})"
    )


# ── kontrata ng kaligtasan ─────────────────────────────────────────────────

def test_an_empty_tape_returns_none_so_the_caller_can_fall_back(db):
    db.execute(text("DELETE FROM iqfeed_trade_ticks"))
    db.flush()
    assert tape_ingest_recency_age_s(db) is None


def test_rows_without_an_arrival_clock_are_skipped_not_treated_as_zero(db):
    """Ang lumang row / ibang bridge build ay maaaring walang available_at."""
    db.execute(text("DELETE FROM iqfeed_trade_ticks"))
    now = datetime.now(timezone.utc)
    # pinakabagong row (pinakamataas na id) ay WALANG arrival clock...
    _plant(db, symbol=_sym(), observed_ago_s=5.0, available_ago_s=5.0, n=1)
    db.execute(
        text(
            "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, source, available_at) "
            "VALUES (:s, :obs, 1.0, 100, 'test', NULL)"
        ),
        {"s": _sym(), "obs": now.replace(tzinfo=None)},
    )
    db.flush()
    age = tape_ingest_recency_age_s(db)
    assert age is not None and age < 30.0, (
        "dapat bumaba sa pinakabagong row na MAY arrival clock, hindi magbalik ng None"
    )


def test_a_future_arrival_clock_never_returns_a_negative_age(db):
    db.execute(text("DELETE FROM iqfeed_trade_ticks"))
    _plant(db, symbol=_sym(), observed_ago_s=1.0, available_ago_s=-60.0, n=1)
    db.flush()
    age = tape_ingest_recency_age_s(db)
    # Ang AS-OF bound ay dapat magbukod ng future row -> None, o clamp sa 0.
    assert age is None or age >= 0.0


def test_an_exploding_read_fails_soft(monkeypatch, db):
    class _Boom:
        def begin_nested(self):
            raise RuntimeError("nasira ang db")

    assert tape_ingest_recency_age_s(_Boom()) is None


# ── ang call site ──────────────────────────────────────────────────────────

def test_the_guard_prefers_the_arrival_clock_and_falls_back(db):
    """Bantayan ang call site: bagong sukat muna, lumang sukat bilang fallback."""
    import inspect

    from app.services.trading.momentum_neural import live_runner as lr

    src = inspect.getsource(lr._register_print_recency_halt_check)
    assert "tape_ingest_recency_age_s(" in src, "dapat gamitin ang arrival clock"
    assert "global_print_recency_age_s(" in src, "dapat may fallback pa rin"
    # Ang arrival clock ay dapat MAUNA sa legacy fallback.
    assert src.index("tape_ingest_recency_age_s(db") < src.index(
        "global_print_recency_age_s(db"
    ), "ang arrival clock ang dapat mangibabaw; ang legacy ay fallback lamang"
