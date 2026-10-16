"""Exclude deleted_ stage paths from both RAW LIM loaders without loading data.

Run with the backend on PYTHONPATH and Snowflake credentials in the environment.
After applying, refresh the full definitions with python snowflake/export_ddl.py.
"""

from snowflake.connector import DictCursor

import snowflake_client as sf


PATCHES = (
    (
        "KUMO_TST.RAW_LIM.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST()",
        "v_pattern := ''.*'' || v_fmt || ''[^/]*$'';",
        "v_pattern := ''^(?!.*(?i:deleted_)).*'' || v_fmt || ''[^/]*$'';",
    ),
    (
        "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)",
        "V_LIST_PATTERN := ''.*'' || V_LIM_FORMAT || ''[^/]*$'';",
        "V_LIST_PATTERN := ''^(?!.*(?i:deleted_)).*'' || V_LIM_FORMAT || ''[^/]*$'';",
    ),
)


def apply_connection(conn):
    """Validate both live definitions before replacing either procedure."""
    cur = conn.cursor(DictCursor)
    try:
        plans = []
        results = {}
        for procedure, old, new in PATCHES:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (procedure,))
            ddl = cur.fetchone()["DDL"]
            if ddl.count(new) == 1 and old not in ddl:
                results[procedure] = "already applied"
                continue
            if ddl.count(old) != 1 or new in ddl:
                raise RuntimeError(
                    f"{procedure}: expected pattern did not match exactly once; "
                    "the procedure has drifted and was not changed"
                )
            expected = ddl.replace(old, new, 1)
            updated = expected
            # Keep existing callers' privileges when replacing the procedure.
            if "\nCOPY GRANTS\n" not in updated:
                if updated.count("\nRETURNS ") != 1:
                    raise RuntimeError(f"{procedure}: unexpected RETURNS declaration")
                updated = updated.replace("\nRETURNS ", "\nCOPY GRANTS\nRETURNS ", 1)
            plans.append((procedure, ddl, updated, expected))

        for procedure, original, updated, expected in plans:
            # DDL changes are individually committed by Snowflake. Detect drift
            # since preflight rather than overwriting another writer's changes.
            cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (procedure,))
            if cur.fetchone()["DDL"] != original:
                raise RuntimeError(f"{procedure}: definition changed since preflight")
            cur.execute(updated)
            cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (procedure,))
            actual = cur.fetchone()["DDL"]
            if actual != expected:
                raise RuntimeError(f"{procedure}: saved definition differs from expected patch")
            results[procedure] = "applied"
        return results
    finally:
        cur.close()


def apply():
    with sf.connection(use_warehouse=False, include_context=False, force_service=True) as conn:
        return apply_connection(conn)


if __name__ == "__main__":
    print(apply())
