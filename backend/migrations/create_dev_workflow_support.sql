-- Create-only support definitions; use apply_dev_workflow_configuration.py --apply.
-- Configuration data is migrated by bound parameters, never stored in this file.
CREATE TABLE KUMO_DEV.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE (
	SUBJECT_AREA VARCHAR(16777216),
	PKG_GROUP_NAME VARCHAR(16777216),
	DATA VARCHAR(16777216),
	DW_BATCH_ID VARCHAR(16777216),
	DW_FILE_CHECK_SUM VARCHAR(16777216),
	DW_FILE_DTTM TIMESTAMP_NTZ(9),
	DW_FILE_NM VARCHAR(16777216),
	DW_FILE_ROW_NUMBER NUMBER(38,0),
	DW_LOAD_DTTM TIMESTAMP_NTZ(9),
	DW_READY_TO_LOAD_FL BOOLEAN,
	DW_ARCHIVED_DTTM TIMESTAMP_NTZ(9) DEFAULT CAST(CONVERT_TIMEZONE('UTC', CAST(CURRENT_TIMESTAMP() AS TIMESTAMP_TZ(9))) AS TIMESTAMP_NTZ(9))
);

CREATE PROCEDURE KUMO_DEV.MONITOR_APP.SP_RAW_LIM_SET_READY("PKG_GROUP_NAME" VARCHAR, "IGNORE_COMMON_END_DT" BOOLEAN DEFAULT FALSE, "IGNORE_SEQ_NO" BOOLEAN DEFAULT FALSE, "P_DATABASE" VARCHAR DEFAULT 'KUMO_DEV')
RETURNS TABLE ("PKG_GROUP_NAME" VARCHAR, "DLVY_END_DATE" DATE, "DLVY_SOURCE_ID" VARCHAR, "DLVY_PKG_ID" VARCHAR, "DLVY_PKG_YEAR" VARCHAR, "DLVY_PKG_YEAR_SEQ_NO" VARCHAR, "STATUS" VARCHAR, "ROWS_UPDATED" NUMBER(38,0), "REASON" VARCHAR, "CONTROL_DATE" TIMESTAMP_NTZ(9))
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(P_DATABASE));
    E_INVALID_DATABASE EXCEPTION (-20002, ''Invalid database identifier'');
    V_SOURCE_MAP VARCHAR;
    V_METADATA_TABLE VARCHAR;
    V_LATEST_LOG VARCHAR;
    V_HISTORY_LOG VARCHAR;
    V_ARCHIVE VARCHAR;
    V_BATCH_CTRL VARCHAR;
    V_RAW_TABLE VARCHAR;
    V_SUBJECT_AREA VARCHAR;
    V_ROWS_AFFECTED INTEGER DEFAULT 0;
