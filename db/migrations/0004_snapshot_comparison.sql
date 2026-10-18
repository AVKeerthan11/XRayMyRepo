-- CIM v1: snapshot comparison (what changed between two snapshots of one repository).
--
-- Contract: src/xraymyrepo/cim/comparison.py, docs/cim/CIM-v1.md §8 and
-- docs/comparison/comparison-v1.md. New tables only; 0001-0003 are unchanged.
--
-- A comparison is an immutable computation over two different complete snapshots of one
-- repository, identified by (from, to, algorithm, algorithm_version, params_hash), like a
-- derivation run. Its rows reference snapshot nodes and edges through
-- (snapshot_id, node_id) / (snapshot_id, edge_id), with each snapshot_id tied to the
-- comparison's own from/to snapshot. Those references are checked at commit (DEFERRABLE
-- INITIALLY DEFERRED): deleting either snapshot deletes its nodes and edges and, through
-- the comparison, the rows citing them, and the second cascade must run before the
-- references are checked.
--
-- Every node of both snapshots has a node_change row (unchanged included). Only changed
-- or undetermined edges have an edge_change row; unchanged edges are counted in the
-- summary. Completing a comparison checks the rows against both snapshots and the summary.
-- Closed vocabularies are text + CHECK (tests/test_schema_consistency.py keeps them equal
-- to the Python enums).

BEGIN;

