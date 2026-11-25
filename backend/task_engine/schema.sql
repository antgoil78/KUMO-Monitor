-- Explicit local prototype initialization only. No Snowflake objects are changed.
CREATE SCHEMA IF NOT EXISTS kumo_task_engine;
CREATE TABLE IF NOT EXISTS kumo_task_engine.schema_version (
    version integer PRIMARY KEY CHECK (version = 1)
);
INSERT INTO kumo_task_engine.schema_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS kumo_task_engine.runs (
    id uuid PRIMARY KEY,
    request_id text NOT NULL UNIQUE,
    definition jsonb NOT NULL,
    definition_hash text NOT NULL,
    requested_by text NOT NULL,
    state text NOT NULL CHECK (state IN ('QUEUED','RUNNING','SUCCESS','FAILED','CANCELLED','SKIPPED','UNKNOWN')),
    cancel_requested boolean NOT NULL DEFAULT false,
    terminal_event_written boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz
);
CREATE TABLE IF NOT EXISTS kumo_task_engine.attempts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES kumo_task_engine.runs(id),
    job_key text NOT NULL,
    position integer NOT NULL,
    snapshot jsonb NOT NULL,
    depends_on text[] NOT NULL,
    state text NOT NULL CHECK (state IN ('QUEUED','CREATING','SUBMITTING','SUBMITTED','SCHEDULED','RUNNING','UNKNOWN','SUCCESS','FAILED','CANCELLED','SKIPPED')),
    task_fqn text NOT NULL UNIQUE,
    task_id text,
    native_group_id text,
    native_attempt integer,
    query_id text,
    submit_started_at timestamptz,
    observed_at timestamptz,
    ended_at timestamptz,
    error text,
    recovery_checks integer NOT NULL DEFAULT 0,
    next_check_at timestamptz DEFAULT clock_timestamp(),
    lease_owner uuid,
    lease_until timestamptz,
    cleanup_state text NOT NULL DEFAULT 'NONE' CHECK (cleanup_state IN ('NONE','PENDING','DONE','ERROR')),
    cleanup_attempts integer NOT NULL DEFAULT 0,
    cleanup_error text,
    UNIQUE(run_id,job_key)
);
CREATE INDEX IF NOT EXISTS attempts_due ON kumo_task_engine.attempts(next_check_at)
    WHERE next_check_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS kumo_task_engine.events (
    sequence bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES kumo_task_engine.runs(id),
    attempt_id uuid REFERENCES kumo_task_engine.attempts(id),
    kind text NOT NULL,
    facts jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS events_run ON kumo_task_engine.events(run_id,sequence);