BEGIN
    IF (V_DATABASE IS NULL OR NOT REGEXP_LIKE(V_DATABASE, ''^[A-Z_][A-Z0-9_$]*$'')) THEN
        RAISE E_INVALID_DATABASE;
    END IF;
    V_SOURCE_MAP := V_DATABASE || ''.MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE'';
    V_METADATA_TABLE := V_DATABASE || ''.MONITOR_META.RAW_LIM_META'';
    V_LATEST_LOG := V_DATABASE || ''.META._TMP_SET_READY_LOG'';
    V_HISTORY_LOG := V_DATABASE || ''.MONITOR_META._SET_READY_LOG'';
    V_ARCHIVE := V_DATABASE || ''.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE'';
    V_BATCH_CTRL := V_DATABASE || ''.META.LIM_BATCH_CTRL'';
    SELECT DISTINCT SUBJECT_AREA INTO :V_SUBJECT_AREA
    FROM IDENTIFIER(:V_SOURCE_MAP)
    WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME
      AND ACTIVE_FL = TRUE
    LIMIT 1;

    V_RAW_TABLE := :V_DATABASE || ''.RAW_LIM.RAW_LIM_'' || UPPER(:V_SUBJECT_AREA);

    CREATE TABLE IF NOT EXISTS IDENTIFIER(:V_LATEST_LOG) (
        PKG_GROUP_NAME VARCHAR,
        DLVY_END_DATE DATE,
        DLVY_SOURCE_ID VARCHAR,
        DLVY_PKG_ID VARCHAR,
        DLVY_PKG_YEAR VARCHAR,
        DLVY_PKG_YEAR_SEQ_NO VARCHAR,
        STATUS VARCHAR,
        ROWS_UPDATED INTEGER,
        REASON VARCHAR,
        CONTROL_DATE TIMESTAMP_NTZ
    );

    CREATE TABLE IF NOT EXISTS IDENTIFIER(:V_HISTORY_LOG) (
        PKG_GROUP_NAME VARCHAR,
        DLVY_END_DATE DATE,
        DLVY_SOURCE_ID VARCHAR,
        DLVY_PKG_ID VARCHAR,
        DLVY_PKG_YEAR VARCHAR,
        DLVY_PKG_YEAR_SEQ_NO VARCHAR,
        STATUS VARCHAR,
        ROWS_UPDATED INTEGER,
        REASON VARCHAR,
        CONTROL_DATE TIMESTAMP_NTZ
    );

    INSERT INTO IDENTIFIER(:V_HISTORY_LOG)
    SELECT * FROM IDENTIFIER(:V_LATEST_LOG)
    WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME;

    DELETE FROM IDENTIFIER(:V_LATEST_LOG)
    WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME;

    INSERT INTO IDENTIFIER(:V_LATEST_LOG)
    WITH deliveries AS (
        SELECT
            DLVY_END_DATE,
            DLVY_SOURCE_ID,
            DLVY_PKG_ID,
            DLVY_PKG_YEAR,
            DLVY_PKG_YEAR_SEQ_NO,
            DW_READY_TO_LOAD_FL,
            FILE_KEY,
            RECEIVED_FL,
            ROWCOUNT_STATUS,
            ALL_SOURCES_FL,
            IS_VALID_SEQUENCE,
            PKG_GROUP_NAME,
            EXPECTED_ROWS,
            ACTUAL_ROWS
        FROM IDENTIFIER(:V_METADATA_TABLE)
        WHERE RAW_TABLE = :V_RAW_TABLE
          AND PKG_GROUP_NAME = :PKG_GROUP_NAME
          AND RUN_DTTM = (SELECT MAX(RUN_DTTM) FROM IDENTIFIER(:V_METADATA_TABLE) WHERE RAW_TABLE = :V_RAW_TABLE)
    ),
    delivery_agg AS (
        SELECT
            DLVY_END_DATE,
            DLVY_SOURCE_ID,
            DLVY_PKG_ID,
            DLVY_PKG_YEAR,
            DLVY_PKG_YEAR_SEQ_NO,
            MAX(DW_READY_TO_LOAD_FL::INTEGER) AS IS_LOADED,
            LISTAGG(DISTINCT
                CASE
                    WHEN DW_READY_TO_LOAD_FL = TRUE THEN NULL
                    WHEN RECEIVED_FL = FALSE THEN ''MISSING_FILE='' || FILE_KEY
                    WHEN ROWCOUNT_STATUS != ''ROWCOUNT_OK'' THEN ''ROWCOUNT_MISMATCH='' || FILE_KEY || '' (expected='' || COALESCE(EXPECTED_ROWS::STRING, ''?'') || '' actual='' || COALESCE(ACTUAL_ROWS::STRING, ''?'') || '')''
                    WHEN :IGNORE_COMMON_END_DT = FALSE AND ALL_SOURCES_FL = FALSE THEN ''INCOMPLETE_GROUP='' || PKG_GROUP_NAME || '' (missing sources for DATE='' || DLVY_END_DATE::STRING || '')''
                    WHEN :IGNORE_SEQ_NO = FALSE AND IS_VALID_SEQUENCE = FALSE THEN ''INVALID_SEQUENCE='' || FILE_KEY
                    ELSE NULL
                END
            , ''; '') AS REASONS
        FROM deliveries
        GROUP BY DLVY_END_DATE, DLVY_SOURCE_ID, DLVY_PKG_ID, DLVY_PKG_YEAR, DLVY_PKG_YEAR_SEQ_NO
    ),
    ordered AS (
        SELECT
            *,
            CASE
                WHEN IS_LOADED = 1 THEN ''PREVIOUSLY_LOADED''
                WHEN REASONS IS NOT NULL AND REASONS != '''' THEN ''FAILED''
                ELSE ''VALID''
            END AS RAW_STATUS,
            MIN(CASE WHEN REASONS IS NOT NULL AND REASONS != '''' AND IS_LOADED = 0 THEN DLVY_END_DATE END)
                OVER () AS FIRST_FAILURE_DATE
        FROM delivery_agg
    )
    SELECT
        :PKG_GROUP_NAME,
        DLVY_END_DATE,
        DLVY_SOURCE_ID,
        DLVY_PKG_ID,
        DLVY_PKG_YEAR,
        DLVY_PKG_YEAR_SEQ_NO,
        CASE
            WHEN RAW_STATUS = ''PREVIOUSLY_LOADED'' THEN ''PREVIOUSLY_LOADED''
            WHEN RAW_STATUS = ''FAILED'' AND DLVY_END_DATE = FIRST_FAILURE_DATE THEN ''STOPPED''
            WHEN RAW_STATUS = ''FAILED'' THEN ''BLOCKED''
            WHEN RAW_STATUS = ''VALID'' AND (FIRST_FAILURE_DATE IS NULL OR DLVY_END_DATE < FIRST_FAILURE_DATE) THEN ''UPDATED''
            ELSE ''BLOCKED''
        END AS STATUS,
        NULL,
        CASE
            WHEN RAW_STATUS = ''PREVIOUSLY_LOADED'' THEN NULL
            WHEN RAW_STATUS = ''FAILED'' THEN REASONS
            WHEN RAW_STATUS = ''VALID'' AND (FIRST_FAILURE_DATE IS NULL OR DLVY_END_DATE < FIRST_FAILURE_DATE) THEN NULL
            ELSE ''Blocked by earlier failure''
        END AS REASON,
        SYSDATE()
    FROM ordered
    ORDER BY DLVY_END_DATE, DLVY_SOURCE_ID;

    -- Update DW_READY_TO_LOAD_FL on payload files
    UPDATE IDENTIFIER(:V_RAW_TABLE) raw
    SET raw.DW_READY_TO_LOAD_FL = TRUE
    WHERE IFNULL(raw.DW_READY_TO_LOAD_FL, FALSE) = FALSE
      AND EXISTS (
          SELECT 1
          FROM IDENTIFIER(:V_METADATA_TABLE) m
          INNER JOIN IDENTIFIER(:V_LATEST_LOG) log
              ON m.DLVY_END_DATE = log.DLVY_END_DATE
             AND m.DLVY_SOURCE_ID = log.DLVY_SOURCE_ID
             AND m.DLVY_PKG_ID = log.DLVY_PKG_ID
             AND m.DLVY_PKG_YEAR = log.DLVY_PKG_YEAR
             AND m.DLVY_PKG_YEAR_SEQ_NO = log.DLVY_PKG_YEAR_SEQ_NO
             AND log.PKG_GROUP_NAME = :PKG_GROUP_NAME
             AND log.STATUS = ''UPDATED''
          WHERE m.RAW_TABLE = :V_RAW_TABLE
            AND m.FILE_TYPE = ''PAYLOAD''
            AND m.PKG_GROUP_NAME = :PKG_GROUP_NAME
            AND m.RUN_DTTM = (SELECT MAX(RUN_DTTM) FROM IDENTIFIER(:V_METADATA_TABLE) WHERE RAW_TABLE = :V_RAW_TABLE)
            AND raw.DW_FILE_NM::STRING = m.ORIGINAL_FILE_NAME
      );

    V_ROWS_AFFECTED := SQLROWCOUNT;

    -- Register or refresh payload files from deliveries marked UPDATED.
    MERGE INTO IDENTIFIER(:V_BATCH_CTRL) tgt
    USING (
        SELECT DISTINCT
            SPLIT_PART(:V_RAW_TABLE, ''.'', -2) || ''.'' ||
                SPLIT_PART(:V_RAW_TABLE, ''.'', -1) AS DW_TABLE_NM,
            raw.DW_FILE_NM::STRING AS DW_FILE_NM,
            src.SOURCE_ID AS DW_SRC_SYST
        FROM IDENTIFIER(:V_RAW_TABLE) raw
        INNER JOIN IDENTIFIER(:V_METADATA_TABLE) m
            ON raw.DW_FILE_NM::STRING = m.ORIGINAL_FILE_NAME
        INNER JOIN IDENTIFIER(:V_LATEST_LOG) log
            ON m.DLVY_END_DATE = log.DLVY_END_DATE
           AND m.DLVY_SOURCE_ID = log.DLVY_SOURCE_ID
           AND m.DLVY_PKG_ID = log.DLVY_PKG_ID
           AND m.DLVY_PKG_YEAR = log.DLVY_PKG_YEAR
           AND m.DLVY_PKG_YEAR_SEQ_NO = log.DLVY_PKG_YEAR_SEQ_NO
           AND log.PKG_GROUP_NAME = :PKG_GROUP_NAME
           AND log.STATUS = ''UPDATED''
        INNER JOIN IDENTIFIER(:V_SOURCE_MAP) src
            ON src.PKG_GROUP_NAME = m.PKG_GROUP_NAME
           AND src.SOURCE_ID = m.DLVY_SOURCE_ID
           AND src.PKG_ID = m.DLVY_PKG_ID
           AND src.ACTIVE_FL = TRUE
        WHERE m.RAW_TABLE = :V_RAW_TABLE
          AND m.FILE_TYPE = ''PAYLOAD''
          AND m.DATA_ROWS > 0
          AND m.PKG_GROUP_NAME = :PKG_GROUP_NAME
          AND m.RUN_DTTM = (
              SELECT MAX(RUN_DTTM)
              FROM IDENTIFIER(:V_METADATA_TABLE)
              WHERE RAW_TABLE = :V_RAW_TABLE
          )
          AND raw.DW_READY_TO_LOAD_FL = TRUE
    ) files
       ON tgt.DW_TABLE_NM = files.DW_TABLE_NM
      AND tgt.DW_FILE_NM = files.DW_FILE_NM
      AND tgt.DW_SRC_SYST = files.DW_SRC_SYST
      /*
    WHEN MATCHED THEN UPDATE SET
        READY_TO_LOAD_DTTM = SYSDATE(),
        DW_VER_CTRL = null,
        DW_VLD_FM_DTTM = null,
        NO_OF_RECS = null,
        DW_BATCH_ID = null,
        PROCESSED_FL = false,
        DELETED_FL = false
        */
    WHEN NOT MATCHED THEN INSERT (
        READY_TO_LOAD_DTTM,
        DW_TABLE_NM, DW_FILE_NM, DW_SRC_SYST,
        DW_VER_CTRL, DW_VLD_FM_DTTM, NO_OF_RECS, DW_BATCH_ID,
        PROCESSED_FL, DELETED_FL
    ) VALUES (
        SYSDATE(),
        files.DW_TABLE_NM, files.DW_FILE_NM, files.DW_SRC_SYST,
        NULL, NULL, NULL, NULL,
        false,false);

    UPDATE IDENTIFIER(:V_LATEST_LOG)
    SET ROWS_UPDATED = :V_ROWS_AFFECTED
    WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME
      AND STATUS = ''UPDATED'';

    -- Flag control files as ready to load
    UPDATE IDENTIFIER(:V_RAW_TABLE) raw
    SET raw.DW_READY_TO_LOAD_FL = TRUE
    WHERE IFNULL(raw.DW_READY_TO_LOAD_FL, FALSE) = FALSE
      AND EXISTS (
          SELECT 1
          FROM IDENTIFIER(:V_METADATA_TABLE) meta
          INNER JOIN IDENTIFIER(:V_RAW_TABLE) raw2
              ON meta.ORIGINAL_FILE_NAME = raw2.DW_FILE_NM::STRING
          WHERE raw2.DW_READY_TO_LOAD_FL
            AND meta.RUN_DTTM = (SELECT MAX(RUN_DTTM) FROM IDENTIFIER(:V_METADATA_TABLE) WHERE RAW_TABLE = :V_RAW_TABLE)
            AND raw.DW_FILE_NM::STRING LIKE meta.CONTROL_FILE_NAME || ''%''
      );

    -- Archive flagged control files using direct JOIN on ORIGINAL_FILE_NAME
    INSERT INTO IDENTIFIER(:V_ARCHIVE) (
        SUBJECT_AREA,
        PKG_GROUP_NAME,
        DATA,
        DW_BATCH_ID,
        DW_FILE_CHECK_SUM,
        DW_FILE_DTTM,
        DW_FILE_NM,
        DW_FILE_ROW_NUMBER,
        DW_LOAD_DTTM,
        DW_READY_TO_LOAD_FL,
        DW_ARCHIVED_DTTM
    )
    SELECT DISTINCT
        :V_SUBJECT_AREA,
        :PKG_GROUP_NAME,
        raw.DATA,
        raw.DW_BATCH_ID,
        raw.DW_FILE_CHECK_SUM,
        raw.DW_FILE_DTTM,
        raw.DW_FILE_NM,
        raw.DW_FILE_ROW_NUMBER,
        raw.DW_LOAD_DTTM,
        raw.DW_READY_TO_LOAD_FL,
        SYSDATE()
    FROM IDENTIFIER(:V_RAW_TABLE) raw
    INNER JOIN IDENTIFIER(:V_METADATA_TABLE) meta
        ON raw.DW_FILE_NM::STRING = meta.ORIGINAL_FILE_NAME
       AND meta.RAW_TABLE = :V_RAW_TABLE
       AND meta.FILE_TYPE = ''CONTROL''
    WHERE raw.DW_READY_TO_LOAD_FL = TRUE;

    -- Delete archived control files from the raw table
    DELETE FROM IDENTIFIER(:V_RAW_TABLE) raw
    USING IDENTIFIER(:V_METADATA_TABLE) meta
    WHERE raw.DW_FILE_NM::STRING = meta.ORIGINAL_FILE_NAME
      AND meta.RAW_TABLE = :V_RAW_TABLE
      AND meta.FILE_TYPE = ''CONTROL''
      AND raw.DW_READY_TO_LOAD_FL = TRUE;

    UPDATE IDENTIFIER(:V_SOURCE_MAP) tgt
    SET tgt.DLVY_PKG_YEAR = src.MAX_YEAR,
        tgt.DLVY_PKG_YEAR_SEQ_NO = src.MAX_SEQ_NO
    FROM (
        SELECT
            DLVY_SOURCE_ID,
            DLVY_PKG_ID,
            MAX_BY(DLVY_PKG_YEAR, DLVY_PKG_YEAR || DLVY_PKG_YEAR_SEQ_NO) AS MAX_YEAR,
            MAX_BY(DLVY_PKG_YEAR_SEQ_NO, DLVY_PKG_YEAR || DLVY_PKG_YEAR_SEQ_NO) AS MAX_SEQ_NO
        FROM IDENTIFIER(:V_LATEST_LOG)
        WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME
          AND STATUS = ''UPDATED''
        GROUP BY DLVY_SOURCE_ID, DLVY_PKG_ID
    ) src
    WHERE tgt.ACTIVE_FL = 1
      AND tgt.PKG_GROUP_NAME = :PKG_GROUP_NAME
      AND tgt.SOURCE_ID = src.DLVY_SOURCE_ID
      AND tgt.PKG_ID = src.DLVY_PKG_ID
      AND (tgt.DLVY_PKG_YEAR IS NULL
           OR src.MAX_YEAR > tgt.DLVY_PKG_YEAR
           OR (src.MAX_YEAR = tgt.DLVY_PKG_YEAR AND src.MAX_SEQ_NO > tgt.DLVY_PKG_YEAR_SEQ_NO));

    LET V_RESULT RESULTSET := (SELECT * FROM IDENTIFIER(:V_LATEST_LOG) WHERE PKG_GROUP_NAME = :PKG_GROUP_NAME ORDER BY DLVY_END_DATE, DLVY_SOURCE_ID);
    RETURN TABLE(V_RESULT);
