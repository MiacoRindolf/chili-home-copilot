"""Autopilot validation must be real, not vacuous.

Regression coverage for the audit findings:

  * run_ast_syntax(worktree, changed_files) crashed every implementation run
    with TypeError (single-arg signature) — swallowed by the run-level
    except, so every run ended "failed" at validate.
  * subprocess_safe_env stripped TEST_DATABASE_URL, so every pytest step
    skip-passed at the conftest guard and "validation passed" meant nothing.
    Passthrough is fail-closed: only ``_test``-suffixed database names.
  * pytest_targeted now reports tests_executed honestly (collect-only or
    skipped steps are not the same as passing tests).
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

from app.services.coding_task import envelope
from app.services.coding_task.validator_runner import run_ast_syntax
from app.services.project_autonomy.orchestrator import run_validation


# ── run_ast_syntax scoping ───────────────────────────────────────────────


def _write(tmp_path: Path, rel: str, body: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def test_ast_syntax_accepts_changed_files_and_scopes_to_them(tmp_path):
    _write(tmp_path, "good.py", "x = 1\n")
    _write(tmp_path, "bad.py", "def broken(:\n")

    ok = run_ast_syntax(tmp_path, changed_files=["good.py"])
    assert ok.exit_code == 0
    assert "good.py" in ok.stdout
    assert "bad.py" not in ok.stdout  # scoped: untouched files not parsed

    bad = run_ast_syntax(tmp_path, changed_files=["bad.py"])
    assert bad.exit_code == 1
    assert "SyntaxError" in bad.stdout


def test_ast_syntax_changed_files_skips_deleted_and_non_python(tmp_path):
    _write(tmp_path, "kept.py", "x = 1\n")
    result = run_ast_syntax(
        tmp_path,
        changed_files=["kept.py", "deleted_in_diff.py", "notes.md"],
    )
    assert result.exit_code == 0
    assert "kept.py" in result.stdout


def test_ast_syntax_changed_files_never_escapes_worktree(tmp_path):
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("x = (", encoding="utf-8")
    try:
        result = run_ast_syntax(tmp_path, changed_files=["../outside_secret.py"])
        assert result.exit_code == 0
        assert "outside_secret" not in result.stdout
    finally:
        outside.unlink(missing_ok=True)


def test_ast_syntax_legacy_no_changed_files_still_walks(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    result = run_ast_syntax(tmp_path)
    assert result.exit_code == 0
    assert "a.py" in result.stdout


def test_changed_typescript_syntax_uses_node_and_records_exact_coverage(tmp_path):
    if not shutil.which("node"):
        return
    _write(tmp_path, "src/good.ts", "export const value: number = 1;\n")
    _write(tmp_path, "src/bad.ts", "export const value: = 1;\n")

    good = run_ast_syntax(tmp_path, changed_files=["src/good.ts"])
    bad = run_ast_syntax(tmp_path, changed_files=["src/bad.ts"])

    assert good.exit_code == 0, good.stdout + good.stderr
    assert good.metadata["changed_files"] == ["src/good.ts"]
    assert good.metadata["syntax_languages"] == ["typescript"]
    assert bad.exit_code != 0
    assert "SyntaxError" in bad.stdout


def test_changed_typescript_syntax_never_executes_repository_code(tmp_path):
    if not shutil.which("node"):
        return
    _write(tmp_path, "src/side_effect.ts", 'throw new Error("must not execute");\n')

    result = run_ast_syntax(tmp_path, changed_files=["src/side_effect.ts"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.metadata["changed_files"] == ["src/side_effect.ts"]


def test_changed_javascript_rejects_unbound_instanceof_class_reference(tmp_path):
    if not shutil.which("node"):
        return
    _write(
        tmp_path,
        "src/client.mjs",
        "export function classify(error) { return error instanceof ClientInputError; }\n",
    )

    result = run_ast_syntax(tmp_path, changed_files=["src/client.mjs"])

    assert result.exit_code == 1
    assert "unbound instanceof identifier(s): ClientInputError" in result.stdout


def test_changed_javascript_accepts_imported_instanceof_class_reference(tmp_path):
    if not shutil.which("node"):
        return
    _write(tmp_path, "src/errors.mjs", "export class ClientInputError extends Error {}\n")
    _write(
        tmp_path,
        "src/client.mjs",
        "import { ClientInputError } from './errors.mjs';\n"
        "export function classify(error) { return error instanceof ClientInputError; }\n",
    )

    result = run_ast_syntax(tmp_path, changed_files=["src/client.mjs"])

    assert result.exit_code == 0, result.stdout + result.stderr


def test_changed_dart_syntax_uses_analyzer_when_available(tmp_path):
    if not shutil.which("dart"):
        return
    _write(tmp_path, "lib/good.dart", "int add(int left, int right) => left + right;\n")
    _write(tmp_path, "lib/bad.dart", "int add( => 1;\n")

    good = run_ast_syntax(tmp_path, changed_files=["lib/good.dart"])
    bad = run_ast_syntax(tmp_path, changed_files=["lib/bad.dart"])

    assert good.exit_code == 0, good.stdout + good.stderr
    assert good.metadata["changed_files"] == ["lib/good.dart"]
    assert good.metadata["syntax_languages"] == ["dart"]
    assert bad.exit_code != 0


def test_changed_sql_materializes_files_in_supplied_order(tmp_path):
    _write(
        tmp_path,
        "schema/20_children.sql",
        """
        CREATE TABLE children (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL REFERENCES parents(id)
        );
        INSERT INTO children (id, parent_id) VALUES (10, 1);
        """,
    )
    _write(
        tmp_path,
        "schema/10_parents.sql",
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE parents (id INTEGER PRIMARY KEY);
        INSERT INTO parents (id) VALUES (1);
        """,
    )

    result = run_ast_syntax(
        tmp_path,
        changed_files=["schema/10_parents.sql", "schema/20_children.sql"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.metadata["changed_files"] == [
        "schema/10_parents.sql",
        "schema/20_children.sql",
    ]
    assert result.metadata["syntax_languages"] == ["sql"]
    assert result.stdout.index("ok schema/10_parents.sql") < result.stdout.index(
        "ok schema/20_children.sql"
    )


def test_changed_sql_reports_invalid_script_without_external_database(tmp_path):
    _write(tmp_path, "schema/broken.sql", "CREATE TABLE broken (id INTEGER PRIMARY KEY,);\n")

    result = run_ast_syntax(tmp_path, changed_files=["schema/broken.sql"])

    assert result.exit_code == 1
    assert "SyntaxError schema/broken.sql: SQLite validation failed" in result.stdout
    assert result.metadata["changed_files"] == ["schema/broken.sql"]
    assert result.metadata["syntax_languages"] == ["sql"]


def test_changed_sql_defers_missing_application_schema(tmp_path):
    _write(
        tmp_path,
        "queries/customer.sql",
        "SELECT display_name FROM customer WHERE customer_id = 1;\n",
    )
    _write(
        tmp_path,
        "queries/order.sql",
        "UPDATE export_order SET status = 'ready' WHERE order_id = 2;\n",
    )

    result = run_ast_syntax(
        tmp_path,
        changed_files=["queries/customer.sql", "queries/order.sql"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.metadata["changed_files"] == [
        "queries/customer.sql",
        "queries/order.sql",
    ]
    assert result.metadata["sql_schema_dependent"] == [
        {"path": "queries/customer.sql", "error": "no such table: customer"},
        {"path": "queries/order.sql", "error": "no such table: export_order"},
    ]
    assert "schema-dependent references deferred" in result.stdout


def test_changed_sql_still_checks_statements_after_missing_schema(tmp_path):
    _write(
        tmp_path,
        "queries/broken.sql",
        "SELECT * FROM customer;\nSELECT FROM definitely_broken;\n",
    )

    result = run_ast_syntax(tmp_path, changed_files=["queries/broken.sql"])

    assert result.exit_code == 1
    assert "near \"FROM\": syntax error" in result.stdout
    assert result.metadata["sql_schema_dependent"] == [
        {"path": "queries/broken.sql", "error": "no such table: customer"}
    ]


def test_changed_sql_rejects_non_sqlite_function_instead_of_deferring_it(tmp_path):
    _write(
        tmp_path,
        "queries/unsupported.sql",
        "SELECT json_contains('[\"reader\"]', 'reader');\n",
    )

    result = run_ast_syntax(tmp_path, changed_files=["queries/unsupported.sql"])

    assert result.exit_code == 1
    assert "no such function: json_contains" in result.stdout
    assert result.metadata["sql_schema_dependent"] == []


def test_changed_sql_keeps_external_database_actions_blocked(tmp_path):
    _write(
        tmp_path,
        "queries/unsafe.sql",
        "ATTACH DATABASE 'outside.sqlite3' AS outside;\n",
    )

    result = run_ast_syntax(tmp_path, changed_files=["queries/unsafe.sql"])

    assert result.exit_code == 1
    assert "not authorized" in result.stdout.lower()


# ── run_validation end-to-end (the TypeError regression) ────────────────


def test_run_validation_does_not_crash_with_changed_files(tmp_path):
    """The exact call shape that crashed every autopilot run. Steps may
    skip (ruff/mypy/pytest availability varies) but the call must complete
    and ast_syntax must reflect the changed file."""
    _write(tmp_path, "feature.py", "value = 42\n")
    results = run_validation(tmp_path, ["feature.py"])
    by_key = {r["step_key"]: r for r in results}
    assert "ast_syntax" in by_key
    assert by_key["ast_syntax"]["exit_code"] == 0
    assert "pytest_targeted" in by_key
    # Honesty marker is surfaced into the orchestrator payload.
    assert "tests_executed" in by_key["pytest_targeted"]
    assert "test_files" in by_key["pytest_targeted"]
    assert "validation_scope" in by_key["pytest_targeted"]


# ── TEST_DATABASE_URL passthrough (fail-closed) ──────────────────────────


def test_safe_env_passes_test_db_url_only_when_test_suffixed(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@localhost:5433/chili_test")
    env = envelope.subprocess_safe_env()
    assert env.get("TEST_DATABASE_URL") == "postgresql://u:p@localhost:5433/chili_test"


def test_safe_env_blocks_non_test_database(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@localhost:5433/chili")
    env = envelope.subprocess_safe_env()
    assert "TEST_DATABASE_URL" not in env


def test_safe_env_blocks_non_postgres_and_query_string_tricks(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "sqlite:///anything_test")
    assert "TEST_DATABASE_URL" not in envelope.subprocess_safe_env()
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@h/chili?x=_test")
    assert "TEST_DATABASE_URL" not in envelope.subprocess_safe_env()


def test_safe_env_without_test_db_url(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    env = envelope.subprocess_safe_env()
    assert "TEST_DATABASE_URL" not in env


# ── dispatch lane: scoped phase-1 validation ─────────────────────────────


def test_phase1_pytest_collect_scoped_skips_when_no_related_tests(tmp_path):
    """Repo-wide collect fails on PRE-EXISTING base breakage (live: every
    dispatch run failed on an unrelated trading check in local main). With
    changed_files and no related tests, the step honestly skips."""
    from app.services.coding_task.validator_runner import run_phase1_validation

    _write(tmp_path, "app/services/code_dispatch/scorer.py", "x = 1\n")
    # A pre-existing broken test that repo-wide collect would die on:
    _write(tmp_path, "tests/test_unrelated_broken.py", "raise RuntimeError('baseline breakage')\n")

    results = run_phase1_validation(
        tmp_path, changed_files=["app/services/code_dispatch/scorer.py"]
    )
    by_key = {r.step_key: r for r in results}
    pc = by_key["pytest_collect"]
    assert pc.exit_code == 0
    assert pc.skipped is True
    assert "baseline" in pc.stdout


def test_phase1_pytest_collect_targets_related_tests(tmp_path):
    from app.services.coding_task.validator_runner import run_phase1_validation

    _write(tmp_path, "app/scorer.py", "x = 1\n")
    _write(tmp_path, "tests/test_scorer.py", "def test_ok():\n    assert True\n")
    _write(tmp_path, "tests/test_unrelated_broken.py", "raise RuntimeError('baseline breakage')\n")

    results = run_phase1_validation(tmp_path, changed_files=["app/scorer.py"])
    by_key = {r.step_key: r for r in results}
    pc = by_key["pytest_collect"]
    assert pc.exit_code == 0, pc.stdout + pc.stderr
    assert pc.skipped is False
    assert "test_scorer" in pc.stdout


def test_phase1_without_changed_files_keeps_legacy_repo_wide(tmp_path):
    from app.services.coding_task.validator_runner import run_phase1_validation

    _write(tmp_path, "a.py", "x = 1\n")
    results = run_phase1_validation(tmp_path)
    by_key = {r.step_key: r for r in results}
    assert by_key["pytest_collect"].step_key == "pytest_collect"
