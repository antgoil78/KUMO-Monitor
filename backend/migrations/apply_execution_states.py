"""Update only the four parallel procedures changed for complete job states.

Read-only unless --apply; refuses to replace procedures while runs are pending.
"""
import argparse
import json
from apply_parallel_engine import DDL, render
from migration_support import connect
from snowflake.connector import DictCursor
import config

NAMES = ('SP_ORCH_REQUEST_RUN', 'SP_ORCH_START_RUN', 'SP_ORCH_EXECUTE_WORKFLOW', 'SP_ORCH_RECONCILE')


def run(apply=False):
    with connect() as connection, connection.cursor(DictCursor) as cursor:
        cursor.execute(f"SELECT PARAMETER_KEY,PARAMETER_VALUE,VALUE_TYPE,ACTIVE_FL,IS_SECRET FROM {config.PARAMETER_STORE} WHERE INSTANCE_NAME='DEFAULT' AND PARAMETER_GROUP='ENVIRONMENT'")
        config.apply_environment(cursor.fetchall())
        cursor.execute(f"SELECT COUNT(*) N FROM {config.T_ORCHESTRATION_EXECUTION_LOG} WHERE JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED','RUNNING')")
        if cursor.fetchone()['N']:
            raise ValueError('Finish pending parallel runs before replacing procedures')
        cursor.execute(f"SELECT PROCEDURE_NAME,COMMENT FROM {config.DB}.INFORMATION_SCHEMA.PROCEDURES WHERE PROCEDURE_SCHEMA=%s AND PROCEDURE_NAME IN (%s,%s,%s,%s)", (config.SCHEMA, *NAMES))
        existing = cursor.fetchall()
        if len(existing) != len(NAMES) or any(not str(row['COMMENT']).startswith('KUMO parallel orchestration v') for row in existing):
            raise ValueError('Expected the installed parallel engine procedures')
        if apply:
            for name in NAMES:
                cursor.execute(render(DDL / ('MONITOR_APP/procedures/' + name + '.sql')).replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
        return dict(applied=apply, namespace=config.APP_NAMESPACE, procedures=list(NAMES))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), indent=2))
