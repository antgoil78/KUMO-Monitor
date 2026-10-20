# Snowflake definitions

This directory stores the current live definitions needed by KUMO Monitor and
its workflow/LIM backend. It complements the incremental patches in
`backend/migrations/` with complete SQL definitions for a future account move.

The DEV readiness investigation and implementation contract are recorded in
[`dev-lim-readiness-plan.json`](../review/dev-lim-readiness-plan.json).
[`checks/dev_lim_readiness.sql`](checks/dev_lim_readiness.sql) contains the
read-only catalog/count checks and eight synthetic state cases used during
preparation. It does not call loaders or workflows or change task state.
The investigation found populated DEV RAW/batch-control tables and started
tasks, while the monitor's validation/history tables were empty. The subsequent
DEV implementation is described below. Full definitions describe the live
deployed objects; investigation observations and deployment receipts are kept
separately.

## DEV readiness without RAW flags

The DEV RAW subject tables contain six columns; neither `DW_READY_TO_LOAD_FL`
nor `DW_BATCH_ID` is required there. Readiness registration uses the existing
`KUMO_DEV.META.LIM_BATCH_CTRL` key `(DW_TABLE_NM, DW_FILE_NM, DW_SRC_SYST)`.
Ordinary validation inserts missing nonempty payload files without resetting
existing batch IDs, processing times or processed/deleted flags. dbt continues
to own processing state and `DW_BATCH_ID` in batch control.

Valid empty payloads are accepted through the existing current/history delivery
logs and never enter the dbt queue. Control files are archived by exact filename
only after the control and all its expected payload deliveries pass. The archive
retains its optional compatibility columns using derived values. Registration,
archiving/deletion, success logs, metadata display flags and sequence advancement
commit together. A refresh replaces the current subject metadata snapshot, so
an empty refresh cannot leave an old candidate set available for validation.

[`apply_dev_lim_readiness.py`](../backend/migrations/apply_dev_lim_readiness.py)
prepares the incremental SQL by default. `--apply` replaces only the three DEV
readiness/loading procedures, preserves grants, verifies their bodies and runs
the DDL exporter. Each updated procedure rejects a database argument other than
`KUMO_DEV`. The migration never invokes a production loader or workflow, edits
existing batch records or changes task state.

```bash
python backend/migrations/apply_dev_lim_readiness.py
python backend/migrations/apply_dev_lim_readiness.py --apply
```

The loader's explicit ready bypass registers selected nonempty payloads instead
of updating RAW flags. Explicit partial reload clears batch-control and archived
control records only for selected files; full reload clears them for the subject.
The existing sequence-reset option and SDL rebuild behavior remain in effect.
Keep sequence reset enabled when replaying older deliveries through normal
validation. The loader returns `FILES_REGISTERED` and `CONTROL_ARCHIVE_DELETED`;
the legacy `READY_ROWS_UPDATED` result is zero because no RAW flags are changed.

The DEV monitor derives status from batch registration and successful empty-file
delivery history, retaining `DW_READY_TO_LOAD_FL` in metadata/API responses.
`ROWS_UPDATED` reports payload data rows accepted for a delivery, and the DEV UI
labels that count accordingly. TST and the unset-environment path retain their
existing queries and semantics.

The explicit live checks in
[`live_dev_lim_readiness.py`](../backend/tests/live_dev_lim_readiness.py) and
[`live_dev_lim_readiness_queries.py`](../backend/tests/live_dev_lim_readiness_queries.py)
create disposable DEV schemas containing only synthetic fixtures, then remove
them. They do not call production loaders or workflows. Results are recorded in
[`dev-lim-readiness-live-tests.json`](../review/dev-lim-readiness-live-tests.json)
and [`dev-lim-readiness-query-tests.json`](../review/dev-lim-readiness-query-tests.json).
Deployment receipts identify the three updated procedures and the DEV service
image. The current DEV service update SQL is
[`spcs/alter_dev_service.sql`](spcs/alter_dev_service.sql).

