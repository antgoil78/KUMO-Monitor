"""Keep the path separator between the external LIM stage URL and FILES."""

from snowflake.connector import DictCursor

import snowflake_client as sf


PROCEDURE = "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)"
MARKER = "RAW_LIM_COPY_FILES_STAGE_SEPARATOR_V1"

NEEDLE = """              STARTSWITH("name", :V_STAGE_URL || ''/''),
              SUBSTR("name", LENGTH(:V_STAGE_URL) + 2),
              "name"
"""

REPLACEMENT = """              STARTSWITH("name", :V_STAGE_URL || ''/''),
              -- RAW_LIM_COPY_FILES_STAGE_SEPARATOR_V1
              -- FILES paths need a leading slash because an external stage URL
              -- and a file name are concatenated without an implicit separator.
              SUBSTR("name", LENGTH(:V_STAGE_URL) + 1),
              "name"
"""


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (PROCEDURE,))
            ddl = cur.fetchone()["DDL"]
            if MARKER in ddl:
                return "already applied"

            count = ddl.count(NEEDLE)
            if count != 1:
                raise RuntimeError(
                    f"LOAD_RAW_LIM_FROM_STAGE stage-path block matched {count} times; "
                    "the procedure has drifted and was not changed"
                )

            ddl = ddl.replace(NEEDLE, REPLACEMENT, 1)
            ddl = ddl.replace(
                'CREATE OR REPLACE PROCEDURE "LOAD_RAW_LIM_FROM_STAGE"',
                'CREATE OR REPLACE PROCEDURE KUMO_TST.META."LOAD_RAW_LIM_FROM_STAGE"',
                1,
            )
            cur.execute(ddl)
            return "applied"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
