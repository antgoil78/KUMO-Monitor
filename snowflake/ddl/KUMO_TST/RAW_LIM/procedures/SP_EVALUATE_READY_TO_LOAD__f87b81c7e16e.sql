-- Source: KUMO_TST.RAW_LIM.SP_EVALUATE_READY_TO_LOAD(VARCHAR, VARCHAR, BOOLEAN)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_TST";
USE SCHEMA "KUMO_TST"."RAW_LIM";

CREATE OR REPLACE PROCEDURE KUMO_TST.RAW_LIM.SP_EVALUATE_READY_TO_LOAD("DLVY_SUBJECT_AREA_NM" VARCHAR, "PKG_GROUP_NAME" VARCHAR DEFAULT null, "REQUIRE_COMMON_ENDDTS" BOOLEAN DEFAULT TRUE)
RETURNS TABLE ("RUN_ID" VARCHAR, "PKG_GROUP_NAME" VARCHAR, "DLVY_SUBJECT_AREA_NM" VARCHAR, "DLVY_END_DT" DATE, "READY_TO_PROCESS" BOOLEAN, "BLOCK_REASON" VARCHAR, "ALL_EXPECTED_SOURCES_PRESENT" BOOLEAN, "SEQUENCE_VALID" BOOLEAN, "REQUIRED_SOURCE_PKG_COUNT" NUMBER(38,0), "FOUND_SOURCE_PKG_COUNT" NUMBER(38,0), "EXPECTED_FILE_CNT" NUMBER(38,0), "RECEIVED_VALID_CNT" NUMBER(38,0), "RECEIVED_PHYSICAL_CNT" NUMBER(38,0), "MISSING_FILE_CNT" NUMBER(38,0), "ROWCOUNT_FAIL_CNT" NUMBER(38,0), "ROWCOUNT_NOT_EVALUATED_CNT" NUMBER(38,0), "SEQUENCE_MISSING_VALUE_CNT" NUMBER(38,0), "OUT_OF_SEQUENCE_CNT" NUMBER(38,0), "RECEIVED_SEQUENCE_MISMATCH_CNT" NUMBER(38,0), "EXPECTED_SOURCE_PKGS" VARIANT, "FOUND_SOURCE_PKGS" VARIANT, "MISSING_SOURCE_PKGS" VARIANT, "SEQUENCE_DETAILS" VARIANT, "OUT_OF_SEQUENCE_FILES" VARIANT, "FAILING_FILES" VARIANT)
LANGUAGE SQL
EXECUTE AS OWNER
AS '
DECLARE
    v_subject       STRING := UPPER(:DLVY_SUBJECT_AREA_NM);
    v_raw_table     STRING := ''KUMO_TST.RAW_LIM.RAW_LIM_'' || UPPER(:DLVY_SUBJECT_AREA_NM);
    v_results_table STRING := ''KUMO_TST.RAW_LIM.RAW_LOAD_RESULTS'';
    v_meta_table    STRING := ''KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE'';
    v_log_table     STRING := ''KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_LOG'';
    v_run_id        STRING := UUID_STRING();
    v_step          STRING := ''INIT'';
    v_result        RESULTSET;
