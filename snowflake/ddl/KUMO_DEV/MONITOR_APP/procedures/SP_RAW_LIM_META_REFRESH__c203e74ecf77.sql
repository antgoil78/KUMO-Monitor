-- Source: KUMO_DEV.MONITOR_APP.SP_RAW_LIM_META_REFRESH(VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_DEV";
USE SCHEMA "KUMO_DEV"."MONITOR_APP";

CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_RAW_LIM_META_REFRESH("DLVY_SUBJECT_AREA" VARCHAR, "P_DATABASE" VARCHAR DEFAULT 'KUMO_DEV')
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS '
-- DEV_LIM_BATCH_READINESS_V1
DECLARE
    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(P_DATABASE));
    V_METADATA_TABLE VARCHAR;
    V_SOURCE_TABLE VARCHAR;
    V_BATCH_CTRL VARCHAR;
    V_LATEST_LOG VARCHAR;
    V_HISTORY_LOG VARCHAR;
    V_ARCHIVE VARCHAR;
    V_TRANSACTION_STARTED BOOLEAN DEFAULT FALSE;
    E_INVALID_DATABASE EXCEPTION (-20002, ''Invalid database identifier'');
    V_RAW_TABLE VARCHAR;
    V_ROW_COUNT INTEGER;
    V_RUN_DTTM TIMESTAMP_NTZ;
