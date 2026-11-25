-- PostgreSQL-only decision history. No foreign keys: retain explanations when
-- execution/configuration records are eventually removed by retention work.
CREATE TABLE IF NOT EXISTS kumo_task_engine.dependency_decisions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    component text NOT NULL,
    decision text NOT NULL,
    graph_id uuid,
    graph_revision integer,
    cascade_id uuid,
    node_key text,
    run_id uuid,
    schedule_id uuid,
    occurrence_id uuid,
    evaluation_id uuid,
    request_id text,
    actor text,
    facts jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS dependency_decisions_cascade
    ON kumo_task_engine.dependency_decisions(cascade_id,id);
CREATE INDEX IF NOT EXISTS dependency_decisions_schedule
    ON kumo_task_engine.dependency_decisions(schedule_id,id) WHERE schedule_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS dependency_decisions_evaluation
    ON kumo_task_engine.dependency_decisions(evaluation_id,id) WHERE evaluation_id IS NOT NULL;
ALTER TABLE kumo_task_engine.schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE kumo_task_engine.schema_version ADD CONSTRAINT schema_version_version_check CHECK (version BETWEEN 1 AND 7);
INSERT INTO kumo_task_engine.schema_version VALUES(7) ON CONFLICT DO NOTHING;
