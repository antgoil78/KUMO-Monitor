-- Source: KUMO_ADMIN.META.SP_FILESTATUS_RAW_LIM(VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."META";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.META.SP_FILESTATUS_RAW_LIM("SUBJECT_AREA" VARCHAR, "DBT_DB" VARCHAR DEFAULT CURRENT_DATABASE())
RETURNS TABLE ()
LANGUAGE SQL
EXECUTE AS CALLER
AS '
BEGIN

    LET raw_table varchar :=  :dbt_db ||''.RAW_LIM.RAW_LIM_'' || :subject_area;

    LET res RESULTSET;

    BEGIN
        res := (
        select 
            source,
            format,
            dt,
            pkg_seq_no,
            case nvl((lag(pkg_seq_no) over (partition by source,format order by dt)+1)::int::varchar,''UNKOWN'')
                when ''UNKOWN'' then ''UNKOWN''
                when pkg_seq_no::int::varchar then ''OK''
            else ''ERROR'' end pkg_seq_no_ok,
            files,
            recs,
            expected_drecs,
            actual_drecs,
            drecs_ok
        from (
                select
                    substr(dw_file_nm,1,3) source,
                    substr(dw_file_nm,4,7) format,
                    substr(dw_file_nm,35,8) dt,
                    substr(dw_file_nm,25,9) pkg_seq_no,
                    count(distinct dw_file_nm) files,
                    count(*) recs,
                    max(case when left(data,2) = ''99'' then substr(data,68,9) else 0 end)::int expected_drecs,
                    count_if(left(data,2) = ''10'') actual_drecs,
                    iff(expected_drecs!=actual_drecs,''ERROR'',''OK'') drecs_ok
                from identifier(:raw_table) group by all)
          order by dt,source,format);
    EXCEPTION
        WHEN OTHER THEN
            res := (select ''Table ''||:raw_table||'' not found'' as message);
    END;

    RETURN TABLE(res);

END
';
