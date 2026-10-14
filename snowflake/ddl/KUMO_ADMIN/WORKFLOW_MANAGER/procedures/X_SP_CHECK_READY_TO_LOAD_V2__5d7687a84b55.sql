-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.X_SP_CHECK_READY_TO_LOAD_V2(VARCHAR, VARCHAR, NUMBER, NUMBER, BOOLEAN)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.X_SP_CHECK_READY_TO_LOAD_V2("DLVY_SUBJECT_AREA_NM" VARCHAR, "PKG_GROUP_NAME" VARCHAR DEFAULT null, "RETRIES" NUMBER(38,0) DEFAULT 1, "DELAY_BETWEEN_RETRIES_SEC" NUMBER(38,0) DEFAULT 0, "REQUIRE_COMMON_ENDDTS" BOOLEAN DEFAULT TRUE)
RETURNS VARIANT
LANGUAGE SQL
COMMENT='user-defined procedure'
EXECUTE AS OWNER
AS '
DECLARE
    v_subject              STRING := UPPER(:DLVY_SUBJECT_AREA_NM);
    v_raw_table            STRING := ''KUMO_TST.RAW_LIM.RAW_LIM_'' || UPPER(:DLVY_SUBJECT_AREA_NM);
    v_results_table        STRING := ''KUMO_TST.RAW_LIM.RAW_LOAD_RESULTS'';
    v_meta_table           STRING := ''KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE'';
    v_status_table         STRING := ''KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_'' || UPPER(:DLVY_SUBJECT_AREA_NM) || ''_PKG_STATUS'';
    v_log_table            STRING := ''KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_LOG'';
    v_run_id               STRING := UUID_STRING();

    v_groups_cnt           NUMBER := 0;
    v_group_idx            NUMBER := 1;
    v_group_name_scalar    STRING;
    v_pkg_retries          NUMBER;
    v_pkg_delay_seconds    NUMBER;

    v_attempt              NUMBER;
    v_todo_cnt             NUMBER := 0;
    v_complete_cnt         NUMBER := 0;
    v_blocked_cnt          NUMBER := 0;
    v_source_driver_cnt    NUMBER := 0;

    v_data_rows_updated    NUMBER := 0;
    v_control_rows_updated NUMBER := 0;
    v_rows_updated         NUMBER := 0;

    v_block_reason         STRING;
    v_step                 STRING := ''INIT'';
    v_total_enddts_done    NUMBER := 0;

    ENDDT_BLOCK_REASON EXCEPTION (-20001, ''ENDDT_BLOCK_REASON: All retries exhausted, no end dates were successfully processed.'');
