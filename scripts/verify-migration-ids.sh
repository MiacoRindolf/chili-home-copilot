#!/usr/bin/env bash
# Verify that no two entries in app.migrations.MIGRATIONS share a version_id
# and that no entry collides with RETIRED_MIGRATIONS (via _assert_migration_ids_unique).
#
# Intended for CI / pre-commit / manual checks. Exits non-zero on collision.
# Run from repo root with PYTHONPATH/repo root as cwd and an activated env:
#     bash scripts/verify-migration-ids.sh
# Local conda (WR8):
#     conda run -n chili-env bash scripts/verify-migration-ids.sh
#
# Belt-and-brangles with run_migrations (see app/migrations.py::_assert_migration_ids_unique).

set -euo pipefail

echo "[verify-migration-ids] running _assert_migration_ids_unique..."

python -c "from app.migrations import _assert_migration_ids_unique, MIGRATIONS, RETIRED_MIGRATIONS; _assert_migration_ids_unique(); print('OK: ' + str(len(MIGRATIONS)) + ' migrations, ' + str(len(RETIRED_MIGRATIONS)) + ' retired; no ID collisions.')"

echo "[verify-migration-ids] PASS"