END;

';

CREATE PROCEDURE KUMO_DEV.MONITOR_APP.SP_CLEANUP_OLD_TMP_TASKS("P_MINUTES_OLD" FLOAT)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
EXECUTE AS OWNER
AS '
function exec(sqlText, binds) {
  return snowflake.execute({ sqlText, binds: binds || [] });
}

const databaseRow = exec("SELECT CURRENT_DATABASE()");
databaseRow.next();
const taskPrefix = String(databaseRow.getColumnValue(1)).toUpperCase() + ".MONITOR_APP.TMP_WF_";

function assertSafeTaskFqn(taskFqn) {
  const s = String(taskFqn || "").trim();

  // Expected: DB.SCHEMA.TASK_NAME
  // Allows normal generated Snowflake identifiers only.
  if (!/^[A-Z0-9_]+\\.[A-Z0-9_]+\\.[A-Z0-9_]+$/i.test(s)) {
    throw new Error("Unsafe TASK_FQN: " + s);
  }

  if (!s.toUpperCase().startsWith(taskPrefix)) {
    throw new Error("Task is outside this database and workflow execution namespace");
  }
  return s;
}

const minutesOld = Math.max(1, Math.floor(Number(P_MINUTES_OLD || 60)));

const rows = exec(`
  SELECT
      t.TASK_FQN,
      t.RUN_ID
  FROM MONITOR_META.WORKFLOW_TMP_TASKS t
  JOIN MONITOR_META.WORKFLOW_HISTORY h
    ON h.RUN_ID = t.RUN_ID
  WHERE COALESCE(t.STATUS, '''') <> ''DROPPED''
    AND t.CREATED_AT < DATEADD(''minute'', -1 * CAST(? AS INTEGER), SYSDATE())
    AND UPPER(TRIM(h.STATUS)) IN (''SUCCESS'', ''FAILED'', ''ERROR'', ''CANCELLED'', ''CANCELED'')
`, [minutesOld]);

