"""Prepare workflow schema and dbt parameters in the DEV parameter store.

Only configuration rows are changed. Run with --apply to commit the migration;
the default is a read-only preflight. Existing conflicting values stop the
migration. Workflows, procedures and task states are not changed.
"""

import argparse
import json

from snowflake.connector import DictCursor

from migration_support import connect


TABLE = "KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS"
PARAMETERS = {
    "DATABASE": ("KUMO_DEV", "Database for this workflow environment."),
    "MONITOR_APP_SCHEMA": ("MONITOR_APP", "Schema containing monitor procedures and tasks."),
    "META_SCHEMA": ("MONITOR_META", "Schema containing monitor configuration and runtime tables."),
    "APP_SCHEMA": ("META", "Application metadata schema for batch control, progress and status tables."),
    "DBT_SCHEMA": ("DBT_APP", "Schema containing the Snowflake dbt project object."),
    "DBT_PROJECT": ("KUMO_DEV", "Snowflake dbt project object name."),
    "DBT_TARGET": ("kumo_DEV", "dbt profile target; case is preserved."),
}


def load_parameters(cursor):
    cursor.execute(f"""
        SELECT PARAMETER_KEY, PARAMETER_VALUE, VALUE_TYPE, ACTIVE_FL, IS_SECRET
          FROM {TABLE}
         WHERE INSTANCE_NAME = 'DEFAULT' AND PARAMETER_GROUP = 'ENVIRONMENT'
    """)
    return {row["PARAMETER_KEY"]: row for row in cursor.fetchall()}


def plan_parameters(rows):
    planned = dict(rows)
    rename = (
        "MONITOR_APP_SCHEMA" not in planned
        and planned.get("APP_SCHEMA", {}).get("PARAMETER_VALUE") == "MONITOR_APP"
    )
    if rename:
        planned["MONITOR_APP_SCHEMA"] = planned.pop("APP_SCHEMA")
    for key, (value, _) in PARAMETERS.items():
        row = planned.get(key)
        if row and (row["PARAMETER_VALUE"] != value or row["VALUE_TYPE"] != "STRING"
                    or not row["ACTIVE_FL"] or row["IS_SECRET"]):
            raise ValueError(f"Conflicting DEV environment parameter: {key}")
    return rename, [key for key in PARAMETERS if key not in planned]


def migrate(apply=False):
    with connect() as conn:
        with conn.cursor(DictCursor) as cursor:
            cursor.execute("ALTER SESSION SET QUERY_TAG = 'KUMO_WORKFLOW_ENVIRONMENT_PARAMETERS'")
            cursor.execute("BEGIN TRANSACTION")
            try:
                rename, missing = plan_parameters(load_parameters(cursor))
                # Verify the configured project and the case-sensitive target
                # before persisting dbt settings.
                cursor.execute("SHOW DBT PROJECTS IN DATABASE KUMO_DEV")
                project = next((row for row in cursor.fetchall()
                                if row["schema_name"] == PARAMETERS["DBT_SCHEMA"][0]
                                and row["name"] == PARAMETERS["DBT_PROJECT"][0]), None)
                if not project or project.get("default_target") != PARAMETERS["DBT_TARGET"][0]:
                    raise ValueError("The DEV dbt project or default target differs from the expected configuration")
                if apply:
                    if rename:
                        cursor.execute(f"""
                            UPDATE {TABLE}
                               SET PARAMETER_KEY = 'MONITOR_APP_SCHEMA',
                                   DESCRIPTION = %s,
                                   UPDATED_DTTM = SYSDATE(), UPDATED_BY = CURRENT_USER()
                             WHERE INSTANCE_NAME = 'DEFAULT' AND PARAMETER_GROUP = 'ENVIRONMENT'
                               AND PARAMETER_KEY = 'APP_SCHEMA' AND PARAMETER_VALUE = 'MONITOR_APP'
                        """, (PARAMETERS["MONITOR_APP_SCHEMA"][1],))
                        if cursor.rowcount != 1:
                            raise RuntimeError("The parameter selected for rename changed during migration")
                    for key in missing:
                        value, description = PARAMETERS[key]
                        cursor.execute(f"""
                            INSERT INTO {TABLE}
                              (INSTANCE_NAME, PARAMETER_GROUP, PARAMETER_KEY, PARAMETER_VALUE,
                               VALUE_TYPE, DESCRIPTION, IS_SECRET, ACTIVE_FL)
                            VALUES ('DEFAULT', 'ENVIRONMENT', %s, %s, 'STRING', %s, FALSE, TRUE)
                        """, (key, value, description))
                    remaining_rename, remaining_missing = plan_parameters(load_parameters(cursor))
                    if remaining_rename or remaining_missing:
                        raise RuntimeError("Parameter verification failed")
                    cursor.execute("COMMIT")
                else:
                    cursor.execute("ROLLBACK")
                return {"table": TABLE, "applied": apply,
                        "rename": "APP_SCHEMA -> MONITOR_APP_SCHEMA" if rename else None,
                        "insert": missing, "verified": True}
            except Exception:
                cursor.execute("ROLLBACK")
                raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the DEV parameter changes")
    print(json.dumps(migrate(parser.parse_args().apply), indent=2))
