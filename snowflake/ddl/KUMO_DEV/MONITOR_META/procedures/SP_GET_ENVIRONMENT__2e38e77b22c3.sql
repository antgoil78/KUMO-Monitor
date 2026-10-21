-- Source: KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()
-- Refresh: python snowflake/export_ddl.py --object PROCEDURE 'KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()'
USE DATABASE "KUMO_DEV";
USE SCHEMA "KUMO_DEV"."MONITOR_META";

CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()
RETURNS VARIANT
LANGUAGE JAVASCRIPT
EXECUTE AS OWNER
AS '
// The deployment binds this single bootstrap address from KUMO_ENVIRONMENT_TABLE.
var parameterTable = ''KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS'';
var rows = snowflake.execute({sqlText: `
  SELECT PARAMETER_KEY, CASE WHEN IS_SECRET THEN NULL ELSE PARAMETER_VALUE END,
         ACTIVE_FL, IS_SECRET, VALUE_TYPE
    FROM IDENTIFIER(?)
   WHERE INSTANCE_NAME = ''DEFAULT'' AND PARAMETER_GROUP = ''ENVIRONMENT''
`, binds: [parameterTable]});
var environment = {}, parameters = Object.create(null);
while (rows.next()) {
  var key = String(rows.getColumnValue(1));
  if (parameters[key]) throw new Error(''Duplicate environment parameter: '' + key);
  parameters[key] = {value: rows.getColumnValue(2), active: rows.getColumnValue(3),
                     secret: rows.getColumnValue(4), type: rows.getColumnValue(5)};
}
var required = [''DATABASE'', ''MONITOR_APP_SCHEMA'', ''META_SCHEMA'', ''APP_SCHEMA'', ''RAW_SCHEMA'',
                ''DBT_SCHEMA'', ''DBT_PROJECT'', ''DBT_TARGET''];
required.forEach(function(key) {
  var row = parameters[key];
  if (!row || !row.active || row.secret || row.type !== ''STRING'' ||
      typeof row.value !== ''string'' || !/^[A-Za-z_][A-Za-z0-9_$]*$/.test(row.value))
    throw new Error(''Missing or invalid environment parameter: '' + key);
  environment[key] = key === ''DBT_TARGET'' ? row.value : row.value.toUpperCase();
});
var context = snowflake.execute({sqlText: ''SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()''});
context.next();
if (environment.DATABASE !== String(context.getColumnValue(1)).toUpperCase() ||
    environment.META_SCHEMA !== String(context.getColumnValue(2)).toUpperCase())
  throw new Error(''Environment parameters do not match the deployed engine; redeploy after moving its database or metadata schema'');
environment._parameters = parameters;
return environment;
';
