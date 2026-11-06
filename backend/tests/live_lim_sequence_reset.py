"""Explicit, isolated regression checks of the saved LIM sequence procedures.

Uses disposable synthetic fixtures in the selected database. Does not execute
an existing workflow or change existing data, configuration, or procedures.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
from migration_support import connect
from apply_lim_sequence_reset import definitions, identifier_path
from snowflake.connector import DictCursor


def run(database):
    database = identifier_path(database, 1)
    schema = 'LIM_SEQUENCE_TEST_' + uuid.uuid4().hex[:10].upper()
    namespace = database + '.' + schema
    environment = dict(DATABASE=database, MONITOR_APP_SCHEMA=schema, META_SCHEMA=schema,
                       APP_SCHEMA=schema, RAW_SCHEMA=schema)
    passed = []
    with connect() as conn, conn.cursor(DictCursor) as cur:
        cur.execute("ALTER SESSION SET QUERY_TAG='KUMO_LIM_SEQUENCE_RESET_ISOLATED_TEST', STATEMENT_TIMEOUT_IN_SECONDS=120")
        cur.execute('CREATE SCHEMA ' + namespace)
        try:
            for table in ('RAW_LIM_META', 'RAW_LIM_PKG_GROUP_SOURCE', '_TMP_SET_READY_LOG',
                          '_SET_READY_LOG', 'RAW_LIM_CONTROL_ARCHIVE'):
                cur.execute(f'CREATE TABLE {namespace}.{table} LIKE {database}.MONITOR_META.{table}')
            cur.execute(f'CREATE TABLE {namespace}.LIM_BATCH_CTRL LIKE {database}.META.LIM_BATCH_CTRL')
            cur.execute(f'CREATE TABLE {namespace}.RAW_LIM_ZZRD (DW_FILE_NM VARCHAR,DW_FILE_DTTM TIMESTAMP_NTZ,DW_FILE_CHECK_SUM VARCHAR,DW_FILE_ROW_NUMBER NUMBER,DW_LOAD_DTTM TIMESTAMP_NTZ,DATA VARCHAR)')
            cur.execute(f'CREATE PROCEDURE {namespace}.SP_GET_ENVIRONMENT() RETURNS VARIANT LANGUAGE JAVASCRIPT AS $$ return ' + json.dumps(environment) + '; $$')
            for _, _, ddl in definitions(environment, namespace + '.UNUSED_PARAMETERS'):
                cur.execute(ddl)

            def scalar(sql, params=None):
                cur.execute(sql, params)
                return next(iter(cur.fetchone().values()))

            def check(name, condition):
                if not condition:
                    raise AssertionError(name)
                passed.append(name)
                print('PASS: ' + name, flush=True)

            def reset():
                for table in ('RAW_LIM_ZZRD', 'RAW_LIM_META', 'RAW_LIM_PKG_GROUP_SOURCE',
                              '_TMP_SET_READY_LOG', '_SET_READY_LOG', 'LIM_BATCH_CTRL', 'RAW_LIM_CONTROL_ARCHIVE'):
                    cur.execute(f'DELETE FROM {namespace}.{table}')
                cur.execute(f"INSERT INTO {namespace}.RAW_LIM_PKG_GROUP_SOURCE (PKG_GROUP_NAME,SUBJECT_AREA,SOURCE_ID,PKG_ID,ACTIVE_FL) VALUES ('TEST','ZZRD','AAA','001',TRUE)")

            def filename(number, sequence, end='20261021'):
                return f'AAAZZRD{number:03}_001_001_0000_2026{sequence:05}_{end}_fixture.gz'

            def delivery(sequence, payloads, end='20261021'):
                end_iso = end[:4] + '-' + end[4:6] + '-' + end[6:8]
                details = ['10AAAZZRD0012026' + f'{sequence:05}{number:03}0012026-10-20' + end_iso
                           for number, _ in payloads]
                for number, records in [(0, details), *payloads]:
                    name = filename(number, sequence, end)
                    lines = ['00 header', *records, '99 footer ' + str(len(records))]
                    cur.executemany(f'INSERT INTO {namespace}.RAW_LIM_ZZRD VALUES (%s,SYSDATE(),%s,%s,SYSDATE(),%s)',
                                    [(name, 'fixture-' + name, i, line) for i, line in enumerate(lines, 1)])

            def refresh():
                cur.execute(f"CALL {namespace}.SP_RAW_LIM_META_REFRESH('ZZRD')")
                cur.fetchall()

            def ready():
                cur.execute(f"CALL {namespace}.SP_RAW_LIM_SET_READY('TEST',FALSE,FALSE)")
                return cur.fetchall()

            reset()
            delivery(212, [(1, ['10 old'])])
            refresh()
            check('arbitrary initial sequence accepted', ready()[0]['STATUS'] == 'UPDATED')
            cur.execute(f"UPDATE {namespace}.LIM_BATCH_CTRL SET PROCESSED_FL=TRUE,DW_BATCH_ID='preserve-me'")
            cur.execute(f'UPDATE {namespace}.RAW_LIM_PKG_GROUP_SOURCE SET DLVY_PKG_YEAR=NULL,DLVY_PKG_YEAR_SEQ_NO=NULL')
            for table in ('_TMP_SET_READY_LOG', '_SET_READY_LOG'):
                cur.execute(f'DELETE FROM {namespace}.{table}')
            delivery(215, [(1, ['10 new'])])
            refresh()
            check('handled delivery remains visible without consuming reset', scalar(f'SELECT COUNT(*) FROM {namespace}.RAW_LIM_META WHERE DLVY_PKG_YEAR_SEQ_NO=\'00212\' AND DW_READY_TO_LOAD_FL AND IS_VALID_SEQUENCE') == 1)
            check('next unhandled delivery becomes first after reset', scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_META WHERE DLVY_PKG_YEAR_SEQ_NO='00215' AND IS_VALID_SEQUENCE") == 2)
            rows = ready()
            check('reset registers next delivery normally', {row['DLVY_PKG_YEAR_SEQ_NO']: row['STATUS'] for row in rows} == {'00212': 'PREVIOUSLY_LOADED', '00215': 'UPDATED'})
            check('new baseline persisted', scalar(f'SELECT DLVY_PKG_YEAR_SEQ_NO FROM {namespace}.RAW_LIM_PKG_GROUP_SOURCE') == '00215')
            check('old processed registration preserved', scalar(f"SELECT COUNT(*) FROM {namespace}.LIM_BATCH_CTRL WHERE PROCESSED_FL AND DW_BATCH_ID='preserve-me'") == 1)
            delivery(216, [(1, ['10 next'])], end='20261022')
            delivery(218, [(1, ['10 gap'])], end='20261023')
            refresh()
            rows = ready()
            check('normal sequence resumes and later gap stops', {row['DLVY_PKG_YEAR_SEQ_NO']: row['STATUS'] for row in rows} == {'00216': 'UPDATED', '00218': 'STOPPED'})
            check('gap does not advance baseline', scalar(f'SELECT DLVY_PKG_YEAR_SEQ_NO FROM {namespace}.RAW_LIM_PKG_GROUP_SOURCE') == '00216')

            reset()
            cur.execute(f"UPDATE {namespace}.RAW_LIM_PKG_GROUP_SOURCE SET DLVY_PKG_YEAR='2026',DLVY_PKG_YEAR_SEQ_NO=NULL")
            delivery(37, [(1, ['10 first'])])
            refresh()
            check('sequence-only NULL reset accepts arbitrary first value', ready()[0]['STATUS'] == 'UPDATED')
            check('sequence-only NULL reset persists baseline', scalar(f'SELECT DLVY_PKG_YEAR_SEQ_NO FROM {namespace}.RAW_LIM_PKG_GROUP_SOURCE') == '00037')

            reset()
            delivery(7, [(1, ['10 accepted']), (2, ['10 missing'])])
            cur.execute(f'DELETE FROM {namespace}.RAW_LIM_ZZRD WHERE DW_FILE_NM=%s', (filename(2, 7),))
            cur.execute(f"INSERT INTO {namespace}.LIM_BATCH_CTRL (READY_TO_LOAD_DTTM,DW_TABLE_NM,DW_FILE_NM,DW_SRC_SYST) VALUES (SYSDATE(),%s,%s,'AAA')", (schema + '.RAW_LIM_ZZRD', filename(1, 7)))
            delivery(10, [(1, ['10 later'])], end='20261022')
            refresh()
            check('partial delivery still participates in sequence ordering', scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_META WHERE DLVY_PKG_YEAR_SEQ_NO='00010' AND NOT IS_VALID_SEQUENCE") == 2)
            rows = ready()
            check('missing payload still blocks registration', {row['DLVY_PKG_YEAR_SEQ_NO']: row['STATUS'] for row in rows} == {'00007': 'STOPPED', '00010': 'BLOCKED'})

            reset()
            delivery(1, [(1, [])])
            refresh()
            check('empty delivery accepted', ready()[0]['STATUS'] == 'UPDATED')
            cur.execute(f'UPDATE {namespace}.RAW_LIM_PKG_GROUP_SOURCE SET DLVY_PKG_YEAR=NULL,DLVY_PKG_YEAR_SEQ_NO=NULL')
            delivery(3, [(1, ['10 after empty'])])
            refresh()
            check('accepted empty delivery does not consume reset', scalar(f"SELECT COUNT(*) FROM {namespace}.RAW_LIM_META WHERE DLVY_PKG_YEAR_SEQ_NO='00003' AND IS_VALID_SEQUENCE") == 2)
            rows = ready()
            check('empty acceptance and new first delivery preserved', {row['DLVY_PKG_YEAR_SEQ_NO']: row['STATUS'] for row in rows} == {'00001': 'PREVIOUSLY_LOADED', '00003': 'UPDATED'})

            reset()
            delivery(9, [(1, ['10 invalid footer'])])
            cur.execute(f"UPDATE {namespace}.RAW_LIM_ZZRD SET DATA='99 footer 2' WHERE DW_FILE_NM=%s AND LEFT(DATA,2)='99'", (filename(1, 9),))
            refresh()
            check('NULL baseline does not bypass row-count checks', ready()[0]['STATUS'] == 'STOPPED')
            check('invalid file does not establish baseline', scalar(f'SELECT DLVY_PKG_YEAR_SEQ_NO FROM {namespace}.RAW_LIM_PKG_GROUP_SOURCE') is None)
        finally:
            cur.execute('DROP SCHEMA ' + namespace + ' CASCADE')
            print('Removed isolated sequence fixture schema', flush=True)
    return {'passed': passed, 'existing_data_changed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='KUMO_DEV')
    print(json.dumps(run(parser.parse_args().database), indent=2))
