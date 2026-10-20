"""Replace DEV RAW readiness flags with batch registration and delivery history.

The default invocation prepares SQL and checks live interfaces. --apply replaces
only the three named DEV procedures, preserves grants, and refreshes full DDL.
It never runs a loader/workflow, changes existing batch rows, or alters tasks.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from apply_dev_workbook_objects import ROOT, connect, procedure_parts, replace_once


MARKER = "DEV_LIM_BATCH_READINESS_V1"
SQL_FILE = Path(__file__).with_suffix(".sql")
TARGETS = {
    "refresh": ("KUMO_DEV.MONITOR_APP.SP_RAW_LIM_META_REFRESH(VARCHAR,VARCHAR)",
                "29d3ac2fe54d39bb7acb7580dbbce64c5f8e28df5acc69dcc51fdf9bae803e90"),
    "ready": ("KUMO_DEV.MONITOR_APP.SP_RAW_LIM_SET_READY(VARCHAR,BOOLEAN,BOOLEAN,VARCHAR)",
              "f6f6892dd17384ce81c768c1e26e1e3170d9e96ac5d2fc86dcf50b7bb7727d16"),
    "loader": ("KUMO_DEV.MONITOR_META.LOAD_RAW_LIM_FROM_STAGE(VARCHAR,DATE,DATE,BOOLEAN,BOOLEAN,BOOLEAN,VARCHAR)",
               "2f812747512066e1caafae0e28256dc1e6562d45da1004b138a612d61e52e06d"),
}


def assemble(header, body):
    return header + "\nAS '" + body.replace("'", "''") + "';\n"


def render_refresh(header, body):
    body = replace_once(body, "    V_SOURCE_TABLE VARCHAR;", """    V_SOURCE_TABLE VARCHAR;
    V_BATCH_CTRL VARCHAR;
    V_LATEST_LOG VARCHAR;
    V_HISTORY_LOG VARCHAR;
    V_ARCHIVE VARCHAR;
    V_TRANSACTION_STARTED BOOLEAN DEFAULT FALSE;""")
    body = replace_once(body, "    V_RUN_DTTM := SYSDATE();", """    V_BATCH_CTRL := V_DATABASE || '.META.LIM_BATCH_CTRL';
    V_LATEST_LOG := V_DATABASE || '.META._TMP_SET_READY_LOG';
    V_HISTORY_LOG := V_DATABASE || '.MONITOR_META._SET_READY_LOG';
    V_ARCHIVE := V_DATABASE || '.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE';
    V_RUN_DTTM := SYSDATE();""")
    start = body.index("    /*\n    DELETE FROM")
    end = body.index("    INSERT INTO IDENTIFIER(:V_METADATA_TABLE)", start)
    body = body[:start] + """    -- Replace the current subject snapshot, including a refresh with no candidates.
    -- Readiness history, rather than old metadata snapshots, records acceptance.
    BEGIN TRANSACTION;
    V_TRANSACTION_STARTED := TRUE;
    DELETE FROM IDENTIFIER(:V_METADATA_TABLE) WHERE RAW_TABLE = :V_RAW_TABLE;

