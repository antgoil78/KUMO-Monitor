-- Source: KUMO_TST.META._LOAD_RAW_LIM_FROM_STAGE_BAK(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_TST";
USE SCHEMA "KUMO_TST"."META";

CREATE OR REPLACE PROCEDURE KUMO_TST.META._LOAD_RAW_LIM_FROM_STAGE_BAK("P_LIM_FORMAT" VARCHAR, "P_FROM_DLVY_END_DATE" DATE, "P_TO_DLVY_END_DATE" DATE, "P_RELOAD" BOOLEAN, "P_RESET_DLVY_PKG_CHECK" BOOLEAN, "P_SET_READY_TO_LOAD" BOOLEAN)
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
    V_RAW_ROWS_DELETED           NUMBER := 0;
    V_PKG_CONTROL_RESET          NUMBER := 0;
    V_COPY_COMMANDS        NUMBER := 0;
    V_ROWS_READY_UPDATED   NUMBER := 0;

    V_COPY_SQL         VARCHAR;
    V_FORCE_FLAG       VARCHAR;

    EX_INVALID_LIM_FORMAT   EXCEPTION (-20001, ''Invalid P_LIM_FORMAT'');
    EX_INVALID_DATE_RANGE   EXCEPTION (-20002, ''Invalid date range'');
    EX_TARGET_NOT_FOUND     EXCEPTION (-20003, ''Target RAW table not found'');

BEGIN

    -- ================================================================
    -- 1. Initialise variables
    -- ================================================================

    V_DB :=
        CURRENT_DATABASE();

    V_LIM_FORMAT :=
        UPPER(TRIM(P_LIM_FORMAT));

    V_RELOAD :=
        COALESCE(P_RELOAD, FALSE);

    V_RESET_DLVY_PKG_CHECK :=
        COALESCE(P_RESET_DLVY_PKG_CHECK, FALSE);

    V_SET_READY_TO_LOAD :=
        COALESCE(P_SET_READY_TO_LOAD, FALSE);


    -- ================================================================
    -- 2. Validate parameters
    -- ================================================================

    IF (
        V_LIM_FORMAT IS NULL
        OR NOT REGEXP_LIKE(
            V_LIM_FORMAT,
            ''^[A-Z0-9_]+$''
        )
    )
    THEN
        RAISE EX_INVALID_LIM_FORMAT;
    END IF;


    IF (
           (
               P_FROM_DLVY_END_DATE IS NULL
               AND P_TO_DLVY_END_DATE IS NOT NULL
           )
        OR (
               P_FROM_DLVY_END_DATE IS NOT NULL
               AND P_TO_DLVY_END_DATE IS NULL
           )
        OR (
               P_FROM_DLVY_END_DATE IS NOT NULL
               AND P_FROM_DLVY_END_DATE > P_TO_DLVY_END_DATE
           )
    )
    THEN
        RAISE EX_INVALID_DATE_RANGE;
    END IF;


    -- ================================================================
    -- 3. Determine load mode
    -- ================================================================

    V_FULL_RELOAD :=
           V_RELOAD
       AND P_FROM_DLVY_END_DATE IS NULL
       AND P_TO_DLVY_END_DATE IS NULL;

    IF (V_FULL_RELOAD)
    THEN
        V_LOAD_MODE := ''FULL_RELOAD'';
    ELSEIF (V_RELOAD)
    THEN
        V_LOAD_MODE := ''PARTIAL_RELOAD'';
    ELSE
        V_LOAD_MODE := ''NORMAL_LOAD'';
    END IF;


    -- ================================================================
    -- 4. Resolve object names
    -- ================================================================

    V_TARGET_TABLE_SHORT :=
        ''RAW_LIM_'' || V_LIM_FORMAT;

    V_TARGET_TABLE :=
          V_DB
        || ''.RAW_LIM.''
        || V_TARGET_TABLE_SHORT;

    V_STAGE_REF :=
          ''@''
        || V_DB
        || ''.META.AZURE_LIM_STAGE'';

    V_FILE_FORMAT :=
          V_DB
        || ''.META.KUMO_LIM'';


    -- ================================================================
    -- 5. Verify target RAW table exists
    -- ================================================================

    SELECT COUNT(*)
      INTO :V_TABLE_EXISTS
      FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = ''RAW_LIM''
       AND TABLE_NAME   = :V_TARGET_TABLE_SHORT;

    IF (V_TABLE_EXISTS = 0)
    THEN
        RAISE EX_TARGET_NOT_FOUND;
    END IF;


    -- ================================================================
    -- 6. Create temporary working tables
    -- ================================================================

    CREATE OR REPLACE TEMPORARY TABLE TMP_RAW_LIM_FILES
    (
          FILE_PATH        VARCHAR
        , FILE_NM          VARCHAR
        , DLVY_END_DATE    DATE
    );

    CREATE OR REPLACE TEMPORARY TABLE TMP_RAW_LIM_RELOAD_PACKAGES
    (
          PKG_GROUP_NAME         VARCHAR
        , DLVY_END_DATE          DATE
        , DLVY_SOURCE_ID         VARCHAR
        , DLVY_SUBJECT_AREA_ID   VARCHAR
        , DLVY_PKG_ID            VARCHAR
        , DLVY_PKG_YEAR          VARCHAR
        , DLVY_PKG_YEAR_SEQ_NO   VARCHAR
    );


    -- ================================================================
    -- 7. List stage files belonging to requested LIM format
    -- ================================================================

    V_LIST_PATTERN :=
          ''.*''
        || V_LIM_FORMAT
        || ''[^/]*$'';

    V_LIST_SQL :=
          ''LIST ''
        || V_STAGE_REF
        || '' PATTERN = ''''''
        || V_LIST_PATTERN
        || '''''''';

    EXECUTE IMMEDIATE :V_LIST_SQL;

    V_LIST_QUERY_ID := SQLID;


    -- ================================================================
    -- 8. Parse DLVY_END_DATE from filename
    -- ================================================================

    INSERT INTO TMP_RAW_LIM_FILES
    (
          FILE_PATH
        , FILE_NM
        , DLVY_END_DATE
    )
    SELECT
          "name"                         AS FILE_PATH

        , SPLIT_PART(
              "name",
              ''/'',
              -1
          )                              AS FILE_NM

        , TRY_TO_DATE(
              SPLIT_PART(
                  SPLIT_PART("name", ''/'', -1),
                  ''_'',
                  -2
              ),
              ''YYYYMMDD''
          )                              AS DLVY_END_DATE

      FROM TABLE(RESULT_SCAN(:V_LIST_QUERY_ID));


    -- ================================================================
    -- 9. Remove files with invalid date parse
    -- ================================================================

    DELETE
      FROM TMP_RAW_LIM_FILES
     WHERE DLVY_END_DATE IS NULL;

    V_INVALID_DATES := SQLROWCOUNT;


    -- ================================================================
    -- 10. Apply date filter (partial reload only)
    -- ================================================================

    IF (
           V_RELOAD
       AND NOT V_FULL_RELOAD
    )
    THEN
        DELETE
          FROM TMP_RAW_LIM_FILES
         WHERE DLVY_END_DATE < :P_FROM_DLVY_END_DATE
            OR DLVY_END_DATE > :P_TO_DLVY_END_DATE;
    END IF;

    SELECT COUNT(*)
      INTO :V_FILES_SELECTED
      FROM TMP_RAW_LIM_FILES;


    -- ================================================================
    -- 10b. Early exit if no files
    -- ================================================================

    IF (V_FILES_SELECTED = 0)
    THEN
        RETURN OBJECT_CONSTRUCT(
              ''STATUS'',        ''NO_FILES_FOUND''
            , ''DATABASE'',      V_DB
            , ''LOAD_MODE'',     V_LOAD_MODE
            , ''LIM_FORMAT'',    V_LIM_FORMAT
            , ''TARGET_TABLE'',  V_TARGET_TABLE
            , ''FROM_DLVY_END_DATE'', P_FROM_DLVY_END_DATE
            , ''TO_DLVY_END_DATE'',   P_TO_DLVY_END_DATE
            , ''FILES_SELECTED'', 0
        );
    END IF;


    -- ================================================================
    -- 11. Begin transaction
    -- ================================================================

    BEGIN TRANSACTION;


    -- ================================================================
    -- 12. Reload cleanup
    -- ================================================================

    IF (V_RELOAD)
    THEN

        -- ------------------------------------------------------------
        -- 12a. Capture package information
        -- ------------------------------------------------------------

        INSERT INTO TMP_RAW_LIM_RELOAD_PACKAGES
        (
              PKG_GROUP_NAME
            , DLVY_END_DATE
            , DLVY_SOURCE_ID
            , DLVY_SUBJECT_AREA_ID
            , DLVY_PKG_ID
            , DLVY_PKG_YEAR
            , DLVY_PKG_YEAR_SEQ_NO
        )
        SELECT DISTINCT
              C.PKG_GROUP_NAME
            , F.DLVY_END_DATE
            , R.DLVY_SOURCE_ID
            , R.DLVY_SUBJECT_AREA_ID
            , R.DLVY_PKG_ID
            , R.DLVY_PKG_YEAR
            , R.DLVY_PKG_YEAR_SEQ_NO
          FROM RAW_LIM.RAW_LOAD_RESULTS R
          INNER JOIN TMP_RAW_LIM_FILES F
             ON R.FILE_NM = F.FILE_NM
          LEFT JOIN KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE C
             ON UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
            AND C.SOURCE_ID IS NOT DISTINCT FROM R.DLVY_SOURCE_ID
            AND C.PKG_ID IS NOT DISTINCT FROM R.DLVY_PKG_ID
            AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE
        ;

        V_PACKAGES_CAPTURED := SQLROWCOUNT;


        -- ------------------------------------------------------------
        -- 12b. Delete _TMP_SET_READY_LOG (config-driven)
        -- ------------------------------------------------------------

        DELETE
          FROM RAW_LIM._TMP_SET_READY_LOG T
         USING KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE C
         WHERE UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
           AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE
           AND T.PKG_GROUP_NAME IS NOT DISTINCT FROM C.PKG_GROUP_NAME
           AND T.DLVY_SOURCE_ID IS NOT DISTINCT FROM C.SOURCE_ID
           AND T.DLVY_PKG_ID IS NOT DISTINCT FROM C.PKG_ID
           AND (
                  :V_FULL_RELOAD
               OR T.DLVY_END_DATE IN (
                      SELECT DLVY_END_DATE FROM TMP_RAW_LIM_FILES
                  )
           );

        V_TMP_SET_READY_LOG_DELETED := SQLROWCOUNT;


        -- ------------------------------------------------------------
        -- 12c. Delete _SET_READY_LOG (config-driven)
        -- ------------------------------------------------------------

        DELETE
          FROM RAW_LIM._SET_READY_LOG T
         USING KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE C
         WHERE UPPER(C.SUBJECT_AREA) = :V_LIM_FORMAT
           AND COALESCE(C.ACTIVE_FL, TRUE) = TRUE
           AND T.PKG_GROUP_NAME IS NOT DISTINCT FROM C.PKG_GROUP_NAME
           AND T.DLVY_SOURCE_ID IS NOT DISTINCT FROM C.SOURCE_ID
           AND T.DLVY_PKG_ID IS NOT DISTINCT FROM C.PKG_ID
           AND (
                  :V_FULL_RELOAD
               OR T.DLVY_END_DATE IN (
                      SELECT DLVY_END_DATE FROM TMP_RAW_LIM_FILES
                  )
           );

        V_SET_READY_LOG_DELETED := SQLROWCOUNT;


        -- ------------------------------------------------------------
        -- 12d. Delete previous RAW_LOAD_RESULTS
        -- ------------------------------------------------------------

        DELETE
          FROM RAW_LIM.RAW_LOAD_RESULTS
         WHERE FILE_NM IN (
             SELECT FILE_NM FROM TMP_RAW_LIM_FILES
         );

        V_LOAD_RESULTS_DELETED := SQLROWCOUNT;


        -- ------------------------------------------------------------
        -- 12f. Delete RAW_LIM_META for subject area
        -- ------------------------------------------------------------

        DELETE
          FROM RAW_LIM.RAW_LIM_META
         WHERE DLVY_SUBJECT_AREA_ID = :V_LIM_FORMAT
           AND (
                  :V_FULL_RELOAD
               OR DLVY_END_DATE IN (
                      SELECT DLVY_END_DATE FROM TMP_RAW_LIM_FILES
                  )
           );

        V_RAW_LIM_META_DELETED := SQLROWCOUNT;


        -- ------------------------------------------------------------
        -- 12e. Clean RAW target
        -- ------------------------------------------------------------

        IF (V_FULL_RELOAD)
        THEN
            SELECT COUNT(*)
              INTO :V_RAW_ROWS_DELETED
              FROM IDENTIFIER(:V_TARGET_TABLE);

            TRUNCATE TABLE IDENTIFIER(:V_TARGET_TABLE);
        ELSE
            DELETE
              FROM IDENTIFIER(:V_TARGET_TABLE)
             WHERE DW_FILE_NM IN (
                 SELECT FILE_NM FROM TMP_RAW_LIM_FILES
             );

            V_RAW_ROWS_DELETED := SQLROWCOUNT;
        END IF;

    END IF;


    -- ================================================================
    -- 13. Reset delivery-package sequence control
    -- ================================================================

    IF (
           V_RESET_DLVY_PKG_CHECK
        OR V_FULL_RELOAD
    )
    THEN
        UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE
           SET DLVY_PKG_YEAR        = NULL,
               DLVY_PKG_YEAR_SEQ_NO = NULL,
               UPDATED_AT           = CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
         WHERE UPPER(SUBJECT_AREA) = :V_LIM_FORMAT
           AND COALESCE(ACTIVE_FL, TRUE) = TRUE;

        V_PKG_CONTROL_RESET := SQLROWCOUNT;
    END IF;


    -- ================================================================
    -- 14. Determine COPY FORCE behavior
    -- ================================================================

    IF (
           V_RELOAD
       AND NOT V_FULL_RELOAD
    )
    THEN
        V_FORCE_FLAG := '' FORCE = TRUE'';
    ELSE
        V_FORCE_FLAG := '''';
    END IF;


    -- ================================================================
    -- 15. Execute COPY INTO (transform with METADATA$ columns)
    -- ================================================================

    V_COPY_SQL :=
          ''COPY INTO ''
        || V_TARGET_TABLE
        || '' (''
        || ''    DW_FILE_NM''
        || ''  , DW_FILE_DTTM''
        || ''  , DW_FILE_CHECK_SUM''
        || ''  , DW_FILE_ROW_NUMBER''
        || ''  , DW_LOAD_DTTM''
        || ''  , DATA''
        || '' )''
        || '' FROM (''
        || ''   SELECT''
        || ''       SPLIT_PART(METADATA$FILENAME, ''''/'''', -1)''
        || ''     , METADATA$FILE_LAST_MODIFIED''
        || ''     , METADATA$FILE_CONTENT_KEY''
        || ''     , METADATA$FILE_ROW_NUMBER''
        || ''     , SYSDATE()''
        || ''     , $1''
        || ''   FROM '' || V_STAGE_REF
        || '' )''
        || '' FILE_FORMAT = (FORMAT_NAME = ''''''
        || V_FILE_FORMAT
        || '''''')''
        || '' PATTERN = ''''''
        || V_LIST_PATTERN
        || ''''''''
        || V_FORCE_FLAG
        || '' ON_ERROR = CONTINUE'';

    EXECUTE IMMEDIATE :V_COPY_SQL;

    V_COPY_COMMANDS := 1;


    -- ================================================================
    -- 16. Record load results
    -- ================================================================

    INSERT INTO RAW_LIM.RAW_LOAD_RESULTS
    (
          TABLE_NAME
        , FILE_NM
        , STATUS
        , ROWS_PARSED
        , ROWS_LOADED
        , ERROR_LIMIT
        , ERRORS_SEEN
        , FIRST_ERROR
        , FIRST_ERROR_LINE
        , FIRST_ERROR_CHARACTER
        , FIRST_ERROR_COLUMN_NAME
        , DLVY_SOURCE_ID
        , DLVY_SUBJECT_AREA_ID
        , DLVY_LIM_OBJ_SEQ_NO
        , DLVY_LIM_OBJ_VER_NO
        , DLVY_PKG_ID
        , DLVY_PKG_YEAR
        , DLVY_PKG_YEAR_SEQ_NO
        , DLVY_END_DATE
        , FOOTER_EXPECTED_ROWS
        , ACTUAL_DATA_ROWS
        , ROWCOUNT_MATCH
        , LOADED_AT
    )
    SELECT
          :V_TARGET_TABLE_SHORT

        , SPLIT_PART("file", ''/'', -1)

        , "status"

        , "rows_parsed"

        , "rows_loaded"

        , "error_limit"

        , "errors_seen"

        , "first_error"

        , "first_error_line"

        , "first_error_character"

        , "first_error_col_name"

        , LEFT(
              SPLIT_PART("file", ''/'', -1),
              3
          )                              -- DLVY_SOURCE_ID

        , SUBSTR(
              SPLIT_PART("file", ''/'', -1),
              4,
              4
          )                              -- DLVY_SUBJECT_AREA_ID

        , SPLIT_PART(
              SPLIT_PART("file", ''/'', -1),
              ''_'',
              2
          )                              -- DLVY_LIM_OBJ_SEQ_NO

        , SPLIT_PART(
              SPLIT_PART("file", ''/'', -1),
              ''_'',
              3
          )                              -- DLVY_LIM_OBJ_VER_NO

        , SPLIT_PART(
              SPLIT_PART("file", ''/'', -1),
              ''_'',
              4
          )                              -- DLVY_PKG_ID

        , LEFT(
              SPLIT_PART(
                  SPLIT_PART("file", ''/'', -1),
                  ''_'',
                  5
              ),
              4
          )                              -- DLVY_PKG_YEAR

        , RIGHT(
              SPLIT_PART(
                  SPLIT_PART("file", ''/'', -1),
                  ''_'',
                  5
              ),
              5
          )                              -- DLVY_PKG_YEAR_SEQ_NO

        , TRY_TO_DATE(
              SPLIT_PART(
                  SPLIT_PART("file", ''/'', -1),
                  ''_'',
                  -2
              ),
              ''YYYYMMDD''
          )                              -- DLVY_END_DATE

        , NULL                           -- FOOTER_EXPECTED_ROWS
        , NULL                           -- ACTUAL_DATA_ROWS
        , NULL                           -- ROWCOUNT_MATCH

        , CURRENT_TIMESTAMP()::TIMESTAMP_NTZ

      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));


    -- ================================================================
    -- 17. SET_READY_TO_LOAD (optional)
    -- ================================================================

    IF (V_SET_READY_TO_LOAD)
    THEN
        CALL META.SET_READY_TO_LOAD(:V_LIM_FORMAT);
        V_ROWS_READY_UPDATED := SQLROWCOUNT;
    END IF;


    -- ================================================================
    -- 18. Commit
    -- ================================================================

    COMMIT;


    -- ================================================================
    -- 19. Return summary
    -- ================================================================

    RETURN OBJECT_CONSTRUCT(
          ''STATUS'',          ''SUCCESS''
        , ''DATABASE'',        V_DB
        , ''LOAD_MODE'',       V_LOAD_MODE
        , ''LIM_FORMAT'',      V_LIM_FORMAT
        , ''TARGET_TABLE'',    V_TARGET_TABLE
        , ''FROM_DLVY_END_DATE'', P_FROM_DLVY_END_DATE
        , ''TO_DLVY_END_DATE'',   P_TO_DLVY_END_DATE
        , ''FILES_SELECTED'',  V_FILES_SELECTED
        , ''INVALID_FILE_DATES'', V_INVALID_DATES
        , ''PACKAGES_CAPTURED'',  V_PACKAGES_CAPTURED
        , ''TMP_SET_READY_LOG_DELETED'', V_TMP_SET_READY_LOG_DELETED
        , ''SET_READY_LOG_DELETED'',     V_SET_READY_LOG_DELETED
        , ''RAW_LOAD_RESULTS_DELETED'',  V_LOAD_RESULTS_DELETED
        , ''RAW_LIM_META_DELETED'',      V_RAW_LIM_META_DELETED
        , ''RAW_ROWS_DELETED'',          V_RAW_ROWS_DELETED
        , ''PKG_CONTROL_ROWS_RESET'',    V_PKG_CONTROL_RESET
        , ''COPY_COMMANDS_EXECUTED'',     V_COPY_COMMANDS
        , ''READY_ROWS_UPDATED'',        V_ROWS_READY_UPDATED
        , ''RELOAD'',                V_RELOAD
        , ''RESET_DLVY_PKG_CHECK'',  V_RESET_DLVY_PKG_CHECK
        , ''SET_READY_TO_LOAD'',     V_SET_READY_TO_LOAD
    );


EXCEPTION

    WHEN OTHER THEN
        IF (CURRENT_TRANSACTION() IS NOT NULL)
        THEN
            ROLLBACK;
        END IF;
        RAISE;

END;
';
