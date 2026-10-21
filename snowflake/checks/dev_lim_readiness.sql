-- DEV readiness preparation. Read-only: no procedures, loaders or tasks run.
-- Results contain metadata, aggregate counts and synthetic cases only.

-- Confirm that every DEV subject table has the six remaining RAW columns.
SELECT TABLE_NAME,
       COUNT(*) AS COLUMN_COUNT,
       COUNT_IF(COLUMN_NAME IN ('DW_READY_TO_LOAD_FL', 'DW_BATCH_ID')) AS REMOVED_COLUMNS
FROM KUMO_DEV.INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'RAW_LIM'
  AND TABLE_NAME LIKE 'RAW_LIM/_%' ESCAPE '/'
GROUP BY TABLE_NAME
ORDER BY TABLE_NAME;

-- This reads catalog counts, rather than scanning large RAW tables.
SELECT TABLE_SCHEMA, TABLE_NAME, ROW_COUNT
FROM KUMO_DEV.INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE = 'BASE TABLE'
  AND (TABLE_SCHEMA = 'RAW_LIM'
       OR (TABLE_SCHEMA IN ('META', 'MONITOR_META')
           AND TABLE_NAME IN ('LIM_BATCH_CTRL', 'RAW_LIM_META', 'RAW_LOAD_RESULTS',
                              '_TMP_SET_READY_LOG', '_SET_READY_LOG',
                              'RAW_LIM_CONTROL_ARCHIVE')))
ORDER BY TABLE_SCHEMA, TABLE_NAME;

SELECT PROCESSED_FL, DELETED_FL, COUNT(*) AS FILES,
       COUNT_IF(DW_BATCH_ID IS NOT NULL) AS WITH_BATCH_ID
FROM KUMO_DEV.META.LIM_BATCH_CTRL
GROUP BY PROCESSED_FL, DELETED_FL;

SELECT DW_TABLE_NM, COUNT(*) AS FILES,
       COUNT_IF(DW_FILE_NM LIKE '%/%') AS WITH_PATH,
       COUNT_IF(SUBSTR(DW_FILE_NM, 8, 3) = '000') AS CONTROL_FILES
FROM KUMO_DEV.META.LIM_BATCH_CTRL
GROUP BY DW_TABLE_NM
ORDER BY DW_TABLE_NM;

SELECT COUNT(*) AS ACTIVE_SOURCE_MAPPINGS,
       COUNT_IF(DLVY_PKG_YEAR IS NOT NULL
                AND DLVY_PKG_YEAR_SEQ_NO IS NOT NULL) AS WITH_SEQUENCE_WATERMARK
FROM KUMO_DEV.MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE
WHERE ACTIVE_FL;

SELECT SOURCE_ID, SUBJECT_AREA, PKG_ID, COUNT(*) AS ACTIVE_MAPPINGS
FROM KUMO_DEV.MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE
WHERE ACTIVE_FL
GROUP BY SOURCE_ID, SUBJECT_AREA, PKG_ID
HAVING COUNT(*) > 1;

-- Inspect task state; this does not resume, suspend or execute tasks.
SHOW TASKS IN DATABASE KUMO_DEV;

SELECT COUNT(*) AS LOG_ROWS,
       COUNT_IF(MESSAGE ILIKE '%DW_READY_TO_LOAD_FL%') AS REMOVED_FLAG_MENTIONS,
       COUNT_IF(MESSAGE ILIKE '%DW_BATCH_ID%') AS REMOVED_BATCH_MENTIONS
FROM KUMO_DEV.MONITOR_META.WORKFLOW_RUN_LOGS;

