"""The per-tick candidate fetch batch must be sized adaptively from the tick
budget and observed per-candidate latency — not a hardcoded count.
"""
import app.services.trading.auto_trader as at


def test_adaptive_grows_for_fast_candidates(monkeypatch):
    monkeypatch.setattr(at.settings, "chili_autotrader_candidate_batch_adaptive", True, raising=False)
    monkeypatch.setattr(at, "_autotrader_tick_soft_budget_seconds", lambda: 15)
    monkeypatch.setattr(at, "_candidate_tick_ewma_s", 0.3)  # fast skips: 0.3s each
    # 15 / 0.3 = 50 -> clamped to the max band
    assert at._autotrader_candidate_batch_size() == at.AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE


def test_adaptive_shrinks_for_slow_candidates(monkeypatch):
    monkeypatch.setattr(at.settings, "chili_autotrader_candidate_batch_adaptive", True, raising=False)
    monkeypatch.setattr(at, "_autotrader_tick_soft_budget_seconds", lambda: 15)
    monkeypatch.setattr(at, "_candidate_tick_ewma_s", 10.0)  # slow LLM revalidation: 10s each
    # 15 / 10 = 1 -> clamped up to the default floor (never overruns badly; budget defers)
    assert at._autotrader_candidate_batch_size() == at.AUTOTRADER_DEFAULT_CANDIDATE_BATCH_SIZE


def test_cold_start_seeds_from_config(monkeypatch):
    monkeypatch.setattr(at.settings, "chili_autotrader_candidate_batch_adaptive", True, raising=False)
    monkeypatch.setattr(at.settings, "chili_autotrader_candidate_batch_size", 7, raising=False)
    monkeypatch.setattr(at, "_candidate_tick_ewma_s", 0.0)  # no telemetry yet
    assert at._autotrader_candidate_batch_size() == 7


def test_static_mode_pins_to_config(monkeypatch):
    monkeypatch.setattr(at.settings, "chili_autotrader_candidate_batch_adaptive", False, raising=False)
    monkeypatch.setattr(at.settings, "chili_autotrader_candidate_batch_size", 12, raising=False)
    monkeypatch.setattr(at, "_candidate_tick_ewma_s", 0.3)  # would be 50 if adaptive
    assert at._autotrader_candidate_batch_size() == 12


def test_ewma_folds_latency(monkeypatch):
    monkeypatch.setattr(at, "_candidate_tick_ewma_s", 0.0)
    at._update_candidate_tick_ewma(6.0, 12)   # 0.5s/candidate; cold start -> 0.5
    assert abs(at._candidate_tick_ewma_s - 0.5) < 1e-9
    at._update_candidate_tick_ewma(15.0, 10)  # 1.5s/candidate -> 0.7*0.5 + 0.3*1.5 = 0.8
    assert abs(at._candidate_tick_ewma_s - 0.8) < 1e-9
    at._update_candidate_tick_ewma(0.0, 0)    # degenerate -> no-op
    assert abs(at._candidate_tick_ewma_s - 0.8) < 1e-9
