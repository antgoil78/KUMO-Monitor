-- Prepared shared infrastructure. This file is not applied automatically.
-- KUMO_SHARED already exists. Run with a role authorized to create schemas,
-- image repositories and account-level compute pools.
-- Existing objects are preserved by IF NOT EXISTS; no service or task is created.

CREATE SCHEMA IF NOT EXISTS KUMO_SHARED.MONITOR_APP;

CREATE IMAGE REPOSITORY IF NOT EXISTS KUMO_SHARED.MONITOR_APP.MONITOR_REPOSITORY;

-- Compute pools belong to the account, not to a database or schema.
-- The shared prefix identifies this pool for the new DEV and TST services.
-- Match the current monitor's CPU_X64_XS family and one-node configuration.
-- Leave the pool suspended without provisioning nodes or enabling auto-resume.
CREATE COMPUTE POOL IF NOT EXISTS KUMO_SHARED_MONITOR_POOL
  MIN_NODES = 1
  MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_XS
  AUTO_RESUME = FALSE
  INITIALLY_SUSPENDED = TRUE
  AUTO_SUSPEND_SECS = 300
  COMMENT = 'Shared compute for the new KUMO Monitor DEV and TST services';
