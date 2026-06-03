from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]


def _compose_service_block(service_name: str) -> str:
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^  {re.escape(service_name)}:\n(?P<block>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        compose,
    )
    assert match is not None, f"{service_name} service not found in docker-compose.yml"
    return match.group("block")


def test_autotrader_worker_forces_single_tick_instance() -> None:
    block = _compose_service_block("autotrader-worker")

    assert "CHILI_AUTOTRADER_TICK_MAX_INSTANCES=1" in block
    assert "CHILI_AUTOTRADER_TICK_MAX_INSTANCES=${" not in block


def test_autotrader_worker_uses_skip_storm_resistant_cadence() -> None:
    block = _compose_service_block("autotrader-worker")

    assert "CHILI_AUTOTRADER_TICK_INTERVAL_SECONDS=60" in block
    assert "CHILI_AUTOTRADER_MONITOR_INTERVAL_SECONDS=60" in block


def test_autotrader_tick_default_is_single_instance() -> None:
    config = (REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")

    assert re.search(
        r"chili_autotrader_tick_max_instances: int = Field\(\s*default=1,",
        config,
    )
