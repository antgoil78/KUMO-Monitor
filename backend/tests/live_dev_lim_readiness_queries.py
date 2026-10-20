"""Test DEV monitor queries and new reload DML against disposable fixtures."""

import json
from pathlib import Path
import sys
from unittest.mock import patch
import uuid

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "migrations")]
from apply_dev_workbook_objects import connect, procedure_parts
import apply_dev_lim_readiness as migration
import config
import lim_readiness


def run():
    from snowflake.connector import DictCursor
    namespace = "KUMO_DEV.READINESS_QUERY_TEST_" + uuid.uuid4().hex[:10].upper()
    results = []
    with connect() as conn, conn.cursor(DictCursor) as cur:
        cur.execute("ALTER SESSION SET QUERY_TAG='KUMO_DEV_LIM_READINESS_QUERY_TEST', STATEMENT_TIMEOUT_IN_SECONDS=90")
        cur.execute("CREATE SCHEMA " + namespace)
        try:
            for table, source in {
                "RAW_LIM_META": "MONITOR_META.RAW_LIM_META",
                "RAW_LIM_PKG_GROUP_SOURCE": "MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE",
                "_TMP_SET_READY_LOG": "META._TMP_SET_READY_LOG",
                "_SET_READY_LOG": "MONITOR_META._SET_READY_LOG",
                "RAW_LIM_CONTROL_ARCHIVE": "MONITOR_META.RAW_LIM_CONTROL_ARCHIVE",
                "LIM_BATCH_CTRL": "META.LIM_BATCH_CTRL",
            }.items():
                # Standard LIKE suffices here; the procedure integration suite
                # separately exercises transactions with an actual hybrid table.
                cur.execute("SELECT GET_DDL('TABLE',%s,TRUE) AS DDL", ("KUMO_DEV." + source,))
                ddl = cur.fetchone()["DDL"].replace("KUMO_DEV." + source, namespace + "." + table)
                cur.execute(ddl)
            cur.execute(f"CREATE TABLE {namespace}.RAW_LIM_ZZRD (DW_FILE_NM VARCHAR,DW_FILE_DTTM TIMESTAMP_NTZ,DW_FILE_CHECK_SUM VARCHAR,DW_FILE_ROW_NUMBER NUMBER,DW_LOAD_DTTM TIMESTAMP_NTZ,DATA VARCHAR)")
            cur.execute(f"CREATE TABLE {namespace}.SELECTED_FILES (FILE_NM VARCHAR)")
            cur.execute(f"INSERT INTO {namespace}.RAW_LIM_PKG_GROUP_SOURCE (PKG_GROUP_NAME,SUBJECT_AREA,SOURCE_ID,PKG_ID,ACTIVE_FL) VALUES ('TEST','ZZRD','AAA','001',TRUE)")
            names = {key: f"AAAZZRD{number:03}_001_001_0000_202600001_20261001_{key}.gz"
                     for key, number in [('nonempty', 1), ('variant', 1), ('empty', 2), ('bad_empty', 3), ('control', 0)]}
            for key, name in names.items():
                records = ["00 header"]
                if key in ('nonempty', 'variant', 'control'):
                    records.append("10 payload")
                if key != 'bad_empty':
                    records.append("99 footer " + str(len(records) - 1))
                for index, data in enumerate(records, 1):
                    cur.execute(f"INSERT INTO {namespace}.RAW_LIM_ZZRD VALUES (%s,SYSDATE(),'fixture',%s,SYSDATE(),%s)", (name, index, data))
                cur.execute(f"INSERT INTO {namespace}.RAW_LIM_META (RAW_TABLE,ORIGINAL_FILE_NAME,FILE_NAME,FILE_KEY,FILE_TYPE,PKG_GROUP_NAME,DLVY_SOURCE_ID,DLVY_SUBJECT_AREA_ID,DLVY_PKG_ID,DLVY_PKG_YEAR,DLVY_PKG_YEAR_SEQ_NO,DLVY_END_DATE,DATA_ROWS,ROWCOUNT_STATUS,RECEIVED_FL,DW_READY_TO_LOAD_FL,READY_STATUS,RUN_DTTM) VALUES ('KUMO_DEV.RAW_LIM.RAW_LIM_ZZRD',%s,%s,%s,%s,'TEST','AAA','ZZRD','001','2026','00001','2026-10-01',%s,%s,TRUE,FALSE,'FILE_VALIDATION_OK','2026-10-01'::TIMESTAMP_NTZ)",
                            (name, name[:42], name[:42], 'CONTROL' if key == 'control' else 'PAYLOAD', 1 if key in ('nonempty', 'variant', 'control') else 0, 'NO_FOOTER' if key == 'bad_empty' else 'ROWCOUNT_OK'))
            cur.execute(f"INSERT INTO {namespace}.LIM_BATCH_CTRL (READY_TO_LOAD_DTTM,DW_TABLE_NM,DW_FILE_NM,DW_SRC_SYST) VALUES (SYSDATE(),'RAW_LIM.RAW_LIM_ZZRD',%s,'AAA')", (names['nonempty'],))

            settings = dict(LIM_DATABASE="KUMO_DEV", LIM_BATCH_CTRL_TABLE=namespace + ".LIM_BATCH_CTRL",
                            RAW_LIM_META_TABLE=namespace + ".RAW_LIM_META", METADATA_NAMESPACE=namespace,
                            T_RAW_LIM_PKG_GROUP_SOURCE=namespace + ".RAW_LIM_PKG_GROUP_SOURCE",
                            SET_READY_LATEST_TABLE=namespace + "._TMP_SET_READY_LOG", SET_READY_HISTORY_TABLE=namespace + "._SET_READY_LOG")
            with patch.multiple(config, **settings):
                raw_query = lim_readiness.raw_file_readiness_sql("ZZRD").replace("FROM KUMO_DEV.RAW_LIM.RAW_LIM_ZZRD", "FROM " + namespace + ".RAW_LIM_ZZRD")
                def check(label, condition):
                    if not condition:
                        raise AssertionError(label)
                    results.append(label)
                    print("Passed:", label, flush=True)

                cur.execute(raw_query)
                ready = {r['DW_FILE_NM']: r['FILE_READY'] for r in cur.fetchall()}
                check("raw status uses exact registration", ready[names['nonempty']] and not ready[names['variant']])
                check("empty file requires acceptance history", not ready[names['empty']])
                cur.execute(f"INSERT INTO {namespace}._SET_READY_LOG VALUES ('TEST','2026-10-01','AAA','001','2026','00001','UPDATED',1,NULL,SYSDATE())")
                cur.execute(raw_query)
                ready = {r['DW_FILE_NM']: r['FILE_READY'] for r in cur.fetchall()}
                check("valid empty file derives acceptance from history", ready[names['empty']])
                check("empty file without footer remains unaccepted", not ready[names['bad_empty']])
                cur.execute(lim_readiness.metadata_detail_sql(), {'group_name': 'TEST'})
                detail = {r['ORIGINAL_FILE_NAME']: r for r in cur.fetchall()}
                check("detail status uses exact filename and current derived value", detail[names['nonempty']]['DW_READY_TO_LOAD_FL']
                      and detail[names['nonempty']]['READY_STATUS'] == 'READY_TO_LOAD'
                      and not detail[names['variant']]['DW_READY_TO_LOAD_FL'])
                check("detail preserves empty-file acceptance", detail[names['empty']]['DW_READY_TO_LOAD_FL'] and not detail[names['bad_empty']]['DW_READY_TO_LOAD_FL'])

                cur.execute("SELECT GET_DDL('PROCEDURE',%s,TRUE) AS DDL", (migration.TARGETS['loader'][0],))
                body = procedure_parts(migration.render('loader', cur.fetchone()['DDL']))[1]
                block = body[body.index('    -- Explicit bypass'):body.index('    -- 18. Commit')]
                # Select the registered file, one empty payload and a control.
                # Unselected nonempty variants must not enter the load queue.
                for key in ('nonempty', 'empty', 'control'):
                    cur.execute(f"INSERT INTO {namespace}.SELECTED_FILES VALUES (%s)", (names[key],))
                cur.execute(f"DELETE FROM {namespace}.LIM_BATCH_CTRL")
                declarations = f"""DECLARE V_TARGET_TABLE_SHORT VARCHAR DEFAULT 'RAW_LIM_ZZRD';
                    V_TARGET_TABLE VARCHAR DEFAULT '{namespace}.RAW_LIM_ZZRD';
                    V_BATCH_CONTROL VARCHAR DEFAULT '{namespace}.LIM_BATCH_CTRL';
                    V_FILES_TABLE VARCHAR DEFAULT '{namespace}.SELECTED_FILES';
                    V_SET_READY_TO_LOAD BOOLEAN DEFAULT TRUE;
                    V_FILES_REGISTERED NUMBER DEFAULT 0;
                BEGIN """
                cur.execute("EXECUTE IMMEDIATE $$" + declarations + block + "RETURN V_FILES_REGISTERED; END;$$")
                check("bypass registers selected nonempty payloads only", next(iter(cur.fetchone().values())) == 1)
                cur.execute(f"UPDATE {namespace}.LIM_BATCH_CTRL SET PROCESSED_FL=TRUE,DW_BATCH_ID='keep'")
                cur.execute("EXECUTE IMMEDIATE $$" + declarations + block + "RETURN V_FILES_REGISTERED; END;$$")
                check("bypass rerun does not reopen processed records", next(iter(cur.fetchone().values())) == 0)
                cur.execute(f"INSERT INTO {namespace}.LIM_BATCH_CTRL (READY_TO_LOAD_DTTM,DW_TABLE_NM,DW_FILE_NM,DW_SRC_SYST) VALUES (SYSDATE(),'RAW_LIM.RAW_LIM_ZZRD',%s,'AAA')", (names['variant'],))
                cleanup = body[body.index('        DELETE FROM IDENTIFIER(:V_BATCH_CONTROL)'):body.index('        -- 12d. Delete RAW_LOAD_RESULTS')]
                declarations = declarations.replace('V_SET_READY_TO_LOAD BOOLEAN DEFAULT TRUE;',
                    f"V_FULL_RELOAD BOOLEAN DEFAULT FALSE; V_LIM_FORMAT VARCHAR DEFAULT 'ZZRD'; V_ARCHIVE VARCHAR DEFAULT '{namespace}.RAW_LIM_CONTROL_ARCHIVE'; V_LIM_BATCH_CTRL_DELETED NUMBER; V_CONTROL_ARCHIVE_DELETED NUMBER;")
                cur.execute("EXECUTE IMMEDIATE $$" + declarations + cleanup + "RETURN V_LIM_BATCH_CTRL_DELETED; END;$$")
                check("partial reload resets only selected batch records", next(iter(cur.fetchone().values())) == 1)
                cur.execute(f"SELECT DW_FILE_NM FROM {namespace}.LIM_BATCH_CTRL")
                check("partial reload preserves unselected files", [r['DW_FILE_NM'] for r in cur.fetchall()] == [names['variant']])
        finally:
            cur.execute("DROP SCHEMA IF EXISTS " + namespace + " CASCADE")
            print("Removed isolated schema:", namespace, flush=True)
    (migration.ROOT / "review/dev-lim-readiness-query-tests.json").write_text(json.dumps({
        "passed": results, "schema_cleaned_up": True, "production_data_changed": False,
        "production_loader_called": False,
    }, indent=2) + "\n")


if __name__ == "__main__":
    run()
