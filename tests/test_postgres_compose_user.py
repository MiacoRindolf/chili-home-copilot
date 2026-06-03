from pathlib import Path


def test_postgres_compose_runs_as_postgres_user():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    service_start = compose.index("  postgres:")
    next_service = compose.index("\n  ollama:", service_start)
    postgres_block = compose[service_start:next_service]

    assert "user: postgres" in postgres_block


def test_postgres_compose_uses_calm_healthcheck():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    service_start = compose.index("  postgres:")
    next_service = compose.index("\n  ollama:", service_start)
    postgres_block = compose[service_start:next_service]

    assert 'test: ["CMD-SHELL", "pg_isready -U chili -d chili -t 1"]' in postgres_block
    assert "pg_controldata" not in postgres_block
    assert "interval: 30s" in postgres_block
    assert "timeout: 5s" in postgres_block
    assert "start_period: 300s" in postgres_block
