"""Guards for retiring CHILI v1 generic breakout-alert machinery."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dispatcher_no_longer_claims_breakout_outcome_events() -> None:
    src = (ROOT / "app" / "services" / "trading" / "brain_work" / "dispatcher.py").read_text(
        encoding="utf-8"
    )

    assert '"breakout_alert_resolved"' not in src
    assert "handle_breakout_alert_resolved" not in src
    assert "breakout_outcomes" not in src


def test_scheduler_no_longer_registers_generic_breakout_jobs() -> None:
    src = (ROOT / "app" / "services" / "trading_scheduler.py").read_text(encoding="utf-8")

    assert 'id="crypto_breakout_scanner"' not in src
    assert 'id="stock_breakout_scanner"' not in src
    assert 'id="breakout_outcome_checker"' not in src
    assert "pattern_imminent_scanner" in src


def test_breakout_outcome_learning_is_noop() -> None:
    from app.services.trading.learning import learn_from_breakout_outcomes

    result = learn_from_breakout_outcomes(None, None)

    assert result == {
        "patterns_learned": 0,
        "scan_patterns_updated": 0,
        "total_resolved": 0,
        "skipped": "legacy_breakout_outcomes_removed",
    }


def test_legacy_scanner_entrypoints_return_disabled() -> None:
    from app.services.trading.scanner import run_breakout_scan, run_crypto_breakout_scan

    crypto = run_crypto_breakout_scan()
    stock = run_breakout_scan()

    assert crypto["disabled"] is True
    assert stock["disabled"] is True
    assert crypto["reason"] == "legacy_breakout_scanner_removed"
    assert stock["reason"] == "legacy_breakout_scanner_removed"