""" + body[end:]
    body = replace_once(body, "    WITH raw_input AS (", """    WITH registered_files AS (
        SELECT DISTINCT DW_FILE_NM, DW_SRC_SYST
        FROM IDENTIFIER(:V_BATCH_CTRL)
        WHERE UPPER(DW_TABLE_NM) IN (
            'RAW_LIM.RAW_LIM_' || UPPER(:DLVY_SUBJECT_AREA), :V_RAW_TABLE)
    ),
    accepted_deliveries AS (
        SELECT DISTINCT PKG_GROUP_NAME, DLVY_END_DATE, DLVY_SOURCE_ID,
               DLVY_PKG_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO
        FROM (
            SELECT * FROM IDENTIFIER(:V_LATEST_LOG)
            UNION ALL
            SELECT * FROM IDENTIFIER(:V_HISTORY_LOG)
        )
        WHERE STATUS IN ('UPDATED', 'PREVIOUSLY_LOADED')
    ),
    raw_input AS (""")
    body = replace_once(body, """            SUBSTR(TRIM(DATA::STRING), 1, 2) AS RECORD_TYPE,
            IFNULL(DW_READY_TO_LOAD_FL, FALSE) AS DW_READY_TO_LOAD_FL
        FROM IDENTIFIER(:V_RAW_TABLE)
        WHERE NOT (SUBSTR(REGEXP_SUBSTR(DW_FILE_NM::STRING, '[^/]+$'), 8, 3) = '000'
                   AND IFNULL(DW_READY_TO_LOAD_FL, FALSE) = TRUE)""", """            SUBSTR(TRIM(DATA::STRING), 1, 2) AS RECORD_TYPE
        FROM IDENTIFIER(:V_RAW_TABLE) raw
        WHERE NOT EXISTS (
            SELECT 1 FROM IDENTIFIER(:V_ARCHIVE) archived
            WHERE archived.SUBJECT_AREA = UPPER(:DLVY_SUBJECT_AREA)
              AND archived.DW_FILE_NM = raw.DW_FILE_NM
              AND archived.DW_FILE_CHECK_SUM IS NOT DISTINCT FROM raw.DW_FILE_CHECK_SUM
        )""")
    body = replace_once(body, "            RECORD_TYPE,\n            DW_READY_TO_LOAD_FL\n        FROM raw_input", "            RECORD_TYPE\n        FROM raw_input")
    body = replace_once(body, "            RECORD_TYPE,\n            DW_READY_TO_LOAD_FL,\n            SUBSTR(FILE_KEY", "            RECORD_TYPE,\n            SUBSTR(FILE_KEY")
    body = replace_once(body, "            MAX(IFF(DW_READY_TO_LOAD_FL, 1, 0)) AS READY_TO_LOAD_ROW_EXISTS,\n", "")
    # Do the registration lookup after aggregation, not once per RAW data row.
    body = replace_once(body, "    physical_file_status AS (", """    physical_file_readiness AS (
        SELECT p.*,
               IFF(b.DW_FILE_NM IS NOT NULL OR
                   (p.DATA_10_CNT = 0 AND p.ACTUAL_ROWS = 0 AND p.EXPECTED_ROWS = 0
                    AND p.FOOTER_99_CNT > 0 AND accepted.DLVY_SOURCE_ID IS NOT NULL),
                   1, 0) AS READY_TO_LOAD_ROW_EXISTS
        FROM physical_file_agg p
        LEFT JOIN registered_files b
          ON b.DW_FILE_NM = p.ORIGINAL_FILE_NAME AND b.DW_SRC_SYST = p.DLVY_SOURCE_ID
        LEFT JOIN IDENTIFIER(:V_SOURCE_TABLE) mapping
          ON mapping.ACTIVE_FL = TRUE AND mapping.SOURCE_ID = p.DLVY_SOURCE_ID
         AND mapping.SUBJECT_AREA = p.DLVY_SUBJECT_AREA_ID AND mapping.PKG_ID = p.DLVY_PKG_ID
        LEFT JOIN accepted_deliveries accepted
          ON accepted.PKG_GROUP_NAME = mapping.PKG_GROUP_NAME
         AND accepted.DLVY_SOURCE_ID = p.DLVY_SOURCE_ID
         AND accepted.DLVY_PKG_ID = p.DLVY_PKG_ID
         AND accepted.DLVY_PKG_YEAR = p.DLVY_PKG_YEAR
         AND accepted.DLVY_PKG_YEAR_SEQ_NO = p.DLVY_PKG_YEAR_SEQ_NO
         AND accepted.DLVY_END_DATE = p.DLVY_END_DATE
    ),
    physical_file_status AS (""")
    body = replace_once(body, "        FROM physical_file_agg\n    ),", "        FROM physical_file_readiness\n    ),")
    body = replace_once(body, "    RETURN 'SUCCESS: '", "    COMMIT;\n    V_TRANSACTION_STARTED := FALSE;\n\n    RETURN 'SUCCESS: '")
    body = replace_once(body, "END;\n", """EXCEPTION
    WHEN OTHER THEN
        IF (V_TRANSACTION_STARTED) THEN
            ROLLBACK;
        END IF;
        RAISE;
END;
""")
    return body


def render_ready(header, old):
    # Keep the public interface and result columns; all RAW input uses only its
    # six remaining columns. Current/history delivery logs retain empty-file acceptance.
    return Path(__file__).with_name("dev_lim_set_ready_body.sql").read_text()


def render_loader(header, body):
    body = replace_once(body, "    V_BATCH_CONTROL VARCHAR;", "    V_BATCH_CONTROL VARCHAR;\n    V_ARCHIVE VARCHAR;")
    body = replace_once(body, "    V_ROWS_READY_UPDATED   NUMBER := 0;", "    V_ROWS_READY_UPDATED   NUMBER := 0;\n    V_FILES_REGISTERED NUMBER := 0;\n    V_CONTROL_ARCHIVE_DELETED NUMBER := 0;")
    body = replace_once(body, "    V_BATCH_CONTROL := V_DB || '.META.LIM_BATCH_CTRL';", "    V_BATCH_CONTROL := V_DB || '.META.LIM_BATCH_CTRL';\n    V_ARCHIVE := V_DB || '.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE';")
    old_cleanup = """        IF (V_FULL_RELOAD) THEN
            DELETE FROM IDENTIFIER(:V_BATCH_CONTROL)
             WHERE SPLIT_PART(UPPER(DW_TABLE_NM), '.', -2) = 'RAW_LIM'
               AND SPLIT_PART(UPPER(DW_TABLE_NM), '.', -1) = :V_TARGET_TABLE_SHORT;
            V_LIM_BATCH_CTRL_DELETED := SQLROWCOUNT;
        END IF;"""
    body = replace_once(body, old_cleanup, """        DELETE FROM IDENTIFIER(:V_BATCH_CONTROL)
         WHERE UPPER(DW_TABLE_NM) IN ('RAW_LIM.' || :V_TARGET_TABLE_SHORT, :V_TARGET_TABLE)
           AND (:V_FULL_RELOAD OR DW_FILE_NM IN (SELECT FILE_NM FROM IDENTIFIER(:V_FILES_TABLE)));
        V_LIM_BATCH_CTRL_DELETED := SQLROWCOUNT;

        DELETE FROM IDENTIFIER(:V_ARCHIVE)
         WHERE SUBJECT_AREA = :V_LIM_FORMAT
           AND (:V_FULL_RELOAD OR DW_FILE_NM IN (SELECT FILE_NM FROM IDENTIFIER(:V_FILES_TABLE)));
        V_CONTROL_ARCHIVE_DELETED := SQLROWCOUNT;""")
    old_ready = """    -- RAW_LIM_SET_READY_SELECTED_FILES_V1
    IF (V_SET_READY_TO_LOAD) THEN
        UPDATE IDENTIFIER(:V_TARGET_TABLE)
           SET DW_READY_TO_LOAD_FL = TRUE
         WHERE DW_FILE_NM IN (SELECT FILE_NM FROM IDENTIFIER(:V_FILES_TABLE))
           AND COALESCE(DW_READY_TO_LOAD_FL, FALSE) = FALSE;
        V_ROWS_READY_UPDATED := SQLROWCOUNT;
    END IF;"""
    body = replace_once(body, old_ready, """    -- Explicit bypass registers only selected, present, nonempty payload files.
    -- Empty/control files never enter the dbt payload queue.
    IF (V_SET_READY_TO_LOAD) THEN
        MERGE INTO IDENTIFIER(:V_BATCH_CONTROL) tgt
        USING (
            SELECT DISTINCT 'RAW_LIM.' || :V_TARGET_TABLE_SHORT AS DW_TABLE_NM,
                   raw.DW_FILE_NM, LEFT(REGEXP_SUBSTR(raw.DW_FILE_NM, '[^/]+$'), 3) AS DW_SRC_SYST
            FROM IDENTIFIER(:V_TARGET_TABLE) raw
            JOIN IDENTIFIER(:V_FILES_TABLE) selected ON selected.FILE_NM = raw.DW_FILE_NM
            WHERE SUBSTR(REGEXP_SUBSTR(raw.DW_FILE_NM, '[^/]+$'), 8, 3) <> '000'
              AND LEFT(TRIM(raw.DATA), 2) = '10'
        ) files
        ON UPPER(tgt.DW_TABLE_NM) IN (files.DW_TABLE_NM, :V_TARGET_TABLE)
           AND tgt.DW_FILE_NM = files.DW_FILE_NM AND tgt.DW_SRC_SYST = files.DW_SRC_SYST
        WHEN NOT MATCHED THEN INSERT (
            READY_TO_LOAD_DTTM, DW_TABLE_NM, DW_FILE_NM, DW_SRC_SYST, PROCESSED_FL, DELETED_FL
        ) VALUES (SYSDATE(), files.DW_TABLE_NM, files.DW_FILE_NM, files.DW_SRC_SYST, FALSE, FALSE);
        V_FILES_REGISTERED := SQLROWCOUNT;
    END IF;""")
    body = replace_once(body, "        'READY_ROWS_UPDATED', V_ROWS_READY_UPDATED,", "        'READY_ROWS_UPDATED', V_ROWS_READY_UPDATED,\n        'FILES_REGISTERED', V_FILES_REGISTERED,\n        'CONTROL_ARCHIVE_DELETED', V_CONTROL_ARCHIVE_DELETED,")
    return body


def render(name, source):
    if MARKER in source:
        return source
    expected = TARGETS[name][1]
    if hashlib.sha256(source.encode()).hexdigest() != expected:
        raise ValueError("Live definition drift: " + TARGETS[name][0])
    header, body = procedure_parts(source)
    body = {"refresh": render_refresh, "ready": render_ready, "loader": render_loader}[name](header, body)
    variable = "V_DB" if name == "loader" else "V_DATABASE"
    # The DEV-only migration must not be usable to mutate TST through its optional parameter.
    if name != "ready":
        body = replace_once(body, f"{variable} IS NULL OR NOT REGEXP_LIKE({variable}, '^[A-Z_][A-Z0-9_$]*$')",
                            f"{variable} IS NULL OR {variable} <> 'KUMO_DEV'")
    return assemble(header, "\n-- " + MARKER + "\n" + body.lstrip("\n"))


def run(apply=False):
    from snowflake.connector import DictCursor
    changed = []
    try:
        with connect() as conn, conn.cursor(DictCursor) as cur:
            cur.execute("ALTER SESSION SET QUERY_TAG='KUMO_DEV_LIM_READINESS_MIGRATION'")
            sources, statements = {}, {}
            for name, (signature, _) in TARGETS.items():
                cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (signature,))
                sources[name] = cur.fetchone()["DDL"]
                statements[name] = render(name, sources[name])
            SQL_FILE.write_text("-- Prepared by apply_dev_lim_readiness.py; DEV only.\n"
                                "-- Procedure definitions only; no loader/workflow invocation.\n\n"
                                + "\n".join(sql.replace("\nRETURNS ", "\nCOPY GRANTS\nRETURNS ", 1)
                                            for sql in statements.values()))
            if not apply:
                print("Prepared:", SQL_FILE)
                return
            for name, (signature, _) in TARGETS.items():
                if MARKER in sources[name]:
                    continue
                cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (signature,))
                if cur.fetchone()["DDL"] != sources[name]:
                    raise ValueError("Definition changed during preparation: " + signature)
                statement = statements[name].replace("\nRETURNS ", "\nCOPY GRANTS\nRETURNS ", 1)
                cur.execute(statement)
                changed.append(signature)
                cur.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (signature,))
                if procedure_parts(cur.fetchone()["DDL"])[1].strip() != procedure_parts(statements[name])[1].strip():
                    raise RuntimeError("Deployed body differs: " + signature)
                print("Updated:", signature, flush=True)
            (ROOT / "review/dev-lim-readiness-deployment.json").write_text(json.dumps({
                "updated": changed, "procedures_verified": list(TARGETS),
                "existing_data_changed": False, "workflow_or_loader_called": False,
                "task_state_changed": False,
            }, indent=2) + "\n")
    finally:
        if changed:
            subprocess.run([sys.executable, str(ROOT / "snowflake/export_ddl.py")], check=True, cwd=ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    run(parser.parse_args().apply)