-- Small populated subject used to verify the empty-file exception.
-- Keep exact physical file names when joining to batch control.
WITH raw_files AS (
    SELECT DW_FILE_NM,
           LEFT(REGEXP_SUBSTR(DW_FILE_NM, '[^/]+$'), 3) AS SOURCE_ID,
           COUNT_IF(LEFT(TRIM(DATA), 2) = '10') AS DATA_ROWS
    FROM KUMO_DEV.RAW_LIM.RAW_LIM_PAAR
    WHERE SUBSTR(REGEXP_SUBSTR(DW_FILE_NM, '[^/]+$'), 8, 3) <> '000'
    GROUP BY DW_FILE_NM
), registered AS (
    SELECT DISTINCT DW_FILE_NM, DW_SRC_SYST
    FROM KUMO_DEV.META.LIM_BATCH_CTRL
    WHERE UPPER(DW_TABLE_NM) IN ('RAW_LIM.RAW_LIM_PAAR',
                               'KUMO_DEV.RAW_LIM.RAW_LIM_PAAR')
)
SELECT COUNT(*) AS RAW_PAYLOAD_FILES,
       COUNT_IF(DATA_ROWS = 0) AS EMPTY_FILES,
       COUNT_IF(ctrl.DW_FILE_NM IS NULL AND DATA_ROWS > 0) AS NONEMPTY_UNREGISTERED
FROM raw_files files
LEFT JOIN registered ctrl
  ON ctrl.DW_FILE_NM = files.DW_FILE_NM
 AND ctrl.DW_SRC_SYST = files.SOURCE_ID;

-- Exercise the proposed state distinction using synthetic records.
-- ACCEPTED_EMPTY stands for durable successful delivery validation; merely
-- containing zero data records must never confer acceptance.
-- Historical registration remains accepted after processing or purging.
WITH fixtures AS (
    SELECT column1 AS CASE_NAME, column2 AS FILE_PRESENT,
           column3 AS DATA_ROWS, column4 AS REGISTERED,
           column5 AS ACCEPTED_EMPTY, column6 AS PROCESSED,
           column7 AS DELETED
    FROM VALUES
        ('new_file', TRUE, 10, FALSE, FALSE, FALSE, FALSE),
        ('queued', TRUE, 10, TRUE, FALSE, FALSE, FALSE),
        ('processed', TRUE, 10, TRUE, FALSE, TRUE, FALSE),
        ('purged', FALSE, 10, TRUE, FALSE, TRUE, TRUE),
        ('empty_unvalidated', TRUE, 0, FALSE, FALSE, FALSE, FALSE),
        ('empty_validated', TRUE, 0, FALSE, TRUE, FALSE, FALSE),
        ('partial_delivery', TRUE, 10, TRUE, FALSE, FALSE, FALSE),
        ('partial_delivery', TRUE, 10, FALSE, FALSE, FALSE, FALSE),
        ('missing_required_file', FALSE, 10, FALSE, FALSE, FALSE, FALSE)
), actual AS (
    SELECT CASE_NAME,
           MIN(IFF(REGISTERED OR
                   (FILE_PRESENT AND DATA_ROWS = 0 AND ACCEPTED_EMPTY), 1, 0)) = 1
               AS DELIVERY_ACCEPTED,
           COUNT_IF(FILE_PRESENT AND DATA_ROWS > 0 AND REGISTERED
                    AND NOT PROCESSED AND NOT DELETED) AS QUEUED_FILES
    FROM fixtures
    GROUP BY CASE_NAME
), expected AS (
    SELECT column1 AS CASE_NAME, column2 AS DELIVERY_ACCEPTED,
           column3 AS QUEUED_FILES
    FROM VALUES
        ('new_file', FALSE, 0),
        ('queued', TRUE, 1),
        ('processed', TRUE, 0),
        ('purged', TRUE, 0),
        ('empty_unvalidated', FALSE, 0),
        ('empty_validated', TRUE, 0),
        ('partial_delivery', FALSE, 1),
        ('missing_required_file', FALSE, 0)
)
SELECT actual.CASE_NAME, actual.DELIVERY_ACCEPTED, actual.QUEUED_FILES,
       actual.DELIVERY_ACCEPTED = expected.DELIVERY_ACCEPTED
           AND actual.QUEUED_FILES = expected.QUEUED_FILES AS PASSED
FROM actual
JOIN expected ON actual.CASE_NAME = expected.CASE_NAME
ORDER BY actual.CASE_NAME;
