-- Prepared by apply_dev_lim_readiness.py; DEV only.
-- Procedure definitions only; no loader/workflow invocation.

CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_RAW_LIM_META_REFRESH("DLVY_SUBJECT_AREA" VARCHAR, "P_DATABASE" VARCHAR DEFAULT 'KUMO_DEV')
COPY GRANTS
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
    V_LATEST_LOG := V_DATABASE || ''.MONITOR_META._TMP_SET_READY_LOG'';
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

CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_RAW_LIM_SET_READY("PKG_GROUP_NAME" VARCHAR, "IGNORE_COMMON_END_DT" BOOLEAN DEFAULT FALSE, "IGNORE_SEQ_NO" BOOLEAN DEFAULT FALSE, "P_DATABASE" VARCHAR DEFAULT 'KUMO_DEV')
COPY GRANTS
RETURNS TABLE ("PKG_GROUP_NAME" VARCHAR, "DLVY_END_DATE" DATE, "DLVY_SOURCE_ID" VARCHAR, "DLVY_PKG_ID" VARCHAR, "DLVY_PKG_YEAR" VARCHAR, "DLVY_PKG_YEAR_SEQ_NO" VARCHAR, "STATUS" VARCHAR, "ROWS_UPDATED" NUMBER(38,0), "REASON" VARCHAR, "CONTROL_DATE" TIMESTAMP_NTZ(9))
LANGUAGE SQL
EXECUTE AS CALLER
AS '
-- DEV_LIM_BATCH_READINESS_V1
DECLARE
    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(P_DATABASE));
    V_SOURCE_MAP VARCHAR;
    V_METADATA_TABLE VARCHAR;
    V_LATEST_LOG VARCHAR;
    V_HISTORY_LOG VARCHAR;
    V_ARCHIVE VARCHAR;
    V_BATCH_CTRL VARCHAR;
    V_RAW_TABLE VARCHAR;
    V_SUBJECT_AREA VARCHAR;
    V_SUBJECT_COUNT INTEGER;
    V_SNAPSHOT VARCHAR;
    V_CONTROLS VARCHAR;
    V_MISSING INTEGER;
    V_TRANSACTION_STARTED BOOLEAN DEFAULT FALSE;
    V_RESULT RESULTSET;
    E_INVALID_DATABASE EXCEPTION (-20002, ''This procedure is restricted to KUMO_DEV'');
    E_INVALID_GROUP EXCEPTION (-20003, ''Package group must map to exactly one active subject'');
    E_MISSING_PAYLOAD EXCEPTION (-20004, ''Validated payload disappeared before batch registration'');
