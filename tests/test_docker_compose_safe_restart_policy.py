from pathlib import Path

import yaml


_COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _services() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def test_safe_support_lanes_auto_restart() -> None:
    services = _services()

    assert services["chili"]["restart"] == "unless-stopped"
    assert services["fast-scan-worker"]["restart"] == "unless-stopped"
    assert services["market-snapshot-worker"]["restart"] == "unless-stopped"


def test_live_order_lanes_remain_operator_opt_in_for_restart() -> None:
    services = _services()

    assert (
        services["autotrader-worker"]["restart"]
        == "${CHILI_BIND_MOUNTED_SERVICE_RESTART_POLICY:-no}"
    )
    assert (
        services["broker-sync-worker"]["restart"]
        == "${CHILI_BIND_MOUNTED_SERVICE_RESTART_POLICY:-no}"
    )