BEGIN
    -----------------------------------------------------------------------------
    -- Purpose
    --   Pure readiness evaluator. No status table. No RAW update.
    --   Returns one row per package group + delivery end date with enough detail
    --   to decide whether that end date can be processed.
    --
    -- Source tables only
    --   RAW_LIM_<subject>
    --   RAW_LIM_PKG_GROUP_SOURCE
    --   RAW_LOAD_RESULTS
    --   RAW_LIM_PKG_LOG
    --
    -- Important source rule
    --   000/control files where DW_READY_TO_LOAD_FL = TRUE are excluded before
    --   expected files are built. Already-ready control files should not create
    --   candidate dates or influence the current readiness decision.
    --
    -- Sequence rule, simple version
    --   Sequence values are read from RAW_LOAD_RESULTS, not RAW_LIM_<subject>.
    --   For each PKG_GROUP_NAME + SOURCE_ID + PKG_ID, only the lowest received
    --   sequence among current not-ready candidates is eligible.
    --
    --   A delivery date is sequence-valid only when all received files for that
    --   date have sequence values, are on the next eligible sequence for their
    --   source/pkg, and do not disagree with each other within the same
    --   group/date/source/pkg.
    -----------------------------------------------------------------------------

    v_step := ''CREATE_LOG_TABLE'';

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

    INSERT INTO IDENTIFIER(:v_log_table)
    SELECT
        :v_run_id,
        CURRENT_TIMESTAMP(),
        :v_subject,
        NULL,
        :PKG_GROUP_NAME,
        ''EVALUATE'',
        NULL,
        NULL,
        ''START'',
        0,
        OBJECT_CONSTRUCT(
            ''raw_table'', :v_raw_table,
            ''results_table'', :v_results_table,
            ''meta_table'', :v_meta_table,
            ''require_common_enddts'', :REQUIRE_COMMON_ENDDTS,
            ''status_table_used'', FALSE,
            ''raw_update_performed'', FALSE,
            ''exclude_ready_control_files'', TRUE,
            ''sequence_validation'', TRUE,
            ''sequence_source_table'', :v_results_table,
            ''sequence_columns'', ARRAY_CONSTRUCT(''DLVY_PKG_YEAR'', ''DLVY_PKG_YEAR_SEQ_NO'')
        );

    -----------------------------------------------------------------------------
    -- 1. Active source/pkg requirements per group.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_DRIVER'';

    CREATE OR REPLACE TEMP TABLE TMP_DRIVER AS
    SELECT DISTINCT
        PKG_GROUP_NAME::STRING AS PKG_GROUP_NAME,
        UPPER(TRIM(SOURCE_ID)) AS SOURCE_ID,
        TRIM(COALESCE(PKG_ID, ''001'')) AS PKG_ID
    FROM IDENTIFIER(:v_meta_table)
    WHERE SUBJECT_AREA = :v_subject
      AND ACTIVE_FL = TRUE
      AND (:PKG_GROUP_NAME IS NULL OR PKG_GROUP_NAME = :PKG_GROUP_NAME);

    CREATE OR REPLACE TEMP TABLE TMP_GROUP_REQUIREMENTS AS
    SELECT
        PKG_GROUP_NAME,
        COUNT(DISTINCT SOURCE_ID || ''|'' || PKG_ID) AS REQUIRED_SOURCE_PKG_COUNT,
        ARRAY_AGG(DISTINCT SOURCE_ID || '':'' || PKG_ID)
            WITHIN GROUP (ORDER BY SOURCE_ID || '':'' || PKG_ID) AS EXPECTED_SOURCE_PKGS
    FROM TMP_DRIVER
    GROUP BY PKG_GROUP_NAME;

    -----------------------------------------------------------------------------
    -- 2. Expected files from RAW control records.
    --    This intentionally does NOT read DLVY_PKG_YEAR / DLVY_PKG_YEAR_SEQ_NO
    --    from RAW_LIM_<subject>. Those values are read from RAW_LOAD_RESULTS.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_EXPECTED'';

    CREATE OR REPLACE TEMP TABLE TMP_EXPECTED AS
    WITH raw_control AS (
        SELECT
            DW_FILE_NM AS CONTROL_DW_FILE_NM,
            TRIM(DATA) AS DATA_T
        FROM IDENTIFIER(:v_raw_table)
        WHERE SUBSTR(DW_FILE_NM, 8, 3) = ''000''
          AND IFNULL(DW_READY_TO_LOAD_FL, FALSE) = FALSE
          AND SUBSTR(TRIM(DATA), 1, 2) = ''10''
    ),
    parsed AS (
        SELECT
            CONTROL_DW_FILE_NM,
            UPPER(TRIM(SUBSTR(DATA_T, 3, 3)))  AS SOURCE_ID,
            UPPER(TRIM(SUBSTR(DATA_T, 6, 4)))  AS SUBJECT_AREA,
            TRIM(SUBSTR(DATA_T, 10, 3))        AS PKG_ID,
            TRIM(SUBSTR(DATA_T, 13, 9))        AS PKG,
            TRIM(SUBSTR(DATA_T, 22, 3))        AS FILE_NUMBER,
            TRIM(SUBSTR(DATA_T, 25, 3))        AS FILE_VER,
            COALESCE(
                TRY_TO_DATE(SUBSTR(DATA_T, 38, 10), ''YYYY-MM-DD''),
                TRY_TO_DATE(SUBSTR(DATA_T, 38, 8),  ''YYYYMMDD'')
            ) AS DLVY_END_DT
        FROM raw_control
    )
    SELECT DISTINCT
        SOURCE_ID,
        SUBJECT_AREA,
        PKG_ID,
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
            PKG_ID || ''_0000_'' ||
            PKG || ''_'' ||
            TO_CHAR(DLVY_END_DT, ''YYYYMMDD'')
        ) AS FILE_EXPECTED
    FROM parsed
    WHERE SUBJECT_AREA = :v_subject
      AND DLVY_END_DT IS NOT NULL;

    -----------------------------------------------------------------------------
    -- 3. Expected files scoped to configured package groups.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_EXPECTED_GROUP'';

    CREATE OR REPLACE TEMP TABLE TMP_EXPECTED_GROUP AS
    SELECT
        d.PKG_GROUP_NAME,
        e.SOURCE_ID,
        e.SUBJECT_AREA,
        e.PKG_ID,
        e.PKG,
        e.FILE_NUMBER,
        e.FILE_VER,
        e.DLVY_END_DT,
        e.CONTROL_DW_FILE_NM,
        e.FILE_EXPECTED
    FROM TMP_EXPECTED e
    INNER JOIN TMP_DRIVER d
        ON e.SOURCE_ID = d.SOURCE_ID
       AND e.PKG_ID    = d.PKG_ID;

    -----------------------------------------------------------------------------
    -- 4. Source/pkg coverage per group + delivery end date.
    --    Important: we do NOT filter away non-common dates. We return them with
    --    ALL_EXPECTED_SOURCES_PRESENT = FALSE so the decision is visible.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_DATE_COVERAGE'';

    CREATE OR REPLACE TEMP TABLE TMP_DATE_COVERAGE AS
    SELECT
        eg.PKG_GROUP_NAME,
        eg.DLVY_END_DT,
        COUNT(DISTINCT eg.SOURCE_ID || ''|'' || eg.PKG_ID) AS FOUND_SOURCE_PKG_COUNT,
        ARRAY_AGG(DISTINCT eg.SOURCE_ID || '':'' || eg.PKG_ID)
            WITHIN GROUP (ORDER BY eg.SOURCE_ID || '':'' || eg.PKG_ID) AS FOUND_SOURCE_PKGS
    FROM TMP_EXPECTED_GROUP eg
    GROUP BY eg.PKG_GROUP_NAME, eg.DLVY_END_DT;

    CREATE OR REPLACE TEMP TABLE TMP_CANDIDATE_DATES AS
    SELECT
        c.PKG_GROUP_NAME,
        c.DLVY_END_DT,
        g.REQUIRED_SOURCE_PKG_COUNT,
        c.FOUND_SOURCE_PKG_COUNT,
        IFF(c.FOUND_SOURCE_PKG_COUNT = g.REQUIRED_SOURCE_PKG_COUNT, TRUE, FALSE) AS ALL_EXPECTED_SOURCES_PRESENT,
        g.EXPECTED_SOURCE_PKGS,
        c.FOUND_SOURCE_PKGS
    FROM TMP_DATE_COVERAGE c
    INNER JOIN TMP_GROUP_REQUIREMENTS g
        ON c.PKG_GROUP_NAME = g.PKG_GROUP_NAME;

    -----------------------------------------------------------------------------
    -- 5. Missing source/pkg combinations per candidate date.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_MISSING_SOURCE_PKGS'';

    CREATE OR REPLACE TEMP TABLE TMP_FOUND_SOURCE_PKGS AS
    SELECT DISTINCT
        PKG_GROUP_NAME,
        DLVY_END_DT,
        SOURCE_ID,
        PKG_ID
    FROM TMP_EXPECTED_GROUP;

    CREATE OR REPLACE TEMP TABLE TMP_MISSING_SOURCE_PKGS AS
    SELECT
        c.PKG_GROUP_NAME,
        c.DLVY_END_DT,
        ARRAY_AGG(d.SOURCE_ID || '':'' || d.PKG_ID)
            WITHIN GROUP (ORDER BY d.SOURCE_ID || '':'' || d.PKG_ID) AS MISSING_SOURCE_PKGS
    FROM TMP_CANDIDATE_DATES c
    INNER JOIN TMP_DRIVER d
        ON c.PKG_GROUP_NAME = d.PKG_GROUP_NAME
    LEFT JOIN TMP_FOUND_SOURCE_PKGS f
        ON f.PKG_GROUP_NAME = c.PKG_GROUP_NAME
       AND f.DLVY_END_DT = c.DLVY_END_DT
       AND f.SOURCE_ID = d.SOURCE_ID
       AND f.PKG_ID = d.PKG_ID
    WHERE f.SOURCE_ID IS NULL
    GROUP BY c.PKG_GROUP_NAME, c.DLVY_END_DT;

    -----------------------------------------------------------------------------
    -- 6. Received files from RAW_LOAD_RESULTS.
    --    DLVY_PKG_YEAR and DLVY_PKG_YEAR_SEQ_NO are used for sequence validation.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_RECEIVED'';

    CREATE OR REPLACE TEMP TABLE TMP_RECEIVED AS
    SELECT
        FILE_NM,
        SUBSTR(FILE_NM, 1, 42) AS FILE_SHORT,
        UPPER(TRIM(DLVY_SOURCE_ID))       AS SOURCE_ID,
        UPPER(TRIM(DLVY_SUBJECT_AREA_ID)) AS SUBJECT_AREA,
        TRIM(DLVY_PKG_ID)                 AS PKG_ID,
        DLVY_END_DATE                     AS DLVY_END_DT,
        TRY_TO_NUMBER(DLVY_PKG_YEAR) AS DLVY_PKG_YEAR,
        TRY_TO_NUMBER(DLVY_PKG_YEAR_SEQ_NO) AS DLVY_PKG_YEAR_SEQ_NO,
        IFF(
            TRY_TO_NUMBER(DLVY_PKG_YEAR) IS NOT NULL
            AND TRY_TO_NUMBER(DLVY_PKG_YEAR_SEQ_NO) IS NOT NULL,
            TRY_TO_NUMBER(DLVY_PKG_YEAR) * 1000000000000 + TRY_TO_NUMBER(DLVY_PKG_YEAR_SEQ_NO),
            NULL
        ) AS RECEIVED_SEQ_KEY,
        FOOTER_EXPECTED_ROWS,
        ACTUAL_DATA_ROWS,
        ROWCOUNT_MATCH
    FROM IDENTIFIER(:v_results_table)
    WHERE UPPER(TRIM(DLVY_SUBJECT_AREA_ID)) = :v_subject
      AND ROWCOUNT_MATCH <> ''CONTROL_FILE'';

    -----------------------------------------------------------------------------
    -- 7. File-level status for all candidate dates.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_FILE_STATUS_BASE'';

    CREATE OR REPLACE TEMP TABLE TMP_FILE_STATUS_BASE AS
    SELECT
        eg.PKG_GROUP_NAME,
        eg.DLVY_END_DT,
        eg.SOURCE_ID AS DLVY_SOURCE_NM,
        eg.PKG_ID,
        eg.FILE_EXPECTED,
        eg.CONTROL_DW_FILE_NM,
        r.FILE_SHORT AS FILE_RECEIVED,
        r.DLVY_PKG_YEAR AS RECEIVED_DLVY_PKG_YEAR,
        r.DLVY_PKG_YEAR_SEQ_NO AS RECEIVED_DLVY_PKG_YEAR_SEQ_NO,
        r.RECEIVED_SEQ_KEY,
        CASE
            WHEN r.FILE_SHORT IS NULL THEN ''MISSING''
            WHEN r.ROWCOUNT_MATCH = ''MATCH'' THEN ''RECEIVED''
            WHEN r.ROWCOUNT_MATCH = ''MISMATCH'' THEN ''ROWCOUNT_FAIL''
            WHEN r.ROWCOUNT_MATCH IN (''NO_FOOTER'', ''CONTROL_FILE'') THEN ''ROWCOUNT_NOT_EVALUATED''
            WHEN r.ROWCOUNT_MATCH IS NULL THEN ''ROWCOUNT_NOT_EVALUATED''
            ELSE ''RECEIVED''
        END AS FILE_STATUS,
        r.FOOTER_EXPECTED_ROWS,
        r.ACTUAL_DATA_ROWS,
        r.ROWCOUNT_MATCH
    FROM TMP_EXPECTED_GROUP eg
    INNER JOIN TMP_CANDIDATE_DATES c
        ON c.PKG_GROUP_NAME = eg.PKG_GROUP_NAME
       AND c.DLVY_END_DT = eg.DLVY_END_DT
    LEFT JOIN TMP_RECEIVED r
        ON r.FILE_SHORT = eg.FILE_EXPECTED;

    -----------------------------------------------------------------------------
    -- 8. Next eligible received sequence per package group + source/pkg.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_NEXT_SEQUENCE'';

    CREATE OR REPLACE TEMP TABLE TMP_NEXT_SEQUENCE AS
    SELECT
        PKG_GROUP_NAME,
        DLVY_SOURCE_NM,
        PKG_ID,
        MIN(RECEIVED_SEQ_KEY) AS NEXT_SEQ_KEY
    FROM TMP_FILE_STATUS_BASE
    WHERE FILE_RECEIVED IS NOT NULL
      AND RECEIVED_SEQ_KEY IS NOT NULL
    GROUP BY PKG_GROUP_NAME, DLVY_SOURCE_NM, PKG_ID;

    CREATE OR REPLACE TEMP TABLE TMP_DATE_SOURCE_SEQUENCE AS
    SELECT
        PKG_GROUP_NAME,
        DLVY_END_DT,
        DLVY_SOURCE_NM,
        PKG_ID,
        COUNT(DISTINCT RECEIVED_SEQ_KEY) AS DISTINCT_RECEIVED_SEQ_CNT,
        MIN(RECEIVED_SEQ_KEY) AS MIN_RECEIVED_SEQ_KEY,
        MAX(RECEIVED_SEQ_KEY) AS MAX_RECEIVED_SEQ_KEY
    FROM TMP_FILE_STATUS_BASE
    WHERE FILE_RECEIVED IS NOT NULL
      AND RECEIVED_SEQ_KEY IS NOT NULL
    GROUP BY PKG_GROUP_NAME, DLVY_END_DT, DLVY_SOURCE_NM, PKG_ID;

    v_step := ''BUILD_FILE_STATUS'';

    CREATE OR REPLACE TEMP TABLE TMP_FILE_STATUS AS
    SELECT
        b.*,
        ns.NEXT_SEQ_KEY,
        ds.DISTINCT_RECEIVED_SEQ_CNT,
        IFF(
            b.FILE_RECEIVED IS NULL,
            NULL,
            IFF(
                b.RECEIVED_SEQ_KEY IS NOT NULL
                AND ns.NEXT_SEQ_KEY IS NOT NULL
                AND b.RECEIVED_SEQ_KEY = ns.NEXT_SEQ_KEY,
                TRUE,
                FALSE
            )
        ) AS IS_NEXT_SEQUENCE,
        IFF(
            b.FILE_RECEIVED IS NULL,
            NULL,
            IFF(NVL(ds.DISTINCT_RECEIVED_SEQ_CNT, 0) <= 1, TRUE, FALSE)
        ) AS RECEIVED_SEQUENCE_CONSISTENT
    FROM TMP_FILE_STATUS_BASE b
    LEFT JOIN TMP_NEXT_SEQUENCE ns
        ON ns.PKG_GROUP_NAME = b.PKG_GROUP_NAME
       AND ns.DLVY_SOURCE_NM = b.DLVY_SOURCE_NM
       AND ns.PKG_ID = b.PKG_ID
    LEFT JOIN TMP_DATE_SOURCE_SEQUENCE ds
        ON ds.PKG_GROUP_NAME = b.PKG_GROUP_NAME
       AND ds.DLVY_END_DT = b.DLVY_END_DT
       AND ds.DLVY_SOURCE_NM = b.DLVY_SOURCE_NM
       AND ds.PKG_ID = b.PKG_ID;

    -----------------------------------------------------------------------------
    -- 9. Aggregate file-level status and sequence status to date-level facts.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_FILE_STATUS_AGG'';

    CREATE OR REPLACE TEMP TABLE TMP_FILE_STATUS_AGG AS
    SELECT
        PKG_GROUP_NAME,
        DLVY_END_DT,
        COUNT(*) AS EXPECTED_FILE_CNT,
        SUM(IFF(FILE_RECEIVED IS NOT NULL, 1, 0)) AS RECEIVED_PHYSICAL_CNT,
        SUM(IFF(FILE_STATUS = ''RECEIVED'', 1, 0)) AS RECEIVED_VALID_CNT,
        SUM(IFF(FILE_STATUS = ''MISSING'', 1, 0)) AS MISSING_FILE_CNT,
        SUM(IFF(FILE_STATUS = ''ROWCOUNT_FAIL'', 1, 0)) AS ROWCOUNT_FAIL_CNT,
        SUM(IFF(FILE_STATUS = ''ROWCOUNT_NOT_EVALUATED'', 1, 0)) AS ROWCOUNT_NOT_EVALUATED_CNT,
        SUM(IFF(FILE_RECEIVED IS NOT NULL AND RECEIVED_SEQ_KEY IS NULL, 1, 0)) AS SEQUENCE_MISSING_VALUE_CNT,
        SUM(IFF(FILE_RECEIVED IS NOT NULL AND RECEIVED_SEQ_KEY IS NOT NULL AND IS_NEXT_SEQUENCE = FALSE, 1, 0)) AS OUT_OF_SEQUENCE_CNT,
        SUM(IFF(FILE_RECEIVED IS NOT NULL AND RECEIVED_SEQUENCE_CONSISTENT = FALSE, 1, 0)) AS RECEIVED_SEQUENCE_MISMATCH_CNT,
        IFF(
            SUM(IFF(FILE_RECEIVED IS NOT NULL AND RECEIVED_SEQ_KEY IS NULL, 1, 0)) = 0
            AND SUM(IFF(FILE_RECEIVED IS NOT NULL AND RECEIVED_SEQ_KEY IS NOT NULL AND IS_NEXT_SEQUENCE = FALSE, 1, 0)) = 0
            AND SUM(IFF(FILE_RECEIVED IS NOT NULL AND RECEIVED_SEQUENCE_CONSISTENT = FALSE, 1, 0)) = 0,
            TRUE,
            FALSE
        ) AS SEQUENCE_VALID
    FROM TMP_FILE_STATUS
    GROUP BY PKG_GROUP_NAME, DLVY_END_DT;

    CREATE OR REPLACE TEMP TABLE TMP_SEQUENCE_DETAILS AS
    SELECT
        PKG_GROUP_NAME,
        DLVY_END_DT,
        ARRAY_AGG(
            OBJECT_CONSTRUCT(
                ''source_id'', DLVY_SOURCE_NM,
                ''pkg_id'', PKG_ID,
                ''file_expected'', FILE_EXPECTED,
                ''file_received'', FILE_RECEIVED,
                ''received_dlvy_pkg_year'', RECEIVED_DLVY_PKG_YEAR,
                ''received_dlvy_pkg_year_seq_no'', RECEIVED_DLVY_PKG_YEAR_SEQ_NO,
                ''received_seq_key'', RECEIVED_SEQ_KEY,
                ''next_seq_key_for_source_pkg'', NEXT_SEQ_KEY,
                ''is_next_sequence'', IS_NEXT_SEQUENCE,
                ''distinct_received_seq_cnt_for_date_source_pkg'', DISTINCT_RECEIVED_SEQ_CNT,
                ''received_sequence_consistent'', RECEIVED_SEQUENCE_CONSISTENT
            )
        ) AS SEQUENCE_DETAILS
    FROM TMP_FILE_STATUS
    GROUP BY PKG_GROUP_NAME, DLVY_END_DT;

    CREATE OR REPLACE TEMP TABLE TMP_OUT_OF_SEQUENCE_FILES AS
    SELECT
        PKG_GROUP_NAME,
        DLVY_END_DT,
        ARRAY_AGG(
            OBJECT_CONSTRUCT(
                ''source_id'', DLVY_SOURCE_NM,
                ''pkg_id'', PKG_ID,
                ''file_expected'', FILE_EXPECTED,
                ''file_received'', FILE_RECEIVED,
                ''received_dlvy_pkg_year'', RECEIVED_DLVY_PKG_YEAR,
                ''received_dlvy_pkg_year_seq_no'', RECEIVED_DLVY_PKG_YEAR_SEQ_NO,
                ''received_seq_key'', RECEIVED_SEQ_KEY,
                ''next_seq_key_for_source_pkg'', NEXT_SEQ_KEY,
                ''is_next_sequence'', IS_NEXT_SEQUENCE,
                ''distinct_received_seq_cnt_for_date_source_pkg'', DISTINCT_RECEIVED_SEQ_CNT,
                ''received_sequence_consistent'', RECEIVED_SEQUENCE_CONSISTENT,
                ''reason'', CASE
                    WHEN FILE_RECEIVED IS NOT NULL AND RECEIVED_SEQ_KEY IS NULL THEN ''MISSING_RECEIVED_SEQUENCE_VALUES''
                    WHEN FILE_RECEIVED IS NOT NULL AND RECEIVED_SEQUENCE_CONSISTENT = FALSE THEN ''INCONSISTENT_RECEIVED_SEQUENCE_WITHIN_DATE_SOURCE_PKG''
                    WHEN FILE_RECEIVED IS NOT NULL AND IS_NEXT_SEQUENCE = FALSE THEN ''NOT_NEXT_SEQUENCE_FOR_SOURCE_PKG''
                    ELSE ''UNKNOWN''
                END
            )
        ) AS OUT_OF_SEQUENCE_FILES
    FROM TMP_FILE_STATUS
    WHERE FILE_RECEIVED IS NOT NULL
      AND (
            RECEIVED_SEQ_KEY IS NULL
         OR IS_NEXT_SEQUENCE = FALSE
         OR RECEIVED_SEQUENCE_CONSISTENT = FALSE
      )
    GROUP BY PKG_GROUP_NAME, DLVY_END_DT;

    CREATE OR REPLACE TEMP TABLE TMP_FAILING_FILES AS
    SELECT
        PKG_GROUP_NAME,
        DLVY_END_DT,
        ARRAY_AGG(
            OBJECT_CONSTRUCT(
                ''source_id'', DLVY_SOURCE_NM,
                ''pkg_id'', PKG_ID,
                ''file_expected'', FILE_EXPECTED,
                ''file_received'', FILE_RECEIVED,
                ''file_status'', FILE_STATUS,
                ''rowcount_match'', ROWCOUNT_MATCH,
                ''footer_expected_rows'', FOOTER_EXPECTED_ROWS,
                ''actual_data_rows'', ACTUAL_DATA_ROWS,
                ''received_dlvy_pkg_year'', RECEIVED_DLVY_PKG_YEAR,
                ''received_dlvy_pkg_year_seq_no'', RECEIVED_DLVY_PKG_YEAR_SEQ_NO
            )
        ) AS FAILING_FILES
    FROM TMP_FILE_STATUS
    WHERE FILE_STATUS <> ''RECEIVED''
    GROUP BY PKG_GROUP_NAME, DLVY_END_DT;

    -----------------------------------------------------------------------------
    -- 10. Final decision table.
    -----------------------------------------------------------------------------
    v_step := ''BUILD_DECISION'';

    CREATE OR REPLACE TEMP TABLE TMP_DECISION AS
    SELECT
        :v_run_id AS RUN_ID,
        c.PKG_GROUP_NAME,
        :v_subject AS DLVY_SUBJECT_AREA_NM,
        c.DLVY_END_DT,
        IFF(
            (:REQUIRE_COMMON_ENDDTS = FALSE OR c.ALL_EXPECTED_SOURCES_PRESENT = TRUE)
            AND a.EXPECTED_FILE_CNT > 0
            AND a.MISSING_FILE_CNT = 0
            AND a.SEQUENCE_VALID = TRUE
            AND a.ROWCOUNT_FAIL_CNT = 0
            AND a.ROWCOUNT_NOT_EVALUATED_CNT = 0,
            TRUE,
            FALSE
        ) AS READY_TO_PROCESS,
        CASE
            WHEN :REQUIRE_COMMON_ENDDTS = TRUE AND c.ALL_EXPECTED_SOURCES_PRESENT = FALSE
                THEN ''NOT_ALL_EXPECTED_SOURCES_PRESENT''
            WHEN a.EXPECTED_FILE_CNT = 0
                THEN ''NO_EXPECTED_FILES''
            WHEN a.MISSING_FILE_CNT > 0
                THEN ''MISSING_FILES''
            WHEN a.SEQUENCE_MISSING_VALUE_CNT > 0
                THEN ''MISSING_SEQUENCE_VALUES''
            WHEN a.RECEIVED_SEQUENCE_MISMATCH_CNT > 0
                THEN ''RECEIVED_SEQUENCE_MISMATCH''
            WHEN a.OUT_OF_SEQUENCE_CNT > 0
                THEN ''OUT_OF_SEQUENCE''
            WHEN a.ROWCOUNT_FAIL_CNT > 0
                THEN ''ROWCOUNT_FAIL''
            WHEN a.ROWCOUNT_NOT_EVALUATED_CNT > 0
                THEN ''ROWCOUNT_NOT_EVALUATED''
            ELSE NULL
        END AS BLOCK_REASON,
        c.ALL_EXPECTED_SOURCES_PRESENT,
        a.SEQUENCE_VALID,
        c.REQUIRED_SOURCE_PKG_COUNT,
        c.FOUND_SOURCE_PKG_COUNT,
        a.EXPECTED_FILE_CNT,
        a.RECEIVED_VALID_CNT,
        a.RECEIVED_PHYSICAL_CNT,
        a.MISSING_FILE_CNT,
        a.ROWCOUNT_FAIL_CNT,
        a.ROWCOUNT_NOT_EVALUATED_CNT,
        a.SEQUENCE_MISSING_VALUE_CNT,
        a.OUT_OF_SEQUENCE_CNT,
        a.RECEIVED_SEQUENCE_MISMATCH_CNT,
        c.EXPECTED_SOURCE_PKGS,
        c.FOUND_SOURCE_PKGS,
        COALESCE(m.MISSING_SOURCE_PKGS, ARRAY_CONSTRUCT()) AS MISSING_SOURCE_PKGS,
        COALESCE(sd.SEQUENCE_DETAILS, ARRAY_CONSTRUCT()) AS SEQUENCE_DETAILS,
        COALESCE(o.OUT_OF_SEQUENCE_FILES, ARRAY_CONSTRUCT()) AS OUT_OF_SEQUENCE_FILES,
        COALESCE(f.FAILING_FILES, ARRAY_CONSTRUCT()) AS FAILING_FILES
    FROM TMP_CANDIDATE_DATES c
    INNER JOIN TMP_FILE_STATUS_AGG a
        ON a.PKG_GROUP_NAME = c.PKG_GROUP_NAME
       AND a.DLVY_END_DT = c.DLVY_END_DT
    LEFT JOIN TMP_MISSING_SOURCE_PKGS m
        ON m.PKG_GROUP_NAME = c.PKG_GROUP_NAME
       AND m.DLVY_END_DT = c.DLVY_END_DT
    LEFT JOIN TMP_SEQUENCE_DETAILS sd
        ON sd.PKG_GROUP_NAME = c.PKG_GROUP_NAME
       AND sd.DLVY_END_DT = c.DLVY_END_DT
    LEFT JOIN TMP_OUT_OF_SEQUENCE_FILES o
        ON o.PKG_GROUP_NAME = c.PKG_GROUP_NAME
       AND o.DLVY_END_DT = c.DLVY_END_DT
    LEFT JOIN TMP_FAILING_FILES f
        ON f.PKG_GROUP_NAME = c.PKG_GROUP_NAME
       AND f.DLVY_END_DT = c.DLVY_END_DT;

    -----------------------------------------------------------------------------
    -- 11. Log evaluation result only. No status table. No RAW update.
    -----------------------------------------------------------------------------
    v_step := ''LOG_COMPLETE'';

    INSERT INTO IDENTIFIER(:v_log_table)
    SELECT
        :v_run_id,
        CURRENT_TIMESTAMP(),
        :v_subject,
        NULL,
        :PKG_GROUP_NAME,
        ''EVALUATE'',
        NULL,
        NULL,
        ''EVALUATION_COMPLETE'',
        (SELECT COUNT(*) FROM TMP_DECISION),
        OBJECT_CONSTRUCT(
            ''decision_rows'', (SELECT COUNT(*) FROM TMP_DECISION),
            ''ready_rows'', (SELECT COUNT(*) FROM TMP_DECISION WHERE READY_TO_PROCESS = TRUE),
            ''blocked_rows'', (SELECT COUNT(*) FROM TMP_DECISION WHERE READY_TO_PROCESS = FALSE),
            ''sequence_blocked_rows'', (SELECT COUNT(*) FROM TMP_DECISION WHERE SEQUENCE_VALID = FALSE),
            ''require_common_enddts'', :REQUIRE_COMMON_ENDDTS,
            ''status_table_used'', FALSE,
            ''raw_update_performed'', FALSE,
            ''exclude_ready_control_files'', TRUE,
            ''sequence_validation'', TRUE,
            ''sequence_source_table'', :v_results_table
        );

    v_result := (
        SELECT
            RUN_ID::STRING AS RUN_ID,
            PKG_GROUP_NAME::STRING AS PKG_GROUP_NAME,
            DLVY_SUBJECT_AREA_NM::STRING AS DLVY_SUBJECT_AREA_NM,
            DLVY_END_DT::DATE AS DLVY_END_DT,
            READY_TO_PROCESS::BOOLEAN AS READY_TO_PROCESS,
            BLOCK_REASON::STRING AS BLOCK_REASON,
            ALL_EXPECTED_SOURCES_PRESENT::BOOLEAN AS ALL_EXPECTED_SOURCES_PRESENT,
            SEQUENCE_VALID::BOOLEAN AS SEQUENCE_VALID,
            REQUIRED_SOURCE_PKG_COUNT::NUMBER AS REQUIRED_SOURCE_PKG_COUNT,
            FOUND_SOURCE_PKG_COUNT::NUMBER AS FOUND_SOURCE_PKG_COUNT,
            EXPECTED_FILE_CNT::NUMBER AS EXPECTED_FILE_CNT,
            RECEIVED_VALID_CNT::NUMBER AS RECEIVED_VALID_CNT,
            RECEIVED_PHYSICAL_CNT::NUMBER AS RECEIVED_PHYSICAL_CNT,
            MISSING_FILE_CNT::NUMBER AS MISSING_FILE_CNT,
            ROWCOUNT_FAIL_CNT::NUMBER AS ROWCOUNT_FAIL_CNT,
            ROWCOUNT_NOT_EVALUATED_CNT::NUMBER AS ROWCOUNT_NOT_EVALUATED_CNT,
            SEQUENCE_MISSING_VALUE_CNT::NUMBER AS SEQUENCE_MISSING_VALUE_CNT,
            OUT_OF_SEQUENCE_CNT::NUMBER AS OUT_OF_SEQUENCE_CNT,
            RECEIVED_SEQUENCE_MISMATCH_CNT::NUMBER AS RECEIVED_SEQUENCE_MISMATCH_CNT,
            EXPECTED_SOURCE_PKGS::VARIANT AS EXPECTED_SOURCE_PKGS,
            FOUND_SOURCE_PKGS::VARIANT AS FOUND_SOURCE_PKGS,
            MISSING_SOURCE_PKGS::VARIANT AS MISSING_SOURCE_PKGS,
            SEQUENCE_DETAILS::VARIANT AS SEQUENCE_DETAILS,
            OUT_OF_SEQUENCE_FILES::VARIANT AS OUT_OF_SEQUENCE_FILES,
            FAILING_FILES::VARIANT AS FAILING_FILES
        FROM TMP_DECISION
        ORDER BY PKG_GROUP_NAME, DLVY_END_DT
    );

    RETURN TABLE(v_result);