let cleaned = [];
let failed = [];

while (rows.next()) {
  const taskFqnRaw = rows.getColumnValue(1);
  const runId = rows.getColumnValue(2);

  try {
    const taskFqn = assertSafeTaskFqn(taskFqnRaw);

    try {
      exec(`ALTER TASK IF EXISTS ${taskFqn} SUSPEND`);
    } catch (e1) {
      // Continue. Task may already be gone or not suspendable.
    }

    try {
      exec(`SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS(?)`, [taskFqn]);
    } catch (e2) {
      // Continue. There may be no active execution.
    }

    exec(`DROP TASK IF EXISTS ${taskFqn}`);

    exec(`
      UPDATE MONITOR_META.WORKFLOW_TMP_TASKS
      SET STATUS = ''DROPPED'',
          CLEANED_AT = SYSDATE(),
          CLEANUP_ERROR = NULL
      WHERE TASK_FQN = ?
    `, [taskFqnRaw]);

    cleaned.push(taskFqn);

  } catch (e) {
    exec(`
      UPDATE MONITOR_META.WORKFLOW_TMP_TASKS
      SET STATUS = ''CLEANUP_FAILED'',
          CLEANUP_ERROR = ?
      WHERE TASK_FQN = ?
    `, [String(e), taskFqnRaw]);

    failed.push({
      task_fqn: String(taskFqnRaw),
      run_id: String(runId),
      error: String(e)
    });
  }
}

