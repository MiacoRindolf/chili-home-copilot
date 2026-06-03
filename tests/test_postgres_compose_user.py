from pathlib import Path


def test_postgres_compose_runs_as_postgres_user():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    service_start = compose.index("  postgres:")
    next_service = compose.index("\n  ollama:", service_start)
    postgres_block = compose[service_start:next_service]

    assert "user: postgres" in postgres_block