EXCEPTION
    WHEN OTHER THEN
        INSERT INTO IDENTIFIER(:v_log_table)
        SELECT
            :v_run_id,
            CURRENT_TIMESTAMP(),
            :v_subject,
            NULL,
            :PKG_GROUP_NAME,
            ''ERROR'',
            NULL,
            NULL,
            ''EXCEPTION'',
            0,
            OBJECT_CONSTRUCT(
                ''failed_step'', :v_step,
                ''status_table_used'', FALSE,
                ''raw_update_performed'', FALSE,
                ''exclude_ready_control_files'', TRUE,
                ''sequence_validation'', TRUE,
                ''sequence_source_table'', :v_results_table
            );

        v_result := (
            SELECT
                :v_run_id::STRING AS RUN_ID,
                :PKG_GROUP_NAME::STRING AS PKG_GROUP_NAME,
                :v_subject::STRING AS DLVY_SUBJECT_AREA_NM,
                NULL::DATE AS DLVY_END_DT,
                FALSE::BOOLEAN AS READY_TO_PROCESS,
                (''EXCEPTION_AT_'' || :v_step)::STRING AS BLOCK_REASON,
                FALSE::BOOLEAN AS ALL_EXPECTED_SOURCES_PRESENT,
                FALSE::BOOLEAN AS SEQUENCE_VALID,
                NULL::NUMBER AS REQUIRED_SOURCE_PKG_COUNT,
                NULL::NUMBER AS FOUND_SOURCE_PKG_COUNT,
                NULL::NUMBER AS EXPECTED_FILE_CNT,
                NULL::NUMBER AS RECEIVED_VALID_CNT,
                NULL::NUMBER AS RECEIVED_PHYSICAL_CNT,
                NULL::NUMBER AS MISSING_FILE_CNT,
                NULL::NUMBER AS ROWCOUNT_FAIL_CNT,
                NULL::NUMBER AS ROWCOUNT_NOT_EVALUATED_CNT,
                NULL::NUMBER AS SEQUENCE_MISSING_VALUE_CNT,
                NULL::NUMBER AS OUT_OF_SEQUENCE_CNT,
                NULL::NUMBER AS RECEIVED_SEQUENCE_MISMATCH_CNT,
                ARRAY_CONSTRUCT()::VARIANT AS EXPECTED_SOURCE_PKGS,
                ARRAY_CONSTRUCT()::VARIANT AS FOUND_SOURCE_PKGS,
                ARRAY_CONSTRUCT()::VARIANT AS MISSING_SOURCE_PKGS,
                ARRAY_CONSTRUCT()::VARIANT AS SEQUENCE_DETAILS,
                ARRAY_CONSTRUCT()::VARIANT AS OUT_OF_SEQUENCE_FILES,
                ARRAY_CONSTRUCT()::VARIANT AS FAILING_FILES
        );

        RETURN TABLE(v_result);
END;
';
