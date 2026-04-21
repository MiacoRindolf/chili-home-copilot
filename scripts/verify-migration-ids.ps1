# Verify that no two entries in app.migrations.MIGRATIONS share a version_id
# and that no entry collides with RETIRED_MIGRATIONS.
#
# Intended for CI / precommit / manual checks. Exits non-zero on collision.
# Run from repo root:
#     .\scripts\verify-migration-ids.ps1
#
# The same assertion runs at app startup inside run_migrations (see
# app/migrations.py::_assert_migration_ids_unique), so this script is a
# belt-and-braces guard for catching accidental collisions before merge.

$ErrorActionPreference = "Stop"

Write-Host "[verify-migration-ids] running _assert_migration_ids_unique..."

# Single-line command: conda run does not support multiline -c scripts on Windows.
$cmd = "from app.migrations import _assert_migration_ids_unique, MIGRATIONS, RETIRED_MIGRATIONS; _assert_migration_ids_unique(); print('OK: ' + str(len(MIGRATIONS)) + ' migrations, ' + str(len(RETIRED_MIGRATIONS)) + ' retired; no ID collisions.')"

& conda run -n chili-env python -c $cmd
if ($LASTEXITCODE -ne 0) {
    Write-Error "[verify-migration-ids] FAILED -- migration ID collision detected. See app/migrations.py header for the contract."
    exit $LASTEXITCODE
}
Write-Host "[verify-migration-ids] PASS"
