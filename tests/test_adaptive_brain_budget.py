"""Tests for the adaptive per-cycle mining resource budget.

The OHLCV/miner-rows caps used to be fixed at slow-serial-fetch sizes (ohlcv=280,
~38 min/cycle), throttling mining to <30% of the configured universe. Provider-
aware concurrency made the full-universe fetch fast + rate-safe, so the caps now
scale to COVER the universe. These tests pin that scaling and the override path.
"""
import pytest

from app.services.trading.brain_resource_budget import BrainResourceBudget
from app.config import settings


def test_ohlcv_cap_adaptive_covers_universe(monkeypatch):
    monkeypatch.setattr(settings, "brain_budget_ohlcv_per_cycle", None)
    monkeypatch.setattr(settings, "brain_mine_patterns_max_tickers", 1000)
    monkeypatch.setattr(settings, "brain_intraday_intervals", "1m,5m,15m")
    b = BrainResourceBudget.from_settings()
    # 1000 * (1 + 3 intraday) + 1000//2 headroom = 4500
    assert b.ohlcv_cap == 4500
    assert b.ohlcv_cap > 280  # strictly larger than the old slow-fetch cap


def test_miner_rows_cap_adaptive(monkeypatch):
    monkeypatch.setattr(settings, "brain_budget_miner_rows_per_cycle", None)
    monkeypatch.setattr(settings, "brain_mine_patterns_max_tickers", 1000)
    b = BrainResourceBudget.from_settings()
    assert b.miner_rows_cap == 400000  # 1000 * 400 bars/ticker
    assert b.miner_rows_cap > 100000


def test_explicit_override_still_pins(monkeypatch):
    monkeypatch.setattr(settings, "brain_budget_ohlcv_per_cycle", 500)
    monkeypatch.setattr(settings, "brain_budget_miner_rows_per_cycle", 50000)
    b = BrainResourceBudget.from_settings()
    assert b.ohlcv_cap == 500
    assert b.miner_rows_cap == 50000


def test_ohlcv_scales_down_with_smaller_universe(monkeypatch):
    monkeypatch.setattr(settings, "brain_budget_ohlcv_per_cycle", None)
    monkeypatch.setattr(settings, "brain_intraday_intervals", "1m,5m,15m")
    monkeypatch.setattr(settings, "brain_mine_patterns_max_tickers", 500)
    b = BrainResourceBudget.from_settings()
    assert b.ohlcv_cap == 500 * 4 + 250  # 2250 — adaptive to the universe size


def test_no_intraday_intervals(monkeypatch):
    monkeypatch.setattr(settings, "brain_budget_ohlcv_per_cycle", None)
    monkeypatch.setattr(settings, "brain_mine_patterns_max_tickers", 1000)
    monkeypatch.setattr(settings, "brain_intraday_intervals", "")
    b = BrainResourceBudget.from_settings()
    assert b.ohlcv_cap == 1000 + 500  # just the 1d sweep + headroom


def test_zero_override_means_unlimited(monkeypatch):
    # explicit 0 stays "unlimited" (remaining_ohlcv -> None), not adaptive
    monkeypatch.setattr(settings, "brain_budget_ohlcv_per_cycle", 0)
    b = BrainResourceBudget.from_settings()
    assert b.ohlcv_cap == 0
    assert b.remaining_ohlcv() is None
    assert b.try_ohlcv("miner", 999999) is True
