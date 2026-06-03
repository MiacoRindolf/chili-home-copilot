from pathlib import Path

import yaml


_COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _services() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def _volume_sources(service: dict) -> set[str]:
    return {str(volume).rsplit(":", 1)[0] for volume in service.get("volumes", [])}


def test_safe_support_lanes_auto_restart() -> None:
    services = _services()

    assert services["chili"]["restart"] == "unless-stopped"
    assert services["fast-scan-worker"]["restart"] == "unless-stopped"
    assert services["market-snapshot-worker"]["restart"] == "unless-stopped"


def test_auto_restart_support_lanes_do_not_bind_mount_runtime_code() -> None:
    services = _services()

    for name in ("chili", "fast-scan-worker", "market-snapshot-worker"):
        volumes = _volume_sources(services[name])
        assert "./app" not in volumes
        assert "./scripts" not in volumes


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
