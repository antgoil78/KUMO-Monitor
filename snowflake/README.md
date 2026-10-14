# Snowflake definitions

This directory stores the current live definitions needed by KUMO Monitor and
its workflow/LIM backend. It complements the incremental patches in
`backend/migrations/` with complete SQL definitions for a future account move.

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
