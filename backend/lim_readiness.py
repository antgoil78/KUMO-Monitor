"""DEV file acceptance queries; physical RAW tables contain no readiness flag."""

import re

import config


def _subject(value):
    value = str(value).upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,49}", value):
        raise ValueError("Invalid LIM subject")
    return value


def accepted_deliveries_sql():
    return f"""SELECT DISTINCT PKG_GROUP_NAME, DLVY_END_DATE, DLVY_SOURCE_ID,
                      DLVY_PKG_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO
               FROM (SELECT * FROM {config.SET_READY_LATEST_TABLE}
                     UNION ALL SELECT * FROM {config.SET_READY_HISTORY_TABLE})
               WHERE STATUS IN ('UPDATED', 'PREVIOUSLY_LOADED')"""


def raw_file_readiness_sql(subject):
    subject = _subject(subject)
    table = f"{config.LIM_DATABASE}.{config.RAW_SCHEMA}.RAW_LIM_{subject}"
    return f"""
        WITH physical_files AS (
            SELECT DW_FILE_NM, REGEXP_SUBSTR(DW_FILE_NM, '[^/]+$') AS BASE_NAME,
                   LEFT(BASE_NAME, 3) AS SOURCE_ID,
                   MAX(DW_LOAD_DTTM) AS FILE_LOAD_DTTM,
                   COUNT_IF(LEFT(TRIM(DATA), 2) = '10') AS DATA_ROWS,
                   COUNT_IF(LEFT(TRIM(DATA), 2) NOT IN ('00', '99')) AS ACTUAL_ROWS,
                   COUNT_IF(LEFT(TRIM(DATA), 2) = '99') AS FOOTERS,
                   MAX(IFF(LEFT(TRIM(DATA), 2) = '99',
                           TRY_TO_NUMBER(REGEXP_SUBSTR(TRIM(DATA), '[+-]?[0-9]+$')), NULL)) AS EXPECTED_ROWS
            FROM {table}
            WHERE DW_FILE_NM IS NOT NULL
              AND SUBSTR(REGEXP_SUBSTR(DW_FILE_NM, '[^/]+$'), 8, 3) <> '000'
            GROUP BY DW_FILE_NM
        ), registered AS (
            SELECT DISTINCT DW_FILE_NM, DW_SRC_SYST FROM {config.LIM_BATCH_CTRL_TABLE}
            WHERE UPPER(DW_TABLE_NM) IN ('{config.RAW_SCHEMA}.RAW_LIM_{subject}', '{table}')
        ), accepted AS ({accepted_deliveries_sql()})
        SELECT DISTINCT '{subject}' AS SUBJECT_AREA, files.SOURCE_ID, files.DW_FILE_NM,
               IFF(b.DW_FILE_NM IS NOT NULL OR
                   (files.DATA_ROWS = 0 AND files.ACTUAL_ROWS = 0 AND files.EXPECTED_ROWS = 0
                    AND files.FOOTERS > 0 AND a.DLVY_SOURCE_ID IS NOT NULL), TRUE, FALSE) AS FILE_READY,
               files.FILE_LOAD_DTTM
        FROM physical_files files
        LEFT JOIN registered b ON b.DW_FILE_NM = files.DW_FILE_NM AND b.DW_SRC_SYST = files.SOURCE_ID
        LEFT JOIN {config.T_RAW_LIM_PKG_GROUP_SOURCE} mapping
          ON mapping.ACTIVE_FL = TRUE AND mapping.SUBJECT_AREA = '{subject}'
         AND mapping.SOURCE_ID = files.SOURCE_ID AND mapping.PKG_ID = SUBSTR(files.BASE_NAME, 16, 3)
        LEFT JOIN accepted a
          ON a.PKG_GROUP_NAME = mapping.PKG_GROUP_NAME AND a.DLVY_SOURCE_ID = files.SOURCE_ID
         AND a.DLVY_PKG_ID = SUBSTR(files.BASE_NAME, 16, 3)
         AND a.DLVY_PKG_YEAR = SUBSTR(files.BASE_NAME, 25, 4)
         AND a.DLVY_PKG_YEAR_SEQ_NO = SUBSTR(files.BASE_NAME, 29, 5)
         AND a.DLVY_END_DATE = COALESCE(TRY_TO_DATE(SUBSTR(files.BASE_NAME, 35, 10), 'YYYY-MM-DD'),
                                      TRY_TO_DATE(SUBSTR(files.BASE_NAME, 35, 8), 'YYYYMMDD'))
    """


def metadata_detail_sql():
    # Derive status at file granularity, without rescanning large RAW subjects.
    # The exact physical filename prevents readiness leaking between variants.
    ready = """(b.DW_FILE_NM IS NOT NULL OR
                (m.FILE_TYPE = 'PAYLOAD' AND m.DATA_ROWS = 0 AND m.ROWCOUNT_STATUS = 'ROWCOUNT_OK'
                 AND m.RECEIVED_FL = TRUE AND accepted.DLVY_SOURCE_ID IS NOT NULL) OR
                (m.FILE_TYPE = 'CONTROL' AND archived.DW_FILE_NM IS NOT NULL))"""
    return f"""
        WITH accepted AS ({accepted_deliveries_sql()}), registered AS (
            SELECT DISTINCT UPPER(DW_TABLE_NM) AS DW_TABLE_NM, DW_FILE_NM, DW_SRC_SYST
            FROM {config.LIM_BATCH_CTRL_TABLE}
        ), archived AS (
            SELECT DISTINCT SUBJECT_AREA, PKG_GROUP_NAME, DW_FILE_NM
            FROM {config.METADATA_NAMESPACE}.RAW_LIM_CONTROL_ARCHIVE
        ), latest AS (
            SELECT * FROM {config.RAW_LIM_META_TABLE}
            WHERE PKG_GROUP_NAME = %(group_name)s
            QUALIFY RUN_DTTM = MAX(RUN_DTTM) OVER (PARTITION BY PKG_GROUP_NAME)
        )
        SELECT DISTINCT m.* EXCLUDE (DW_READY_TO_LOAD_FL, READY_STATUS),
               {ready} AS DW_READY_TO_LOAD_FL,
               IFF({ready}, 'READY_TO_LOAD', m.READY_STATUS) AS READY_STATUS
        FROM latest m
        LEFT JOIN registered b
          ON b.DW_TABLE_NM IN (UPPER(m.RAW_TABLE), REGEXP_REPLACE(UPPER(m.RAW_TABLE), '^[^.]+[.]', ''))
         AND b.DW_FILE_NM = m.ORIGINAL_FILE_NAME AND b.DW_SRC_SYST = m.DLVY_SOURCE_ID
        LEFT JOIN accepted
          ON accepted.PKG_GROUP_NAME = m.PKG_GROUP_NAME AND accepted.DLVY_END_DATE = m.DLVY_END_DATE
         AND accepted.DLVY_SOURCE_ID = m.DLVY_SOURCE_ID AND accepted.DLVY_PKG_ID = m.DLVY_PKG_ID
         AND accepted.DLVY_PKG_YEAR = m.DLVY_PKG_YEAR AND accepted.DLVY_PKG_YEAR_SEQ_NO = m.DLVY_PKG_YEAR_SEQ_NO
        LEFT JOIN archived
          ON archived.SUBJECT_AREA = m.DLVY_SUBJECT_AREA_ID AND archived.PKG_GROUP_NAME = m.PKG_GROUP_NAME
         AND archived.DW_FILE_NM = m.ORIGINAL_FILE_NAME
        ORDER BY m.DLVY_END_DATE DESC, m.DLVY_SOURCE_ID, m.FILE_NAME
    """
