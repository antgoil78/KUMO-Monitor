-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.X_SP_CHECK_READY_TO_LOAD(VARCHAR, VARCHAR, NUMBER, NUMBER, BOOLEAN)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.X_SP_CHECK_READY_TO_LOAD("DLVY_SUBJECT_AREA_NM" VARCHAR, "PKG_GROUP_NAME" VARCHAR DEFAULT null, "RETRIES" NUMBER(38,0) DEFAULT 1, "DELAY_BETWEEN_RETRIES_SEC" NUMBER(38,0) DEFAULT 0, "REQUIRE_COMMON_ENDDTS" BOOLEAN DEFAULT TRUE)
RETURNS VARIANT
LANGUAGE SQL
COMMENT='user-defined procedure'
EXECUTE AS OWNER
AS '
DECLARE
    v_subject            STRING := UPPER(:DLVY_SUBJECT_AREA_NM);
    v_raw_table          STRING := ''KUMO_TST.RAW_LIM.RAW_LIM_'' || UPPER(:DLVY_SUBJECT_AREA_NM);
    v_results_table      STRING := ''KUMO_TST.RAW_LIM.RAW_LOAD_RESULTS'';
    v_meta_table         STRING := ''KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE'';
    v_status_table       STRING := ''KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_'' || UPPER(:DLVY_SUBJECT_AREA_NM) || ''_PKG_STATUS'';
    v_log_table          STRING := ''KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_LOG'';
    v_run_id             STRING := UUID_STRING();

    v_groups_cnt         NUMBER := 0;
    v_group_idx          NUMBER := 1;
    v_group_name_scalar  STRING;
    v_pkg_retries        NUMBER;
    v_pkg_delay_seconds  NUMBER;

    v_current_end_dt     DATE;
    v_attempt            NUMBER;
    v_cycle_done         BOOLEAN;
    v_attempt_blocked    BOOLEAN;

    v_expected_cnt       NUMBER;
    v_received_cnt       NUMBER;
    v_missing_cnt        NUMBER;
    v_rowcount_fail_cnt  NUMBER;
    v_rowcount_ne_cnt    NUMBER;
    v_all_received       BOOLEAN;
    v_rows_updated       NUMBER;

    v_block_reason       STRING;
    v_block_reason_last  STRING;
    v_block_reason_json  VARIANT;
    v_step               STRING := ''INIT'';

    v_total_enddts_done  NUMBER := 0;

    ENDDT_BLOCK_REASON EXCEPTION (-20001, ''ENDDT_BLOCK_REASON: All retries exhausted, no end dates were successfully processed.'');
