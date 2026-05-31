from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent


def _environment_map(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        env: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, value = item.split("=", 1)
            env[key] = value
        return env
    return {}


def _postgres_service_budgets() -> dict[str, int]:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    budgets: dict[str, int] = {}
    missing: list[str] = []

    for name, service in compose.get("services", {}).items():
        env = _environment_map(service.get("environment"))
        database_url = env.get("DATABASE_URL", "")
        if "@postgres:" not in database_url:
            continue
        try:
            pool_size = int(env["DATABASE_POOL_SIZE"])
            max_overflow = int(env["DATABASE_MAX_OVERFLOW"])
        except KeyError:
            missing.append(name)
            continue
        budgets[name] = pool_size + max_overflow

    assert not missing, (
        "Compose Postgres clients must declare DATABASE_POOL_SIZE and "
        f"DATABASE_MAX_OVERFLOW: {missing}"
    )
    return budgets


def test_compose_postgres_clients_have_bounded_pool_budgets() -> None:
    budgets = _postgres_service_budgets()

    assert sum(budgets.values()) <= 100, budgets
    assert budgets["scheduler-worker"] <= 12
    assert budgets["broker-sync-worker"] <= 8
    assert budgets["autotrader-worker"] <= 8
    assert budgets["fast-data-worker"] <= 4
