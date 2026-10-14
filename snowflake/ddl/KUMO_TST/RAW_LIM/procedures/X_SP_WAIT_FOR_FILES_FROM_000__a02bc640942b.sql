-- Source: KUMO_TST.RAW_LIM.X_SP_WAIT_FOR_FILES_FROM_000(VARCHAR, VARCHAR, NUMBER, NUMBER)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_TST";
USE SCHEMA "KUMO_TST"."RAW_LIM";

CREATE OR REPLACE PROCEDURE KUMO_TST.RAW_LIM.X_SP_WAIT_FOR_FILES_FROM_000("DLVY_SUBJECT_AREA_NM" VARCHAR, "PKG_GROUP_NAME" VARCHAR, "RETRIES" NUMBER(38,0), "DELAY_BETWEEN_RETRIES_SEC" NUMBER(38,0))
RETURNS VARIANT
LANGUAGE SQL
COMMENT='user-defined procedure'
EXECUTE AS CALLER
AS '
DECLARE
    v_subject            STRING := UPPER(TRIM(DLVY_SUBJECT_AREA_NM));
    v_group_filter       STRING := IFF(PKG_GROUP_NAME IS NULL, NULL, TRIM(PKG_GROUP_NAME));

    v_raw_table          STRING := ''KUMO_TST.RAW_LIM.RAW_LIM_'' || v_subject;

    v_meta_table         STRING := ''RAW_LIM_PKG_GROUP_SOURCE'';
    v_log_table          STRING := ''RAW_LIM_PKG_LOG'';
    v_status_table       STRING := ''RAW_LIM_'' || v_subject || ''_PKG_STATUS'';

    v_run_id             STRING := UUID_STRING();

    v_000_retries        NUMBER := RETRIES;
    v_000_delay_seconds  NUMBER := DELAY_BETWEEN_RETRIES_SEC;

    v_pkg_retries        NUMBER := 3;
    v_pkg_delay_seconds  NUMBER := 3;

    v_attempt            NUMBER := 0;

    v_has_raw_rows       NUMBER := 0;

    v_group_name_scalar  STRING;

    v_groups_cnt         NUMBER := 0;
    v_todo_cnt           NUMBER := 0;

    v_step               STRING := ''INIT'';

    v_group_idx          NUMBER := 0;

    v_expected_cnt       NUMBER := 0;
    v_missing_cnt        NUMBER := 0;
    v_rowcount_fail_cnt  NUMBER := 0;
    v_all_received       BOOLEAN := FALSE;

    v_rows_updated       NUMBER := 0;
    v_update_qid         STRING;

    v_block_reason       STRING;
    v_block_reason_json  VARIANT;
    v_block_reason_last  STRING;

    v_received_physical_cnt  NUMBER := 0;
    v_received_valid_cnt     NUMBER := 0;
    v_rowcount_not_eval_cnt  NUMBER := 0;

    v_last_done_end_dt   DATE := NULL;
    v_cycle_end_dt       DATE;
    v_current_end_dt     DATE;
    v_blocked_end_dt     DATE;

    v_attempt_blocked    BOOLEAN := FALSE;
    v_cycle_done         BOOLEAN := FALSE;