BEGIN
  IF (V_DATABASE IS NULL OR V_DATABASE <> ''KUMO_DEV'') THEN
    RAISE E_INVALID_DATABASE;
  END IF;
    V_METADATA_TABLE := V_DATABASE || ''.MONITOR_META.RAW_LIM_META'';
    V_SOURCE_TABLE := V_DATABASE || ''.MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE'';
    V_BATCH_CTRL := V_DATABASE || ''.META.LIM_BATCH_CTRL'';
    V_LATEST_LOG := V_DATABASE || ''.META._TMP_SET_READY_LOG'';
    V_HISTORY_LOG := V_DATABASE || ''.MONITOR_META._SET_READY_LOG'';
    V_ARCHIVE := V_DATABASE || ''.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE'';
    V_RUN_DTTM := SYSDATE();
    V_RAW_TABLE := :V_DATABASE || ''.RAW_LIM.RAW_LIM_'' || UPPER(:DLVY_SUBJECT_AREA);

    -- Replace the current subject snapshot, including a refresh with no candidates.
    -- Readiness history, rather than old metadata snapshots, records acceptance.
    BEGIN TRANSACTION;
    V_TRANSACTION_STARTED := TRUE;
    DELETE FROM IDENTIFIER(:V_METADATA_TABLE) WHERE RAW_TABLE = :V_RAW_TABLE;

    INSERT INTO IDENTIFIER(:V_METADATA_TABLE) (
        RAW_TABLE,
        ORIGINAL_FILE_NAME,
        FILE_NAME,
        FILE_KEY,
        CONTROL_FILE_NAME,
        FILE_TYPE,
        EXPECTED_BY_CONTROL_FL,
        RECEIVED_FL,
        READY_STATUS,
        ROWCOUNT_STATUS,
        FILE_EXISTS_IN_RAW,
        DW_READY_TO_LOAD_FL,
        DLVY_SOURCE_ID,
        DLVY_SUBJECT_AREA_ID,
        DLVY_LIM_OBJ_SEQ_NO,
        DLVY_LIM_OBJ_VER_NO,
        DLVY_PKG_ID,
        DLVY_PKG_YEAR,
        DLVY_PKG_YEAR_SEQ_NO,
        DLVY_END_DATE,
        EXPECTED_ROWS,
        DATA_ROWS,
        ACTUAL_ROWS,
        IS_VALID_SEQUENCE,
        PKG_GROUP_NAME,
        ALL_SOURCES_FL,
        RUN_DTTM
    )
    WITH registered_files AS (
        SELECT DISTINCT DW_FILE_NM, DW_SRC_SYST
        FROM IDENTIFIER(:V_BATCH_CTRL)
        WHERE UPPER(DW_TABLE_NM) IN (
            ''RAW_LIM.RAW_LIM_'' || UPPER(:DLVY_SUBJECT_AREA), :V_RAW_TABLE)
    ),
    accepted_deliveries AS (
        SELECT DISTINCT PKG_GROUP_NAME, DLVY_END_DATE, DLVY_SOURCE_ID,
               DLVY_PKG_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO
        FROM (
            SELECT * FROM IDENTIFIER(:V_LATEST_LOG)
            UNION ALL
            SELECT * FROM IDENTIFIER(:V_HISTORY_LOG)
        )
        WHERE STATUS IN (''UPDATED'', ''PREVIOUSLY_LOADED'')
    ),
    raw_input AS (
        SELECT
            :V_RAW_TABLE AS RAW_TABLE,
            DW_FILE_NM::STRING AS ORIGINAL_FILE_NAME,
            REGEXP_SUBSTR(DW_FILE_NM::STRING, ''[^/]+$'') AS BASE_FILE_NAME,
            TRIM(DATA::STRING) AS DATA_T,
            SUBSTR(TRIM(DATA::STRING), 1, 2) AS RECORD_TYPE
        FROM IDENTIFIER(:V_RAW_TABLE) raw
        WHERE NOT EXISTS (
            SELECT 1 FROM IDENTIFIER(:V_ARCHIVE) archived
            WHERE archived.SUBJECT_AREA = UPPER(:DLVY_SUBJECT_AREA)
              AND archived.DW_FILE_NM = raw.DW_FILE_NM
              AND archived.DW_FILE_CHECK_SUM IS NOT DISTINCT FROM raw.DW_FILE_CHECK_SUM
        )
    ),
    raw_rows AS (
        SELECT
            RAW_TABLE,
            ORIGINAL_FILE_NAME,
            COALESCE(
                REGEXP_SUBSTR(
                    BASE_FILE_NAME,
                    ''^[A-Z0-9]{10}_[0-9]{3}_[0-9]{3}_0000_[0-9]{9}_[0-9]{8}''
                ),
                SUBSTR(BASE_FILE_NAME, 1, 42)
            ) AS FILE_NAME,
            COALESCE(
                REGEXP_SUBSTR(
                    BASE_FILE_NAME,
                    ''^[A-Z0-9]{10}_[0-9]{3}_[0-9]{3}_0000_[0-9]{9}_[0-9]{8}''
                ),
                SUBSTR(BASE_FILE_NAME, 1, 42)
            ) AS FILE_KEY,
            DATA_T,
            RECORD_TYPE
        FROM raw_input
    ),
    physical_file_rows AS (
        SELECT
            RAW_TABLE,
            ORIGINAL_FILE_NAME,
            FILE_NAME,
            FILE_KEY,
            DATA_T,
            RECORD_TYPE,
            SUBSTR(FILE_KEY, 1, 3)  AS DLVY_SOURCE_ID,
            SUBSTR(FILE_KEY, 4, 4)  AS DLVY_SUBJECT_AREA_ID,
            SUBSTR(FILE_KEY, 8, 3)  AS DLVY_LIM_OBJ_SEQ_NO,
            SUBSTR(FILE_KEY, 12, 3) AS DLVY_LIM_OBJ_VER_NO,
            SUBSTR(FILE_KEY, 16, 3) AS DLVY_PKG_ID,
            SUBSTR(FILE_KEY, 25, 4) AS DLVY_PKG_YEAR,
            SUBSTR(FILE_KEY, 29, 5) AS DLVY_PKG_YEAR_SEQ_NO,
            COALESCE(
                TRY_TO_DATE(SUBSTR(FILE_KEY, 35, 10), ''YYYY-MM-DD''),
                TRY_TO_DATE(SUBSTR(FILE_KEY, 35, 8),  ''YYYYMMDD'')
            ) AS DLVY_END_DATE,
            CASE
                WHEN RECORD_TYPE = ''99''
                    THEN TRY_TO_NUMBER(REGEXP_SUBSTR(DATA_T, ''[+-]?[0-9]+$''))
                ELSE NULL
            END AS FOOTER_EXPECTED_ROWS
        FROM raw_rows
    ),
    physical_file_agg AS (
        SELECT
            RAW_TABLE,
            MAX(ORIGINAL_FILE_NAME) AS ORIGINAL_FILE_NAME,
            FILE_NAME,
            FILE_KEY,
            IFF(DLVY_LIM_OBJ_SEQ_NO = ''000'', ''CONTROL'', ''PAYLOAD'') AS FILE_TYPE,
            DLVY_SOURCE_ID,
            DLVY_SUBJECT_AREA_ID,
            DLVY_LIM_OBJ_SEQ_NO,
            DLVY_LIM_OBJ_VER_NO,
            DLVY_PKG_ID,
            DLVY_PKG_YEAR,
            DLVY_PKG_YEAR_SEQ_NO,
            DLVY_END_DATE,
            COUNT(*) AS RAW_ROW_COUNT,
            SUM(IFF(RECORD_TYPE = ''00'', 1, 0)) AS HEADER_00_CNT,
            SUM(IFF(RECORD_TYPE = ''10'', 1, 0)) AS DATA_10_CNT,
            SUM(IFF(RECORD_TYPE = ''99'', 1, 0)) AS FOOTER_99_CNT,
            MAX(FOOTER_EXPECTED_ROWS) AS EXPECTED_ROWS,
            COUNT(*) - SUM(IFF(RECORD_TYPE = ''00'', 1, 0)) - SUM(IFF(RECORD_TYPE = ''99'', 1, 0)) AS ACTUAL_ROWS
        FROM physical_file_rows
        GROUP BY
            RAW_TABLE, FILE_NAME, FILE_KEY,
            DLVY_SOURCE_ID, DLVY_SUBJECT_AREA_ID,
            DLVY_LIM_OBJ_SEQ_NO, DLVY_LIM_OBJ_VER_NO,
            DLVY_PKG_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO,
            DLVY_END_DATE
    ),
    physical_file_readiness AS (
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
    physical_file_status AS (
        SELECT
            RAW_TABLE,
            ORIGINAL_FILE_NAME,
            FILE_NAME,
            FILE_KEY,
            FILE_TYPE,
            DLVY_SOURCE_ID,
            DLVY_SUBJECT_AREA_ID,
            DLVY_LIM_OBJ_SEQ_NO,
            DLVY_LIM_OBJ_VER_NO,
            DLVY_PKG_ID,
            DLVY_PKG_YEAR,
            DLVY_PKG_YEAR_SEQ_NO,
            DLVY_END_DATE,
            READY_TO_LOAD_ROW_EXISTS,
            RAW_ROW_COUNT,
            HEADER_00_CNT,
            DATA_10_CNT,
            FOOTER_99_CNT,
            EXPECTED_ROWS,
            DATA_10_CNT AS DATA_ROWS,
            ACTUAL_ROWS,
            TRUE AS FILE_EXISTS_IN_RAW,
            IFF(READY_TO_LOAD_ROW_EXISTS = 1, TRUE, FALSE) AS DW_READY_TO_LOAD_FL,
            CASE
                WHEN READY_TO_LOAD_ROW_EXISTS = 1
                    THEN ''READY_TO_LOAD''
                WHEN FOOTER_99_CNT > 0 AND EXPECTED_ROWS IS NOT NULL AND ACTUAL_ROWS = EXPECTED_ROWS
                    THEN ''FILE_VALIDATION_OK''
                ELSE ''NOT_READY_TO_LOAD''
            END AS READY_STATUS,
            CASE
                WHEN FOOTER_99_CNT = 0
                    THEN ''NO_FOOTER''
                WHEN EXPECTED_ROWS IS NULL
                    THEN ''ROWCOUNT_NOT_EVALUATED''
                WHEN ACTUAL_ROWS = EXPECTED_ROWS AND DATA_10_CNT = EXPECTED_ROWS
                    THEN ''ROWCOUNT_OK''
                WHEN ACTUAL_ROWS = EXPECTED_ROWS AND DATA_10_CNT <> EXPECTED_ROWS
                    THEN ''DATAROW_MISMATCH''
                WHEN ACTUAL_ROWS <> EXPECTED_ROWS AND DATA_10_CNT = EXPECTED_ROWS
                    THEN ''ROWCOUNT_MISMATCH''
                WHEN ACTUAL_ROWS <> EXPECTED_ROWS AND DATA_10_CNT <> EXPECTED_ROWS
                    THEN ''ROWCOUNT_MISMATCH''
                ELSE ''ROWCOUNT_NOT_EVALUATED''
            END AS ROWCOUNT_STATUS
        FROM physical_file_readiness
    ),
    control_files AS (
        SELECT
            RAW_TABLE,
            ORIGINAL_FILE_NAME,
            FILE_NAME,
            FILE_KEY,
            FILE_NAME AS CONTROL_FILE_NAME,
            ''CONTROL'' AS FILE_TYPE,
            FALSE AS EXPECTED_BY_CONTROL_FL,
            TRUE AS RECEIVED_FL,
            READY_STATUS,
            ROWCOUNT_STATUS,
            FILE_EXISTS_IN_RAW,
            DW_READY_TO_LOAD_FL,
            DLVY_SOURCE_ID,
            DLVY_SUBJECT_AREA_ID,
            DLVY_LIM_OBJ_SEQ_NO,
            DLVY_LIM_OBJ_VER_NO,
            DLVY_PKG_ID,
            DLVY_PKG_YEAR,
            DLVY_PKG_YEAR_SEQ_NO,
            DLVY_END_DATE,
            EXPECTED_ROWS,
            DATA_ROWS,
            ACTUAL_ROWS
        FROM physical_file_status
        WHERE DLVY_LIM_OBJ_SEQ_NO = ''000''
    ),
    control_detail_expected AS (
        SELECT DISTINCT
            rr.RAW_TABLE,
            rr.FILE_NAME AS CONTROL_FILE_NAME,
            rr.FILE_KEY AS CONTROL_FILE_KEY,
            UPPER(TRIM(SUBSTR(rr.DATA_T, 3, 3)))  AS DLVY_SOURCE_ID,
            UPPER(TRIM(SUBSTR(rr.DATA_T, 6, 4)))  AS DLVY_SUBJECT_AREA_ID,
            TRIM(SUBSTR(rr.DATA_T, 22, 3))        AS DLVY_LIM_OBJ_SEQ_NO,
            TRIM(SUBSTR(rr.DATA_T, 25, 3))        AS DLVY_LIM_OBJ_VER_NO,
            TRIM(SUBSTR(rr.DATA_T, 10, 3))        AS DLVY_PKG_ID,
            SUBSTR(TRIM(SUBSTR(rr.DATA_T, 13, 9)), 1, 4) AS DLVY_PKG_YEAR,
            SUBSTR(TRIM(SUBSTR(rr.DATA_T, 13, 9)), 5, 5) AS DLVY_PKG_YEAR_SEQ_NO,
            COALESCE(
                TRY_TO_DATE(SUBSTR(rr.DATA_T, 38, 10), ''YYYY-MM-DD''),
                TRY_TO_DATE(SUBSTR(rr.DATA_T, 38, 8),  ''YYYYMMDD'')
            ) AS DLVY_END_DATE,
            UPPER(TRIM(SUBSTR(rr.DATA_T, 3, 3))) ||
            UPPER(TRIM(SUBSTR(rr.DATA_T, 6, 4))) ||
            TRIM(SUBSTR(rr.DATA_T, 22, 3)) || ''_'' ||
            TRIM(SUBSTR(rr.DATA_T, 25, 3)) || ''_'' ||
            TRIM(SUBSTR(rr.DATA_T, 10, 3)) || ''_0000_'' ||
            SUBSTR(TRIM(SUBSTR(rr.DATA_T, 13, 9)), 1, 4) ||
            SUBSTR(TRIM(SUBSTR(rr.DATA_T, 13, 9)), 5, 5) || ''_'' ||
            TO_CHAR(
                COALESCE(
                    TRY_TO_DATE(SUBSTR(rr.DATA_T, 38, 10), ''YYYY-MM-DD''),
                    TRY_TO_DATE(SUBSTR(rr.DATA_T, 38, 8),  ''YYYYMMDD'')
                ),
                ''YYYYMMDD''
            ) AS EXPECTED_FILE_KEY
        FROM raw_rows rr
        WHERE SUBSTR(rr.FILE_KEY, 8, 3) = ''000''
          AND rr.RECORD_TYPE = ''10''
    ),
    expected_payload_files AS (
        SELECT
            e.RAW_TABLE,
            p.ORIGINAL_FILE_NAME,
            COALESCE(p.FILE_NAME, e.EXPECTED_FILE_KEY) AS FILE_NAME,
            COALESCE(p.FILE_KEY, e.EXPECTED_FILE_KEY) AS FILE_KEY,
            e.CONTROL_FILE_NAME,
            ''PAYLOAD'' AS FILE_TYPE,
            TRUE AS EXPECTED_BY_CONTROL_FL,
            IFF(p.FILE_KEY IS NULL, FALSE, TRUE) AS RECEIVED_FL,
            COALESCE(p.READY_STATUS, ''NOT_READY_TO_LOAD'') AS READY_STATUS,
            CASE
                WHEN p.FILE_KEY IS NULL
                    THEN ''ROWCOUNT_NOT_EVALUATED''
                ELSE p.ROWCOUNT_STATUS
            END AS ROWCOUNT_STATUS,
            COALESCE(p.FILE_EXISTS_IN_RAW, FALSE) AS FILE_EXISTS_IN_RAW,
            COALESCE(p.DW_READY_TO_LOAD_FL, FALSE) AS DW_READY_TO_LOAD_FL,
            e.DLVY_SOURCE_ID,
            e.DLVY_SUBJECT_AREA_ID,
            e.DLVY_LIM_OBJ_SEQ_NO,
            e.DLVY_LIM_OBJ_VER_NO,
            e.DLVY_PKG_ID,
            e.DLVY_PKG_YEAR,
            e.DLVY_PKG_YEAR_SEQ_NO,
            e.DLVY_END_DATE,
            p.EXPECTED_ROWS,
            p.DATA_ROWS,
            p.ACTUAL_ROWS
        FROM control_detail_expected e
        LEFT JOIN physical_file_status p
            ON p.RAW_TABLE = e.RAW_TABLE
           AND p.FILE_KEY = e.EXPECTED_FILE_KEY
    ),
    unexpected_payload_files AS (
        SELECT
            p.RAW_TABLE,
            p.ORIGINAL_FILE_NAME,
            p.FILE_NAME,
            p.FILE_KEY,
            NULL AS CONTROL_FILE_NAME,
            ''PAYLOAD'' AS FILE_TYPE,
            FALSE AS EXPECTED_BY_CONTROL_FL,
            TRUE AS RECEIVED_FL,
            p.READY_STATUS,
            p.ROWCOUNT_STATUS,
            p.FILE_EXISTS_IN_RAW,
            p.DW_READY_TO_LOAD_FL,
            p.DLVY_SOURCE_ID,
            p.DLVY_SUBJECT_AREA_ID,
            p.DLVY_LIM_OBJ_SEQ_NO,
            p.DLVY_LIM_OBJ_VER_NO,
            p.DLVY_PKG_ID,
            p.DLVY_PKG_YEAR,
            p.DLVY_PKG_YEAR_SEQ_NO,
            p.DLVY_END_DATE,
            p.EXPECTED_ROWS,
            p.DATA_ROWS,
            p.ACTUAL_ROWS
        FROM physical_file_status p
        LEFT JOIN control_detail_expected e
            ON e.RAW_TABLE = p.RAW_TABLE
           AND e.EXPECTED_FILE_KEY = p.FILE_KEY
        WHERE p.DLVY_LIM_OBJ_SEQ_NO <> ''000''
          AND e.EXPECTED_FILE_KEY IS NULL
    ),
    final_rows AS (
        SELECT * FROM control_files
        UNION ALL
        SELECT * FROM expected_payload_files
        UNION ALL
        SELECT * FROM unexpected_payload_files
    ),
    distinct_deliveries AS (
        SELECT DISTINCT
            f.RAW_TABLE,
            f.DLVY_SOURCE_ID,
            f.DLVY_SUBJECT_AREA_ID,
            f.DLVY_PKG_ID,
            f.DLVY_PKG_YEAR,
            f.DLVY_PKG_YEAR_SEQ_NO
        FROM final_rows f
        WHERE NOT EXISTS (
            SELECT 1
            FROM IDENTIFIER(:V_SOURCE_TABLE) pgs
            WHERE pgs.ACTIVE_FL = 1
              AND pgs.SOURCE_ID = f.DLVY_SOURCE_ID
              AND pgs.SUBJECT_AREA = f.DLVY_SUBJECT_AREA_ID
              AND pgs.PKG_ID = f.DLVY_PKG_ID
              AND pgs.DLVY_PKG_YEAR IS NOT NULL
              AND pgs.DLVY_PKG_YEAR_SEQ_NO IS NOT NULL
              AND (f.DLVY_PKG_YEAR < pgs.DLVY_PKG_YEAR
                   OR (f.DLVY_PKG_YEAR = pgs.DLVY_PKG_YEAR
                       AND f.DLVY_PKG_YEAR_SEQ_NO <= pgs.DLVY_PKG_YEAR_SEQ_NO))
        )
    ),
    delivery_sequence_check AS (
        SELECT
            d.*,
            CASE
                WHEN ROW_NUMBER() OVER (
                    PARTITION BY d.RAW_TABLE, d.DLVY_SOURCE_ID, d.DLVY_SUBJECT_AREA_ID, d.DLVY_PKG_ID
                    ORDER BY d.DLVY_PKG_YEAR, d.DLVY_PKG_YEAR_SEQ_NO
                ) = 1
                AND pgs_stored.DLVY_PKG_YEAR_SEQ_NO IS NOT NULL
                AND (d.DLVY_PKG_YEAR != pgs_stored.DLVY_PKG_YEAR
                     OR TRY_TO_NUMBER(d.DLVY_PKG_YEAR_SEQ_NO) != TRY_TO_NUMBER(pgs_stored.DLVY_PKG_YEAR_SEQ_NO) + 1)
                THEN FALSE
                WHEN ROW_NUMBER() OVER (
                    PARTITION BY d.RAW_TABLE, d.DLVY_SOURCE_ID, d.DLVY_SUBJECT_AREA_ID, d.DLVY_PKG_ID
                    ORDER BY d.DLVY_PKG_YEAR, d.DLVY_PKG_YEAR_SEQ_NO
                ) = 1
                AND pgs_stored.DLVY_PKG_YEAR_SEQ_NO IS NULL
                THEN TRUE
                WHEN LAG(d.DLVY_PKG_YEAR) OVER (
                    PARTITION BY d.RAW_TABLE, d.DLVY_SOURCE_ID, d.DLVY_SUBJECT_AREA_ID, d.DLVY_PKG_ID
                    ORDER BY d.DLVY_PKG_YEAR, d.DLVY_PKG_YEAR_SEQ_NO
                ) IS NOT NULL
                AND d.DLVY_PKG_YEAR = LAG(d.DLVY_PKG_YEAR) OVER (
                    PARTITION BY d.RAW_TABLE, d.DLVY_SOURCE_ID, d.DLVY_SUBJECT_AREA_ID, d.DLVY_PKG_ID
                    ORDER BY d.DLVY_PKG_YEAR, d.DLVY_PKG_YEAR_SEQ_NO
                )
                AND TRY_TO_NUMBER(d.DLVY_PKG_YEAR_SEQ_NO) != TRY_TO_NUMBER(LAG(d.DLVY_PKG_YEAR_SEQ_NO) OVER (
                    PARTITION BY d.RAW_TABLE, d.DLVY_SOURCE_ID, d.DLVY_SUBJECT_AREA_ID, d.DLVY_PKG_ID
                    ORDER BY d.DLVY_PKG_YEAR, d.DLVY_PKG_YEAR_SEQ_NO
                )) + 1
                THEN FALSE
                WHEN LAG(d.DLVY_PKG_YEAR) OVER (
                    PARTITION BY d.RAW_TABLE, d.DLVY_SOURCE_ID, d.DLVY_SUBJECT_AREA_ID, d.DLVY_PKG_ID
                    ORDER BY d.DLVY_PKG_YEAR, d.DLVY_PKG_YEAR_SEQ_NO
                ) IS NOT NULL
                AND d.DLVY_PKG_YEAR != LAG(d.DLVY_PKG_YEAR) OVER (
                    PARTITION BY d.RAW_TABLE, d.DLVY_SOURCE_ID, d.DLVY_SUBJECT_AREA_ID, d.DLVY_PKG_ID
                    ORDER BY d.DLVY_PKG_YEAR, d.DLVY_PKG_YEAR_SEQ_NO
                )
                AND TRY_TO_NUMBER(d.DLVY_PKG_YEAR_SEQ_NO) != 1
                THEN FALSE
                ELSE TRUE
            END AS IS_VALID_SEQUENCE
        FROM distinct_deliveries d
        LEFT JOIN (
            SELECT SOURCE_ID, SUBJECT_AREA, PKG_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO
            FROM IDENTIFIER(:V_SOURCE_TABLE)
            WHERE ACTIVE_FL = 1
        ) pgs_stored
            ON pgs_stored.SOURCE_ID = d.DLVY_SOURCE_ID
            AND pgs_stored.SUBJECT_AREA = d.DLVY_SUBJECT_AREA_ID
            AND pgs_stored.PKG_ID = d.DLVY_PKG_ID
    ),
    sequence_validation AS (
        SELECT
            f.*,
            dsc.IS_VALID_SEQUENCE
        FROM final_rows f
        INNER JOIN delivery_sequence_check dsc
            ON dsc.RAW_TABLE = f.RAW_TABLE
            AND dsc.DLVY_SOURCE_ID = f.DLVY_SOURCE_ID
            AND dsc.DLVY_SUBJECT_AREA_ID = f.DLVY_SUBJECT_AREA_ID
            AND dsc.DLVY_PKG_ID = f.DLVY_PKG_ID
            AND dsc.DLVY_PKG_YEAR = f.DLVY_PKG_YEAR
            AND dsc.DLVY_PKG_YEAR_SEQ_NO = f.DLVY_PKG_YEAR_SEQ_NO
    ),
    pkg_group_validation AS (
        SELECT
            sv.RAW_TABLE,
            sv.ORIGINAL_FILE_NAME,
            sv.FILE_NAME,
            sv.FILE_KEY,
            sv.CONTROL_FILE_NAME,
            sv.FILE_TYPE,
            sv.EXPECTED_BY_CONTROL_FL,
            sv.RECEIVED_FL,
            sv.READY_STATUS,
            sv.ROWCOUNT_STATUS,
            sv.FILE_EXISTS_IN_RAW,
            sv.DW_READY_TO_LOAD_FL,
            sv.DLVY_SOURCE_ID,
            sv.DLVY_SUBJECT_AREA_ID,
            sv.DLVY_LIM_OBJ_SEQ_NO,
            sv.DLVY_LIM_OBJ_VER_NO,
            sv.DLVY_PKG_ID,
            sv.DLVY_PKG_YEAR,
            sv.DLVY_PKG_YEAR_SEQ_NO,
            sv.DLVY_END_DATE,
            sv.EXPECTED_ROWS,
            sv.DATA_ROWS,
            sv.ACTUAL_ROWS,
            sv.IS_VALID_SEQUENCE,
            pg.PKG_GROUP_NAME,
            IFF(COUNT(DISTINCT sv.DLVY_SOURCE_ID) OVER (
                PARTITION BY pg.PKG_GROUP_NAME, sv.DLVY_PKG_ID, sv.DLVY_PKG_YEAR, sv.DLVY_PKG_YEAR_SEQ_NO
            ) >= pg.EXPECTED_SOURCE_COUNT, TRUE, FALSE) AS ALL_SOURCES_FL
        FROM sequence_validation sv
        LEFT JOIN (
            SELECT
                SOURCE_ID,
                SUBJECT_AREA,
                PKG_ID,
                PKG_GROUP_NAME,
                COUNT(DISTINCT SOURCE_ID) OVER (PARTITION BY PKG_GROUP_NAME) AS EXPECTED_SOURCE_COUNT
            FROM IDENTIFIER(:V_SOURCE_TABLE)
            WHERE ACTIVE_FL = 1
        ) pg
            ON sv.DLVY_SOURCE_ID = pg.SOURCE_ID
            AND sv.DLVY_SUBJECT_AREA_ID = pg.SUBJECT_AREA
            AND sv.DLVY_PKG_ID = pg.PKG_ID
    )
    SELECT
        RAW_TABLE,
        ORIGINAL_FILE_NAME,
        FILE_NAME,
        FILE_KEY,
        CONTROL_FILE_NAME,
        FILE_TYPE,
        EXPECTED_BY_CONTROL_FL,
        RECEIVED_FL,
        READY_STATUS,
        ROWCOUNT_STATUS,
        FILE_EXISTS_IN_RAW,
        DW_READY_TO_LOAD_FL,
        DLVY_SOURCE_ID,
        DLVY_SUBJECT_AREA_ID,
        DLVY_LIM_OBJ_SEQ_NO,
        DLVY_LIM_OBJ_VER_NO,
        DLVY_PKG_ID,
        DLVY_PKG_YEAR,
        DLVY_PKG_YEAR_SEQ_NO,
        DLVY_END_DATE,
        EXPECTED_ROWS,
        DATA_ROWS,
        ACTUAL_ROWS,
        IS_VALID_SEQUENCE,
        PKG_GROUP_NAME,
        ALL_SOURCES_FL,
        :V_RUN_DTTM
    FROM pkg_group_validation;

    SELECT COUNT(*) INTO :V_ROW_COUNT
    FROM IDENTIFIER(:V_METADATA_TABLE)
    WHERE RAW_TABLE = :V_RAW_TABLE
    AND RUN_DTTM = :V_RUN_DTTM;

    COMMIT;
    V_TRANSACTION_STARTED := FALSE;

    RETURN ''SUCCESS: '' || :V_ROW_COUNT || '' rows loaded for '' || :V_RAW_TABLE;
EXCEPTION
    WHEN OTHER THEN
        IF (V_TRANSACTION_STARTED) THEN
            ROLLBACK;
        END IF;
        RAISE;
END;
';
