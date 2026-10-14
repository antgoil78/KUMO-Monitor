-- Source: KUMO_ADMIN.META.SP_ROWCOUNT_EDV(VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.META.SP_ROWCOUNT_EDV("SUBJECT_AREA" VARCHAR DEFAULT '%', "DBT_DB" VARCHAR DEFAULT CURRENT_DATABASE())
RETURNS TABLE ()
LANGUAGE SQL
EXECUTE AS CALLER
AS 'BEGIN

dbt_db := UPPER(dbt_db);
subject_area := UPPER(subject_area);

    let sql1 := ''select c from (
                                select 1 o,''''select ''''''''''''||table_name||'''''''''''' tab,
                                nvl(sum(1),0) cnt,
                                null is_current,
                                null is_not_current,
                                null is_active,
                                null is_not_active
                            from  '''' || table_catalog ||''''.''''||table_schema||''''.''''||table_name||'''' union all '''' c
                        from ''||dbt_db||''.information_schema.tables
                        where table_schema = ''''EDV''''
                            and table_name like ''''EDV/_/_'' || subject_area || ''/_%/_H'''' escape ''''/''''
                        union all
                            select 1 o,''''select ''''''''''''||table_name||'''''''''''' tab,
                                nvl(sum(1),0) cnt,
                                nvl(sum(case when dw_crnt_fl then 1 else 0 end),0) is_current,
                                nvl(sum(case when dw_crnt_fl then 0 else 1 end),0) is_not_current,
                                null is_active,
                                null is_not_active
                            from  '''' || table_catalog ||''''.''''||table_schema||''''.''''||table_name||'''' union all '''' c
                        from ''||dbt_db||''.information_schema.tables
                        where table_schema = ''''EDV''''
                            and table_name like ''''EDV/_/_'' || subject_area || ''/_%/_S'''' escape ''''/''''
                        union all
                            select 2 o,''''select ''''''''''''||table_name||'''''''''''' tab,
                                nvl(sum(1),0) cnt,
                                null is_current,
                                null is_not_current,
                                null is_active,
                                null is_not_active
                            from  '''' || table_catalog ||''''.''''||table_schema||''''.''''||table_name||'''' union all '''' c
                        from ''||dbt_db||''.information_schema.tables
                        where table_schema = ''''EDV''''
                            and table_name like ''''EDV/_/_'' || subject_area || ''/_%/_L'''' escape ''''/''''
                        union all
                            select 2 o,''''select ''''''''''''||table_name||'''''''''''' tab,
                                nvl(sum(1),0) cnt,
                                nvl(sum(case when dw_crnt_fl then 1 else 0 end),0) is_current,
                                nvl(sum(case when dw_crnt_fl then 0 else 1 end),0) is_not_current,
                                nvl(sum(case when dw_crnt_fl and dw_active_fl then 1 else 0 end),0) is_active,
                                nvl(sum(case when dw_crnt_fl and not dw_active_fl then 1 else 0 end),0) is_not_active 
                            from  '''' || table_catalog ||''''.''''||table_schema||''''.''''||table_name||'''' union all '''' c
                        from ''||dbt_db||''.information_schema.tables
                        where table_schema = ''''EDV''''
                            and table_name like ''''EDV/_/_'' || subject_area || ''/_%/_LS'''' escape ''''/''''
                        union all select 99 o,''''select null tab,null cnt,null is_current,null is_not_current,null is_active,null is_not_active where 1=2 
                        order by case right(tab,2) when ''''''''_H'''''''' then 1 when ''''''''_S'''''''' then 1 when ''''''''_L'''''''' then 2 when ''''''''LS'''''''' then 2 end,tab''''
                ) order by o'';


    let res1 resultset := (execute immediate sql1);
    let sql2 := '''';
    for rec in res1 do
        sql2 := sql2 || rec.c;
    end for;
    let res2 resultset;
    BEGIN
        res2 := (execute immediate sql2);
    EXCEPTION
        WHEN OTHER THEN
            res2 := (select ''Table not found'' as message);
    END;
    return table(res2);

END';