**Whenever an object is changed in Snowflake, always update its SQL definition
in Git in the same change.** After a direct Snowsight edit, refresh this snapshot,
review the diff and commit it. For changes deployed from Git, keep the full
definition and the applicable migration script consistent.

## Scope and layout

`export_scope.json` includes these schemas:

| Schema | Purpose |
| --- | --- |
| `KUMO_ADMIN.WORKFLOW_MANAGER` | Workflow engine, app configuration, audit, notifications and LIM administration |
| `KUMO_ADMIN.META` | Shared metadata procedures and the dbt query-history helper |
| `KUMO_TST.META` | Model/workflow status, execution metadata and stage-loading procedures |
| `KUMO_TST.RAW_LIM` | LIM table definitions, readiness/loading procedures and support logs |
| `KUMO_TST.MONITOR_APP` | New workflow/job orchestration model |
| `KUMO_TST.MONITOR_META` | New metadata namespace, including when it has no objects yet |
| `KUMO_DEV.MONITOR_APP` | DEV workflow engine procedures and suspended dispatcher |
| `KUMO_DEV.MONITOR_META` | DEV monitor metadata, queue stream and LIM loader procedure |
| `KUMO_DEV.META` | Existing DEV data-platform metadata and file format, plus the reviewed readiness log |

Each object has its own SQL file under `ddl/<database>/<schema>/<object_type>/`.
Supporting streams and file formats are included: the master dispatcher needs
`WORKFLOW_EXECUTION_QUEUE_STREAM`, and LIM loaders need the `KUMO_LIM` format.
Overloaded procedures/functions have a signature hash in the filename; their
full signature appears in the header and in `manifest.json`. The manifest records
the source account, export time, object counts and restore order. Tables are
ordered so referenced parent tables are created before their children.

`TASK_WF_MASTER_DISPATCHER` is persistent and included. Generated
`TASK_WF_<eight-hex-digits>` tasks, their `_SCHEDULER` variants and `TMP_WF_*`
run tasks are excluded. Their creation logic remains in the exported procedures
and application code. Workflow-related configuration tables are included.

The snapshot includes the existing backup/legacy objects in these schemas so
they remain available during migration. It contains definitions only, without
table rows or credentials. It does not export the full SDL/EDV/BDL warehouse,
dbt project files, SPCS deployment resources or cloud/notification integrations.

## Refresh from Snowflake

Use Python 3.11 and the dependencies from the repository's `requirements.txt`.
The exporter reads the local ignored `.env` file and accepts the same password
credentials as local development. Use the environment for a current MFA
passcode if one is required; do not put credentials in SQL or source control.

From the repository root:

```bash
python snowflake/export_ddl.py
git diff -- snowflake/
```

If needed, select a role that can see all objects in the configured scope:

```bash
python snowflake/export_ddl.py --role KUMO_ADMIN
```

