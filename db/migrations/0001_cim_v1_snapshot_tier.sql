-- CIM v1: snapshot tier (system of record).
--
-- Contract: docs/cim/CIM-v1.md and src/xraymyrepo/cim/. Closed vocabularies are
-- text + CHECK (not PostgreSQL ENUM types) so they can be versioned with plain
-- constraint swaps; tests/test_schema_consistency.py keeps them equal to the
-- Python enums.
--
-- Partitioning: node, edge, classification, unresolved_reference and
-- extraction_issue are snapshot-scoped. Every primary key, unique constraint and
-- foreign key between them leads with snapshot_id, so they can later be turned
-- into tables partitioned by snapshot_id without changing their shape.
--
-- Evidence is JSONB (a capped sample, <= 20 items) and is deliberately not
-- GIN-indexed. Node references inside evidence are node *keys*, not ids.
--
-- Derivation and interpretation tables are intentionally not part of this
-- migration (contract-only in v1).

BEGIN;

CREATE TABLE schema_migration (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Repository level

CREATE TABLE repository (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host        text NOT NULL,
    owner       text NOT NULL,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT repository_host_check  CHECK (host ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$'),
    CONSTRAINT repository_owner_check CHECK (owner ~ '^[A-Za-z0-9_.-]+$'),
    CONSTRAINT repository_name_check  CHECK (name ~ '^[A-Za-z0-9_.-]+$')
);
-- Hosting providers treat owner/name case-insensitively.
CREATE UNIQUE INDEX repository_identity_key ON repository (host, lower(owner), lower(name));

-- An extractor or algorithm at a version and configuration.
CREATE TABLE producer (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL,
    version      text NOT NULL,
    config_hash  text NOT NULL,
    CONSTRAINT producer_identity_key UNIQUE (name, version, config_hash),
    CONSTRAINT producer_id_name_key  UNIQUE (id, name),
    CONSTRAINT producer_name_check   CHECK (name ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT producer_version_check CHECK (version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)([-+].+)?$'),
    CONSTRAINT producer_config_hash_check CHECK (config_hash ~ '^sha256:[0-9a-f]{64}$')
);

-- ---------------------------------------------------------------------------
-- Snapshot: identity is (repository, commit_sha, extractor_version, config_hash)

CREATE TABLE snapshot (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_id       bigint NOT NULL REFERENCES repository (id),
    commit_sha          text NOT NULL,
    extractor_version   text NOT NULL,
    config_hash         text NOT NULL,
    cim_schema_version  text NOT NULL,
    config              jsonb NOT NULL,
    coverage            jsonb NOT NULL DEFAULT '[]',
    status              text NOT NULL DEFAULT 'pending',
    failure_reason      text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    CONSTRAINT snapshot_identity_key UNIQUE (repository_id, commit_sha, extractor_version, config_hash),
    CONSTRAINT snapshot_commit_sha_check CHECK (commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    CONSTRAINT snapshot_extractor_version_check CHECK (extractor_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)([-+].+)?$'),
    CONSTRAINT snapshot_config_hash_check CHECK (config_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT snapshot_config_check CHECK (jsonb_typeof(config) = 'object'),
    CONSTRAINT snapshot_coverage_check CHECK (jsonb_typeof(coverage) = 'array'),
    CONSTRAINT snapshot_status_check CHECK (status IN ('pending', 'complete', 'failed')),
    CONSTRAINT snapshot_failure_reason_check CHECK ((status = 'failed') = (failure_reason IS NOT NULL)),
    CONSTRAINT snapshot_finished_at_check CHECK ((status = 'pending') = (finished_at IS NULL))
);

-- A snapshot never changes what it represents. The only permitted update is the
-- one-way status transition pending -> complete | failed.
CREATE FUNCTION snapshot_guard_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.repository_id      IS DISTINCT FROM OLD.repository_id
       OR NEW.commit_sha         IS DISTINCT FROM OLD.commit_sha
       OR NEW.extractor_version  IS DISTINCT FROM OLD.extractor_version
       OR NEW.config_hash        IS DISTINCT FROM OLD.config_hash
       OR NEW.cim_schema_version IS DISTINCT FROM OLD.cim_schema_version
       OR NEW.config             IS DISTINCT FROM OLD.config
       OR NEW.coverage           IS DISTINCT FROM OLD.coverage
       OR NEW.created_at         IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'snapshot % is immutable: identity, config and coverage cannot change', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'snapshot % is %, only pending snapshots can transition', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER snapshot_guard_immutable
    BEFORE UPDATE ON snapshot
    FOR EACH ROW EXECUTE FUNCTION snapshot_guard_immutable();

-- Producers used by a snapshot. Evidence JSONB names producers; within one
-- snapshot a producer name is unique, so the name resolves to exactly one row.
CREATE TABLE snapshot_producer (
    snapshot_id    bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    producer_id    bigint NOT NULL,
    producer_name  text NOT NULL,
    PRIMARY KEY (snapshot_id, producer_id),
    CONSTRAINT snapshot_producer_name_key UNIQUE (snapshot_id, producer_name),
    CONSTRAINT snapshot_producer_producer_fkey FOREIGN KEY (producer_id, producer_name)
        REFERENCES producer (id, name)
);

-- ---------------------------------------------------------------------------
-- Nodes. Containment is parent_id (the syntactic declaration tree), not an edge.
-- Groups are derivation-tier and never stored here.

CREATE TABLE node (
    snapshot_id        bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    id                 bigint GENERATED ALWAYS AS IDENTITY,
    key                text COLLATE "C" NOT NULL,
    kind               text NOT NULL,
    name               text NOT NULL,
    parent_id          bigint,
    depth              smallint NOT NULL,
    file_id            bigint,  -- containing file of type/function/endpoint nodes
    language           text,
    start_line         integer,
    start_col          integer,
    end_line           integer,
    end_col            integer,
    basis              text NOT NULL,
    confidence         text NOT NULL,
    producer_id        bigint NOT NULL,
    rule               text,
    extraction_status  text,
    blob_sha           text,
    content_hash       text,
    signature_hash     text,
    evidence           jsonb NOT NULL DEFAULT '[]',
    attributes         jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_id, id),
    CONSTRAINT node_key_unique UNIQUE (snapshot_id, key),
    CONSTRAINT node_parent_fkey FOREIGN KEY (snapshot_id, parent_id) REFERENCES node (snapshot_id, id),
    CONSTRAINT node_file_fkey FOREIGN KEY (snapshot_id, file_id) REFERENCES node (snapshot_id, id),
    CONSTRAINT node_producer_fkey FOREIGN KEY (snapshot_id, producer_id)
        REFERENCES snapshot_producer (snapshot_id, producer_id),
    CONSTRAINT node_kind_check CHECK (kind IN (
        'repository', 'directory', 'file', 'type', 'function', 'endpoint', 'package', 'external_package')),
    CONSTRAINT node_basis_check CHECK (basis IN ('observed', 'resolved', 'heuristic')),
    CONSTRAINT node_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT node_observed_confidence_check CHECK (basis <> 'observed' OR confidence = 'high'),
    CONSTRAINT node_key_check CHECK (key <> '' AND key !~ '[\x01-\x1f\x7f]'),
    CONSTRAINT node_repository_key_check CHECK ((kind = 'repository') = (key = '/')),
    CONSTRAINT node_parent_check CHECK (
        (kind IN ('repository', 'package', 'external_package')) = (parent_id IS NULL)),
    CONSTRAINT node_depth_check CHECK (depth >= 0 AND (parent_id IS NULL) = (depth = 0)),
    CONSTRAINT node_file_id_check CHECK (
        (kind IN ('type', 'function', 'endpoint')) = (file_id IS NOT NULL)),
    CONSTRAINT node_span_check CHECK (
        (kind IN ('type', 'function', 'endpoint')) = (start_line IS NOT NULL)
        AND num_nulls(start_line, start_col, end_line, end_col) IN (0, 4)
        AND (start_line IS NULL OR (
            start_line >= 1 AND start_col >= 0 AND end_col >= 0
            AND (end_line, end_col) >= (start_line, start_col)))),
    CONSTRAINT node_rule_check CHECK (rule ~ '^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$'),
    CONSTRAINT node_rule_required_check CHECK (basis <> 'heuristic' OR rule IS NOT NULL),
    CONSTRAINT node_extraction_status_check CHECK (
        extraction_status IN ('full', 'partial', 'failed', 'unsupported', 'excluded')),
    CONSTRAINT node_file_columns_check CHECK (
        (kind = 'file') = (extraction_status IS NOT NULL)
        AND (kind = 'file') = (blob_sha IS NOT NULL)),
    CONSTRAINT node_blob_sha_check CHECK (blob_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    CONSTRAINT node_fingerprint_check CHECK (
        kind IN ('type', 'function') OR (content_hash IS NULL AND signature_hash IS NULL)),
    CONSTRAINT node_content_hash_check CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT node_signature_hash_check CHECK (signature_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT node_evidence_check CHECK (
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) <= 20),
    CONSTRAINT node_attributes_check CHECK (jsonb_typeof(attributes) = 'object')
);
-- node_key_unique (COLLATE "C") also serves subtree prefix queries: key LIKE 'src/app/%'.
CREATE INDEX node_parent_idx ON node (snapshot_id, parent_id);
CREATE INDEX node_kind_idx ON node (snapshot_id, kind);
CREATE INDEX node_file_idx ON node (snapshot_id, file_id) WHERE file_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Edges. Unique per (snapshot, source, kind, target); direction source -> target.

CREATE TABLE edge (
    snapshot_id       bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    id                bigint GENERATED ALWAYS AS IDENTITY,
    kind              text NOT NULL,
    source_id         bigint NOT NULL,
    target_id         bigint NOT NULL,
    basis             text NOT NULL,
    confidence        text NOT NULL,
    occurrence_count  integer NOT NULL DEFAULT 1,
    ambiguity_group   text,
    contested         boolean NOT NULL DEFAULT false,
    evidence          jsonb NOT NULL,
    attributes        jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_id, id),
    -- Leads with source_id so it also serves forward traversal.
    CONSTRAINT edge_unique UNIQUE (snapshot_id, source_id, kind, target_id),
    CONSTRAINT edge_source_fkey FOREIGN KEY (snapshot_id, source_id) REFERENCES node (snapshot_id, id),
    CONSTRAINT edge_target_fkey FOREIGN KEY (snapshot_id, target_id) REFERENCES node (snapshot_id, id),
    CONSTRAINT edge_kind_check CHECK (kind IN (
        'IMPORTS', 'REQUIRES', 'INHERITS', 'HANDLES', 'TESTS', 'CALLS')),
    CONSTRAINT edge_basis_check CHECK (basis IN ('observed', 'resolved', 'heuristic')),
    -- Allowed basis per kind (edges.EDGE_RULES). A syntactic reference without a
    -- concrete target is an unresolved_reference, never an 'observed' edge.
    CONSTRAINT edge_kind_basis_check CHECK (
        (kind = 'IMPORTS' AND basis IN ('resolved', 'heuristic'))
        OR (kind = 'INHERITS' AND basis IN ('resolved', 'heuristic'))
        OR (kind = 'CALLS' AND basis IN ('resolved', 'heuristic'))
        OR (kind = 'REQUIRES' AND basis IN ('observed'))
        OR (kind = 'HANDLES' AND basis IN ('heuristic'))
        OR (kind = 'TESTS' AND basis IN ('heuristic'))),
    CONSTRAINT edge_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT edge_observed_confidence_check CHECK (basis <> 'observed' OR confidence = 'high'),
    CONSTRAINT edge_self_loop_check CHECK (source_id <> target_id OR kind = 'CALLS'),
    CONSTRAINT edge_occurrence_count_check CHECK (occurrence_count >= 1),
    CONSTRAINT edge_ambiguity_check CHECK (ambiguity_group IS NULL OR confidence = 'low'),
    CONSTRAINT edge_evidence_check CHECK (
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) BETWEEN 1 AND 20),
    CONSTRAINT edge_attributes_check CHECK (jsonb_typeof(attributes) = 'object')
);
-- Reverse traversal (impact analysis).
CREATE INDEX edge_target_idx ON edge (snapshot_id, target_id, kind);

-- ---------------------------------------------------------------------------
-- Classifications: roles and facets over existing nodes (never node kinds).

CREATE TABLE classification (
    snapshot_id  bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    id           bigint GENERATED ALWAYS AS IDENTITY,
    node_id      bigint NOT NULL,
    facet        text NOT NULL,
    value        text NOT NULL,
    basis        text NOT NULL,
    confidence   text NOT NULL,
    contested    boolean NOT NULL DEFAULT false,
    evidence     jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, id),
    CONSTRAINT classification_unique UNIQUE (snapshot_id, node_id, facet, value),
    CONSTRAINT classification_node_fkey FOREIGN KEY (snapshot_id, node_id) REFERENCES node (snapshot_id, id),
    CONSTRAINT classification_facet_check CHECK (facet IN ('role', 'origin', 'layer')),
    CONSTRAINT classification_role_check CHECK (facet <> 'role' OR value IN (
        'test_file', 'test_function', 'orm_entity', 'schema', 'config', 'entrypoint', 'manifest')),
    CONSTRAINT classification_origin_check CHECK (facet <> 'origin' OR value IN (
        'source', 'generated', 'vendored', 'docs', 'build_artifact')),
    CONSTRAINT classification_layer_check CHECK (
        facet <> 'layer' OR value ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT classification_basis_check CHECK (basis IN ('observed', 'resolved', 'heuristic')),
    CONSTRAINT classification_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT classification_observed_confidence_check CHECK (basis <> 'observed' OR confidence = 'high'),
    CONSTRAINT classification_evidence_check CHECK (
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) BETWEEN 1 AND 20)
);
CREATE INDEX classification_value_idx ON classification (snapshot_id, facet, value);

-- ---------------------------------------------------------------------------
-- What the extractors could not see.

CREATE TABLE unresolved_reference (
    snapshot_id  bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    id           bigint GENERATED ALWAYS AS IDENTITY,
    source_id    bigint NOT NULL,
    file_id      bigint NOT NULL,
    ref_kind     text NOT NULL,
    raw_text     text NOT NULL,
    start_line   integer NOT NULL,
    start_col    integer NOT NULL,
    end_line     integer NOT NULL,
    end_col      integer NOT NULL,
    reason       text NOT NULL,
    candidates   jsonb NOT NULL DEFAULT '[]',  -- node keys, for reason = 'ambiguous'
    producer_id  bigint NOT NULL,
    rule         text,
    PRIMARY KEY (snapshot_id, id),
    CONSTRAINT unresolved_reference_source_fkey FOREIGN KEY (snapshot_id, source_id) REFERENCES node (snapshot_id, id),
    CONSTRAINT unresolved_reference_file_fkey FOREIGN KEY (snapshot_id, file_id) REFERENCES node (snapshot_id, id),
    CONSTRAINT unresolved_reference_producer_fkey FOREIGN KEY (snapshot_id, producer_id)
        REFERENCES snapshot_producer (snapshot_id, producer_id),
    CONSTRAINT unresolved_reference_ref_kind_check CHECK (ref_kind IN ('import', 'call', 'inheritance', 'handler')),
    CONSTRAINT unresolved_reference_reason_check CHECK (
        reason IN ('not_found', 'dynamic', 'ambiguous', 'unsupported_language')),
    CONSTRAINT unresolved_reference_raw_text_check CHECK (raw_text <> ''),
    CONSTRAINT unresolved_reference_span_check CHECK (
        start_line >= 1 AND start_col >= 0 AND end_col >= 0
        AND (end_line, end_col) >= (start_line, start_col)),
    CONSTRAINT unresolved_reference_candidates_check CHECK (
        jsonb_typeof(candidates) = 'array'
        AND (reason = 'ambiguous') = (jsonb_array_length(candidates) >= 2)
        AND (reason = 'ambiguous' OR jsonb_array_length(candidates) = 0)),
    CONSTRAINT unresolved_reference_rule_check CHECK (rule ~ '^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$')
);
CREATE INDEX unresolved_reference_file_idx ON unresolved_reference (snapshot_id, file_id);
CREATE INDEX unresolved_reference_source_idx ON unresolved_reference (snapshot_id, source_id);

CREATE TABLE extraction_issue (
    snapshot_id  bigint NOT NULL REFERENCES snapshot (id) ON DELETE CASCADE,
    id           bigint GENERATED ALWAYS AS IDENTITY,
    file_id      bigint,
    severity     text NOT NULL,
    code         text NOT NULL,
    message      text NOT NULL,
    start_line   integer,
    start_col    integer,
    end_line     integer,
    end_col      integer,
    producer_id  bigint NOT NULL,
    rule         text,
    PRIMARY KEY (snapshot_id, id),
    CONSTRAINT extraction_issue_file_fkey FOREIGN KEY (snapshot_id, file_id) REFERENCES node (snapshot_id, id),
    CONSTRAINT extraction_issue_producer_fkey FOREIGN KEY (snapshot_id, producer_id)
        REFERENCES snapshot_producer (snapshot_id, producer_id),
    CONSTRAINT extraction_issue_severity_check CHECK (severity IN ('error', 'warning', 'info')),
    CONSTRAINT extraction_issue_code_check CHECK (code ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT extraction_issue_message_check CHECK (message <> ''),
    CONSTRAINT extraction_issue_span_check CHECK (
        num_nulls(start_line, start_col, end_line, end_col) IN (0, 4)
        AND (start_line IS NULL OR file_id IS NOT NULL)),
    CONSTRAINT extraction_issue_rule_check CHECK (rule ~ '^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$')
);
CREATE INDEX extraction_issue_file_idx ON extraction_issue (snapshot_id, file_id);

INSERT INTO schema_migration (version) VALUES ('0001_cim_v1_snapshot_tier');

COMMIT;