BEGIN
    v_step := ''USE_DB_SCHEMA'';
    EXECUTE IMMEDIATE ''USE DATABASE KUMO_ADMIN'';
    EXECUTE IMMEDIATE ''USE SCHEMA WORKFLOW_MANAGER'';

    --------------------------------------------------------------------------
    -- Base tables (meta/log/status)
    --------------------------------------------------------------------------
    v_step := ''CREATE_META_TABLE'';
    EXECUTE IMMEDIATE
    ''CREATE TABLE IF NOT EXISTS '' || v_meta_table || '' (
        PKG_GROUP_NAME  STRING,
        SUBJECT_AREA    STRING,
        SOURCE_ID       STRING,
        ACTIVE_FL       BOOLEAN,
        UPDATED_AT      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
     )'';

    v_step := ''CREATE_LOG_TABLE'';
    EXECUTE IMMEDIATE
    ''CREATE TABLE IF NOT EXISTS '' || v_log_table || '' (
        RUN_ID          STRING,
        EVENT_DTTM      TIMESTAMP_NTZ,
        SUBJECT_AREA    STRING,
        SOURCE_ID       STRING,
        PKG_GROUP_NAME  STRING,
        PHASE           STRING,
        ATTEMPT_NO      NUMBER,
        DLVY_END_DT     DATE,
        EVENT_TYPE      STRING,
        ROWS_AFFECTED   NUMBER,
        DETAILS         VARIANT
     )'';

    v_step := ''CREATE_STATUS_TABLE'';
    EXECUTE IMMEDIATE
    ''CREATE TABLE IF NOT EXISTS '' || v_status_table || '' (
        PKG_GROUP_NAME        STRING,
        DLVY_SUBJECT_AREA_NM   STRING,
        DLVY_SOURCE_NM         STRING,
        DLVY_END_DT            DATE,
        ALL_RECEIVED           BOOLEAN,
        EXPECTED_CNT           NUMBER,
        RECEIVED_CNT           NUMBER,
        MISSING_CNT            NUMBER,
        ROWCOUNT_FAIL_CNT      NUMBER,
        LAST_CHECKED_AT        TIMESTAMP_NTZ,
        UPDATED_AT             TIMESTAMP_NTZ
     )'';

    --------------------------------------------------------------------------
    -- Reusable scalar temp table
    --------------------------------------------------------------------------
    v_step := ''CREATE_TMP_SCALAR'';
    EXECUTE IMMEDIATE ''CREATE OR REPLACE TEMP TABLE TMP_SCALAR_NUM (VAL NUMBER)'';

    --------------------------------------------------------------------------
    -- RAW has rows?
    --------------------------------------------------------------------------
    v_step := ''RAW_HAS_ROWS'';
    EXECUTE IMMEDIATE ''TRUNCATE TABLE TMP_SCALAR_NUM'';
    EXECUTE IMMEDIATE
    ''INSERT INTO TMP_SCALAR_NUM(VAL)
     SELECT IFF(COUNT(*) > 0, 1, 0)
     FROM '' || v_raw_table;

    SELECT VAL INTO :v_has_raw_rows
    FROM (SELECT VAL FROM TMP_SCALAR_NUM LIMIT 1);

    IF (COALESCE(v_has_raw_rows, 0) = 0) THEN
        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT
            :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, NULL,
            ''INIT'', NULL, NULL, ''NO_ROWS_IN_RAW_TABLE'', 0,
            OBJECT_CONSTRUCT(''raw_table'', :v_raw_table);

        RETURN OBJECT_CONSTRUCT(''status'',''NO_ROWS_IN_RAW_TABLE'',''subject'',v_subject,''run_id'',v_run_id);
    END IF;

    --------------------------------------------------------------------------
    -- Build a numbered driver table for groups
    --------------------------------------------------------------------------
    v_step := ''CREATE_TMP_GROUPS_NUMBERED'';
    EXECUTE IMMEDIATE
    ''CREATE OR REPLACE TEMP TABLE TMP_GROUPS_NUMBERED AS
     SELECT
         PKG_GROUP_NAME::STRING AS PKG_GROUP_NAME,
         ROW_NUMBER() OVER (ORDER BY PKG_GROUP_NAME) AS RN
     FROM (
         SELECT DISTINCT PKG_GROUP_NAME
         FROM '' || v_meta_table || ''
         WHERE UPPER(TRIM(SUBJECT_AREA)) = '''''' || v_subject || ''''''
           AND ACTIVE_FL = TRUE '' ||
           IFF(v_group_filter IS NULL, '''',
               '' AND PKG_GROUP_NAME = '''''' || REPLACE(v_group_filter,'''''''','''''''''''') || ''''''''
           ) || ''
     )'';

    v_step := ''COUNT_GROUPS'';
    EXECUTE IMMEDIATE ''TRUNCATE TABLE TMP_SCALAR_NUM'';
    EXECUTE IMMEDIATE ''INSERT INTO TMP_SCALAR_NUM(VAL) SELECT COUNT(*) FROM TMP_GROUPS_NUMBERED'';

    SELECT VAL INTO :v_groups_cnt
    FROM (SELECT VAL FROM TMP_SCALAR_NUM LIMIT 1);

    IF (COALESCE(v_groups_cnt, 0) = 0) THEN
        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT
            :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, NULL,
            ''INIT'', NULL, NULL, ''NO_ACTIVE_GROUPS_FOUND'', 0,
            OBJECT_CONSTRUCT(''group_filter'', :v_group_filter);

        RETURN OBJECT_CONSTRUCT(''status'',''NO_ACTIVE_GROUPS_FOUND'',''subject'',v_subject,''group_filter'',v_group_filter,''run_id'',v_run_id);
    END IF;

    --------------------------------------------------------------------------
    -- Iterate groups using WHILE + RN
    --------------------------------------------------------------------------
    v_group_idx := 1;

    WHILE (v_group_idx <= v_groups_cnt) DO
        v_step := ''FETCH_GROUP_BY_RN'';
        v_group_name_scalar := (
            SELECT PKG_GROUP_NAME
            FROM TMP_GROUPS_NUMBERED
            WHERE RN = :v_group_idx
        );

        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT
            :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
            ''GROUP'', NULL, NULL, ''GROUP_START'', 0,
            OBJECT_CONSTRUCT(''group_index'', :v_group_idx, ''groups_total'', :v_groups_cnt);

        ----------------------------------------------------------------------
        -- Sources for group
        ----------------------------------------------------------------------
        v_step := ''CREATE_TMP_SOURCES'';
        EXECUTE IMMEDIATE
        ''CREATE OR REPLACE TEMP TABLE TMP_SOURCES AS
         SELECT DISTINCT UPPER(TRIM(SOURCE_ID)) AS SOURCE_ID
         FROM '' || v_meta_table || ''
         WHERE UPPER(TRIM(SUBJECT_AREA)) = '''''' || v_subject || ''''''
           AND PKG_GROUP_NAME = '''''' || REPLACE(v_group_name_scalar,'''''''','''''''''''') || ''''''
           AND ACTIVE_FL = TRUE'';

        EXECUTE IMMEDIATE ''CREATE OR REPLACE TEMP TABLE TMP_END_DTS_DONE (DLVY_END_DT DATE)'';
        v_last_done_end_dt := NULL;

        ----------------------------------------------------------------------
        -- PRE-LOOP: Build TMP_EXPECTED_ALL for ALL end-dates (one raw scan)
        ----------------------------------------------------------------------
        v_step := ''PRE_EXPECTED_ALL'';
        EXECUTE IMMEDIATE
        ''CREATE OR REPLACE TEMP TABLE TMP_EXPECTED_ALL AS
         WITH parse_00 AS (
             SELECT
                 UPPER(TRIM(SUBSTR(TRIM(r.DATA), 3, 3)))  AS SOURCE_SYS,
                 UPPER(TRIM(SUBSTR(TRIM(r.DATA), 6, 4)))  AS SUBJECT_AREA,
                 TRIM(SUBSTR(TRIM(r.DATA), 10, 3))        AS VERSION,
                 TRIM(SUBSTR(TRIM(r.DATA), 13, 9))        AS PKG,
                 TRIM(SUBSTR(TRIM(r.DATA), 22, 3))        AS FILE_NUMBER,
                 TRIM(SUBSTR(TRIM(r.DATA), 25, 3))        AS X,
                 TO_DATE(SUBSTR(TRIM(r.DATA), 38, 10), ''''YYYY-MM-DD'''') AS FILE_TO_DT
             FROM '' || v_raw_table || '' r
             JOIN TMP_SOURCES s
               ON UPPER(TRIM(SUBSTR(TRIM(r.DATA), 3, 3))) = s.SOURCE_ID
             WHERE SUBSTR(r.DW_FILE_NM, 8, 3) = ''''000''''
               AND SUBSTR(TRIM(r.DATA), 1, 2) = ''''10''''
               AND UPPER(TRIM(SUBSTR(TRIM(r.DATA), 6, 4))) = '''''' || v_subject || ''''''
               AND TRY_TO_DATE(SUBSTR(TRIM(r.DATA), 38, 10), ''''YYYY-MM-DD'''') IS NOT NULL
               AND IFNULL(r.DW_READY_TO_LOAD_FL, FALSE) = FALSE
         )
         SELECT
             (SOURCE_SYS || SUBJECT_AREA || FILE_NUMBER || ''''_'''' || X || ''''_'''' || VERSION || ''''_0000_'''' ||
              PKG || ''''_'''' || TO_CHAR(FILE_TO_DT, ''''YYYYMMDD'''')) AS FILE_EXPECTED,
             SUBJECT_AREA  AS DLVY_SUBJECT_AREA_NM,
             SOURCE_SYS    AS DLVY_SOURCE_NM,
             FILE_TO_DT    AS DLVY_END_DT
         FROM parse_00'';

        ----------------------------------------------------------------------
        -- PRE-LOOP: Build TMP_RECEIVED with rowcount stats (one raw scan)
        ----------------------------------------------------------------------
        v_step := ''PRE_RECEIVED'';
        EXECUTE IMMEDIATE
        ''CREATE OR REPLACE TEMP TABLE TMP_RECEIVED AS
         SELECT
             SUBSTR(DW_FILE_NM, 1, 42) AS FILE_RECEIVED_SHORT,
             MAX(IFF(SUBSTR(TRIM(DATA),1,2) = ''''99'''', ABS(TRY_TO_NUMBER(REGEXP_SUBSTR(TRIM(DATA), ''''[+-][0-9]+$''''))), NULL)) AS EXPECTED_ROWS,
             SUM(IFF(SUBSTR(TRIM(DATA),1,2) IN (''''00'''',''''99''''), 0, 1)) AS ACTUAL_ROWS,
             SUM(IFF(SUBSTR(TRIM(DATA),1,2) = ''''99'''', 1, 0)) AS FOOTER_ROWS,
             SUM(IFF(SUBSTR(TRIM(DATA),1,2) = ''''00'''', 1, 0)) AS HEADER_ROWS
         FROM '' || v_raw_table || ''
         WHERE SUBSTR(DW_FILE_NM, 8, 3) <> ''''000''''
         GROUP BY 1'';

        ----------------------------------------------------------------------
        -- GROUP_CYCLE_LOOP: PH1 (next common enddt) -> PH2 (process that enddt)
        ----------------------------------------------------------------------
        v_step := ''GROUP_CYCLE_LOOP'';
        WHILE (TRUE) DO

            ------------------------------------------------------------------
            -- PHASE 1: wait for NEXT common enddt across ALL sources
            ------------------------------------------------------------------
            v_step := ''PH1_WAIT_FOR_NEXT_COMMON_ENDDT'';
            v_attempt := 0;
            v_cycle_end_dt := NULL;

            WHILE (v_attempt < v_000_retries) DO
                v_attempt := v_attempt + 1;

                v_step := ''PH1_BUILD_000_ENDDTS'';
                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_000_ENDDTS AS
                 SELECT DISTINCT
                     s.SOURCE_ID,
                     TO_DATE(SUBSTR(TRIM(r.DATA), 38, 10), ''''YYYY-MM-DD'''') AS DLVY_END_DT
                 FROM TMP_SOURCES s
                 JOIN '' || v_raw_table || '' r
                   ON UPPER(TRIM(SUBSTR(TRIM(r.DATA), 3, 3))) = s.SOURCE_ID
                 WHERE SUBSTR(r.DW_FILE_NM, 8, 3) = ''''000''''
                   AND SUBSTR(TRIM(r.DATA), 1, 2) = ''''10''''
                   AND UPPER(TRIM(SUBSTR(TRIM(r.DATA), 6, 4))) = '''''' || v_subject || ''''''
                   AND TRY_TO_DATE(SUBSTR(TRIM(r.DATA), 38, 10), ''''YYYY-MM-DD'''') IS NOT NULL
                   AND IFNULL(r.DW_READY_TO_LOAD_FL, FALSE) = FALSE'';

                v_step := ''PH1_PICK_NEXT_COMMON_ENDDT'';

                IF (v_last_done_end_dt IS NULL) THEN
                    v_cycle_end_dt := (
                        SELECT MIN(DLVY_END_DT)
                        FROM (
                            SELECT DLVY_END_DT
                            FROM TMP_000_ENDDTS
                            GROUP BY DLVY_END_DT
                            HAVING COUNT(DISTINCT SOURCE_ID) = (SELECT COUNT(*) FROM TMP_SOURCES)
                        )
                    );
                ELSE
                    v_cycle_end_dt := (
                        SELECT MIN(DLVY_END_DT)
                        FROM (
                            SELECT DLVY_END_DT
                            FROM TMP_000_ENDDTS
                            WHERE DLVY_END_DT > :v_last_done_end_dt
                            GROUP BY DLVY_END_DT
                            HAVING COUNT(DISTINCT SOURCE_ID) = (SELECT COUNT(*) FROM TMP_SOURCES)
                        )
                    );
                END IF;

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id, CURRENT_TIMESTAMP(), :v_subject, s.SOURCE_ID, :v_group_name_scalar,
                    ''PHASE_1'', :v_attempt, NULL,
                    ''SOURCE_000_ENDDTS_VISIBLE'',
                    0,
                    OBJECT_CONSTRUCT(
                        ''last_done_end_dt'', IFF(:v_last_done_end_dt IS NULL, NULL, TO_CHAR(:v_last_done_end_dt,''YYYY-MM-DD'')),
                        ''min_end_dt_for_source'',
                           (SELECT TO_CHAR(MIN(DLVY_END_DT),''YYYY-MM-DD'')
                            FROM TMP_000_ENDDTS e
                            WHERE e.SOURCE_ID = s.SOURCE_ID
                              AND (:v_last_done_end_dt IS NULL OR e.DLVY_END_DT > :v_last_done_end_dt)
                           )
                    )
                FROM TMP_SOURCES s;

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                    ''PHASE_1'', :v_attempt, NULL,
                    IFF(:v_cycle_end_dt IS NULL, ''WAITING_FOR_NEXT_COMMON_ENDDT'', ''FOUND_NEXT_COMMON_ENDDT''),
                    0,
                    OBJECT_CONSTRUCT(
                        ''next_common_end_dt'', IFF(:v_cycle_end_dt IS NULL, NULL, TO_CHAR(:v_cycle_end_dt,''YYYY-MM-DD'')),
                        ''retries'', :v_000_retries,
                        ''delay_seconds'', :v_000_delay_seconds
                    );

                IF (v_cycle_end_dt IS NOT NULL) THEN
                    EXIT;
                END IF;

                IF (v_attempt < v_000_retries) THEN
                    EXECUTE IMMEDIATE ''CALL SYSTEM$WAIT('' || TO_VARCHAR(GREATEST(0, v_000_delay_seconds)) || '')'';
                END IF;
            END WHILE;

            IF (v_cycle_end_dt IS NULL) THEN
                v_step := ''PH1_NO_COMMON_ENDDT'';

                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_SOURCES_MISSING AS
                 SELECT s.SOURCE_ID
                 FROM TMP_SOURCES s
                 LEFT JOIN TMP_000_ENDDTS e
                   ON e.SOURCE_ID = s.SOURCE_ID
                  AND ('' || IFF(v_last_done_end_dt IS NULL, ''TRUE'', ''e.DLVY_END_DT > TO_DATE('''''' || TO_CHAR(v_last_done_end_dt,''YYYY-MM-DD'') || '''''')'') || '')
                 GROUP BY s.SOURCE_ID
                 HAVING COUNT(e.DLVY_END_DT) = 0'';

                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_SOURCES_PRESENT AS
                 SELECT DISTINCT e.SOURCE_ID
                 FROM TMP_000_ENDDTS e
                 WHERE '' || IFF(v_last_done_end_dt IS NULL, ''TRUE'', ''e.DLVY_END_DT > TO_DATE('''''' || TO_CHAR(v_last_done_end_dt,''YYYY-MM-DD'') || '''''')'') || '''';

                IF ( (SELECT COUNT(*) FROM TMP_SOURCES_PRESENT) > 0
                     AND (SELECT COUNT(*) FROM TMP_SOURCES_MISSING) > 0 ) THEN

                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT
                        :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                        ''PHASE_1'', :v_attempt, NULL, ''MISSING_FILES_FROM_SOURCE'', 0,
                        OBJECT_CONSTRUCT(
                            ''last_done_end_dt'', IFF(:v_last_done_end_dt IS NULL, NULL, TO_CHAR(:v_last_done_end_dt,''YYYY-MM-DD'')),
                            ''missing_sources'', (SELECT ARRAY_AGG(SOURCE_ID) FROM TMP_SOURCES_MISSING),
                            ''present_sources'', (SELECT ARRAY_AGG(SOURCE_ID) FROM TMP_SOURCES_PRESENT),
                            ''retries'', :v_000_retries,
                            ''delay_seconds'', :v_000_delay_seconds
                        );

                    RETURN OBJECT_CONSTRUCT(
                        ''status'', ''MISSING_000_FILES_FROM_SOURCE'',
                        ''message'', ''MISSING 000 FILES FROM SOURCE '' || (
                            SELECT LISTAGG(SOURCE_ID, '','') WITHIN GROUP (ORDER BY SOURCE_ID)
                            FROM TMP_SOURCES_MISSING
                        ),
                        ''subject'', v_subject,
                        ''pkg_group_name'', v_group_name_scalar,
                        ''missing_sources'', (SELECT ARRAY_AGG(SOURCE_ID) FROM TMP_SOURCES_MISSING),
                        ''run_id'', v_run_id,
                        ''failed_step'', v_step
                    );
                END IF;

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                    ''PHASE_1'', :v_attempt, NULL, ''NO_MORE_COMMON_ENDDT_TIMEOUT'', 0,
                    OBJECT_CONSTRUCT(
                        ''last_done_end_dt'', IFF(:v_last_done_end_dt IS NULL, NULL, TO_CHAR(:v_last_done_end_dt,''YYYY-MM-DD'')),
                        ''retries'', :v_000_retries,
                        ''delay_seconds'', :v_000_delay_seconds
                    );

                EXIT;
            END IF;

            ------------------------------------------------------------------
            -- PHASE 2: process ONLY this v_cycle_end_dt with proper retries
            ------------------------------------------------------------------
            v_step := ''PH2_INIT'';
            EXECUTE IMMEDIATE ''CREATE OR REPLACE TEMP TABLE TMP_END_DTS_TODO (DLVY_END_DT DATE)'';

            INSERT INTO TMP_END_DTS_TODO(DLVY_END_DT)
            SELECT :v_cycle_end_dt
            WHERE NOT EXISTS (SELECT 1 FROM TMP_END_DTS_DONE WHERE DLVY_END_DT = :v_cycle_end_dt);

            v_attempt := 0;
            v_block_reason_last := NULL;
            v_cycle_done := FALSE;

            WHILE (v_attempt < v_pkg_retries) DO
                v_attempt := v_attempt + 1;

                v_attempt_blocked := FALSE;

                IF (v_attempt > 1) THEN
                    v_step := ''PH2_REFRESH_EXPECTED_ALL'';
                    EXECUTE IMMEDIATE
                    ''CREATE OR REPLACE TEMP TABLE TMP_EXPECTED_ALL AS
                     WITH parse_00 AS (
                         SELECT
                             UPPER(TRIM(SUBSTR(TRIM(r.DATA), 3, 3)))  AS SOURCE_SYS,
                             UPPER(TRIM(SUBSTR(TRIM(r.DATA), 6, 4)))  AS SUBJECT_AREA,
                             TRIM(SUBSTR(TRIM(r.DATA), 10, 3))        AS VERSION,
                             TRIM(SUBSTR(TRIM(r.DATA), 13, 9))        AS PKG,
                             TRIM(SUBSTR(TRIM(r.DATA), 22, 3))        AS FILE_NUMBER,
                             TRIM(SUBSTR(TRIM(r.DATA), 25, 3))        AS X,
                             TO_DATE(SUBSTR(TRIM(r.DATA), 38, 10), ''''YYYY-MM-DD'''') AS FILE_TO_DT
                         FROM '' || v_raw_table || '' r
                         JOIN TMP_SOURCES s
                           ON UPPER(TRIM(SUBSTR(TRIM(r.DATA), 3, 3))) = s.SOURCE_ID
                         WHERE SUBSTR(r.DW_FILE_NM, 8, 3) = ''''000''''
                           AND SUBSTR(TRIM(r.DATA), 1, 2) = ''''10''''
                           AND UPPER(TRIM(SUBSTR(TRIM(r.DATA), 6, 4))) = '''''' || v_subject || ''''''
                           AND TRY_TO_DATE(SUBSTR(TRIM(r.DATA), 38, 10), ''''YYYY-MM-DD'''') IS NOT NULL
                           AND IFNULL(r.DW_READY_TO_LOAD_FL, FALSE) = FALSE
                     )
                     SELECT
                         (SOURCE_SYS || SUBJECT_AREA || FILE_NUMBER || ''''_'''' || X || ''''_'''' || VERSION || ''''_0000_'''' ||
                          PKG || ''''_'''' || TO_CHAR(FILE_TO_DT, ''''YYYYMMDD'''')) AS FILE_EXPECTED,
                         SUBJECT_AREA  AS DLVY_SUBJECT_AREA_NM,
                         SOURCE_SYS    AS DLVY_SOURCE_NM,
                         FILE_TO_DT    AS DLVY_END_DT
                     FROM parse_00'';

                    v_step := ''PH2_REFRESH_RECEIVED'';
                    EXECUTE IMMEDIATE
                    ''CREATE OR REPLACE TEMP TABLE TMP_RECEIVED AS
                     SELECT
                         SUBSTR(DW_FILE_NM, 1, 42) AS FILE_RECEIVED_SHORT,
                         MAX(IFF(SUBSTR(TRIM(DATA),1,2) = ''''99'''', ABS(TRY_TO_NUMBER(REGEXP_SUBSTR(TRIM(DATA), ''''[+-][0-9]+$''''))), NULL)) AS EXPECTED_ROWS,
                         SUM(IFF(SUBSTR(TRIM(DATA),1,2) IN (''''00'''',''''99''''), 0, 1)) AS ACTUAL_ROWS,
                         SUM(IFF(SUBSTR(TRIM(DATA),1,2) = ''''99'''', 1, 0)) AS FOOTER_ROWS,
                         SUM(IFF(SUBSTR(TRIM(DATA),1,2) = ''''00'''', 1, 0)) AS HEADER_ROWS
                     FROM '' || v_raw_table || ''
                     WHERE SUBSTR(DW_FILE_NM, 8, 3) <> ''''000''''
                     GROUP BY 1'';
                END IF;

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                    ''PHASE_2'', :v_attempt, :v_cycle_end_dt, ''PKG_CYCLE_START'', 0,
                    OBJECT_CONSTRUCT(''pkg_retries'', :v_pkg_retries, ''pkg_delay_seconds'', :v_pkg_delay_seconds);

                v_current_end_dt := v_cycle_end_dt;

                v_step := ''PH2_EXPECTED_ENDDT'';
                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_EXPECTED AS
                 SELECT *
                 FROM TMP_EXPECTED_ALL
                 WHERE DLVY_END_DT = TO_DATE('''''' || TO_CHAR(v_current_end_dt,''YYYY-MM-DD'') || '''''')'';

                v_step := ''PH2_FILE_STATUS_BASE'';
                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_FILE_STATUS_BASE AS
                 SELECT
                     e.FILE_EXPECTED,
                     r.FILE_RECEIVED_SHORT AS FILE_RECEIVED,
                     IFF(r.FILE_RECEIVED_SHORT IS NULL, ''''MISSING'''', ''''RECEIVED'''') AS FILE_STATUS,
                     e.DLVY_SUBJECT_AREA_NM,
                     e.DLVY_SOURCE_NM,
                     e.DLVY_END_DT,
                     r.EXPECTED_ROWS,
                     r.ACTUAL_ROWS,
                     r.FOOTER_ROWS,
                     r.HEADER_ROWS
                 FROM TMP_EXPECTED e
                 LEFT JOIN TMP_RECEIVED r
                   ON e.FILE_EXPECTED = r.FILE_RECEIVED_SHORT'';

                v_step := ''PH2_FILE_STATUS'';
                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_FILE_STATUS AS
                 SELECT
                     FILE_EXPECTED,
                     FILE_RECEIVED,
                     CASE
                       WHEN FILE_STATUS = ''''MISSING'''' THEN ''''MISSING''''
                       WHEN FOOTER_ROWS = 1
                            AND HEADER_ROWS = 1
                            AND EXPECTED_ROWS IS NOT NULL
                            AND EXPECTED_ROWS = ACTUAL_ROWS THEN ''''RECEIVED''''
                       WHEN FILE_RECEIVED IS NOT NULL
                            AND (FOOTER_ROWS IS NULL OR FOOTER_ROWS <> 1
                                 OR HEADER_ROWS IS NULL OR HEADER_ROWS <> 1
                                 OR EXPECTED_ROWS IS NULL
                                 OR EXPECTED_ROWS <> ACTUAL_ROWS) THEN ''''ROWCOUNT_FAIL''''
                       ELSE ''''ROWCOUNT_NOT_EVALUATED''''
                     END AS FILE_STATUS,
                     DLVY_SUBJECT_AREA_NM,
                     DLVY_SOURCE_NM,
                     DLVY_END_DT
                 FROM TMP_FILE_STATUS_BASE'';

                ------------------------------------------------------------------
                -- Per-source summary log
                ------------------------------------------------------------------
                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                  :v_run_id, CURRENT_TIMESTAMP(), :v_subject, DLVY_SOURCE_NM, :v_group_name_scalar,
                  ''PHASE_2'', :v_attempt, :v_current_end_dt,
                  ''ENDDT_SOURCE_SUMMARY'',
                  0,
                  OBJECT_CONSTRUCT(
                    ''expected_cnt'', COUNT(*),
                    ''received_physical_cnt'', SUM(IFF(FILE_RECEIVED IS NOT NULL, 1, 0)),
                    ''received_valid_cnt'', SUM(IFF(FILE_STATUS = ''RECEIVED'', 1, 0)),
                    ''missing_cnt'', SUM(IFF(FILE_STATUS = ''MISSING'', 1, 0)),
                    ''rowcount_fail_cnt'', SUM(IFF(FILE_STATUS = ''ROWCOUNT_FAIL'', 1, 0)),
                    ''rowcount_not_evaluated_cnt'', SUM(IFF(FILE_STATUS = ''ROWCOUNT_NOT_EVALUATED'', 1, 0))
                  )
                FROM TMP_FILE_STATUS
                GROUP BY DLVY_SOURCE_NM;

                v_step := ''PH2_END_STATUS'';
                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_END_STATUS AS
                 SELECT
                     '''''' || REPLACE(v_group_name_scalar,'''''''','''''''''''') || '''''' AS PKG_GROUP_NAME,
                     MIN(DLVY_SUBJECT_AREA_NM) AS DLVY_SUBJECT_AREA_NM,
                     NULL AS DLVY_SOURCE_NM,
                     DLVY_END_DT,
                     COUNT(*) AS expected_cnt,
                     SUM(IFF(FILE_RECEIVED IS NOT NULL, 1, 0)) AS received_physical_cnt,
                     SUM(IFF(FILE_STATUS = ''''RECEIVED'''', 1, 0)) AS received_valid_cnt,
                     SUM(IFF(FILE_STATUS = ''''MISSING'''', 1, 0))  AS missing_cnt,
                     SUM(IFF(FILE_STATUS = ''''ROWCOUNT_FAIL'''', 1, 0)) AS rowcount_fail_cnt,
                     SUM(IFF(FILE_STATUS = ''''ROWCOUNT_NOT_EVALUATED'''', 1, 0)) AS rowcount_not_evaluated_cnt,
                     IFF(
                         SUM(IFF(FILE_STATUS = ''''MISSING'''', 1, 0)) = 0
                         AND SUM(IFF(FILE_STATUS IN (''''ROWCOUNT_FAIL'''',''''ROWCOUNT_NOT_EVALUATED''''), 1, 0)) = 0
                         AND COUNT(*) > 0,
                         TRUE, FALSE
                     ) AS all_received
                 FROM TMP_FILE_STATUS
                 GROUP BY 1,4'';

                v_step := ''PH2_LOAD_METRICS'';
                SELECT
                    EXPECTED_CNT,
                    RECEIVED_PHYSICAL_CNT,
                    RECEIVED_VALID_CNT,
                    MISSING_CNT,
                    ROWCOUNT_FAIL_CNT,
                    ROWCOUNT_NOT_EVALUATED_CNT,
                    ALL_RECEIVED
                INTO
                    :v_expected_cnt,
                    :v_received_physical_cnt,
                    :v_received_valid_cnt,
                    :v_missing_cnt,
                    :v_rowcount_fail_cnt,
                    :v_rowcount_not_eval_cnt,
                    :v_all_received
                FROM TMP_END_STATUS;

                ------------------------------------------------------------------
                -- Merge status snapshot
                ------------------------------------------------------------------
                v_step := ''PH2_MERGE_STATUS'';
                EXECUTE IMMEDIATE
                ''MERGE INTO '' || v_status_table || '' t
                 USING (
                     SELECT
                         PKG_GROUP_NAME,
                         DLVY_SUBJECT_AREA_NM,
                         DLVY_SOURCE_NM,
                         DLVY_END_DT,
                         ALL_RECEIVED,
                         EXPECTED_CNT,
                         RECEIVED_VALID_CNT AS RECEIVED_CNT,
                         MISSING_CNT,
                         ROWCOUNT_FAIL_CNT,
                         CURRENT_TIMESTAMP() AS LAST_CHECKED_AT,
                         CURRENT_TIMESTAMP() AS UPDATED_AT
                     FROM TMP_END_STATUS
                 ) s
                 ON  t.PKG_GROUP_NAME       = s.PKG_GROUP_NAME
                 AND t.DLVY_SUBJECT_AREA_NM = s.DLVY_SUBJECT_AREA_NM
                 AND t.DLVY_END_DT          = s.DLVY_END_DT
                 WHEN MATCHED THEN UPDATE SET
                     DLVY_SOURCE_NM     = s.DLVY_SOURCE_NM,
                     ALL_RECEIVED       = s.ALL_RECEIVED,
                     EXPECTED_CNT       = s.EXPECTED_CNT,
                     RECEIVED_CNT       = s.RECEIVED_CNT,
                     MISSING_CNT        = s.MISSING_CNT,
                     ROWCOUNT_FAIL_CNT  = s.ROWCOUNT_FAIL_CNT,
                     LAST_CHECKED_AT    = s.LAST_CHECKED_AT,
                     UPDATED_AT         = s.UPDATED_AT
                 WHEN NOT MATCHED THEN INSERT (
                     PKG_GROUP_NAME, DLVY_SUBJECT_AREA_NM, DLVY_SOURCE_NM, DLVY_END_DT,
                     ALL_RECEIVED, EXPECTED_CNT, RECEIVED_CNT, MISSING_CNT, ROWCOUNT_FAIL_CNT,
                     LAST_CHECKED_AT, UPDATED_AT
                 ) VALUES (
                     s.PKG_GROUP_NAME, s.DLVY_SUBJECT_AREA_NM, s.DLVY_SOURCE_NM, s.DLVY_END_DT,
                     s.ALL_RECEIVED, s.EXPECTED_CNT, s.RECEIVED_CNT, s.MISSING_CNT, s.ROWCOUNT_FAIL_CNT,
                     s.LAST_CHECKED_AT, s.UPDATED_AT
                 )'';

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                    ''PHASE_2'', :v_attempt, :v_current_end_dt,
                    IFF(:v_all_received, ''ENDDT_COMPLETE'', ''ENDDT_BLOCKED''),
                    0,
                    OBJECT_CONSTRUCT(
                      ''expected_cnt'', :v_expected_cnt,
                      ''received_physical_cnt'', :v_received_physical_cnt,
                      ''received_valid_cnt'', :v_received_valid_cnt,
                      ''missing_cnt'', :v_missing_cnt,
                      ''rowcount_fail_cnt'', :v_rowcount_fail_cnt,
                      ''rowcount_not_evaluated_cnt'', :v_rowcount_not_eval_cnt
                    );

                ------------------------------------------------------------------
                -- If blocked: log reason + failing files, SKIP UPDATE
                ------------------------------------------------------------------
                IF (NOT v_all_received) THEN
                    v_attempt_blocked := TRUE;

                    v_block_reason := (
                      SELECT CASE
                               WHEN SUM(IFF(FILE_STATUS = ''MISSING'', 1, 0)) > 0 THEN ''MISSING''
                               WHEN SUM(IFF(FILE_STATUS = ''ROWCOUNT_FAIL'', 1, 0)) > 0 THEN ''ROWCOUNT_FAIL''
                               WHEN SUM(IFF(FILE_STATUS = ''ROWCOUNT_NOT_EVALUATED'', 1, 0)) > 0 THEN ''ROWCOUNT_NOT_EVALUATED''
                               ELSE ''UNKNOWN''
                             END
                      FROM TMP_FILE_STATUS
                    );

                    v_block_reason_last := v_block_reason;

                    v_block_reason_json := (
                      SELECT OBJECT_CONSTRUCT(
                        ''block_reason'', :v_block_reason,
                        ''failing_files'',
                          ARRAY_AGG(
                            OBJECT_CONSTRUCT(
                              ''source_id'', DLVY_SOURCE_NM,
                              ''file_expected'', FILE_EXPECTED,
                              ''file_status'', FILE_STATUS
                            )
                          )
                      )
                      FROM TMP_FILE_STATUS
                      WHERE FILE_STATUS <> ''RECEIVED''
                    );

                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT
                      :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                      ''PHASE_2'', :v_attempt, :v_current_end_dt,
                      ''ENDDT_BLOCK_REASON'',
                      0,
                      :v_block_reason_json;

                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT
                        :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                        ''PHASE_2'', :v_attempt, :v_current_end_dt,
                        ''SKIP_UPDATE_BLOCKED'',
                        0,
                        OBJECT_CONSTRUCT(
                            ''block_reason_last'', NVL(:v_block_reason_last,''UNKNOWN''),
                            ''expected_cnt'', :v_expected_cnt,
                            ''received_valid_cnt'', :v_received_valid_cnt,
                            ''missing_cnt'', :v_missing_cnt,
                            ''rowcount_fail_cnt'', :v_rowcount_fail_cnt,
                            ''rowcount_not_evaluated_cnt'', :v_rowcount_not_eval_cnt
                        );

                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT
                      :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                      ''PHASE_2'', :v_attempt, :v_current_end_dt,
                      ''CYCLE_COMPLETE'',
                      0,
                      OBJECT_CONSTRUCT(
                        ''cycle_end_dt'', TO_CHAR(:v_current_end_dt,''YYYY-MM-DD''),
                        ''attempt_blocked'', TRUE,
                        ''block_reason_last'', NVL(:v_block_reason_last,''UNKNOWN''),
                        ''attempt_no'', :v_attempt,
                        ''attempts_total'', :v_pkg_retries
                      );

                    IF (v_attempt < v_pkg_retries) THEN
                        INSERT INTO IDENTIFIER(:v_log_table)
                        SELECT
                          :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                          ''PHASE_2'', :v_attempt, :v_current_end_dt,
                          ''WAIT_BEFORE_RETRY'',
                          0,
                          OBJECT_CONSTRUCT(''delay_seconds'', :v_pkg_delay_seconds, ''next_attempt'', :v_attempt + 1);

                        EXECUTE IMMEDIATE ''CALL SYSTEM$WAIT('' || TO_VARCHAR(GREATEST(0, v_pkg_delay_seconds)) || '')'';
                    END IF;

                    CONTINUE;
                END IF;

                ------------------------------------------------------------------
                -- PH2_UPDATE_READY (only when all_received)
                ------------------------------------------------------------------
                v_step := ''PH2_UPDATE_READY'';

                EXECUTE IMMEDIATE
                ''UPDATE '' || v_raw_table || '' r
                 SET r.DW_READY_TO_LOAD_FL = TRUE
                 FROM TMP_FILE_STATUS f
                 WHERE SUBSTR(r.DW_FILE_NM, 1, 42) = f.FILE_EXPECTED
                   AND f.FILE_STATUS = ''''RECEIVED''''
                   AND IFNULL(r.DW_READY_TO_LOAD_FL, FALSE) = FALSE'';

                v_update_qid := LAST_QUERY_ID();

                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_UPDATE_RESULT AS
                 SELECT * FROM TABLE(RESULT_SCAN('''''' || v_update_qid || ''''''))'';

                v_rows_updated := (
                  SELECT NVL(TRY_TO_NUMBER($1::STRING), 0)
                  FROM TMP_UPDATE_RESULT
                  LIMIT 1
                );

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                    ''PHASE_2'', :v_attempt, :v_current_end_dt,
                    ''RAW_READY_FLAG_UPDATED'',
                    :v_rows_updated,
                    OBJECT_CONSTRUCT(''raw_table'', :v_raw_table, ''update_qid'', :v_update_qid);

                v_step := ''PH2_FLAG_000_FILE'';
                EXECUTE IMMEDIATE
                ''UPDATE '' || v_raw_table || ''
                 SET DW_READY_TO_LOAD_FL = TRUE
                 WHERE SUBSTR(DW_FILE_NM, 8, 3) = ''''000''''
                   AND IFNULL(DW_READY_TO_LOAD_FL, FALSE) = FALSE
                   AND DW_FILE_NM IN (
                       SELECT DISTINCT DW_FILE_NM
                       FROM '' || v_raw_table || ''
                       WHERE SUBSTR(DW_FILE_NM, 8, 3) = ''''000''''
                         AND SUBSTR(TRIM(DATA), 1, 2) = ''''10''''
                         AND TO_DATE(SUBSTR(TRIM(DATA), 38, 10), ''''YYYY-MM-DD'''') = TO_DATE('''''' || TO_CHAR(v_current_end_dt,''YYYY-MM-DD'') || '''''')
                         AND UPPER(TRIM(SUBSTR(TRIM(DATA), 6, 4))) = '''''' || v_subject || ''''''
                   )'';

                v_step := ''PH2_MARK_DONE'';
                INSERT INTO TMP_END_DTS_DONE (DLVY_END_DT)
                SELECT :v_current_end_dt
                WHERE NOT EXISTS (SELECT 1 FROM TMP_END_DTS_DONE WHERE DLVY_END_DT = :v_current_end_dt);

                DELETE FROM TMP_END_DTS_TODO
                WHERE DLVY_END_DT = :v_current_end_dt;

                v_last_done_end_dt := v_current_end_dt;
                v_cycle_done := TRUE;

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                  :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                  ''PHASE_2'', :v_attempt, :v_current_end_dt,
                  ''CYCLE_COMPLETE'',
                  :v_rows_updated,
                  OBJECT_CONSTRUCT(
                    ''cycle_end_dt'', TO_CHAR(:v_current_end_dt,''YYYY-MM-DD''),
                    ''attempt_blocked'', FALSE,
                    ''rows_updated'', :v_rows_updated
                  );

                EXIT;
            END WHILE;

            ------------------------------------------------------------------
            -- After PH2 attempts: if still not done => return blocked/timeout
            ------------------------------------------------------------------
            IF (v_cycle_done = FALSE) THEN
                v_step := ''PH2_BLOCKED_ENDDT'';
                v_blocked_end_dt := v_cycle_end_dt;
                v_block_reason := NVL(v_block_reason_last, ''UNKNOWN'');

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                    ''PHASE_2'', :v_attempt, :v_blocked_end_dt,
                    IFF(:v_block_reason IN (''ROWCOUNT_FAIL'',''ROWCOUNT_NOT_EVALUATED'',''MISSING''), ''BLOCKED_BY_VALIDATION'', ''WAITING_FOR_PACKAGES''),
                    0,
                    OBJECT_CONSTRUCT(
                        ''blocked_dlvy_end_dt'', TO_CHAR(:v_blocked_end_dt,''YYYY-MM-DD''),
                        ''block_reason'', :v_block_reason,
                        ''attempts_total'', :v_pkg_retries
                    );

                RETURN OBJECT_CONSTRUCT(
                    ''status'', IFF(v_block_reason IN (''ROWCOUNT_FAIL'',''ROWCOUNT_NOT_EVALUATED'',''MISSING''), ''BLOCKED_BY_VALIDATION'', ''WAITING_FOR_PACKAGES''),
                    ''subject'', v_subject,
                    ''pkg_group_name'', v_group_name_scalar,
                    ''blocked_dlvy_end_dt'', TO_CHAR(v_blocked_end_dt, ''YYYY-MM-DD''),
                    ''block_reason'', v_block_reason,
                    ''run_id'', v_run_id,
                    ''failed_step'', v_step
                );
            END IF;

        END WHILE;

        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT
            :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
            ''GROUP'', NULL, NULL, ''GROUP_COMPLETE'', 0,
            OBJECT_CONSTRUCT(''group_index'', :v_group_idx);

        v_group_idx := v_group_idx + 1;
    END WHILE;

    INSERT INTO IDENTIFIER(:v_log_table)
    SELECT
        :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, NULL,
        ''DONE'', NULL, NULL, ''SUCCESS_ALL_GROUPS'', 0,
        OBJECT_CONSTRUCT(''groups_total'', :v_groups_cnt);

    RETURN OBJECT_CONSTRUCT(''status'',''SUCCESS_ALL_GROUPS'',''subject'',v_subject,''run_id'',v_run_id);

EXCEPTION
    WHEN OTHER THEN
        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT
            :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
            ''ERROR'', NULL, NULL, ''EXCEPTION'', 0,
            OBJECT_CONSTRUCT(''failed_step'', :v_step);

        RETURN OBJECT_CONSTRUCT(
            ''status'',''ERROR'',
            ''subject'', v_subject,
            ''run_id'', v_run_id,
            ''failed_step'', v_step,
            ''error_message'', ''EXCEPTION_THROWN (see query history for details)''
        );
END;
';