BEGIN
    -----------------------------------------------------------------------------
    -- NOTE FOR EVALUATION
    -- This V2 version keeps your outer group loop and retry behavior, but removes
    -- the inner per-end-date loop. For each group/attempt, it evaluates all TODO
    -- delivery end dates in one set-based pass, merges status once, and updates
    -- ready raw rows once.
    --
    -- Semantic difference versus the current procedure:
    --   If one end date is blocked while another end date is complete in the same
    --   group/attempt, this V2 version will advance the complete end date. The old
    --   procedure processes MIN(DLVY_END_DT) first and may fail before reaching a
    --   later complete date when no previous end date has succeeded.
    -----------------------------------------------------------------------------

    v_step := ''CREATE_TABLES'';

    CREATE TABLE IF NOT EXISTS IDENTIFIER(:v_meta_table) (
        PKG_GROUP_NAME  STRING,
        SUBJECT_AREA    STRING,
        SOURCE_ID       STRING,
        PKG_ID          STRING DEFAULT ''001'',
        ACTIVE_FL       BOOLEAN,
        UPDATED_AT      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
    );

    CREATE TABLE IF NOT EXISTS IDENTIFIER(:v_log_table) (
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
    );

    CREATE TABLE IF NOT EXISTS IDENTIFIER(:v_status_table) (
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
    );

    INSERT INTO IDENTIFIER(:v_log_table)
    SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :PKG_GROUP_NAME,
           ''INIT'', NULL, NULL, ''START'', 0,
           OBJECT_CONSTRUCT(
               ''raw_table'', :v_raw_table,
               ''results_table'', :v_results_table,
               ''retries'', :RETRIES,
               ''delay_sec'', :DELAY_BETWEEN_RETRIES_SEC,
               ''set_based_version'', TRUE
           );

    -----------------------------------------------------------------------------
    -- Build expected files once.
    -- Keeps original parsing logic, but computes TRIM(DATA) once and carries the
    -- control DW_FILE_NM so the later 000-file update does not need to rescan raw.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_EXPECTED'';

    CREATE OR REPLACE TEMP TABLE TMP_EXPECTED AS
    WITH raw_control AS (
        SELECT
            DW_FILE_NM AS CONTROL_DW_FILE_NM,
            TRIM(DATA) AS DATA_T
        FROM IDENTIFIER(:v_raw_table)
        WHERE SUBSTR(DW_FILE_NM, 8, 3) = ''000''
          AND SUBSTR(TRIM(DATA), 1, 2) = ''10''
    ),
    parsed AS (
        SELECT
            CONTROL_DW_FILE_NM,
            UPPER(TRIM(SUBSTR(DATA_T, 3, 3)))  AS SOURCE_ID,
            UPPER(TRIM(SUBSTR(DATA_T, 6, 4)))  AS SUBJECT_AREA,
            TRIM(SUBSTR(DATA_T, 10, 3))         AS VERSION,
            TRIM(SUBSTR(DATA_T, 13, 9))         AS PKG,
            TRIM(SUBSTR(DATA_T, 22, 3))         AS FILE_NUMBER,
            TRIM(SUBSTR(DATA_T, 25, 3))         AS FILE_VER,
            COALESCE(
                TRY_TO_DATE(SUBSTR(DATA_T, 38, 10), ''YYYY-MM-DD''),
                TRY_TO_DATE(SUBSTR(DATA_T, 38, 8),  ''YYYYMMDD'')
            ) AS DLVY_END_DT
        FROM raw_control
    )
    SELECT
        SOURCE_ID,
        SUBJECT_AREA,
        VERSION,
        PKG,
        FILE_NUMBER,
        FILE_VER,
        DLVY_END_DT,
        CONTROL_DW_FILE_NM,
        (
            SOURCE_ID ||
            SUBJECT_AREA ||
            FILE_NUMBER || ''_'' ||
            FILE_VER || ''_'' ||
            VERSION || ''_0000_'' ||
            PKG || ''_'' ||
            TO_CHAR(DLVY_END_DT, ''YYYYMMDD'')
        ) AS FILE_EXPECTED
    FROM parsed
    WHERE SUBJECT_AREA = :v_subject
      AND DLVY_END_DT IS NOT NULL;

    -----------------------------------------------------------------------------
    -- Build received files once.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_RECEIVED'';

    CREATE OR REPLACE TEMP TABLE TMP_RECEIVED AS
    SELECT
        FILE_NM,
        SUBSTR(FILE_NM, 1, 42) AS FILE_SHORT,
        DLVY_SOURCE_ID         AS SOURCE_ID,
        DLVY_SUBJECT_AREA_ID   AS SUBJECT_AREA,
        DLVY_PKG_ID            AS PKG_ID,
        DLVY_END_DATE          AS DLVY_END_DT,
        FOOTER_EXPECTED_ROWS,
        ACTUAL_DATA_ROWS,
        ROWCOUNT_MATCH
    FROM IDENTIFIER(:v_results_table)
    WHERE DLVY_SUBJECT_AREA_ID = :v_subject
      AND ROWCOUNT_MATCH <> ''CONTROL_FILE'';

    -----------------------------------------------------------------------------
    -- Build groups once.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_GROUPS'';

    CREATE OR REPLACE TEMP TABLE TMP_GROUPS AS
    SELECT
        PKG_GROUP_NAME::STRING AS PKG_GROUP_NAME,
        ROW_NUMBER() OVER (ORDER BY PKG_GROUP_NAME) AS RN
    FROM (
        SELECT DISTINCT PKG_GROUP_NAME
        FROM IDENTIFIER(:v_meta_table)
        WHERE SUBJECT_AREA = :v_subject
          AND ACTIVE_FL = TRUE
          AND (:PKG_GROUP_NAME IS NULL OR PKG_GROUP_NAME = :PKG_GROUP_NAME)
    );

    SELECT COUNT(*) INTO :v_groups_cnt FROM TMP_GROUPS;

    IF (v_groups_cnt = 0) THEN
        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, NULL,
               ''INIT'', NULL, NULL, ''NO_ACTIVE_GROUPS'', 0,
               OBJECT_CONSTRUCT(''meta_table'', :v_meta_table);

        RETURN OBJECT_CONSTRUCT(
            ''status'', ''NO_ACTIVE_GROUPS'',
            ''subject'', v_subject,
            ''run_id'', v_run_id
        );
    END IF;

    CREATE OR REPLACE TEMP TABLE TMP_END_DTS_DONE (DLVY_END_DT DATE);
    CREATE OR REPLACE TEMP TABLE TMP_END_DTS_TODO (DLVY_END_DT DATE);

    -----------------------------------------------------------------------------
    -- Group loop retained; inner end-date loop removed.
    -----------------------------------------------------------------------------
    WHILE (v_group_idx <= v_groups_cnt) DO

        SELECT PKG_GROUP_NAME
          INTO :v_group_name_scalar
        FROM TMP_GROUPS
        WHERE RN = :v_group_idx;

        v_pkg_retries := IFF(:RETRIES IS NULL OR :RETRIES < 1, 1, :RETRIES);
        v_pkg_delay_seconds := IFF(:DELAY_BETWEEN_RETRIES_SEC IS NULL OR :DELAY_BETWEEN_RETRIES_SEC < 0, 0, :DELAY_BETWEEN_RETRIES_SEC);

        v_step := ''PH1_SOURCE_DRIVER'';

        CREATE OR REPLACE TEMP TABLE TMP_SOURCE_DRIVER AS
        SELECT SOURCE_ID, PKG_ID
        FROM IDENTIFIER(:v_meta_table)
        WHERE SUBJECT_AREA = :v_subject
          AND ACTIVE_FL = TRUE
          AND PKG_GROUP_NAME = :v_group_name_scalar;

        SELECT COUNT(*) INTO :v_source_driver_cnt FROM TMP_SOURCE_DRIVER;

        -------------------------------------------------------------------------
        -- Build candidate end dates for the group.
        -------------------------------------------------------------------------
        v_step := ''PH1_COMMON_ENDDTS'';

        IF (:REQUIRE_COMMON_ENDDTS) THEN
            CREATE OR REPLACE TEMP TABLE TMP_END_DTS_TODO AS
            SELECT e.DLVY_END_DT
            FROM TMP_EXPECTED e
            INNER JOIN TMP_SOURCE_DRIVER d
                ON e.SOURCE_ID = d.SOURCE_ID
               AND e.VERSION   = d.PKG_ID
            WHERE NOT EXISTS (
                    SELECT 1
                    FROM TMP_END_DTS_DONE done
                    WHERE done.DLVY_END_DT = e.DLVY_END_DT
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM IDENTIFIER(:v_status_table) st
                    WHERE st.PKG_GROUP_NAME = :v_group_name_scalar
                      AND st.ALL_RECEIVED = TRUE
                      AND st.DLVY_END_DT = e.DLVY_END_DT
                  )
            GROUP BY e.DLVY_END_DT
            HAVING COUNT(DISTINCT e.SOURCE_ID || ''|'' || e.VERSION) = :v_source_driver_cnt
            ORDER BY e.DLVY_END_DT;
        ELSE
            CREATE OR REPLACE TEMP TABLE TMP_END_DTS_TODO AS
            SELECT DISTINCT e.DLVY_END_DT
            FROM TMP_EXPECTED e
            INNER JOIN TMP_SOURCE_DRIVER d
                ON e.SOURCE_ID = d.SOURCE_ID
               AND e.VERSION   = d.PKG_ID
            WHERE NOT EXISTS (
                    SELECT 1
                    FROM TMP_END_DTS_DONE done
                    WHERE done.DLVY_END_DT = e.DLVY_END_DT
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM IDENTIFIER(:v_status_table) st
                    WHERE st.PKG_GROUP_NAME = :v_group_name_scalar
                      AND st.ALL_RECEIVED = TRUE
                      AND st.DLVY_END_DT = e.DLVY_END_DT
                  )
            ORDER BY e.DLVY_END_DT;
        END IF;

        SELECT COUNT(*) INTO :v_todo_cnt FROM TMP_END_DTS_TODO;

        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
               ''PHASE_1'', NULL, NULL, ''COMMON_ENDDTS_FOUND'', 0,
               OBJECT_CONSTRUCT(
                   ''end_dates'', COALESCE((SELECT ARRAY_AGG(DLVY_END_DT) FROM TMP_END_DTS_TODO), ARRAY_CONSTRUCT()),
                   ''end_date_count'', :v_todo_cnt,
                   ''sources'', COALESCE((SELECT ARRAY_AGG(DISTINCT SOURCE_ID) FROM TMP_SOURCE_DRIVER), ARRAY_CONSTRUCT()),
                   ''source_pkg_combos'', COALESCE((SELECT ARRAY_AGG(SOURCE_ID || '':'' || PKG_ID) FROM TMP_SOURCE_DRIVER), ARRAY_CONSTRUCT()),
                   ''source_pkg_count'', :v_source_driver_cnt
               );

        -------------------------------------------------------------------------
        -- Retry loop retained. Each attempt evaluates all remaining TODO dates.
        -------------------------------------------------------------------------
        v_attempt := 1;

        WHILE (v_attempt <= v_pkg_retries AND EXISTS (SELECT 1 FROM TMP_END_DTS_TODO)) DO

            ---------------------------------------------------------------------
            -- Set-based file status for all remaining end dates in this group.
            ---------------------------------------------------------------------
            v_step := ''PH2_FILE_STATUS_ALL'';

            CREATE OR REPLACE TEMP TABLE TMP_FILE_STATUS_ALL AS
            SELECT
                e.SOURCE_ID          AS DLVY_SOURCE_NM,
                e.VERSION            AS PKG_ID,
                e.FILE_EXPECTED,
                e.CONTROL_DW_FILE_NM,
                e.DLVY_END_DT,
                :v_group_name_scalar AS PKG_GROUP_NAME,
                r.FILE_SHORT         AS FILE_RECEIVED,
                CASE
                    WHEN r.FILE_SHORT IS NULL THEN ''MISSING''
                    WHEN r.ROWCOUNT_MATCH = ''MATCH'' THEN ''RECEIVED''
                    WHEN r.ROWCOUNT_MATCH = ''MISMATCH'' THEN ''ROWCOUNT_FAIL''
                    WHEN r.ROWCOUNT_MATCH IN (''NO_FOOTER'', ''CONTROL_FILE'') THEN ''ROWCOUNT_NOT_EVALUATED''
                    WHEN r.ROWCOUNT_MATCH IS NULL THEN ''ROWCOUNT_NOT_EVALUATED''
                    ELSE ''RECEIVED''
                END AS FILE_STATUS,
                r.FOOTER_EXPECTED_ROWS,
                r.ACTUAL_DATA_ROWS
            FROM TMP_EXPECTED e
            INNER JOIN TMP_SOURCE_DRIVER d
                ON e.SOURCE_ID = d.SOURCE_ID
               AND e.VERSION   = d.PKG_ID
            INNER JOIN TMP_END_DTS_TODO todo
                ON e.DLVY_END_DT = todo.DLVY_END_DT
            LEFT JOIN TMP_RECEIVED r
                ON e.FILE_EXPECTED = r.FILE_SHORT;

            ---------------------------------------------------------------------
            -- Set-based end-date status for all remaining dates.
            ---------------------------------------------------------------------
            v_step := ''PH2_END_STATUS_ALL'';

            CREATE OR REPLACE TEMP TABLE TMP_END_STATUS_ALL AS
            SELECT
                PKG_GROUP_NAME,
                :v_subject AS DLVY_SUBJECT_AREA_NM,
                LISTAGG(DISTINCT DLVY_SOURCE_NM, '','') AS DLVY_SOURCE_NM,
                DLVY_END_DT,
                COUNT(*) AS EXPECTED_CNT,
                SUM(IFF(FILE_RECEIVED IS NOT NULL, 1, 0)) AS RECEIVED_PHYSICAL_CNT,
                SUM(IFF(FILE_STATUS = ''RECEIVED'', 1, 0)) AS RECEIVED_VALID_CNT,
                SUM(IFF(FILE_STATUS = ''MISSING'', 1, 0)) AS MISSING_CNT,
                SUM(IFF(FILE_STATUS = ''ROWCOUNT_FAIL'', 1, 0)) AS ROWCOUNT_FAIL_CNT,
                SUM(IFF(FILE_STATUS = ''ROWCOUNT_NOT_EVALUATED'', 1, 0)) AS ROWCOUNT_NE_CNT,
                IFF(
                    SUM(IFF(FILE_STATUS = ''MISSING'', 1, 0)) = 0
                    AND SUM(IFF(FILE_STATUS IN (''ROWCOUNT_FAIL'', ''ROWCOUNT_NOT_EVALUATED''), 1, 0)) = 0
                    AND COUNT(*) > 0,
                    TRUE,
                    FALSE
                ) AS ALL_RECEIVED
            FROM TMP_FILE_STATUS_ALL
            GROUP BY PKG_GROUP_NAME, DLVY_END_DT;

            ---------------------------------------------------------------------
            -- Merge all status rows once per group/attempt.
            ---------------------------------------------------------------------
            v_step := ''PH2_MERGE_STATUS_ALL'';

            MERGE INTO IDENTIFIER(:v_status_table) t
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
                FROM TMP_END_STATUS_ALL
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
                PKG_GROUP_NAME,
                DLVY_SUBJECT_AREA_NM,
                DLVY_SOURCE_NM,
                DLVY_END_DT,
                ALL_RECEIVED,
                EXPECTED_CNT,
                RECEIVED_CNT,
                MISSING_CNT,
                ROWCOUNT_FAIL_CNT,
                LAST_CHECKED_AT,
                UPDATED_AT
            ) VALUES (
                s.PKG_GROUP_NAME,
                s.DLVY_SUBJECT_AREA_NM,
                s.DLVY_SOURCE_NM,
                s.DLVY_END_DT,
                s.ALL_RECEIVED,
                s.EXPECTED_CNT,
                s.RECEIVED_CNT,
                s.MISSING_CNT,
                s.ROWCOUNT_FAIL_CNT,
                s.LAST_CHECKED_AT,
                s.UPDATED_AT
            );

            ---------------------------------------------------------------------
            -- Log one summary row per evaluated end date.
            ---------------------------------------------------------------------
            INSERT INTO IDENTIFIER(:v_log_table)
            SELECT
                :v_run_id,
                CURRENT_TIMESTAMP(),
                :v_subject,
                DLVY_SOURCE_NM,
                :v_group_name_scalar,
                ''PHASE_2'',
                :v_attempt,
                DLVY_END_DT,
                IFF(ALL_RECEIVED, ''ENDDT_COMPLETE'', ''ENDDT_BLOCKED''),
                0,
                OBJECT_CONSTRUCT(
                    ''expected_cnt'', EXPECTED_CNT,
                    ''received_cnt'', RECEIVED_VALID_CNT,
                    ''received_physical_cnt'', RECEIVED_PHYSICAL_CNT,
                    ''missing_cnt'', MISSING_CNT,
                    ''rowcount_fail_cnt'', ROWCOUNT_FAIL_CNT,
                    ''rowcount_not_evaluated_cnt'', ROWCOUNT_NE_CNT
                )
            FROM TMP_END_STATUS_ALL;

            ---------------------------------------------------------------------
            -- Log blocker details for blocked end dates.
            ---------------------------------------------------------------------
            INSERT INTO IDENTIFIER(:v_log_table)
            SELECT
                :v_run_id,
                CURRENT_TIMESTAMP(),
                :v_subject,
                b.DLVY_SOURCE_NM,
                :v_group_name_scalar,
                ''PHASE_2'',
                :v_attempt,
                b.DLVY_END_DT,
                ''ENDDT_BLOCK_REASON'',
                0,
                OBJECT_CONSTRUCT(
                    ''block_reason'', b.BLOCK_REASON,
                    ''failing_files'', b.FAILING_FILES
                )
            FROM (
                SELECT
                    f.DLVY_END_DT,
                    LISTAGG(DISTINCT f.DLVY_SOURCE_NM, '','') AS DLVY_SOURCE_NM,
                    CASE
                        WHEN SUM(IFF(f.FILE_STATUS = ''MISSING'', 1, 0)) > 0 THEN ''MISSING''
                        WHEN SUM(IFF(f.FILE_STATUS = ''ROWCOUNT_FAIL'', 1, 0)) > 0 THEN ''ROWCOUNT_FAIL''
                        WHEN SUM(IFF(f.FILE_STATUS = ''ROWCOUNT_NOT_EVALUATED'', 1, 0)) > 0 THEN ''ROWCOUNT_NOT_EVALUATED''
                        ELSE ''UNKNOWN''
                    END AS BLOCK_REASON,
                    ARRAY_AGG(
                        OBJECT_CONSTRUCT(
                            ''source_id'', f.DLVY_SOURCE_NM,
                            ''pkg_id'', f.PKG_ID,
                            ''file_expected'', f.FILE_EXPECTED,
                            ''file_status'', f.FILE_STATUS,
                            ''footer_expected_rows'', f.FOOTER_EXPECTED_ROWS,
                            ''actual_data_rows'', f.ACTUAL_DATA_ROWS
                        )
                    ) AS FAILING_FILES
                FROM TMP_FILE_STATUS_ALL f
                INNER JOIN TMP_END_STATUS_ALL s
                    ON s.PKG_GROUP_NAME = f.PKG_GROUP_NAME
                   AND s.DLVY_END_DT = f.DLVY_END_DT
                WHERE s.ALL_RECEIVED = FALSE
                  AND f.FILE_STATUS <> ''RECEIVED''
                GROUP BY f.DLVY_END_DT
            ) b;

            SELECT COUNT(*)
              INTO :v_complete_cnt
            FROM TMP_END_STATUS_ALL
            WHERE ALL_RECEIVED = TRUE;

            ---------------------------------------------------------------------
            -- Update all ready files once for the complete end dates from this
            -- attempt. This replaces the per-end-date raw update.
            ---------------------------------------------------------------------
            IF (v_complete_cnt > 0) THEN
                v_step := ''PH2_BUILD_COMPLETE_FILES'';

                CREATE OR REPLACE TEMP TABLE TMP_COMPLETE_FILES AS
                SELECT DISTINCT f.FILE_EXPECTED
                FROM TMP_FILE_STATUS_ALL f
                INNER JOIN TMP_END_STATUS_ALL s
                    ON s.PKG_GROUP_NAME = f.PKG_GROUP_NAME
                   AND s.DLVY_END_DT = f.DLVY_END_DT
                WHERE s.ALL_RECEIVED = TRUE
                  AND f.FILE_STATUS = ''RECEIVED'';

                CREATE OR REPLACE TEMP TABLE TMP_COMPLETE_CONTROL_FILES AS
                SELECT DISTINCT f.CONTROL_DW_FILE_NM AS DW_FILE_NM
                FROM TMP_FILE_STATUS_ALL f
                INNER JOIN TMP_END_STATUS_ALL s
                    ON s.PKG_GROUP_NAME = f.PKG_GROUP_NAME
                   AND s.DLVY_END_DT = f.DLVY_END_DT
                WHERE s.ALL_RECEIVED = TRUE
                  AND f.CONTROL_DW_FILE_NM IS NOT NULL;

                v_step := ''PH2_UPDATE_READY_ALL'';

                UPDATE IDENTIFIER(:v_raw_table) r
                SET DW_READY_TO_LOAD_FL = TRUE
                FROM TMP_COMPLETE_FILES f
                WHERE SUBSTR(r.DW_FILE_NM, 1, 42) = f.FILE_EXPECTED
                  AND IFNULL(r.DW_READY_TO_LOAD_FL, FALSE) = FALSE;

                v_data_rows_updated := SQLROWCOUNT;

                v_step := ''PH2_FLAG_000_ALL'';

                UPDATE IDENTIFIER(:v_raw_table) r
                SET DW_READY_TO_LOAD_FL = TRUE
                FROM TMP_COMPLETE_CONTROL_FILES c
                WHERE r.DW_FILE_NM = c.DW_FILE_NM
                  AND IFNULL(r.DW_READY_TO_LOAD_FL, FALSE) = FALSE;

                v_control_rows_updated := SQLROWCOUNT;
                v_rows_updated := v_data_rows_updated + v_control_rows_updated;

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id,
                    CURRENT_TIMESTAMP(),
                    :v_subject,
                    (SELECT LISTAGG(DISTINCT DLVY_SOURCE_NM, '','') FROM TMP_FILE_STATUS_ALL f INNER JOIN TMP_END_STATUS_ALL s ON s.PKG_GROUP_NAME = f.PKG_GROUP_NAME AND s.DLVY_END_DT = f.DLVY_END_DT WHERE s.ALL_RECEIVED = TRUE),
                    :v_group_name_scalar,
                    ''PHASE_2'',
                    :v_attempt,
                    NULL,
                    ''RAW_READY_FLAG_UPDATED'',
                    :v_rows_updated,
                    OBJECT_CONSTRUCT(
                        ''raw_table'', :v_raw_table,
                        ''complete_end_dates'', (SELECT ARRAY_AGG(DLVY_END_DT) FROM TMP_END_STATUS_ALL WHERE ALL_RECEIVED = TRUE),
                        ''complete_end_date_count'', :v_complete_cnt,
                        ''data_rows_updated'', :v_data_rows_updated,
                        ''control_rows_updated'', :v_control_rows_updated
                    );

                INSERT INTO TMP_END_DTS_DONE (DLVY_END_DT)
                SELECT DLVY_END_DT
                FROM TMP_END_STATUS_ALL s
                WHERE s.ALL_RECEIVED = TRUE
                  AND NOT EXISTS (
                        SELECT 1
                        FROM TMP_END_DTS_DONE done
                        WHERE done.DLVY_END_DT = s.DLVY_END_DT
                  );

                DELETE FROM TMP_END_DTS_TODO todo
                USING TMP_END_STATUS_ALL s
                WHERE todo.DLVY_END_DT = s.DLVY_END_DT
                  AND s.ALL_RECEIVED = TRUE;

                v_total_enddts_done := v_total_enddts_done + v_complete_cnt;

                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT
                    :v_run_id,
                    CURRENT_TIMESTAMP(),
                    :v_subject,
                    DLVY_SOURCE_NM,
                    :v_group_name_scalar,
                    ''PHASE_2'',
                    :v_attempt,
                    DLVY_END_DT,
                    ''CYCLE_COMPLETE'',
                    0,
                    OBJECT_CONSTRUCT(
                        ''attempt_blocked'', FALSE,
                        ''set_based_attempt'', TRUE
                    )
                FROM TMP_END_STATUS_ALL
                WHERE ALL_RECEIVED = TRUE;
            END IF;

            SELECT COUNT(*)
              INTO :v_blocked_cnt
            FROM TMP_END_DTS_TODO;

            IF (v_blocked_cnt = 0) THEN
                EXIT;
            END IF;

            ---------------------------------------------------------------------
            -- Remaining TODO dates are blocked. Retry them, or fail/warn when the
            -- retry budget is exhausted.
            ---------------------------------------------------------------------
            IF (v_attempt < v_pkg_retries) THEN
                INSERT INTO IDENTIFIER(:v_log_table)
                SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
                       ''PHASE_2'', :v_attempt, NULL,
                       ''WAIT_BEFORE_RETRY'', 0,
                       OBJECT_CONSTRUCT(
                           ''delay_seconds'', :v_pkg_delay_seconds,
                           ''next_attempt'', :v_attempt + 1,
                           ''remaining_end_dates'', (SELECT ARRAY_AGG(DLVY_END_DT) FROM TMP_END_DTS_TODO)
                       );

                EXECUTE IMMEDIATE ''CALL SYSTEM$WAIT('' || TO_VARCHAR(v_pkg_delay_seconds) || '')'';
                v_attempt := v_attempt + 1;
            ELSE
                IF (v_total_enddts_done > 0) THEN
                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT
                        :v_run_id,
                        CURRENT_TIMESTAMP(),
                        :v_subject,
                        NULL,
                        :v_group_name_scalar,
                        ''PHASE_2'',
                        :v_attempt,
                        s.DLVY_END_DT,
                        ''BLOCKED_BY_VALIDATION_WARNING'',
                        0,
                        OBJECT_CONSTRUCT(
                            ''blocked_dlvy_end_dt'', TO_CHAR(s.DLVY_END_DT, ''YYYY-MM-DD''),
                            ''block_reason'', CASE
                                WHEN s.MISSING_CNT > 0 THEN ''MISSING''
                                WHEN s.ROWCOUNT_FAIL_CNT > 0 THEN ''ROWCOUNT_FAIL''
                                WHEN s.ROWCOUNT_NE_CNT > 0 THEN ''ROWCOUNT_NOT_EVALUATED''
                                ELSE ''UNKNOWN''
                            END,
                            ''attempts_total'', :v_pkg_retries,
                            ''enddts_already_done'', :v_total_enddts_done,
                            ''outcome'', ''WARNING - partial success, continuing workflow'',
                            ''set_based_attempt'', TRUE
                        )
                    FROM TMP_END_STATUS_ALL s
                    WHERE s.ALL_RECEIVED = FALSE;

                    DELETE FROM TMP_END_DTS_TODO;
                    EXIT;
                ELSE
                    INSERT INTO IDENTIFIER(:v_log_table)
                    SELECT
                        :v_run_id,
                        CURRENT_TIMESTAMP(),
                        :v_subject,
                        NULL,
                        :v_group_name_scalar,
                        ''PHASE_2'',
                        :v_attempt,
                        s.DLVY_END_DT,
                        ''BLOCKED_BY_VALIDATION'',
                        0,
                        OBJECT_CONSTRUCT(
                            ''blocked_dlvy_end_dt'', TO_CHAR(s.DLVY_END_DT, ''YYYY-MM-DD''),
                            ''block_reason'', CASE
                                WHEN s.MISSING_CNT > 0 THEN ''MISSING''
                                WHEN s.ROWCOUNT_FAIL_CNT > 0 THEN ''ROWCOUNT_FAIL''
                                WHEN s.ROWCOUNT_NE_CNT > 0 THEN ''ROWCOUNT_NOT_EVALUATED''
                                ELSE ''UNKNOWN''
                            END,
                            ''attempts_total'', :v_pkg_retries,
                            ''enddts_already_done'', 0,
                            ''outcome'', ''FAIL - no end dates processed'',
                            ''set_based_attempt'', TRUE
                        )
                    FROM TMP_END_STATUS_ALL s
                    WHERE s.ALL_RECEIVED = FALSE;

                    RAISE ENDDT_BLOCK_REASON;
                END IF;
            END IF;
        END WHILE;

        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
               ''GROUP'', NULL, NULL, ''GROUP_COMPLETE'', 0,
               OBJECT_CONSTRUCT(
                   ''group_index'', :v_group_idx,
                   ''set_based_version'', TRUE
               );

        v_group_idx := v_group_idx + 1;
    END WHILE;

    INSERT INTO IDENTIFIER(:v_log_table)
    SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, NULL,
           ''DONE'', NULL, NULL, ''SUCCESS_ALL_GROUPS'', 0,
           OBJECT_CONSTRUCT(
               ''groups_total'', :v_groups_cnt,
               ''enddts_done'', :v_total_enddts_done,
               ''set_based_version'', TRUE
           );

    RETURN OBJECT_CONSTRUCT(
        ''status'', ''SUCCESS_ALL_GROUPS'',
        ''subject'', v_subject,
        ''run_id'', v_run_id,
        ''enddts_done'', v_total_enddts_done,
        ''set_based_version'', TRUE
    );

EXCEPTION
    WHEN ENDDT_BLOCK_REASON THEN
        RAISE;

    WHEN OTHER THEN
        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT :v_run_id, CURRENT_TIMESTAMP(), :v_subject, NULL, :v_group_name_scalar,
               ''ERROR'', NULL, NULL, ''EXCEPTION'', 0,
               OBJECT_CONSTRUCT(
                   ''failed_step'', :v_step,
                   ''set_based_version'', TRUE
               );

        RETURN OBJECT_CONSTRUCT(
            ''status'', ''ERROR'',
            ''subject'', v_subject,
            ''run_id'', v_run_id,
            ''failed_step'', v_step,
            ''error_message'', ''EXCEPTION_THROWN (see query history for details)'',
            ''set_based_version'', TRUE
        );
END;
';
