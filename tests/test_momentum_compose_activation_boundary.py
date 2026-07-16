from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_compose_cannot_reenable_broker_fsm_on_rebuild() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    runner_values = re.findall(
        r"CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=([^\s#]+)", compose
    )
    scheduler_values = re.findall(
        r"CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED=([^\s#]+)", compose
    )

    assert runner_values, "compose must pin the broker-facing runner boundary"
    assert scheduler_values, "compose must pin the broker-facing scheduler boundary"
    assert set(runner_values) == {"0"}
    assert set(scheduler_values) == {"0"}