return {
  minutes_old: minutesOld,
  cleaned: cleaned,
  failed: failed
};
';

CREATE PROCEDURE KUMO_DEV.MONITOR_APP.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST("P_DATABASE" VARCHAR DEFAULT 'KUMO_DEV')
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(P_DATABASE));
    V_INFORMATION_TABLES VARCHAR;
    V_TABLES RESULTSET;
    E_INVALID_DATABASE EXCEPTION (-20002, ''Invalid database identifier'');
    v_run_ts          TIMESTAMP_NTZ DEFAULT SYSDATE();
    v_table_start_ts  TIMESTAMP_NTZ;
    v_tbl             VARCHAR;
    v_fmt             VARCHAR;
    v_pattern         VARCHAR;
    v_table_ref       VARCHAR;
    v_copy_sql        VARCHAR;
    v_ins_sql         VARCHAR;
    v_upd_sql         VARCHAR;
    v_err_msg         VARCHAR;
    v_audit_rows      NUMBER DEFAULT 0;
    v_copy_ok         BOOLEAN DEFAULT FALSE;
    v_res             RESULTSET;



BEGIN
    IF (V_DATABASE IS NULL OR NOT REGEXP_LIKE(V_DATABASE, ''^[A-Z_][A-Z0-9_$]*$'')) THEN
        RAISE E_INVALID_DATABASE;
    END IF;
    V_INFORMATION_TABLES := V_DATABASE || ''.INFORMATION_SCHEMA.TABLES'';
    V_TABLES := (SELECT TABLE_NAME
        FROM IDENTIFIER(:V_INFORMATION_TABLES)
        WHERE TABLE_SCHEMA = ''RAW_LIM''
          AND TABLE_NAME LIKE ''RAW_LIM_%''
          AND TABLE_TYPE = ''BASE TABLE''
        ORDER BY TABLE_NAME);

    FOR rec IN V_TABLES DO

        v_tbl := rec.TABLE_NAME;
        v_fmt := SUBSTRING(v_tbl, 9);
        v_pattern := ''^(?!.*(?i:deleted_)).*'' || v_fmt || ''[^/]*$'';
        v_copy_ok := FALSE;
        v_audit_rows := 0;

        v_table_ref :=
            ''"'' || V_DATABASE || ''"."RAW_LIM".'' ||
            ''"'' || REPLACE(v_tbl, ''"'', ''""'') || ''"'';

        v_copy_sql :=
            ''COPY INTO '' || v_table_ref || ''
             (
                 DW_FILE_NM,
                 DW_FILE_DTTM,
                 DW_FILE_CHECK_SUM,
                 DW_FILE_ROW_NUMBER,
                 DW_LOAD_DTTM,
                 DATA
             )
             FROM (
                 SELECT
                     SPLIT_PART(METADATA$FILENAME, ''''/'''', -1) AS DW_FILE_NM,
                     METADATA$FILE_LAST_MODIFIED              AS DW_FILE_DTTM,
                     METADATA$FILE_CONTENT_KEY                AS DW_FILE_CHECK_SUM,
                     METADATA$FILE_ROW_NUMBER                 AS DW_FILE_ROW_NUMBER,
                     SYSDATE()                                AS DW_LOAD_DTTM,
                     $1                                       AS DATA
                 FROM @'' || V_DATABASE || ''.META.AZURE_LIM_STAGE
             )
             FILE_FORMAT = (FORMAT_NAME = '' || V_DATABASE || ''.META.KUMO_LIM)
             PATTERN = '' || CHR(39) || REPLACE(v_pattern, CHR(39), CHR(39) || CHR(39)) || CHR(39);

        BEGIN
            v_table_start_ts := SYSDATE();
            EXECUTE IMMEDIATE v_copy_sql;
        EXCEPTION
            WHEN OTHER THEN
                v_err_msg := SQLERRM;
        END;

    END FOR;

    RETURN ''Done'';

END;
';

CREATE PROCEDURE KUMO_DEV.MONITOR_APP.SP_PURGE_LIM_BATCH("DBT_DB" VARCHAR DEFAULT null)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(COALESCE(DBT_DB, ''KUMO_DEV'')));
    V_BATCH_CTRL VARCHAR;
    V_SNAPSHOT VARCHAR;
    V_TABLE VARCHAR;
    V_TARGET VARCHAR;
    V_DELETE_SQL VARCHAR;
    V_TOTAL INTEGER DEFAULT 0;
    V_COUNT INTEGER;
    V_TABLES RESULTSET;
    E_INVALID_DATABASE EXCEPTION (-20002, ''Invalid database identifier'');
    E_INVALID_TARGET EXCEPTION (-20003, ''Purge target is outside this database RAW_LIM schema'');
