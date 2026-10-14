-- Source: KUMO_ADMIN.META.SP_ROWCOUNT_SDL(VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.META.SP_ROWCOUNT_SDL("SUBJECT_AREA" VARCHAR DEFAULT '%', "DBT_DB" VARCHAR DEFAULT CURRENT_DATABASE())
RETURNS TABLE ()
LANGUAGE SQL
EXECUTE AS CALLER
AS 'BEGIN

dbt_db := UPPER(dbt_db);


    let sql1 := ''select c from (
                            select 1 o,''''select ''''''''''''||table_name||'''''''''''' as tab,
                                nvl(sum(1),0) cnt,
                                nvl(sum(case when dw_crnt_fl then 1 else 0 end),0) is_current,
                                nvl(sum(case when dw_crnt_fl then 0 else 1 end),0) is_not_current,
                                nvl(sum(case when dw_note is null then 0 else 1 end),0) with_note,
                            from  '''' || table_catalog ||''''.''''||table_schema||''''.''''||table_name||'''' union all '''' c
                        from ''||dbt_db||''.information_schema.tables
                        where table_schema = ''''SDL''''
                            and table_name like ''''SDL/_''||subject_area||''/_%'''' escape ''''/''''
                        
                        union all select 99 o,''''select null::varchar as tab,null::number as cnt,null::number as is_current,null::number as is_not_current,null::number as with_note where 1=2 
                        order by tab''''
                ) order by o'';

    let res1 resultset := (execute immediate sql1);
    let sql2 := '''';
    for rec in res1 do
        sql2 := sql2 || rec.c;
    end for;
    if (sql2 = '''') then
        sql2 := ''select null::varchar as tab, null::number as cnt, null::number as is_current, null::number as is_not_current, null::number as with_note where 1=2'';
    end if;
    let res2 resultset;
    BEGIN
        res2 := (execute immediate sql2);
    EXCEPTION
        WHEN OTHER THEN
            res2 := (select ''Table not found'' as message);
    END;
    return table(res2);

END';
