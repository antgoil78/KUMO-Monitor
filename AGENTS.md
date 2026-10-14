# Snowflake definitions

When changing a Snowflake table, procedure, function, view, stream, file format,
or persistent task covered by `snowflake/export_scope.json`, always update its
SQL definition in `snowflake/ddl/` in the same change. This applies to changes made directly in
Snowflake as well as changes deployed from this repository.

Use `python snowflake/export_ddl.py` to refresh the saved definitions from the
live account, and review the resulting Git diff. Keep applicable incremental
migrations in `backend/migrations/` alongside the current full definitions.
See `snowflake/README.md` for export and migration instructions.

Do not save generated workflow schedulers or temporary workflow run tasks as
persistent task definitions. Preserve the master dispatcher and other static
engine tasks. Never commit credentials or table contents with the DDL snapshot.
