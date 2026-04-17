"""Unit tests for `app/services/trading/triple_barrier.py` (pure math)."""
from __future__ import annotations

import pytest

from app.services.trading.triple_barrier import (
    OHLCVBar,
    TripleBarrierConfig,
    TripleBarrierLabel,
    compute_label,
    compute_label_atr,
)


def _bar(o: float, h: float, lo: float, c: float, v: float = 1e6) -> OHLCVBar:
    return OHLCVBar(open=o, high=h, low=lo, close=c, volume=v)


# ───────────────── TripleBarrierConfig validation ─────────────────


class TestConfigValidation:
    def test_valid_config(self) -> None:
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")
        assert cfg.tp_pct == 0.02
        assert cfg.side == "long"

    @pytest.mark.parametrize("bad_tp", [0, -0.01, -1.0])
    def test_invalid_tp(self, bad_tp: float) -> None:
        with pytest.raises(ValueError, match="tp_pct"):
            TripleBarrierConfig(tp_pct=bad_tp, sl_pct=0.01, max_bars=5)

    @pytest.mark.parametrize("bad_sl", [0, -0.01])
    def test_invalid_sl(self, bad_sl: float) -> None:
        with pytest.raises(ValueError, match="sl_pct"):
            TripleBarrierConfig(tp_pct=0.02, sl_pct=bad_sl, max_bars=5)

    @pytest.mark.parametrize("bad_n", [0, -1])
    def test_invalid_max_bars(self, bad_n: int) -> None:
        with pytest.raises(ValueError, match="max_bars"):
            TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=bad_n)

    def test_invalid_side(self) -> None:
        with pytest.raises(ValueError, match="side"):
            TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="sideways")  # type: ignore[arg-type]


# ───────────────── long trades ─────────────────


class TestLongTrades:
    def _cfg(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")

    def test_tp_hit_first_bar(self) -> None:
        """Entry 100, bar 0 high=103 breaches TP (102). Label = +1."""
        cfg = self._cfg()
        bars = [_bar(o=100, h=103, lo=99.5, c=102.5)]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == 1
        assert out.barrier_hit == "tp"
        assert out.exit_bar_idx == 0
        assert out.tp_price == pytest.approx(102.0)
        assert out.sl_price == pytest.approx(99.0)
        assert out.realized_return_pct == pytest.approx(0.02)

    def test_sl_hit_first_bar(self) -> None:
        """Entry 100, bar 0 low=98.5 breaches SL (99.0). Label = -1."""
        cfg = self._cfg()
        bars = [_bar(o=100, h=100.5, lo=98.5, c=99.0)]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == -1
        assert out.barrier_hit == "sl"
        assert out.exit_bar_idx == 0
        assert out.realized_return_pct == pytest.approx(-0.01)

    def test_tp_hit_middle_bar(self) -> None:
        cfg = self._cfg()
        bars = [
            _bar(100, 100.5, 99.5, 100.0),   # inside
            _bar(100, 101.5, 99.5, 101.0),   # inside
            _bar(101, 103.0, 100.5, 102.5),  # TP hit
        ]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == 1
        assert out.exit_bar_idx == 2
        assert out.barrier_hit == "tp"

    def test_timeout_positive(self) -> None:
        cfg = self._cfg()
        bars = [_bar(100, 101.5, 99.5, 100.5 + i * 0.1) for i in range(5)]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == 0
        assert out.barrier_hit == "timeout"
        assert out.exit_bar_idx == 4
        assert out.realized_return_pct == pytest.approx((bars[-1].close - 100.0) / 100.0)

    def test_timeout_negative(self) -> None:
        cfg = self._cfg()
        bars = [_bar(100, 100.5, 99.5, 100 - i * 0.05) for i in range(5)]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == 0
        assert out.barrier_hit == "timeout"
        assert out.realized_return_pct < 0

    def test_tie_break_sl_first(self) -> None:
        """Bar 0 has both extremes — by rule we assume SL hit first."""
        cfg = self._cfg()
        bars = [_bar(o=100, h=103.0, lo=98.5, c=101.0)]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == -1
        assert out.barrier_hit == "sl"
        assert out.realized_return_pct == pytest.approx(-0.01)

    def test_no_future_bars(self) -> None:
        cfg = self._cfg()
        out = compute_label(entry_close=100.0, future_bars=[], cfg=cfg)
        assert out.label == 0
        assert out.barrier_hit == "missing_data"
        assert out.exit_bar_idx == -1

    def test_max_bars_truncation(self) -> None:
        """If more bars are provided than ``max_bars``, only first ``max_bars`` are used."""
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=3, side="long")
        bars = (
            [_bar(100, 100.5, 99.5, 100.0)] * 3
            + [_bar(100, 103.0, 99.5, 102.5)]  # would be TP but beyond horizon
        )
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == 0
        assert out.barrier_hit == "timeout"
        assert out.exit_bar_idx == 2


