-- Incremental PostgreSQL migration. Does not change any Snowflake object.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM kumo_task_engine.schema_version WHERE version=2) THEN
        ALTER TABLE kumo_task_engine.schema_version DROP CONSTRAINT schema_version_version_check;
        ALTER TABLE kumo_task_engine.schema_version ADD CONSTRAINT schema_version_version_check CHECK (version IN (1,2));
    END IF;
END $$;
ALTER TABLE kumo_task_engine.runs ADD COLUMN IF NOT EXISTS workflow_key text;
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_outstanding_workflow
    ON kumo_task_engine.runs(workflow_key)
    WHERE workflow_key IS NOT NULL AND state IN ('QUEUED','RUNNING','UNKNOWN');
CREATE TABLE IF NOT EXISTS kumo_task_engine.schedules (
    id uuid PRIMARY KEY,
    request_id text NOT NULL UNIQUE,
    creation_hash text NOT NULL,
    name text NOT NULL,
    cron text NOT NULL,
    timezone text NOT NULL,
    plan jsonb NOT NULL,
    grace_seconds integer NOT NULL CHECK (grace_seconds BETWEEN 0 AND 3600),
    enabled boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 1,
    next_fire_at timestamptz,
    validation_error text,
    created_by text NOT NULL,
    updated_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS schedules_due ON kumo_task_engine.schedules(next_fire_at) WHERE enabled;
CREATE TABLE IF NOT EXISTS kumo_task_engine.schedule_occurrences (
    id uuid PRIMARY KEY,
    schedule_id uuid NOT NULL REFERENCES kumo_task_engine.schedules(id),
    revision integer NOT NULL,
    scheduled_for timestamptz NOT NULL,
    window_end timestamptz,
    state text NOT NULL CHECK (state IN ('ADMITTED','MISSED','SKIPPED_OVERLAP')),
    reason text,
    snapshot jsonb NOT NULL,
    run_id uuid REFERENCES kumo_task_engine.runs(id),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(schedule_id,scheduled_for)
);
CREATE TABLE IF NOT EXISTS kumo_task_engine.schedule_events (
    sequence bigserial PRIMARY KEY,
    schedule_id uuid NOT NULL REFERENCES kumo_task_engine.schedules(id),
    revision integer NOT NULL,
    action text NOT NULL,
    actor text NOT NULL,
    facts jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS kumo_task_engine.scheduler_workers (
    id uuid PRIMARY KEY,
    execution_enabled boolean NOT NULL,
    last_seen timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO kumo_task_engine.schema_version VALUES (2) ON CONFLICT DO NOTHING;
