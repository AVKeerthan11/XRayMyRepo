-- CIM v1: derivation tier (architecture intelligence).
--
-- Contract: src/xraymyrepo/cim/derivation.py and docs/architecture/architecture-v1.md.
-- New tables only; the snapshot tier (0001, 0002) is unchanged.
--
-- A derivation run is an immutable computation over one complete snapshot,
-- identified by (snapshot, algorithm, algorithm_version, params_hash). Every row
-- below belongs to one run, keys lead with run_id (partitionable by run, like the
-- snapshot tier by snapshot), and snapshot nodes are referenced through
-- (snapshot_id, node_id) with snapshot_id tied to the run's snapshot. Those node
-- references are checked at commit (DEFERRABLE INITIALLY DEFERRED): deleting a
-- snapshot deletes its nodes and, through derivation_run, the rows citing them, and
-- the second cascade must run before the references are checked.
--
-- Evidence, sample edge refs and finding subjects are JSONB holding CIM keys, as in
-- the snapshot tier. Closed vocabularies are text + CHECK
-- (tests/test_schema_consistency.py keeps them equal to the Python enums).

BEGIN;

CREATE TABLE derivation_run (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id        bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    algorithm          text NOT NULL,
    algorithm_version  text NOT NULL,
    params_hash        text NOT NULL,
    params             jsonb NOT NULL,
    status             text NOT NULL DEFAULT 'pending',
    failure_reason     text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    CONSTRAINT derivation_run_identity_key
        UNIQUE (snapshot_id, algorithm, algorithm_version, params_hash),
    CONSTRAINT derivation_run_id_snapshot_key UNIQUE (id, snapshot_id),
    CONSTRAINT derivation_run_algorithm_check CHECK (algorithm ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT derivation_run_algorithm_version_check CHECK (algorithm_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)([-+].+)?$'),
    CONSTRAINT derivation_run_params_hash_check CHECK (params_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT derivation_run_params_check CHECK (jsonb_typeof(params) = 'object'),
    CONSTRAINT derivation_run_status_check CHECK (status IN ('pending', 'complete', 'failed')),
    CONSTRAINT derivation_run_failure_reason_check CHECK ((status = 'failed') = (failure_reason IS NOT NULL)),
    CONSTRAINT derivation_run_finished_at_check CHECK ((status = 'pending') = (finished_at IS NULL))
);
CREATE INDEX derivation_run_snapshot_idx ON derivation_run (snapshot_id);

-- Derivations are computed from complete snapshots only.
CREATE FUNCTION derivation_run_require_complete_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM snapshot WHERE id = NEW.snapshot_id AND status = 'complete') THEN
        RAISE EXCEPTION 'snapshot % is not complete; it cannot be derived from', NEW.snapshot_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER derivation_run_require_complete_snapshot
    BEFORE INSERT ON derivation_run
    FOR EACH ROW EXECUTE FUNCTION derivation_run_require_complete_snapshot();

-- Like snapshot_guard_immutable: identity and params never change; the only update
-- is the one-way transition pending -> complete | failed.
CREATE FUNCTION derivation_run_guard_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.snapshot_id          IS DISTINCT FROM OLD.snapshot_id
       OR NEW.algorithm         IS DISTINCT FROM OLD.algorithm
       OR NEW.algorithm_version IS DISTINCT FROM OLD.algorithm_version
       OR NEW.params_hash       IS DISTINCT FROM OLD.params_hash
       OR NEW.params            IS DISTINCT FROM OLD.params
       OR NEW.created_at        IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'derivation run % is immutable: identity and params cannot change', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'derivation run % is %, only pending runs can transition', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER derivation_run_guard_immutable
    BEFORE UPDATE ON derivation_run
    FOR EACH ROW EXECUTE FUNCTION derivation_run_guard_immutable();

-- ---------------------------------------------------------------------------
-- Lenses and groups. "group" is a reserved word, hence derived_group.

CREATE TABLE lens (
    run_id  bigint NOT NULL REFERENCES derivation_run (id) ON DELETE CASCADE,
    name    text NOT NULL,
    kind    text NOT NULL,
    PRIMARY KEY (run_id, name),
    CONSTRAINT lens_name_check CHECK (name ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT lens_kind_check CHECK (kind IN ('directory', 'package', 'inferred'))
);

CREATE TABLE derived_group (
    run_id      bigint NOT NULL,
    id          bigint GENERATED ALWAYS AS IDENTITY,
    key         text COLLATE "C" NOT NULL,
    lens        text NOT NULL,
    parent_id   bigint,
    depth       smallint NOT NULL,
    label       text,
    basis       text NOT NULL,
    confidence  text NOT NULL,
    producer    text NOT NULL,
    rule        text,
    evidence    jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, id),
    CONSTRAINT derived_group_key_unique UNIQUE (run_id, key),
    CONSTRAINT derived_group_lens_id_key UNIQUE (run_id, lens, id),
    CONSTRAINT derived_group_lens_fkey FOREIGN KEY (run_id, lens) REFERENCES lens (run_id, name)
        ON DELETE CASCADE,
    -- A parent is in the same lens.
    CONSTRAINT derived_group_parent_fkey FOREIGN KEY (run_id, lens, parent_id)
        REFERENCES derived_group (run_id, lens, id),
    CONSTRAINT derived_group_key_check CHECK (starts_with(key, 'group:' || lens || ':')
        AND key !~ '[\x01-\x1f\x7f]'),
    CONSTRAINT derived_group_depth_check CHECK (depth >= 0 AND (parent_id IS NULL) = (depth = 0)),
    CONSTRAINT derived_group_basis_check CHECK (basis IN ('observed', 'resolved', 'heuristic', 'inferred')),
    CONSTRAINT derived_group_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT derived_group_producer_check CHECK (producer ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT derived_group_rule_check CHECK (rule ~ '^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$'),
    CONSTRAINT derived_group_evidence_check CHECK (
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) <= 20)
);
-- derived_group_key_unique (COLLATE "C") serves key lookups and key-ordered pages.
CREATE INDEX derived_group_parent_idx ON derived_group (run_id, lens, parent_id);

-- A file is in at most one group per lens.
CREATE TABLE group_member (
    run_id       bigint NOT NULL,
    lens         text NOT NULL,
    node_id      bigint NOT NULL,
    group_id     bigint NOT NULL,
    snapshot_id  bigint NOT NULL,
    basis        text NOT NULL,
    confidence   text NOT NULL,
    PRIMARY KEY (run_id, lens, node_id),
    CONSTRAINT group_member_group_fkey FOREIGN KEY (run_id, lens, group_id)
        REFERENCES derived_group (run_id, lens, id) ON DELETE CASCADE,
    CONSTRAINT group_member_run_fkey FOREIGN KEY (run_id, snapshot_id)
        REFERENCES derivation_run (id, snapshot_id) ON DELETE CASCADE,
    CONSTRAINT group_member_node_fkey FOREIGN KEY (snapshot_id, node_id) REFERENCES node (snapshot_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT group_member_basis_check CHECK (basis IN ('observed', 'resolved', 'heuristic', 'inferred')),
    CONSTRAINT group_member_confidence_check CHECK (confidence IN ('high', 'medium', 'low'))
);
CREATE INDEX group_member_group_idx ON group_member (run_id, group_id, node_id);

-- Rollups of leaf edges between two groups of one lens; never within one group.
CREATE TABLE group_edge (
    run_id            bigint NOT NULL,
    lens              text NOT NULL,
    source_group_id   bigint NOT NULL,
    edge_kind         text NOT NULL,
    target_group_id   bigint NOT NULL,
    count             integer NOT NULL,
    basis_counts      jsonb NOT NULL,
    sample_edge_refs  jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, source_group_id, edge_kind, target_group_id),
    CONSTRAINT group_edge_source_fkey FOREIGN KEY (run_id, lens, source_group_id)
        REFERENCES derived_group (run_id, lens, id) ON DELETE CASCADE,
    CONSTRAINT group_edge_target_fkey FOREIGN KEY (run_id, lens, target_group_id)
        REFERENCES derived_group (run_id, lens, id) ON DELETE CASCADE,
    CONSTRAINT group_edge_kind_check CHECK (edge_kind IN (
        'IMPORTS', 'REQUIRES', 'INHERITS', 'HANDLES', 'TESTS', 'CALLS')),
    CONSTRAINT group_edge_self_check CHECK (source_group_id <> target_group_id),
    CONSTRAINT group_edge_count_check CHECK (count >= 1),
    CONSTRAINT group_edge_basis_counts_check CHECK (jsonb_typeof(basis_counts) = 'object'),
    CONSTRAINT group_edge_samples_check CHECK (
        jsonb_typeof(sample_edge_refs) = 'array' AND jsonb_array_length(sample_edge_refs) <= 20)
);
CREATE INDEX group_edge_target_idx ON group_edge (run_id, target_group_id, edge_kind);

-- ---------------------------------------------------------------------------
-- Metrics (of a node or of a group), findings and inferred layers.

CREATE TABLE node_metric (
    run_id       bigint NOT NULL,
    snapshot_id  bigint NOT NULL,
    node_id      bigint,
    group_id     bigint,
    metric       text NOT NULL,
    value        double precision NOT NULL,
    producer     text NOT NULL,
    rule         text,
    CONSTRAINT node_metric_subject_key UNIQUE NULLS NOT DISTINCT (run_id, node_id, group_id, metric),
    CONSTRAINT node_metric_run_fkey FOREIGN KEY (run_id, snapshot_id)
        REFERENCES derivation_run (id, snapshot_id) ON DELETE CASCADE,
    CONSTRAINT node_metric_node_fkey FOREIGN KEY (snapshot_id, node_id) REFERENCES node (snapshot_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT node_metric_group_fkey FOREIGN KEY (run_id, group_id)
        REFERENCES derived_group (run_id, id) ON DELETE CASCADE,
    CONSTRAINT node_metric_subject_check CHECK (num_nonnulls(node_id, group_id) = 1),
    CONSTRAINT node_metric_metric_check CHECK (metric ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    -- PostgreSQL treats NaN as equal to itself, so it is listed explicitly.
    CONSTRAINT node_metric_value_check CHECK (value NOT IN ('NaN', 'Infinity', '-Infinity')),
    CONSTRAINT node_metric_producer_check CHECK (producer ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT node_metric_rule_check CHECK (rule ~ '^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$')
);
CREATE INDEX node_metric_group_idx ON node_metric (run_id, group_id) WHERE group_id IS NOT NULL;
CREATE INDEX node_metric_node_idx ON node_metric (run_id, node_id) WHERE node_id IS NOT NULL;

CREATE TABLE finding (
    run_id        bigint NOT NULL REFERENCES derivation_run (id) ON DELETE CASCADE,
    id            bigint GENERATED ALWAYS AS IDENTITY,
    rule          text NOT NULL,
    severity      text NOT NULL,
    subject_keys  jsonb NOT NULL,  -- node and/or group keys
    message       text NOT NULL,
    basis         text NOT NULL,
    confidence    text NOT NULL,
    evidence      jsonb NOT NULL,
    PRIMARY KEY (run_id, id),
    CONSTRAINT finding_rule_check CHECK (rule ~ '^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$'),
    CONSTRAINT finding_severity_check CHECK (severity IN ('info', 'low', 'medium', 'high')),
    CONSTRAINT finding_subject_keys_check CHECK (
        jsonb_typeof(subject_keys) = 'array' AND jsonb_array_length(subject_keys) >= 1),
    CONSTRAINT finding_message_check CHECK (message <> ''),
    CONSTRAINT finding_basis_check CHECK (basis IN ('observed', 'resolved', 'heuristic', 'inferred')),
    CONSTRAINT finding_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT finding_evidence_check CHECK (
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) BETWEEN 1 AND 20)
);
CREATE INDEX finding_rule_idx ON finding (run_id, rule);

-- At most one inferred layer per node and run; always basis 'inferred' with a rule.
CREATE TABLE inferred_layer (
    run_id       bigint NOT NULL,
    snapshot_id  bigint NOT NULL,
    node_id      bigint NOT NULL,
    layer        text NOT NULL,
    basis        text NOT NULL,
    confidence   text NOT NULL,
    producer     text NOT NULL,
    rule         text NOT NULL,
    evidence     jsonb NOT NULL,
    PRIMARY KEY (run_id, node_id),
    CONSTRAINT inferred_layer_run_fkey FOREIGN KEY (run_id, snapshot_id)
        REFERENCES derivation_run (id, snapshot_id) ON DELETE CASCADE,
    CONSTRAINT inferred_layer_node_fkey FOREIGN KEY (snapshot_id, node_id) REFERENCES node (snapshot_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT inferred_layer_layer_check CHECK (layer ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT inferred_layer_basis_check CHECK (basis = 'inferred'),
    CONSTRAINT inferred_layer_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT inferred_layer_producer_check CHECK (producer ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT inferred_layer_rule_check CHECK (rule ~ '^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$'),
    CONSTRAINT inferred_layer_evidence_check CHECK (
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) BETWEEN 1 AND 20)
);
CREATE INDEX inferred_layer_layer_idx ON inferred_layer (run_id, layer, node_id);

-- ---------------------------------------------------------------------------
-- Rows of a finished run are immutable (the 0002 pattern, keyed on the run).

CREATE FUNCTION derivation_child_guard_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    frozen_id      bigint;
    frozen_status  text;
BEGIN
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT r.id, r.status INTO frozen_id, frozen_status
        FROM new_rows n JOIN derivation_run r ON r.id = n.run_id
        WHERE r.status <> 'pending' LIMIT 1;
    END IF;
    IF frozen_id IS NULL AND TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT r.id, r.status INTO frozen_id, frozen_status
        FROM old_rows o JOIN derivation_run r ON r.id = o.run_id
        WHERE r.status <> 'pending' LIMIT 1;
    END IF;
    IF frozen_id IS NOT NULL THEN
        RAISE EXCEPTION 'derivation run % is %: its % rows are immutable', frozen_id, frozen_status, TG_TABLE_NAME
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
        'lens', 'derived_group', 'group_member', 'group_edge', 'node_metric', 'finding',
        'inferred_layer']
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_insert AFTER INSERT ON %1$I '
            'REFERENCING NEW TABLE AS new_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION derivation_child_guard_immutable()', child);
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_update AFTER UPDATE ON %1$I '
            'REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION derivation_child_guard_immutable()', child);
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_delete AFTER DELETE ON %1$I '
            'REFERENCING OLD TABLE AS old_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION derivation_child_guard_immutable()', child);
    END LOOP;
END;
$$;

INSERT INTO schema_migration (version) VALUES ('0003_derivation_tier');

COMMIT;