# ───────────────── short trades ─────────────────


class TestShortTrades:
    def _cfg(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="short")

    def test_tp_hit(self) -> None:
        """Short entry 100 → TP at 98.0. Bar low 97.5 breaches TP."""
        cfg = self._cfg()
        bars = [_bar(100, 100.5, 97.5, 98.0)]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == 1
        assert out.barrier_hit == "tp"
        assert out.realized_return_pct == pytest.approx(0.02)

    def test_sl_hit(self) -> None:
        """Short entry 100 → SL at 101.0. Bar high 101.5 breaches SL."""
        cfg = self._cfg()
        bars = [_bar(100, 101.5, 99.0, 101.0)]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == -1
        assert out.barrier_hit == "sl"
        assert out.realized_return_pct == pytest.approx(-0.01)

    def test_tie_break(self) -> None:
        cfg = self._cfg()
        bars = [_bar(100, 101.5, 97.5, 99.0)]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == -1
        assert out.barrier_hit == "sl"


# ───────────────── input coercion ─────────────────


class TestInputCoercion:
    def test_accepts_dict_bars_lowercase(self) -> None:
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")
        bars = [
            {"open": 100.0, "high": 103.0, "low": 99.5, "close": 102.5, "volume": 1000},
        ]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == 1

    def test_accepts_dict_bars_capitalized(self) -> None:
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")
        bars = [
            {"Open": 100.0, "High": 103.0, "Low": 99.5, "Close": 102.5, "Volume": 1000},
        ]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        assert out.label == 1

    def test_skips_malformed_bars(self) -> None:
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")
        bars = [
            {"open": 100.0},  # missing fields
            _bar(100, 103.0, 99.5, 102.5),
        ]
        out = compute_label(entry_close=100.0, future_bars=bars, cfg=cfg)
        # first bar discarded by coercer → second bar seen at idx 0
        assert out.label == 1
        assert out.exit_bar_idx == 0


# ───────────────── invalid entry handling ─────────────────


class TestInvalidEntry:
    @pytest.mark.parametrize("bad_price", [0, -10.0, None])
    def test_bad_entry_close(self, bad_price: float) -> None:
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")
        out = compute_label(
            entry_close=bad_price,  # type: ignore[arg-type]
            future_bars=[_bar(100, 103, 99, 101)],
            cfg=cfg,
        )
        assert out.label == 0
        assert out.barrier_hit == "missing_data"


# ───────────────── ATR variant ─────────────────


class TestATRVariant:
    def test_atr_tp_hit(self) -> None:
        """Entry 100, ATR 2.0, tp_mult 2, sl_mult 1 → TP @ 104, SL @ 98."""
        bars = [_bar(100, 104.5, 99.5, 104.0)]
        out = compute_label_atr(
            entry_close=100.0,
            entry_atr=2.0,
            future_bars=bars,
            atr_mult_tp=2.0,
            atr_mult_sl=1.0,
            max_bars=5,
            side="long",
        )
        assert out.label == 1
        assert out.tp_price == pytest.approx(104.0)
        assert out.sl_price == pytest.approx(98.0)

    def test_atr_sl_hit(self) -> None:
        bars = [_bar(100, 101.0, 97.5, 98.0)]
        out = compute_label_atr(
            entry_close=100.0,
            entry_atr=2.0,
            future_bars=bars,
            atr_mult_tp=2.0,
            atr_mult_sl=1.0,
            max_bars=5,
            side="long",
        )
        assert out.label == -1
        assert out.sl_price == pytest.approx(98.0)

    @pytest.mark.parametrize("bad_atr", [0, -1.0, None])
    def test_atr_invalid_atr_returns_missing(self, bad_atr: float) -> None:
        bars = [_bar(100, 105, 95, 102)]
        out = compute_label_atr(
            entry_close=100.0,
            entry_atr=bad_atr,  # type: ignore[arg-type]
            future_bars=bars,
        )
        assert out.label == 0
        assert out.barrier_hit == "missing_data"


# ───────────────── invariants ─────────────────


class TestInvariants:
    """Properties the labeler must always satisfy."""

    def test_realized_return_sign_matches_label_long(self) -> None:
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")
        # TP
        tp = compute_label(100.0, [_bar(100, 103, 99.5, 102.5)], cfg)
        assert tp.label == 1 and tp.realized_return_pct > 0
        # SL
        sl = compute_label(100.0, [_bar(100, 100.5, 98.5, 99.0)], cfg)
        assert sl.label == -1 and sl.realized_return_pct < 0

    def test_label_determinism(self) -> None:
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")
        bars = [_bar(100, 103, 99.5, 102.5)]
        out1 = compute_label(100.0, bars, cfg)
        out2 = compute_label(100.0, bars, cfg)
        assert out1 == out2

    def test_is_dataclass_frozen(self) -> None:
        cfg = TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5)
        with pytest.raises(Exception):
            cfg.tp_pct = 0.05  # type: ignore[misc]
