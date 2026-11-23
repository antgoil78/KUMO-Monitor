-- Frozen, isolated workflow dependency trials. No Snowflake DDL or catalog writes.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM kumo_task_engine.schema_version WHERE version=3) THEN
        ALTER TABLE kumo_task_engine.schema_version DROP CONSTRAINT schema_version_version_check;
        ALTER TABLE kumo_task_engine.schema_version ADD CONSTRAINT schema_version_version_check CHECK (version IN (1,2,3));
    END IF;
END $$;
ALTER TABLE kumo_task_engine.runs ADD COLUMN IF NOT EXISTS started_at timestamptz;
ALTER TABLE kumo_task_engine.attempts ADD COLUMN IF NOT EXISTS native_started_at timestamptz;
CREATE INDEX IF NOT EXISTS runs_workflow_started ON kumo_task_engine.runs(workflow_key,started_at) WHERE workflow_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS kumo_task_engine.dependency_graphs (
    id uuid PRIMARY KEY,
    key text NOT NULL UNIQUE,
    name text NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    enabled boolean NOT NULL DEFAULT true,
    definition jsonb NOT NULL,
    definition_hash text NOT NULL,
    source jsonb NOT NULL,
    created_by text NOT NULL,
    updated_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS kumo_task_engine.cascades (
    id uuid PRIMARY KEY,
    graph_id uuid NOT NULL REFERENCES kumo_task_engine.dependency_graphs(id),
    request_id text NOT NULL UNIQUE,
    graph_revision integer NOT NULL,
    snapshot jsonb NOT NULL,
    requested_by text NOT NULL,
    cancel_requested boolean NOT NULL DEFAULT false,
    state text NOT NULL CHECK (state IN ('QUEUED','RUNNING','SUCCESS','FAILED','CANCELLED','SKIPPED','UNKNOWN')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS one_outstanding_cascade_per_graph
    ON kumo_task_engine.cascades(graph_id) WHERE state IN ('QUEUED','RUNNING','UNKNOWN');
CREATE TABLE IF NOT EXISTS kumo_task_engine.cascade_nodes (
    cascade_id uuid NOT NULL REFERENCES kumo_task_engine.cascades(id),
    node_key text NOT NULL,
    workflow_key text NOT NULL,
    state text NOT NULL CHECK (state IN ('WAITING','ADMITTED','SUCCESS','FAILED','CANCELLED','SKIPPED','BLOCKED','UNKNOWN')),
    run_id uuid UNIQUE REFERENCES kumo_task_engine.runs(id),
    blockers jsonb NOT NULL DEFAULT '[]',
    parent_runs jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY(cascade_id,node_key)
);
CREATE TABLE IF NOT EXISTS kumo_task_engine.dependency_work (
    cascade_id uuid PRIMARY KEY REFERENCES kumo_task_engine.cascades(id),
    state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','DONE','ERROR')),
    next_check_at timestamptz DEFAULT clock_timestamp(),
    attempts integer NOT NULL DEFAULT 0,
    error text
);
CREATE TABLE IF NOT EXISTS kumo_task_engine.dependency_workers (
    id uuid PRIMARY KEY,
    execution_enabled boolean NOT NULL,
    last_seen timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS dependency_work_due ON kumo_task_engine.dependency_work(next_check_at) WHERE state='PENDING';
INSERT INTO kumo_task_engine.schema_version VALUES (3) ON CONFLICT DO NOTHING;
