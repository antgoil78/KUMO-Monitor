-- Create the two missing support tables in the new TST database.
-- Definitions at creation time: DEV readiness log and the original TST dependency rule.
-- Applied before readiness logs were consolidated into MONITOR_META.
-- On resuming TST setup, relocate this empty log to MONITOR_META; use the
-- current saved DEV MONITOR_META definitions for subsequent provisioning.
-- Creates empty tables; existing tables are never replaced.

CREATE TABLE KUMO_TST.META._TMP_SET_READY_LOG (
    PKG_GROUP_NAME VARCHAR(16777216),
    DLVY_END_DATE DATE,
    DLVY_SOURCE_ID VARCHAR(16777216),
    DLVY_PKG_ID VARCHAR(16777216),
    DLVY_PKG_YEAR VARCHAR(16777216),
    DLVY_PKG_YEAR_SEQ_NO VARCHAR(16777216),
    STATUS VARCHAR(16777216),
    ROWS_UPDATED NUMBER(38,0),
    REASON VARCHAR(16777216),
    CONTROL_DATE TIMESTAMP_NTZ(9)
);

CREATE HYBRID TABLE KUMO_TST.META.WORKFLOW_DEP_RULE (
    ACTIVE_FL BOOLEAN,
    WORKFLOW_ID VARCHAR(256) NOT NULL,
    DEPENDEE_TYPE VARCHAR(256),
    DEPENDEE_ID VARCHAR(256) NOT NULL,
    SUCCESS BOOLEAN,
    WARNING BOOLEAN,
    ERROR BOOLEAN,
    NEWER BOOLEAN,
    PRIMARY KEY (WORKFLOW_ID, DEPENDEE_ID)
);
