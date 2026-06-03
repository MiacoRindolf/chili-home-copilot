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

    assert "pg_controldata /var/lib/postgresql/data" in postgres_block
    assert "Database cluster state:.*in production" in postgres_block
    assert "pg_isready" not in postgres_block
    assert "interval: 30s" in postgres_block
    assert "timeout: 5s" in postgres_block
    assert "start_period: 300s" in postgres_block


def test_postgres_data_source_can_move_to_named_volume_without_changing_default():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    service_start = compose.index("  postgres:")
    next_service = compose.index("\n  ollama:", service_start)
    postgres_block = compose[service_start:next_service]

    assert (
        "${CHILI_POSTGRES_DATA_SOURCE:-D:/CHILI-Docker/postgres}:/var/lib/postgresql/data"
        in postgres_block
    )
    assert "\nvolumes:\n" in compose
    assert "  chili-postgres-data:" in compose


def test_postgres_volume_migration_helper_is_non_destructive_by_default():
    script = Path("scripts/migrate-postgres-bind-to-volume.ps1").read_text(encoding="utf-8")

    assert "[switch]$Execute" in script
    assert "dry-run" in script
    assert "never deletes the original" in script
    assert 'Invoke-Logged docker @("compose", "stop", "postgres")' in script
    assert "Remove-Item" not in script
    assert 'Invoke-Logged docker @("compose", "down")' not in script
