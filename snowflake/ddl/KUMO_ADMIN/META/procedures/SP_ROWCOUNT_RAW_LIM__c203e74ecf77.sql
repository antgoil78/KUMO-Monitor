-- Source: KUMO_ADMIN.META.SP_ROWCOUNT_RAW_LIM(VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.META.SP_ROWCOUNT_RAW_LIM("SUBJECT_AREA" VARCHAR DEFAULT '%', "DBT_DB" VARCHAR DEFAULT CURRENT_DATABASE())
RETURNS TABLE ()
LANGUAGE SQL
EXECUTE AS CALLER
AS 'BEGIN


dbt_db := UPPER(dbt_db);

    let sql1 := ''select c from (
                            select 1 o,''''select
                                table_name,
                                format,
                                subject_area,
                                source_id,
                                pkg_id,
                                listagg(replace(file_date,''''''''-'''''''','''''''''''''''')||''''''''(''''''''||data_recs||'''''''')'''''''' ,'''''''', '''''''') within group (order by file_date) dates,
                                sum(files) files,
                                sum(unique_files) unique_files,
                                sum(mutated_files) mutated_files,
                                sum(data_recs) data_recs,
                                sum(non_data_recs) non_data_recs
                            from (
                                select
                                    table_name,
                                    format,
                                    subject_area,
                                    source_id,
                                    pkg_id,
                                    file_date,
                                    count(distinct DW_FILE_NM||DW_LOAD_DTTM) files,
                                    count(distinct DW_FILE_NM||DW_FILE_CHECK_SUM) unique_files,
                                    files-count(distinct DW_FILE_NM||DW_FILE_CHECK_SUM||DW_LOAD_DTTM) mutated_files,
                                    count_if(data_rec) data_recs,
                                    count_if(non_data_rec) non_data_recs
                                from (
                                    select
                                        ''''''''''''||table_name||'''''''''''' table_name,
                                        substring(dw_file_nm,4,4)||''''''''_''''''''||substring(dw_file_nm,8,3) format,
                                        substring(dw_file_nm,4,4) subject_area,
                                        left(dw_file_nm,3) source_id,
                                        substring(dw_file_nm,16,3) pkg_id,
                                        DW_FILE_NM,
                                        DW_LOAD_DTTM,
                                        DW_FILE_CHECK_SUM,
                                        to_date(substr(dw_file_nm,35,8),''''''''YYYYMMDD'''''''') file_date,
                                        left(data,2)!=''''''''10'''''''' non_data_rec,
                                        left(data,2)=''''''''10'''''''' data_rec
                                    from '''' || table_catalog ||''''.''''||table_schema||''''.''''||table_name||''''
                                ) t
                                group by all
                            ) group by all union all '''' c
                        from ''||dbt_db||''.information_schema.tables
                        where table_schema = ''''RAW_LIM''''
                            and table_name like ''''RAW_LIM/_''||subject_area||'''''' escape ''''/''''
                            and length(table_name) = 12
                        
                        union all select 99 o,''''select null,null,null,null,null,null,null,null,null,null,null where 1=2 
                        order by 3,2,4,5''''
                ) order by o'';

    let res1 resultset := (execute immediate sql1);
    let sql2 := '''';
    for rec in res1 do
        sql2 := sql2 || rec.c;
    end for;
    let res2 resultset;
    BEGIN
        res2 := (execute immediate sql2);
    --EXCEPTION
        --WHEN OTHER THEN
          --  res2 := (select ''Table not found'' as message);
    END;
    return table(res2);

END';
