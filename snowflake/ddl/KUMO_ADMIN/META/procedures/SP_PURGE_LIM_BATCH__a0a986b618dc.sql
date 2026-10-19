-- Source: KUMO_ADMIN.META.SP_PURGE_LIM_BATCH(VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.META.SP_PURGE_LIM_BATCH("DBT_DB" VARCHAR DEFAULT null)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
    v_db        VARCHAR;
    v_table     VARCHAR;
    v_del_sql   VARCHAR;
    v_total     INTEGER DEFAULT 0;
    v_count     INTEGER;
    c1 CURSOR FOR
        SELECT DISTINCT DW_TABLE_NM
        FROM _purge_batch_snapshot;
BEGIN
    v_db := COALESCE(:dbt_db, CURRENT_DATABASE());

    -- Snapshot the exact rows we intend to process
    CREATE OR REPLACE TEMPORARY TABLE _purge_batch_snapshot AS
    SELECT DW_TABLE_NM, DW_FILE_NM
    FROM META.LIM_BATCH_CTRL
    WHERE PROCESSED_FL
      AND NOT DELETED_FL
      AND PROCESSED_DTTM < DATEADD(''day'', -3, sysdate());

    OPEN c1;
    FOR rec IN c1 DO
        v_table := rec.DW_TABLE_NM;

        v_del_sql := ''
            DELETE FROM '' || :v_db || ''.'' || :v_table || '' t
            USING _purge_batch_snapshot snap
            WHERE t.DW_FILE_NM = snap.DW_FILE_NM
              AND snap.DW_TABLE_NM = '''''' || :v_table || '''''''';

        EXECUTE IMMEDIATE :v_del_sql;

        v_count := SQLROWCOUNT;
        v_total := :v_total + :v_count;
    END FOR;

    -- Mark exactly the snapshotted rows as deleted
    UPDATE META.LIM_BATCH_CTRL ctrl
    SET DELETED_FL = TRUE
    FROM _purge_batch_snapshot snap
    WHERE ctrl.DW_TABLE_NM = snap.DW_TABLE_NM
      AND ctrl.DW_FILE_NM  = snap.DW_FILE_NM
      AND ctrl.PROCESSED_FL
      AND NOT ctrl.DELETED_FL;

    DROP TABLE IF EXISTS _purge_batch_snapshot;

    RETURN ''Deleted '' || :v_total || '' total rows'';
END;
';
