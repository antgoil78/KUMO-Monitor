"""Exercise the DEV readiness procedures in an isolated, disposable DEV schema.

Explicitly run this file; normal unittest discovery never connects to Snowflake.
No production loader, workflow or task is called. No existing data is copied.
"""

from datetime import date
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "migrations"))
from migration_support import connect, procedure_parts
import apply_dev_lim_readiness as migration


def run():
    from snowflake.connector import DictCursor
    schema = "READINESS_TEST_" + uuid.uuid4().hex[:12].upper()
    namespace = "KUMO_DEV." + schema
    results = []
    with connect() as conn, conn.cursor(DictCursor) as cur:
        cur.execute("ALTER SESSION SET QUERY_TAG='KUMO_DEV_LIM_READINESS_ISOLATED_TEST', STATEMENT_TIMEOUT_IN_SECONDS=90")
        cur.execute("CREATE SCHEMA " + namespace)
        try:
            for table, source in {
                "RAW_LIM_META": "MONITOR_META.RAW_LIM_META",
                "RAW_LIM_PKG_GROUP_SOURCE": "MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE",
                "_TMP_SET_READY_LOG": "MONITOR_META._TMP_SET_READY_LOG",
                "_SET_READY_LOG": "MONITOR_META._SET_READY_LOG",
                "RAW_LIM_CONTROL_ARCHIVE": "MONITOR_META.RAW_LIM_CONTROL_ARCHIVE",
            }.items():
                cur.execute(f"CREATE TABLE {namespace}.{table} LIKE KUMO_DEV.{source}")
            cur.execute("SELECT GET_DDL('TABLE','KUMO_DEV.META.LIM_BATCH_CTRL',TRUE) AS DDL")
            cur.execute(cur.fetchone()["DDL"].replace("KUMO_DEV.META.LIM_BATCH_CTRL", namespace + ".LIM_BATCH_CTRL"))
            cur.execute(f"CREATE TABLE {namespace}.RAW_LIM_ZZRD (DW_FILE_NM VARCHAR, DW_FILE_DTTM TIMESTAMP_NTZ, DW_FILE_CHECK_SUM VARCHAR, DW_FILE_ROW_NUMBER NUMBER, DW_LOAD_DTTM TIMESTAMP_NTZ, DATA VARCHAR)")

            for name, (signature, _) in migration.TARGETS.items():
                cur.execute("SELECT GET_DDL('PROCEDURE',%s,TRUE) AS DDL", (signature,))
                statement = migration.render(name, cur.fetchone()["DDL"])
                for old in ("MONITOR_APP", "MONITOR_META", "META", "RAW_LIM"):
                    statement = statement.replace("." + old + ".", "." + schema + ".")
                cur.execute(statement)
                print("Compiled isolated procedure:", name, flush=True)

            def scalar(sql, params=None):
                cur.execute(sql, params or {})
                return next(iter(cur.fetchone().values()))

            def check(name, condition):
                if not condition:
                    raise AssertionError(name)
                results.append(name)
                print("Passed:", name, flush=True)

            def reset():
                for table in ("RAW_LIM_ZZRD", "RAW_LIM_META", "RAW_LIM_PKG_GROUP_SOURCE",
                              "_TMP_SET_READY_LOG", "_SET_READY_LOG", "LIM_BATCH_CTRL", "RAW_LIM_CONTROL_ARCHIVE"):
                    cur.execute(f"DELETE FROM {namespace}.{table}")
                cur.execute(f"INSERT INTO {namespace}.RAW_LIM_PKG_GROUP_SOURCE (PKG_GROUP_NAME,SUBJECT_AREA,SOURCE_ID,PKG_ID,ACTIVE_FL) VALUES ('TEST','ZZRD','AAA','001',TRUE)")

            def filename(number, sequence=1, end="20261001", source="AAA"):
                return f"{source}ZZRD{number:03}_001_001_0000_2026{sequence:05}_{end}_fixture.gz"

            def put_file(number, records, sequence=1, end="20261001", footer=None, source="AAA"):
                name = filename(number, sequence, end, source)
                rows = ["00 header"] + records + ["99 footer " + str(len(records) if footer is None else footer)]
                for row_number, data in enumerate(rows, 1):
                    cur.execute(f"INSERT INTO {namespace}.RAW_LIM_ZZRD VALUES (%s,SYSDATE(),%s,%s,SYSDATE(),%s)",
                                (name, "checksum-" + name, row_number, data))
                return name

            def delivery(payloads, sequence=1, end="20261001", source="AAA"):
                details = []
                for number, records in payloads:
                    put_file(number, records, sequence, end, source=source)
                    details.append("10" + source + "ZZRD0012026" + f"{sequence:05}{number:03}001"
                                   + "2026-09-30" + str(date(int(end[:4]), int(end[4:6]), int(end[6:8]))))
                put_file(0, details, sequence, end, source=source)

            def refresh():
                cur.execute(f"CALL {namespace}.SP_RAW_LIM_META_REFRESH('ZZRD')")
                cur.fetchall()

            def ready(ignore_group=False, ignore_sequence=False):
                cur.execute(f"CALL {namespace}.SP_RAW_LIM_SET_READY('TEST',%s,%s)", (ignore_group, ignore_sequence))
                return cur.fetchall()

            reset()
            delivery([(1, ["10 payload"]), (2, [])])
            refresh()
            rows = ready()
            check("valid mixed delivery", len(rows) == 1 and rows[0]["STATUS"] == "UPDATED")
            check("only nonempty payload registered", scalar(f"SELECT COUNT(*) FROM {namespace}.LIM_BATCH_CTRL") == 1)
            check("exact control archived and removed", scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_CONTROL_ARCHIVE") == 4
                  and scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_ZZRD WHERE SUBSTR(DW_FILE_NM,8,3)='000'") == 0)
            check("empty payload accepted in metadata", scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_META WHERE FILE_TYPE='PAYLOAD' AND DATA_ROWS=0 AND DW_READY_TO_LOAD_FL") == 1)
            cur.execute(f"UPDATE {namespace}.LIM_BATCH_CTRL SET PROCESSED_FL=TRUE,DW_BATCH_ID='preserve-me',PROCESSED_DTTM='2026-10-01'::TIMESTAMP_NTZ")
            rows = ready()
            check("rerun accepts complete delivery", rows[0]["STATUS"] == "PREVIOUSLY_LOADED")
            check("processed batch preserved", scalar(f"SELECT COUNT(*) FROM {namespace}.LIM_BATCH_CTRL WHERE PROCESSED_FL AND DW_BATCH_ID='preserve-me' AND PROCESSED_DTTM='2026-10-01'::TIMESTAMP_NTZ") == 1)
            check("control archive idempotent", scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_CONTROL_ARCHIVE") == 4)
            refresh()
            check("zero-candidate refresh removes stale snapshot", scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_META") == 0)

            delivery([(1, [])], sequence=2, end="20261002")
            refresh()
            rows = ready()
            check("entirely empty delivery accepted", rows[0]["STATUS"] == "UPDATED" and rows[0]["ROWS_UPDATED"] == 0)
            check("empty delivery creates no queue entries", scalar(f"SELECT COUNT(*) FROM {namespace}.LIM_BATCH_CTRL") == 1)
            check("empty delivery advances sequence", scalar(f"SELECT DLVY_PKG_YEAR_SEQ_NO FROM {namespace}.RAW_LIM_PKG_GROUP_SOURCE") == "00002")

            reset()
            delivery([(1, ["10 accepted"]), (2, ["10 missing"])])
            cur.execute(f"DELETE FROM {namespace}.RAW_LIM_ZZRD WHERE DW_FILE_NM=%s", (filename(2),))
            cur.execute(f"INSERT INTO {namespace}.LIM_BATCH_CTRL (READY_TO_LOAD_DTTM,DW_TABLE_NM,DW_FILE_NM,DW_SRC_SYST) VALUES (SYSDATE(),'RAW_LIM.RAW_LIM_ZZRD',%s,'AAA')", (filename(1),))
            delivery([(1, ["10 later"])], sequence=2, end="20261002")
            refresh()
            rows = ready()
            check("one registered file does not skip missing sibling", [r["STATUS"] for r in rows] == ["STOPPED", "BLOCKED"])
            check("failed delivery preserves controls and sequence", scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_CONTROL_ARCHIVE") == 0
                  and scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_PKG_GROUP_SOURCE WHERE DLVY_PKG_YEAR IS NOT NULL") == 0)

            reset()
            delivery([(1, ["10 payload"])])
            cur.execute(f"UPDATE {namespace}.RAW_LIM_ZZRD SET DATA='99 footer 2' WHERE DW_FILE_NM=%s AND LEFT(DATA,2)='99'", (filename(1),))
            refresh()
            check("row-count mismatch blocks registration", ready()[0]["STATUS"] == "STOPPED"
                  and scalar(f"SELECT COUNT(*) FROM {namespace}.LIM_BATCH_CTRL") == 0)

            reset()
            delivery([(1, ["10 payload"])], sequence=3)
            cur.execute(f"UPDATE {namespace}.RAW_LIM_PKG_GROUP_SOURCE SET DLVY_PKG_YEAR='2026',DLVY_PKG_YEAR_SEQ_NO='00001'")
            refresh()
            check("sequence gap blocks", ready()[0]["STATUS"] == "STOPPED")
            check("explicit sequence override retained", ready(ignore_sequence=True)[0]["STATUS"] == "UPDATED")

            reset()
            delivery([(1, ["10 payload"])])
            cur.execute(f"INSERT INTO {namespace}.RAW_LIM_PKG_GROUP_SOURCE (PKG_GROUP_NAME,SUBJECT_AREA,SOURCE_ID,PKG_ID,ACTIVE_FL) VALUES ('TEST','ZZRD','BBB','001',TRUE)")
            refresh()
            check("missing group source blocks", ready()[0]["STATUS"] == "STOPPED")
            check("explicit group override retained", ready(ignore_group=True)[0]["STATUS"] == "UPDATED")

            reset()
            delivery([(1, ["10 payload"])])
            refresh()
            cur.execute(f"DELETE FROM {namespace}.RAW_LIM_ZZRD WHERE DW_FILE_NM=%s", (filename(1),))
            try:
                ready()
            except Exception as exc:
                check("disappearing payload raises", "disappeared" in str(exc))
            else:
                raise AssertionError("Expected disappearing payload failure")
            check("registration failure rolls back success log and sequence", scalar(f"SELECT COUNT(*) FROM {namespace}._TMP_SET_READY_LOG") == 0
                  and scalar(f"SELECT COUNT(*) FROM {namespace}.LIM_BATCH_CTRL") == 0
                  and scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_PKG_GROUP_SOURCE WHERE DLVY_PKG_YEAR IS NOT NULL") == 0)

            reset()
            delivery([(1, ["10 payload"])])
            refresh()
            cur.execute(f"ALTER TABLE {namespace}.RAW_LIM_CONTROL_ARCHIVE ALTER COLUMN DW_BATCH_ID SET NOT NULL")
            try:
                ready()
            except Exception:
                check("archive failure rolls back registration", scalar(f"SELECT COUNT(*) FROM {namespace}.LIM_BATCH_CTRL") == 0
                      and scalar(f"SELECT COUNT(*) FROM {namespace}._TMP_SET_READY_LOG") == 0)
            else:
                raise AssertionError("Expected archive constraint failure")

            receipt = {"passed": results, "schema_cleaned_up": True,
                       "production_data_changed": False, "production_workflows_or_loaders_called": False}
        finally:
            cur.execute("DROP SCHEMA IF EXISTS " + namespace + " CASCADE")
            print("Removed isolated schema:", namespace, flush=True)
    (migration.ROOT / "review/dev-lim-readiness-live-tests.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    run()
