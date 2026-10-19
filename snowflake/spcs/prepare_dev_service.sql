-- Prepared one-time setup; not executed automatically.
-- Run this before create_dev_service.sql. This resumes the shared pool.

USE ROLE ACCOUNTADMIN;
GRANT USAGE ON COMPUTE POOL KUMO_SHARED_MONITOR_POOL TO ROLE KUMO_ADMIN;
GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO ROLE KUMO_ADMIN;
ALTER COMPUTE POOL KUMO_SHARED_MONITOR_POOL RESUME;
DESCRIBE COMPUTE POOL KUMO_SHARED_MONITOR_POOL;

-- Wait until state is IDLE or ACTIVE before running create_dev_service.sql.
-- If state is STARTING, rerun DESCRIBE to check progress.
