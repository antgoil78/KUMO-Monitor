# Current application

The current local/DEV Flask backend in `backend/` and React app in `frontend/`
are the only maintained application. Use that same code for TST, with
environment configuration. Do not maintain separate legacy, DEV or TST apps,
duplicate entrypoints, or copies of old pages. Historical versions live in Git.

Keep work focused on the requested change. Use the smallest useful migration
and relevant checks; avoid unrelated refactoring, repeated verification, live
account inventories, and backup work that the task does not require.

# Snowflake definitions

Keep DDL copies for `KUMO_DEV.MONITOR_APP` and `KUMO_DEV.MONITOR_META` in
`snowflake/ddl/`. These are the current app definitions and the starting point
for TST. Other data-platform schemas and legacy environments are outside the
maintained DDL copy unless the user explicitly requests them.

Do not refresh or export Snowflake definitions by default. When we change an
object's DDL in either app schema, update only that object's saved definition
in the same change. Keep an incremental migration when needed. A configuration
row/data change, application-code change, investigation, test, or deployment
does not require a DDL refresh.

Run a schema-wide refresh only when the user explicitly asks. Use the targeted
export option for individual objects changed by us. Never add automatic full
exports to migrations or routine checks. See `snowflake/README.md` for commands.

Exclude generated workflow schedulers and temporary run tasks from saved DDL.
Keep the master dispatcher and other static engine tasks. Never commit
credentials or table contents with the DDL copy.

# App releases

Route app releases through Git and `.github/workflows/deploy-image.yml`.
For now, let the existing workflow run automatically when changes are merged
to `main`; do not manually dispatch it for routine changes. The workflow
builds and publishes the image; it does not update the SPCS service.

Do not publish images, change service specifications, or update SPCS directly
unless the user explicitly requests that deployment path.
