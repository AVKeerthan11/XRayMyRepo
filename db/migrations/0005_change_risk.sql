-- CIM v1: change risk (how much each changed file of a comparison could break other code).
--
-- Contract: src/xraymyrepo/cim/change_risk.py, docs/cim/CIM-v1.md §8 and
-- docs/insights/change-risk-v1.md. New tables only; 0001-0004 are unchanged.
--
-- A change-risk run is an immutable computation over one complete snapshot comparison,
-- identified by (comparison, algorithm, algorithm_version, params_hash), like a derivation
-- run. It cascades with its comparison, and so with either snapshot. Each item is a
-- changed file of the comparison: (comparison_id, subject_key) references the
-- comparison's node_change row, checked at commit (DEFERRABLE INITIALLY DEFERRED): deleting
-- a snapshot deletes the comparison's rows and, through the run, the items citing them,
-- and both cascades must run before the reference is checked.
--
-- A level is a lower bound on what was observed: points only add up, and caveats lower
-- confidence without changing points. Completing a run checks the items, factors and
-- summary against each other (change_risk_check_complete). Closed vocabularies are
-- text + CHECK (tests/test_schema_consistency.py keeps them equal to the Python enums).

BEGIN;

-- Caveat lists: a JSON array of distinct caveats. Order is the model's (enum order) and
-- is checked when a run is loaded.
CREATE FUNCTION change_risk_caveats_valid(caveats jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
    IF jsonb_typeof(caveats) <> 'array' THEN
        RETURN false;
    END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(caveats) c
        WHERE jsonb_typeof(c) <> 'string' OR c #>> '{}' NOT IN (
            'incomparable_snapshots', 'undetermined_changes', 'incomplete_extraction',
            'hidden_dependencies', 'impact_bounded', 'heuristic_paths',
            'resolution_not_comparable', 'dependent_resolution_changed'))
        AND (SELECT count(DISTINCT c) FROM jsonb_array_elements(caveats) c)
            = jsonb_array_length(caveats);
END;
$$;