BEGIN
    IF (V_DATABASE IS NULL OR V_DATABASE <> ''KUMO_DEV'') THEN
        RAISE E_INVALID_DATABASE;
    END IF;
    V_SOURCE_MAP := V_DATABASE || ''.MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE'';
    V_METADATA_TABLE := V_DATABASE || ''.MONITOR_META.RAW_LIM_META'';
    V_LATEST_LOG := V_DATABASE || ''.MONITOR_META._TMP_SET_READY_LOG'';
    V_HISTORY_LOG := V_DATABASE || ''.MONITOR_META._SET_READY_LOG'';
    V_ARCHIVE := V_DATABASE || ''.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE'';
    V_BATCH_CTRL := V_DATABASE || ''.META.LIM_BATCH_CTRL'';

    SELECT MAX(SUBJECT_AREA), COUNT(DISTINCT SUBJECT_AREA)
      INTO :V_SUBJECT_AREA, :V_SUBJECT_COUNT
      FROM IDENTIFIER(:V_SOURCE_MAP)
     WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME AND ACTIVE_FL = TRUE;
    IF (V_SUBJECT_COUNT <> 1) THEN
        RAISE E_INVALID_GROUP;
    END IF;
    V_RAW_TABLE := V_DATABASE || ''.RAW_LIM.RAW_LIM_'' || UPPER(V_SUBJECT_AREA);
    V_SNAPSHOT := V_DATABASE || ''.MONITOR_META.TMP_LIM_READY_'' || REPLACE(UUID_STRING(), ''-'', ''_'');
    V_CONTROLS := V_SNAPSHOT || ''_CONTROLS'';

    -- Materialize one metadata run before transaction DML. Later refreshes cannot
    -- change which files this invocation validates, registers and archives.
    CREATE TEMPORARY TABLE IDENTIFIER(:V_SNAPSHOT) AS
    SELECT * FROM IDENTIFIER(:V_METADATA_TABLE)
    WHERE RAW_TABLE = :V_RAW_TABLE AND PKG_GROUP_NAME = :PKG_GROUP_NAME
      AND RUN_DTTM = (SELECT MAX(RUN_DTTM) FROM IDENTIFIER(:V_METADATA_TABLE)
                     WHERE RAW_TABLE = :V_RAW_TABLE);
    CREATE TEMPORARY TABLE IDENTIFIER(:V_CONTROLS) (DW_FILE_NM VARCHAR);

    BEGIN TRANSACTION;
    V_TRANSACTION_STARTED := TRUE;

    INSERT INTO IDENTIFIER(:V_HISTORY_LOG)
    SELECT * FROM IDENTIFIER(:V_LATEST_LOG) WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME;
    DELETE FROM IDENTIFIER(:V_LATEST_LOG) WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME;

    INSERT INTO IDENTIFIER(:V_LATEST_LOG)
    WITH delivery_agg AS (
        SELECT DLVY_END_DATE, DLVY_SOURCE_ID, DLVY_PKG_ID,
               DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO,
               IFF(COUNT_IF(FILE_TYPE = ''PAYLOAD'') > 0
                   AND COUNT_IF(FILE_TYPE = ''PAYLOAD''
                                AND NOT COALESCE(DW_READY_TO_LOAD_FL, FALSE)) = 0,
                   1, 0) AS ALL_PAYLOADS_ACCEPTED,
               LISTAGG(DISTINCT CASE
                   WHEN FILE_TYPE = ''PAYLOAD'' AND DW_READY_TO_LOAD_FL = TRUE THEN NULL
                   WHEN DLVY_END_DATE IS NULL THEN ''INVALID_DATE='' || FILE_KEY
                   WHEN COALESCE(RECEIVED_FL, FALSE) = FALSE THEN ''MISSING_FILE='' || FILE_KEY
                   WHEN COALESCE(ROWCOUNT_STATUS, '''') <> ''ROWCOUNT_OK''
                       THEN ''ROWCOUNT_MISMATCH='' || FILE_KEY || '' (expected=''
                            || COALESCE(EXPECTED_ROWS::STRING, ''?'') || '' actual=''
                            || COALESCE(ACTUAL_ROWS::STRING, ''?'') || '')''
                   WHEN :IGNORE_COMMON_END_DT = FALSE AND COALESCE(ALL_SOURCES_FL, FALSE) = FALSE
                       THEN ''INCOMPLETE_GROUP='' || PKG_GROUP_NAME || '' (missing sources for DATE=''
                            || DLVY_END_DATE::STRING || '')''
                   WHEN :IGNORE_SEQ_NO = FALSE AND COALESCE(IS_VALID_SEQUENCE, FALSE) = FALSE
                       THEN ''INVALID_SEQUENCE='' || FILE_KEY
                   ELSE NULL END, ''; '') AS REASONS,
               SUM(IFF(FILE_TYPE = ''PAYLOAD'', COALESCE(DATA_ROWS, 0), 0)) AS PAYLOAD_ROWS
        FROM IDENTIFIER(:V_SNAPSHOT)
        GROUP BY DLVY_END_DATE, DLVY_SOURCE_ID, DLVY_PKG_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO
    ), ordered AS (
        SELECT *, MIN(IFF(COALESCE(REASONS, '''') <> '''',
                         COALESCE(DLVY_END_DATE, ''0001-01-01''::DATE), NULL)) OVER () AS FIRST_FAILURE_DATE
        FROM delivery_agg
    )
    SELECT :PKG_GROUP_NAME, DLVY_END_DATE, DLVY_SOURCE_ID, DLVY_PKG_ID,
           DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO,
           CASE
               WHEN COALESCE(REASONS, '''') = '''' AND ALL_PAYLOADS_ACCEPTED = 1 THEN ''PREVIOUSLY_LOADED''
               WHEN COALESCE(REASONS, '''') <> ''''
                    AND COALESCE(DLVY_END_DATE, ''0001-01-01''::DATE) = FIRST_FAILURE_DATE THEN ''STOPPED''
               WHEN COALESCE(REASONS, '''') <> '''' THEN ''BLOCKED''
               WHEN FIRST_FAILURE_DATE IS NULL OR DLVY_END_DATE < FIRST_FAILURE_DATE THEN ''UPDATED''
               ELSE ''BLOCKED''
           END,
           IFF(COALESCE(REASONS, '''') = '''' AND ALL_PAYLOADS_ACCEPTED = 0
               AND (FIRST_FAILURE_DATE IS NULL OR DLVY_END_DATE < FIRST_FAILURE_DATE), PAYLOAD_ROWS, 0),
           CASE WHEN COALESCE(REASONS, '''') <> '''' THEN REASONS
                WHEN ALL_PAYLOADS_ACCEPTED = 1 OR FIRST_FAILURE_DATE IS NULL
                     OR DLVY_END_DATE < FIRST_FAILURE_DATE THEN NULL
                ELSE ''Blocked by earlier failure'' END,
           SYSDATE()
    FROM ordered;

    -- Batch control is the queue for nonempty payloads. A normal rerun never
    -- clears batch IDs, processing timestamps, or processed/deleted flags.
    MERGE INTO IDENTIFIER(:V_BATCH_CTRL) tgt
    USING (
        SELECT DISTINCT ''RAW_LIM.RAW_LIM_'' || UPPER(:V_SUBJECT_AREA) AS DW_TABLE_NM,
               m.ORIGINAL_FILE_NAME AS DW_FILE_NM, m.DLVY_SOURCE_ID AS DW_SRC_SYST
        FROM IDENTIFIER(:V_SNAPSHOT) m
        JOIN IDENTIFIER(:V_LATEST_LOG) l
          ON l.PKG_GROUP_NAME = m.PKG_GROUP_NAME AND l.DLVY_END_DATE = m.DLVY_END_DATE
         AND l.DLVY_SOURCE_ID = m.DLVY_SOURCE_ID AND l.DLVY_PKG_ID = m.DLVY_PKG_ID
         AND l.DLVY_PKG_YEAR = m.DLVY_PKG_YEAR AND l.DLVY_PKG_YEAR_SEQ_NO = m.DLVY_PKG_YEAR_SEQ_NO
         AND l.STATUS = ''UPDATED''
        WHERE m.FILE_TYPE = ''PAYLOAD'' AND m.DATA_ROWS > 0 AND m.RECEIVED_FL = TRUE
          AND EXISTS (SELECT 1 FROM IDENTIFIER(:V_RAW_TABLE) raw
                      WHERE raw.DW_FILE_NM = m.ORIGINAL_FILE_NAME AND LEFT(TRIM(raw.DATA), 2) = ''10'')
    ) files
    ON UPPER(tgt.DW_TABLE_NM) IN (files.DW_TABLE_NM, :V_RAW_TABLE)
       AND tgt.DW_FILE_NM = files.DW_FILE_NM AND tgt.DW_SRC_SYST = files.DW_SRC_SYST
    WHEN NOT MATCHED THEN INSERT (
        READY_TO_LOAD_DTTM, DW_TABLE_NM, DW_FILE_NM, DW_SRC_SYST, PROCESSED_FL, DELETED_FL
    ) VALUES (SYSDATE(), files.DW_TABLE_NM, files.DW_FILE_NM, files.DW_SRC_SYST, FALSE, FALSE);

    SELECT COUNT(*) INTO :V_MISSING
    FROM IDENTIFIER(:V_SNAPSHOT) m
    JOIN IDENTIFIER(:V_LATEST_LOG) l
      ON l.PKG_GROUP_NAME = m.PKG_GROUP_NAME AND l.DLVY_END_DATE = m.DLVY_END_DATE
     AND l.DLVY_SOURCE_ID = m.DLVY_SOURCE_ID AND l.DLVY_PKG_ID = m.DLVY_PKG_ID
     AND l.DLVY_PKG_YEAR = m.DLVY_PKG_YEAR AND l.DLVY_PKG_YEAR_SEQ_NO = m.DLVY_PKG_YEAR_SEQ_NO
     AND l.STATUS IN (''UPDATED'', ''PREVIOUSLY_LOADED'')
    WHERE m.FILE_TYPE = ''PAYLOAD'' AND m.DATA_ROWS > 0
      AND NOT EXISTS (
          SELECT 1 FROM IDENTIFIER(:V_BATCH_CTRL) b
          WHERE UPPER(b.DW_TABLE_NM) IN (''RAW_LIM.RAW_LIM_'' || UPPER(:V_SUBJECT_AREA), :V_RAW_TABLE)
            AND b.DW_FILE_NM = m.ORIGINAL_FILE_NAME AND b.DW_SRC_SYST = m.DLVY_SOURCE_ID
      );
    IF (V_MISSING > 0) THEN
        RAISE E_MISSING_PAYLOAD;
    END IF;

    -- A control is complete only if its own delivery and every payload delivery
    -- it names were accepted. Empty validated payloads are covered by the log.
    INSERT INTO IDENTIFIER(:V_CONTROLS)
    SELECT DISTINCT control.ORIGINAL_FILE_NAME
    FROM IDENTIFIER(:V_SNAPSHOT) control
    JOIN IDENTIFIER(:V_LATEST_LOG) l
      ON l.PKG_GROUP_NAME = control.PKG_GROUP_NAME AND l.DLVY_END_DATE = control.DLVY_END_DATE
     AND l.DLVY_SOURCE_ID = control.DLVY_SOURCE_ID AND l.DLVY_PKG_ID = control.DLVY_PKG_ID
     AND l.DLVY_PKG_YEAR = control.DLVY_PKG_YEAR AND l.DLVY_PKG_YEAR_SEQ_NO = control.DLVY_PKG_YEAR_SEQ_NO
     AND l.STATUS IN (''UPDATED'', ''PREVIOUSLY_LOADED'')
    WHERE control.FILE_TYPE = ''CONTROL''
      AND NOT EXISTS (
          SELECT 1 FROM IDENTIFIER(:V_SNAPSHOT) payload
          WHERE payload.FILE_TYPE = ''PAYLOAD'' AND payload.CONTROL_FILE_NAME = control.FILE_NAME
            AND NOT EXISTS (
                SELECT 1 FROM IDENTIFIER(:V_LATEST_LOG) accepted
                WHERE accepted.PKG_GROUP_NAME = payload.PKG_GROUP_NAME
                  AND accepted.DLVY_END_DATE = payload.DLVY_END_DATE
                  AND accepted.DLVY_SOURCE_ID = payload.DLVY_SOURCE_ID
                  AND accepted.DLVY_PKG_ID = payload.DLVY_PKG_ID
                  AND accepted.DLVY_PKG_YEAR = payload.DLVY_PKG_YEAR
                  AND accepted.DLVY_PKG_YEAR_SEQ_NO = payload.DLVY_PKG_YEAR_SEQ_NO
                  AND accepted.STATUS IN (''UPDATED'', ''PREVIOUSLY_LOADED'')
            )
      );

    INSERT INTO IDENTIFIER(:V_ARCHIVE) (
        SUBJECT_AREA, PKG_GROUP_NAME, DATA, DW_BATCH_ID, DW_FILE_CHECK_SUM,
        DW_FILE_DTTM, DW_FILE_NM, DW_FILE_ROW_NUMBER, DW_LOAD_DTTM,
        DW_READY_TO_LOAD_FL, DW_ARCHIVED_DTTM
    )
    SELECT :V_SUBJECT_AREA, :PKG_GROUP_NAME, raw.DATA, NULL, raw.DW_FILE_CHECK_SUM,
           raw.DW_FILE_DTTM, raw.DW_FILE_NM, raw.DW_FILE_ROW_NUMBER, raw.DW_LOAD_DTTM, TRUE, SYSDATE()
    FROM IDENTIFIER(:V_RAW_TABLE) raw
    JOIN IDENTIFIER(:V_CONTROLS) selected ON selected.DW_FILE_NM = raw.DW_FILE_NM
    WHERE NOT EXISTS (
        SELECT 1 FROM IDENTIFIER(:V_ARCHIVE) archived
        WHERE archived.SUBJECT_AREA = :V_SUBJECT_AREA AND archived.PKG_GROUP_NAME = :PKG_GROUP_NAME
          AND archived.DW_FILE_NM = raw.DW_FILE_NM
          AND archived.DW_FILE_CHECK_SUM IS NOT DISTINCT FROM raw.DW_FILE_CHECK_SUM
          AND archived.DW_FILE_ROW_NUMBER = raw.DW_FILE_ROW_NUMBER
    );

    DELETE FROM IDENTIFIER(:V_RAW_TABLE) raw
    USING IDENTIFIER(:V_CONTROLS) selected, IDENTIFIER(:V_ARCHIVE) archived
    WHERE selected.DW_FILE_NM = raw.DW_FILE_NM
      AND archived.SUBJECT_AREA = :V_SUBJECT_AREA AND archived.PKG_GROUP_NAME = :PKG_GROUP_NAME
      AND archived.DW_FILE_NM = raw.DW_FILE_NM
      AND archived.DW_FILE_CHECK_SUM IS NOT DISTINCT FROM raw.DW_FILE_CHECK_SUM
      AND archived.DW_FILE_ROW_NUMBER = raw.DW_FILE_ROW_NUMBER;

    -- Retain the metadata/API column as a derived display value. It is never
    -- read from or written to a RAW subject table.
    UPDATE IDENTIFIER(:V_METADATA_TABLE) m
    SET DW_READY_TO_LOAD_FL = TRUE, READY_STATUS = ''READY_TO_LOAD''
    FROM IDENTIFIER(:V_LATEST_LOG) l
    WHERE m.RAW_TABLE = :V_RAW_TABLE AND m.PKG_GROUP_NAME = :PKG_GROUP_NAME
      AND m.RUN_DTTM = (SELECT MAX(RUN_DTTM) FROM IDENTIFIER(:V_SNAPSHOT))
      AND l.PKG_GROUP_NAME = m.PKG_GROUP_NAME AND l.DLVY_END_DATE = m.DLVY_END_DATE
      AND l.DLVY_SOURCE_ID = m.DLVY_SOURCE_ID AND l.DLVY_PKG_ID = m.DLVY_PKG_ID
      AND l.DLVY_PKG_YEAR = m.DLVY_PKG_YEAR AND l.DLVY_PKG_YEAR_SEQ_NO = m.DLVY_PKG_YEAR_SEQ_NO
      AND l.STATUS IN (''UPDATED'', ''PREVIOUSLY_LOADED'')
      AND (m.FILE_TYPE = ''PAYLOAD'' OR m.ORIGINAL_FILE_NAME IN (SELECT DW_FILE_NM FROM IDENTIFIER(:V_CONTROLS)));

    UPDATE IDENTIFIER(:V_SOURCE_MAP) target
    SET DLVY_PKG_YEAR = accepted.MAX_YEAR, DLVY_PKG_YEAR_SEQ_NO = accepted.MAX_SEQUENCE,
        UPDATED_AT = SYSDATE()
    FROM (
        SELECT DLVY_SOURCE_ID, DLVY_PKG_ID,
               MAX_BY(DLVY_PKG_YEAR, DLVY_PKG_YEAR || DLVY_PKG_YEAR_SEQ_NO) AS MAX_YEAR,
               MAX_BY(DLVY_PKG_YEAR_SEQ_NO, DLVY_PKG_YEAR || DLVY_PKG_YEAR_SEQ_NO) AS MAX_SEQUENCE
        FROM IDENTIFIER(:V_LATEST_LOG)
        WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME AND STATUS = ''UPDATED''
        GROUP BY DLVY_SOURCE_ID, DLVY_PKG_ID
    ) accepted
    WHERE target.ACTIVE_FL = TRUE AND target.PKG_GROUP_NAME = :PKG_GROUP_NAME
      AND target.SOURCE_ID = accepted.DLVY_SOURCE_ID AND target.PKG_ID = accepted.DLVY_PKG_ID
      AND (target.DLVY_PKG_YEAR IS NULL OR accepted.MAX_YEAR > target.DLVY_PKG_YEAR
           OR (accepted.MAX_YEAR = target.DLVY_PKG_YEAR
               AND accepted.MAX_SEQUENCE > target.DLVY_PKG_YEAR_SEQ_NO));

    V_RESULT := (SELECT * FROM IDENTIFIER(:V_LATEST_LOG) WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME
                 ORDER BY DLVY_END_DATE, DLVY_SOURCE_ID);
    COMMIT;
    V_TRANSACTION_STARTED := FALSE;
    DROP TABLE IF EXISTS IDENTIFIER(:V_CONTROLS);
    DROP TABLE IF EXISTS IDENTIFIER(:V_SNAPSHOT);
    RETURN TABLE(V_RESULT);
EXCEPTION
    WHEN OTHER THEN
        IF (V_TRANSACTION_STARTED) THEN
            ROLLBACK;
        END IF;
        IF (V_CONTROLS IS NOT NULL) THEN
            DROP TABLE IF EXISTS IDENTIFIER(:V_CONTROLS);
        END IF;
        IF (V_SNAPSHOT IS NOT NULL) THEN
            DROP TABLE IF EXISTS IDENTIFIER(:V_SNAPSHOT);
        END IF;
        RAISE;
END;
';

CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_META.LOAD_RAW_LIM_FROM_STAGE("P_LIM_FORMAT" VARCHAR, "P_FROM_DLVY_END_DATE" DATE, "P_TO_DLVY_END_DATE" DATE, "P_RELOAD" BOOLEAN, "P_RESET_DLVY_PKG_CHECK" BOOLEAN, "P_SET_READY_TO_LOAD" BOOLEAN, "P_DATABASE" VARCHAR DEFAULT 'KUMO_DEV')
COPY GRANTS
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS '
-- DEV_LIM_BATCH_READINESS_V1
DECLARE
    V_SOURCE_MAP VARCHAR;
    V_LOAD_RESULTS VARCHAR;
    V_READY_LATEST VARCHAR;
    V_READY_HISTORY VARCHAR;
    V_RAW_METADATA VARCHAR;
    V_BATCH_CONTROL VARCHAR;
    V_ARCHIVE VARCHAR;
    V_INFORMATION_TABLES VARCHAR;
    V_FILES_TABLE VARCHAR;
    V_PACKAGES_TABLE VARCHAR;
    EX_INVALID_DATABASE EXCEPTION (-20004, ''Invalid P_DATABASE'');

    V_LIM_FORMAT       VARCHAR;
    V_RELOAD           BOOLEAN;
    V_FULL_RELOAD      BOOLEAN;
    V_LOAD_MODE        VARCHAR;
    V_RESET_DLVY_PKG_CHECK BOOLEAN;
    V_SET_READY_TO_LOAD    BOOLEAN;

    V_DB                   VARCHAR;
    V_TARGET_TABLE_SHORT   VARCHAR;
    V_TARGET_TABLE         VARCHAR;
    V_STAGE_REF            VARCHAR;
    V_STAGE_URL            VARCHAR;
    V_STAGE_DESC_QUERY_ID  VARCHAR;
    V_FILE_FORMAT          VARCHAR;

    V_LIST_PATTERN     VARCHAR;
    V_LIST_SQL         VARCHAR;
    V_LIST_QUERY_ID    VARCHAR;

    V_TABLE_EXISTS         NUMBER;
    V_FILES_SELECTED       NUMBER := 0;
    V_INVALID_DATES        NUMBER := 0;
    V_PACKAGES_CAPTURED    NUMBER := 0;
    V_TMP_SET_READY_LOG_DELETED  NUMBER := 0;
    V_SET_READY_LOG_DELETED      NUMBER := 0;
    V_LOAD_RESULTS_DELETED       NUMBER := 0;
    V_RAW_LIM_META_DELETED       NUMBER := 0;
    V_LIM_BATCH_CTRL_DELETED     NUMBER := 0;
    V_RAW_ROWS_DELETED           NUMBER := 0;
    V_PKG_CONTROL_RESET          NUMBER := 0;
    V_COPY_COMMANDS        NUMBER := 0;
    V_ROWS_READY_UPDATED   NUMBER := 0;
    V_FILES_REGISTERED NUMBER := 0;
    V_CONTROL_ARCHIVE_DELETED NUMBER := 0;

    -- SDL clone+drop variables
    V_SDL_PREFIX           VARCHAR;
    V_SDL_PATTERN          VARCHAR;
    V_SDL_TABLES_CLONED    NUMBER := 0;
    V_SDL_TABLES_DROPPED   NUMBER := 0;

    V_COPY_SQL         VARCHAR;
    V_FORCE_FLAG       VARCHAR;

    EX_INVALID_LIM_FORMAT   EXCEPTION (-20001, ''Invalid P_LIM_FORMAT'');
    EX_INVALID_DATE_RANGE   EXCEPTION (-20002, ''Invalid date range'');
    EX_TARGET_NOT_FOUND     EXCEPTION (-20003, ''Target RAW table not found'');

BEGIN

    V_DB := UPPER(TRIM(P_DATABASE));
    IF (V_DB IS NULL OR V_DB <> ''KUMO_DEV'') THEN
        RAISE EX_INVALID_DATABASE;
    END IF;
    V_SOURCE_MAP := V_DB || ''.MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE'';
    V_LOAD_RESULTS := V_DB || ''.MONITOR_META.RAW_LOAD_RESULTS'';
    V_READY_LATEST := V_DB || ''.MONITOR_META._TMP_SET_READY_LOG'';
    V_READY_HISTORY := V_DB || ''.MONITOR_META._SET_READY_LOG'';
    V_RAW_METADATA := V_DB || ''.MONITOR_META.RAW_LIM_META'';
    V_BATCH_CONTROL := V_DB || ''.META.LIM_BATCH_CTRL'';
    V_ARCHIVE := V_DB || ''.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE'';
    V_INFORMATION_TABLES := V_DB || ''.INFORMATION_SCHEMA.TABLES'';
    V_FILES_TABLE := V_DB || ''.MONITOR_META.TMP_RAW_LIM_FILES'';
    V_PACKAGES_TABLE := V_DB || ''.MONITOR_META.TMP_RAW_LIM_RELOAD_PACKAGES'';

    V_LIM_FORMAT := UPPER(TRIM(P_LIM_FORMAT));
    V_RELOAD := COALESCE(P_RELOAD, FALSE);
    V_RESET_DLVY_PKG_CHECK := COALESCE(P_RESET_DLVY_PKG_CHECK, FALSE);
    V_SET_READY_TO_LOAD := COALESCE(P_SET_READY_TO_LOAD, FALSE);

    -- 2. Validate
    IF (V_LIM_FORMAT IS NULL OR NOT REGEXP_LIKE(V_LIM_FORMAT, ''^[A-Z0-9_]+$''))
    THEN
        RAISE EX_INVALID_LIM_FORMAT;
    END IF;

    IF (
           (P_FROM_DLVY_END_DATE IS NULL AND P_TO_DLVY_END_DATE IS NOT NULL)
        OR (P_FROM_DLVY_END_DATE IS NOT NULL AND P_TO_DLVY_END_DATE IS NULL)
        OR (P_FROM_DLVY_END_DATE IS NOT NULL AND P_FROM_DLVY_END_DATE > P_TO_DLVY_END_DATE)
    )
    THEN
        RAISE EX_INVALID_DATE_RANGE;
    END IF;

    -- 3. Load mode
    V_FULL_RELOAD := V_RELOAD AND P_FROM_DLVY_END_DATE IS NULL AND P_TO_DLVY_END_DATE IS NULL;

    IF (V_FULL_RELOAD) THEN
        V_LOAD_MODE := ''FULL_RELOAD'';
    ELSEIF (V_RELOAD) THEN
        V_LOAD_MODE := ''PARTIAL_RELOAD'';
    ELSE
        V_LOAD_MODE := ''NORMAL_LOAD'';
    END IF;

    -- 4. Object names
    V_TARGET_TABLE_SHORT := ''RAW_LIM_'' || V_LIM_FORMAT;
    V_TARGET_TABLE := V_DB || ''.RAW_LIM.'' || V_TARGET_TABLE_SHORT;
    V_STAGE_REF := ''@'' || V_DB || ''.META.AZURE_LIM_STAGE'';
    V_FILE_FORMAT := V_DB || ''.META.KUMO_LIM'';

    -- LIST returns full external URLs, while COPY FILES requires paths relative
    -- to the stage URL. Read that URL once so nested dataset folders are kept.
    EXECUTE IMMEDIATE ''DESC STAGE '' || SUBSTR(V_STAGE_REF, 2);
    V_STAGE_DESC_QUERY_ID := SQLID;
    SELECT RTRIM(PARSE_JSON("property_value")[0]::VARCHAR, ''/'')
      INTO :V_STAGE_URL
      FROM TABLE(RESULT_SCAN(:V_STAGE_DESC_QUERY_ID))
     WHERE UPPER("property") = ''URL'';

    -- SDL prefix: first 4 characters of LIM_FORMAT (e.g. DISB from DISB)
    V_SDL_PREFIX := LEFT(V_LIM_FORMAT, 4);
    V_SDL_PATTERN := ''^SDL_'' || V_SDL_PREFIX || ''_[0-9]{3}$'';

    -- 5. Verify target
    SELECT COUNT(*) INTO :V_TABLE_EXISTS
      FROM IDENTIFIER(:V_INFORMATION_TABLES)
     WHERE TABLE_SCHEMA = ''RAW_LIM'' AND TABLE_NAME = :V_TARGET_TABLE_SHORT;

    IF (V_TABLE_EXISTS = 0) THEN
        RAISE EX_TARGET_NOT_FOUND;
    END IF;

    -- 6. Temp tables
    CREATE OR REPLACE TEMPORARY TABLE IDENTIFIER(:V_FILES_TABLE)
    (FILE_PATH VARCHAR, FILE_NM VARCHAR, DLVY_END_DATE DATE);

    CREATE OR REPLACE TEMPORARY TABLE IDENTIFIER(:V_PACKAGES_TABLE)
    (PKG_GROUP_NAME VARCHAR, DLVY_END_DATE DATE, DLVY_SOURCE_ID VARCHAR,
     DLVY_SUBJECT_AREA_ID VARCHAR, DLVY_PKG_ID VARCHAR,
     DLVY_PKG_YEAR VARCHAR, DLVY_PKG_YEAR_SEQ_NO VARCHAR);

    -- 7. List files
    V_LIST_PATTERN := ''^(?!.*(?i:deleted_)).*'' || V_LIM_FORMAT || ''[^/]*$'';
    V_LIST_SQL := ''LIST '' || V_STAGE_REF || '' PATTERN = '''''' || V_LIST_PATTERN || '''''''';
    EXECUTE IMMEDIATE :V_LIST_SQL;
    V_LIST_QUERY_ID := SQLID;

    -- 8. Parse dates
    INSERT INTO IDENTIFIER(:V_FILES_TABLE) (FILE_PATH, FILE_NM, DLVY_END_DATE)
    SELECT
          IFF(
              STARTSWITH("name", :V_STAGE_URL || ''/''),
              -- RAW_LIM_COPY_FILES_STAGE_SEPARATOR_V1
              -- FILES paths need a leading slash because an external stage URL
              -- and a file name are concatenated without an implicit separator.
              SUBSTR("name", LENGTH(:V_STAGE_URL) + 1),
              "name"
          ),
          SPLIT_PART("name", ''/'', -1),
          TRY_TO_DATE(SPLIT_PART(SPLIT_PART("name", ''/'', -1), ''_'', -2), ''YYYYMMDD'')
      FROM TABLE(RESULT_SCAN(:V_LIST_QUERY_ID));

    -- 9. Remove invalid dates
    DELETE FROM IDENTIFIER(:V_FILES_TABLE) WHERE DLVY_END_DATE IS NULL;
    V_INVALID_DATES := SQLROWCOUNT;

    -- 10. Date filter (partial only)
    IF (V_RELOAD AND NOT V_FULL_RELOAD) THEN
        DELETE FROM IDENTIFIER(:V_FILES_TABLE)
         WHERE DLVY_END_DATE < :P_FROM_DLVY_END_DATE
            OR DLVY_END_DATE > :P_TO_DLVY_END_DATE;
    END IF;

    SELECT COUNT(*) INTO :V_FILES_SELECTED FROM IDENTIFIER(:V_FILES_TABLE);

    -- 10b. Early exit
    IF (V_FILES_SELECTED = 0) THEN
        RETURN OBJECT_CONSTRUCT(
            ''STATUS'', ''NO_FILES_FOUND'', ''DATABASE'', V_DB,
            ''LOAD_MODE'', V_LOAD_MODE, ''LIM_FORMAT'', V_LIM_FORMAT,
            ''TARGET_TABLE'', V_TARGET_TABLE,
            ''FROM_DLVY_END_DATE'', P_FROM_DLVY_END_DATE,
            ''TO_DLVY_END_DATE'', P_TO_DLVY_END_DATE,
            ''FILES_SELECTED'', 0
        );
    END IF;

    -- 11. Transaction
    BEGIN TRANSACTION;

    -- 12. Reload cleanup
    IF (V_RELOAD) THEN

        -- 12a. Capture packages
        INSERT INTO IDENTIFIER(:V_PACKAGES_TABLE)
        SELECT DISTINCT
              C.PKG_GROUP_NAME, F.DLVY_END_DATE, R.DLVY_SOURCE_ID,
              R.DLVY_SUBJECT_AREA_ID, R.DLVY_PKG_ID, R.DLVY_PKG_YEAR,
              R.DLVY_PKG_YEAR_SEQ_NO
          FROM IDENTIFIER(:V_LOAD_RESULTS) R
          INNER JOIN IDENTIFIER(:V_FILES_TABLE) F ON R.FILE_NM = F.FILE_NM
          LEFT JOIN IDENTIFIER(:V_SOURCE_MAP) C
             ON UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
            AND C.SOURCE_ID IS NOT DISTINCT FROM R.DLVY_SOURCE_ID
            AND C.PKG_ID IS NOT DISTINCT FROM R.DLVY_PKG_ID
            AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE;
        V_PACKAGES_CAPTURED := SQLROWCOUNT;

        -- 12b. Delete _TMP_SET_READY_LOG (config-driven)
        DELETE FROM IDENTIFIER(:V_READY_LATEST) T
         USING IDENTIFIER(:V_SOURCE_MAP) C
         WHERE UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
           AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE
           AND T.PKG_GROUP_NAME IS NOT DISTINCT FROM C.PKG_GROUP_NAME
           AND T.DLVY_SOURCE_ID IS NOT DISTINCT FROM C.SOURCE_ID
           AND T.DLVY_PKG_ID IS NOT DISTINCT FROM C.PKG_ID
           AND (:V_FULL_RELOAD OR T.DLVY_END_DATE IN (SELECT DLVY_END_DATE FROM IDENTIFIER(:V_FILES_TABLE)));
        V_TMP_SET_READY_LOG_DELETED := SQLROWCOUNT;

        -- 12c. Delete _SET_READY_LOG (config-driven)
        DELETE FROM IDENTIFIER(:V_READY_HISTORY) T
         USING IDENTIFIER(:V_SOURCE_MAP) C
         WHERE UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
           AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE
           AND T.PKG_GROUP_NAME IS NOT DISTINCT FROM C.PKG_GROUP_NAME
           AND T.DLVY_SOURCE_ID IS NOT DISTINCT FROM C.SOURCE_ID
           AND T.DLVY_PKG_ID IS NOT DISTINCT FROM C.PKG_ID
           AND (:V_FULL_RELOAD OR T.DLVY_END_DATE IN (SELECT DLVY_END_DATE FROM IDENTIFIER(:V_FILES_TABLE)));
        V_SET_READY_LOG_DELETED := SQLROWCOUNT;

        -- RAW_LIM_FULL_RELOAD_BATCH_CTRL_V1
        -- Clear all dates/statuses for this RAW subject, including qualified
        -- DW_TABLE_NM values retained by earlier batch registration versions.
        DELETE FROM IDENTIFIER(:V_BATCH_CONTROL)
         WHERE UPPER(DW_TABLE_NM) IN (''RAW_LIM.'' || :V_TARGET_TABLE_SHORT, :V_TARGET_TABLE)
           AND (:V_FULL_RELOAD OR DW_FILE_NM IN (SELECT FILE_NM FROM IDENTIFIER(:V_FILES_TABLE)));
        V_LIM_BATCH_CTRL_DELETED := SQLROWCOUNT;

        DELETE FROM IDENTIFIER(:V_ARCHIVE)
         WHERE SUBJECT_AREA = :V_LIM_FORMAT
           AND (:V_FULL_RELOAD OR DW_FILE_NM IN (SELECT FILE_NM FROM IDENTIFIER(:V_FILES_TABLE)));
        V_CONTROL_ARCHIVE_DELETED := SQLROWCOUNT;

        -- 12d. Delete RAW_LOAD_RESULTS
        IF (V_FULL_RELOAD) THEN
            DELETE FROM IDENTIFIER(:V_LOAD_RESULTS)
             WHERE TABLE_NAME = :V_TARGET_TABLE_SHORT;
        ELSE
            DELETE FROM IDENTIFIER(:V_LOAD_RESULTS)
             WHERE FILE_NM IN (SELECT FILE_NM FROM IDENTIFIER(:V_FILES_TABLE));
        END IF;
        V_LOAD_RESULTS_DELETED := SQLROWCOUNT;

        -- 12f. Delete RAW_LIM_META
        DELETE FROM IDENTIFIER(:V_RAW_METADATA)
         WHERE DLVY_SUBJECT_AREA_ID = :V_LIM_FORMAT
           AND (:V_FULL_RELOAD OR DLVY_END_DATE IN (SELECT DLVY_END_DATE FROM IDENTIFIER(:V_FILES_TABLE)));
        V_RAW_LIM_META_DELETED := SQLROWCOUNT;

        -- 12e. Clean RAW target
        IF (V_FULL_RELOAD) THEN
            SELECT COUNT(*) INTO :V_RAW_ROWS_DELETED FROM IDENTIFIER(:V_TARGET_TABLE);
            TRUNCATE TABLE IDENTIFIER(:V_TARGET_TABLE);
        ELSE
            DELETE FROM IDENTIFIER(:V_TARGET_TABLE)
             WHERE DW_FILE_NM IN (SELECT FILE_NM FROM IDENTIFIER(:V_FILES_TABLE));
            V_RAW_ROWS_DELETED := SQLROWCOUNT;
        END IF;

        -- 12g. Clone and drop SDL tables
        LET V_TODAY_STR VARCHAR := TO_CHAR(CURRENT_DATE(), ''YYYYMMDD'');
        LET V_CLONE_NAME VARCHAR;
        LET V_CLONE_SEQ NUMBER;
        LET V_TBL_NAME VARCHAR;
        LET C_SDL RESULTSET;

        C_SDL := (SELECT TABLE_NAME
                    FROM IDENTIFIER(:V_INFORMATION_TABLES)
                   WHERE TABLE_SCHEMA = ''SDL''
                     AND TABLE_NAME RLIKE :V_SDL_PATTERN);

        LET C_SDL_CUR CURSOR FOR C_SDL;
        OPEN C_SDL_CUR;
        FOR REC IN C_SDL_CUR DO
            V_TBL_NAME := REC.TABLE_NAME;

            -- Determine next clone number for today
            SELECT COALESCE(MAX(
                TRY_TO_NUMBER(
                    REGEXP_SUBSTR(TABLE_NAME, ''_([0-9]+)$'', 1, 1, ''e'', 1)
                )
            ), 0) + 1
            INTO :V_CLONE_SEQ
            FROM IDENTIFIER(:V_INFORMATION_TABLES)
            WHERE TABLE_SCHEMA = ''SDL''
              AND TABLE_NAME LIKE ''X_'' || :V_TODAY_STR || ''_'' || :V_TBL_NAME || ''_%'';

            V_CLONE_NAME := ''X_'' || V_TODAY_STR || ''_'' || V_TBL_NAME || ''_'' || V_CLONE_SEQ::VARCHAR;

            -- Clone the table
            EXECUTE IMMEDIATE ''CREATE TABLE '' || :V_DB || ''.SDL.'' || :V_CLONE_NAME || '' CLONE '' || :V_DB || ''.SDL.'' || :V_TBL_NAME;
            V_SDL_TABLES_CLONED := V_SDL_TABLES_CLONED + 1;

            -- Drop the original
            EXECUTE IMMEDIATE ''DROP TABLE IF EXISTS '' || :V_DB || ''.SDL.'' || :V_TBL_NAME;
            V_SDL_TABLES_DROPPED := V_SDL_TABLES_DROPPED + 1;
        END FOR;
        CLOSE C_SDL_CUR;

    END IF;

    -- 13. Reset sequence control
    IF (V_RESET_DLVY_PKG_CHECK OR V_FULL_RELOAD) THEN
        UPDATE IDENTIFIER(:V_SOURCE_MAP)
           SET DLVY_PKG_YEAR = NULL,
               DLVY_PKG_YEAR_SEQ_NO = NULL,
               UPDATED_AT = CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
         WHERE UPPER(SUBJECT_AREA) = :V_LIM_FORMAT
           AND COALESCE(ACTIVE_FL, TRUE) = TRUE;
        V_PKG_CONTROL_RESET := SQLROWCOUNT;
    END IF;

    -- 14. FORCE flag. Reloads must ignore retained COPY load history.
    IF (V_RELOAD) THEN
        V_FORCE_FLAG := '' FORCE = TRUE'';
    ELSE
        V_FORCE_FLAG := '''';
    END IF;

    -- 15. RAW_LIM_FULL_RELOAD_PATTERN_V1
    -- Full reload performance test: use one subject-wide PATTERN COPY. Partial
    -- and normal loads retain exact FILES batches to preserve their selection.
    IF (V_FULL_RELOAD) THEN
        V_COPY_SQL :=
              ''COPY INTO '' || V_TARGET_TABLE
            || '' (DW_FILE_NM, DW_FILE_DTTM, DW_FILE_CHECK_SUM, DW_FILE_ROW_NUMBER, DW_LOAD_DTTM, DATA)''
            || '' FROM (SELECT''
            || ''   SPLIT_PART(METADATA$FILENAME, ''''/'''', -1)''
            || '' , METADATA$FILE_LAST_MODIFIED''
            || '' , METADATA$FILE_CONTENT_KEY''
            || '' , METADATA$FILE_ROW_NUMBER''
            || '' , SYSDATE()''
            || '' , $1''
            || '' FROM '' || V_STAGE_REF || '')''
            || '' FILE_FORMAT = (FORMAT_NAME = '''''' || V_FILE_FORMAT || '''''')''
            || '' PATTERN = '''''' || V_LIST_PATTERN || ''''''''
            || V_FORCE_FLAG
            || '' ON_ERROR = ABORT_STATEMENT'';

        EXECUTE IMMEDIATE :V_COPY_SQL;
        V_COPY_COMMANDS := 1;
    ELSE
        LET V_FILE_BATCHES RESULTSET := (
            SELECT
                BATCH_NO,
                LISTAGG(
                    '''''''' || REPLACE(FILE_PATH, '''''''', '''''''''''') || '''''''',
                    '',''
                ) WITHIN GROUP (ORDER BY FILE_PATH) AS FILE_LIST
            FROM (
                SELECT
                    FILE_PATH,
                    FLOOR((ROW_NUMBER() OVER (ORDER BY FILE_PATH) - 1) / 1000) AS BATCH_NO
                FROM IDENTIFIER(:V_FILES_TABLE)
            )
            GROUP BY BATCH_NO
        );

        FOR V_FILE_BATCH IN V_FILE_BATCHES DO
            V_COPY_SQL :=
                  ''COPY INTO '' || V_TARGET_TABLE
                || '' (DW_FILE_NM, DW_FILE_DTTM, DW_FILE_CHECK_SUM, DW_FILE_ROW_NUMBER, DW_LOAD_DTTM, DATA)''
                || '' FROM (SELECT''
                || ''   SPLIT_PART(METADATA$FILENAME, ''''/'''', -1)''
                || '' , METADATA$FILE_LAST_MODIFIED''
                || '' , METADATA$FILE_CONTENT_KEY''
                || '' , METADATA$FILE_ROW_NUMBER''
                || '' , SYSDATE()''
                || '' , $1''
                || '' FROM '' || V_STAGE_REF || '')''
                || '' FILES = ('' || V_FILE_BATCH.FILE_LIST || '')''
                || '' FILE_FORMAT = (FORMAT_NAME = '''''' || V_FILE_FORMAT || '''''')''
                || V_FORCE_FLAG
                || '' ON_ERROR = ABORT_STATEMENT'';

            EXECUTE IMMEDIATE :V_COPY_SQL;
            V_COPY_COMMANDS := V_COPY_COMMANDS + 1;
        END FOR;
    END IF;

    -- Explicit bypass registers only selected, present, nonempty payload files.
    -- Empty/control files never enter the dbt payload queue.
    IF (V_SET_READY_TO_LOAD) THEN
        MERGE INTO IDENTIFIER(:V_BATCH_CONTROL) tgt
        USING (
            SELECT DISTINCT ''RAW_LIM.'' || :V_TARGET_TABLE_SHORT AS DW_TABLE_NM,
                   raw.DW_FILE_NM, LEFT(REGEXP_SUBSTR(raw.DW_FILE_NM, ''[^/]+$''), 3) AS DW_SRC_SYST
            FROM IDENTIFIER(:V_TARGET_TABLE) raw
            JOIN IDENTIFIER(:V_FILES_TABLE) selected ON selected.FILE_NM = raw.DW_FILE_NM
            WHERE SUBSTR(REGEXP_SUBSTR(raw.DW_FILE_NM, ''[^/]+$''), 8, 3) <> ''000''
              AND LEFT(TRIM(raw.DATA), 2) = ''10''
        ) files
        ON UPPER(tgt.DW_TABLE_NM) IN (files.DW_TABLE_NM, :V_TARGET_TABLE)
           AND tgt.DW_FILE_NM = files.DW_FILE_NM AND tgt.DW_SRC_SYST = files.DW_SRC_SYST
        WHEN NOT MATCHED THEN INSERT (
            READY_TO_LOAD_DTTM, DW_TABLE_NM, DW_FILE_NM, DW_SRC_SYST, PROCESSED_FL, DELETED_FL
        ) VALUES (SYSDATE(), files.DW_TABLE_NM, files.DW_FILE_NM, files.DW_SRC_SYST, FALSE, FALSE);
        V_FILES_REGISTERED := SQLROWCOUNT;
    END IF;

    -- 18. Commit
    COMMIT;

    -- 19. Return
    RETURN OBJECT_CONSTRUCT(
        ''STATUS'', ''SUCCESS'',
        ''DATABASE'', V_DB,
        ''LOAD_MODE'', V_LOAD_MODE,
        ''LIM_FORMAT'', V_LIM_FORMAT,
        ''TARGET_TABLE'', V_TARGET_TABLE,
        ''FROM_DLVY_END_DATE'', P_FROM_DLVY_END_DATE,
        ''TO_DLVY_END_DATE'', P_TO_DLVY_END_DATE,
        ''FILES_SELECTED'', V_FILES_SELECTED,
        ''INVALID_FILE_DATES'', V_INVALID_DATES,
        ''PACKAGES_CAPTURED'', V_PACKAGES_CAPTURED,
        ''TMP_SET_READY_LOG_DELETED'', V_TMP_SET_READY_LOG_DELETED,
        ''SET_READY_LOG_DELETED'', V_SET_READY_LOG_DELETED,
        ''RAW_LOAD_RESULTS_DELETED'', V_LOAD_RESULTS_DELETED,
        ''RAW_LIM_META_DELETED'', V_RAW_LIM_META_DELETED,
        ''LIM_BATCH_CTRL_DELETED'', V_LIM_BATCH_CTRL_DELETED,
        ''RAW_ROWS_DELETED'', V_RAW_ROWS_DELETED,
        ''PKG_CONTROL_ROWS_RESET'', V_PKG_CONTROL_RESET,
        ''COPY_COMMANDS_EXECUTED'', V_COPY_COMMANDS,
        ''READY_ROWS_UPDATED'', V_ROWS_READY_UPDATED,
        ''FILES_REGISTERED'', V_FILES_REGISTERED,
        ''CONTROL_ARCHIVE_DELETED'', V_CONTROL_ARCHIVE_DELETED,
        ''SDL_TABLES_CLONED'', V_SDL_TABLES_CLONED,
        ''SDL_TABLES_DROPPED'', V_SDL_TABLES_DROPPED,
        ''RELOAD'', V_RELOAD,
        ''RESET_DLVY_PKG_CHECK'', V_RESET_DLVY_PKG_CHECK,
        ''SET_READY_TO_LOAD'', V_SET_READY_TO_LOAD
    );

EXCEPTION
    WHEN OTHER THEN
        IF (CURRENT_TRANSACTION() IS NOT NULL) THEN
            ROLLBACK;
        END IF;
        RAISE;

END;
';
