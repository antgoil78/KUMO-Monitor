-- Use the same Azure location and access settings as DEV.
-- Snowflake copies the stage configuration without exposing credentials here.
-- Existing TST stages are never replaced.

CREATE STAGE KUMO_TST.META.AZURE_LIM_STAGE
    CLONE KUMO_DEV.META.AZURE_LIM_STAGE;
