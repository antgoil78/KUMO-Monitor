-- Stable display identities and independent execution groups in one catalog.
ALTER TABLE kumo_task_engine.cascade_nodes ADD COLUMN IF NOT EXISTS id uuid NOT NULL DEFAULT gen_random_uuid();
CREATE UNIQUE INDEX IF NOT EXISTS cascade_node_identity ON kumo_task_engine.cascade_nodes(id);
ALTER TABLE kumo_task_engine.cascades ADD COLUMN IF NOT EXISTS scope_key text NOT NULL DEFAULT 'FULL';
DROP INDEX IF EXISTS kumo_task_engine.one_outstanding_cascade_per_graph;
CREATE UNIQUE INDEX IF NOT EXISTS one_outstanding_cascade_per_scope ON kumo_task_engine.cascades(graph_id,scope_key)
    WHERE state IN ('QUEUED','RUNNING','UNKNOWN');
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM kumo_task_engine.schema_version WHERE version=5) THEN
        ALTER TABLE kumo_task_engine.schema_version DROP CONSTRAINT schema_version_version_check;
        ALTER TABLE kumo_task_engine.schema_version ADD CONSTRAINT schema_version_version_check CHECK (version IN (1,2,3,4,5));
    END IF;
END $$;
INSERT INTO kumo_task_engine.schema_version VALUES(5) ON CONFLICT DO NOTHING;
