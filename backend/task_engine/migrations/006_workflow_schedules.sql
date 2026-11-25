-- Catalog schedules refer to workflows; each occurrence can point to a cascade.
ALTER TABLE kumo_task_engine.schedule_occurrences ADD COLUMN IF NOT EXISTS cascade_id uuid REFERENCES kumo_task_engine.cascades(id);
CREATE INDEX IF NOT EXISTS schedule_occurrence_cascade ON kumo_task_engine.schedule_occurrences(cascade_id) WHERE cascade_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS one_schedule_per_catalog_workflow
    ON kumo_task_engine.schedules((plan->>'graphId'),(plan->>'workflowId')) WHERE plan ? 'graphId';
ALTER TABLE kumo_task_engine.schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE kumo_task_engine.schema_version ADD CONSTRAINT schema_version_version_check CHECK (version BETWEEN 1 AND 6);
INSERT INTO kumo_task_engine.schema_version VALUES(6) ON CONFLICT DO NOTHING;
