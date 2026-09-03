"""Idempotently add per-run child suppression to the Snowflake workflow engine."""

from snowflake.connector import DictCursor

import snowflake_client as sf


SCHEMA = "KUMO_ADMIN.WORKFLOW_MANAGER"
COMPLETION_PROCEDURES = (
    "SP_SQL_COMPLETE(VARCHAR, BOOLEAN, VARCHAR)",
    "SP_DBT_COMPLETE(VARCHAR, BOOLEAN, VARCHAR)",
)


def apply():
    with sf.connection(force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            for table in ("WORKFLOW_RUN_QUEUE", "WORKFLOW_HISTORY"):
                cur.execute(
                    f"ALTER TABLE {SCHEMA}.{table} "
                    "ADD COLUMN IF NOT EXISTS SKIP_CHILDREN BOOLEAN DEFAULT FALSE"
                )

            for procedure in COMPLETION_PROCEDURES:
                cur.execute(
                    "SELECT GET_DDL(%s, %s) AS DDL",
                    ("PROCEDURE", f"{SCHEMA}.{procedure}"),
                )
                ddl = cur.fetchone()["DDL"]
                if "V_SKIP_CHILDREN" in ddl:
                    continue
                ddl = ddl.replace(
                    "CREATE OR REPLACE PROCEDURE ",
                    f"CREATE OR REPLACE PROCEDURE {SCHEMA}.",
                    1,
                )
                ddl = ddl.replace(
                    "V_WF_ID STRING; V_CHAIN_ID STRING; V_CHAIN_DEPTH NUMBER;",
                    "V_WF_ID STRING; V_CHAIN_ID STRING; V_CHAIN_DEPTH NUMBER; "
                    "V_SKIP_CHILDREN BOOLEAN DEFAULT FALSE;",
                    1,
                )
                ddl = ddl.replace(
                    "SELECT WORKFLOW_ID, CHAIN_ID, CHAIN_DEPTH "
                    "INTO :V_WF_ID, :V_CHAIN_ID, :V_CHAIN_DEPTH",
                    "SELECT WORKFLOW_ID, CHAIN_ID, CHAIN_DEPTH, "
                    "COALESCE(SKIP_CHILDREN, FALSE) "
                    "INTO :V_WF_ID, :V_CHAIN_ID, :V_CHAIN_DEPTH, :V_SKIP_CHILDREN",
                    1,
                )
                marker = "  IF (:P_SUCCESS) THEN\n    SELECT COALESCE(ON_SUCCESS"
                guard = (
                    "  IF (V_SKIP_CHILDREN) THEN\n"
                    "    INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS "
                    "(RUN_ID, LOG_LEVEL, MESSAGE)\n"
                    "    VALUES (:P_RUN_ID, ''INFO'', "
                    "''COMPLETE: child workflows skipped for this run'');\n"
                    "    RETURN ''OK'';\n"
                    "  END IF;\n\n"
                    "  IF (:P_SUCCESS) THEN\n    SELECT COALESCE(ON_SUCCESS"
                )
                if marker not in ddl:
                    raise RuntimeError(f"Cannot patch {procedure}: completion marker missing")
                cur.execute(ddl.replace(marker, guard, 1))
        finally:
            cur.close()


if __name__ == "__main__":
    apply()
