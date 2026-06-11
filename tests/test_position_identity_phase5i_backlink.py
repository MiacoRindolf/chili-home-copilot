"""Position-identity Phase 5I -- envelope->position first-fill backlink.

Mig 257's Phase 5A envelope-insert trigger links ``position_id`` only when a
matching ``trading_positions`` row already exists. The first fill ever in a
natural key races the broker position observer (envelope insert -> position
insert minutes later) and the envelope stays unlinked forever, tripping the
Phase 5B view's ``broker_envelope_missing_position`` hard linkage status
(live incident 2026-06-05: BFLY/IYH envelopes 2285/2286, positions 332/331).
Mig 304 installs the inverse trigger on ``trading_positions`` plus an
idempotent backfill.
"""
from __future__ import annotations

import inspect

from app import migrations


def test_phase5i_backlink_migration_registered_after_303():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]
    assert "303_momentum_nbbo_spread_tape" in ids
    assert "304_position_identity_phase5i_position_insert_backlink" in ids
    assert ids.index("304_position_identity_phase5i_position_insert_backlink") == (
        ids.index("303_momentum_nbbo_spread_tape") + 1
    )


def test_phase5i_backlink_trigger_covers_position_insert_side():
    src = inspect.getsource(
        migrations._migration_304_position_identity_phase5i_position_insert_backlink
    )
    assert "CREATE TRIGGER trg_trading_positions_phase5i_backlink_after_insert" in src
    assert "AFTER INSERT ON trading_positions" in src
    assert "SET position_id = NEW.id" in src
    # The position's own pointer is only filled on an unambiguous single match.
    assert "current_envelope_id" in src
    assert "v_open_count = 1" in src


def test_phase5i_backlink_trigger_never_breaks_position_inserts():
    src = inspect.getsource(
        migrations._migration_304_position_identity_phase5i_position_insert_backlink
    )
    assert "EXCEPTION WHEN others THEN" in src
    assert "RAISE WARNING" in src
    assert "RETURN NEW" in src


def test_phase5i_backlink_backfill_is_idempotent_and_guarded():
    src = inspect.getsource(
        migrations._migration_304_position_identity_phase5i_position_insert_backlink
    )
    # NULL-guarded updates re-run safely.
    assert "e.position_id IS NULL" in src
    assert "p.current_envelope_id IS NULL" in src
    # Ambiguity guards: never guess between multiple candidates.
    assert "HAVING COUNT(DISTINCT p.id) = 1" in src
    assert "HAVING COUNT(e.id) = 1" in src
    # Only the OPEN hard-issue class; closed rows stay historical debt.
    assert "e.status = 'open'" in src


def test_phase5i_backlink_excludes_option_envelopes():
    src = inspect.getsource(
        migrations._migration_304_position_identity_phase5i_position_insert_backlink
    )
    assert "NOT IN ('option', 'options')" in src