-- The confidence caveats allow (cim CAVEAT_CONFIDENCE): low for these, else medium,
-- high without caveats.
CREATE FUNCTION change_risk_confidence_of(caveats jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE
        WHEN caveats ?| ARRAY['undetermined_changes', 'incomplete_extraction'] THEN 'low'
        WHEN jsonb_array_length(caveats) > 0 THEN 'medium'
        ELSE 'high'
    END
$$;

CREATE FUNCTION change_risk_level_rank(level text) RETURNS integer
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE level WHEN 'low' THEN 0 WHEN 'medium' THEN 1 WHEN 'high' THEN 2 END
$$;

CREATE FUNCTION change_risk_confidence_rank(confidence text) RETURNS integer
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE confidence WHEN 'low' THEN 0 WHEN 'medium' THEN 1 WHEN 'high' THEN 2 END
$$;

-- {"levels": {<every level>: n}, "confidences": {<every confidence>: n}}, non-negative
-- integers. NULL-safe (coalesce), as snapshot_comparison_summary_valid.
CREATE FUNCTION change_risk_summary_valid(summary jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT coalesce(
        jsonb_typeof(summary) = 'object'
        AND summary - ARRAY['levels', 'confidences'] = '{}'
        AND jsonb_typeof(summary -> 'levels') = 'object'
        AND jsonb_typeof(summary -> 'confidences') = 'object'
        AND summary -> 'levels' ?& ARRAY['low', 'medium', 'high']
        AND (summary -> 'levels') - ARRAY['low', 'medium', 'high'] = '{}'
        AND summary -> 'confidences' ?& ARRAY['high', 'medium', 'low']
        AND (summary -> 'confidences') - ARRAY['high', 'medium', 'low'] = '{}'
        AND NOT jsonb_path_exists(summary, '$.*.* ? (@.type() != "number" || @ < 0 || @ != @.floor())'),
        false)
$$;

CREATE TABLE change_risk_run (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    comparison_id       bigint NOT NULL REFERENCES snapshot_comparison (id) ON DELETE CASCADE,
    algorithm           text NOT NULL,
    algorithm_version   text NOT NULL,
    params_hash         text NOT NULL,
    params              jsonb NOT NULL,
    medium_at           integer NOT NULL,
    high_at             integer NOT NULL,
    caveats             jsonb NOT NULL DEFAULT '[]',
    excluded_by_origin  integer NOT NULL DEFAULT 0,
    status              text NOT NULL DEFAULT 'pending',
    failure_reason      text,
    level               text,
    confidence          text,
    summary             jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    CONSTRAINT change_risk_run_identity_key
        UNIQUE (comparison_id, algorithm, algorithm_version, params_hash),
    CONSTRAINT change_risk_run_id_comparison_key UNIQUE (id, comparison_id),
    CONSTRAINT change_risk_run_algorithm_check CHECK (algorithm ~ '^[a-z][a-z0-9]*([-_][a-z0-9]+)*$'),
    CONSTRAINT change_risk_run_algorithm_version_check CHECK (algorithm_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)([-+].+)?$'),
    CONSTRAINT change_risk_run_params_hash_check CHECK (params_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT change_risk_run_params_check CHECK (jsonb_typeof(params) = 'object'),
    CONSTRAINT change_risk_run_thresholds_check CHECK (medium_at >= 1 AND high_at > medium_at),
    CONSTRAINT change_risk_run_excluded_check CHECK (excluded_by_origin >= 0),
    CONSTRAINT change_risk_run_caveats_check CHECK (change_risk_caveats_valid(caveats)),
    CONSTRAINT change_risk_run_status_check CHECK (status IN ('pending', 'complete', 'failed')),
    CONSTRAINT change_risk_run_failure_reason_check CHECK ((status = 'failed') = (failure_reason IS NOT NULL)),
    CONSTRAINT change_risk_run_finished_at_check CHECK ((status = 'pending') = (finished_at IS NULL)),
    -- The derived level, confidence and summary of the items: required exactly when
    -- complete, and checked against the items on completion.
    CONSTRAINT change_risk_run_result_required_check CHECK (
        (status = 'complete') = (level IS NOT NULL)
        AND (status = 'complete') = (confidence IS NOT NULL)
        AND (status = 'complete') = (summary IS NOT NULL)),
    CONSTRAINT change_risk_run_level_check CHECK (level IN ('low', 'medium', 'high')),
    CONSTRAINT change_risk_run_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT change_risk_run_summary_check CHECK (
        summary IS NULL OR change_risk_summary_valid(summary))
);
-- change_risk_run_identity_key leads with comparison_id.

-- Runs assess complete comparisons, and say exactly when the comparison is not fully
-- comparable (cim: incomparable_snapshots is an assessment caveat exactly then).
CREATE FUNCTION change_risk_run_require_comparison() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    c  snapshot_comparison%ROWTYPE;
BEGIN
    SELECT * INTO c FROM snapshot_comparison WHERE id = NEW.comparison_id;
    IF c.id IS NULL OR c.status <> 'complete' THEN
        RAISE EXCEPTION 'snapshot comparison % must exist and be complete to assess its risk',
            NEW.comparison_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.caveats ? 'incomparable_snapshots') <> (c.incomparable <> '[]') THEN
        RAISE EXCEPTION 'incomparable_snapshots must be a caveat exactly when comparison % is not fully comparable',
            NEW.comparison_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER change_risk_run_require_comparison
    BEFORE INSERT ON change_risk_run
    FOR EACH ROW EXECUTE FUNCTION change_risk_run_require_comparison();

-- ---------------------------------------------------------------------------
-- One row per assessed changed file. `subject_key` is the comparison row's key (the file
-- in `to`, else in `from`).

CREATE TABLE change_risk_item (
    run_id         bigint NOT NULL,
    comparison_id  bigint NOT NULL,
    subject_key    text COLLATE "C" NOT NULL,
    change         text NOT NULL,
    level          text NOT NULL,
    points         integer NOT NULL,
    confidence     text NOT NULL,
    caveats        jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, subject_key),
    CONSTRAINT change_risk_item_run_fkey FOREIGN KEY (run_id, comparison_id)
        REFERENCES change_risk_run (id, comparison_id) ON DELETE CASCADE,
    CONSTRAINT change_risk_item_subject_fkey FOREIGN KEY (comparison_id, subject_key)
        REFERENCES node_change (comparison_id, key) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT change_risk_item_change_check CHECK (change IN (
        'added', 'removed', 'modified', 'moved', 'unchanged', 'undetermined')),
    CONSTRAINT change_risk_item_level_check CHECK (level IN ('low', 'medium', 'high')),
    CONSTRAINT change_risk_item_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT change_risk_item_points_check CHECK (points >= 0),
    CONSTRAINT change_risk_item_caveats_check CHECK (change_risk_caveats_valid(caveats)),
    -- An item's confidence is the lowest its caveats allow.
    CONSTRAINT change_risk_item_caveat_confidence_check CHECK (
        confidence = change_risk_confidence_of(caveats)),
    CONSTRAINT change_risk_item_key_check CHECK (subject_key <> '' AND subject_key !~ '[\x01-\x1f\x7f]')
);
-- Listing by level: change_risk_level_rank(level) DESC, points DESC, subject_key.
CREATE INDEX change_risk_item_rank_idx
    ON change_risk_item (run_id, change_risk_level_rank(level) DESC, points DESC, subject_key);

