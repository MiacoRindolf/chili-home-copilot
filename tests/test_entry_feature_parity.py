"""P1.4 — runtime feature-parity assertion at entry tests.

Verifies the core parity module + its ``TradingExecutionEvent`` drift-event
persistence. Headline guarantees:

* Feature flag off → every call returns ``ok=True`` with
  ``mode='disabled'`` and writes nothing.
* ``soft`` mode (default) records + alerts on drift but never blocks —
  this is the 2-week shakedown state.
* ``hard`` mode blocks only on ``critical`` severity. Warn-level drift
  still records + alerts but is allowed through.
* A single boolean mismatch is always ``critical`` (semantic-contract
  violation) regardless of the mismatch-count threshold.
* Numeric tolerance uses OR semantics (``abs OR rel``) — small-magnitude
  values pass on absolute, large-magnitude pass on relative.
* Missing-key asymmetry is ``warn``, not critical: schema drift worth
  flagging but not blocking entry.
* The drift event is written to the ``TradingExecutionEvent`` stream
  with ``event_type='feature_parity_drift'`` and carries per-feature
  deltas in ``payload_json.deltas`` so a watchdog can aggregate across
  calls.
* All exceptions (compute / persist / alert) fail open.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.trading import TradingExecutionEvent
from app.services.trading import feature_parity
from app.services.trading.feature_parity import (
    DEFAULT_FEATURES,
    DEFAULT_NUMERIC_FEATURES,
    EVENT_TYPE,
    FeatureDelta,
    MODE_DISABLED,
    MODE_HARD,
    MODE_SOFT,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    ParityResult,
    _resolve_settings,
    check_entry_feature_parity,
    diff_feature_vectors,
    extract_last_row_snapshot,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def _enable_parity_soft(monkeypatch):
    """Flag on, soft mode, tight tolerances, alerts off (so tests don't
    hit the SMS path)."""
    monkeypatch.setattr(settings, "chili_feature_parity_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_feature_parity_mode", MODE_SOFT, raising=False)
    monkeypatch.setattr(settings, "chili_feature_parity_epsilon_abs", 1e-6, raising=False)
    monkeypatch.setattr(settings, "chili_feature_parity_epsilon_rel", 0.005, raising=False)
    monkeypatch.setattr(
        settings, "chili_feature_parity_critical_mismatch_count", 3, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_feature_parity_alert_on_warn", False, raising=False
    )
    yield


@pytest.fixture()
def _enable_parity_hard(monkeypatch):
    """Flag on, hard mode, same tolerances. Hard mode blocks on critical."""
    monkeypatch.setattr(settings, "chili_feature_parity_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_feature_parity_mode", MODE_HARD, raising=False)
    monkeypatch.setattr(settings, "chili_feature_parity_epsilon_abs", 1e-6, raising=False)
    monkeypatch.setattr(settings, "chili_feature_parity_epsilon_rel", 0.005, raising=False)
    monkeypatch.setattr(
        settings, "chili_feature_parity_critical_mismatch_count", 3, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_feature_parity_alert_on_warn", False, raising=False
    )
    yield


def _synth_ohlcv(n_bars: int = 250, seed: int = 42) -> pd.DataFrame:
    """Deterministic synthetic OHLCV. Ramp + noise so indicators have
    valid last-row values."""
    rng = np.random.default_rng(seed)
    base = np.linspace(100.0, 150.0, n_bars)
    noise = rng.normal(0.0, 0.8, n_bars)
    close = base + noise
    high = close + rng.uniform(0.2, 1.2, n_bars)
    low = close - rng.uniform(0.2, 1.2, n_bars)
    open_ = close + rng.uniform(-0.5, 0.5, n_bars)
    volume = rng.integers(800_000, 3_000_000, n_bars).astype(float)
    idx = pd.date_range("2025-01-01", periods=n_bars, freq="D")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


# ── 1. Settings resolution ────────────────────────────────────────────────


class TestResolveSettings:
    def test_defaults_when_unchanged(self):
        cfg = _resolve_settings()
        assert cfg["enabled"] is False
        assert cfg["mode"] == MODE_SOFT  # config default
        assert 0.0 < cfg["epsilon_abs"] < 1.0
        assert 0.0 < cfg["epsilon_rel"] < 1.0
        assert int(cfg["critical_mismatch_count"]) >= 1
        assert isinstance(cfg["alert_on_warn"], bool)

    def test_invalid_mode_coerces_to_soft(self, monkeypatch):
        """Typo-safe: garbage mode → soft, not an exception at call time."""
        monkeypatch.setattr(
            settings, "chili_feature_parity_mode", "gibberish", raising=False
        )
        cfg = _resolve_settings()
        assert cfg["mode"] == MODE_SOFT

    def test_monkeypatch_takes_effect_live(self, monkeypatch):
        monkeypatch.setattr(settings, "chili_feature_parity_enabled", True, raising=False)
        monkeypatch.setattr(settings, "chili_feature_parity_mode", MODE_HARD, raising=False)
        cfg = _resolve_settings()
        assert cfg["enabled"] is True
        assert cfg["mode"] == MODE_HARD

    def test_mode_trimmed_and_lowercased(self, monkeypatch):
        monkeypatch.setattr(
            settings, "chili_feature_parity_mode", "  HARD  ", raising=False
        )
        cfg = _resolve_settings()
        assert cfg["mode"] == MODE_HARD


# ── 2. extract_last_row_snapshot ──────────────────────────────────────────


class TestExtractLastRow:
    def test_basic_last_row_kept(self):
        arrays = {
            "price": [100.0, 101.0, 102.0],
            "rsi_14": [45.0, 50.0, 55.0],
        }
        snap = extract_last_row_snapshot(arrays)
        assert snap == {"price": 102.0, "rsi_14": 55.0}

    def test_none_last_row_dropped(self):
        """None last value means 'not computable yet' — both sides will
        treat this identically and no delta fires."""
        arrays = {
            "rsi_14": [None, None, None],
            "price": [100.0, 101.0, 102.0],
        }
        snap = extract_last_row_snapshot(arrays)
        assert "rsi_14" not in snap
        assert snap["price"] == 102.0

    def test_needed_filters_output(self):
        arrays = {
            "price": [100.0],
            "rsi_14": [50.0],
            "atr": [1.2],
        }
        snap = extract_last_row_snapshot(arrays, needed={"price", "atr"})
        assert set(snap) == {"price", "atr"}
        assert snap["price"] == 100.0

    def test_empty_arrays_skipped(self):
        arrays = {
            "price": [],
            "rsi_14": [50.0],
        }
        snap = extract_last_row_snapshot(arrays)
        assert snap == {"rsi_14": 50.0}

    def test_non_list_values_skipped(self):
        arrays = {
            "price": "not a list",  # defensive: stray value shouldn't crash
            "rsi_14": [50.0],
        }
        snap = extract_last_row_snapshot(arrays)
        assert snap == {"rsi_14": 50.0}


# ── 3. diff_feature_vectors ───────────────────────────────────────────────


class TestDiffVectors:
    def test_all_equal_is_ok(self):
        live = {"price": 100.0, "rsi_14": 50.0, "ema_stack": True}
        ref = {"price": 100.0, "rsi_14": 50.0, "ema_stack": True}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_OK
        assert deltas == ()

    def test_within_abs_tolerance_is_ok(self):
        """Small numeric diff at large magnitude — abs passes even if rel
        would nearly match. Demonstrates OR semantics for small abs."""
        live = {"price": 100.0000005}
        ref = {"price": 100.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.0001
        )
        assert sev == SEVERITY_OK
        assert deltas == ()

    def test_within_rel_tolerance_is_ok(self):
        """Large numeric diff at large magnitude — rel passes even if abs
        would flag. Demonstrates OR semantics for large rel."""
        live = {"price": 100.4}  # 0.4% diff vs 100.0
        ref = {"price": 100.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_OK
        assert deltas == ()

    def test_outside_tolerance_is_warn(self):
        """Numeric diff beyond BOTH thresholds — single warn."""
        live = {"price": 105.0}  # 5% diff
        ref = {"price": 100.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_WARN
        assert len(deltas) == 1
        assert deltas[0].name == "price"
        assert deltas[0].kind == "numeric"
        assert deltas[0].severity == SEVERITY_WARN
        assert deltas[0].abs_delta == pytest.approx(5.0)
        assert deltas[0].rel_delta == pytest.approx(0.05)

    def test_bool_mismatch_is_always_critical(self):
        """**Headline guarantee**: a single bool mismatch trips critical
        regardless of ``critical_mismatch_count`` — pattern-engine conditions
        like ``ema_stack`` feed rule gates; a silent flip changes decisions."""
        live = {"ema_stack": True}
        ref = {"ema_stack": False}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005,
            critical_mismatch_count=99,  # deliberately huge
        )
        assert sev == SEVERITY_CRITICAL
        assert len(deltas) == 1
        assert deltas[0].kind == "bool"
        assert deltas[0].severity == SEVERITY_CRITICAL

    def test_missing_live_is_warn(self):
        live = {"price": 100.0}
        ref = {"price": 100.0, "rsi_14": 50.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_WARN
        assert len(deltas) == 1
        assert deltas[0].kind == "missing_live"
        assert deltas[0].name == "rsi_14"

    def test_missing_reference_is_warn(self):
        live = {"price": 100.0, "rsi_14": 50.0}
        ref = {"price": 100.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_WARN
        assert len(deltas) == 1
        assert deltas[0].kind == "missing_reference"
        assert deltas[0].name == "rsi_14"

    def test_both_none_is_not_a_mismatch(self):
        """Both sides agree on 'not computable yet' → no delta."""
        live = {"rsi_14": None, "price": 100.0}
        ref = {"rsi_14": None, "price": 100.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_OK
        assert deltas == ()

    def test_critical_triggers_on_threshold_count(self):
        """Three numeric warns → overall critical when threshold==3."""
        live = {"price": 110.0, "rsi_14": 55.0, "atr": 2.0}
        ref = {"price": 100.0, "rsi_14": 50.0, "atr": 1.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005,
            critical_mismatch_count=3,
        )
        assert sev == SEVERITY_CRITICAL
        assert len(deltas) == 3

    def test_below_threshold_stays_warn(self):
        """Two numeric mismatches with threshold=3 → warn, not critical."""
        live = {"price": 110.0, "rsi_14": 55.0}
        ref = {"price": 100.0, "rsi_14": 50.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005,
            critical_mismatch_count=3,
        )
        assert sev == SEVERITY_WARN
        assert len(deltas) == 2

    def test_features_allowlist_restricts_diff(self):
        """When ``features`` is supplied, keys outside are ignored even if
        they mismatch — operator opts into what they care about."""
        live = {"price": 110.0, "rsi_14": 99.0}  # rsi_14 mismatches hugely
        ref = {"price": 100.0, "rsi_14": 50.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005,
            features={"price"},  # only check price
        )
        assert sev == SEVERITY_WARN
        assert len(deltas) == 1
        assert deltas[0].name == "price"

    def test_non_numeric_non_bool_is_warn(self):
        """Defensive: if somehow a string slips in, classify as warn
        (don't raise)."""
        live = {"weird_key": "a"}
        ref = {"weird_key": "b"}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_WARN
        assert len(deltas) == 1
        assert deltas[0].kind == "numeric"  # caught in the float() except path

    def test_int_is_numeric_not_bool(self):
        """``isinstance(True, int)`` trap: our strict ``_is_bool`` guards
        against classifying int 1 as boolean."""
        live = {"count": 10}  # plain int
        ref = {"count": 10}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_OK

    def test_zero_reference_rel_not_computed(self):
        """When reference is 0, rel_delta is None (divide-by-zero guard).
        Gate falls back to abs tolerance only."""
        live = {"stoch_k": 0.5}
        ref = {"stoch_k": 0.0}
        deltas, sev = diff_feature_vectors(
            live, ref, epsilon_abs=1e-6, epsilon_rel=0.005
        )
        assert sev == SEVERITY_WARN
        assert len(deltas) == 1
        assert deltas[0].rel_delta is None


# ── 4. Feature-flag / disabled ────────────────────────────────────────────


class TestFeatureFlag:
    def test_disabled_is_always_ok(self, monkeypatch):
        """Flag OFF = hard bypass regardless of inputs. This is the
        migration-safety headline for P1.4."""
        monkeypatch.setattr(
            settings, "chili_feature_parity_enabled", False, raising=False
        )
        df = _synth_ohlcv()
        # Deliberately corrupt live snap so that if the check ran, it
        # would definitely fire deltas.
        live = {"price": 999_999.0, "rsi_14": -99.0}
        result = check_entry_feature_parity(
            None,
            ticker="TEST",
            live_snap=live,
            reference_df=df,
            source="unit",
        )
        assert result.ok is True
        assert result.severity == SEVERITY_OK
        assert result.mode == MODE_DISABLED
        assert result.deltas == ()
        assert result.record_id is None

    def test_disabled_writes_nothing(self, db, monkeypatch):
        monkeypatch.setattr(
            settings, "chili_feature_parity_enabled", False, raising=False
        )
        df = _synth_ohlcv()
        live = {"price": 999_999.0}
        n_before = db.scalar(
            select(TradingExecutionEvent.id)
            .where(TradingExecutionEvent.event_type == EVENT_TYPE)
            .order_by(TradingExecutionEvent.id.desc())
        )
        check_entry_feature_parity(
            db,
            ticker="TEST",
            live_snap=live,
            reference_df=df,
            source="unit",
        )
        n_after = db.scalar(
            select(TradingExecutionEvent.id)
            .where(TradingExecutionEvent.event_type == EVENT_TYPE)
            .order_by(TradingExecutionEvent.id.desc())
        )
        assert n_before == n_after


# ── 5. Clean-path / OK behavior ───────────────────────────────────────────


class TestCleanPath:
    def test_matching_live_snap_is_ok(self, _enable_parity_soft, db):
        """When the live snap was computed the same way as the reference
        — the canonical case — severity is OK and no row is written."""
        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        result = check_entry_feature_parity(
            db,
            ticker="TEST",
            live_snap=live,
            reference_df=df,
            source="unit",
        )
        assert result.ok is True
        assert result.severity == SEVERITY_OK
        assert result.deltas == ()
        assert result.record_id is None

    def test_no_reference_df_is_ok(self, _enable_parity_soft, db):
        result = check_entry_feature_parity(
            db,
            ticker="TEST",
            live_snap={"price": 100.0},
            reference_df=None,
            source="unit",
        )
        assert result.ok is True
        assert result.severity == SEVERITY_OK
        assert result.reason == "no_reference_df"

    def test_empty_reference_df_is_ok(self, _enable_parity_soft, db):
        result = check_entry_feature_parity(
            db,
            ticker="TEST",
            live_snap={"price": 100.0},
            reference_df=pd.DataFrame(),
            source="unit",
        )
        assert result.ok is True
        assert result.reason == "no_reference_df"


# ── 6. Soft mode — drift detected, never blocks ───────────────────────────


class TestSoftMode:
    def test_soft_warn_records_but_allows(self, _enable_parity_soft, db):
        """Soft mode: drift is recorded + returned but ``ok=True``."""
        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        # Corrupt one numeric feature so it's clearly out of tolerance.
        live["price"] = float(live["price"]) * 1.05  # 5% drift
        result = check_entry_feature_parity(
            db,
            ticker="AAPL",
            live_snap=live,
            reference_df=df,
            source="auto_trader_v1",
        )
        assert result.ok is True  # soft never blocks
        assert result.severity == SEVERITY_WARN
        assert result.mode == MODE_SOFT
        assert len(result.deltas) == 1
        assert result.deltas[0].name == "price"
        assert result.record_id is not None  # persisted

    def test_soft_critical_still_allows_but_reason_set(self, _enable_parity_soft, db):
        """**Headline**: a critical boolean mismatch in SOFT mode records
        and reports severity=critical, but ``ok=True``. This is the
        shakedown contract — we observe without gating."""
        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        # Flip a bool value in live to create a semantic mismatch.
        live["ema_stack"] = not bool(arrays.get("ema_stack", [False])[-1])
        result = check_entry_feature_parity(
            db,
            ticker="AAPL",
            live_snap=live,
            reference_df=df,
            source="auto_trader_v1",
        )
        assert result.ok is True  # soft never blocks
        assert result.severity == SEVERITY_CRITICAL
        assert result.mode == MODE_SOFT
        assert result.reason is not None
        assert "critical" in result.reason
        assert result.record_id is not None


# ── 7. Hard mode — blocks only on critical ────────────────────────────────


class TestHardMode:
    def test_hard_warn_still_allows(self, _enable_parity_hard, db):
        """Hard mode allows warn (1-2 numeric deltas) — only critical blocks."""
        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        # Single numeric drift — warn, not critical.
        live["price"] = float(live["price"]) * 1.05
        result = check_entry_feature_parity(
            db,
            ticker="AAPL",
            live_snap=live,
            reference_df=df,
            source="auto_trader_v1",
        )
        assert result.ok is True  # warn passes through in hard mode
        assert result.severity == SEVERITY_WARN
        assert result.mode == MODE_HARD
        assert result.record_id is not None

    def test_hard_critical_blocks(self, _enable_parity_hard, db):
        """**Headline guarantee**: hard mode + critical severity returns
        ``ok=False`` with ``reason`` set — the entry gate must block."""
        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        # Flip a bool — always critical.
        live["ema_stack"] = not bool(arrays.get("ema_stack", [False])[-1])
        result = check_entry_feature_parity(
            db,
            ticker="AAPL",
            live_snap=live,
            reference_df=df,
            source="auto_trader_v1",
        )
        assert result.ok is False  # hard mode blocks critical
        assert result.severity == SEVERITY_CRITICAL
        assert result.mode == MODE_HARD
        assert result.reason is not None
        assert "feature_parity_critical" in result.reason
        assert result.record_id is not None


# ── 8. Persistence shape ──────────────────────────────────────────────────


class TestPersistence:
    def test_drift_event_is_readable(self, _enable_parity_soft, db):
        """The written row has event_type='feature_parity_drift',
        severity in ``status``, and per-delta payload."""
        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        live["price"] = float(live["price"]) * 1.10  # 10% drift
        result = check_entry_feature_parity(
            db,
            ticker="AAPL",
            live_snap=live,
            reference_df=df,
            source="auto_trader_v1",
            venue="robinhood",
        )
        assert result.record_id is not None

        row = db.get(TradingExecutionEvent, result.record_id)
        assert row is not None
        assert row.event_type == EVENT_TYPE
        assert row.status == SEVERITY_WARN
        assert row.ticker == "AAPL"
        assert row.venue == "robinhood"
        payload = row.payload_json or {}
        assert payload.get("severity") == SEVERITY_WARN
        assert payload.get("source") == "auto_trader_v1"
        assert isinstance(payload.get("deltas"), list)
        assert len(payload["deltas"]) >= 1
        d0 = payload["deltas"][0]
        assert set(d0.keys()) >= {
            "name", "kind", "severity",
            "live_value", "reference_value",
            "abs_delta", "rel_delta",
        }

    def test_persist_db_none_is_tolerated(self, _enable_parity_soft):
        """When no DB is supplied, result still classifies correctly — it
        just can't persist. ``record_id`` is None."""
        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        live["price"] = float(live["price"]) * 1.05
        result = check_entry_feature_parity(
            None,  # no db
            ticker="TEST",
            live_snap=live,
            reference_df=df,
            source="unit",
        )
        assert result.severity == SEVERITY_WARN
        assert result.record_id is None


# ── 9. Failure-mode tolerance ─────────────────────────────────────────────


class TestFailureModeTolerance:
    def test_compute_exception_fails_open(self, _enable_parity_soft, monkeypatch):
        """If ``compute_all_from_df`` raises, the gate returns ok=True
        with reason='ref_compute_failed' — never blocks."""
        def _boom(*a, **kw):
            raise RuntimeError("synthetic compute failure")

        monkeypatch.setattr(feature_parity, "compute_all_from_df", _boom)

        df = _synth_ohlcv()
        result = check_entry_feature_parity(
            None,
            ticker="TEST",
            live_snap={"price": 100.0},
            reference_df=df,
            source="unit",
        )
        assert result.ok is True
        assert result.reason == "ref_compute_failed"
        assert result.severity == SEVERITY_OK

    def test_persist_exception_does_not_block(self, _enable_parity_soft, db, monkeypatch):
        """If the persist call raises, the parity result is still returned
        with the severity — record_id just ends up None."""
        def _boom(*a, **kw):
            raise RuntimeError("synthetic persist failure")

        monkeypatch.setattr(feature_parity, "_persist_drift_event", _boom)

        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        live["price"] = float(live["price"]) * 1.05
        result = check_entry_feature_parity(
            db,
            ticker="TEST",
            live_snap=live,
            reference_df=df,
            source="unit",
        )
        assert result.ok is True  # soft; never blocks
        assert result.severity == SEVERITY_WARN
        assert result.record_id is None  # persist swallowed


# ── 10. Feature-set independence ──────────────────────────────────────────


class TestFeatureSet:
    def test_live_keys_outside_requested_set_ignored(self, _enable_parity_soft, db):
        """Passing a ``features`` allowlist filters what's diffed. Keys in
        ``live_snap`` that aren't in the allowlist AND aren't in the
        reference are simply not compared (no 'missing_reference' noise)."""
        df = _synth_ohlcv()
        live = {
            "price": 100.0,  # in allowlist
            "alert_id": 42,  # NOT in DEFAULT_FEATURES, shouldn't produce delta
        }
        result = check_entry_feature_parity(
            db,
            ticker="TEST",
            live_snap=live,
            reference_df=df,
            features={"price"},
            source="unit",
        )
        # The filtered live ({'price':100.0}) vs reference (ref['price'] from df)
        # differs by ~30% so price fires a warn — but no delta for alert_id.
        for d in result.deltas:
            assert d.name != "alert_id"


# ── 11. ParityResult contract ─────────────────────────────────────────────


class TestParityResultContract:
    def test_frozen_dataclass(self):
        r = ParityResult(
            ok=True, severity=SEVERITY_OK, mode=MODE_SOFT, reason=None,
            deltas=(), n_features_checked=0, n_mismatches=0, record_id=None,
        )
        with pytest.raises(Exception):
            r.ok = False  # type: ignore[misc]

    def test_deltas_is_tuple_not_list(self, _enable_parity_soft, db):
        """Frozen shape — ``deltas`` must be a tuple so callers can't
        mutate it in-place."""
        df = _synth_ohlcv()
        from app.services.trading.indicator_core import compute_all_from_df

        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
        live = extract_last_row_snapshot(arrays, needed=DEFAULT_FEATURES)
        live["price"] = float(live["price"]) * 1.05
        result = check_entry_feature_parity(
            db,
            ticker="TEST",
            live_snap=live,
            reference_df=df,
            source="unit",
        )
        assert isinstance(result.deltas, tuple)


# ── 12. Default feature catalog sanity ────────────────────────────────────


class TestDefaultFeatures:
    def test_default_has_numeric_and_bool(self):
        assert len(DEFAULT_NUMERIC_FEATURES) > 10
        assert len(DEFAULT_FEATURES) > len(DEFAULT_NUMERIC_FEATURES)

    def test_price_is_in_defaults(self):
        assert "price" in DEFAULT_FEATURES
        assert "price" in DEFAULT_NUMERIC_FEATURES

    def test_bools_are_not_in_numeric(self):
        """``ema_stack`` is a bool; must not be in the numeric set or
        tolerance rules would apply the wrong way."""
        assert "ema_stack" not in DEFAULT_NUMERIC_FEATURES
        assert "ema_stack" in DEFAULT_FEATURES