BEGIN
    IF (V_DATABASE IS NULL OR NOT REGEXP_LIKE(V_DATABASE, ''^[A-Z_][A-Z0-9_$]*$'')) THEN
        RAISE E_INVALID_DATABASE;
    END IF;

    V_BATCH_CTRL := V_DATABASE || ''.META.LIM_BATCH_CTRL'';
    V_SNAPSHOT := V_DATABASE || ''.MONITOR_META._PURGE_BATCH_SNAPSHOT'';

    CREATE OR REPLACE TEMPORARY TABLE IDENTIFIER(:V_SNAPSHOT) AS
    SELECT DW_TABLE_NM, DW_FILE_NM
      FROM IDENTIFIER(:V_BATCH_CTRL)
     WHERE PROCESSED_FL
       AND NOT DELETED_FL
       AND PROCESSED_DTTM < DATEADD(''day'', -3, SYSDATE());

    V_TABLES := (SELECT DISTINCT DW_TABLE_NM FROM IDENTIFIER(:V_SNAPSHOT));
    FOR REC IN V_TABLES DO
        V_TABLE := REC.DW_TABLE_NM;
        IF (REGEXP_LIKE(UPPER(V_TABLE), ''^RAW_LIM[.][A-Z_][A-Z0-9_$]*$'')) THEN
            V_TARGET := V_DATABASE || ''.'' || UPPER(V_TABLE);
        ELSEIF (REGEXP_LIKE(UPPER(V_TABLE), ''^[A-Z_][A-Z0-9_$]*[.]RAW_LIM[.][A-Z_][A-Z0-9_$]*$'')
                AND SPLIT_PART(UPPER(V_TABLE), ''.'', 1) = V_DATABASE) THEN
            V_TARGET := UPPER(V_TABLE);
        ELSE
            RAISE E_INVALID_TARGET;
        END IF;

        V_DELETE_SQL := ''DELETE FROM '' || V_TARGET || '' T USING '' || V_SNAPSHOT ||
            '' SNAP WHERE T.DW_FILE_NM = SNAP.DW_FILE_NM AND SNAP.DW_TABLE_NM = ?'';
        EXECUTE IMMEDIATE :V_DELETE_SQL USING (V_TABLE);
        V_COUNT := SQLROWCOUNT;
        V_TOTAL := V_TOTAL + V_COUNT;
    END FOR;

    UPDATE IDENTIFIER(:V_BATCH_CTRL) CTRL
       SET DELETED_FL = TRUE
      FROM IDENTIFIER(:V_SNAPSHOT) SNAP
     WHERE CTRL.DW_TABLE_NM = SNAP.DW_TABLE_NM
       AND CTRL.DW_FILE_NM = SNAP.DW_FILE_NM
       AND CTRL.PROCESSED_FL
       AND NOT CTRL.DELETED_FL;

    DROP TABLE IF EXISTS IDENTIFIER(:V_SNAPSHOT);
    RETURN ''Deleted '' || V_TOTAL || '' total rows'';
END;
';