-- ---------------------------------------------------------------------------
-- The factors of an item, one per rule. Evidence is stored verbatim as JSONB (RiskEvidence
-- items, each tagged with the snapshot its keys belong to), validated on load.

CREATE TABLE change_risk_factor (
    run_id       bigint NOT NULL,
    subject_key  text COLLATE "C" NOT NULL,
    rule         text NOT NULL,
    points       integer NOT NULL,
    value        integer NOT NULL,
    confidence   text NOT NULL,
    message      text NOT NULL,
    evidence     jsonb NOT NULL,
    caveats      jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, subject_key, rule),
    CONSTRAINT change_risk_factor_item_fkey FOREIGN KEY (run_id, subject_key)
        REFERENCES change_risk_item (run_id, subject_key) ON DELETE CASCADE,
    CONSTRAINT change_risk_factor_rule_check CHECK (rule IN (
        'risk.change.kind', 'risk.change.interface', 'risk.impact.dependents',
        'risk.impact.endpoints', 'risk.change.sensitive_role', 'risk.health.hub',
        'risk.health.cycle', 'risk.dependency.unresolved_after_removal',
        'risk.test.no_dependent_test', 'risk.cap.test_file')),
    CONSTRAINT change_risk_factor_confidence_check CHECK (confidence IN ('high', 'medium', 'low')),
    CONSTRAINT change_risk_factor_points_check CHECK (points >= 0 AND value >= 0),
    CONSTRAINT change_risk_factor_caveats_check CHECK (change_risk_caveats_valid(caveats)),
    CONSTRAINT change_risk_factor_message_check CHECK (
        message <> '' AND message = btrim(message) AND message !~ '[\x00-\x1f\x7f]'),
    CONSTRAINT change_risk_factor_evidence_check CHECK (
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) BETWEEN 1 AND 20),
    -- The test-file cap adds no points; an absence without points says why.
    CONSTRAINT change_risk_factor_cap_check CHECK (rule <> 'risk.cap.test_file' OR points = 0),
    CONSTRAINT change_risk_factor_absence_check CHECK (
        rule <> 'risk.test.no_dependent_test' OR points > 0 OR caveats <> '[]')
);

-- ---------------------------------------------------------------------------
-- Completing a run: what the items, factors and summary must satisfy together, and
-- against the comparison. Runs once, in the pending -> complete update.

CREATE FUNCTION change_risk_check_complete(r change_risk_run) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    problem  text;
    top_rank  integer;
    expected_level  text;
    expected_confidence  text;
