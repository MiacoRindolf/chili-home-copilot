from pathlib import Path


_COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _postgres_section() -> str:
    text = _COMPOSE.read_text(encoding="utf-8")
    return text.split("  postgres:", 1)[1].split("\n  ollama:", 1)[0]


def test_postgres_recovery_uses_syncfs_not_per_file_fsync() -> None:
    section = _postgres_section()

    assert "recovery_init_sync_method=syncfs" in section
    assert "recovery_init_sync_method=fsync" not in section


def test_postgres_healthcheck_does_not_open_database_connection_during_recovery() -> None:
    section = _postgres_section()

    assert "pg_controldata /var/lib/postgresql/data" in section
    assert "Database cluster state:.*in production" in section
    assert "pg_isready" not in section
