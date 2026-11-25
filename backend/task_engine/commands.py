"""Frozen SQL/dbt definitions executed only inside an admitted native task."""
import json
import os

from workflow_commands import validate_definition


def enabled():
    return os.getenv('KUMO_PG_BUSINESS_JOBS_ENABLED', '').lower() == 'true'


def definition(value):
    if not enabled():
        raise ValueError('SQL/dbt business execution is not enabled in this worker')
    if not isinstance(value, dict) or set(value) - {'workflowType', 'sqlCommand', 'dbtCommand', 'dbtProjectFqn', 'dbtTarget'}:
        raise ValueError('Invalid SQL/dbt command definition')
    kind = value.get('workflowType')
    if kind not in ('SQL', 'DBT'):
        raise ValueError('Only SQL and DBT jobs are supported')
    fields = ('sqlCommand',) if kind == 'SQL' else ('dbtCommand', 'dbtProjectFqn', 'dbtTarget')
    if any(not isinstance(value.get(f), str) or not value[f].strip() or len(value[f]) > 100000 for f in fields):
        raise ValueError('Supply the SQL command or complete DBT command/project/target')
    if set(value) - {'workflowType', *fields}:
        raise ValueError('Command fields must match the job type')
    return validate_definition({k: value[k].strip() for k in ('workflowType', *fields)})


def task_body(command, run_id):
    """Reuse the maintained resolver, including environment variables and dbt runId.

    Resolution and execution happen on the native task's warehouse. The worker
    never executes business SQL, and failures propagate to native TASK_HISTORY.
    """
    from uuid import UUID
    run_id = str(UUID(str(run_id)))
    literal = json.dumps(command, separators=(',', ':')).replace('\\', '\\\\').replace("'", "''")
    return f"""DECLARE
  resolved VARIANT;
  command_sql VARCHAR;
  resolution_failed EXCEPTION (-20001, 'SQL/dbt command resolution failed; check the saved definition and environment parameters');
BEGIN
  CALL KUMO_DEV.MONITOR_APP.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON('{literal}'), '{run_id}') INTO :resolved;
  IF (NOT COALESCE(resolved:ok::BOOLEAN, FALSE)) THEN
    RAISE resolution_failed;
  END IF;
  command_sql := resolved:execSql::VARCHAR;
  IF (resolved:workflowType::VARCHAR = 'SQL') THEN
    command_sql := 'BEGIN\\n' || command_sql || ';\\nEND;';
  END IF;
  EXECUTE IMMEDIATE command_sql;
END"""