BEGIN
    -- A malformed summary is left to change_risk_run_summary_check, which runs after this
    -- trigger and reports it as such.
    IF r.summary IS NULL OR NOT change_risk_summary_valid(r.summary) THEN
        RETURN;
    END IF;
    -- Every item starts with its change, and its points are its factors' points.
    IF EXISTS (
        SELECT 1 FROM change_risk_item i
        WHERE i.run_id = r.id AND NOT EXISTS (
            SELECT 1 FROM change_risk_factor f
            WHERE f.run_id = i.run_id AND f.subject_key = i.subject_key
              AND f.rule = 'risk.change.kind')) THEN
        problem := 'an item has no risk.change.kind factor';
    ELSIF EXISTS (
        SELECT 1 FROM change_risk_item i
        WHERE i.run_id = r.id AND i.points <> (
            SELECT coalesce(sum(f.points), 0) FROM change_risk_factor f
            WHERE f.run_id = i.run_id AND f.subject_key = i.subject_key)) THEN
        problem := 'an item''s points are not the sum of its factors'' points';
    -- A subject is a file row of the comparison, with that row's change.
    ELSIF EXISTS (
        SELECT 1 FROM change_risk_item i
        JOIN node_change n ON n.comparison_id = i.comparison_id AND n.key = i.subject_key
        LEFT JOIN node fn ON fn.snapshot_id = n.from_snapshot_id AND fn.id = n.from_node_id
        LEFT JOIN node tn ON tn.snapshot_id = n.to_snapshot_id AND tn.id = n.to_node_id
        WHERE i.run_id = r.id AND (
            n.change <> i.change
            OR NOT (fn.kind IS NOT DISTINCT FROM 'file' OR tn.kind IS NOT DISTINCT FROM 'file'))) THEN
        problem := 'an item is not a file row of the comparison with its change';
    -- Factor caveats are item caveats.
    ELSIF EXISTS (
        SELECT 1 FROM change_risk_factor f
        JOIN change_risk_item i ON i.run_id = f.run_id AND i.subject_key = f.subject_key
        WHERE f.run_id = r.id AND NOT i.caveats @> f.caveats) THEN
        problem := 'a factor caveat is not a caveat of its item';
    -- A level follows from the points and the thresholds; a test file's level is capped.
    ELSIF EXISTS (
        SELECT 1 FROM change_risk_item i
        WHERE i.run_id = r.id AND i.level <> CASE
            WHEN EXISTS (SELECT 1 FROM change_risk_factor f
                         WHERE f.run_id = i.run_id AND f.subject_key = i.subject_key
                           AND f.rule = 'risk.cap.test_file') THEN 'low'
            WHEN i.points >= r.high_at THEN 'high'
            WHEN i.points >= r.medium_at THEN 'medium'
            ELSE 'low'
        END) THEN
        problem := 'an item''s level does not follow from its points';
    -- Not fully comparable: every item says so.
    ELSIF r.caveats ? 'incomparable_snapshots' AND EXISTS (
        SELECT 1 FROM change_risk_item i
        WHERE i.run_id = r.id AND NOT i.caveats ? 'incomparable_snapshots') THEN
        problem := 'the comparison is not fully comparable, so every item carries the caveat';
    -- The summary counts the items ...
    ELSIF EXISTS (
        SELECT 1 FROM jsonb_each(r.summary -> 'levels') s (level, n)
        WHERE s.n <> to_jsonb((SELECT count(*) FROM change_risk_item
                               WHERE run_id = r.id AND level = s.level)))
       OR EXISTS (
        SELECT 1 FROM jsonb_each(r.summary -> 'confidences') s (confidence, n)
        WHERE s.n <> to_jsonb((SELECT count(*) FROM change_risk_item
                               WHERE run_id = r.id AND confidence = s.confidence))) THEN
        problem := 'the summary does not count the items';
    END IF;
    IF problem IS NULL THEN
        -- ... and the run's level and confidence are the items' (highest level; lowest
        -- confidence, at most what the run's caveats allow).
        SELECT coalesce(max(change_risk_level_rank(level)), 0) INTO top_rank
        FROM change_risk_item WHERE run_id = r.id;
        expected_level := CASE top_rank WHEN 2 THEN 'high' WHEN 1 THEN 'medium' ELSE 'low' END;
        SELECT CASE least(min(change_risk_confidence_rank(confidence)),
                          change_risk_confidence_rank(change_risk_confidence_of(r.caveats)))
                   WHEN 0 THEN 'low' WHEN 1 THEN 'medium' ELSE 'high' END
        INTO expected_confidence
        FROM change_risk_item WHERE run_id = r.id;
        IF r.level IS DISTINCT FROM expected_level THEN
            problem := 'the run''s level is not the highest item level';
        ELSIF r.confidence IS DISTINCT FROM expected_confidence THEN
            problem := 'the run''s confidence is not the lowest its items and caveats allow';
        END IF;
    END IF;
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'change risk run % cannot complete: %', r.id, problem
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
END;
$$;

-- Like snapshot_comparison_guard_immutable: identity, params, thresholds and the run's
-- inputs never change; the only update is the one-way transition pending -> complete |
-- failed. Completing checks the rows (change_risk_check_complete).
CREATE FUNCTION change_risk_run_guard_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.comparison_id         IS DISTINCT FROM OLD.comparison_id
       OR NEW.algorithm          IS DISTINCT FROM OLD.algorithm
       OR NEW.algorithm_version  IS DISTINCT FROM OLD.algorithm_version
       OR NEW.params_hash        IS DISTINCT FROM OLD.params_hash
       OR NEW.params             IS DISTINCT FROM OLD.params
       OR NEW.medium_at          IS DISTINCT FROM OLD.medium_at
       OR NEW.high_at            IS DISTINCT FROM OLD.high_at
       OR NEW.caveats            IS DISTINCT FROM OLD.caveats
       OR NEW.excluded_by_origin IS DISTINCT FROM OLD.excluded_by_origin
       OR NEW.created_at         IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'change risk run % is immutable: identity, params and inputs cannot change', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'change risk run % is %, only pending runs can transition', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'complete' THEN
        PERFORM change_risk_check_complete(NEW);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER change_risk_run_guard_immutable
    BEFORE UPDATE ON change_risk_run
    FOR EACH ROW EXECUTE FUNCTION change_risk_run_guard_immutable();

-- ---------------------------------------------------------------------------
-- Rows of a finished run are immutable (the 0002-0004 pattern, keyed on the run).
-- Deleting a comparison or a snapshot still cascades: by the time the cascaded row
-- deletes run, the run row is gone.

CREATE FUNCTION change_risk_child_guard_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    frozen_id      bigint;
    frozen_status  text;
BEGIN
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT r.id, r.status INTO frozen_id, frozen_status
        FROM new_rows n JOIN change_risk_run r ON r.id = n.run_id
        WHERE r.status <> 'pending' LIMIT 1;
    END IF;
    IF frozen_id IS NULL AND TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT r.id, r.status INTO frozen_id, frozen_status
        FROM old_rows o JOIN change_risk_run r ON r.id = o.run_id
        WHERE r.status <> 'pending' LIMIT 1;
    END IF;
    IF frozen_id IS NOT NULL THEN
        RAISE EXCEPTION 'change risk run % is %: its % rows are immutable', frozen_id, frozen_status, TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END;
$$;

DO $$
DECLARE
    child text;
BEGIN
    FOREACH child IN ARRAY ARRAY['change_risk_item', 'change_risk_factor']
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_insert AFTER INSERT ON %1$I '
            'REFERENCING NEW TABLE AS new_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION change_risk_child_guard_immutable()', child);
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_update AFTER UPDATE ON %1$I '
            'REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION change_risk_child_guard_immutable()', child);
        EXECUTE format(
            'CREATE TRIGGER %1$I_guard_delete AFTER DELETE ON %1$I '
            'REFERENCING OLD TABLE AS old_rows '
            'FOR EACH STATEMENT EXECUTE FUNCTION change_risk_child_guard_immutable()', child);
    END LOOP;
END;
$$;

INSERT INTO schema_migration (version) VALUES ('0005_change_risk');

COMMIT;
