"""WAVE-4 ITEM-7 — MICRO-FRAME LOG-FIRST bundle (the parts main's shape supports).

F1: a swallowed micro/tick-frame build EXCEPTION is no longer silent — it is logged with
    exc_info AND recorded as meta["micro_error_detail"] so the operator can see WHY a name
    silently degraded off the micro frame.
F2: on a build error, RETRY ONCE on a FRESH short-lived SessionLocal before falling back
    (the tape is in-DB; a transient session error must not drop the micro frame). Flag
    chili_momentum_micro_fallback_1m_from_ticks_enabled (default True); OFF -> legacy
    single-attempt swallow (byte-identical).

F3 (density override) and F5 (revalidate-then-submit) target the `stale_pre_submit_pending`
    frame-staleness gate, which is CODEX-ONLY (not present in main's shape) — noted, not ported.

PURE-LOGIC + mocks (no live DB): the build helper is patched to raise / recover.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.trading.momentum_neural import live_runner as LR


class _RaisingDB:
    """A db double whose .execute raises — drives the F1/F2 error path."""

    def execute(self, *a, **k):
        raise RuntimeError("boom-tape-read")


# --------------------------------------------------------------------------- #
# F1 — a swallowed build error surfaces in meta (micro_error_detail)          #
# --------------------------------------------------------------------------- #
def test_build_error_records_micro_error_detail_and_returns_none():
    meta: dict = {}
    with patch.object(LR, "settings", SimpleNamespace(
        chili_momentum_micro_fallback_1m_from_ticks_enabled=False,
    )):
        out = LR._build_micro_bar_df(_RaisingDB(), "ABCD", bar_seconds=15, meta=meta)
    assert out is None
    assert "micro_error_detail" in meta
    assert "boom-tape-read" in meta["micro_error_detail"]


# --------------------------------------------------------------------------- #
# F2 — on error, retry ONCE on a fresh session; recovery is recorded          #
# --------------------------------------------------------------------------- #
def test_build_error_retries_on_fresh_session_and_recovers():
    meta: dict = {}
    recovered_df = SimpleNamespace(empty=False)

    # first attempt (the passed db) raises; the retry (fresh SessionLocal) succeeds.
    calls = {"n": 0}

    def _from_session(db, symbol, *, bar_seconds, lookback_minutes):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom-primary")
        return recovered_df  # the fresh-session retry recovers

    fake_session = SimpleNamespace(rollback=lambda: None, close=lambda: None)

    with patch.object(LR, "settings", SimpleNamespace(
        chili_momentum_micro_fallback_1m_from_ticks_enabled=True,
    )), patch.object(LR, "_micro_bar_df_from_session", _from_session), \
        patch("app.db.SessionLocal", lambda: fake_session):
        out = LR._build_micro_bar_df(_RaisingDB(), "ABCD", bar_seconds=15, meta=meta)

    assert out is recovered_df, "the fresh-session retry must recover the micro frame"
    assert meta.get("micro_retry_recovered") is True
    assert "micro_error_detail" in meta  # F1 still recorded the primary error
    assert calls["n"] == 2               # primary + one retry


def test_flag_off_no_retry_single_attempt():
    meta: dict = {}
    calls = {"n": 0}

    def _from_session(db, symbol, *, bar_seconds, lookback_minutes):
        calls["n"] += 1
        raise RuntimeError("boom-primary")

    with patch.object(LR, "settings", SimpleNamespace(
        chili_momentum_micro_fallback_1m_from_ticks_enabled=False,
    )), patch.object(LR, "_micro_bar_df_from_session", _from_session):
        out = LR._build_micro_bar_df(_RaisingDB(), "ABCD", bar_seconds=15, meta=meta)

    assert out is None
    assert calls["n"] == 1, "flag OFF -> single attempt (byte-identical legacy)"
    assert "micro_retry_recovered" not in meta


def test_retry_also_fails_records_retry_error():
    meta: dict = {}

    def _from_session(db, symbol, *, bar_seconds, lookback_minutes):
        raise RuntimeError("boom-both")

    fake_session = SimpleNamespace(rollback=lambda: None, close=lambda: None)

    with patch.object(LR, "settings", SimpleNamespace(
        chili_momentum_micro_fallback_1m_from_ticks_enabled=True,
    )), patch.object(LR, "_micro_bar_df_from_session", _from_session), \
        patch("app.db.SessionLocal", lambda: fake_session):
        out = LR._build_micro_bar_df(_RaisingDB(), "ABCD", bar_seconds=15, meta=meta)

    assert out is None
    assert "micro_error_detail" in meta
    assert "micro_retry_error_detail" in meta


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