-- The shape of a comparison summary. NULL-safe (coalesce): a CHECK that evaluates to NULL
-- passes, so a missing key must make this false, not NULL.
CREATE FUNCTION snapshot_comparison_summary_valid(summary jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT coalesce(
        jsonb_typeof(summary) = 'object'
        AND summary - ARRAY['nodes', 'edges'] = '{}'
        AND jsonb_typeof(summary -> 'nodes') = 'object'
        AND jsonb_typeof(summary -> 'edges') = 'object'
        AND summary -> 'nodes' ?& ARRAY['added', 'removed', 'modified', 'moved', 'unchanged', 'undetermined']
        AND (summary -> 'nodes') - ARRAY['added', 'removed', 'modified', 'moved', 'unchanged', 'undetermined'] = '{}'
        AND summary -> 'edges' ?& ARRAY['added', 'removed', 'modified', 'unchanged', 'undetermined']
        AND (summary -> 'edges') - ARRAY['added', 'removed', 'modified', 'unchanged', 'undetermined'] = '{}'
        AND NOT jsonb_path_exists(summary, '$.*.* ? (@.type() != "number" || @ < 0 || @ != @.floor())'),
        false)
$$;

CREATE TABLE snapshot_comparison (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_snapshot_id   bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    to_snapshot_id     bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    algorithm          text NOT NULL,
    algorithm_version  text NOT NULL,
    params_hash        text NOT NULL,
    params             jsonb NOT NULL,
    incomparable       jsonb NOT NULL DEFAULT '[]',
    status             text NOT NULL DEFAULT 'pending',
    failure_reason     text,
    summary            jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    CONSTRAINT snapshot_comparison_identity_key
        UNIQUE (from_snapshot_id, to_snapshot_id, algorithm, algorithm_version, params_hash),
    CONSTRAINT snapshot_comparison_id_snapshots_key UNIQUE (id, from_snapshot_id, to_snapshot_id),
    CONSTRAINT snapshot_comparison_distinct_check CHECK (from_snapshot_id <> to_snapshot_id),
    CONSTRAINT snapshot_comparison_algorithm_check CHECK (algorithm ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT snapshot_comparison_algorithm_version_check CHECK (algorithm_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)([-+].+)?$'),
    CONSTRAINT snapshot_comparison_params_hash_check CHECK (params_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT snapshot_comparison_params_check CHECK (jsonb_typeof(params) = 'object'),
    -- In enum order; the exact value is checked against the two identities on insert.
    CONSTRAINT snapshot_comparison_incomparable_check CHECK (incomparable IN (
        '[]', '["extractor_version_differs"]', '["config_differs"]',
        '["extractor_version_differs", "config_differs"]')),
    CONSTRAINT snapshot_comparison_status_check CHECK (status IN ('pending', 'complete', 'failed')),
    CONSTRAINT snapshot_comparison_failure_reason_check CHECK ((status = 'failed') = (failure_reason IS NOT NULL)),
    CONSTRAINT snapshot_comparison_finished_at_check CHECK ((status = 'pending') = (finished_at IS NULL)),
    -- {"nodes": {<every node change kind>: n}, "edges": {<every edge change kind>: n}},
    -- non-negative integers; required exactly when complete.
    CONSTRAINT snapshot_comparison_summary_required_check CHECK ((status = 'complete') = (summary IS NOT NULL)),
    CONSTRAINT snapshot_comparison_summary_check CHECK (
        summary IS NULL OR snapshot_comparison_summary_valid(summary))
);
-- snapshot_comparison_identity_key leads with from_snapshot_id.
CREATE INDEX snapshot_comparison_to_snapshot_idx ON snapshot_comparison (to_snapshot_id);

-- Comparisons are computed from two complete snapshots of one repository, and
-- `incomparable` says exactly how their identities differ.
CREATE FUNCTION snapshot_comparison_require_snapshots() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    a  snapshot%ROWTYPE;
    b  snapshot%ROWTYPE;
    expected  jsonb := '[]';
BEGIN
    SELECT * INTO a FROM snapshot WHERE id = NEW.from_snapshot_id;
    SELECT * INTO b FROM snapshot WHERE id = NEW.to_snapshot_id;
    IF a.id IS NULL OR b.id IS NULL OR a.status <> 'complete' OR b.status <> 'complete' THEN
        RAISE EXCEPTION 'snapshots % and % must both exist and be complete to be compared',
            NEW.from_snapshot_id, NEW.to_snapshot_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF a.repository_id <> b.repository_id THEN
        RAISE EXCEPTION 'snapshots % and % belong to different repositories',
            NEW.from_snapshot_id, NEW.to_snapshot_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF a.extractor_version <> b.extractor_version THEN
        expected := expected || '"extractor_version_differs"';
    END IF;
    IF a.config_hash <> b.config_hash THEN
        expected := expected || '"config_differs"';
    END IF;
    IF NEW.incomparable <> expected THEN
        RAISE EXCEPTION 'incomparable must be %, how the identities of snapshots % and % differ',
            expected, NEW.from_snapshot_id, NEW.to_snapshot_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER snapshot_comparison_require_snapshots
    BEFORE INSERT ON snapshot_comparison
    FOR EACH ROW EXECUTE FUNCTION snapshot_comparison_require_snapshots();

-- Like derivation_run_guard_immutable: identity, params and comparability never change;
-- the only update is the one-way transition pending -> complete | failed. Completing
-- checks the rows (see snapshot_comparison_check_complete).
CREATE FUNCTION snapshot_comparison_guard_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.from_snapshot_id      IS DISTINCT FROM OLD.from_snapshot_id
       OR NEW.to_snapshot_id     IS DISTINCT FROM OLD.to_snapshot_id
       OR NEW.algorithm          IS DISTINCT FROM OLD.algorithm
       OR NEW.algorithm_version  IS DISTINCT FROM OLD.algorithm_version
       OR NEW.params_hash        IS DISTINCT FROM OLD.params_hash
       OR NEW.params             IS DISTINCT FROM OLD.params
       OR NEW.incomparable       IS DISTINCT FROM OLD.incomparable
       OR NEW.created_at         IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'snapshot comparison % is immutable: identity, params and comparability cannot change', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'snapshot comparison % is %, only pending comparisons can transition', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'complete' THEN
        PERFORM snapshot_comparison_check_complete(NEW);
    END IF;
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- One row per node of either snapshot. `key` is the node's key in `to`, else in `from`
-- (unique per comparison: a key present in both snapshots is one row).

CREATE TABLE node_change (
    comparison_id     bigint NOT NULL,
    from_snapshot_id  bigint NOT NULL,
    to_snapshot_id    bigint NOT NULL,
    key               text COLLATE "C" NOT NULL,
    change            text NOT NULL,
    kind              text NOT NULL,
    from_node_id      bigint,
    to_node_id        bigint,
    match_method      text,
    confidence        text NOT NULL,
    move_ambiguous    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (comparison_id, key),
    CONSTRAINT node_change_from_node_key UNIQUE (comparison_id, from_node_id),
    CONSTRAINT node_change_to_node_key UNIQUE (comparison_id, to_node_id),
    CONSTRAINT node_change_comparison_fkey FOREIGN KEY (comparison_id, from_snapshot_id, to_snapshot_id)
        REFERENCES snapshot_comparison (id, from_snapshot_id, to_snapshot_id) ON DELETE CASCADE,
    CONSTRAINT node_change_from_node_fkey FOREIGN KEY (from_snapshot_id, from_node_id)
        REFERENCES node (snapshot_id, id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT node_change_to_node_fkey FOREIGN KEY (to_snapshot_id, to_node_id)
        REFERENCES node (snapshot_id, id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT node_change_change_check CHECK (change IN (
        'added', 'removed', 'modified', 'moved', 'unchanged', 'undetermined')),
    CONSTRAINT node_change_kind_check CHECK (kind IN (
        'repository', 'directory', 'file', 'type', 'function', 'endpoint', 'package', 'external_package')),
    CONSTRAINT node_change_match_method_check CHECK (match_method IN ('same_key', 'git_rename', 'same_content_hash')),
    CONSTRAINT node_change_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT node_change_key_check CHECK (key <> '' AND key !~ '[\x01-\x1f\x7f]'),
    -- Which sides and which match each change has (cim NodeChange).
    CONSTRAINT node_change_shape_check CHECK (
        (change = 'added' AND from_node_id IS NULL AND to_node_id IS NOT NULL AND match_method IS NULL)
        OR (change = 'removed' AND from_node_id IS NOT NULL AND to_node_id IS NULL AND match_method IS NULL)
        OR (change IN ('unchanged', 'modified') AND from_node_id IS NOT NULL AND to_node_id IS NOT NULL
            AND match_method IS NOT DISTINCT FROM 'same_key')
        OR (change = 'moved' AND from_node_id IS NOT NULL AND to_node_id IS NOT NULL
            AND match_method IS NOT DISTINCT FROM 'same_content_hash')
        OR (change = 'undetermined' AND (
            (from_node_id IS NOT NULL AND to_node_id IS NOT NULL AND match_method IS NOT DISTINCT FROM 'same_key')
            OR (num_nonnulls(from_node_id, to_node_id) = 1 AND match_method IS NULL)))),
    CONSTRAINT node_change_change_confidence_check CHECK (
        CASE change
            WHEN 'moved' THEN confidence = 'medium'
            WHEN 'undetermined' THEN confidence = 'low'
            ELSE confidence <> 'low'
        END),
    CONSTRAINT node_change_move_ambiguous_check CHECK (
        NOT move_ambiguous OR (kind = 'file' AND change IN ('added', 'removed')))
);
CREATE INDEX node_change_change_idx ON node_change (comparison_id, change, key);
CREATE INDEX node_change_kind_idx ON node_change (comparison_id, kind, key);

-- ---------------------------------------------------------------------------
-- Changed and undetermined edges only (unchanged edges are counted in the summary).
-- source_key / target_key are the edge's ends in `to`, else in `from`, for ordering and
-- display; completing checks them against the referenced edges.

CREATE TABLE edge_change (
    comparison_id     bigint NOT NULL,
    id                bigint GENERATED ALWAYS AS IDENTITY,
    from_snapshot_id  bigint NOT NULL,
    to_snapshot_id    bigint NOT NULL,
    change            text NOT NULL,
    kind              text NOT NULL,
    from_edge_id      bigint,
    to_edge_id        bigint,
    source_key        text COLLATE "C" NOT NULL,
    target_key        text COLLATE "C" NOT NULL,
    confidence        text NOT NULL,
    PRIMARY KEY (comparison_id, id),
    CONSTRAINT edge_change_from_edge_key UNIQUE (comparison_id, from_edge_id),
    CONSTRAINT edge_change_to_edge_key UNIQUE (comparison_id, to_edge_id),
    CONSTRAINT edge_change_comparison_fkey FOREIGN KEY (comparison_id, from_snapshot_id, to_snapshot_id)
        REFERENCES snapshot_comparison (id, from_snapshot_id, to_snapshot_id) ON DELETE CASCADE,
    CONSTRAINT edge_change_from_edge_fkey FOREIGN KEY (from_snapshot_id, from_edge_id)
        REFERENCES edge (snapshot_id, id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT edge_change_to_edge_fkey FOREIGN KEY (to_snapshot_id, to_edge_id)
        REFERENCES edge (snapshot_id, id) DEFERRABLE INITIALLY DEFERRED,
    -- 'unchanged' is deliberately absent: unchanged edges are not rows.
    CONSTRAINT edge_change_change_check CHECK (change IN ('added', 'removed', 'modified', 'undetermined')),
    CONSTRAINT edge_change_kind_check CHECK (kind IN (
        'IMPORTS', 'REQUIRES', 'INHERITS', 'HANDLES', 'TESTS', 'CALLS')),
    CONSTRAINT edge_change_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT edge_change_keys_check CHECK (
        source_key <> '' AND target_key <> '' AND source_key !~ '[\x01-\x1f\x7f]'
        AND target_key !~ '[\x01-\x1f\x7f]'),
    CONSTRAINT edge_change_shape_check CHECK (
        (change = 'added' AND from_edge_id IS NULL AND to_edge_id IS NOT NULL)
        OR (change = 'removed' AND from_edge_id IS NOT NULL AND to_edge_id IS NULL)
        OR (change = 'modified' AND from_edge_id IS NOT NULL AND to_edge_id IS NOT NULL)
        OR (change = 'undetermined' AND num_nonnulls(from_edge_id, to_edge_id) = 1)),
    CONSTRAINT edge_change_change_confidence_check CHECK (
        (change = 'undetermined') = (confidence = 'low'))
);
CREATE INDEX edge_change_change_idx ON edge_change (comparison_id, change, kind, source_key, target_key);

-- ---------------------------------------------------------------------------
-- Completing a comparison: what the rows must satisfy against both snapshots and the
-- summary. Runs once, in the pending -> complete update.

CREATE FUNCTION snapshot_comparison_check_complete(c snapshot_comparison) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    problem  text;
    unchanged_edges  jsonb := c.summary -> 'edges' -> 'unchanged';
BEGIN
    -- A malformed summary is left to snapshot_comparison_summary_check, which runs after
    -- this trigger and reports it as such.
    IF c.summary IS NULL OR NOT snapshot_comparison_summary_valid(c.summary) THEN
        RETURN;
    END IF;
    -- Every node of each snapshot is in exactly one row (rows are unique per side).
    IF (SELECT count(*) FROM node WHERE snapshot_id = c.from_snapshot_id)
       <> (SELECT count(*) FROM node_change WHERE comparison_id = c.id AND from_node_id IS NOT NULL)
       OR (SELECT count(*) FROM node WHERE snapshot_id = c.to_snapshot_id)
       <> (SELECT count(*) FROM node_change WHERE comparison_id = c.id AND to_node_id IS NOT NULL) THEN
        problem := 'node changes do not cover both snapshots';
    -- A row's key and kind are its node's (in to, else from); same_key joins equal keys,
    -- same_content_hash different ones, and a moved file keeps its content.
    ELSIF EXISTS (
        SELECT 1 FROM node_change r
        LEFT JOIN node f ON f.snapshot_id = r.from_snapshot_id AND f.id = r.from_node_id
        LEFT JOIN node t ON t.snapshot_id = r.to_snapshot_id AND t.id = r.to_node_id
        WHERE r.comparison_id = c.id AND (
            r.key IS DISTINCT FROM coalesce(t.key, f.key)
            OR r.kind IS DISTINCT FROM coalesce(t.kind, f.kind)
            OR (r.match_method = 'same_key' AND f.key IS DISTINCT FROM t.key)
            OR (r.match_method = 'same_content_hash' AND f.key = t.key)
            OR (r.change = 'moved' AND r.kind = 'file' AND f.blob_sha IS DISTINCT FROM t.blob_sha))) THEN
        problem := 'a node change does not match its nodes';
    -- An edge row's kind and ends are its edge's (in to, else from); a modified row's two
    -- edges have its kind.
    ELSIF EXISTS (
        SELECT 1 FROM edge_change r
        JOIN edge e ON e.snapshot_id = CASE WHEN r.to_edge_id IS NOT NULL
                                            THEN r.to_snapshot_id ELSE r.from_snapshot_id END
                   AND e.id = coalesce(r.to_edge_id, r.from_edge_id)
        JOIN node s ON s.snapshot_id = e.snapshot_id AND s.id = e.source_id
        JOIN node t ON t.snapshot_id = e.snapshot_id AND t.id = e.target_id
        LEFT JOIN edge fe ON fe.snapshot_id = r.from_snapshot_id AND fe.id = r.from_edge_id
        WHERE r.comparison_id = c.id
          AND (r.kind <> e.kind OR r.source_key <> s.key OR r.target_key <> t.key
               OR fe.kind <> r.kind)) THEN
        problem := 'an edge change does not match its edges';
    -- Not fully comparable: no change has high confidence.
    ELSIF c.incomparable <> '[]' AND (
        EXISTS (SELECT 1 FROM node_change WHERE comparison_id = c.id AND confidence = 'high')
        OR EXISTS (SELECT 1 FROM edge_change WHERE comparison_id = c.id AND confidence = 'high')) THEN
        problem := 'the snapshots are not fully comparable, so no change has high confidence';
    -- The summary counts the rows ...
    ELSIF EXISTS (
        SELECT 1 FROM jsonb_each(c.summary -> 'nodes') s (change, n)
        WHERE s.n <> to_jsonb((SELECT count(*) FROM node_change
                               WHERE comparison_id = c.id AND change = s.change)))
       OR EXISTS (
        SELECT 1 FROM jsonb_each(c.summary -> 'edges') s (change, n)
        WHERE s.change <> 'unchanged'
          AND s.n <> to_jsonb((SELECT count(*) FROM edge_change
                               WHERE comparison_id = c.id AND change = s.change))) THEN
        problem := 'the summary does not count the rows';
    -- ... and every edge of each snapshot is a row or one of the unchanged edges.
    ELSIF to_jsonb((SELECT count(*) FROM edge WHERE snapshot_id = c.from_snapshot_id)
                   - (SELECT count(*) FROM edge_change WHERE comparison_id = c.id AND from_edge_id IS NOT NULL))
          <> unchanged_edges
       OR to_jsonb((SELECT count(*) FROM edge WHERE snapshot_id = c.to_snapshot_id)
                   - (SELECT count(*) FROM edge_change WHERE comparison_id = c.id AND to_edge_id IS NOT NULL))
          <> unchanged_edges THEN
        problem := 'edge changes and unchanged edges do not account for every edge';
    END IF;
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'snapshot comparison % cannot complete: %', c.id, problem
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
END;
$$;

CREATE TRIGGER snapshot_comparison_guard_immutable
    BEFORE UPDATE ON snapshot_comparison
    FOR EACH ROW EXECUTE FUNCTION snapshot_comparison_guard_immutable();

-- ---------------------------------------------------------------------------
-- Rows of a finished comparison are immutable (the 0002/0003 pattern, keyed on the
-- comparison). Deleting a snapshot still cascades: by the time the cascaded row deletes
-- run, the comparison row is gone.

CREATE FUNCTION comparison_child_guard_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    frozen_id      bigint;
    frozen_status  text;
BEGIN
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT c.id, c.status INTO frozen_id, frozen_status
        FROM new_rows n JOIN snapshot_comparison c ON c.id = n.comparison_id
        WHERE c.status <> 'pending' LIMIT 1;
    END IF;
    IF frozen_id IS NULL AND TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT c.id, c.status INTO frozen_id, frozen_status
        FROM old_rows o JOIN snapshot_comparison c ON c.id = o.comparison_id
        WHERE c.status <> 'pending' LIMIT 1;
    END IF;
    IF frozen_id IS NOT NULL THEN
        RAISE EXCEPTION 'snapshot comparison % is %: its % rows are immutable', frozen_id, frozen_status, TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END;
$$;

DO $$
DECLARE
    child text;
BEGIN
    FOREACH child IN ARRAY ARRAY['node_change', 'edge_change']
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_insert AFTER INSERT ON %1$I '
            'REFERENCING NEW TABLE AS new_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION comparison_child_guard_immutable()', child);
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_update AFTER UPDATE ON %1$I '
            'REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION comparison_child_guard_immutable()', child);
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_delete AFTER DELETE ON %1$I '
            'REFERENCING OLD TABLE AS old_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION comparison_child_guard_immutable()', child);
    END LOOP;
END;
$$;

INSERT INTO schema_migration (version) VALUES ('0004_snapshot_comparison');

COMMIT;
