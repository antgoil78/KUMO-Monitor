-- Source: KUMO_DEV.META.SP_REMOVE_DUPLICATES(VARCHAR, VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_DEV";
USE SCHEMA "KUMO_DEV"."META";

CREATE OR REPLACE PROCEDURE KUMO_DEV.META.SP_REMOVE_DUPLICATES("TABLE_NAME" VARCHAR DEFAULT 'INVALID TABLE', "OPERATION" VARCHAR DEFAULT 'ANALYSIS', "DBT_DB" VARCHAR DEFAULT CURRENT_DATABASE())
RETURNS TABLE ()
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
    v_business_key     VARCHAR;
    v_bk_columns       VARCHAR;
    v_join_clause      VARCHAR;
    v_fq_table         VARCHAR;
    v_fq_meta          VARCHAR;
    v_sql              VARCHAR;
    v_agg_func         VARCHAR;
    v_deleted          INTEGER;
    rs                 RESULTSET;
BEGIN
    IF (LEFT(UPPER(table_name), 4) != ''SDL_'') THEN
        rs := (SELECT ''ERROR: table_name must start with SDL_'' AS RESULT);
        RETURN TABLE(rs);
    END IF;

    v_fq_meta := dbt_db || ''.META.UNIQUE_KEY'';
    v_fq_table := dbt_db || ''.SDL.'' || table_name;

    SELECT BUSINESS_KEY INTO :v_business_key
    FROM IDENTIFIER(:v_fq_meta)
    WHERE TABLE_NAME = :table_name;

    IF (v_business_key IS NULL) THEN
        rs := (SELECT ''ERROR: No business key found for table '' || :table_name AS RESULT);
        RETURN TABLE(rs);
    END IF;

    v_bk_columns := v_business_key;

    LET col_array := SPLIT(v_business_key, '','');
    v_join_clause := '''';
    FOR i IN 0 TO ARRAY_SIZE(col_array) - 1 DO
        LET col VARCHAR := TRIM(col_array[i]::VARCHAR, '' "'');
        IF (i > 0) THEN
            v_join_clause := v_join_clause || '' AND '';
        END IF;
        v_join_clause := v_join_clause || ''sdl.'' || col || '' = dups.'' || col;
    END FOR;

    IF (UPPER(operation) = ''ANALYSIS'') THEN
        v_sql := ''SELECT '' || v_bk_columns || '', DW_VLD_FM_DTTM, DUPS, '' ||
                 ''''''Records are ''''||CASE DUPS != NON_IDENTICAL_DUPS WHEN TRUE THEN ''''identical'''' ELSE ''''not identical'''' END AS DATA '' ||
                 ''FROM (SELECT '' || v_bk_columns || '', DW_VLD_FM_DTTM, '' ||
                 ''COUNT(*) AS DUPS, COUNT(DISTINCT DW_CHKSUM) AS NON_IDENTICAL_DUPS '' ||
                 ''FROM '' || v_fq_table || '' GROUP BY ALL '' ||
                 ''HAVING COUNT(*) != 1 OR COUNT(DISTINCT DW_CHKSUM) != 1) ORDER BY '' || v_bk_columns || '', DW_VLD_FM_DTTM'';
        rs := (EXECUTE IMMEDIATE :v_sql);
        RETURN TABLE(rs);

    ELSEIF (UPPER(operation) = ''LIST'') THEN
        v_sql := ''SELECT * FROM '' || v_fq_table ||
                 '' QUALIFY COUNT(*) OVER (PARTITION BY '' || v_bk_columns || '', DW_VLD_FM_DTTM) > 1'' ||
                 '' ORDER BY '' || v_bk_columns || '', DW_VLD_FM_DTTM'';
        rs := (EXECUTE IMMEDIATE :v_sql);
        RETURN TABLE(rs);

    ELSEIF (UPPER(operation) = ''KEEP FIRST'') THEN
        v_agg_func := ''MIN'';
    ELSEIF (UPPER(operation) = ''KEEP LAST'') THEN
        v_agg_func := ''MAX'';
    ELSE
        rs := (SELECT ''ERROR: Invalid operation. Use ANALYSIS, LIST, KEEP FIRST, or KEEP LAST.'' AS RESULT);
        RETURN TABLE(rs);
    END IF;

    v_sql := ''DELETE FROM '' || v_fq_table || '' sdl USING ('' ||
             ''SELECT '' || v_bk_columns || '', DW_VLD_FM_DTTM, '' || v_agg_func || ''(DW_FILE_ROW_NUMBER) AS DW_FILE_ROW_NUMBER '' ||
             ''FROM '' || v_fq_table || '' GROUP BY ALL HAVING COUNT(*) != 1'' ||
             '') dups WHERE '' || v_join_clause ||
             '' AND sdl.DW_VLD_FM_DTTM = dups.DW_VLD_FM_DTTM'' ||
             '' AND sdl.DW_FILE_ROW_NUMBER != dups.DW_FILE_ROW_NUMBER'';

    EXECUTE IMMEDIATE v_sql;
    v_deleted := SQLROWCOUNT;
    IF (v_deleted > 0) THEN
        rs := (SELECT :v_deleted || '' duplicates removed from '' || :v_fq_table || '' using '' || :operation AS RESULT
               UNION ALL
               SELECT ''NB: The versioning of '' || :table_name || '' now must be corrected, run this command:''
               UNION ALL
               SELECT ''CALL KUMO_ADMIN.META.SP_FIX_VERSIONING(dbt_db=>'''''' || :dbt_db || '''''', table_name=>'''''' || :table_name || '''''');'');
    ELSE
        rs := (SELECT ''0 duplicates removed from '' || :v_fq_table || '' using '' || :operation AS RESULT);
    END IF;
    RETURN TABLE(rs);
END;
';