BEGIN
    v_step := ''CREATE_TABLES'';

    EXECUTE IMMEDIATE
    ''CREATE TABLE IF NOT EXISTS '' || :v_meta_table || '' (
        PKG_GROUP_NAME  STRING,
        SUBJECT_AREA    STRING,
        SOURCE_ID       STRING,
        PKG_ID          STRING DEFAULT ''''001'''',
        ACTIVE_FL       BOOLEAN,
        UPDATED_AT      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
     )'';

    EXECUTE IMMEDIATE
    ''CREATE TABLE IF NOT EXISTS '' || :v_log_table || '' (
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

    EXECUTE IMMEDIATE
    ''CREATE TABLE IF NOT EXISTS '' || :v_status_table || '' (
        PKG_GROUP_NAME        STRING,
        DLVY_SUBJECT_AREA_NM  STRING,
        DLVY_SOURCE_NM        STRING,
        DLVY_END_DT           DATE,
        ALL_RECEIVED          BOOLEAN,
        EXPECTED_CNT          NUMBER,
        RECEIVED_CNT          NUMBER,
        MISSING_CNT           NUMBER,
        ROWCOUNT_FAIL_CNT     NUMBER,
        LAST_CHECKED_AT       TIMESTAMP_NTZ,
        UPDATED_AT            TIMESTAMP_NTZ
     )'';

    INSERT INTO IDENTIFIER(:v_log_table)
    SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :PKG_GROUP_NAME,
           ''INIT'', NULL, NULL, ''START'', 0,
           OBJECT_CONSTRUCT(''raw_table'', :v_raw_table, ''retries'', :RETRIES, ''delay_sec'', :DELAY_BETWEEN_RETRIES_SEC);

    v_step := ''BUILD_EXPECTED'';
    EXECUTE IMMEDIATE
    ''CREATE OR REPLACE TEMP TABLE TMP_EXPECTED AS
     SELECT
         UPPER(TRIM(SUBSTR(TRIM(DATA), 3, 3)))  AS SOURCE_ID,
         UPPER(TRIM(SUBSTR(TRIM(DATA), 6, 4)))  AS SUBJECT_AREA,
         TRIM(SUBSTR(TRIM(DATA), 10, 3))         AS VERSION,
         TRIM(SUBSTR(TRIM(DATA), 13, 9))         AS PKG,
         TRIM(SUBSTR(TRIM(DATA), 22, 3))         AS FILE_NUMBER,
         TRIM(SUBSTR(TRIM(DATA), 25, 3))         AS FILE_VER,
         COALESCE(TRY_TO_DATE(SUBSTR(TRIM(DATA), 38, 10), ''''YYYY-MM-DD''''), TRY_TO_DATE(SUBSTR(TRIM(DATA), 38, 8), ''''YYYYMMDD'''')) AS DLVY_END_DT,
         (UPPER(TRIM(SUBSTR(TRIM(DATA), 3, 3))) ||
          UPPER(TRIM(SUBSTR(TRIM(DATA), 6, 4))) ||
          TRIM(SUBSTR(TRIM(DATA), 22, 3)) || ''''_'''' ||
          TRIM(SUBSTR(TRIM(DATA), 25, 3)) || ''''_'''' ||
          TRIM(SUBSTR(TRIM(DATA), 10, 3)) || ''''_0000_'''' ||
          TRIM(SUBSTR(TRIM(DATA), 13, 9)) || ''''_'''' ||
          TO_CHAR(COALESCE(TRY_TO_DATE(SUBSTR(TRIM(DATA), 38, 10), ''''YYYY-MM-DD''''), TRY_TO_DATE(SUBSTR(TRIM(DATA), 38, 8), ''''YYYYMMDD'''')), ''''YYYYMMDD'''')
         ) AS FILE_EXPECTED
     FROM '' || :v_raw_table || ''
     WHERE SUBSTR(DW_FILE_NM, 8, 3) = ''''000''''
       AND SUBSTR(TRIM(DATA), 1, 2) = ''''10''''
       AND UPPER(TRIM(SUBSTR(TRIM(DATA), 6, 4))) = '''''' || :v_subject || '''''''';

    v_step := ''BUILD_RECEIVED'';
    EXECUTE IMMEDIATE
    ''CREATE OR REPLACE TEMP TABLE TMP_RECEIVED AS
     SELECT
         file_nm,
         SUBSTR(file_nm, 1, 42)   AS FILE_SHORT,
         dlvy_source_id            AS SOURCE_ID,
         dlvy_subject_area_id      AS SUBJECT_AREA,
         dlvy_pkg_id               AS PKG_ID,
         dlvy_end_date             AS DLVY_END_DT,
         footer_expected_rows,
         actual_data_rows,
         rowcount_match
     FROM '' || :v_results_table || ''
     WHERE dlvy_subject_area_id = '''''' || :v_subject || ''''''
       AND rowcount_match <> ''''CONTROL_FILE'''''';

    v_step := ''BUILD_GROUPS'';
    EXECUTE IMMEDIATE
    ''CREATE OR REPLACE TEMP TABLE TMP_GROUPS AS
     SELECT
         PKG_GROUP_NAME::STRING AS PKG_GROUP_NAME,
         ROW_NUMBER() OVER (ORDER BY PKG_GROUP_NAME) AS RN
     FROM (
         SELECT DISTINCT PKG_GROUP_NAME
         FROM '' || :v_meta_table || ''
         WHERE SUBJECT_AREA = '''''' || :v_subject || ''''''
           AND ACTIVE_FL = TRUE
           '' || IFF(:PKG_GROUP_NAME IS NOT NULL, ''AND PKG_GROUP_NAME = '''''' || :PKG_GROUP_NAME || '''''''', '''') || ''
     )'';

    SELECT COUNT(*) INTO :v_groups_cnt FROM TMP_GROUPS;

    IF (v_groups_cnt = 0) THEN
        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, NULL,
               ''INIT'', NULL, NULL, ''NO_ACTIVE_GROUPS'', 0,
               OBJECT_CONSTRUCT(''meta_table'', :v_meta_table);
        RETURN OBJECT_CONSTRUCT(''status'',''NO_ACTIVE_GROUPS'',''subject'',v_subject,''run_id'',v_run_id);
    END IF;

    CREATE OR REPLACE TEMP TABLE TMP_END_DTS_DONE  (DLVY_END_DT DATE);
    CREATE OR REPLACE TEMP TABLE TMP_END_DTS_TODO   (DLVY_END_DT DATE);

    WHILE (v_group_idx <= v_groups_cnt) DO

        SELECT PKG_GROUP_NAME INTO :v_group_name_scalar
        FROM TMP_GROUPS WHERE RN = :v_group_idx;

        v_pkg_retries       := :RETRIES;
        v_pkg_delay_seconds := :DELAY_BETWEEN_RETRIES_SEC;

        v_step := ''PH1_COMMON_ENDDTS'';
        EXECUTE IMMEDIATE
        ''CREATE OR REPLACE TEMP TABLE TMP_SOURCE_DRIVER AS
         SELECT SOURCE_ID, PKG_ID
         FROM '' || :v_meta_table || ''
         WHERE SUBJECT_AREA = '''''' || :v_subject || ''''''
           AND ACTIVE_FL = TRUE
           AND PKG_GROUP_NAME = '''''' || :v_group_name_scalar || '''''''';

        IF (:REQUIRE_COMMON_ENDDTS) THEN
            CREATE OR REPLACE TEMP TABLE TMP_END_DTS_TODO AS
            SELECT e.DLVY_END_DT
            FROM TMP_EXPECTED e
            INNER JOIN TMP_SOURCE_DRIVER d
                ON e.SOURCE_ID = d.SOURCE_ID AND e.VERSION = d.PKG_ID
            WHERE e.DLVY_END_DT NOT IN (SELECT DLVY_END_DT FROM TMP_END_DTS_DONE)
              AND e.DLVY_END_DT NOT IN (SELECT DLVY_END_DT FROM IDENTIFIER(:v_status_table) WHERE PKG_GROUP_NAME = :v_group_name_scalar AND ALL_RECEIVED = TRUE)
            GROUP BY e.DLVY_END_DT
            HAVING COUNT(DISTINCT e.SOURCE_ID || ''|'' || e.VERSION)
                 = (SELECT COUNT(*) FROM TMP_SOURCE_DRIVER)
            ORDER BY e.DLVY_END_DT;
        ELSE
            CREATE OR REPLACE TEMP TABLE TMP_END_DTS_TODO AS
            SELECT DISTINCT e.DLVY_END_DT
            FROM TMP_EXPECTED e
            INNER JOIN TMP_SOURCE_DRIVER d
                ON e.SOURCE_ID = d.SOURCE_ID AND e.VERSION = d.PKG_ID
            WHERE e.DLVY_END_DT NOT IN (SELECT DLVY_END_DT FROM TMP_END_DTS_DONE)
              AND e.DLVY_END_DT NOT IN (SELECT DLVY_END_DT FROM IDENTIFIER(:v_status_table) WHERE PKG_GROUP_NAME = :v_group_name_scalar AND ALL_RECEIVED = TRUE)
            ORDER BY e.DLVY_END_DT;
        END IF;

        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
               ''PHASE_1'', NULL, NULL, ''COMMON_ENDDTS_FOUND'', 0,
               OBJECT_CONSTRUCT(
                   ''end_dates'', (SELECT ARRAY_AGG(DLVY_END_DT) FROM TMP_END_DTS_TODO),
                   ''sources'', (SELECT ARRAY_AGG(DISTINCT SOURCE_ID) FROM TMP_SOURCE_DRIVER),
                   ''source_pkg_combos'', (SELECT ARRAY_AGG(SOURCE_ID || '':'' || PKG_ID) FROM TMP_SOURCE_DRIVER),
                   ''source_pkg_count'', (SELECT COUNT(*) FROM TMP_SOURCE_DRIVER)
               );

        WHILE (EXISTS (SELECT 1 FROM TMP_END_DTS_TODO)) DO

            SELECT MIN(DLVY_END_DT) INTO :v_current_end_dt FROM TMP_END_DTS_TODO;

            v_cycle_done := FALSE;
            v_attempt := 1;
            v_block_reason_last := NULL;

            WHILE (v_attempt <= v_pkg_retries AND NOT v_cycle_done) DO

                v_step := ''PH2_FILE_STATUS'';
                CREATE OR REPLACE TEMP TABLE TMP_FILE_STATUS AS
                SELECT
                    e.SOURCE_ID         AS DLVY_SOURCE_NM,
                    e.VERSION           AS PKG_ID,
                    e.FILE_EXPECTED,
                    e.DLVY_END_DT,
                    :v_group_name_scalar AS PKG_GROUP_NAME,
                    r.FILE_SHORT        AS FILE_RECEIVED,
                    CASE
                        WHEN r.FILE_SHORT IS NULL                   THEN ''MISSING''
                        WHEN r.ROWCOUNT_MATCH = ''MATCH''             THEN ''RECEIVED''
                        WHEN r.ROWCOUNT_MATCH = ''MISMATCH''          THEN ''ROWCOUNT_FAIL''
                        WHEN r.ROWCOUNT_MATCH IN (''NO_FOOTER'',''CONTROL_FILE'')
                                                                    THEN ''ROWCOUNT_NOT_EVALUATED''
                        WHEN r.ROWCOUNT_MATCH IS NULL               THEN ''ROWCOUNT_NOT_EVALUATED''
                        ELSE ''RECEIVED''
                    END AS FILE_STATUS,
                    r.FOOTER_EXPECTED_ROWS,
                    r.ACTUAL_DATA_ROWS
                FROM TMP_EXPECTED e
                INNER JOIN TMP_SOURCE_DRIVER d
                    ON e.SOURCE_ID = d.SOURCE_ID AND e.VERSION = d.PKG_ID
                LEFT JOIN TMP_RECEIVED r
                    ON e.FILE_EXPECTED = r.FILE_SHORT
                WHERE e.DLVY_END_DT = :v_current_end_dt;

                CREATE OR REPLACE TEMP TABLE TMP_END_STATUS AS
                SELECT
                    PKG_GROUP_NAME,
                    :v_subject           AS DLVY_SUBJECT_AREA_NM,
                    LISTAGG(DISTINCT DLVY_SOURCE_NM, '','') AS DLVY_SOURCE_NM,
                    DLVY_END_DT,
                    COUNT(*)                                              AS EXPECTED_CNT,
                    SUM(IFF(FILE_RECEIVED IS NOT NULL, 1, 0))             AS RECEIVED_PHYSICAL_CNT,
                    SUM(IFF(FILE_STATUS = ''RECEIVED'', 1, 0))              AS RECEIVED_VALID_CNT,
                    SUM(IFF(FILE_STATUS = ''MISSING'', 1, 0))               AS MISSING_CNT,
                    SUM(IFF(FILE_STATUS = ''ROWCOUNT_FAIL'', 1, 0))         AS ROWCOUNT_FAIL_CNT,
                    SUM(IFF(FILE_STATUS = ''ROWCOUNT_NOT_EVALUATED'', 1, 0)) AS ROWCOUNT_NE_CNT,
                    IFF(
                        SUM(IFF(FILE_STATUS = ''MISSING'', 1, 0)) = 0
                        AND SUM(IFF(FILE_STATUS IN (''ROWCOUNT_FAIL'',''ROWCOUNT_NOT_EVALUATED''), 1, 0)) = 0
                        AND COUNT(*) > 0,
                        TRUE, FALSE
                    ) AS ALL_RECEIVED
                FROM TMP_FILE_STATUS
                GROUP BY PKG_GROUP_NAME, DLVY_END_DT;

                SELECT EXPECTED_CNT, RECEIVED_VALID_CNT, MISSING_CNT,
                       ROWCOUNT_FAIL_CNT, ROWCOUNT_NE_CNT, ALL_RECEIVED
                INTO :v_expected_cnt, :v_received_cnt, :v_missing_cnt,
                     :v_rowcount_fail_cnt, :v_rowcount_ne_cnt, :v_all_received
                FROM TMP_END_STATUS;

                v_step := ''PH2_MERGE_STATUS'';
                EXECUTE IMMEDIATE
                ''MERGE INTO '' || :v_status_table || '' t
                 USING (
                     SELECT PKG_GROUP_NAME, DLVY_SUBJECT_AREA_NM, DLVY_SOURCE_NM, DLVY_END_DT,
                            ALL_RECEIVED, EXPECTED_CNT, RECEIVED_VALID_CNT AS RECEIVED_CNT,
                            MISSING_CNT, ROWCOUNT_FAIL_CNT,
                            CURRENT_TIMESTAMP() AS LAST_CHECKED_AT,
                            CURRENT_TIMESTAMP() AS UPDATED_AT
                     FROM TMP_END_STATUS
                 ) s
                 ON  t.PKG_GROUP_NAME       = s.PKG_GROUP_NAME
                 AND t.DLVY_SUBJECT_AREA_NM = s.DLVY_SUBJECT_AREA_NM
                 AND t.DLVY_END_DT          = s.DLVY_END_DT
                 WHEN MATCHED THEN UPDATE SET
                     DLVY_SOURCE_NM    = s.DLVY_SOURCE_NM,
                     ALL_RECEIVED      = s.ALL_RECEIVED,
                     EXPECTED_CNT      = s.EXPECTED_CNT,
                     RECEIVED_CNT      = s.RECEIVED_CNT,
                     MISSING_CNT       = s.MISSING_CNT,
                     ROWCOUNT_FAIL_CNT = s.ROWCOUNT_FAIL_CNT,
                     LAST_CHECKED_AT   = s.LAST_CHECKED_AT,
                     UPDATED_AT        = s.UPDATED_AT
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
                SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject,
                       (SELECT LISTAGG(DISTINCT DLVY_SOURCE_NM, '','') FROM TMP_FILE_STATUS),
                       :v_group_name_scalar,
                       ''PHASE_2'', :v_attempt, :v_current_end_dt,
                       IFF(:v_all_received, ''ENDDT_COMPLETE'', ''ENDDT_BLOCKED''), 0,
                       OBJECT_CONSTRUCT(
                           ''expected_cnt'', :v_expected_cnt,
                           ''received_cnt'', :v_received_cnt,
                           ''missing_cnt'', :v_missing_cnt,
                           ''rowcount_fail_cnt'', :v_rowcount_fail_cnt,
                           ''rowcount_not_evaluated_cnt'', :v_rowcount_ne_cnt
                       );

                IF (NOT v_all_received) THEN
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
                            ''failing_files'', ARRAY_AGG(
                                OBJECT_CONSTRUCT(
                                    ''source_id'', DLVY_SOURCE_NM,
                                    ''pkg_id'', PKG_ID,
                                    ''file_expected'', FILE_EXPECTED,
                                    ''file_status'', FILE_STATUS,
                                    ''footer_expected_rows'', FOOTER_EXPECTED_ROWS,
                                    ''actual_data_rows'', ACTUAL_DATA_ROWS
                                )
                            )
                        )
                        FROM TMP_FILE_STATUS
                        WHERE FILE_STATUS <> ''RECEIVED''
                    );

                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject,
                           (SELECT LISTAGG(DISTINCT DLVY_SOURCE_NM, '','') FROM TMP_FILE_STATUS WHERE FILE_STATUS <> ''RECEIVED''),
                           :v_group_name_scalar,
                           ''PHASE_2'', :v_attempt, :v_current_end_dt,
                           ''ENDDT_BLOCK_REASON'', 0, :v_block_reason_json;

                    IF (v_attempt < v_pkg_retries) THEN
                        INSERT INTO IDENTIFIER(:v_log_table)
                        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                               ''PHASE_2'', :v_attempt, :v_current_end_dt,
                               ''WAIT_BEFORE_RETRY'', 0,
                               OBJECT_CONSTRUCT(''delay_seconds'', :v_pkg_delay_seconds, ''next_attempt'', :v_attempt + 1);

                        EXECUTE IMMEDIATE ''CALL SYSTEM$WAIT('' || TO_VARCHAR(GREATEST(0, v_pkg_delay_seconds)) || '')'';
                    END IF;

                    v_attempt := v_attempt + 1;
                    CONTINUE;
                END IF;

                v_step := ''PH2_UPDATE_READY'';
                EXECUTE IMMEDIATE
                ''UPDATE '' || :v_raw_table || '' r
                 SET r.DW_READY_TO_LOAD_FL = TRUE
                 FROM TMP_FILE_STATUS f
                 WHERE SUBSTR(r.DW_FILE_NM, 1, 42) = f.FILE_EXPECTED
                   AND f.FILE_STATUS = ''''RECEIVED''''
                   AND IFNULL(r.DW_READY_TO_LOAD_FL, FALSE) = FALSE'';

                LET v_update_qid VARCHAR := LAST_QUERY_ID();

                EXECUTE IMMEDIATE
                ''CREATE OR REPLACE TEMP TABLE TMP_UPDATE_RESULT AS
                 SELECT * FROM TABLE(RESULT_SCAN('''''' || :v_update_qid || ''''''))'';

                v_rows_updated := (
                    SELECT NVL(TRY_TO_NUMBER($1::STRING), 0)
                    FROM TMP_UPDATE_RESULT LIMIT 1
                );

                v_step := ''PH2_FLAG_000'';
                EXECUTE IMMEDIATE
                ''UPDATE '' || :v_raw_table || ''
                 SET DW_READY_TO_LOAD_FL = TRUE
                 WHERE SUBSTR(DW_FILE_NM, 8, 3) = ''''000''''
                   AND IFNULL(DW_READY_TO_LOAD_FL, FALSE) = FALSE
                   AND DW_FILE_NM IN (
                       SELECT DISTINCT DW_FILE_NM
                       FROM '' || :v_raw_table || ''
                       WHERE SUBSTR(DW_FILE_NM, 8, 3) = ''''000''''
                         AND SUBSTR(TRIM(DATA), 1, 2) = ''''10''''
                         AND COALESCE(TRY_TO_DATE(SUBSTR(TRIM(DATA), 38, 10), ''''YYYY-MM-DD''''), TRY_TO_DATE(SUBSTR(TRIM(DATA), 38, 8), ''''YYYYMMDD'''')) = TO_DATE('''''' || TO_CHAR(v_current_end_dt, ''YYYY-MM-DD'') || '''''')
                         AND UPPER(TRIM(SUBSTR(TRIM(DATA), 6, 4))) = '''''' || :v_subject || ''''''
                   )'';

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject,
                       (SELECT LISTAGG(DISTINCT DLVY_SOURCE_NM, '','') FROM TMP_FILE_STATUS),
                       :v_group_name_scalar,
                       ''PHASE_2'', :v_attempt, :v_current_end_dt,
                       ''RAW_READY_FLAG_UPDATED'', :v_rows_updated,
                       OBJECT_CONSTRUCT(''raw_table'', :v_raw_table);

                INSERT INTO TMP_END_DTS_DONE (DLVY_END_DT)
                SELECT :v_current_end_dt
                WHERE NOT EXISTS (SELECT 1 FROM TMP_END_DTS_DONE WHERE DLVY_END_DT = :v_current_end_dt);

                DELETE FROM TMP_END_DTS_TODO WHERE DLVY_END_DT = :v_current_end_dt;

                v_cycle_done := TRUE;
                v_total_enddts_done := v_total_enddts_done + 1;

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject,
                       (SELECT LISTAGG(DISTINCT DLVY_SOURCE_NM, '','') FROM TMP_FILE_STATUS),
                       :v_group_name_scalar,
                       ''PHASE_2'', :v_attempt, :v_current_end_dt,
                       ''CYCLE_COMPLETE'', :v_rows_updated,
                       OBJECT_CONSTRUCT(''attempt_blocked'', FALSE, ''rows_updated'', :v_rows_updated);

                EXIT;
            END WHILE;

            IF (v_cycle_done = FALSE) THEN
                v_block_reason := NVL(v_block_reason_last, ''UNKNOWN'');

                IF (v_total_enddts_done > 0) THEN
                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                           ''PHASE_2'', :v_attempt - 1, :v_current_end_dt,
                           ''BLOCKED_BY_VALIDATION_WARNING'', 0,
                           OBJECT_CONSTRUCT(
                               ''blocked_dlvy_end_dt'', TO_CHAR(:v_current_end_dt, ''YYYY-MM-DD''),
                               ''block_reason'', :v_block_reason,
                               ''attempts_total'', :v_pkg_retries,
                               ''enddts_already_done'', :v_total_enddts_done,
                               ''outcome'', ''WARNING - partial success, continuing workflow''
                           );

                    DELETE FROM TMP_END_DTS_TODO WHERE DLVY_END_DT = :v_current_end_dt;
                ELSE
                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                           ''PHASE_2'', :v_attempt - 1, :v_current_end_dt,
                           ''BLOCKED_BY_VALIDATION'', 0,
                           OBJECT_CONSTRUCT(
                               ''blocked_dlvy_end_dt'', TO_CHAR(:v_current_end_dt, ''YYYY-MM-DD''),
                               ''block_reason'', :v_block_reason,
                               ''attempts_total'', :v_pkg_retries,
                               ''enddts_already_done'', 0,
                               ''outcome'', ''FAIL - no end dates processed''
                           );

                    RAISE ENDDT_BLOCK_REASON;
                END IF;
            END IF;
        END WHILE;

        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
               ''GROUP'', NULL, NULL, ''GROUP_COMPLETE'', 0,
               OBJECT_CONSTRUCT(''group_index'', :v_group_idx);

        v_group_idx := v_group_idx + 1;
    END WHILE;

    INSERT INTO IDENTIFIER(:v_log_table)
    SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, NULL,
           ''DONE'', NULL, NULL, ''SUCCESS_ALL_GROUPS'', 0,
           OBJECT_CONSTRUCT(''groups_total'', :v_groups_cnt, ''enddts_done'', :v_total_enddts_done);

    RETURN OBJECT_CONSTRUCT(''status'',''SUCCESS_ALL_GROUPS'',''subject'',v_subject,''run_id'',v_run_id,''enddts_done'',v_total_enddts_done);

EXCEPTION
    WHEN ENDDT_BLOCK_REASON THEN
        RAISE;
    WHEN OTHER THEN
        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
               ''ERROR'', NULL, NULL, ''EXCEPTION'', 0,
               OBJECT_CONSTRUCT(''failed_step'', :v_step);

        RETURN OBJECT_CONSTRUCT(
            ''status'', ''ERROR'',
            ''subject'', v_subject,
            ''run_id'', v_run_id,
            ''failed_step'', v_step,
            ''error_message'', ''EXCEPTION_THROWN (see query history for details)''
        );
END;
';
