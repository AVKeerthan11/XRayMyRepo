-- CIM v1: completed snapshots are immutable down to their rows.
--
-- 0001 guards the snapshot row itself (snapshot_guard_immutable). This extends the
-- same rule to the rows a snapshot owns: once the snapshot has left 'pending',
-- no row of node, edge, classification, unresolved_reference, extraction_issue
-- or snapshot_producer belonging to it can be inserted, updated or deleted.
--
-- The checks are statement-level, over transition tables, so a bulk load (COPY)
-- costs one join per statement rather than one lookup per row. Deleting a whole
-- snapshot still cascades: by the time the cascaded child deletes run, the
-- snapshot row is gone, so there is nothing left to protect.

BEGIN;

CREATE FUNCTION snapshot_child_guard_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    frozen_id      bigint;
    frozen_status  text;
BEGIN
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT s.id, s.status INTO frozen_id, frozen_status
        FROM new_rows r JOIN snapshot s ON s.id = r.snapshot_id
        WHERE s.status <> 'pending' LIMIT 1;
    END IF;
    IF frozen_id IS NULL AND TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT s.id, s.status INTO frozen_id, frozen_status
        FROM old_rows r JOIN snapshot s ON s.id = r.snapshot_id
        WHERE s.status <> 'pending' LIMIT 1;
    END IF;
    IF frozen_id IS NOT NULL THEN
        RAISE EXCEPTION 'snapshot % is %: its % rows are immutable', frozen_id, frozen_status, TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END;
$$;

DO $$
DECLARE
    child text;
BEGIN
    FOREACH child IN ARRAY ARRAY[
        'snapshot_producer', 'node', 'edge', 'classification', 'unresolved_reference',
        'extraction_issue']
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_insert AFTER INSERT ON %1$I '
            'REFERENCING NEW TABLE AS new_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION snapshot_child_guard_immutable()', child);
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_update AFTER UPDATE ON %1$I '
            'REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION snapshot_child_guard_immutable()', child);
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_delete AFTER DELETE ON %1$I '
            'REFERENCING OLD TABLE AS old_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION snapshot_child_guard_immutable()', child);
    END LOOP;
END;
$$;

INSERT INTO schema_migration (version) VALUES ('0002_snapshot_children_immutable');

COMMIT;
