-- Source: KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_TST";
USE SCHEMA "KUMO_TST"."META";

CREATE OR REPLACE PROCEDURE KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE("P_LIM_FORMAT" VARCHAR, "P_FROM_DLVY_END_DATE" DATE, "P_TO_DLVY_END_DATE" DATE, "P_RELOAD" BOOLEAN, "P_RESET_DLVY_PKG_CHECK" BOOLEAN, "P_SET_READY_TO_LOAD" BOOLEAN)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE

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

    V_DB := CURRENT_DATABASE();
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
      FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = ''RAW_LIM'' AND TABLE_NAME = :V_TARGET_TABLE_SHORT;

    IF (V_TABLE_EXISTS = 0) THEN
        RAISE EX_TARGET_NOT_FOUND;
    END IF;

    -- 6. Temp tables
    CREATE OR REPLACE TEMPORARY TABLE TMP_RAW_LIM_FILES
    (FILE_PATH VARCHAR, FILE_NM VARCHAR, DLVY_END_DATE DATE);

    CREATE OR REPLACE TEMPORARY TABLE TMP_RAW_LIM_RELOAD_PACKAGES
    (PKG_GROUP_NAME VARCHAR, DLVY_END_DATE DATE, DLVY_SOURCE_ID VARCHAR,
     DLVY_SUBJECT_AREA_ID VARCHAR, DLVY_PKG_ID VARCHAR,
     DLVY_PKG_YEAR VARCHAR, DLVY_PKG_YEAR_SEQ_NO VARCHAR);

    -- 7. List files
    V_LIST_PATTERN := ''^(?!.*(?i:deleted_)).*'' || V_LIM_FORMAT || ''[^/]*$'';
    V_LIST_SQL := ''LIST '' || V_STAGE_REF || '' PATTERN = '''''' || V_LIST_PATTERN || '''''''';
    EXECUTE IMMEDIATE :V_LIST_SQL;
    V_LIST_QUERY_ID := SQLID;

    -- 8. Parse dates
    INSERT INTO TMP_RAW_LIM_FILES (FILE_PATH, FILE_NM, DLVY_END_DATE)
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
    DELETE FROM TMP_RAW_LIM_FILES WHERE DLVY_END_DATE IS NULL;
    V_INVALID_DATES := SQLROWCOUNT;

    -- 10. Date filter (partial only)
    IF (V_RELOAD AND NOT V_FULL_RELOAD) THEN
        DELETE FROM TMP_RAW_LIM_FILES
         WHERE DLVY_END_DATE < :P_FROM_DLVY_END_DATE
            OR DLVY_END_DATE > :P_TO_DLVY_END_DATE;
    END IF;

    SELECT COUNT(*) INTO :V_FILES_SELECTED FROM TMP_RAW_LIM_FILES;

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
        INSERT INTO TMP_RAW_LIM_RELOAD_PACKAGES
        SELECT DISTINCT
              C.PKG_GROUP_NAME, F.DLVY_END_DATE, R.DLVY_SOURCE_ID,
              R.DLVY_SUBJECT_AREA_ID, R.DLVY_PKG_ID, R.DLVY_PKG_YEAR,
              R.DLVY_PKG_YEAR_SEQ_NO
          FROM RAW_LIM.RAW_LOAD_RESULTS R
          INNER JOIN TMP_RAW_LIM_FILES F ON R.FILE_NM = F.FILE_NM
          LEFT JOIN KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE C
             ON UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
            AND C.SOURCE_ID IS NOT DISTINCT FROM R.DLVY_SOURCE_ID
            AND C.PKG_ID IS NOT DISTINCT FROM R.DLVY_PKG_ID
            AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE;
        V_PACKAGES_CAPTURED := SQLROWCOUNT;

        -- 12b. Delete _TMP_SET_READY_LOG (config-driven)
        DELETE FROM RAW_LIM._TMP_SET_READY_LOG T
         USING KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE C
         WHERE UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
           AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE
           AND T.PKG_GROUP_NAME IS NOT DISTINCT FROM C.PKG_GROUP_NAME
           AND T.DLVY_SOURCE_ID IS NOT DISTINCT FROM C.SOURCE_ID
           AND T.DLVY_PKG_ID IS NOT DISTINCT FROM C.PKG_ID
           AND (:V_FULL_RELOAD OR T.DLVY_END_DATE IN (SELECT DLVY_END_DATE FROM TMP_RAW_LIM_FILES));
        V_TMP_SET_READY_LOG_DELETED := SQLROWCOUNT;

        -- 12c. Delete _SET_READY_LOG (config-driven)
        DELETE FROM RAW_LIM._SET_READY_LOG T
         USING KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE C
         WHERE UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
           AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE
           AND T.PKG_GROUP_NAME IS NOT DISTINCT FROM C.PKG_GROUP_NAME
           AND T.DLVY_SOURCE_ID IS NOT DISTINCT FROM C.SOURCE_ID
           AND T.DLVY_PKG_ID IS NOT DISTINCT FROM C.PKG_ID
           AND (:V_FULL_RELOAD OR T.DLVY_END_DATE IN (SELECT DLVY_END_DATE FROM TMP_RAW_LIM_FILES));
        V_SET_READY_LOG_DELETED := SQLROWCOUNT;

        -- RAW_LIM_FULL_RELOAD_BATCH_CTRL_V1
        -- Clear all dates/statuses for this RAW subject, including qualified
        -- DW_TABLE_NM values retained by earlier batch registration versions.
        IF (V_FULL_RELOAD) THEN
            DELETE FROM META.LIM_BATCH_CTRL
             WHERE SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -2) = ''RAW_LIM''
               AND SPLIT_PART(UPPER(DW_TABLE_NM), ''.'', -1) = :V_TARGET_TABLE_SHORT;
            V_LIM_BATCH_CTRL_DELETED := SQLROWCOUNT;
        END IF;

        -- 12d. Delete RAW_LOAD_RESULTS
        IF (V_FULL_RELOAD) THEN
            DELETE FROM RAW_LIM.RAW_LOAD_RESULTS
             WHERE TABLE_NAME = :V_TARGET_TABLE_SHORT;
        ELSE
            DELETE FROM RAW_LIM.RAW_LOAD_RESULTS
             WHERE FILE_NM IN (SELECT FILE_NM FROM TMP_RAW_LIM_FILES);
        END IF;
        V_LOAD_RESULTS_DELETED := SQLROWCOUNT;

        -- 12f. Delete RAW_LIM_META
        DELETE FROM RAW_LIM.RAW_LIM_META
         WHERE DLVY_SUBJECT_AREA_ID = :V_LIM_FORMAT
           AND (:V_FULL_RELOAD OR DLVY_END_DATE IN (SELECT DLVY_END_DATE FROM TMP_RAW_LIM_FILES));
        V_RAW_LIM_META_DELETED := SQLROWCOUNT;

        -- 12e. Clean RAW target
        IF (V_FULL_RELOAD) THEN
            SELECT COUNT(*) INTO :V_RAW_ROWS_DELETED FROM IDENTIFIER(:V_TARGET_TABLE);
            TRUNCATE TABLE IDENTIFIER(:V_TARGET_TABLE);
        ELSE
            DELETE FROM IDENTIFIER(:V_TARGET_TABLE)
             WHERE DW_FILE_NM IN (SELECT FILE_NM FROM TMP_RAW_LIM_FILES);
            V_RAW_ROWS_DELETED := SQLROWCOUNT;
        END IF;

        -- 12g. Clone and drop SDL tables
        LET V_TODAY_STR VARCHAR := TO_CHAR(CURRENT_DATE(), ''YYYYMMDD'');
        LET V_CLONE_NAME VARCHAR;
        LET V_CLONE_SEQ NUMBER;
        LET V_TBL_NAME VARCHAR;
        LET C_SDL RESULTSET;

        C_SDL := (SELECT TABLE_NAME
                    FROM INFORMATION_SCHEMA.TABLES
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
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ''SDL''
              AND TABLE_NAME LIKE ''X_'' || :V_TODAY_STR || ''_'' || :V_TBL_NAME || ''_%'';

            V_CLONE_NAME := ''X_'' || V_TODAY_STR || ''_'' || V_TBL_NAME || ''_'' || V_CLONE_SEQ::VARCHAR;

            -- Clone the table
            EXECUTE IMMEDIATE ''CREATE TABLE SDL.'' || :V_CLONE_NAME || '' CLONE SDL.'' || :V_TBL_NAME;
            V_SDL_TABLES_CLONED := V_SDL_TABLES_CLONED + 1;

            -- Drop the original
            EXECUTE IMMEDIATE ''DROP TABLE IF EXISTS SDL.'' || :V_TBL_NAME;
            V_SDL_TABLES_DROPPED := V_SDL_TABLES_DROPPED + 1;
        END FOR;
        CLOSE C_SDL_CUR;

    END IF;

    -- 13. Reset sequence control
    IF (V_RESET_DLVY_PKG_CHECK OR V_FULL_RELOAD) THEN
        UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE
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
                FROM TMP_RAW_LIM_FILES
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

    -- RAW_LIM_SET_READY_SELECTED_FILES_V1
    IF (V_SET_READY_TO_LOAD) THEN
        UPDATE IDENTIFIER(:V_TARGET_TABLE)
           SET DW_READY_TO_LOAD_FL = TRUE
         WHERE DW_FILE_NM IN (SELECT FILE_NM FROM TMP_RAW_LIM_FILES)
           AND COALESCE(DW_READY_TO_LOAD_FL, FALSE) = FALSE;
        V_ROWS_READY_UPDATED := SQLROWCOUNT;
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
