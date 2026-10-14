"""Register payload files in LIM_BATCH_CTRL only when metadata has data rows.

Run with the repository environment loaded and backend on PYTHONPATH. After
deployment, refresh the full definitions with python snowflake/export_ddl.py.
"""

from snowflake.connector import DictCursor

import snowflake_client as sf


PROCEDURE = "KUMO_ADMIN.WORKFLOW_MANAGER.SP_RAW_LIM_SET_READY(VARCHAR, BOOLEAN, BOOLEAN)"

# Match only the MERGE source; other readiness and validation logic stays intact.
NEEDLE = """        WHERE m.RAW_TABLE = :V_RAW_TABLE
          AND m.FILE_TYPE = ''PAYLOAD''
          AND m.PKG_GROUP_NAME = :PKG_GROUP_NAME
          AND m.RUN_DTTM = (
"""
REPLACEMENT = NEEDLE.replace(
    "          AND m.FILE_TYPE = ''PAYLOAD''\n",
    "          AND m.FILE_TYPE = ''PAYLOAD''\n          AND m.DATA_ROWS > 0\n",
    1,
)


def apply():
    with sf.connection(use_warehouse=False, include_context=False, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (PROCEDURE,))
            ddl = cur.fetchone()["DDL"]
            if ddl.count(REPLACEMENT) == 1 and NEEDLE not in ddl:
                return "already applied"

            if ddl.count(NEEDLE) != 1 or ddl.count("MERGE INTO KUMO_TST.META.LIM_BATCH_CTRL") != 1:
                raise RuntimeError(
                    "SP_RAW_LIM_SET_READY MERGE source did not match the expected version; "
                    "the procedure was not changed"
                )

            updated = ddl.replace(NEEDLE, REPLACEMENT, 1)
            # Replacing the procedure must preserve existing caller access.
            header = "\nRETURNS TABLE ("
            if updated.count(header) != 1:
                raise RuntimeError("SP_RAW_LIM_SET_READY return signature did not match")
            updated = updated.replace(header, "\nCOPY GRANTS" + header, 1)
            cur.execute(updated)

            cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (PROCEDURE,))
            deployed = cur.fetchone()["DDL"].replace("\nCOPY GRANTS", "", 1)
            if deployed != ddl.replace(NEEDLE, REPLACEMENT, 1):
                raise RuntimeError("Deployed SP_RAW_LIM_SET_READY did not match the intended definition")
            return "applied"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
