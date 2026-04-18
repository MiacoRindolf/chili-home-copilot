-- Repair orphaned pattern FKs in the neural-network / reasoning tables.
--
-- After today's scan_patterns wipe + re-seed from the Apr 7 backup, the
-- pattern IDs changed (old pid 52 -> new pid 537 etc.) because they were
-- re-inserted with fresh autoincrement IDs. Tables that store pattern IDs
-- by value (not cascade-FK) are left pointing at non-existent rows.
--
-- Affected (measured before repair):
--   trading_hypotheses.related_pattern_id  : 139 orphans
--   trading_proposals.scan_pattern_id      : 128 orphans (13 distinct pids)
--
-- Strategy: for each orphan, look up the original pattern's NAME in the
-- Apr 7 backup (chili_recover db via dblink), then find the current live
-- pattern with the same name and rewrite the FK. 100% of the orphan IDs
-- were verified to exist in the backup, so the remap is complete.

BEGIN;

CREATE EXTENSION IF NOT EXISTS dblink;

-- -----------------------------------------------------------------------------
-- Build old_id -> new_id map from backup names to live names
-- -----------------------------------------------------------------------------
CREATE TEMP TABLE _pid_map (
    old_id integer PRIMARY KEY,
    pattern_name text NOT NULL,
    new_id integer NOT NULL
) ON COMMIT DROP;

INSERT INTO _pid_map (old_id, pattern_name, new_id)
SELECT DISTINCT ON (backup.id)
    backup.id          AS old_id,
    backup.name        AS pattern_name,
    live.id            AS new_id
FROM dblink(
    'dbname=chili_recover user=chili',
    'SELECT id, name FROM scan_patterns'
) AS backup(id integer, name text)
JOIN scan_patterns live ON live.name = backup.name
ORDER BY backup.id, live.id ASC;  -- prefer earliest live id on duplicate names

SELECT 'PATTERN ID MAP SIZE' AS msg, COUNT(*) AS rows FROM _pid_map;

-- -----------------------------------------------------------------------------
-- Pre-repair orphan counts
-- -----------------------------------------------------------------------------
SELECT 'BEFORE: trading_hypotheses orphan count' AS msg, COUNT(*) AS orphans
FROM trading_hypotheses h
WHERE h.related_pattern_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = h.related_pattern_id);

SELECT 'BEFORE: trading_proposals orphan count' AS msg, COUNT(*) AS orphans
FROM trading_proposals p
WHERE p.scan_pattern_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = p.scan_pattern_id);

-- -----------------------------------------------------------------------------
-- Repair: trading_hypotheses.related_pattern_id
-- -----------------------------------------------------------------------------
UPDATE trading_hypotheses h
SET related_pattern_id = m.new_id
FROM _pid_map m
WHERE h.related_pattern_id = m.old_id
  AND h.related_pattern_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = h.related_pattern_id);

-- -----------------------------------------------------------------------------
-- Repair: trading_proposals.scan_pattern_id
-- -----------------------------------------------------------------------------
UPDATE trading_proposals p
SET scan_pattern_id = m.new_id
FROM _pid_map m
WHERE p.scan_pattern_id = m.old_id
  AND p.scan_pattern_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = p.scan_pattern_id);

-- -----------------------------------------------------------------------------
-- Post-repair verification
-- -----------------------------------------------------------------------------
SELECT 'AFTER: trading_hypotheses orphan count' AS msg, COUNT(*) AS orphans
FROM trading_hypotheses h
WHERE h.related_pattern_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = h.related_pattern_id);

SELECT 'AFTER: trading_proposals orphan count' AS msg, COUNT(*) AS orphans
FROM trading_proposals p
WHERE p.scan_pattern_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = p.scan_pattern_id);

SELECT 'AFTER: hypotheses now correctly linked' AS msg, COUNT(*) AS count
FROM trading_hypotheses h
WHERE h.related_pattern_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = h.related_pattern_id);

SELECT 'AFTER: proposals now correctly linked' AS msg, COUNT(*) AS count
FROM trading_proposals p
WHERE p.scan_pattern_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = p.scan_pattern_id);

COMMIT;
