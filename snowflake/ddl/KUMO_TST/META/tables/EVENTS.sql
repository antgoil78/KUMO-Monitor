-- Source: KUMO_TST.META.EVENTS
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_TST";
USE SCHEMA "KUMO_TST"."META";

create or replace event table KUMO_TST.META.EVENTS;
