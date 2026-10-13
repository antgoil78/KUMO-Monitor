-- Run after create_monitor_workflow_job.sql as SECURITYADMIN, or another role
-- authorized to manage existing and future grants in these schemas.
-- Run the SHOW and SET statements together, in order, in the same session.
-- The recipient is the account role that owns the deployed SPCS service.
SHOW GRANTS ON SERVICE KUMO_ADMIN.KUMO_MONITOR.KUMO_MONITOR_SERVICE;

SET monitor_service_role = (
  SELECT "grantee_name"
  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
  WHERE "privilege" = 'OWNERSHIP' AND "granted_to" = 'ROLE'
);

SELECT $monitor_service_role AS MONITOR_SERVICE_OWNER_ROLE;

GRANT USAGE ON DATABASE KUMO_TST
  TO ROLE IDENTIFIER($monitor_service_role);
GRANT USAGE ON SCHEMA KUMO_TST.MONITOR_APP
  TO ROLE IDENTIFIER($monitor_service_role);
GRANT USAGE ON SCHEMA KUMO_TST.MONITOR_META
  TO ROLE IDENTIFIER($monitor_service_role);

-- Existing tables need explicit grants; future grants do not backfill them.
-- SELECT loads the pages; INSERT, UPDATE and DELETE support catalog editing.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA KUMO_TST.MONITOR_APP
  TO ROLE IDENTIFIER($monitor_service_role);
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA KUMO_TST.MONITOR_META
  TO ROLE IDENTIFIER($monitor_service_role);

-- Include tables created by later migrations. The initial migration creates
-- MONITOR_META without tables. No ownership or creation privileges are required.
GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA KUMO_TST.MONITOR_APP
  TO ROLE IDENTIFIER($monitor_service_role);
GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA KUMO_TST.MONITOR_META
  TO ROLE IDENTIFIER($monitor_service_role);

SHOW GRANTS TO ROLE IDENTIFIER($monitor_service_role);
SHOW FUTURE GRANTS IN SCHEMA KUMO_TST.MONITOR_APP;
SHOW FUTURE GRANTS IN SCHEMA KUMO_TST.MONITOR_META;