The exporter reads object metadata and
[`GET_DDL`](https://docs.snowflake.com/en/sql-reference/functions/get_ddl).
It never executes the exported definitions or alters source objects. It gathers
the complete export before updating the local snapshot and removes old exported
files only when their objects no longer exist in the selected scope.

Review newly exported procedure bodies for account-specific settings before
committing. If a definition is edited locally, deploy it and refresh the snapshot
before committing so the saved SQL and live definition agree.

## Restore into a new account

1. Provision target owner/application roles and warehouses, including the
   warehouse names referenced by tasks. Set up the external LIM stage and its
   cloud access, email/notification integrations, dbt projects and
   the data/model schemas required by procedure bodies. These account resources
   need destination-specific setup.
2. Keep the database/schema names used here, or update both the definitions and
   their references in procedure bodies, task SQL and application configuration.
   Fully qualified creation names do not rewrite references inside SQL bodies.
3. Build a combined SQL script from the saved snapshot, without connecting to
   Snowflake:

   ```bash
   python snowflake/export_ddl.py --build-restore /tmp/kumo-monitor-restore.sql
   ```

4. Review and run that script in the new account as the intended owner role. It
   creates databases/schemas, sequences, file formats, tables, functions, views,
   streams, procedures and persistent tasks in the order recorded in
   `manifest.json`. These definitions
   use `CREATE OR REPLACE` and are intended for a new account with empty objects.
5. Migrate the required configuration/table contents separately. Workflow/job
   definitions, dependencies, application parameters, notification groups and
   source mappings are table data; the DDL alone does not restore them. Let the
   engine/application regenerate workflow tasks rather than copying run tasks.
6. Reapply destination grants. The snapshot does not capture account roles or
   object grants. After deploying the SPCS service, the existing
   [`grant_monitor_orchestration_access.sql`](../backend/migrations/grant_monitor_orchestration_access.sql)
   sets access for `MONITOR_APP` and `MONITOR_META`; provision the remaining
   engine, metadata, warehouse and integration grants for the intended roles.
7. Validate the application and procedures, then explicitly resume persistent
   tasks when the destination is ready. Task creation does not restore their
   running state; the original state is recorded in `manifest.json`.

The restore bundle is generated outside this directory to keep Git focused on
individual object definitions. No SQL in this directory is automatically applied
by the container build or deployment workflow.

## DEV workbook migration

The reviewed workbook `kumo-monitor-object-review_updated.xlsx` is represented
by [`dev_workbook_object_plan.json`](../backend/migrations/dev_workbook_object_plan.json),
including its SHA-256 digest. The plan contains 34 creation targets:
20 table definitions, 11 procedure overloads, one stream, one external stage
and one persistent task. It creates no table contents or service resources.

[`apply_dev_workbook_objects.py`](../backend/migrations/apply_dev_workbook_objects.py)
reads fresh source DDL and prepares
[`create_dev_workbook_objects.sql`](../backend/migrations/create_dev_workbook_objects.sql).
Without `--apply`, it only prepares the SQL and inspects existing DEV objects.
With `--apply`, it creates missing objects in `KUMO_DEV` using create-only DDL.
It skips an existing overload at the requested location and stops if a matching
object name exists in another DEV schema, rather than creating a duplicate.
It never replaces, removes or modifies a pre-existing DEV object, nor writes
to `KUMO_ADMIN` or `KUMO_TST`.

```bash
python backend/migrations/apply_dev_workbook_objects.py
python backend/migrations/apply_dev_workbook_objects.py --apply
```

The workbook's explicit locations take precedence: `RAW_LIM_META` and
`_SET_READY_LOG` belong in `KUMO_DEV.MONITOR_META`, while `_TMP_SET_READY_LOG`
belongs in `KUMO_DEV.META`. Existing `META.LIM_BATCH_CTRL`,
`META.WORKFLOW_STATUS`, `META.MODEL_STATUS` and the `META.KUMO_LIM` file format
are reused. They are not copied into monitor schemas.

Owner-rights procedures use database-relative schema/object references. The
two `SP_RUN_WORKFLOW` overloads capture `CURRENT_DATABASE()` and put that
database in their generated execution task names and execution contexts.
The caller-rights rule evaluator and RAW metadata refresher instead have an
explicit trailing `P_DATABASE VARCHAR DEFAULT 'KUMO_DEV'` parameter, because
caller rights inherit the caller's current database. Their database parameter
is validated as an identifier. Existing calls using the original argument
counts can continue using the DEV default. No DEV procedure body contains a
hard-coded `KUMO_TST` or `KUMO_ADMIN` reference.

The external stage is created using `CREATE STAGE ... CLONE` to copy its
parameters without putting cloud credentials in source files. This is another
reference to the same external storage, not a copy of staged files. The
migration does not list, load, copy, remove or otherwise change staged files.
The external stage's credential-bearing definition is outside the generic
DDL export; its creation recipe is retained in the migration.

The master dispatcher is created suspended. The migration does not call any
workflow procedure, resume or execute a task, or instantiate generated
workflow scheduler/run tasks. Generated tasks remain excluded from the DDL
snapshot. Workflow configuration is handled by the follow-up below; service
deployment remains a later step.
`META.WORKFLOW_DEP_RULE` remains an unresolved dependency: its workbook row
was still marked for ownership review, so this migration does not create it.

After any creations, including a partial migration, the script adds
`KUMO_DEV.MONITOR_APP`, `KUMO_DEV.MONITOR_META` and `KUMO_DEV.META` to the
export scope, explicitly preserves the DEV dispatcher, and runs
`snowflake/export_ddl.py`. Review the resulting Git diff. The result receipt
is written to `review/dev-workbook-migration-result.json` on success.

## DEV LIM loader follow-up

The subsequent schema decision places both `RAW_LOAD_RESULTS` and
`LOAD_RAW_LIM_FROM_STAGE` in `KUMO_DEV.MONITOR_META`.
[`create_dev_lim_loader.py`](../backend/migrations/create_dev_lim_loader.py)
prepares their create-only definitions; `--apply` creates only missing objects
and refreshes the full DDL snapshot. The migration preserves the TST originals,
copies no rows, never invokes the loader, and does not change tasks or
`WORKFLOW_DEP_RULE`. It refuses duplicate names in other DEV schemas or an
existing loader with a different signature.

```bash
python backend/migrations/create_dev_lim_loader.py
python backend/migrations/create_dev_lim_loader.py --apply
```

The loader preserves caller rights and adds a trailing optional
`P_DATABASE VARCHAR DEFAULT 'KUMO_DEV'`. All data, temporary table, catalog,
stage, file-format and SDL references are bound to that explicit database;
the caller's database/schema does not select reload targets. Metadata uses
`MONITOR_META`, except the existing `META.LIM_BATCH_CTRL` and the workbook's
`META._TMP_SET_READY_LOG`. RAW subject data remains in `RAW_LIM`.

The application selects the new loader when `KUMO_ENV` is set and passes its
database explicitly. `KUMO_LIM_LOAD_PROCEDURE` can override the procedure name
within that environment. With `KUMO_ENV` unset, the existing
`KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE` six-argument call remains the default.
No running service is changed by this repository update.

## DEV workflow configuration and support procedures

[`apply_dev_workflow_configuration.py`](../backend/migrations/apply_dev_workflow_configuration.py)
prepares the following create-only definitions in
[`create_dev_workflow_support.sql`](../backend/migrations/create_dev_workflow_support.sql):

| Schema | Type | Object |
| --- | --- | --- |
| `KUMO_DEV.MONITOR_APP` | Procedure | `SP_RAW_LIM_SET_READY` |
| `KUMO_DEV.MONITOR_APP` | Procedure | `SP_CLEANUP_OLD_TMP_TASKS` |
| `KUMO_DEV.MONITOR_APP` | Procedure | `SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST` |
| `KUMO_DEV.MONITOR_APP` | Procedure | `SP_PURGE_LIM_BATCH` |
| `KUMO_DEV.MONITOR_META` | Empty table | `RAW_LIM_CONTROL_ARCHIVE` |

```bash
python backend/migrations/apply_dev_workflow_configuration.py
python backend/migrations/apply_dev_workflow_configuration.py --apply
```

The first command validates without creating objects or inserting rows. The
second creates only missing definitions and copies configuration into the
existing DEV metadata tables in one transaction. Existing identical rows are
skipped; conflicting IDs, names or relationships stop the migration without
overwriting DEV data. Existing matching objects are skipped; a different
procedure interface stops the migration instead of adding another overload.
Source objects and rows are never changed. Configuration
contents stay in memory and are bound as data, never executed or saved in Git.
The receipt records only object names, counts and validation results.

The copy includes 27 engine workflows, 29 catalog workflows, 44 jobs and their
schedule settings, notifications, email group, application parameters, source
mappings, job assignments and dependencies: 268 rows across ten tables. Three
orphan notification rows are excluded. IDs and graph relationships are preserved.
Executable SQL and dbt references are rewritten for DEV, using the existing
`KUMO_DEV.DBT_APP.KUMO_DEV` project and its actual `kumo_DEV` profile target.
Historical provenance descriptions are retained. Copied schedules are disabled,
old task names and schedule timestamps are cleared, and all DEV tasks remain
suspended. Runtime history, queues, locks and logs are not copied.

Caller-rights procedures retain their rights and use an explicit validated
database parameter, defaulting to DEV. SET_READY and the incremental loader add
an optional trailing `P_DATABASE`; PURGE reuses `DBT_DB`. Original argument counts
continue to work. Metadata uses the reviewed locations, including
`META.LIM_BATCH_CTRL`, `META._TMP_SET_READY_LOG` and the new archive in
`MONITOR_META`; RAW data stays in `RAW_LIM`. The owner-rights cleanup procedure
uses database-relative metadata references and rejects task names outside its
own database's `MONITOR_APP.TMP_WF_` namespace. PURGE similarly rejects targets
outside the selected database's `RAW_LIM` schema.

The migration never calls these procedures, runs workflows, sends email,
creates generated tasks or resumes tasks. It refreshes the DDL snapshot after
any object creations, including partial deployments. Review that Git diff and
`review/dev-workflow-configuration-result.json`. Procedure creation and local
read validation do not establish that loading, dbt execution or purging succeeds.
`WORKFLOW_DEP_RULE`, grants and service deployment remain separate work.

## Earlier procedure-only draft

The DEV-first migration uses the existing `KUMO_DEV.MONITOR_APP` schema for
executable procedures and `KUMO_DEV.MONITOR_META` for monitor-owned table
references. It leaves the existing instance in `KUMO_ADMIN.WORKFLOW_MANAGER`
unchanged. No DEV tables or tasks are created by this procedure migration.

[`create_dev_monitor_procedures.py`](../backend/migrations/create_dev_monitor_procedures.py)
prepares the eight workflow procedure names (ten definitions including both
`SP_RUN_WORKFLOW` and `SP_WORKFLOW_DISPATCH` overloads). Its external targets
are deliberately required rather than inferred from the procedure database:

| Argument | Use |
| --- | --- |
| `--workflow-status-table` | Read/write target for dispatch, dbt completion and dependency evaluation |
| `--model-status-table` | Read-only dbt model status source for dependency evaluation |
| `--execution-namespace` | Explicit `database.schema` context for SQL in generated execution tasks |
| `--execution-warehouse` | Warehouse assigned to generated execution tasks |

The rule configuration table is referenced in `KUMO_DEV.MONITOR_META`; workflow
and model status sources are resolved separately. This removes the original
rule evaluator's dependency on the caller's current database for `META`.
The dbt project FQN and target still come from workflow configuration rows.
These sources and targets must be agreed before deployment or execution;
keeping a table in TST does not imply permission for DEV to write to it.

Use `--build-sql /tmp/kumo-dev-procedures.sql` with all four arguments to
review an offline bundle. Only `--apply` connects and creates procedures. It
checks all saved source definitions against live DDL and refuses to proceed
if any source has drifted or any requested DEV procedure name already exists.
It uses create-only DDL, never calls a procedure, and never replaces or removes
an existing object. Referenced DEV tables are a later migration prerequisite;
creation alone is not validation that the workflow engine can execute.

After creating any definitions, including a partial deployment, the migration
adds both DEV schemas to the export scope and runs `snowflake/export_ddl.py` to
save their full definitions. Review that Git diff before committing. Snowflake
DDL is not rolled back as a group; if creation fails partway through, inspect
the created signatures printed by the migration before preparing a follow-up.

This earlier draft is superseded by the reviewed workbook migration above.
It remains available for reference; it is not used to deploy the workbook
objects. Never put credentials or an MFA passcode in a migration, command
argument or committed file.
