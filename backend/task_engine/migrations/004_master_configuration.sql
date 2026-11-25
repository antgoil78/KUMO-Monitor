-- PostgreSQL owns the WF1 trial configuration. Keep an auditable revision trail.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM kumo_task_engine.schema_version WHERE version=4) THEN
        ALTER TABLE kumo_task_engine.schema_version DROP CONSTRAINT schema_version_version_check;
        ALTER TABLE kumo_task_engine.schema_version ADD CONSTRAINT schema_version_version_check CHECK (version IN (1,2,3,4));
    END IF;
END $$;
ALTER TABLE kumo_task_engine.dependency_graphs ADD COLUMN IF NOT EXISTS configuration_managed boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS kumo_task_engine.graph_revisions (
    graph_id uuid NOT NULL REFERENCES kumo_task_engine.dependency_graphs(id) ON DELETE CASCADE,
    revision integer NOT NULL,
    definition jsonb NOT NULL,
    definition_hash text NOT NULL,
    changed_by text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(graph_id, revision)
);
INSERT INTO kumo_task_engine.graph_revisions(graph_id,revision,definition,definition_hash,changed_by,changed_at)
    SELECT id,revision,definition,definition_hash,updated_by,updated_at FROM kumo_task_engine.dependency_graphs
    ON CONFLICT DO NOTHING;
UPDATE kumo_task_engine.dependency_graphs SET configuration_managed=true WHERE key='wf1-sql-trial';
INSERT INTO kumo_task_engine.schema_version VALUES (4) ON CONFLICT DO NOTHING;
