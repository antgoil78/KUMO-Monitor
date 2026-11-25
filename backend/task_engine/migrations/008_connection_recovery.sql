-- Recover accepted work across transport outages without replaying submissions.
ALTER TABLE kumo_task_engine.attempts
    ADD COLUMN IF NOT EXISTS recovery_kind text CHECK (recovery_kind IN ('NETWORK')),
    ADD COLUMN IF NOT EXISTS recovery_started_at timestamptz;
ALTER TABLE kumo_task_engine.schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE kumo_task_engine.schema_version ADD CONSTRAINT schema_version_version_check CHECK (version BETWEEN 1 AND 8);
INSERT INTO kumo_task_engine.schema_version VALUES(8) ON CONFLICT DO NOTHING;
