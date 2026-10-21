"""Explicit live check of readiness logging with separate monitor/data schemas.

Only disposable fixture tables/procedures are used; no existing data is copied
and no production readiness procedure, loader, workflow or task is executed.
"""
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
from migration_support import connect, procedure_parts
from apply_workflow_command_templates import logical_body
from snowflake.connector import DictCursor


def run():
    suffix = uuid.uuid4().hex[:10].upper()
    monitor = 'KUMO_DEV.READY_LOG_CHECK_' + suffix
    data = monitor + '_DATA'
    environment = dict(DATABASE='KUMO_DEV', MONITOR_APP_SCHEMA=monitor.split('.')[1],
                       META_SCHEMA=monitor.split('.')[1], APP_SCHEMA=data.split('.')[1],
                       RAW_SCHEMA=monitor.split('.')[1])
    created = []
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_READY_LOG_SCHEMA_CHECK'")
        try:
            for schema in (monitor, data):
                cursor.execute('CREATE SCHEMA ' + schema)
                created.append(schema)
            for table in ('_TMP_SET_READY_LOG', '_SET_READY_LOG', 'RAW_LIM_META',
                          'RAW_LIM_PKG_GROUP_SOURCE', 'RAW_LIM_CONTROL_ARCHIVE'):
                cursor.execute(f'CREATE TABLE {monitor}.{table} LIKE KUMO_DEV.MONITOR_META.{table}')
            cursor.execute("SELECT GET_DDL('TABLE','KUMO_DEV.META.LIM_BATCH_CTRL',TRUE) AS DDL")
            cursor.execute(cursor.fetchone()['DDL'].replace('KUMO_DEV.META.LIM_BATCH_CTRL', data + '.LIM_BATCH_CTRL'))
            cursor.execute(f'CREATE TABLE {monitor}.RAW_LIM_ZZRD (DW_FILE_NM VARCHAR, DW_FILE_DTTM TIMESTAMP_NTZ, DW_FILE_CHECK_SUM VARCHAR, DW_FILE_ROW_NUMBER NUMBER, DW_LOAD_DTTM TIMESTAMP_NTZ, DATA VARCHAR)')
            # A fixed fixture environment isolates both the data and monitor paths.
            cursor.execute(f'CREATE PROCEDURE {monitor}.SP_GET_ENVIRONMENT() RETURNS VARIANT LANGUAGE JAVASCRIPT AS $$ return ' + json.dumps(environment) + '; $$')
            for name, args in [('SP_RAW_LIM_META_REFRESH', '(VARCHAR,VARCHAR)'),
                               ('SP_RAW_LIM_SET_READY', '(VARCHAR,BOOLEAN,BOOLEAN,VARCHAR)')]:
                original = 'KUMO_DEV.MONITOR_APP.' + name
                cursor.execute("SELECT GET_DDL('PROCEDURE',%s,TRUE) AS DDL", (original + args,))
                ddl = cursor.fetchone()['DDL']
                header, _ = procedure_parts(ddl)
                body = logical_body(ddl).replace('KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT', monitor + '.SP_GET_ENVIRONMENT')
                cursor.execute(header.replace(original, monitor + '.' + name, 1) + '\nAS $$' + body + '$$')
            cursor.execute(f"INSERT INTO {monitor}.RAW_LIM_PKG_GROUP_SOURCE (PKG_GROUP_NAME,SUBJECT_AREA,SOURCE_ID,PKG_ID,ACTIVE_FL) VALUES ('TEST','ZZRD','AAA','001',TRUE)")
            for number, records in [(1, ['10 payload']),
                                    (0, ['10AAAZZRD0012026000010010012026-09-302026-10-01'])]:
                filename = f'AAAZZRD{number:03}_001_001_0000_202600001_20261001_fixture.gz'
                lines = ['00 header'] + records + ['99 footer ' + str(len(records))]
                cursor.executemany(f'INSERT INTO {monitor}.RAW_LIM_ZZRD VALUES (%s,SYSDATE(),%s,%s,SYSDATE(),%s)',
                                   [(filename, 'fixture-' + filename, i, line) for i, line in enumerate(lines, 1)])
            cursor.execute(f"CALL {monitor}.SP_RAW_LIM_META_REFRESH('ZZRD')")
            cursor.fetchall()
            cursor.execute(f"CALL {monitor}.SP_RAW_LIM_SET_READY('TEST',FALSE,FALSE)")
            result = cursor.fetchall()
            assert len(result) == 1 and result[0]['STATUS'] == 'UPDATED', result
            cursor.execute(f'SELECT COUNT(*) AS N FROM {monitor}._TMP_SET_READY_LOG')
            assert cursor.fetchone()['N'] == 1
            cursor.execute(f'SELECT COUNT(*) AS N FROM {data}.LIM_BATCH_CTRL')
            assert cursor.fetchone()['N'] == 1
            cursor.execute(f"CALL {monitor}.SP_RAW_LIM_SET_READY('TEST',FALSE,FALSE)")
            result = cursor.fetchall()
            assert len(result) == 1 and result[0]['STATUS'] == 'PREVIOUSLY_LOADED', result
            cursor.execute(f'SELECT COUNT(*) AS N FROM {monitor}._SET_READY_LOG')
            assert cursor.fetchone()['N'] == 1
            # The data schema has no log table: old APP_SCHEMA routing would fail.
            cursor.execute(f"SELECT COUNT(*) AS N FROM KUMO_DEV.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='{data.split('.')[1]}' AND TABLE_NAME IN ('_TMP_SET_READY_LOG','_SET_READY_LOG')")
            assert cursor.fetchone()['N'] == 0
            print('PASS: refresh and readiness write the current log and archive history in META_SCHEMA, with APP_SCHEMA separate', flush=True)
        finally:
            for schema in reversed(created):
                cursor.execute('DROP SCHEMA ' + schema + ' CASCADE')
            print('Removed isolated readiness fixture schemas', flush=True)


if __name__ == '__main__':
    run()
